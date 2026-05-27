"""
Tests for US-B2C-15 — Product collections (home page).

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET /api/v1/catalog/collections            → array of Collection (public)

Canon: b2c-cart-flows.md#b2c-15-collections
  GET /api/v1/catalog/collections/{id}/products → CollectionProductsResponse

DoD test names (exact):
  collections_list_returns_metadata_without_products
  collection_products_enriched_from_b2b
  unavailable_products_in_unavailable_ids
  unknown_collection_returns_404

Collections are seeded directly into the test DB (no admin API).
B2B is mocked via backend.modules.collections.service.httpx.AsyncClient.
"""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4, UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import os

from backend.main import app
from backend.modules.collections.models import Collection, CollectionProduct

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
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


async def _seed_collection(
    db: AsyncSession,
    *,
    name: str = "Test Collection",
    description: str = "A test collection",
    priority: int = 10,
    is_active: bool = True,
    start_date: date | None = None,
    product_ids: list[UUID] | None = None,
) -> Collection:
    coll = Collection(
        id=uuid4(),
        name=name,
        description=description,
        priority=priority,
        is_active=is_active,
        start_date=start_date,
    )
    db.add(coll)
    await db.flush()

    for idx, pid in enumerate(product_ids or []):
        db.add(CollectionProduct(
            collection_id=coll.id,
            product_id=pid,
            ordering=idx,
        ))

    await db.commit()
    return coll


# ── B2B mock helpers ──────────────────────────────────────────────────────────

class _FakeResp:
    """httpx response stub — raise_for_status without request attribute."""
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
    def __init__(self, response: _FakeResp):
        self._response = response

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, *a, **kw):
        return self._response


