"""
Tests for US-B2C-08 — Shopping cart.

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET    /api/v1/cart                → CartResponse (200)
  POST   /api/v1/cart/items          → CartResponse (200)
  PATCH  /api/v1/cart/items/{sku_id} → CartResponse (200)
  DELETE /api/v1/cart/items/{sku_id} → CartResponse (200)
  DELETE /api/v1/cart                → 204
  POST   /api/v1/cart/merge          → CartResponse (200)

DoD test names (exact):
  add_sku_increments_quantity_if_already_in_cart
  get_cart_enriched_with_b2b_data
  unavailable_sku_shown_with_reason
  guest_cart_merged_on_login

Identity:
  - Authenticated: Bearer JWT (user_id from sub claim)
  - Guest: X-Session-Id header (opaque UUID string)

B2B mocked via backend.modules.cart.service.httpx.AsyncClient.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Optional
from unittest.mock import patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient

from backend.auth import create_test_token
from backend.main import app

# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────


def _auth_headers(user_id) -> dict:
    return {"Authorization": f"Bearer {create_test_token(user_id)}"}


def _session_headers(session_id: str) -> dict:
    return {"X-Session-Id": session_id}


@asynccontextmanager
async def _make_client(user_id=None, session_id: str = None):
    headers = {}
    if user_id:
        headers.update(_auth_headers(user_id))
    if session_id:
        headers["X-Session-Id"] = session_id
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        yield client


# ── B2B response mocks ──

class _FakeResp:
    """httpx response stub. raise_for_status() works without request attribute."""
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
    Multi-response mock: takes a list of _FakeResp and returns them in order.
    Supports both GET and POST.
    """
    def __init__(self, *responses):
        self._responses = list(responses)
        self._idx = 0

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def _next(self):
        if self._idx < len(self._responses):
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._responses[-1]  # repeat last

    async def get(self, *a, **kw):
        return self._next()

    async def post(self, *a, **kw):
        return self._next()


def _sku_resp(sku_id, product_id, price=1000, discount=0, active_qty=5):
    """SKUPublicResponse from B2B."""
    return _FakeResp({
        "id": str(sku_id),
        "product_id": str(product_id),
        "name": "Red / L",
        "article": "ART-001",
        "price": price,
        "discount": discount,
        "stock_quantity": active_qty,
        "active_quantity": active_qty,
        "images": [{"url": "http://img.test/sku.jpg", "ordering": 0}],
        "characteristics": [],
    })


def _product_resp(product_id, sku_id, price=1000, discount=0, active_qty=5, title="Test Product"):
    """ProductPublicResponse from B2B batch endpoint."""
    return _FakeResp([{
        "id": str(product_id),
        "title": title,
        "slug": "test-product",
        "category_id": str(uuid4()),
        "seller_id": str(uuid4()),
        "images": [{"url": "http://img.test/prod.jpg", "ordering": 0}],
        "skus": [{
            "id": str(sku_id),
            "name": "Red / L",
            "article": "ART-001",
            "price": price,
            "discount": discount,
            "active_quantity": active_qty,
            "images": [{"url": "http://img.test/sku.jpg", "ordering": 0}],
        }],
    }])


def _product_resp_out_of_stock(product_id, sku_id):
    """Product exists but SKU has active_quantity=0."""
    return _FakeResp([{
        "id": str(product_id),
        "title": "Out of Stock Product",
        "slug": "oos-product",
        "category_id": str(uuid4()),
        "seller_id": str(uuid4()),
        "images": [],
        "skus": [{
            "id": str(sku_id),
            "name": "Blue / S",
            "article": "ART-002",
            "price": 500,
            "discount": 0,
            "active_quantity": 0,
            "images": [],
        }],
    }])


