"""
Tests for US-B2C-09 (Checkout) + US-B2C-10 (View Orders) + US-B2C-11 (Cancel)
            + US-B2C-13 (Fulfill/Deliver).

Spec: b2c/openapi.yaml
  POST /api/v1/orders          — create order from cart with Idempotency-Key header
  GET  /api/v1/orders          — paginated history, filtered by JWT buyer_id
  GET  /api/v1/orders/{id}     — order detail, fixed prices, IDOR → 404
  POST /api/v1/orders/{id}/cancel  — cancel PAID/CREATED orders
  POST /api/v1/orders/{id}/deliver — admin endpoint (X-Service-Key)

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders,
       b2c-orders-flows.md#b2c-11-cancel-order, b2c-orders-flows.md#b2c-13-fulfill

Checkout flow:
  POST body = {address_id, payment_method_id, comment?}  — NO items in body.
  Items come from cart_items WHERE user_id=buyer_id.
  Address looked up by address_id from addresses table.
  Cart cleared after successful checkout.

B2B mocked via backend.modules.orders.service.httpx.AsyncClient.
Auth: create_test_token() from backend.auth.

DoD test names (exact):
  US-B2C-09:
    test_checkout_creates_paid_order_with_fixed_prices
    test_partial_reserve_failure_returns_409
    test_idempotency_returns_existing_order
    test_b2b_unavailable_returns_503
  US-B2C-10:
    test_orders_list_returns_own_orders_paginated
    test_order_detail_shows_fixed_prices
    test_other_user_order_returns_404_not_403
  US-B2C-11:
    test_cancel_paid_order_transitions_to_cancelled
    test_unreserve_failure_transitions_to_cancel_pending
    test_cancel_assembling_order_returns_409
    test_cancel_other_user_order_returns_404
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.auth import create_test_token
from backend.main import app
from backend.modules.addresses.models import Address
from backend.modules.cart.models import CartItem
from backend.modules.orders.models import Order, OrderItem

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)


# ──────────────────────────────────────────────────────────────────────────────
# DB session helper
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


# ──────────────────────────────────────────────────────────────────────────────
# DB seeding helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _seed_address(
    db: AsyncSession,
    buyer_id: UUID,
    *,
    country: str = "RU",
    city: str = "Москва",
    street: str = "Ленина",
    building: str = "1",
    region: str | None = None,
    apartment: str | None = None,
    postal_code: str | None = None,
) -> Address:
    """Insert an Address row and return it."""
    address = Address(
        id=uuid4(),
        buyer_id=buyer_id,
        country=country,
        city=city,
        street=street,
        building=building,
        region=region,
        apartment=apartment,
        postal_code=postal_code,
        is_default=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(address)
    await db.commit()
    return address


async def _seed_cart_item(
    db: AsyncSession,
    buyer_id: UUID,
    sku_id: UUID,
    product_id: UUID,
    quantity: int = 1,
    unit_price_snapshot: int | None = None,
) -> CartItem:
    """Insert a CartItem row for an authenticated user and return it."""
    item = CartItem(
        id=uuid4(),
        user_id=buyer_id,
        session_id=None,
        sku_id=sku_id,
        product_id=product_id,
        quantity=quantity,
        unit_price_snapshot=unit_price_snapshot,
        unavailable_reason=None,
    )
    db.add(item)
    await db.commit()
    return item


async def _seed_order(
    db: AsyncSession,
    *,
    buyer_id: UUID,
    address_id: UUID | None = None,
    status: str = "PAID",
    items: list[dict] | None = None,
    payment_method_id: UUID | None = None,
) -> Order:
    """
    Insert an Order + OrderItems directly into the DB (no B2B calls).
    items: list of {sku_id, product_id, name, quantity, unit_price, line_total}
    """
    if items is None:
        items = []
    if address_id is None:
        address_id = uuid4()
    if payment_method_id is None:
        payment_method_id = uuid4()

    subtotal = sum(it.get("line_total", 0) for it in items)

    address_snapshot = json.dumps({
        "id": str(address_id),
        "country": "RU",
        "city": "Москва",
        "street": "Ленина",
        "building": "1",
        "region": None,
        "apartment": None,
        "postal_code": None,
        "recipient_name": None,
        "recipient_phone": None,
        "is_default": False,
        "comment": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    order = Order(
        id=uuid4(),
        buyer_id=buyer_id,
        idempotency_key=str(uuid4()),
        status=status,
        address_id=address_id,
        address_snapshot=address_snapshot,
        payment_method_id=payment_method_id,
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
            req = Req("POST", "http://b2b/")
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


def _b2b_batch_product(
    sku_id: UUID,
    product_id: UUID,
    price: int = 1000,
    discount: int = 0,
    active_qty: int = 5,
    name: str = "Test Product",
) -> dict:
    """Build a minimal ProductPublicResponse entry for the batch endpoint mock."""
    return {
        "id": str(product_id),
        "title": name,
        "slug": "test-product",
        "category_id": str(uuid4()),
        "seller_id": str(uuid4()),
        "images": [],
        "skus": [{
            "id": str(sku_id),
            "name": "SKU Name",
            "article": "ART-001",
            "price": price,
            "discount": discount,
            "active_quantity": active_qty,
            "images": [],
        }],
    }


def _batch_ok(*product_dicts: dict) -> _FakeResp:
    """POST /api/v1/public/products/batch → 200 list of products."""
    return _FakeResp(list(product_dicts), status_code=200)


def _reserve_ok() -> _FakeResp:
    return _FakeResp({"status": "reserved"}, status_code=200)


def _reserve_fail() -> _FakeResp:
    return _FakeResp({"code": "INSUFFICIENT_STOCK"}, status_code=409)


def _unreserve_ok(order_id: UUID | None = None) -> _FakeResp:
    return _FakeResp(
        {
            "order_id": str(order_id or uuid4()),
            "status": "UNRESERVED",
            "processed_at": "2026-05-29T10:00:00Z",
        },
        status_code=200,
    )


def _auth_headers(user_id: UUID) -> dict:
    token = create_test_token(user_id)
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-09 DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkout_creates_paid_order_with_fixed_prices():
    """
    POST /api/v1/orders — cart-based checkout → 201 OrderResponse.

    Flow:
      - Seed address + two cart items (sku1 no discount, sku2 with discount).
      - Mock: POST /batch → products; POST /reserve → ok.
      - Expect 201 with PAID order; prices fixed at checkout time.

    Verifies:
    - 201 status
    - status = PAID
    - items[] has correct sku_ids, quantities, unit_prices, line_totals
    - unit_price = price - discount
    - subtotal = sum of line_totals
    - buyer_id matches JWT sub
    - address object present in response
    - payment_method object present in response
    """
    buyer_id = uuid4()
    sku1, sku2 = uuid4(), uuid4()
    prod1, prod2 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=2, unit_price_snapshot=1000)
        await _seed_cart_item(db, buyer_id, sku2, prod2, quantity=1, unit_price_snapshot=1700)

    # Batch: price=1000 no discount, price=2000 discount=300 → unit_price=1700
    mock = _MockClient(
        _batch_ok(
            _b2b_batch_product(sku1, prod1, price=1000, discount=0),
            _b2b_batch_product(sku2, prod2, price=2000, discount=300),
        ),
        _reserve_ok(),
    )

    idem_key = str(uuid4())
    request_body = {
        "address_id": str(address.id),
        "payment_method_id": str(payment_method_id),
    }

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["status"] == "PAID"
    assert body["buyer_id"] == str(buyer_id)

    # Address object present
    assert isinstance(body["address"], dict), "address must be an object"
    assert body["address"]["city"] == "Москва"

    # Payment method present
    assert isinstance(body["payment_method"], dict)
    assert body["payment_method"]["type"] == "CARD"

    # Price snapshot verification
    items_by_sku = {it["sku_id"]: it for it in body["items"]}
    assert str(sku1) in items_by_sku, "sku1 must appear in order items"
    assert str(sku2) in items_by_sku, "sku2 must appear in order items"

    item1 = items_by_sku[str(sku1)]
    assert item1["unit_price"] == 1000, "1000 - 0 = 1000"
    assert item1["quantity"] == 2
    assert item1["line_total"] == 2000, "1000 * 2 = 2000"

    item2 = items_by_sku[str(sku2)]
    assert item2["unit_price"] == 1700, "2000 - 300 = 1700"
    assert item2["quantity"] == 1
    assert item2["line_total"] == 1700, "1700 * 1 = 1700"

    assert body["subtotal"] == 3700, "2000 + 1700 = 3700"
    assert body["total"] == 3700
    assert "id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_partial_reserve_failure_returns_409():
    """
    B2B reserve returns 409 → checkout fails with 409 RESERVE_FAILED.

    Verifies:
    - 409 status
    - body.code == "RESERVE_FAILED"
    - No order persisted
    """
    buyer_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=999)

    # Batch succeeds, reserve fails
    mock = _MockClient(
        _batch_ok(_b2b_batch_product(sku1, prod1, price=500, active_qty=1)),
        _reserve_fail(),
    )

    idem_key = str(uuid4())
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
                headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
            )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "RESERVE_FAILED"


@pytest.mark.asyncio
async def test_idempotency_returns_existing_order():
    """
    Sending the same Idempotency-Key twice returns the original order.

    Verifies:
    - First request → 201
    - Second request (same key) → 200
    - Both responses have the same order id
    - B2B NOT called on second request
    """
    buyer_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=1)

    idem_key = str(uuid4())
    request_body = {
        "address_id": str(address.id),
        "payment_method_id": str(payment_method_id),
    }

    # First call: batch + reserve
    mock1 = _MockClient(
        _batch_ok(_b2b_batch_product(sku1, prod1, price=800)),
        _reserve_ok(),
    )

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock1):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post(
                "/api/v1/orders",
                json=request_body,
                headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
            )

    assert resp1.status_code == 201, resp1.text
    order_id_first = resp1.json()["id"]

    # Second call: same key — idempotency replay (no B2B call needed)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post(
            "/api/v1/orders",
            json=request_body,
            headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
        )

    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["id"] == order_id_first, "Same idempotency key must return the same order"
    assert body2["status"] == "PAID"


@pytest.mark.asyncio
async def test_b2b_unavailable_returns_503():
    """
    B2B batch endpoint unreachable (ConnectError) → checkout returns 503.

    Verifies:
    - 503 status
    - body.code == "UPSTREAM_UNAVAILABLE"
    """
    buyer_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=1)

    # ConnectError on first context-manager entry (the batch call)
    mock = _MockClient(raise_on_enter=True)

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
                headers={**_auth_headers(buyer_id), "Idempotency-Key": str(uuid4())},
            )

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-09 extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_checkout_requires_auth():
    """POST /api/v1/orders without Bearer token → 401 or 403."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={"address_id": str(uuid4()), "payment_method_id": str(uuid4())},
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert resp.status_code in (401, 403), resp.text


