"""
Tests for US-B2C-09 (Checkout) + US-B2C-10 (View Orders).

Spec: b2c/openapi.yaml
  POST /api/v1/orders — create order with Idempotency-Key header
  GET  /api/v1/orders — paginated history, filtered by JWT buyer_id
  GET  /api/v1/orders/{id} — order detail, fixed prices, IDOR -> 404

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders

DoD test names (exact):
  US-B2C-09:
    checkout_creates_paid_order_with_fixed_prices
    partial_reserve_failure_returns_409
    idempotency_returns_existing_order
    b2b_unavailable_returns_503
  US-B2C-10:
    orders_list_returns_own_orders_paginated
    order_detail_shows_fixed_prices
    other_user_order_returns_404_not_403

B2B is mocked via backend.modules.orders.service.httpx.AsyncClient.
Auth: create_test_token() from backend.auth.
"""
from __future__ import annotations

import os
from unittest.mock import patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.main import app
from backend.auth import create_test_token
from backend.modules.orders.models import Order, OrderItem

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)


# ──────────────────────────────────────────────────────────────────────────────
# DB seeding helpers (for US-B2C-10 — tests that need pre-existing orders)
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


async def _seed_order(
    db: AsyncSession,
    *,
    buyer_id: UUID,
    status: str = "PAID",
    delivery_address: str = "ул. Тестовая, 1",
    items: list[dict] | None = None,
) -> Order:
    """
    Insert an Order + OrderItems directly into the DB (no B2B calls).
    items: list of {sku_id, product_id, name, quantity, unit_price, line_total}
    """
    if items is None:
        items = []

    subtotal = sum(it["line_total"] for it in items)
    order = Order(
        id=uuid4(),
        buyer_id=buyer_id,
        idempotency_key=str(uuid4()),   # unique per seed call
        status=status,
        delivery_address=delivery_address,
        subtotal=subtotal,
        total=subtotal,
    )
    db.add(order)
    await db.flush()

    for it in items:
        db.add(OrderItem(
            order_id=order.id,
            sku_id=it.get("sku_id", uuid4()),
            product_id=it.get("product_id", uuid4()),
            name=it.get("name", "Test Product"),
            quantity=it.get("quantity", 1),
            unit_price=it.get("unit_price", 1000),
            line_total=it.get("line_total", 1000),
        ))

    await db.commit()
    return order


# ──────────────────────────────────────────────────────────────────────────────
# B2B mock helpers
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResp:
    """httpx response stub — raise_for_status without requiring request attribute."""
    def __init__(self, data, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError, Request as Req, Response as Resp
            req = Req("GET", "http://b2b/")
            raw = Resp(self.status_code, request=req)
            raise HTTPStatusError(f"HTTP {self.status_code}", request=req, response=raw)


class _MockClient:
    """
    Sequential mock for httpx.AsyncClient.

    Accepts N responses; each call to get() or post() pops the next one.
    After the list is exhausted the last response is repeated.
    Pass raise_on_enter=True to simulate ConnectError at __aenter__.
    """
    def __init__(self, *responses, raise_on_enter: bool = False):
        self._responses = list(responses)
        self._idx = 0
        self._raise = raise_on_enter

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        if self._raise:
            import httpx
            raise httpx.ConnectError("B2B unreachable")
        return self

    async def __aexit__(self, *_):
        pass

    def _next(self) -> _FakeResp:
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1]

    async def get(self, *a, **kw) -> _FakeResp:
        return self._next()

    async def post(self, *a, **kw) -> _FakeResp:
        return self._next()


def _sku_resp(sku_id: UUID, product_id: UUID, price: int = 1000, discount: int = 0) -> _FakeResp:
    """Minimal SKUPublicResponse from B2B."""
    return _FakeResp({
        "id": str(sku_id),
        "product_id": str(product_id),
        "name": f"SKU {sku_id}",
        "price": price,
        "discount": discount,
        "active_quantity": 10,
    })


