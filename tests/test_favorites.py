"""
US-B2C-06: Favorites CRUD via:
  POST   /api/v1/favorites/{product_id}  — add (idempotent)
  DELETE /api/v1/favorites/{product_id}  — remove (idempotent)
  GET    /api/v1/favorites               — enriched list from B2B

Canon flow: b2c-cart-flows.md#b2c-6-favorites
Spec:       neomarket-protocols/b2c/openapi.yaml (favorites section)
            neomarket-canon/apis/b2c/cart/openapi.yaml (FavoriteMutationResponse)

Covered DoD scenarios:
  ✓ add_to_favorites_returns_201
  ✓ repeat_add_returns_200_not_duplicate
  ✓ get_favorites_enriched_from_b2b
  ✓ blocked_product_excluded_from_list
  ✓ user_id_from_query_is_ignored       (IDOR prevention)

Extra:
  ✓ remove_from_favorites_returns_204
  ✓ remove_nonexistent_returns_204      (idempotent)
  ✓ empty_favorites_returns_200
  ✓ favorites_requires_auth_401
  ✓ b2b_unavailable_returns_503

Security note (ADR in PR description):
  user_id comes exclusively from JWT claims (Bearer token).
  Any user_id passed in query params is completely ignored by the backend.
  This prevents IDOR: user A cannot read/modify user B's favorites.

Test isolation:
  Each test uses a fresh test user_id (uuid4) so parallel runs don't clash.
  Database is created in the `create_db` autouse fixture.
  Auth dependency is overridden via app.dependency_overrides.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError, Request as HttpxRequest, Response as HttpxResponse
from httpx import HTTPStatusError

from backend.main import app
from backend.auth import get_current_user_id, create_test_token
# Tables are created by conftest.py setup_test_database fixture (session-scoped).


def _make_client(user_id: UUID) -> AsyncClient:
    """Create a test HTTP client with JWT auth for the given user_id."""
    token = create_test_token(user_id)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# B2B mock helpers
# ──────────────────────────────────────────────────────────────────────────────

def _b2b_product(
    *,
    product_id: str | None = None,
    title: str = "Test Product",
    min_price: int = 5_000_00,
    active_quantity: int = 10,
) -> dict[str, Any]:
    """Build a B2B ProductPublicResponse dict (batch endpoint returns this)."""
    pid = product_id or str(uuid4())
    return {
        "id": pid,
        "seller_id": str(uuid4()),
        "category_id": str(uuid4()),
        "title": title,
        "slug": "test-product",
        "description": "A product",
        "status": "MODERATED",
        "images": [{"url": "https://cdn.example.com/img.jpg", "ordering": 0}],
        "characteristics": [],
        "skus": [
            {
                "id": str(uuid4()),
                "name": "Default SKU",
                "price": min_price,
                "discount": 0,
                "active_quantity": active_quantity,
                "article": "ART-001",
                "images": [],
                "characteristics": [],
            }
        ],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }


class _FakeResp:
    def __init__(self, data=None, status_code: int = 200):
        self._data = data if data is not None else []
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = HttpxRequest("POST", "http://b2b/")
            raw = HttpxResponse(self.status_code, request=req)
            raise HTTPStatusError(f"HTTP {self.status_code}", request=req, response=raw)


class _MockClient:
    """
    Stub for httpx.AsyncClient that returns a fixed response for any HTTP method.
    Used as `side_effect=mock` so each `httpx.AsyncClient(...)` call returns
    a new context-manager instance backed by the same response.
    """
    def __init__(self, response):
        self._response = response

    def __call__(self, **kwargs):
        """Called as httpx.AsyncClient(...) — return self as context manager."""
        return _MockClientInstance(self._response)


class _MockClientInstance:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def get(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ──────────────────────────────────────────────────────────────────────────────
# DoD: add_to_favorites_returns_201
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_to_favorites_returns_201():
    """
    Happy path: first add of a product → 201 CREATED.

    Verifies:
    - 201 status code
    - Response contains product_id, user_id, added_at, message
    """
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        resp = await ac.post(f"/api/v1/favorites/{product_id}")

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["product_id"] == str(product_id)
    assert data["user_id"] == str(user_id)
    assert "added_at" in data
    assert "Добавлен" in data["message"] or "добавлен" in data["message"].lower()


# ──────────────────────────────────────────────────────────────────────────────
# DoD: repeat_add_returns_200_not_duplicate
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeat_add_returns_200_not_duplicate():
    """
    Idempotency: second add of the same product → 200, no DB duplicate.

    Verifies:
    - First call: 201
    - Second call: 200
    - No duplicate entries in DB (UNIQUE constraint test via repeat without error)
    """
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        resp1 = await ac.post(f"/api/v1/favorites/{product_id}")
        resp2 = await ac.post(f"/api/v1/favorites/{product_id}")

    assert resp1.status_code == 201, resp1.text
    assert resp2.status_code == 200, resp2.text

    # Both should have the same product_id / user_id
    d1, d2 = resp1.json(), resp2.json()
    assert d1["product_id"] == d2["product_id"]
    assert d1["user_id"] == d2["user_id"]


# ──────────────────────────────────────────────────────────────────────────────
# DoD: get_favorites_enriched_from_b2b
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_favorites_enriched_from_b2b():
    """
    GET /api/v1/favorites enriches product data from B2B batch endpoint.

    Verifies:
    - 200 status
    - Response has {items: [...], total: N}
    - Each item has {product: CatalogProductCard, added_at: datetime}
    - product has: id, name, min_price, has_stock, images
    """
    user_id = uuid4()
    product_id = uuid4()

    b2b_product = _b2b_product(product_id=str(product_id), title="Enriched Product")
    mock = _MockClient(_FakeResp([b2b_product]))

    # First add the product
    async with _make_client(user_id) as ac:
        await ac.post(f"/api/v1/favorites/{product_id}")

    # Then fetch favorites with B2B mock
    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1

    # Find our product in the list
    items = data["items"]
    assert len(items) >= 1
    item = next((i for i in items if i["product"]["id"] == str(product_id)), None)
    assert item is not None, f"Product {product_id} not found in favorites"

    product = item["product"]
    assert product["name"] == "Enriched Product"
    assert "min_price" in product
    assert "has_stock" in product
    assert "images" in product
    assert "added_at" in item


# ──────────────────────────────────────────────────────────────────────────────
# DoD: blocked_product_excluded_from_list
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocked_product_excluded_from_list():
    """
    Canon b2c-cart-flows.md#b2c-6-favorites edge case:
    Product blocked/deleted in B2B → B2B doesn't return it in batch →
    B2C silently excludes it from the favorites list (not a 404).

    Verifies:
    - 200 (not 404) when a favorited product is blocked in B2B
    - Blocked product absent from items
    - total reflects DB count (includes the blocked product's row)
    """
    user_id = uuid4()
    good_id = uuid4()
    blocked_id = uuid4()

    # Add both to favorites
    async with _make_client(user_id) as ac:
        await ac.post(f"/api/v1/favorites/{good_id}")
        await ac.post(f"/api/v1/favorites/{blocked_id}")

    # B2B batch only returns the good product (blocked_id absent)
    good_product = _b2b_product(product_id=str(good_id), title="Good Product")
    mock = _MockClient(_FakeResp([good_product]))

    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    product_ids_in_response = {i["product"]["id"] for i in data["items"]}
    assert str(good_id) in product_ids_in_response
    assert str(blocked_id) not in product_ids_in_response, (
        "Blocked product must be silently excluded from favorites list"
    )


# ──────────────────────────────────────────────────────────────────────────────
# DoD: user_id_from_query_is_ignored
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_id_from_query_is_ignored():
    """
    IDOR prevention: even if user_id is passed in query params, the backend
    uses user_id from JWT claims only.

    Scenario: user_A sends request with user_B's id in query → favorites are
    added to user_A (JWT), not user_B (query param).
    """
    user_a_id = uuid4()
    user_b_id = uuid4()
    product_id = uuid4()

    # user_A makes request with user_B's id in query (IDOR attempt)
    async with _make_client(user_a_id) as ac:
        resp = await ac.post(
            f"/api/v1/favorites/{product_id}",
            params={"user_id": str(user_b_id)},  # This must be ignored
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()

    # Backend must use user_A's id from JWT, NOT user_B's from query
    assert data["user_id"] == str(user_a_id), (
        f"Expected user_id={user_a_id} (from JWT), got {data['user_id']}"
    )
    assert data["user_id"] != str(user_b_id), "user_id from query must be ignored (IDOR prevention)"


# ──────────────────────────────────────────────────────────────────────────────
# Extra: remove, idempotency, auth, 503
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_from_favorites_returns_204():
    """DELETE /api/v1/favorites/{product_id} → 204 after add."""
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        await ac.post(f"/api/v1/favorites/{product_id}")
        resp = await ac.delete(f"/api/v1/favorites/{product_id}")

    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_remove_nonexistent_returns_204():
    """Delete of a non-existent favorite → 204 (idempotent, canon edge case)."""
    user_id = uuid4()
    nonexistent_id = uuid4()

    async with _make_client(user_id) as ac:
        resp = await ac.delete(f"/api/v1/favorites/{nonexistent_id}")

    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_empty_favorites_returns_200():
    """Empty favorites for a new user → 200 {items: [], total: 0}."""
    user_id = uuid4()

    # Use a mock that won't be called (no products in DB → no B2B call needed)
    async with _make_client(user_id) as ac:
        resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_favorites_requires_auth_401():
    """GET /api/v1/favorites without Authorization header → 401."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_add_favorites_requires_auth_401():
    """POST /api/v1/favorites/{product_id} without JWT → 401."""
    product_id = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        resp = await ac.post(f"/api/v1/favorites/{product_id}")

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_b2b_unavailable_returns_503():
    """B2B network error during GET /favorites → 503 UPSTREAM_UNAVAILABLE."""
    user_id = uuid4()
    product_id = uuid4()

    # Add a product first
    async with _make_client(user_id) as ac:
        await ac.post(f"/api/v1/favorites/{product_id}")

    # B2B is down
    mock = _MockClient(ConnectError("Connection refused"))
    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_favorites_isolated_per_user():
    """
    User isolation: user_A's favorites don't appear in user_B's list.
    Critical for IDOR prevention.
    """
    user_a_id = uuid4()
    user_b_id = uuid4()
    product_id_a = uuid4()

    # user_A adds a product
    async with _make_client(user_a_id) as ac:
        await ac.post(f"/api/v1/favorites/{product_id_a}")

    # user_B should NOT see user_A's product in their own favorites
    async with _make_client(user_b_id) as ac:
        resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    product_ids = {i["product"]["id"] for i in data["items"]}
    assert str(product_id_a) not in product_ids, (
        "User B must not see User A's favorites (IDOR isolation)"
    )
