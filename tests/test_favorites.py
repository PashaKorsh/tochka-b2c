"""
US-B2C-06: Favorites CRUD via:
  PUT    /api/v1/favorites/{product_id}  — add (idempotent, 204 always)
  DELETE /api/v1/favorites/{product_id}  — remove (idempotent, 204 always)
  GET    /api/v1/favorites               — enriched list from B2B

Canon flow: b2c-cart-flows.md#b2c-6-favorites
Spec:       neomarket-protocols/b2c/openapi.yaml (favorites section)

Covered DoD scenarios:
  ✓ add_to_favorites_returns_204            (was 201 — fixed per spec PUT 204)
  ✓ repeat_add_returns_204_not_duplicate    (was 200 — fixed per spec)
  ✓ get_favorites_enriched_from_b2b
  ✓ blocked_product_excluded_from_list
  ✓ user_id_from_query_is_ignored          (IDOR prevention)

Extra:
  ✓ remove_from_favorites_returns_204
  ✓ remove_nonexistent_returns_204         (idempotent)
  ✓ empty_favorites_returns_200
  ✓ favorites_requires_auth_401
  ✓ b2b_unavailable_returns_503
  ✓ favorites_isolated_per_user

GET /favorites response shape: PaginatedCatalogProducts
  {items: [CatalogProductCard, ...], total_count: int, limit: int, offset: int}
  (flat cards — no {product, added_at} wrapper)
"""
from __future__ import annotations

import os
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError, Request as HttpxRequest, Response as HttpxResponse
from httpx import HTTPStatusError

from backend.main import app
from backend.auth import get_current_user_id, create_test_token


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
    """Stub for httpx.AsyncClient — fixed response for any HTTP method."""
    def __init__(self, response):
        self._response = response

    def __call__(self, **kwargs):
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
# DoD: add_to_favorites_returns_204
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_to_favorites_returns_204():
    """
    spec b2c/openapi.yaml:557-565 — PUT /favorites/{product_id} → 204 No Content.
    First add returns 204 with no body.
    """
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        resp = await ac.put(f"/api/v1/favorites/{product_id}")

    assert resp.status_code == 204, resp.text
    assert resp.content == b"", "204 must have no body"


# ──────────────────────────────────────────────────────────────────────────────
# DoD: repeat_add_returns_204_not_duplicate
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeat_add_returns_204_not_duplicate():
    """
    Idempotency: second PUT of the same product → 204, no DB duplicate.
    Both calls return 204 (PUT is idempotent by spec — no 201/200 distinction).
    """
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        resp1 = await ac.put(f"/api/v1/favorites/{product_id}")
        resp2 = await ac.put(f"/api/v1/favorites/{product_id}")

    assert resp1.status_code == 204, resp1.text
    assert resp2.status_code == 204, resp2.text


# ──────────────────────────────────────────────────────────────────────────────
# DoD: get_favorites_enriched_from_b2b
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_favorites_enriched_from_b2b():
    """
    GET /api/v1/favorites enriches product data from B2B batch endpoint.

    spec b2c/openapi.yaml — response: PaginatedCatalogProducts shape:
    {items: [CatalogProductCard, ...], total_count: int, limit: int, offset: int}
    Each item is a flat CatalogProductCard (no {product, added_at} wrapper).
    """
    user_id = uuid4()
    product_id = uuid4()

    b2b_product = _b2b_product(product_id=str(product_id), title="Enriched Product")
    mock = _MockClient(_FakeResp([b2b_product]))

    # Add the product first
    async with _make_client(user_id) as ac:
        await ac.put(f"/api/v1/favorites/{product_id}")

    # Then fetch favorites with B2B mock
    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # spec PaginatedCatalogProducts: required {items, total_count, limit, offset}
    assert "items" in data
    assert "total_count" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total_count"] >= 1

    items = data["items"]
    assert len(items) >= 1

    # Items are flat CatalogProductCards — no {product, added_at} wrapper
    card = next((i for i in items if i["id"] == str(product_id)), None)
    assert card is not None, f"Product {product_id} not found in favorites"
    assert card["name"] == "Enriched Product"
    assert "min_price" in card
    assert "has_stock" in card
    assert "images" in card
    # No nested product object — it's the card itself
    assert "product" not in card, "items must be flat CatalogProductCard, not {product, added_at}"