def _empty_product_resp():
    """B2B returns no products (all deleted/blocked)."""
    return _FakeResp([])


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_sku_increments_quantity_if_already_in_cart():
    """
    POST /api/v1/cart/items twice with the same SKU → quantity increments, no duplicate.

    Verifies:
    - First add: cart has 1 item with quantity=1
    - Second add (same SKU, quantity=2): cart has 1 item with quantity=3
    - items_count = 3 (total across all rows)
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # First add: GET sku → POST batch (for get_cart after add)
    mock = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=10),
        _product_resp(product_id, sku_id, active_qty=10),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id=user_id) as client:
            resp1 = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )

    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    assert len(body1["items"]) == 1
    assert body1["items"][0]["quantity"] == 1
    assert body1["items_count"] == 1

    # Second add (same SKU): GET sku → POST batch
    mock2 = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=10),
        _product_resp(product_id, sku_id, active_qty=10),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock2):
        async with _make_client(user_id=user_id) as client:
            resp2 = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 2},
            )

    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert len(body2["items"]) == 1, "Should still be 1 distinct item"
    assert body2["items"][0]["quantity"] == 3, "quantity should increment: 1+2=3"
    assert body2["items_count"] == 3


@pytest.mark.asyncio
async def test_get_cart_enriched_with_b2b_data():
    """
    GET /api/v1/cart returns items enriched with B2B product data.

    Verifies:
    - 200 status
    - Each CartItem has: sku_id, product_id, name, quantity, unit_price, line_total, is_available
    - line_total = unit_price * quantity
    - subtotal = sum of line_total for available items
    - is_valid = True when all items available
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item first
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, price=2000, discount=200, active_qty=5),
        _product_resp(product_id, sku_id, price=2000, discount=200, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 3},
            )

    # GET /cart — B2B batch returns enriched data
    mock_get = _MockClient(
        _product_resp(product_id, sku_id, price=2000, discount=200, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_get):
        async with _make_client(user_id=user_id) as client:
            resp = await client.get("/api/v1/cart")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["items"]) == 1
    item = body["items"][0]

    # Spec required fields
    assert item["sku_id"] == str(sku_id)
    assert item["product_id"] == str(product_id)
    assert "name" in item
    assert item["quantity"] == 3
    assert item["unit_price"] == 1800  # 2000 - 200 discount
    assert item["line_total"] == 5400  # 1800 * 3
    assert item["is_available"] is True
    assert item["available_quantity"] == 5

    # Summary fields
    assert body["subtotal"] == 5400
    assert body["items_count"] == 3
    assert body["is_valid"] is True


