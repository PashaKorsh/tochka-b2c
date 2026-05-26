"""
US-B2C-03: Product card via GET /api/v1/catalog/products/{product_id}.

Canon flow: b2c-catalog-flows.md#b2c-3-product-card
Spec:       b2c/openapi.yaml#CatalogProductDetail / #CatalogSku

Covered DoD scenarios:
  ✓ product_card_returns_full_data_with_skus
  ✓ cost_price_absent_in_response
  ✓ blocked_product_returns_404
  ✓ sku_without_stock_is_shown_as_unavailable

Extra:
  ✓ reserved_quantity_absent_in_response
  ✓ discount_sets_old_price
  ✓ b2b_unavailable_returns_502
  ✓ deleted_product_returns_404
  ✓ non_moderated_product_returns_404

ADR — serialiser approach (for PR description):
  Three ways to separate B2C/B2B representations were considered:
    1. Runtime field exclusion (dict pop / response_model_exclude) — fields
       could re-appear if someone adds them to the shared model and forgets
       to update the exclusion list. Easy to miss in code review.
    2. View-level filtering in the router (del resp["cost_price"]) — same
       fragility; no type-safety.
    3. Separate Pydantic schema per representation (chosen) — CatalogSkuResponse
       declares only buyer-visible fields. A new field added to B2B's
       SKUPublicResponse cannot leak because Pydantic ignores extra keys on
       parse and only serialises declared fields. Zero ongoing maintenance cost.
  Criteria: (a) zero risk of accidental leak on model evolution; (b) clear
  self-documentation — the schema is the access-control list.
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

def _b2b_sku(
    *,
    sku_id: str | None = None,
    name: str = "Вариант A",
    price: int = 10_000_00,
    discount: int = 0,
    stock_quantity: int = 10,
    reserved_quantity: int = 2,
    cost_price: int = 5_000_00,
) -> dict[str, Any]:
    """Build a B2B SKUPublicResponse dict (as returned by X-Service-Key mode)."""
    # Note: B2B service mode doesn't include cost_price/reserved_quantity,
    # but we simulate a buggy B2B that does — B2C must strip them.
    return {
        "id": sku_id or str(uuid4()),
        "product_id": str(uuid4()),
        "name": name,
        "price": price,
        "discount": discount,
        "stock_quantity": stock_quantity,
        "reserved_quantity": reserved_quantity,   # <-- B2C must NOT expose this
        "cost_price": cost_price,                  # <-- B2C must NOT expose this
        "active_quantity": stock_quantity - reserved_quantity,
        "article": "ART-001",
        "images": [{"id": str(uuid4()), "url": "https://cdn.example.com/img.jpg", "ordering": 0}],
        "characteristics": [{"name": "Цвет", "value": "Чёрный"}],
    }


def _b2b_product(
    *,
    product_id: str | None = None,
    status: str = "MODERATED",
    deleted: bool = False,
    skus: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a B2B ProductPublicResponse dict."""
    pid = product_id or str(uuid4())
    return {
        "id": pid,
        "seller_id": str(uuid4()),
        "category_id": str(uuid4()),
        "title": "Тестовый товар",
        "slug": "test-product",
        "description": "Подробное описание товара для покупателя.",
        "status": status,
        "deleted": deleted,
        "images": [{"url": "https://cdn.example.com/main.jpg", "ordering": 0}],
        "characteristics": [{"name": "Бренд", "value": "Acme"}],
        "skus": skus if skus is not None else [_b2b_sku()],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-05-01T00:00:00Z",
    }


class _FakeResp:
    def __init__(self, data: dict | None = None, status_code: int = 200):
        self._data = data or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            # Raise an HTTPStatusError the way httpx does it
            req = HttpxRequest("GET", "http://b2b/")
            raw = HttpxResponse(self.status_code, request=req)
            raise HTTPStatusError(
                f"HTTP {self.status_code}",
                request=req,
                response=raw,
            )


class _MockClient:
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


PRODUCT_ID = str(uuid4())


# ──────────────────────────────────────────────────────────────────────────────
# test_product_card_returns_full_data_with_skus
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_product_card_returns_full_data_with_skus(client):
    """
    Happy path: B2B returns a MODERATED product with SKUs.
    B2C wraps it in CatalogProductDetail and returns 200.

    Verifies:
    - 200 status
    - Required fields: id, name, description, skus, images, has_stock, min_price
    - Each SKU has: id, name, price, available_quantity, in_stock, images, characteristics
    """
    sku1 = _b2b_sku(name="Вариант S", price=5_000_00, discount=0, stock_quantity=5, reserved_quantity=1)
    sku2 = _b2b_sku(name="Вариант L", price=6_000_00, discount=500_00, stock_quantity=3, reserved_quantity=0)
    product = _b2b_product(product_id=PRODUCT_ID, skus=[sku1, sku2])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Top-level fields
    assert data["id"] == PRODUCT_ID
    assert data["name"] == "Тестовый товар"
    assert data["description"] == "Подробное описание товара для покупателя."
    assert data["has_stock"] is True
    assert isinstance(data["images"], list)
    assert len(data["images"]) >= 1

    # SKUs
    skus = data["skus"]
    assert len(skus) == 2

    # SKU shape
    sku = skus[0]
    assert "id" in sku
    assert "name" in sku
    assert "price" in sku
    assert "available_quantity" in sku
    assert "in_stock" in sku
    assert isinstance(sku["images"], list)
    assert isinstance(sku["characteristics"], list)