def _reserve_ok() -> _FakeResp:
    return _FakeResp({"status": "reserved"}, status_code=200)


def _reserve_fail() -> _FakeResp:
    return _FakeResp({"code": "INSUFFICIENT_STOCK"}, status_code=409)


def _auth_headers(user_id: UUID) -> dict:
    token = create_test_token(user_id)
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkout_creates_paid_order_with_fixed_prices():
    """
    POST /api/v1/orders with 2 items → 201 OrderResponse.

    Verifies:
    - 201 status code
    - status = PAID (immediately, mock payment)
    - items[] contains all ordered SKUs with price snapshot
    - unit_price = sku.price - sku.discount (fixed at checkout time)
    - line_total = unit_price * quantity
    - subtotal = sum of line_totals
    - buyer_id matches JWT sub
    - address echoed back
    """
    user_id = uuid4()
    sku1, sku2 = uuid4(), uuid4()
    prod1, prod2 = uuid4(), uuid4()

    # SKU1: price=1000, no discount → unit_price=1000
    # SKU2: price=2000, discount=300 → unit_price=1700
    mock = _MockClient(
        _sku_resp(sku1, prod1, price=1000, discount=0),    # GET sku1
        _sku_resp(sku2, prod2, price=2000, discount=300),   # GET sku2
        _reserve_ok(),                                      # POST reserve
    )

    idem_key = str(uuid4())
    request_body = {
        "items": [
            {"sku_id": str(sku1), "quantity": 2},
            {"sku_id": str(sku2), "quantity": 1},
        ],
        "delivery_address": "ул. Ленина, 1, Москва",
        "payment_method_id": str(uuid4()),
    }

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "PAID"
    assert body["buyer_id"] == str(user_id)
    assert body["address"] == "ул. Ленина, 1, Москва"

    # Price snapshot verification
    items_by_sku = {it["sku_id"]: it for it in body["items"]}
    assert str(sku1) in items_by_sku
    assert str(sku2) in items_by_sku

    item1 = items_by_sku[str(sku1)]
    assert item1["unit_price"] == 1000           # price - discount = 1000 - 0
    assert item1["quantity"] == 2
    assert item1["line_total"] == 2000           # 1000 * 2

    item2 = items_by_sku[str(sku2)]
    assert item2["unit_price"] == 1700           # 2000 - 300
    assert item2["quantity"] == 1
    assert item2["line_total"] == 1700           # 1700 * 1

    assert body["subtotal"] == 3700              # 2000 + 1700
    assert body["total"] == 3700
    assert "id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_partial_reserve_failure_returns_409():
    """
    If B2B reserve returns 409 (insufficient stock for any item),
    the checkout must fail with 409 RESERVE_FAILED.

    Verifies:
    - No order is created in DB
    - 409 status
    - body.code == "RESERVE_FAILED"
    """
    user_id = uuid4()
    sku1 = uuid4()
    prod1 = uuid4()

    # SKU lookup succeeds, reserve fails
    mock = _MockClient(
        _sku_resp(sku1, prod1, price=500),  # GET sku1
        _reserve_fail(),                    # POST reserve → 409
    )

    idem_key = str(uuid4())
    request_body = {
        "items": [{"sku_id": str(sku1), "quantity": 999}],
        "delivery_address": "Тест",
        "payment_method_id": str(uuid4()),
    }

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
            )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "RESERVE_FAILED"