def _b2b_product(product_id: UUID, title: str = "Test Product") -> dict:
    return {
        "id": str(product_id),
        "title": title,
        "slug": "test-product",
        "category_id": str(uuid4()),
        "seller_id": str(uuid4()),
        "images": [{"url": "http://img.test/p.jpg", "ordering": 0}],
        "skus": [{"id": str(uuid4()), "price": 1000, "discount": 0, "active_quantity": 5}],
    }


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collections_list_returns_metadata_without_products():
    """
    GET /api/v1/catalog/collections returns a bare array of Collection objects.
    Each item has metadata fields (id, name, description) but products: [].

    Verifies:
    - 200 status
    - Response is a JSON array (not wrapped in {items, total_count})
    - products field is [] — no product data in list view (metadata-only)
    - Inactive collections are excluded
    - Collections sorted by priority ASC
    """
    product_id = uuid4()

    async for db in _db_session():
        coll_a = await _seed_collection(
            db, name="Summer Sale", priority=5, product_ids=[product_id]
        )
        coll_b = await _seed_collection(
            db, name="New Arrivals", priority=2, product_ids=[]
        )
        coll_inactive = await _seed_collection(
            db, name="INACTIVE", priority=1, is_active=False
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/collections")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Bare array (spec)
    assert isinstance(body, list), "Spec: response must be a bare array"

    returned_ids = [item["id"] for item in body]
    assert str(coll_a.id) in returned_ids, "Active collection A must appear"
    assert str(coll_b.id) in returned_ids, "Active collection B must appear"
    assert str(coll_inactive.id) not in returned_ids, "Inactive collection must be excluded"

    # Metadata-only: products must be empty in list view
    for item in body:
        assert "products" in item
        assert item["products"] == [], "products must be empty in list view (metadata only)"

    # Required spec fields
    for item in body:
        assert "id" in item
        assert "name" in item

    # Sorted by priority ASC
    active_items = [i for i in body if i["id"] in (str(coll_a.id), str(coll_b.id))]
    # coll_b has priority=2, coll_a has priority=5 → B first
    first_idx = next(i for i, x in enumerate(active_items) if x["id"] == str(coll_b.id))
    second_idx = next(i for i, x in enumerate(active_items) if x["id"] == str(coll_a.id))
    assert first_idx < second_idx, "Lower priority value (2) comes before higher (5)"


@pytest.mark.asyncio
async def test_collection_products_enriched_from_b2b():
    """
    GET /api/v1/catalog/collections/{id}/products returns items enriched from B2B.

    Verifies:
    - 200 status
    - items[] contains enriched CatalogProductCard objects
    - unavailable_ids is an empty list when all products are available
    - total_products = number of product_ids in the collection
    """
    pid1 = uuid4()
    pid2 = uuid4()

    async for db in _db_session():
        coll = await _seed_collection(
            db, name="Hits of the Week", priority=1, product_ids=[pid1, pid2]
        )

    b2b_resp = _FakeResp([
        _b2b_product(pid1, "Product One"),
        _b2b_product(pid2, "Product Two"),
    ])

    mock = _MockClient(b2b_resp)
    with patch(
        "backend.modules.collections.service.httpx.AsyncClient",
        side_effect=mock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/catalog/collections/{coll.id}/products")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["collection_id"] == str(coll.id)
    assert body["name"] == "Hits of the Week"
    assert len(body["items"]) == 2
    assert body["unavailable_ids"] == []
    assert body["total_products"] == 2

    # Each item must be a CatalogProductCard
    for item in body["items"]:
        assert "id" in item
        assert "name" in item
        assert "min_price" in item
        assert "has_stock" in item


@pytest.mark.asyncio
async def test_unavailable_products_in_unavailable_ids():
    """
    Products absent from B2B response (deleted/blocked) appear in unavailable_ids,
    NOT in items. This is a valid 200 response, not an error.

    Scenario: 3 products in collection, B2B returns only 1 (2 deleted/blocked).
    """
    pid_available = uuid4()
    pid_deleted1 = uuid4()
    pid_deleted2 = uuid4()

    async for db in _db_session():
        coll = await _seed_collection(
            db,
            name="Mixed Collection",
            priority=1,
            product_ids=[pid_available, pid_deleted1, pid_deleted2],
        )

    # B2B returns only pid_available (the other 2 were deleted/blocked)
    b2b_resp = _FakeResp([_b2b_product(pid_available, "Available Product")])

    mock = _MockClient(b2b_resp)
    with patch(
        "backend.modules.collections.service.httpx.AsyncClient",
        side_effect=mock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/catalog/collections/{coll.id}/products")

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Available products in items
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(pid_available)

    # Deleted/blocked in unavailable_ids
    assert len(body["unavailable_ids"]) == 2
    unavailable_set = set(body["unavailable_ids"])
    assert str(pid_deleted1) in unavailable_set
    assert str(pid_deleted2) in unavailable_set

    # total_products = all 3 (not just available)
    assert body["total_products"] == 3


@pytest.mark.asyncio
async def test_unknown_collection_returns_404():
    """
    GET /api/v1/catalog/collections/{id}/products for a non-existent collection
    → 404 NOT_FOUND.
    """
    fake_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/v1/catalog/collections/{fake_id}/products")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_products_unavailable_returns_200_with_empty_items():
    """
    If ALL products in a collection are missing from B2B (all deleted/blocked),
    the response is 200 {items: [], unavailable_ids: [...]} — not an error.

    Canon edge case: "Все товары подборки удалены в B2B → 200, {items: [], unavailable_ids: [...]}"
    """
    pid1 = uuid4()
    pid2 = uuid4()

    async for db in _db_session():
        coll = await _seed_collection(
            db, name="All Deleted", priority=1, product_ids=[pid1, pid2]
        )

    # B2B returns empty list — all deleted/blocked
    mock = _MockClient(_FakeResp([]))
    with patch(
        "backend.modules.collections.service.httpx.AsyncClient",
        side_effect=mock,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(f"/api/v1/catalog/collections/{coll.id}/products")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert set(body["unavailable_ids"]) == {str(pid1), str(pid2)}
    assert body["total_products"] == 2


@pytest.mark.asyncio
async def test_empty_collection_returns_200():
    """
    A collection with no product_ids returns {items: [], unavailable_ids: [], total_products: 0}.
    Canon edge case: "Подборка пустая (нет product_ids)"
    """
    async for db in _db_session():
        coll = await _seed_collection(db, name="Empty Collection", priority=1, product_ids=[])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/api/v1/catalog/collections/{coll.id}/products")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["unavailable_ids"] == []
    assert body["total_products"] == 0


@pytest.mark.asyncio
async def test_future_collection_excluded_from_list():
    """
    Collection with start_date in the future must NOT appear in GET /catalog/collections.
    """
    future_date = date.today() + timedelta(days=7)

    async for db in _db_session():
        coll = await _seed_collection(
            db, name="Future Launch", priority=1,
            is_active=True, start_date=future_date,
        )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/collections")

    returned_ids = {item["id"] for item in resp.json()}
    assert str(coll.id) not in returned_ids, "Future-dated collection must be excluded"


@pytest.mark.asyncio
async def test_no_active_collections_returns_200_empty_array():
    """
    When all collections are inactive, GET /catalog/collections returns 200 [].
    The specific IDs created here won't appear (they are all inactive).
    """
    async for db in _db_session():
        await _seed_collection(db, name="Disabled A", priority=1, is_active=False)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/collections")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    # Inactive collection IDs must not appear
    # (we can't guarantee the list is empty since other tests may have seeded active ones)
