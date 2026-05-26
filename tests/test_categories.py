"""
US-B2C-05: Category navigation via:
  GET /api/v1/catalog/categories          — flat list (spec b2c/openapi.yaml)
  GET /api/v1/catalog/categories/tree     — nested tree (spec b2c/openapi.yaml)
  GET /api/v1/catalog/categories/{id}     — category detail
  GET /api/v1/catalog/breadcrumbs         — breadcrumb chain (canon b2c-catalog-flows.md#b2c-5)

Canon flow: b2c-catalog-flows.md#b2c-5-category-nav
Spec:       neomarket-protocols/b2c/openapi.yaml (CategoryRef, CategoryTreeNode)
            neomarket-canon/apis/b2c/catalog/openapi.yaml (breadcrumb_response)

Covered DoD scenarios:
  ✓ category_tree_returns_nested_structure
  ✓ breadcrumbs_return_path_from_root
  ✓ unknown_category_returns_404
  ✓ orphan_node_returns_422
  ✓ ambiguous_params_returns_400

Extra:
  ✓ flat_categories_returns_list
  ✓ breadcrumbs_via_product_id
  ✓ missing_params_returns_400
  ✓ b2b_unavailable_returns_502
  ✓ no_params_breadcrumbs_returns_400

ADR — hierarchy storage strategy (for PR description):
  Three approaches considered for building the category tree on B2C:
    (a) ltree PostgreSQL — fast for deep hierarchies, but requires a local B2C
        schema for categories, violating the "B2C is a proxy" principle.
    (b) Adjacency list with in-memory traversal (chosen) — works with B2B's
        flat API response. O(n) tree build; orphan detection is a single set-
        membership check. No local DB schema needed.
    (c) Materialized path stored in B2C Redis cache — fast reads, but adds
        infrastructure and cache-invalidation complexity.
  Criteria: (1) B2C has no local category store (proxy principle); (2) orphan
  detection is a hard requirement — adjacency list makes it trivial.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import AsyncClient, ASGITransport, ConnectError, HTTPStatusError, Request as HttpxRequest, Response as HttpxResponse

from backend.main import app


# ──────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ──────────────────────────────────────────────────────────────────────────────

# UUIDs for a 3-level hierarchy: Electronics → Smartphones → Android
CAT_ROOT   = str(uuid4())   # Электроника,  level=0, parent=None
CAT_CHILD  = str(uuid4())   # Смартфоны,    level=1, parent=ROOT
CAT_LEAF   = str(uuid4())   # Android,      level=2, parent=CHILD
PRODUCT_ID = str(uuid4())


def _b2b_category(
    *,
    cat_id: str,
    name: str,
    parent_id: str | None,
    level: int,
    path: str = "",
    is_active: bool = True,
) -> dict[str, Any]:
    """Build a B2B CategoryResponse dict."""
    return {
        "id": cat_id,
        "name": name,
        "parent_id": parent_id,
        "level": level,
        "path": path,
        "is_active": is_active,
        "created_at": "2024-01-01T00:00:00Z",
    }


def _flat_3level() -> list[dict]:
    """Standard 3-level flat list: Электроника → Смартфоны → Android."""
    return [
        _b2b_category(cat_id=CAT_ROOT,  name="Электроника", parent_id=None,      level=0, path="electronics"),
        _b2b_category(cat_id=CAT_CHILD, name="Смартфоны",   parent_id=CAT_ROOT,  level=1, path="electronics/phones"),
        _b2b_category(cat_id=CAT_LEAF,  name="Android",     parent_id=CAT_CHILD, level=2, path="electronics/phones/android"),
    ]


def _b2b_product_detail(
    *,
    product_id: str | None = None,
    category_id: str | None = None,
    status: str = "MODERATED",
    deleted: bool = False,
) -> dict[str, Any]:
    return {
        "id": product_id or PRODUCT_ID,
        "seller_id": str(uuid4()),
        "category_id": category_id or CAT_LEAF,
        "title": "Test Product",
        "slug": "test-product",
        "description": "...",
        "status": status,
        "deleted": deleted,
        "images": [],
        "characteristics": [],
        "skus": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }


class _FakeResp:
    def __init__(self, data=None, status_code: int = 200):
        self._data = data if data is not None else {}
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            req = HttpxRequest("GET", "http://b2b/")
            raw = HttpxResponse(self.status_code, request=req)
            raise HTTPStatusError(f"HTTP {self.status_code}", request=req, response=raw)


class _SequenceMockClient:
    """
    Mock for httpx.AsyncClient that serves responses in sequence.
    Each new __call__ (i.e., each `httpx.AsyncClient(...)` instantiation) pops
    the next response from the list.
    """
    def __init__(self, responses: list):
        self._responses = list(responses)
        self._index = 0

    def __call__(self, **kwargs):
        resp = self._responses[self._index]
        self._index += 1
        return _SingleRespClient(resp)


class _SingleRespClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ──────────────────────────────────────────────────────────────────────────────
# DoD: category_tree_returns_nested_structure
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_category_tree_returns_nested_structure(client):
    """
    Happy path: B2B flat list → B2C builds nested tree.

    Verifies:
    - 200 status
    - Response is a JSON array (top-level roots only)
    - Root has children, leaf has empty children
    - All nodes have: id, name, level, path
    """
    mock = _SequenceMockClient([_FakeResp(_flat_3level())])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Top-level is a list of root nodes
    assert isinstance(data, list)
    assert len(data) == 1, "Expected one root (Электроника)"

    root = data[0]
    assert root["id"] == CAT_ROOT
    assert root["name"] == "Электроника"
    assert root["level"] == 0
    assert root["parent_id"] is None
    assert isinstance(root["children"], list)
    assert len(root["children"]) == 1

    child = root["children"][0]
    assert child["id"] == CAT_CHILD
    assert child["name"] == "Смартфоны"
    assert child["level"] == 1
    assert isinstance(child["children"], list)
    assert len(child["children"]) == 1

    leaf = child["children"][0]
    assert leaf["id"] == CAT_LEAF
    assert leaf["name"] == "Android"
    assert leaf["level"] == 2
    assert leaf["children"] == []


# ──────────────────────────────────────────────────────────────────────────────
# DoD: breadcrumbs_return_path_from_root
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_breadcrumbs_return_path_from_root(client):
    """
    Happy path: breadcrumbs for a leaf category chain from root to leaf.

    Verifies:
    - 200 status
    - response.data is ordered root → current
    - First item: level=0, is_current=False
    - Last item:  is_current=True
    - meta.resolved_via == "category_id"
    """
    mock = _SequenceMockClient([_FakeResp(_flat_3level())])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"category_id": CAT_LEAF},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]

    assert len(data) == 3, "Expected 3 breadcrumbs: root → child → leaf"
    assert data[0]["id"] == CAT_ROOT
    assert data[0]["level"] == 0
    assert data[0]["is_current"] is False

    assert data[1]["id"] == CAT_CHILD
    assert data[1]["is_current"] is False

    assert data[2]["id"] == CAT_LEAF
    assert data[2]["is_current"] is True

    # Meta
    meta = body["meta"]
    assert meta["resolved_via"] == "category_id"
    assert meta["category_id"] == CAT_LEAF


# ──────────────────────────────────────────────────────────────────────────────
# DoD: unknown_category_returns_404
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_category_returns_404(client):
    """
    Canon b2c-catalog-flows.md#b2c-5-category-nav edge case:
    Requesting a category_id that is not in B2B flat list → 404 NOT_FOUND.
    """
    unknown_id = str(uuid4())
    # B2B returns an empty list (no categories at all)
    mock = _SequenceMockClient([_FakeResp([])])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"category_id": unknown_id},
            )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_unknown_category_detail_returns_404(client):
    """GET /api/v1/catalog/categories/{id} for unknown id → 404."""
    unknown_id = str(uuid4())
    mock = _SequenceMockClient([_FakeResp(status_code=404)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/categories/{unknown_id}")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# DoD: orphan_node_returns_422
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orphan_node_returns_422(client):
    """
    Canon b2c-catalog-flows.md#b2c-5-category-nav edge case:
    A category's parent_id references a non-existent node → 422 ORPHAN_NODE.

    Scenario: B2B returns a flat list where CAT_CHILD's parent (CAT_ROOT)
    is absent from the list — making CAT_CHILD an orphan.
    """
    broken_flat = [
        # CAT_ROOT is intentionally missing → CAT_CHILD is orphaned
        _b2b_category(cat_id=CAT_CHILD, name="Смартфоны", parent_id=CAT_ROOT, level=1, path="electronics/phones"),
        _b2b_category(cat_id=CAT_LEAF,  name="Android",   parent_id=CAT_CHILD, level=2, path="electronics/phones/android"),
    ]
    mock = _SequenceMockClient([_FakeResp(broken_flat)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "ORPHAN_NODE"


@pytest.mark.asyncio
async def test_orphan_node_in_breadcrumbs_returns_422(client):
    """
    Breadcrumb traversal: if the chain reaches a parent_id not in the flat list → 422.
    """
    broken_flat = [
        # CAT_ROOT is absent; CAT_CHILD references it → orphan when traversing from CAT_LEAF
        _b2b_category(cat_id=CAT_CHILD, name="Смартфоны", parent_id=CAT_ROOT, level=1, path="electronics/phones"),
        _b2b_category(cat_id=CAT_LEAF,  name="Android",   parent_id=CAT_CHILD, level=2, path="electronics/phones/android"),
    ]
    mock = _SequenceMockClient([_FakeResp(broken_flat)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"category_id": CAT_LEAF},
            )

    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "ORPHAN_NODE"


# ──────────────────────────────────────────────────────────────────────────────
# DoD: ambiguous_params_returns_400
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ambiguous_params_returns_400(client):
    """
    Canon b2c-catalog-flows.md#b2c-5-category-nav edge case:
    Breadcrumbs with BOTH category_id and product_id → 400 INVALID_REQUEST.
    """
    async with client as ac:
        resp = await ac.get(
            "/api/v1/catalog/breadcrumbs",
            params={"category_id": CAT_LEAF, "product_id": PRODUCT_ID},
        )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "INVALID_REQUEST"
    assert "only one" in body["message"].lower()


@pytest.mark.asyncio
async def test_missing_params_returns_400(client):
    """
    Neither category_id nor product_id → 400 INVALID_REQUEST.
    """
    async with client as ac:
        resp = await ac.get("/api/v1/catalog/breadcrumbs")

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "INVALID_REQUEST"


# ──────────────────────────────────────────────────────────────────────────────
# Extra: flat list, breadcrumbs via product_id, 502
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flat_categories_returns_list(client):
    """GET /api/v1/catalog/categories → flat JSON array of CategoryRef."""
    mock = _SequenceMockClient([_FakeResp(_flat_3level())])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/categories")

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 3

    ids = {item["id"] for item in data}
    assert CAT_ROOT in ids
    assert CAT_CHILD in ids
    assert CAT_LEAF in ids

    # Each item has required CategoryRef fields
    for item in data:
        assert "id" in item
        assert "name" in item
        assert "level" in item
        assert "path" in item


@pytest.mark.asyncio
async def test_breadcrumbs_via_product_id(client):
    """
    product_id → resolve category_id from B2B product detail → build chain.
    Two sequential B2B calls: product detail + flat category list.
    """
    product = _b2b_product_detail(product_id=PRODUCT_ID, category_id=CAT_LEAF)
    mock = _SequenceMockClient([
        _FakeResp(product),          # call 1: product detail
        _FakeResp(_flat_3level()),   # call 2: flat category list
    ])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"product_id": PRODUCT_ID},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["data"]) == 3
    assert body["data"][-1]["is_current"] is True
    assert body["meta"]["resolved_via"] == "product_id"
    assert body["meta"]["product_id"] == PRODUCT_ID


@pytest.mark.asyncio
async def test_categories_b2b_unavailable_returns_502(client):
    """B2B network error → 502 UPSTREAM_UNAVAILABLE."""
    mock = _SequenceMockClient([ConnectError("Connection refused")])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get("/api/v1/catalog/categories/tree")

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_breadcrumbs_unknown_product_returns_404(client):
    """Breadcrumbs with product_id → B2B returns 404 for product → 404 NOT_FOUND."""
    mock = _SequenceMockClient([_FakeResp(status_code=404)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"product_id": PRODUCT_ID},
            )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_single_root_category_breadcrumb(client):
    """Requesting breadcrumbs for a root category → chain has exactly 1 item."""
    flat = [
        _b2b_category(cat_id=CAT_ROOT, name="Электроника", parent_id=None, level=0, path="electronics"),
    ]
    mock = _SequenceMockClient([_FakeResp(flat)])

    with patch("backend.modules.catalog.service.httpx.AsyncClient", side_effect=mock):
        async with client as ac:
            resp = await ac.get(
                "/api/v1/catalog/breadcrumbs",
                params={"category_id": CAT_ROOT},
            )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["is_current"] is True
    assert data[0]["level"] == 0
