"""
OrdersService — checkout + read + cancel + deliver flows
(US-B2C-09, US-B2C-10, US-B2C-11, US-B2C-13).

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders,
       b2c-orders-flows.md#b2c-11-cancel-order,
       b2c-orders-flows.md#b2c-13-fulfill
Spec:  b2c/openapi.yaml — POST /api/v1/orders, GET /api/v1/orders,
       GET /api/v1/orders/{id}, POST /api/v1/orders/{id}/cancel,
       POST /api/v1/orders/{id}/deliver

Architecture:
  Idempotency: UNIQUE constraint on orders.idempotency_key.
    - Race condition: two simultaneous POSTs with the same key →
      one wins INSERT, the other catches IntegrityError, reads the committed
      row, returns 200 (not 201). DB enforces atomicity; no Redis needed.
    - ADR options considered:
        A) UNIQUE index (chosen): zero extra infra, DB-atomic, portable.
        B) Separate idempotency_cache table: allows TTL cleanup, but adds
           a join and two writes per request; overkill for low-throughput checkout.
        C) Redis: sub-ms lookup but requires extra infra, TTL management, and
           does NOT prevent DB race without a DB-level lock anyway.

Checkout flow (canon §enrichment, adapted):
  1. Check idempotency_key in DB → return existing order if found.
  2. Validate items: GET /api/v1/public/skus/{sku_id} per item (B2B).
     - Raises ValueError("SKU_NOT_FOUND:{sku_id}") if SKU absent from B2B.
  3. Build price snapshot: unit_price = sku.price - sku.discount (≥ 0).
  4. Pre-generate order_id (UUID4).
  5. POST /api/v1/inventory/reserve {idempotency_key, order_id, items}.
     - B2B 4xx → ValueError("RESERVE_FAILED:{body}").
     - httpx connection/timeout error → re-raised as httpx.ConnectError / TimeoutException.
  6. Insert Order + OrderItems in one transaction.
  7. On IntegrityError (race on idempotency_key) → read and return the winner's row.

Status is immediately PAID (mock payment — no real gateway).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_BASE_URL, B2C_TO_B2B_KEY

logger = logging.getLogger(__name__)
from backend.modules.orders.models import Order, OrderItem
from backend.modules.orders.schemas import (
    OrderCreateRequest,
    OrderItemSchema,
    OrderResponse,
    OrderStatus,
    PaginatedOrdersResponse,
)


# ──────────────────────────────────────────────────────────────────────────────
# B2B helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _fetch_sku(
    sku_id: UUID,
    b2b_base_url: str,
    service_key: str,
) -> dict[str, Any]:
    """
    GET /api/v1/public/skus/{sku_id} → SKUPublicResponse dict.
    Returns only MODERATED SKUs with active_quantity > 0.
    Raises ValueError("SKU_NOT_FOUND:...") on 404.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/public/skus/{sku_id}",
            headers={"X-Service-Key": service_key},
        )
    if resp.status_code == 404:
        raise ValueError(f"SKU_NOT_FOUND:{sku_id}")
    resp.raise_for_status()
    return resp.json()


async def _reserve(
    idempotency_key: str,
    order_id: UUID,
    items: list[dict[str, Any]],
    b2b_base_url: str,
    service_key: str,
) -> None:
    """
    POST /api/v1/inventory/reserve → all-or-nothing reserve.
    Raises ValueError("RESERVE_FAILED:...") on B2B 4xx (reserve impossible).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/inventory/reserve",
            json={
                "idempotency_key": idempotency_key,
                "order_id": str(order_id),
                "items": items,
            },
            headers={"X-Service-Key": service_key},
        )
    if resp.status_code >= 400:
        body = ""
        try:
            body = str(resp.json())
        except Exception:
            pass
        raise ValueError(f"RESERVE_FAILED:{resp.status_code}:{body}")
    # 200 / 201 / 204 — all fine


async def _fulfill(
    order_id: UUID,
    items: list[dict[str, Any]],
    b2b_base_url: str,
    service_key: str,
) -> None:
    """
    POST /api/v1/inventory/fulfill — inform B2B that the order was delivered.
    B2B deducts reserved_quantity; idempotent by order_id on the B2B side.
    Raises httpx.ConnectError / TimeoutException on network failure
    (caller logs and proceeds — order stays DELIVERED; retry scaffold via Celery TBD).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/inventory/fulfill",
            json={
                "order_id": str(order_id),
                "items": items,
            },
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()