@pytest.mark.asyncio
async def test_unavailable_sku_shown_with_reason():
    """
    A SKU that goes out of stock after being added to the cart
    is shown in GET /cart with is_available=False and an unavailable_reason.

    Verifies:
    - Item appears in cart (not silently removed)
    - is_available = False
    - unavailable_reason is set (OUT_OF_STOCK or similar)
    - line_total = 0 for unavailable items
    - subtotal excludes unavailable items
    - is_valid = False
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item (in stock when added)
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            r = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 2},
            )
    assert r.status_code == 200

    # GET /cart — SKU now out of stock in B2B
    mock_get = _MockClient(
        _product_resp_out_of_stock(product_id, sku_id),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_get):
        async with _make_client(user_id=user_id) as client:
            resp = await client.get("/api/v1/cart")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Item must be present (not removed)
    assert len(body["items"]) == 1
    item = body["items"][0]

    # Unavailability indicators
    assert item["is_available"] is False
    assert item["unavailable_reason"] is not None, "unavailable_reason must be set"
    assert item["unavailable_reason"] == "OUT_OF_STOCK"
    assert item["line_total"] == 0, "Unavailable items have line_total=0"
    assert item["available_quantity"] == 0

    # Summary excludes unavailable
    assert body["subtotal"] == 0
    assert body["is_valid"] is False


@pytest.mark.asyncio
async def test_guest_cart_merged_on_login():
    """
    POST /api/v1/cart/merge merges guest cart into authenticated user cart.

    Merge strategy (canon B2C-8): MAX(guest_qty, auth_qty) on conflict.

    Scenario:
    - Guest (session_id) has SKU-A: qty=3
    - Auth user has SKU-A: qty=1, SKU-B: qty=2
    - After merge: SKU-A qty=3 (MAX), SKU-B qty=2 (no conflict)
    """
    session_id = str(uuid4())
    user_id = uuid4()
    sku_a = uuid4()
    sku_b = uuid4()
    product_a = uuid4()
    product_b = uuid4()

    # Guest adds SKU-A qty=3
    mock_guest_add = _MockClient(
        _sku_resp(sku_a, product_a, active_qty=10),
        _product_resp(product_a, sku_a, active_qty=10),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_guest_add):
        async with _make_client(session_id=session_id) as client:
            r = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_a), "quantity": 3},
            )
    assert r.status_code == 200

    # Auth user adds SKU-A qty=1
    mock_auth_a = _MockClient(
        _sku_resp(sku_a, product_a, active_qty=10),
        _product_resp(product_a, sku_a, active_qty=10),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_auth_a):
        async with _make_client(user_id=user_id) as client:
            r = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_a), "quantity": 1},
            )
    assert r.status_code == 200

    # Auth user adds SKU-B qty=2
    mock_auth_b = _MockClient(
        _sku_resp(sku_b, product_b, active_qty=10),
        _product_resp(product_b, sku_b, active_qty=10),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_auth_b):
        async with _make_client(user_id=user_id) as client:
            r = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_b), "quantity": 2},
            )
    assert r.status_code == 200

    # Merge — B2B batch returns both products for the enriched response
    mock_merge = _MockClient(
        # get_cart after merge: batch with product_a and product_b
        _FakeResp([
            {
                "id": str(product_a),
                "title": "Product A",
                "slug": "product-a",
                "category_id": str(uuid4()),
                "seller_id": str(uuid4()),
                "images": [],
                "skus": [{
                    "id": str(sku_a),
                    "name": "SKU A",
                    "article": "A1",
                    "price": 1000,
                    "discount": 0,
                    "active_quantity": 10,
                    "images": [],
                }],
            },
            {
                "id": str(product_b),
                "title": "Product B",
                "slug": "product-b",
                "category_id": str(uuid4()),
                "seller_id": str(uuid4()),
                "images": [],
                "skus": [{
                    "id": str(sku_b),
                    "name": "SKU B",
                    "article": "B1",
                    "price": 2000,
                    "discount": 0,
                    "active_quantity": 10,
                    "images": [],
                }],
            },
        ]),
    )

    merge_headers = {
        **_auth_headers(user_id),
        "X-Session-Id": session_id,
    }
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_merge):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=merge_headers,
        ) as client:
            resp = await client.post("/api/v1/cart/merge")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    items_by_sku = {item["sku_id"]: item for item in body["items"]}

    # Guest cart should be gone; user now has both SKUs
    assert str(sku_a) in items_by_sku, "SKU-A must be in merged cart"
    assert str(sku_b) in items_by_sku, "SKU-B must be in merged cart"

    # Conflict resolution: MAX(guest=3, auth=1) = 3
    assert items_by_sku[str(sku_a)]["quantity"] == 3, "SKU-A: MAX(guest=3, auth=1) = 3"
    # No conflict: SKU-B stays at 2
    assert items_by_sku[str(sku_b)]["quantity"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# Additional quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_empty_cart_returns_zero_totals():
    """GET /cart with no items → empty list, zero subtotal, is_valid=True."""
    user_id = uuid4()
    async with _make_client(user_id=user_id) as client:
        resp = await client.get("/api/v1/cart")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["items_count"] == 0
    assert body["subtotal"] == 0
    assert body["is_valid"] is True


@pytest.mark.asyncio
async def test_guest_cart_via_session_id():
    """Guest can add to cart using X-Session-Id without JWT."""
    session_id = str(uuid4())
    sku_id = uuid4()
    product_id = uuid4()

    mock = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(session_id=session_id) as client:
            resp = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["sku_id"] == str(sku_id)


@pytest.mark.asyncio
async def test_add_to_cart_returns_400_without_identity():
    """POST /api/v1/cart/items without JWT or X-Session-Id → 400 MISSING_CART_IDENTITY."""
    sku_id = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/cart/items",
            json={"sku_id": str(sku_id), "quantity": 1},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "MISSING_CART_IDENTITY"


@pytest.mark.asyncio
async def test_add_to_cart_returns_404_for_unknown_sku():
    """POST with unknown sku_id → 404 from B2B → 404 to client."""
    user_id = uuid4()
    sku_id = uuid4()

    mock = _MockClient(_FakeResp({}, status_code=404))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_add_to_cart_returns_409_insufficient_stock():
    """POST requesting more than available → 409 INSUFFICIENT_STOCK."""
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # B2B says active_qty=2, client requests 5
    mock = _MockClient(_sku_resp(sku_id, product_id, active_qty=2))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 5},
            )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "INSUFFICIENT_STOCK"


@pytest.mark.asyncio
async def test_delete_cart_item_returns_200_with_updated_cart():
    """DELETE /api/v1/cart/items/{sku_id} removes item and returns updated CartResponse."""
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id),
        _product_resp(product_id, sku_id),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )

    # Delete — cart will be empty after, so no B2B call needed
    async with _make_client(user_id=user_id) as client:
        resp = await client.delete(f"/api/v1/cart/items/{sku_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["subtotal"] == 0


@pytest.mark.asyncio
async def test_clear_cart_returns_204():
    """DELETE /api/v1/cart removes all items and returns 204."""
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item first
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id),
        _product_resp(product_id, sku_id),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )

    # Clear
    async with _make_client(user_id=user_id) as client:
        resp = await client.delete("/api/v1/cart")

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_user_carts_are_isolated():
    """Two users' carts don't interfere — each user sees only their own items."""
    user_a = uuid4()
    user_b = uuid4()
    sku_a = uuid4()
    sku_b = uuid4()
    product_a = uuid4()
    product_b = uuid4()

    # User A adds SKU-A
    mock_a = _MockClient(
        _sku_resp(sku_a, product_a),
        _product_resp(product_a, sku_a),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_a):
        async with _make_client(user_id=user_a) as client:
            await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_a), "quantity": 1},
            )

    # User B adds SKU-B
    mock_b = _MockClient(
        _sku_resp(sku_b, product_b),
        _product_resp(product_b, sku_b),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_b):
        async with _make_client(user_id=user_b) as client:
            await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_b), "quantity": 1},
            )

    # User A's cart has only SKU-A
    mock_get_a = _MockClient(_product_resp(product_a, sku_a))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_get_a):
        async with _make_client(user_id=user_a) as client:
            resp_a = await client.get("/api/v1/cart")

    sku_ids_a = [i["sku_id"] for i in resp_a.json()["items"]]
    assert str(sku_a) in sku_ids_a
    assert str(sku_b) not in sku_ids_a


@pytest.mark.asyncio
async def test_deleted_product_shown_as_unavailable_in_cart():
    """
    If a product is deleted/blocked in B2B after being added to cart,
    it appears as is_available=False with PRODUCT_DELETED reason.
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item (product exists)
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            r = await client.post(
                "/api/v1/cart/items",
                json={"sku_id": str(sku_id), "quantity": 1},
            )
    assert r.status_code == 200

    # GET /cart — product now missing from B2B (blocked/deleted)
    mock_get = _MockClient(_empty_product_resp())
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_get):
        async with _make_client(user_id=user_id) as client:
            resp = await client.get("/api/v1/cart")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["is_available"] is False
    assert item["unavailable_reason"] == "PRODUCT_DELETED"
    assert body["is_valid"] is False


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/cart/validate (US-B2C-08 addition)
# ──────────────────────────────────────────────────────────────────────────────


