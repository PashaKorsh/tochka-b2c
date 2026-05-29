"""
Tests for US-B2C-12 — B2B Event handling (POST /api/v1/b2b/events).

Spec: b2c/openapi.yaml — POST /api/v1/b2b/events
Canon: b2c-orders-flows.md#b2c-12-handle-events

DoD test names (exact):
  product_blocked_marks_cart_items_unavailable
  orders_not_affected_by_product_blocked
  idempotent_event_no_side_effects
  missing_service_key_returns_401

Auth: X-Service-Key header (service-to-service, not Bearer JWT).
DB seeding: CartItem and OrderItem inserted directly via SQLAlchemy.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.main import app
from backend.modules.cart.models import CartItem
from backend.modules.orders.models import Order, OrderItem

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)

_SERVICE_KEY = "dev-service-key"   # matches B2B_TO_B2C_KEY env default


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _db_session():
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def _seed_cart_item(
    db: AsyncSession,
    *,
    user_id: UUID,
    sku_id: UUID,
    product_id: UUID,
    quantity: int = 1,
) -> CartItem:
    item = CartItem(
        id=uuid4(),
        user_id=user_id,
        session_id=None,
        sku_id=sku_id,
        product_id=product_id,
        quantity=quantity,
    )
    db.add(item)
    await db.commit()
    return item


async def _seed_order_with_item(
    db: AsyncSession,
    *,
    buyer_id: UUID,
    sku_id: UUID,
    product_id: UUID,
) -> tuple[Order, OrderItem]:
    import json as _json
    from datetime import datetime, timezone as _tz
    addr_id = uuid4()
    addr_snap = _json.dumps({
        "id": str(addr_id), "country": "RU", "city": "Москва",
        "street": "Тестовая", "building": "1",
        "created_at": datetime.now(_tz.utc).isoformat(),
    })
    order = Order(
        id=uuid4(),
        buyer_id=buyer_id,
        idempotency_key=str(uuid4()),
        status="PAID",
        address_id=addr_id,
        address_snapshot=addr_snap,
        payment_method_id=uuid4(),
        subtotal=1000,
        total=1000,
    )
    db.add(order)
    await db.flush()

    item = OrderItem(
        order_id=order.id,
        sku_id=sku_id,
        product_id=product_id,
        name="Test Product",
        quantity=1,
        unit_price=1000,
        line_total=1000,
    )
    db.add(item)
    await db.commit()
    return order, item


def _event_headers(service_key: str = _SERVICE_KEY) -> dict:
    return {"X-Service-Key": service_key}


def _blocked_event(product_id: UUID, idempotency_key: UUID | None = None) -> dict:
    return {
        "event_type": "PRODUCT_BLOCKED",
        "idempotency_key": str(idempotency_key or uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "product_id": str(product_id),
            "reason": "Content policy violation",
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_product_blocked_marks_cart_items_unavailable():
    """
    POST /api/v1/b2b/events with event_type=PRODUCT_BLOCKED
    → all cart_items with that product_id get unavailable_reason set.

    Verifies:
    - 202 response
    - body.accepted == True
    - cart_items.unavailable_reason == "PRODUCT_BLOCKED" for matching items
    - cart_items for OTHER products are NOT affected
    """
    user_id = uuid4()
    blocked_product = uuid4()
    other_product = uuid4()
    sku_blocked = uuid4()
    sku_other = uuid4()

    async for db in _db_session():
        blocked_item = await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_blocked, product_id=blocked_product
        )
        other_item = await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_other, product_id=other_product
        )

    event = _blocked_event(blocked_product)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/b2b/events",
            json=event,
            headers=_event_headers(),
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["accepted"] is True

    # Verify DB state
    async for db in _db_session():
        result_blocked = await db.execute(
            select(CartItem).where(CartItem.sku_id == sku_blocked)
        )
        cart_blocked = result_blocked.scalar_one()
        assert cart_blocked.unavailable_reason == "PRODUCT_BLOCKED", (
            "Cart item for blocked product must have unavailable_reason=PRODUCT_BLOCKED"
        )

        result_other = await db.execute(
            select(CartItem).where(CartItem.sku_id == sku_other)
        )
        cart_other = result_other.scalar_one()
        assert cart_other.unavailable_reason is None, (
            "Cart item for unaffected product must NOT be marked unavailable"
        )


@pytest.mark.asyncio
async def test_orders_not_affected_by_product_blocked():
    """
    PRODUCT_BLOCKED must NOT change order_items — orders are immutable post-checkout.

    Canon rule: "Заказы НЕ трогать. Продавец обязан отгрузить по уже принятому заказу."

    Verifies:
    - 202 response (event processed)
    - OrderItem.sku_id / product_id / unit_price unchanged
    - Order.status unchanged
    """
    buyer_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    async for db in _db_session():
        order, order_item = await _seed_order_with_item(
            db, buyer_id=buyer_id, sku_id=sku_id, product_id=product_id
        )
        order_id = order.id
        original_price = order_item.unit_price

    event = _blocked_event(product_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/b2b/events",
            json=event,
            headers=_event_headers(),
        )

    assert resp.status_code == 202, resp.text

    # Orders must be completely unaffected
    async for db in _db_session():
        order_result = await db.execute(select(Order).where(Order.id == order_id))
        db_order = order_result.scalar_one()
        assert db_order.status == "PAID", "Order status must not change on PRODUCT_BLOCKED"

        item_result = await db.execute(
            select(OrderItem).where(OrderItem.order_id == order_id)
        )
        db_item = item_result.scalar_one()
        assert db_item.unit_price == original_price, (
            "OrderItem.unit_price must be unchanged — prices are fixed at checkout"
        )
        assert db_item.product_id == product_id, "OrderItem.product_id must be unchanged"


@pytest.mark.asyncio
async def test_idempotent_event_no_side_effects():
    """
    Sending the same event (same idempotency_key) twice must not double-apply.

    Verifies:
    - Both requests return 202
    - cart_items.unavailable_reason set exactly once (not overwritten or duplicated)
    - No crash or error on second identical request

    Canon: "Идемпотентность: если событие с таким idempotency_key уже обработано —
    вернуть 200 без повторной обработки."
    """
    user_id = uuid4()
    product_id = uuid4()
    sku_id = uuid4()

    async for db in _db_session():
        await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_id, product_id=product_id
        )

    idem_key = uuid4()
    event = _blocked_event(product_id, idempotency_key=idem_key)

    # First request — processes the event
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp1 = await client.post("/api/v1/b2b/events", json=event, headers=_event_headers())

    assert resp1.status_code == 202, resp1.text
    assert resp1.json()["accepted"] is True

    # Temporarily clear unavailable_reason to verify second call is truly a no-op
    async for db in _db_session():
        from sqlalchemy import update
        await db.execute(
            update(CartItem)
            .where(CartItem.sku_id == sku_id)
            .values(unavailable_reason=None)
        )
        await db.commit()

    # Second request — same idempotency_key → no-op (unavailable_reason stays None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post("/api/v1/b2b/events", json=event, headers=_event_headers())

    assert resp2.status_code == 202, resp2.text
    assert resp2.json()["accepted"] is True

    # DB must NOT have been re-updated (still None — second event was skipped)
    async for db in _db_session():
        result = await db.execute(select(CartItem).where(CartItem.sku_id == sku_id))
        cart_item = result.scalar_one()
        assert cart_item.unavailable_reason is None, (
            "Idempotent replay must not re-apply the event — unavailable_reason should be None"
        )


@pytest.mark.asyncio
async def test_missing_service_key_returns_401():
    """
    POST /api/v1/b2b/events without X-Service-Key header → 401 UNAUTHORIZED.

    Verifies:
    - 401 status (not 422 — we make the header Optional and validate manually)
    - body.code == "UNAUTHORIZED"
    """
    event = _blocked_event(uuid4())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/b2b/events",
            json=event,
            # Deliberately omit X-Service-Key
        )

    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] == "UNAUTHORIZED"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_service_key_returns_401():
    """Wrong X-Service-Key value (not missing) → 401."""
    event = _blocked_event(uuid4())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/b2b/events",
            json=event,
            headers={"X-Service-Key": "wrong-key-totally-invalid"},
        )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_product_deleted_marks_cart_items():
    """PRODUCT_DELETED also marks cart items as unavailable."""
    user_id = uuid4()
    product_id = uuid4()
    sku_id = uuid4()

    async for db in _db_session():
        await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_id, product_id=product_id
        )

    event = {
        "event_type": "PRODUCT_DELETED",
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"product_id": str(product_id)},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/b2b/events", json=event, headers=_event_headers())

    assert resp.status_code == 202

    async for db in _db_session():
        result = await db.execute(select(CartItem).where(CartItem.sku_id == sku_id))
        assert result.scalar_one().unavailable_reason == "PRODUCT_DELETED"


@pytest.mark.asyncio
async def test_sku_out_of_stock_marks_cart_item_by_sku():
    """SKU_OUT_OF_STOCK marks the specific SKU, not by product_id."""
    user_id = uuid4()
    product_id = uuid4()
    sku_target = uuid4()
    sku_other = uuid4()

    async for db in _db_session():
        await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_target, product_id=product_id
        )
        await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_other, product_id=product_id
        )

    event = {
        "event_type": "SKU_OUT_OF_STOCK",
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "sku_id": str(sku_target),
            "product_id": str(product_id),
            "available_quantity": 0,
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/b2b/events", json=event, headers=_event_headers())

    assert resp.status_code == 202

    async for db in _db_session():
        r1 = await db.execute(select(CartItem).where(CartItem.sku_id == sku_target))
        assert r1.scalar_one().unavailable_reason == "OUT_OF_STOCK"

        r2 = await db.execute(select(CartItem).where(CartItem.sku_id == sku_other))
        assert r2.scalar_one().unavailable_reason is None, (
            "Other SKU of same product must not be affected by SKU_OUT_OF_STOCK"
        )


@pytest.mark.asyncio
async def test_price_changed_event_accepted_no_cart_mutation():
    """PRICE_CHANGED is accepted (202) but does not update cart items."""
    user_id = uuid4()
    product_id = uuid4()
    sku_id = uuid4()

    async for db in _db_session():
        await _seed_cart_item(
            db, user_id=user_id, sku_id=sku_id, product_id=product_id
        )

    event = {
        "event_type": "PRICE_CHANGED",
        "idempotency_key": str(uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "sku_id": str(sku_id),
            "product_id": str(product_id),
            "old_price": 1000,
            "new_price": 900,
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/b2b/events", json=event, headers=_event_headers())

    assert resp.status_code == 202

    async for db in _db_session():
        result = await db.execute(select(CartItem).where(CartItem.sku_id == sku_id))
        assert result.scalar_one().unavailable_reason is None, (
            "PRICE_CHANGED must not mark cart items as unavailable"
        )