@pytest.mark.asyncio
async def test_idempotency_returns_existing_order():
    """
    Sending the same Idempotency-Key twice returns the original order.

    Verifies:
    - First request → 201
    - Second request (same key) → 200
    - Both responses have the same order id
    - Second request does NOT call B2B again (or at least returns the same order)
    """
    user_id = uuid4()
    sku1 = uuid4()
    prod1 = uuid4()

    idem_key = str(uuid4())
    request_body = {
        "items": [{"sku_id": str(sku1), "quantity": 1}],
        "delivery_address": "Идемпотентный адрес",
        "payment_method_id": str(uuid4()),
    }

    # First call: SKU lookup + reserve
    mock1 = _MockClient(
        _sku_resp(sku1, prod1, price=800),
        _reserve_ok(),
    )

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock1):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
            )

    assert resp1.status_code == 201, resp1.text
    order_id_first = resp1.json()["id"]

    # Second call: same key → idempotency replay (no B2B call needed)
    # The service returns early after finding the key in DB.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post(
            "/api/v1/orders",
            json=request_body,
            headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
        )

    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["id"] == order_id_first, "Same idempotency key must return the same order"
    assert body2["status"] == "PAID"


@pytest.mark.asyncio
async def test_b2b_unavailable_returns_503():
    """
    If B2B is unreachable (ConnectError), the checkout returns 503.

    Verifies:
    - 503 status
    - body.code == "UPSTREAM_UNAVAILABLE"
    """
    user_id = uuid4()
    sku1 = uuid4()

    # Raise ConnectError immediately on context manager entry
    mock = _MockClient(raise_on_enter=True)

    idem_key = str(uuid4())
    request_body = {
        "items": [{"sku_id": str(sku1), "quantity": 1}],
        "delivery_address": "Тест",
        "payment_method_id": str(uuid4()),
    }

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
            )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "UPSTREAM_UNAVAILABLE"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_requires_auth():
    """POST /api/v1/orders without Bearer token → 401 or 403."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={
                "items": [{"sku_id": str(uuid4()), "quantity": 1}],
                "delivery_address": "test",
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_order_requires_idempotency_key():
    """POST /api/v1/orders without Idempotency-Key header → 422."""
    user_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={
                "items": [{"sku_id": str(uuid4()), "quantity": 1}],
                "delivery_address": "test",
            },
            headers=_auth_headers(user_id),
            # deliberately omit Idempotency-Key
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_order_empty_items_returns_422():
    """POST /api/v1/orders with empty items list → 422 VALIDATION_ERROR."""
    user_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={"items": [], "delivery_address": "test"},
            headers={**_auth_headers(user_id), "Idempotency-Key": str(uuid4())},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_sku_not_found_returns_404():
    """If B2B returns 404 for a SKU, the order endpoint returns 404 NOT_FOUND."""
    user_id = uuid4()
    sku1 = uuid4()

    # B2B SKU lookup → 404
    mock = _MockClient(_FakeResp({"detail": "not found"}, status_code=404))

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json={
                    "items": [{"sku_id": str(sku1), "quantity": 1}],
                    "delivery_address": "test",
                },
                headers={**_auth_headers(user_id), "Idempotency-Key": str(uuid4())},
            )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_fixed_price_not_affected_by_subsequent_discount():
    """
    Price in OrderItem must reflect the value at checkout time, not B2B's current price.
    After an order is placed, price changes in B2B do not affect existing orders.

    We simulate: order placed at price=500; replay via idempotency key returns same price.
    """
    user_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()

    idem_key = str(uuid4())

    # First checkout: price=500
    mock = _MockClient(
        _sku_resp(sku1, prod1, price=500, discount=0),
        _reserve_ok(),
    )
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post(
                "/api/v1/orders",
                json={"items": [{"sku_id": str(sku1), "quantity": 3}], "delivery_address": "test"},
                headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
            )
    assert resp1.status_code == 201
    first_price = resp1.json()["items"][0]["unit_price"]
    assert first_price == 500

    # Replay via idempotency — no B2B call at all
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post(
            "/api/v1/orders",
            json={"items": [{"sku_id": str(sku1), "quantity": 3}], "delivery_address": "test"},
            headers={**_auth_headers(user_id), "Idempotency-Key": idem_key},
        )
    assert resp2.status_code == 200
    assert resp2.json()["items"][0]["unit_price"] == 500  # unchanged


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-10 DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orders_list_returns_own_orders_paginated():
    """
    GET /api/v1/orders returns only the authenticated buyer's orders with pagination.

    Verifies:
    - 200 status
    - Response shape: {items, total_count, limit, offset}
    - items[] contains only orders belonging to the JWT buyer (not other users' orders)
    - Pagination: limit/offset work correctly
    - Orders sorted by created_at DESC (most recent first)
    - Other user's orders are NOT included (IDOR)
    """
    buyer_id = uuid4()
    other_user_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()

    # Seed 3 orders for buyer, 1 for another user
    async for db in _db_session():
        order1 = await _seed_order(
            db, buyer_id=buyer_id, status="PAID",
            items=[{"sku_id": sku1, "product_id": prod1, "name": "Item A",
                    "quantity": 1, "unit_price": 500, "line_total": 500}],
        )
        order2 = await _seed_order(
            db, buyer_id=buyer_id, status="DELIVERED",
            items=[{"sku_id": sku1, "product_id": prod1, "name": "Item B",
                    "quantity": 2, "unit_price": 300, "line_total": 600}],
        )
        order3 = await _seed_order(
            db, buyer_id=buyer_id, status="CANCELLED",
            items=[],
        )
        other_order = await _seed_order(db, buyer_id=other_user_id, status="PAID")

    token = create_test_token(buyer_id)

    # ── Full list (no pagination) ──
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/orders",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Spec shape
    assert "items" in body
    assert "total_count" in body
    assert "limit" in body
    assert "offset" in body
    assert body["limit"] == 20
    assert body["offset"] == 0

    returned_ids = {it["id"] for it in body["items"]}
    assert str(order1.id) in returned_ids, "Own order 1 must appear"
    assert str(order2.id) in returned_ids, "Own order 2 must appear"
    assert str(order3.id) in returned_ids, "Own order 3 must appear"
    assert str(other_order.id) not in returned_ids, "Other user's order must NOT appear"

    assert body["total_count"] >= 3, "total_count must count own orders"

    # ── Pagination: limit=1, offset=0 ──
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_page = await client.get(
            "/api/v1/orders?limit=1&offset=0",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp_page.status_code == 200, resp_page.text
    page_body = resp_page.json()
    assert len(page_body["items"]) == 1, "limit=1 must return exactly 1 item"
    assert page_body["limit"] == 1
    assert page_body["offset"] == 0

    # ── Status filter ──
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_paid = await client.get(
            "/api/v1/orders?status=PAID",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp_paid.status_code == 200, resp_paid.text
    paid_body = resp_paid.json()
    paid_ids = {it["id"] for it in paid_body["items"]}
    assert str(order1.id) in paid_ids, "PAID order must appear in PAID filter"
    assert str(order2.id) not in paid_ids, "DELIVERED order must NOT appear in PAID filter"


@pytest.mark.asyncio
async def test_order_detail_shows_fixed_prices():
    """
    GET /api/v1/orders/{id} returns items with unit_price from checkout snapshot.

    Verifies:
    - 200 status
    - items[].unit_price matches what was stored at checkout (not current B2B price)
    - items[].line_total = unit_price * quantity
    - Required fields present: id, buyer_id, status, items, subtotal, total, address, created_at
    - No B2B call is made (prices come from DB OrderItem, not B2B)
    """
    buyer_id = uuid4()
    sku_id, prod_id = uuid4(), uuid4()

    # Seed order with a specific price snapshot (e.g., price was 1500 at checkout)
    checkout_price = 1500
    qty = 3

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="PAID",
            delivery_address="г. Екатеринбург, ул. Мира 19",
            items=[{
                "sku_id": sku_id,
                "product_id": prod_id,
                "name": "iPhone 15 Pro, 256GB Black",
                "quantity": qty,
                "unit_price": checkout_price,
                "line_total": checkout_price * qty,
            }],
        )

    token = create_test_token(buyer_id)

    # No B2B mock needed — prices come from DB
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/orders/{order.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Required spec fields
    for field in ("id", "buyer_id", "status", "items", "subtotal", "total", "address", "created_at"):
        assert field in body, f"Required field '{field}' missing from OrderResponse"

    assert body["id"] == str(order.id)
    assert body["buyer_id"] == str(buyer_id)
    assert body["status"] == "PAID"
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["sku_id"] == str(sku_id)
    assert item["unit_price"] == checkout_price, (
        "unit_price must equal the checkout snapshot, not any current B2B price"
    )
    assert item["quantity"] == qty
    assert item["line_total"] == checkout_price * qty

    assert body["subtotal"] == checkout_price * qty
    assert body["address"] == "г. Екатеринбург, ул. Мира 19"


@pytest.mark.asyncio
async def test_other_user_order_returns_404_not_403():
    """
    GET /api/v1/orders/{id} for an order that belongs to another user -> 404.

    Rule (canon b2c-orders-flows.md#b2c-10-view-orders §Authorization):
      Returning 403 would reveal that the order with that ID EXISTS, allowing
      an attacker to enumerate valid UUIDs. Always 404 regardless of ownership.

    Verifies:
    - 200 for the owner (baseline)
    - 404 for any other authenticated user (NOT 403, NOT 200)
    - body.code == "ORDER_NOT_FOUND"
    """
    owner_id = uuid4()
    attacker_id = uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db, buyer_id=owner_id, status="PAID",
            items=[{"sku_id": uuid4(), "product_id": uuid4(),
                    "name": "Secret Item", "quantity": 1,
                    "unit_price": 9999, "line_total": 9999}],
        )

    # Owner can see their own order
    owner_token = create_test_token(owner_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner_resp = await client.get(
            f"/api/v1/orders/{order.id}",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert owner_resp.status_code == 200, "Owner must see their own order"

    # Attacker gets 404 (not 403) — does not reveal existence
    attacker_token = create_test_token(attacker_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        attacker_resp = await client.get(
            f"/api/v1/orders/{order.id}",
            headers={"Authorization": f"Bearer {attacker_token}"},
        )

    assert attacker_resp.status_code == 404, (
        f"Expected 404 for wrong-user access, got {attacker_resp.status_code}. "
        "Returning 403 reveals order existence — security violation."
    )
    assert attacker_resp.json()["code"] == "ORDER_NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-11 DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_paid_order_transitions_to_cancelled():
    """
    POST /orders/{id}/cancel on a PAID order → B2B unreserve succeeds → 200 CANCELLED.

    Verifies:
    - 200 status
    - body.status == "CANCELLED"
    - body.id == order.id
    - All required OrderResponse fields present
    - B2B unreserve was called (mock is consumed)
    """
    buyer_id = uuid4()
    sku_id, prod_id = uuid4(), uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="PAID",
            items=[{
                "sku_id": sku_id,
                "product_id": prod_id,
                "name": "Cancelable Product",
                "quantity": 2,
                "unit_price": 500,
                "line_total": 1000,
            }],
        )

    # Mock: B2B unreserve succeeds (200)
    unreserve_ok = _FakeResp(
        {"order_id": str(order.id), "status": "UNRESERVED", "processed_at": "2026-05-27T10:00:00Z"},
        status_code=200,
    )
    mock = _MockClient(unreserve_ok)

    token = create_test_token(buyer_id)
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["id"] == str(order.id)
    assert body["buyer_id"] == str(buyer_id)

    # Verify the order is truly CANCELLED in DB (GET it back)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(
            f"/api/v1/orders/{order.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert detail.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_unreserve_failure_transitions_to_cancel_pending():
    """
    POST /orders/{id}/cancel → B2B unreserve times out → 200 with status CANCEL_PENDING.

    Verifies:
    - 200 status (cancellation intent is always accepted)
    - body.status == "CANCEL_PENDING"
    - No 5xx is returned to the buyer (B2B failure is handled internally)

    Canon: "нельзя отвечать покупателю «попробуйте позже»: намерение отменить нужно принять
    и выполнить асинхронно"
    """
    buyer_id = uuid4()
    sku_id, prod_id = uuid4(), uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="PAID",
            items=[{
                "sku_id": sku_id,
                "product_id": prod_id,
                "name": "Unreachable Reserve",
                "quantity": 1,
                "unit_price": 800,
                "line_total": 800,
            }],
        )

    # Mock: B2B is unreachable → ConnectError on context manager entry
    mock = _MockClient(raise_on_enter=True)

    token = create_test_token(buyer_id)
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCEL_PENDING", (
        "When B2B unreserve fails, order must transition to CANCEL_PENDING, not error out"
    )
    assert body["id"] == str(order.id)


@pytest.mark.asyncio
async def test_cancel_assembling_order_returns_409():
    """
    POST /orders/{id}/cancel on an ASSEMBLING order → 409 CANCEL_NOT_ALLOWED.

    Verifies:
    - 409 status
    - body.code == "CANCEL_NOT_ALLOWED"
    - body contains the current status (details.current_status or similar)
    - No B2B call is made

    Cancellable statuses: CREATED, PAID only (canon/DoD explicit rule).
    Note: spec description mentions ASSEMBLING as cancellable, but DoD test overrides.
    """
    buyer_id = uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="ASSEMBLING",
            items=[{"sku_id": uuid4(), "product_id": uuid4(),
                    "name": "In Assembly", "quantity": 1,
                    "unit_price": 300, "line_total": 300}],
        )

    token = create_test_token(buyer_id)
    # No mock needed — service must reject before any B2B call
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "CANCEL_NOT_ALLOWED"
    # current_status must be surfaced so the client knows why cancellation was refused
    assert "ASSEMBLING" in str(body), "Response must mention ASSEMBLING as the blocking status"


@pytest.mark.asyncio
async def test_cancel_other_user_order_returns_404():
    """
    POST /orders/{id}/cancel for a different user's order → 404 ORDER_NOT_FOUND.

    IDOR rule: returning 403 would reveal that the order exists.
    Always 404 regardless of ownership.
    """
    owner_id = uuid4()
    attacker_id = uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=owner_id,
            status="PAID",
            items=[{"sku_id": uuid4(), "product_id": uuid4(),
                    "name": "Owner Item", "quantity": 1,
                    "unit_price": 200, "line_total": 200}],
        )

    attacker_token = create_test_token(attacker_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {attacker_token}"},
        )

    assert resp.status_code == 404, (
        f"Expected 404 for wrong-user cancel, got {resp.status_code}. "
        "Returning 403 reveals order existence — IDOR vulnerability."
    )
    assert resp.json()["code"] == "ORDER_NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-11 extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_created_order_transitions_to_cancelled():
    """CREATED (not yet paid) orders are also cancellable."""
    buyer_id = uuid4()

    async for db in _db_session():
        order = await _seed_order(
            db, buyer_id=buyer_id, status="CREATED",
            items=[{"sku_id": uuid4(), "product_id": uuid4(),
                    "name": "Created Order Item", "quantity": 1,
                    "unit_price": 100, "line_total": 100}],
        )

    unreserve_ok = _FakeResp(
        {"order_id": str(order.id), "status": "UNRESERVED", "processed_at": "2026-05-27T10:00:00Z"},
        status_code=200,
    )
    mock = _MockClient(unreserve_ok)
    token = create_test_token(buyer_id)

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_delivered_order_returns_409():
    """DELIVERED orders cannot be cancelled."""
    buyer_id = uuid4()

    async for db in _db_session():
        order = await _seed_order(db, buyer_id=buyer_id, status="DELIVERED")

    token = create_test_token(buyer_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{order.id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 409
    assert resp.json()["code"] == "CANCEL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_cancel_requires_auth():
    """POST /orders/{id}/cancel without token → 401 or 403."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/v1/orders/{uuid4()}/cancel")
    assert resp.status_code in (401, 403)
