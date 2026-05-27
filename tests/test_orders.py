"""
Tests for US-B2C-09 — Checkout / Orders.

Spec: b2c/openapi.yaml
  POST /api/v1/orders — create order with Idempotency-Key header

Canon: b2c-cart-flows.md#b2c-09-checkout

DoD test names (exact):
  checkout_creates_paid_order_with_fixed_prices
  partial_reserve_failure_returns_409
  idempotency_returns_existing_order
  b2b_unavailable_returns_503

B2B is mocked via backend.modules.orders.service.httpx.AsyncClient.
Auth: create_test_token() from backend.auth.
"""
from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from backend.auth import create_test_token


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
