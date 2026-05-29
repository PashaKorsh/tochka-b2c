"""
US-B2C-04: Similar products via GET /api/v1/catalog/products/{product_id}/similar

Canon flow: b2c-catalog-flows.md#b2c-4-similar-products
Spec:       b2c/openapi.yaml — response: array of CatalogProductCard, limit param

Covered DoD scenarios:
  ✓ similar_returns_up_to_8_from_same_category
  ✓ empty_category_returns_200_empty_list
  ✓ unknown_product_returns_404

Extra:
  ✓ similar_excludes_current_product
  ✓ similar_respects_limit_param
  ✓ b2b_unavailable_returns_502
  ✓ deleted_product_returns_404
  ✓ non_moderated_product_returns_404

Algorithm (canon b2c-catalog-flows.md#b2c-4-similar-products):
  1. Fetch target product from B2B to validate existence and get category_id.
  2. Fetch a wider batch (limit*3, min 30) from same category for shuffle variety.
  3. Filter out current product_id from results.
  4. If len(similar) < limit: parent-category fallback — GET /categories/{id} for
     parent_id, then fetch from parent category and merge (deduplicated).
  5. random.shuffle + cap at limit.

Mock strategy: _get_similar makes up to 4 sequential httpx.AsyncClient calls.
  _SequenceMockClient iterates through a pre-loaded list of responses — one per call.
  When exhausted, returns empty {} so fallback path is skipped gracefully.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError, HTTPStatusError, Request as HttpxRequest, Response as HttpxResponse

from backend.main import app


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

PRODUCT_ID = str(uuid4())
CATEGORY_ID = str(uuid4())


def _make_b2b_product_detail(
    *,
    product_id: str | None = None,
    category_id: str | None = None,
    status: str = "MODERATED",
    deleted: bool = False,
) -> dict[str, Any]:
    """Build a B2B ProductPublicResponse dict for product detail endpoint."""
    return {
        "id": product_id or PRODUCT_ID,
        "seller_id": str(uuid4()),
        "category_id": category_id or CATEGORY_ID,
        "title": "Target Product",
        "slug": "target-product",
        "description": "A target product for similar search.",
        "status": status,
        "deleted": deleted,
        "images": [{"url": "https://cdn.example.com/main.jpg", "ordering": 0}],
        "characteristics": [],
        "skus": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }


def _make_b2b_short_product(
    *,
    product_id: str | None = None,
    title: str = "Similar Product",
    min_price: int = 5_000_00,
) -> dict[str, Any]:
    """Build a B2B ProductPublicShortResponse dict (catalog list item)."""
    return {
        "id": product_id or str(uuid4()),
        "title": title,
        "slug": f"slug-{title.lower().replace(' ', '-')}",
        "category_id": CATEGORY_ID,
        "min_price": min_price,
        "cover_image": "https://cdn.example.com/img.jpg",
        "created_at": "2026-01-01T00:00:00Z",
    }


def _b2b_catalog_page(items: list[dict]) -> dict:
    return {"items": items, "total_count": len(items), "limit": 20, "offset": 0}


PARENT_CATEGORY_ID = str(uuid4())


def _make_b2b_category(
    category_id: str = CATEGORY_ID,
    parent_id: str | None = None,
) -> dict:
    """Build a B2B CategoryResponse for the category detail endpoint."""
    return {
        "id": category_id,
        "name": "Electronics",
        "parent_id": parent_id,
        "level": 1 if parent_id else 0,
        "path": f"electronics/{category_id}" if parent_id else category_id,
        "children": [],
    }


class _FakeResp:
    def __init__(self, data: dict | None = None, status_code: int = 200):
        self._data = data or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = HttpxRequest("GET", "http://b2b/")
            raw = HttpxResponse(self.status_code, request=req)
            raise HTTPStatusError(
                f"HTTP {self.status_code}",
                request=req,
                response=raw,
            )


class _SequenceMockClient:
    """
    Async context-manager stub that serves responses in sequence.

    _get_similar now makes up to 4 sequential AsyncClient instantiations:
      Call 1 → product detail (validate existence / get category_id)
      Call 2 → catalog list in same category (wider batch for shuffle)
      Call 3 → category detail (get parent_id for fallback) [only if len<limit]
      Call 4 → catalog list in parent category              [only if parent_id found]

    When the response list is exhausted, returns an empty 200 response so that
    tests that don't mock the fallback path (calls 3-4) degrade gracefully:
    parent_id will be missing → fallback skipped → primary results returned.
    """

    def __init__(self, responses: list[_FakeResp | Exception]):
        self._responses = list(responses)
        self._index = 0

    def __call__(self, **kwargs):
        """Called when `httpx.AsyncClient(...)` is instantiated."""
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
        else:
            resp = _FakeResp({})  # graceful exhaustion: empty 200 → fallback skipped
        return _SingleResponseClient(resp)


class _SingleResponseClient:
    def __init__(self, response: _FakeResp | Exception):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, *args, **kwargs) -> _FakeResp:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ──────────────────────────────────────────────────────────────────────────────
# DoD scenario: similar_returns_up_to_8_from_same_category
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_returns_up_to_8_from_same_category(client):
    """
    Happy path: B2B returns N products in the same category.
    B2C proxies them as a list of CatalogProductCard (not paginated).

    Verifies:
    - 200 status
    - Response is a JSON array (not a dict with 'items' key)
    - Each item has: id, name, min_price, has_stock, images
    - Current product is NOT in the list
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, category_id=CATEGORY_ID)

    similar_items = [
        _make_b2b_short_product(title=f"Similar Product {i}")
        for i in range(8)
    ]
    catalog_page = _b2b_catalog_page(similar_items)

    mock = _SequenceMockClient([_FakeResp(product), _FakeResp(catalog_page)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Response must be a plain JSON array (spec: array of CatalogProductCard)
    assert isinstance(data, list), f"Expected array, got {type(data)}"
    assert len(data) == 8

    # Each item must have required CatalogProductCard fields
    for item in data:
        assert "id" in item
        assert "name" in item
        assert "min_price" in item
        assert "has_stock" in item
        assert "images" in item


# ──────────────────────────────────────────────────────────────────────────────
# DoD scenario: empty_category_returns_200_empty_list
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_category_returns_200_empty_list(client):
    """
    Canon b2c-catalog-flows.md#b2c-4-similar-products edge case:
    Category has no other products (only the target product) → 200 with [].
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID)

    # B2B catalog returns only the current product itself
    catalog_page = _b2b_catalog_page([
        _make_b2b_short_product(product_id=PRODUCT_ID, title="Target Product"),
    ])

    mock = _SequenceMockClient([_FakeResp(product), _FakeResp(catalog_page)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert data == []


# ──────────────────────────────────────────────────────────────────────────────
# DoD scenario: unknown_product_returns_404
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_product_returns_404(client):
    """
    Canon b2c-catalog-flows.md#b2c-4-similar-products edge case:
    B2B returns 404 for the target product → B2C returns 404 NOT_FOUND.
    """
    mock = _SequenceMockClient([_FakeResp(status_code=404)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# Extra: similar_excludes_current_product
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_excludes_current_product(client):
    """
    Canon step 3: current product must be excluded from the similar list
    even if B2B returns it in the category batch.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID)

    # B2B returns current product among others
    items = [
        _make_b2b_short_product(product_id=PRODUCT_ID, title="This is the target"),
        _make_b2b_short_product(title="Other A"),
        _make_b2b_short_product(title="Other B"),
    ]
    catalog_page = _b2b_catalog_page(items)

    mock = _SequenceMockClient([_FakeResp(product), _FakeResp(catalog_page)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    ids = {item["id"] for item in data}
    assert PRODUCT_ID not in ids, "Current product must not appear in similar list"
    assert len(data) == 2


# ──────────────────────────────────────────────────────────────────────────────
# Extra: similar_respects_limit_param
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_respects_limit_param(client):
    """
    Spec: limit param (default=10, max=50). When limit=3 is provided,
    response has at most 3 items.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID)

    # B2B returns more items than the limit
    items = [_make_b2b_short_product(title=f"P{i}") for i in range(10)]
    catalog_page = _b2b_catalog_page(items)

    mock = _SequenceMockClient([_FakeResp(product), _FakeResp(catalog_page)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                f"/api/v1/catalog/products/{PRODUCT_ID}/similar",
                params={"limit": 3},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) <= 3


# ──────────────────────────────────────────────────────────────────────────────
# Extra: b2b_unavailable_returns_502
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_b2b_unavailable_returns_502(client):
    """B2B network error on first call (product fetch) → 502 UPSTREAM_UNAVAILABLE."""
    mock = _SequenceMockClient([ConnectError("Connection refused")])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


# ──────────────────────────────────────────────────────────────────────────────
# Extra: deleted and non-moderated product → 404
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_deleted_product_returns_404(client):
    """Deleted product in B2B response → 404 (extra visibility guard)."""
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, deleted=True)
    mock = _SequenceMockClient([_FakeResp(product)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_similar_non_moderated_product_returns_404(client):
    """Non-MODERATED product (e.g. BLOCKED) → 404."""
    for bad_status in ("BLOCKED", "HARD_BLOCKED", "ON_MODERATION"):
        product = _make_b2b_product_detail(
            product_id=PRODUCT_ID, status=bad_status, deleted=False
        )
        mock = _SequenceMockClient([_FakeResp(product)])

        ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
            async with ac:
                resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}/similar")

        assert resp.status_code == 404, f"Expected 404 for status={bad_status}, got {resp.status_code}"
        assert resp.json()["code"] == "NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# Random shuffle: results are a valid subset (not ordered by date)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_results_are_shuffled(client):
    """
    ADR: B2B has no sort=random. B2C fetches a wide batch and shuffles in-place.
    Verify: result items are a valid subset of available products (no phantoms),
    and the count matches the limit. Exact order is not asserted.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, category_id=CATEGORY_ID)

    available = [_make_b2b_short_product(title=f"P{i}") for i in range(15)]
    catalog_page = _b2b_catalog_page(available)

    mock = _SequenceMockClient([_FakeResp(product), _FakeResp(catalog_page)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                f"/api/v1/catalog/products/{PRODUCT_ID}/similar",
                params={"limit": 8},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 8

    available_ids = {p["id"] for p in available}
    for item in data:
        assert item["id"] in available_ids, "Result contains an ID not from B2B response"
        assert item["id"] != PRODUCT_ID, "Current product must not be in results"


# ──────────────────────────────────────────────────────────────────────────────
# Parent-category fallback
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_similar_fallback_to_parent_category(client):
    """
    Canon §4 step 3: when primary category has fewer than `limit` visible products,
    B2C fetches from the parent category and merges results (deduplicated).

    Setup:
      - Primary category has 2 visible products (< limit=5).
      - Category detail returns parent_id.
      - Parent category has 4 more products.
    Expected: 2 + 4 = 6 items, capped at limit=5.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, category_id=CATEGORY_ID)

    primary_items = [_make_b2b_short_product(title=f"Primary {i}") for i in range(2)]
    primary_page = _b2b_catalog_page(primary_items)

    category_detail = _make_b2b_category(category_id=CATEGORY_ID, parent_id=PARENT_CATEGORY_ID)

    parent_items = [_make_b2b_short_product(title=f"Parent {i}") for i in range(4)]
    parent_page = _b2b_catalog_page(parent_items)

    mock = _SequenceMockClient([
        _FakeResp(product),           # Call 1: product detail
        _FakeResp(primary_page),      # Call 2: primary category products
        _FakeResp(category_detail),   # Call 3: category → get parent_id
        _FakeResp(parent_page),       # Call 4: parent category products
    ])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                f"/api/v1/catalog/products/{PRODUCT_ID}/similar",
                params={"limit": 5},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 5  # capped at limit

    # All results must be from the known pool (primary + parent), not the current product
    known_ids = {p["id"] for p in primary_items + parent_items}
    for item in data:
        assert item["id"] in known_ids, f"Unknown id {item['id']} in result"
        assert item["id"] != PRODUCT_ID


@pytest.mark.asyncio
async def test_similar_fallback_no_parent_returns_primary_results(client):
    """
    When category has no parent (root category), fallback is skipped.
    B2C returns whatever it found in the primary category.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, category_id=CATEGORY_ID)

    primary_items = [_make_b2b_short_product(title=f"P{i}") for i in range(3)]
    primary_page = _b2b_catalog_page(primary_items)

    # Category detail: parent_id = None (root category)
    category_detail = _make_b2b_category(category_id=CATEGORY_ID, parent_id=None)

    mock = _SequenceMockClient([
        _FakeResp(product),
        _FakeResp(primary_page),
        _FakeResp(category_detail),  # parent_id=None → fallback skipped
    ])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                f"/api/v1/catalog/products/{PRODUCT_ID}/similar",
                params={"limit": 10},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 3  # only primary results; no phantom items


@pytest.mark.asyncio
async def test_similar_fallback_deduplicates_across_categories(client):
    """
    If a product appears in both the primary and parent category response,
    it must appear only once in the result.
    """
    product = _make_b2b_product_detail(product_id=PRODUCT_ID, category_id=CATEGORY_ID)
    shared_id = str(uuid4())

    # Same product appears in both primary and parent category results
    primary_items = [_make_b2b_short_product(product_id=shared_id, title="Shared Product")]
    primary_page = _b2b_catalog_page(primary_items)

    category_detail = _make_b2b_category(category_id=CATEGORY_ID, parent_id=PARENT_CATEGORY_ID)

    parent_items = [
        _make_b2b_short_product(product_id=shared_id, title="Shared Product"),  # duplicate
        _make_b2b_short_product(title="Unique Parent Product"),
    ]
    parent_page = _b2b_catalog_page(parent_items)

    mock = _SequenceMockClient([
        _FakeResp(product),
        _FakeResp(primary_page),
        _FakeResp(category_detail),
        _FakeResp(parent_page),
    ])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                f"/api/v1/catalog/products/{PRODUCT_ID}/similar",
                params={"limit": 10},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    result_ids = [item["id"] for item in data]
    assert len(result_ids) == len(set(result_ids)), "Duplicate product in similar results"
    assert shared_id in result_ids