async def _unreserve(
    order_id: UUID,
    items: list[dict[str, Any]],
    b2b_base_url: str,
    service_key: str,
) -> None:
    """
    POST /api/v1/inventory/unreserve — release reserved stock on order cancellation.

    Idempotent by order_id (per B2B spec). No idempotency_key required.
    Raises httpx.ConnectError / TimeoutException on network failure (caller maps to CANCEL_PENDING).
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/inventory/unreserve",
            json={
                "order_id": str(order_id),
                "items": items,
            },
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()


# ──────────────────────────────────────────────────────────────────────────────
# DB read helper
# ──────────────────────────────────────────────────────────────────────────────


async def _load_order(db: AsyncSession, order: Order) -> OrderResponse:
    """Load OrderItems for `order` and build OrderResponse."""
    items_result = await db.execute(
        select(OrderItem).where(OrderItem.order_id == order.id)
    )
    db_items = items_result.scalars().all()
    return OrderResponse(
        id=order.id,
        buyer_id=order.buyer_id,
        status=OrderStatus(order.status),
        items=[
            OrderItemSchema(
                sku_id=it.sku_id,
                product_id=it.product_id,
                name=it.name,
                quantity=it.quantity,
                unit_price=it.unit_price,
                line_total=it.line_total,
            )
            for it in db_items
        ],
        subtotal=order.subtotal,
        total=order.total,
        address=order.delivery_address,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


async def _load_orders_bulk(
    db: AsyncSession, orders: list[Order]
) -> list[OrderResponse]:
    """Load OrderItems for a list of orders in one query and build responses."""
    if not orders:
        return []
    order_ids = [o.id for o in orders]
    items_result = await db.execute(
        select(OrderItem).where(OrderItem.order_id.in_(order_ids))
    )
    all_items = items_result.scalars().all()

    # Group items by order_id
    items_map: dict[Any, list[OrderItem]] = {}
    for it in all_items:
        items_map.setdefault(it.order_id, []).append(it)

    responses = []
    for order in orders:
        order_items = items_map.get(order.id, [])
        responses.append(
            OrderResponse(
                id=order.id,
                buyer_id=order.buyer_id,
                status=OrderStatus(order.status),
                items=[
                    OrderItemSchema(
                        sku_id=it.sku_id,
                        product_id=it.product_id,
                        name=it.name,
                        quantity=it.quantity,
                        unit_price=it.unit_price,
                        line_total=it.line_total,
                    )
                    for it in order_items
                ],
                subtotal=order.subtotal,
                total=order.total,
                address=order.delivery_address,
                created_at=order.created_at,
                updated_at=order.updated_at,
            )
        )
    return responses


async def _find_by_idempotency_key(
    db: AsyncSession, key: str
) -> Order | None:
    result = await db.execute(
        select(Order).where(Order.idempotency_key == key)
    )
    return result.scalar_one_or_none()


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────


class OrdersService:
    @staticmethod
    async def create_order(
        db: AsyncSession,
        *,
        buyer_id: UUID,
        idempotency_key: str,
        payload: OrderCreateRequest,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> tuple[OrderResponse, bool]:
        """
        Create a new order or return an existing one for the same idempotency key.

        Returns:
          (order_response, is_new) where is_new=True → 201, is_new=False → 200.

        Raises:
          ValueError("SKU_NOT_FOUND:...") → 404
          ValueError("RESERVE_FAILED:...") → 409
          httpx errors (ConnectError, TimeoutException, …) → caller maps to 503
        """
        # Step 1 — idempotency check (fast path)
        existing = await _find_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return await _load_order(db, existing), False

        # Step 2 — validate SKUs and snapshot prices from B2B
        enriched: list[dict[str, Any]] = []
        for req_item in payload.items:
            sku_data = await _fetch_sku(req_item.sku_id, b2b_base_url, service_key)
            unit_price = max(
                0,
                (sku_data.get("price") or 0) - (sku_data.get("discount") or 0),
            )
            enriched.append(
                {
                    "sku_id": req_item.sku_id,
                    "product_id": UUID(sku_data["product_id"]),
                    "name": sku_data.get("name") or sku_data.get("title") or "",
                    "quantity": req_item.quantity,
                    "unit_price": unit_price,
                    "line_total": unit_price * req_item.quantity,
                }
            )

        subtotal = sum(e["line_total"] for e in enriched)

        # Step 3 — B2B all-or-nothing reserve
        order_id = uuid.uuid4()
        reserve_items = [
            {"sku_id": str(e["sku_id"]), "quantity": e["quantity"]}
            for e in enriched
        ]
        await _reserve(idempotency_key, order_id, reserve_items, b2b_base_url, service_key)

        # Step 4 — persist order
        order = Order(
            id=order_id,
            buyer_id=buyer_id,
            idempotency_key=idempotency_key,
            status=OrderStatus.PAID.value,
            delivery_address=payload.delivery_address,
            payment_method_id=payload.payment_method_id,
            subtotal=subtotal,
            total=subtotal,  # no extra fees (mock)
        )
        db.add(order)

        for e in enriched:
            db.add(
                OrderItem(
                    order_id=order_id,
                    sku_id=e["sku_id"],
                    product_id=e["product_id"],
                    name=e["name"],
                    quantity=e["quantity"],
                    unit_price=e["unit_price"],
                    line_total=e["line_total"],
                )
            )

        try:
            await db.commit()
        except IntegrityError:
            # Race: another request committed the same idempotency_key first.
            await db.rollback()
            winner = await _find_by_idempotency_key(db, idempotency_key)
            if winner is None:
                raise  # should not happen, but don't swallow
            return await _load_order(db, winner), False

        return await _load_order(db, order), True

    @staticmethod
    async def list_orders(
        db: AsyncSession,
        *,
        buyer_id: UUID,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedOrdersResponse:
        """
        Return paginated list of the buyer's own orders.

        Spec: GET /api/v1/orders → PaginatedOrders
          - Filtered by buyer_id from JWT (IDOR-safe — never from query param)
          - Optional status filter
          - Sorted by created_at DESC (most recent first)
          - Returns full OrderResponse per item (spec shape, not summary)

        Canon: b2c-orders-flows.md#b2c-10-view-orders
        """
        from sqlalchemy import func

        base_where = [Order.buyer_id == buyer_id]
        if status is not None:
            base_where.append(Order.status == status)

        # COUNT
        count_result = await db.execute(
            select(func.count()).select_from(Order).where(*base_where)
        )
        total_count: int = count_result.scalar_one()

        # PAGE
        orders_result = await db.execute(
            select(Order)
            .where(*base_where)
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        orders = list(orders_result.scalars().all())

        items = await _load_orders_bulk(db, orders)
        return PaginatedOrdersResponse(
            items=items,
            total_count=total_count,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    async def get_order(
        db: AsyncSession,
        *,
        order_id: UUID,
        buyer_id: UUID,
    ) -> OrderResponse:
        """
        Return a single order belonging to buyer_id.

        IDOR rule (canon b2c-orders-flows.md#b2c-10-view-orders §Authorization):
          Filter includes BOTH id AND buyer_id in one query.
          If the row doesn't exist OR belongs to another user → 404, never 403.
          This prevents timing-based enumeration of order existence.

        ADR — IDOR protection options:
          A) WHERE id = ? AND buyer_id = ? (chosen):
             Single query, naturally returns None for wrong user.
             Attacker cannot distinguish "not found" from "not yours" —
             no information leak, no timing side-channel from a second query.
          B) get(id=?) + compare owner:
             Two round-trips; also returns None/404, but takes slightly longer
             than Option A when the order exists but belongs to another user,
             creating a measurable timing difference that leaks existence.
          C) Permission class / middleware (e.g. DRF-style):
             Adds framework coupling; underlying DB query is still one of A or B.
             More abstraction, same result as A when implemented correctly.

          Criteria: 1) security (no existence leak, no timing side-channel),
                    2) code clarity (one self-documenting query).
          Winner: Option A.

        Raises:
          ValueError("ORDER_NOT_FOUND") when order doesn't exist or belongs to another user.
        """
        result = await db.execute(
            select(Order).where(
                Order.id == order_id,
                Order.buyer_id == buyer_id,  # IDOR guard in same query
            )
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise ValueError("ORDER_NOT_FOUND")
        return await _load_order(db, order)

    @staticmethod
    async def cancel_order(
        db: AsyncSession,
        *,
        order_id: UUID,
        buyer_id: UUID,
        reason: str | None = None,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> OrderResponse:
        """
        Cancel an order: call B2B unreserve, set status CANCELLED (or CANCEL_PENDING on failure).

        Canon flow (b2c-orders-flows.md#b2c-11-cancel-order):
          1. Ownership + status guard (same-query IDOR pattern from get_order).
          2. Load OrderItems to build the unreserve payload.
          3. POST /api/v1/inventory/unreserve {order_id, items}.
          4. On success  → status = CANCELLED.
          5. On network error → status = CANCEL_PENDING (scaffold; Celery retry TBD).

        Cancellable statuses (canon/DoD): CREATED, PAID.
          ASSEMBLING and later are non-cancellable → 409 CANCEL_NOT_ALLOWED.
          Note: spec description lists ASSEMBLING as cancellable; DoD test
          cancel_assembling_order_returns_409 takes precedence as the explicit rule.

        IDOR: wrong-user or nonexistent order → ValueError("ORDER_NOT_FOUND") → 404.
        Status guard: non-cancellable status → ValueError("CANCEL_NOT_ALLOWED:{status}") → 409.

        Async retry scaffold:
          CANCEL_PENDING orders should be retried by a Celery worker (see ADR in PR).
          On this iteration, failure is logged and the status is persisted as CANCEL_PENDING
          without triggering a retry task (acceptable per DoD "первая итерация").
        """
        # Step 1 — ownership check (IDOR-safe: buyer_id in WHERE clause)
        order_result = await db.execute(
            select(Order).where(
                Order.id == order_id,
                Order.buyer_id == buyer_id,
            )
        )
        order = order_result.scalar_one_or_none()
        if order is None:
            raise ValueError("ORDER_NOT_FOUND")

        # Step 2 — status guard
        _CANCELLABLE = {"CREATED", "PAID"}
        if order.status not in _CANCELLABLE:
            raise ValueError(f"CANCEL_NOT_ALLOWED:{order.status}")

        # Step 3 — load items for unreserve payload
        items_result = await db.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )
        order_items = items_result.scalars().all()
        unreserve_items = [
            {"sku_id": str(it.sku_id), "quantity": it.quantity}
            for it in order_items
        ]

        # Step 4 — call B2B unreserve
        new_status: str
        try:
            await _unreserve(order_id, unreserve_items, b2b_base_url, service_key)
            new_status = "CANCELLED"
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            # Scaffold: log failure, set CANCEL_PENDING without Celery retry yet.
            # Production: enqueue a Celery task here with exponential backoff.
            logger.warning(
                "Unreserve failed for order %s, setting CANCEL_PENDING: %s",
                order_id,
                exc,
            )
            new_status = "CANCEL_PENDING"

        # Step 5 — persist new status
        order.status = new_status
        order.updated_at = datetime.now(timezone.utc)
        await db.commit()

        return await _load_order(db, order)

    @staticmethod
    async def deliver_order(
        db: AsyncSession,
        *,
        order_id: UUID,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> OrderResponse:
        """
        Mark an order as DELIVERED and trigger B2B inventory/fulfill.

        Called by an internal/admin endpoint (X-Service-Key auth), not by the buyer.
        This is the US-B2C-13 trigger — the equivalent of a post_save signal in Django.

        Canon flow (b2c-orders-flows.md#b2c-13-fulfill):
          1. Fetch order by id (no buyer_id guard — this is an admin operation).
          2. Check order is not already terminal (CANCELLED, CANCEL_PENDING) → 409.
          3. If already DELIVERED and fulfill_completed_at is set → idempotent return (skip B2B).
          4. Transition status to DELIVERED, persist.
          5. POST /api/v1/inventory/fulfill {order_id, items}.
          6. On B2B success → set fulfill_completed_at = now().
          7. On B2B network failure → log error, fulfill_completed_at remains NULL
             (retry scaffold: a Celery worker should pick up rows WHERE status=DELIVERED
              AND fulfill_completed_at IS NULL). Order stays DELIVERED — the buyer gets
              their goods regardless of a temporary B2B outage.

        Idempotency (DoD test repeated_fulfill_idempotent):
          If fulfill_completed_at is already set, skip the B2B call and return the order.
          This prevents double-fulfill when deliver is called twice with B2B succeeding
          on the first call. Uses fulfill_completed_at as the single-truth flag.

        ADR — trigger mechanism options:
          A) Admin endpoint POST /orders/{id}/deliver + X-Service-Key (chosen):
             Clean separation: delivery confirmation arrives from a logistics/admin service.
             No background thread: trigger is synchronous in request context, fully testable
             by mocking httpx without any Celery/worker infrastructure.
             Double-trigger risk: mitigated by fulfill_completed_at column checked before
             calling B2B; second call is a no-op if first succeeded.
          B) FastAPI startup background task / APScheduler polling:
             Polls for DELIVERING orders older than N minutes. Harder to test (timing-based).
             Risk: poll interval adds latency; no natural idempotency boundary.
          C) SQLAlchemy event.listen on attribute_set / after_flush:
             Couples persistence to HTTP side-effect. Hard to mock in tests without
             monkeypatching module-level globals. Breaks unit test isolation.
          D) Outbox pattern (recommended for production):
             Persist OutboxEvent row in the same transaction; separate worker reads & sends.
             Zero double-trigger risk, retryable. Out of scope for first iteration.

          Criteria: testability without admin UI, low double-trigger risk, no Celery infra now.
          Winner: Option A (endpoint), with Outbox as the stated next step.

        Raises:
          ValueError("ORDER_NOT_FOUND") → 404
          ValueError("DELIVER_NOT_ALLOWED:{status}") → 409
        """
        # Step 1 — fetch order (admin — no buyer_id filter)
        result = await db.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if order is None:
            raise ValueError("ORDER_NOT_FOUND")

        # Step 2 — terminal status guard (can't deliver a cancelled order)
        _TERMINAL = {"CANCELLED", "CANCEL_PENDING"}
        if order.status in _TERMINAL:
            raise ValueError(f"DELIVER_NOT_ALLOWED:{order.status}")

        # Step 3 — idempotency: already delivered AND fulfill acknowledged → skip B2B
        if order.status == "DELIVERED" and order.fulfill_completed_at is not None:
            return await _load_order(db, order)

        # Step 4 — load items for fulfill payload
        items_result = await db.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )
        order_items = items_result.scalars().all()
        fulfill_items = [
            {"sku_id": str(it.sku_id), "quantity": it.quantity}
            for it in order_items
        ]

        # Step 5 — persist DELIVERED status
        order.status = "DELIVERED"
        order.updated_at = datetime.now(timezone.utc)
        await db.commit()

        # Step 6 — call B2B fulfill (fire-and-forget with error logging)
        try:
            await _fulfill(order_id, fulfill_items, b2b_base_url, service_key)
            order.fulfill_completed_at = datetime.now(timezone.utc)
            await db.commit()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            # Scaffold: order stays DELIVERED; retry via Celery worker TBD.
            # Worker query: SELECT * FROM orders WHERE status='DELIVERED' AND fulfill_completed_at IS NULL
            logger.warning(
                "B2B fulfill failed for order %s — fulfill_completed_at not set, retry pending: %s",
                order_id,
                exc,
            )

        return await _load_order(db, order)