def _validate_product_resp(product_id, sku_id, price=1000, discount=0, active_qty=5):
    """ProductPublicResponse used in validate mock (same shape as batch endpoint)."""
    return _FakeResp([{
        "id": str(product_id),
        "title": "Test Product",
        "slug": "test",
        "category_id": str(uuid4()),
        "seller_id": str(uuid4()),
        "images": [],
        "skus": [{
            "id": str(sku_id),
            "name": "SKU",
            "article": "ART",
            "price": price,
            "discount": discount,
            "active_quantity": active_qty,
            "images": [],
        }],
    }])


@pytest.mark.asyncio
async def test_validate_cart_valid():
    """
    Happy path: all items in stock, prices unchanged → is_valid=True, issues=[].
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item at price 1000
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, price=1000, discount=0, active_qty=5),
        _product_resp(product_id, sku_id, price=1000, discount=0, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            r = await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 2})
    assert r.status_code == 200

    # Validate — price still 1000, qty=5 >= requested 2
    mock_validate = _MockClient(_validate_product_resp(product_id, sku_id, price=1000, active_qty=5))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_valid"] is True
    assert body["issues"] == []
    assert "cart" in body
    assert body["cart"]["is_valid"] is True


@pytest.mark.asyncio
async def test_validate_detects_price_changed():
    """
    PRICE_CHANGED issue: B2B price differs from unit_price_snapshot stored at add time.
    old_value = price at add, new_value = current price.
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    # Add item at price 1000
    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, price=1000, discount=0, active_qty=5),
        _product_resp(product_id, sku_id, price=1000, discount=0, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 1})

    # Validate — price is now 1200 (raised)
    mock_validate = _MockClient(_validate_product_resp(product_id, sku_id, price=1200, active_qty=5))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is False
    issue = next((i for i in body["issues"] if i["type"] == "PRICE_CHANGED"), None)
    assert issue is not None, "Expected PRICE_CHANGED issue"
    assert issue["sku_id"] == str(sku_id)
    assert issue["old_value"] == 1000
    assert issue["new_value"] == 1200


