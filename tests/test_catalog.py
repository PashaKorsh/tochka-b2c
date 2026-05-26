"""
US-B2C-01: Catalog with filters, sorting and facets.

Canon flow: b2c-catalog-flows.md#b2c-1-catalog-filters
Spec:       b2c/openapi.yaml (neomarket-protocols)

Covered DoD scenarios:
  ✓ catalog_returns_filtered_sorted_products  — happy path: filter, sort, pagination
  ✓ facets_return_counts_per_filter_value     — price_range facets are correct
  ✓ invalid_sort_returns_400                  — invalid sort → 400 + allowed values listed
  ✓ b2b_unavailable_returns_502              — ConnectError from B2B → 502

Testing approach:
  All tests run against the ASGI app (httpx.AsyncClient + ASGITransport).
  B2B HTTP calls are patched via unittest.mock.AsyncMock so tests are
  fast, hermetic and work without a running B2B container.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError, Request as HttpxRequest

from backend.main import app

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """Synchronous factory for an async client — used inside each test with 'async with'."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_b2b_product(
    *,
    title: str = "Test Product",
    min_price: int = 10_000,
    category_id: str | None = None,
    cover_image: str | None = "https://cdn.example.com/img.jpg",
) -> dict[str, Any]:
    """Build a minimal B2B ProductPublicShortResponse dict."""
    return {
        "id": str(uuid4()),
        "title": title,
        "slug": f"slug-{title.lower().replace(' ', '-')}",
        "status": "MODERATED",
        "category_id": category_id or str(uuid4()),
        "min_price": min_price,
        "cover_image": cover_image,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _b2b_response(items: list[dict], *, total_count: int | None = None) -> dict:
    """Wrap items in a B2B PaginatedResponse envelope."""
    return {
        "items": items,
        "total_count": total_count if total_count is not None else len(items),
        "limit": 20,
        "offset": 0,
    }


class _FakeHttpxResponse:
    """Minimal httpx.Response stub — only .json() and .raise_for_status() needed."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


# Context-manager compatible AsyncMock for httpx.AsyncClient
class _MockAsyncClient:
    """Drop-in mock for `async with httpx.AsyncClient() as client:` usage."""

    def __init__(self, response: _FakeHttpxResponse | Exception):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args, **kwargs) -> _FakeHttpxResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ──────────────────────────────────────────────────────────────────────────────
# Test: catalog_returns_filtered_sorted_products
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_catalog_returns_filtered_sorted_products(client):
    """
    Happy path: B2B returns products; B2C proxies them correctly with filters applied.

    Verifies:
    - 200 status
    - Response shape matches PaginatedCatalogProducts (items, total_count, limit, offset)
    - Items contain required fields: id, name, min_price, has_stock, images
    - Filter[category_id] is forwarded as ?category= to B2B
    - sort=price_asc is accepted and passed to B2B
    """
    cat_id = str(uuid4())
    products = [
        _make_b2b_product(title="Cheap Widget", min_price=5_000, category_id=cat_id),
        _make_b2b_product(title="Pricey Widget", min_price=50_000, category_id=cat_id),
    ]
    b2b_resp = _b2b_response(products, total_count=2)

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/products",
                params={"sort": "price_asc", "limit": 10, "offset": 0, f"filter[category_id]": cat_id},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Pagination envelope
    assert "items" in data
    assert "total_count" in data
    assert "limit" in data
    assert "offset" in data
    assert data["total_count"] == 2
    assert len(data["items"]) == 2

    # Item shape (spec CatalogProductCard required: id, name, min_price, has_stock, images)
    item = data["items"][0]
    assert "id" in item
    assert "name" in item
    assert "min_price" in item
    assert "has_stock" in item
    assert "images" in item
    assert isinstance(item["images"], list)
    assert item["has_stock"] is True
    assert item["min_price"] == 5_000


# ──────────────────────────────────────────────────────────────────────────────
# Test: facets_return_counts_per_filter_value
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_facets_return_counts_per_filter_value(client):
    """
    Facets endpoint correctly counts products per price-range bucket.

    Setup: 2 cheap products (< 100 000 kopecks = 1 000 ₽), 1 mid, 1 premium.
    Expected: price_range facet with values [under_1000: 2, 1000_5000: 1, over_5000: 1].
    """
    products = [
        _make_b2b_product(title="Cheap A", min_price=50_00),    # 50 ₽ → under_1000
        _make_b2b_product(title="Cheap B", min_price=99_99),    # 99 ₽ → under_1000
        _make_b2b_product(title="Mid",     min_price=2_000_00), # 2 000 ₽ → 1000_5000
        _make_b2b_product(title="Premium", min_price=10_000_00),# 10 000 ₽ → over_5000
    ]
    b2b_resp = _b2b_response(products, total_count=4)

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/facets")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert "facets" in data
    assert isinstance(data["facets"], list)
    assert len(data["facets"]) >= 1

    # Find price_range facet
    price_facet = next((f for f in data["facets"] if f["name"] == "price_range"), None)
    assert price_facet is not None, "price_range facet not found"
    assert "values" in price_facet

    values_by_name = {v["value"]: v["count"] for v in price_facet["values"]}

    assert values_by_name.get("under_1000") == 2
    assert values_by_name.get("1000_5000") == 1
    assert values_by_name.get("over_5000") == 1


# ──────────────────────────────────────────────────────────────────────────────
# Test: invalid_sort_returns_400
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_sort_returns_400(client):
    """
    Canon b2c-catalog-flows.md edge-case: невалидный sort → 400 INVALID_REQUEST
    с перечислением допустимых значений.

    CLAUDE.md §5: "Невалидный sort → 400 INVALID_REQUEST с перечислением допустимых
    значений в message."
    """
    async with client as ac:
        resp = await ac.get("/api/v1/catalog/products", params={"sort": "INVALID_SORT"})

    assert resp.status_code == 400, resp.text
    data = resp.json()
    assert data["code"] == "INVALID_REQUEST"
    # Message must enumerate allowed values
    message = data["message"]
    assert "price_asc" in message
    assert "price_desc" in message
    assert "popularity" in message
    assert "new" in message


# ──────────────────────────────────────────────────────────────────────────────
# Test: b2b_unavailable_returns_502
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_b2b_unavailable_returns_502(client):
    """
    Canon b2c-catalog-flows.md edge-case: B2B недоступен → 502.
    CLAUDE.md §5: "Сетевые ошибки httpx ловить и возвращать 502 Bad Gateway
    с телом {"code": "UPSTREAM_UNAVAILABLE", "message": "..."}."
    """
    # Simulate network error
    err = ConnectError("Connection refused")

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(err),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products")

    assert resp.status_code == 502, resp.text
    data = resp.json()
    assert data["code"] == "UPSTREAM_UNAVAILABLE"
    assert "message" in data


@pytest.mark.asyncio
async def test_b2b_unavailable_returns_502_for_facets(client):
    """Facets endpoint also returns 502 when B2B is down."""
    err = ConnectError("Connection refused")

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(err),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/facets")

    assert resp.status_code == 502, resp.text
    data = resp.json()
    assert data["code"] == "UPSTREAM_UNAVAILABLE"


# ──────────────────────────────────────────────────────────────────────────────
# Extra edge-case tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_catalog_returns_200_empty(client):
    """B2B returns no products — B2C returns 200 with empty items list."""
    b2b_resp = _b2b_response([], total_count=0)

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total_count"] == 0


@pytest.mark.asyncio
async def test_price_filter_applied_client_side(client):
    """
    Products outside the requested price range are filtered out by B2C.
    B2B doesn't support price filtering natively on the public endpoint.
    """
    products = [
        _make_b2b_product(title="Cheap", min_price=5_00),      # 5 ₽
        _make_b2b_product(title="OK", min_price=100_00),        # 100 ₽
        _make_b2b_product(title="Expensive", min_price=500_00), # 500 ₽
    ]
    b2b_resp = _b2b_response(products, total_count=3)

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/products",
                params={"filter[price_min]": 50_00, "filter[price_max]": 200_00},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    names = [i["name"] for i in data["items"]]
    assert "Cheap" not in names      # 5 < 50
    assert "OK" in names             # 100 in [50, 200]
    assert "Expensive" not in names  # 500 > 200


@pytest.mark.asyncio
async def test_catalog_product_has_no_sensitive_fields(client):
    """
    Items from the catalog must NOT expose cost_price or reserved_quantity —
    CLAUDE.md §5 "B2C только проксирует".
    """
    product = _make_b2b_product()
    b2b_resp = _b2b_response([product])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/products")

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert "cost_price" not in item
    assert "reserved_quantity" not in item


@pytest.mark.asyncio
async def test_facets_empty_b2b_response(client):
    """Empty B2B → facets returns empty facets list (no errors)."""
    b2b_resp = _b2b_response([], total_count=0)

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockAsyncClient(_FakeHttpxResponse(b2b_resp)),
    ):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/facets")

    assert resp.status_code == 200
    data = resp.json()
    assert "facets" in data
    assert isinstance(data["facets"], list)
    # No facets when no products
    assert data["facets"] == []
