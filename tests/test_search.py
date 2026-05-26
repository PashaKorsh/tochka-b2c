"""
US-B2C-02: Full-text search via GET /api/v1/catalog/products?q=...

Canon flow: b2c-catalog-flows.md#b2c-2-search
Spec:       b2c/openapi.yaml (neomarket-protocols) — parameter `q`, maxLength: 200

Covered DoD scenarios:
  ✓ search_returns_matching_products      — happy path: B2B results proxied correctly
  ✓ short_query_returns_400               — len(q.strip()) < 3 → 400 INVALID_REQUEST
  ✓ special_chars_do_not_break_query      — %, _, ' are forwarded; no crash
  ✓ empty_results_returns_200             — B2B returns [] → 200 with items:[]

Extra edge cases:
  ✓ long_query_returns_400               — len > 200 chars → 400
  ✓ search_combines_with_category_filter  — q + filter[category_id] both forwarded
  ✓ whitespace_only_query_returns_400    — strip() catches "   " → 400
  ✓ exactly_3_chars_is_valid             — boundary: 3 chars accepted

Implementation note (ADR for PR):
  Three search approaches were considered:
    1. SQL LIKE / icontains via B2B — simplest; zero new infra; B2B already has the
       data and applies visibility filter together with search. No relevance ranking.
    2. pg_trgm trigram index — better fuzzy matching, still in-DB; needs B2B migration.
    3. Full-text SearchVector (tsvector) — best relevance ranking (TF-IDF); needs index
       on title+description, language-aware stemming.
  Chosen: SQL LIKE proxied through B2B (approach 1).
  Criteria: (a) minimal implementation complexity on MVP — no new indexes or migrations;
  (b) visibility filter stays co-located with the search predicate in one B2B query,
  preventing stale-data bugs. pg_trgm / tsvector can replace LIKE in B2B later
  without changing the B2C contract.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError

from backend.main import app


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers (same as test_catalog.py — kept local for readability)
# ──────────────────────────────────────────────────────────────────────────────

def _make_b2b_product(
    *,
    title: str = "Test Product",
    min_price: int = 10_000,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "title": title,
        "slug": f"slug-{title.lower().replace(' ', '-')}",
        "status": "MODERATED",
        "category_id": str(uuid4()),
        "min_price": min_price,
        "cover_image": "https://cdn.example.com/img.jpg",
        "created_at": "2026-01-01T00:00:00Z",
    }


def _b2b_page(items: list[dict]) -> dict:
    return {"items": items, "total_count": len(items), "limit": 20, "offset": 0}


class _FakeResp:
    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _MockClient:
    """Async context-manager stub for httpx.AsyncClient."""

    def __init__(self, response: _FakeResp | Exception, *, capture: list | None = None):
        self._response = response
        self._capture = capture  # if set, appends kwargs from each .get() call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url: str, **kwargs) -> _FakeResp:
        if self._capture is not None:
            self._capture.append({"url": url, **kwargs})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ──────────────────────────────────────────────────────────────────────────────
# test_search_returns_matching_products
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_returns_matching_products(client):
    """
    Happy path: B2B returns matching products; B2C wraps them in PaginatedCatalogProducts.

    Verifies:
    - 200 response
    - items contain the expected product names
    - search param `q` is forwarded to B2B as `search`
    """
    products = [
        _make_b2b_product(title="Беспроводные наушники Sony"),
        _make_b2b_product(title="Наушники Apple AirPods"),
    ]
    calls: list[dict] = []

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(_b2b_page(products)), capture=calls),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products", params={"q": "наушники"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_count"] == 2
    names = {item["name"] for item in data["items"]}
    assert "Беспроводные наушники Sony" in names
    assert "Наушники Apple AirPods" in names

    # Verify search was forwarded to B2B
    assert len(calls) == 1
    forwarded_params = calls[0].get("params", {})
    assert forwarded_params.get("search") == "наушники"


# ──────────────────────────────────────────────────────────────────────────────
# test_short_query_returns_400
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_short_query_returns_400(client):
    """
    Canon b2c-catalog-flows.md#b2c-2-search edge case:
    query shorter than 3 chars → 400 INVALID_REQUEST (no B2B call made).
    """
    async with client as ac:
        resp_one = await ac.get("/api/v1/catalog/products", params={"q": "ab"})
        resp_single = await ac.get("/api/v1/catalog/products", params={"q": "a"})
        resp_empty = await ac.get("/api/v1/catalog/products", params={"q": ""})

    for resp in (resp_one, resp_single):
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "INVALID_REQUEST"
        assert "3" in body["message"]  # message mentions minimum length

    # Empty string: 0 chars < 3 → also 400
    assert resp_empty.status_code == 400, resp_empty.text


@pytest.mark.asyncio
async def test_whitespace_only_query_returns_400(client):
    """Spaces-only is treated as effectively empty → 400 (strip before length check)."""
    async with client as ac:
        resp = await ac.get("/api/v1/catalog/products", params={"q": "   "})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "INVALID_REQUEST"


# ──────────────────────────────────────────────────────────────────────────────
# test_special_chars_do_not_break_query
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_special_chars_do_not_break_query(client):
    """
    Canon b2c-catalog-flows.md#b2c-2-search edge case:
    %, _, ' are forwarded verbatim to B2B — B2B is responsible for escaping.
    B2C must not crash or return 5xx on special chars.
    """
    special_inputs = [
        "iPhone%15",   # URL percent char
        "кофе'мол",    # single quote (SQL injection risk for raw LIKE)
        "usb_cable",   # SQL LIKE wildcard
        "100% wool",   # percent in text
    ]

    for query in special_inputs:
        b2b_resp = _FakeResp(_b2b_page([_make_b2b_product(title=query)]))
        with patch(
            "backend.modules.catalog.service.httpx.AsyncClient",
            return_value=_MockClient(b2b_resp),
        ):
            # Create a fresh client per iteration — AsyncClient cannot be reopened
            ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            async with ac:
                resp = await ac.get("/api/v1/catalog/products", params={"q": query})

        assert resp.status_code == 200, f"Failed for query={query!r}: {resp.text}"
        data = resp.json()
        # Response is valid — no crash
        assert "items" in data


# ──────────────────────────────────────────────────────────────────────────────
# test_empty_results_returns_200
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_results_returns_200(client):
    """
    Canon b2c-catalog-flows.md#b2c-2-search edge case:
    B2B finds no products for the query → 200 with items:[], total_count:0.
    """
    b2b_resp = _FakeResp(_b2b_page([]))

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(b2b_resp),
    ):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/products", params={"q": "xtremely-rare-product-xyz"}
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Extra edge-case tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_long_query_returns_400(client):
    """Query longer than 200 chars → 400 (spec b2c/openapi.yaml q.maxLength: 200)."""
    long_q = "а" * 201
    async with client as ac:
        resp = await ac.get("/api/v1/catalog/products", params={"q": long_q})
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_exactly_3_chars_is_valid(client):
    """Boundary: exactly 3 non-space chars → search proceeds normally (no 400)."""
    b2b_resp = _FakeResp(_b2b_page([]))
    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(b2b_resp),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products", params={"q": "abc"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_search_combines_with_category_filter(client):
    """
    Search + filter[category_id] both forwarded to B2B in the same call.
    Verifies that US-B2C-02 is compatible with US-B2C-01 filters.
    """
    cat_id = str(uuid4())
    products = [_make_b2b_product(title="Sony WH-1000XM5")]
    calls: list[dict] = []

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(_b2b_page(products)), capture=calls),
    ):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/products",
                params={"q": "Sony", f"filter[category_id]": cat_id},
            )

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    params = calls[0].get("params", {})
    assert params.get("search") == "Sony"
    assert params.get("category") == cat_id


@pytest.mark.asyncio
async def test_search_no_q_returns_full_catalog(client):
    """
    Without `q` the endpoint behaves as a regular catalog (no search applied).
    B2B is called without `search` param.
    """
    products = [_make_b2b_product(title="Any product")]
    calls: list[dict] = []

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(_b2b_page(products)), capture=calls),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products")

    assert resp.status_code == 200
    assert calls[0].get("params", {}).get("search") is None