@pytest.mark.asyncio
async def test_validate_detects_out_of_stock():
    """
    OUT_OF_STOCK issue: active_quantity == 0 after item was added.
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 1})

    # Validate — now out of stock
    mock_validate = _MockClient(_validate_product_resp(product_id, sku_id, active_qty=0))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is False
    issue = next((i for i in body["issues"] if i["type"] == "OUT_OF_STOCK"), None)
    assert issue is not None
    assert issue["sku_id"] == str(sku_id)


@pytest.mark.asyncio
async def test_validate_detects_quantity_reduced():
    """
    QUANTITY_REDUCED issue: 0 < active_quantity < requested quantity.
    old_value = requested qty, new_value = available qty.
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 4})

    # Validate — only 2 available now (less than requested 4)
    mock_validate = _MockClient(_validate_product_resp(product_id, sku_id, active_qty=2))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is False
    issue = next((i for i in body["issues"] if i["type"] == "QUANTITY_REDUCED"), None)
    assert issue is not None
    assert issue["old_value"] == 4
    assert issue["new_value"] == 2


@pytest.mark.asyncio
async def test_validate_detects_product_deleted():
    """
    PRODUCT_DELETED issue: product absent from B2B batch response.
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, active_qty=5),
        _product_resp(product_id, sku_id, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 1})

    # Validate — product gone from B2B
    mock_validate = _MockClient(_FakeResp([]))  # empty batch response
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is False
    issue = next((i for i in body["issues"] if i["type"] == "PRODUCT_DELETED"), None)
    assert issue is not None
    assert issue["sku_id"] == str(sku_id)


@pytest.mark.asyncio
async def test_validate_empty_cart_is_valid():
    """Empty cart → is_valid=True, issues=[], no B2B call needed."""
    user_id = uuid4()
    async with _make_client(user_id=user_id) as client:
        resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_valid"] is True
    assert body["issues"] == []
    assert body["cart"]["items"] == []


@pytest.mark.asyncio
async def test_validate_response_shape():
    """
    spec b2c/openapi.yaml#CartValidationResponse required: [is_valid, cart, issues].
    spec b2c/openapi.yaml#CartValidationIssue required: [sku_id, type, message].
    """
    user_id = uuid4()
    sku_id = uuid4()
    product_id = uuid4()

    mock_add = _MockClient(
        _sku_resp(sku_id, product_id, price=500, active_qty=5),
        _product_resp(product_id, sku_id, price=500, active_qty=5),
    )
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_add):
        async with _make_client(user_id=user_id) as client:
            await client.post("/api/v1/cart/items", json={"sku_id": str(sku_id), "quantity": 1})

    # Price changed
    mock_validate = _MockClient(_validate_product_resp(product_id, sku_id, price=700, active_qty=5))
    with patch("backend.modules.cart.service.httpx.AsyncClient", side_effect=mock_validate):
        async with _make_client(user_id=user_id) as client:
            resp = await client.post("/api/v1/cart/validate")

    assert resp.status_code == 200
    body = resp.json()

    # Top-level required fields
    assert "is_valid" in body
    assert "cart" in body
    assert "issues" in body
    assert isinstance(body["is_valid"], bool)
    assert isinstance(body["issues"], list)

    # Issue required fields
    issue = body["issues"][0]
    assert "sku_id" in issue
    assert "type" in issue
    assert "message" in issue
    assert issue["type"] in [
        "PRICE_CHANGED", "OUT_OF_STOCK", "QUANTITY_REDUCED",
        "PRODUCT_BLOCKED", "PRODUCT_DELETED",
    ]