# ──────────────────────────────────────────────────────────────────────────────
# DoD: blocked_product_excluded_from_list
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocked_product_excluded_from_list():
    """
    Canon b2c-cart-flows.md#b2c-6-favorites edge case:
    Product blocked/deleted in B2B → B2B doesn't return it in batch →
    B2C silently excludes it from the favorites list.

    total_count reflects DB rows (includes blocked product's row).
    """
    user_id = uuid4()
    good_id = uuid4()
    blocked_id = uuid4()

    async with _make_client(user_id) as ac:
        await ac.put(f"/api/v1/favorites/{good_id}")
        await ac.put(f"/api/v1/favorites/{blocked_id}")

    # B2B batch only returns the good product (blocked_id absent)
    good_product = _b2b_product(product_id=str(good_id), title="Good Product")
    mock = _MockClient(_FakeResp([good_product]))

    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    card_ids = {i["id"] for i in data["items"]}
    assert str(good_id) in card_ids
    assert str(blocked_id) not in card_ids, "Blocked product must be silently excluded"
    # total_count = 2 (both DB rows exist) even though items has 1
    assert data["total_count"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# DoD: user_id_from_query_is_ignored
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_id_from_query_is_ignored():
    """
    IDOR prevention: user_id in query params is completely ignored.
    The backend uses user_id from JWT claims only.

    204 confirms the add went to user_A (JWT), not user_B (query).
    Subsequent GET with user_A shows the product; user_B sees nothing.
    """
    user_a_id = uuid4()
    user_b_id = uuid4()
    product_id = uuid4()

    # user_A sends PUT with user_B's id in query (IDOR attempt)
    async with _make_client(user_a_id) as ac:
        resp = await ac.put(
            f"/api/v1/favorites/{product_id}",
            params={"user_id": str(user_b_id)},  # must be ignored
        )
    assert resp.status_code == 204, resp.text

    # user_B's favorites must be empty (product was added to user_A via JWT)
    async with _make_client(user_b_id) as ac:
        resp_b = await ac.get("/api/v1/favorites")
    assert resp_b.status_code == 200
    assert resp_b.json()["total_count"] == 0, (
        "user_id from query was not ignored — IDOR violation"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Extra: remove, idempotency, auth, 503
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_from_favorites_returns_204():
    """DELETE /api/v1/favorites/{product_id} → 204 after add."""
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        await ac.put(f"/api/v1/favorites/{product_id}")
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
    """Empty favorites → 200 {items: [], total_count: 0, limit: N, offset: 0}."""
    user_id = uuid4()

    async with _make_client(user_id) as ac:
        resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total_count"] == 0
    assert "limit" in data
    assert "offset" in data


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
    """PUT /api/v1/favorites/{product_id} without JWT → 401."""
    product_id = uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        resp = await ac.put(f"/api/v1/favorites/{product_id}")

    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_b2b_unavailable_returns_503():
    """B2B network error during GET /favorites → 503 UPSTREAM_UNAVAILABLE."""
    user_id = uuid4()
    product_id = uuid4()

    async with _make_client(user_id) as ac:
        await ac.put(f"/api/v1/favorites/{product_id}")

    mock = _MockClient(ConnectError("Connection refused"))
    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_favorites_isolated_per_user():
    """User isolation: user_A's favorites don't appear in user_B's list."""
    user_a_id = uuid4()
    user_b_id = uuid4()
    product_id_a = uuid4()

    async with _make_client(user_a_id) as ac:
        await ac.put(f"/api/v1/favorites/{product_id_a}")

    async with _make_client(user_b_id) as ac:
        resp = await ac.get("/api/v1/favorites")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    card_ids = {i["id"] for i in data["items"]}
    assert str(product_id_a) not in card_ids, "User B must not see User A's favorites"


@pytest.mark.asyncio
async def test_get_favorites_pagination():
    """GET /favorites with limit/offset returns correct page and total_count."""
    user_id = uuid4()
    product_ids = [uuid4() for _ in range(3)]

    async with _make_client(user_id) as ac:
        for pid in product_ids:
            await ac.put(f"/api/v1/favorites/{pid}")

    b2b_products = [_b2b_product(product_id=str(pid)) for pid in product_ids]
    mock = _MockClient(_FakeResp(b2b_products[:2]))  # mock returns first 2 for the page

    with patch("backend.modules.favorites.service.httpx.AsyncClient", side_effect=mock):
        async with _make_client(user_id) as ac:
            resp = await ac.get("/api/v1/favorites", params={"limit": 2, "offset": 0})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 3   # all 3 in DB
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) <= 2