# ──────────────────────────────────────────────────────────────────────────────
# test_cost_price_absent_in_response
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cost_price_absent_in_response(client):
    """
    Security: cost_price MUST NOT appear in the buyer-facing response.
    Even if B2B accidentally includes it (buggy response), B2C schema strips it.

    assert 'cost_price' not in response.json()['skus'][0]
    """
    # The mock B2B response intentionally includes cost_price (simulating a bug)
    sku = _b2b_sku(cost_price=7_000_00)
    product = _b2b_product(skus=[sku])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["skus"]) >= 1
    sku_resp = body["skus"][0]

    assert "cost_price" not in sku_resp, "cost_price must never appear in buyer response"
    assert "reserved_quantity" not in sku_resp, "reserved_quantity must never appear in buyer response"


# ──────────────────────────────────────────────────────────────────────────────
# test_blocked_product_returns_404
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocked_product_returns_404(client):
    """
    Canon b2c-catalog-flows.md#b2c-3-product-card edge case:
    Blocked product → 404 (B2B returns 404 for blocked items in service mode).
    """
    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(status_code=404)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 404, resp.text
    data = resp.json()
    assert data["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_deleted_product_returns_404(client):
    """
    Extra guard: if B2B somehow returns a deleted product, B2C enforces 404.
    Canon: deleted=True → not visible to buyer.
    """
    product = _b2b_product(deleted=True, status="MODERATED")

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_non_moderated_product_returns_404(client):
    """
    Extra guard: status != MODERATED (e.g. BLOCKED, HARD_BLOCKED, ON_MODERATION) → 404.
    B2C must not expose non-public products even via direct link.
    """
    for bad_status in ("BLOCKED", "HARD_BLOCKED", "ON_MODERATION", "CREATED"):
        product = _b2b_product(status=bad_status, deleted=False)

        with patch(
            "backend.modules.catalog.service.httpx.AsyncClient",
            return_value=_MockClient(_FakeResp(product)),
        ):
            ac = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            async with ac:
                resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

        assert resp.status_code == 404, f"Expected 404 for status={bad_status}, got {resp.status_code}"


# ──────────────────────────────────────────────────────────────────────────────
# test_sku_without_stock_is_shown_as_unavailable
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sku_without_stock_is_shown_as_unavailable(client):
    """
    Canon b2c-catalog-flows.md#b2c-3-product-card edge case:
    SKU with zero active_quantity is included in the SKU list but marked unavailable.
    Buyer sees "Нет в наличии", "В корзину" button is disabled.
    """
    sku_in_stock = _b2b_sku(name="In Stock", stock_quantity=5, reserved_quantity=0)
    sku_out = _b2b_sku(name="Out of Stock", stock_quantity=3, reserved_quantity=3)  # active=0
    product = _b2b_product(skus=[sku_in_stock, sku_out])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200, resp.text
    skus = resp.json()["skus"]
    assert len(skus) == 2

    in_stock_sku = next(s for s in skus if s["name"] == "In Stock")
    out_sku = next(s for s in skus if s["name"] == "Out of Stock")

    assert in_stock_sku["in_stock"] is True
    assert in_stock_sku["available_quantity"] > 0

    assert out_sku["in_stock"] is False
    assert out_sku["available_quantity"] == 0

    # Product-level has_stock is True because at least one SKU has stock
    assert resp.json()["has_stock"] is True


# ──────────────────────────────────────────────────────────────────────────────
# Extra tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reserved_quantity_absent_in_response(client):
    """reserved_quantity must never appear in any SKU in the buyer response."""
    product = _b2b_product(skus=[_b2b_sku(reserved_quantity=5)])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200
    for sku in resp.json()["skus"]:
        assert "reserved_quantity" not in sku


@pytest.mark.asyncio
async def test_discount_sets_old_price(client):
    """
    Canon b2c-catalog-flows.md#b2c-3-product-card pricing:
    When B2B discount > 0:
      B2C.price     = B2B.price - B2B.discount   (effective selling price)
      B2C.old_price = B2B.price                  (strikethrough price for frontend)
    """
    base_price = 13_000_00   # 13 000 ₽
    discount   =    500_00   #    500 ₽
    sku = _b2b_sku(price=base_price, discount=discount)
    product = _b2b_product(skus=[sku])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200
    sku_resp = resp.json()["skus"][0]
    assert sku_resp["price"] == base_price - discount       # 12 500 ₽
    assert sku_resp["old_price"] == base_price              # 13 000 ₽ (strikethrough)


@pytest.mark.asyncio
async def test_no_discount_old_price_is_null(client):
    """When discount == 0, old_price must be null (no strikethrough on frontend)."""
    sku = _b2b_sku(price=5_000_00, discount=0)
    product = _b2b_product(skus=[sku])

    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(_FakeResp(product)),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 200
    sku_resp = resp.json()["skus"][0]
    assert sku_resp["price"] == 5_000_00
    assert sku_resp["old_price"] is None


@pytest.mark.asyncio
async def test_product_card_b2b_unavailable_returns_502(client):
    """B2B network error → 502 UPSTREAM_UNAVAILABLE (CLAUDE.md §5)."""
    with patch(
        "backend.modules.catalog.service.httpx.AsyncClient",
        return_value=_MockClient(ConnectError("Connection refused")),
    ):
        async with client as ac:
            resp = await ac.get(f"/api/v1/catalog/products/{PRODUCT_ID}")

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "UPSTREAM_UNAVAILABLE"