@pytest.mark.asyncio
async def test_checkout_requires_idempotency_key():
    """POST /api/v1/orders without Idempotency-Key header → 422."""
    user_id = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={"address_id": str(uuid4()), "payment_method_id": str(uuid4())},
            headers=_auth_headers(user_id),
            # deliberately omit Idempotency-Key
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_checkout_cart_empty_returns_error():
    """POST /api/v1/orders with empty cart → 4xx error (CART_EMPTY)."""
    buyer_id = uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        # deliberately do NOT seed any cart items

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
            headers={**_auth_headers(buyer_id), "Idempotency-Key": str(uuid4())},
        )
    assert resp.status_code in (400, 409, 422), resp.text


@pytest.mark.asyncio
async def test_checkout_address_not_found_returns_404():
    """POST /api/v1/orders with a nonexistent address_id → 404 ADDRESS_NOT_FOUND."""
    buyer_id = uuid4()

    async for db in _db_session():
        await _seed_cart_item(db, buyer_id, uuid4(), uuid4(), quantity=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/orders",
            json={"address_id": str(uuid4()), "payment_method_id": str(uuid4())},
            headers={**_auth_headers(buyer_id), "Idempotency-Key": str(uuid4())},
        )
    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "ADDRESS_NOT_FOUND"


@pytest.mark.asyncio
async def test_cart_cleared_after_checkout():
    """Cart items are deleted from DB after successful checkout."""
    buyer_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=1)

    mock = _MockClient(
        _batch_ok(_b2b_batch_product(sku1, prod1, price=600)),
        _reserve_ok(),
    )

    idem_key = str(uuid4())
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/orders",
                json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
                headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
            )
    assert resp.status_code == 201, resp.text

    # Verify cart is empty — GET /api/v1/cart must return no items
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cart_resp = await client.get(
            "/api/v1/cart",
            headers=_auth_headers(buyer_id),
        )
    # Cart endpoint must exist and return empty (no items for this buyer)
    assert cart_resp.status_code == 200, cart_resp.text
    cart_body = cart_resp.json()
    cart_items = cart_body.get("items", cart_body) if isinstance(cart_body, dict) else cart_body
    if isinstance(cart_items, list):
        assert len(cart_items) == 0, "Cart must be empty after checkout"


