"""
OrdersService — checkout flow (US-B2C-09).

Canon: b2c-cart-flows.md#b2c-09-checkout
Spec:  b2c/openapi.yaml — POST /api/v1/orders

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

import uuid
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.config import B2B_BASE_URL, B2C_TO_B2B_KEY
from backend.modules.orders.models import Order, OrderItem
from backend.modules.orders.schemas import (
    OrderCreateRequest,
    OrderItemSchema,
    OrderResponse,
    OrderStatus,
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
    )


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