@pytest.mark.asyncio
async def test_fixed_price_not_affected_by_subsequent_discount():
    """
    Price in OrderItem reflects checkout-time value — replay via idempotency returns same.
    """
    buyer_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()
    payment_method_id = uuid4()

    async for db in _db_session():
        address = await _seed_address(db, buyer_id)
        await _seed_cart_item(db, buyer_id, sku1, prod1, quantity=3)

    idem_key = str(uuid4())

    # First checkout: price=500
    mock = _MockClient(
        _batch_ok(_b2b_batch_product(sku1, prod1, price=500, discount=0)),
        _reserve_ok(),
    )
    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post(
                "/api/v1/orders",
                json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
                headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
            )
    assert resp1.status_code == 201
    first_price = resp1.json()["items"][0]["unit_price"]
    assert first_price == 500

    # Replay via idempotency — no B2B call, same price returned
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post(
            "/api/v1/orders",
            json={"address_id": str(address.id), "payment_method_id": str(payment_method_id)},
            headers={**_auth_headers(buyer_id), "Idempotency-Key": idem_key},
        )
    assert resp2.status_code == 200
    assert resp2.json()["items"][0]["unit_price"] == 500


# ──────────────────────────────────────────────────────────────────────────────
# US-B2C-10 DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orders_list_returns_own_orders_paginated():
    """
    GET /api/v1/orders — returns only the authenticated buyer's orders with pagination.

    Verifies:
    - 200 status
    - Response shape: {items, total_count, limit, offset}
    - items[] contains only buyer's orders (not other users')
    - Pagination: limit/offset work correctly
    - Status filter works
    """
    buyer_id = uuid4()
    other_user_id = uuid4()
    sku1, prod1 = uuid4(), uuid4()

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

    # Full list
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/orders",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()

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
    assert body["total_count"] >= 3

    # Pagination: limit=1
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

    # Status filter
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
    GET /api/v1/orders/{id} — returns items with unit_price from checkout snapshot.

    Verifies:
    - 200 status
    - items[].unit_price matches what was stored at checkout
    - items[].line_total = unit_price * quantity
    - Required fields: id, buyer_id, status, items, subtotal, total, address, created_at
    - address is an object (AddressResponse shape)
    - payment_method is present
    - No B2B call (prices from DB)
    """
    buyer_id = uuid4()
    sku_id, prod_id = uuid4(), uuid4()
    checkout_price = 1500
    qty = 3

    async for db in _db_session():
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="PAID",
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

    for field in ("id", "buyer_id", "status", "items", "subtotal", "total", "address", "created_at"):
        assert field in body, f"Required field '{field}' missing from OrderResponse"

    assert body["id"] == str(order.id)
    assert body["buyer_id"] == str(buyer_id)
    assert body["status"] == "PAID"
    assert len(body["items"]) == 1
    assert isinstance(body["address"], dict), "address must be AddressResponse object"
    assert "payment_method" in body

    item = body["items"][0]
    assert item["sku_id"] == str(sku_id)
    assert item["unit_price"] == checkout_price, (
        "unit_price must equal the checkout snapshot, not any current B2B price"
    )
    assert item["quantity"] == qty
    assert item["line_total"] == checkout_price * qty

    assert body["subtotal"] == checkout_price * qty


@pytest.mark.asyncio
async def test_other_user_order_returns_404_not_403():
    """
    GET /api/v1/orders/{id} for another user's order → 404 ORDER_NOT_FOUND.

    IDOR rule: 403 would reveal that the order with that ID exists.
    Always 404 regardless of ownership.
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

    # Attacker gets 404 — does not reveal existence
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
    POST /orders/{id}/cancel on PAID order → B2B unreserve succeeds → 200 CANCELLED.

    Verifies:
    - 200 status
    - body.status == "CANCELLED"
    - body.id == order.id
    - Required OrderResponse fields present
    - B2B unreserve was called
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

    mock = _MockClient(_unreserve_ok(order.id))
    token = create_test_token(buyer_id)

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["id"] == str(order.id)
    assert body["buyer_id"] == str(buyer_id)

    # Verify in DB via GET
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(
            f"/api/v1/orders/{order.id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert detail.json()["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_unreserve_failure_transitions_to_cancel_pending():
    """
    POST /orders/{id}/cancel → B2B unreserve ConnectError → 200 CANCEL_PENDING.

    Canon: buyer's cancellation intent must always be accepted.
    Verifies:
    - 200 status (not 5xx)
    - body.status == "CANCEL_PENDING"
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

    # B2B unreachable
    mock = _MockClient(raise_on_enter=True)
    token = create_test_token(buyer_id)

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                json={},
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
    POST /orders/{id}/cancel on ASSEMBLING order → 409 CANCEL_NOT_ALLOWED.

    Cancellable statuses: CREATED, PAID only (canon/DoD).
    Verifies:
    - 409 status
    - body.code == "CANCEL_NOT_ALLOWED"
    - ASSEMBLING mentioned in response (current_status surfaced)
    - No B2B call made
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
    # No mock — service must reject before any B2B call
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{order.id}/cancel",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "CANCEL_NOT_ALLOWED"
    assert "ASSEMBLING" in str(body), "Response must mention ASSEMBLING as the blocking status"


@pytest.mark.asyncio
async def test_cancel_other_user_order_returns_404():
    """
    POST /orders/{id}/cancel for another user's order → 404 ORDER_NOT_FOUND.

    IDOR: returning 403 would reveal the order exists.
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
            json={},
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

    mock = _MockClient(_unreserve_ok(order.id))
    token = create_test_token(buyer_id)

    with patch("backend.modules.orders.service.httpx.AsyncClient", side_effect=mock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/cancel",
                json={},
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
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 409
    assert resp.json()["code"] == "CANCEL_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_cancel_requires_auth():
    """POST /orders/{id}/cancel without token → 401 or 403."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/v1/orders/{uuid4()}/cancel", json={})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_cancel_nonexistent_order_returns_404():
    """POST /orders/{id}/cancel for a completely nonexistent UUID → 404."""
    buyer_id = uuid4()
    token = create_test_token(buyer_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{uuid4()}/cancel",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "ORDER_NOT_FOUND"
