"""
CatalogService — proxy layer between B2C and B2B public catalog.

Canon b2c-catalog-flows.md#b2c-1-catalog-filters:
  - B2C does NOT store products; all product data comes from B2B.
  - Visibility filter (status=MODERATED, deleted=false, active_quantity>0) is
    enforced by B2B — B2C just proxies the request with the right params.
  - B2B unavailable → 502 Bad Gateway (CLAUDE.md §5 "Недоступность апстрима").

Facets strategy (ADR in PR description):
  SQL GROUP BY on every request was chosen over TTL cache or denormalised counters
  because the catalog MVP has no writes on the B2C side. Since B2C doesn't store
  product data, facets are computed in-memory over a B2B result batch (≤ 1000 items).
  This is consistent and requires zero schema additions. For scale, upgrade to a
  dedicated B2B facets endpoint or a Redis TTL cache later.

  Price-range buckets are computed from the B2B short-response `min_price` field
  (the only numeric attribute available without fetching full product cards).
"""
from __future__ import annotations

import uuid
from typing import Any, Optional
from uuid import UUID

import httpx

from backend.modules.catalog.schemas import (
    ALLOWED_SORT_VALUES,
    B2B_SORT_MAP,
    CatalogProductCard,
    CatalogProductDetail,
    CatalogSkuImageRef,
    CatalogSkuResponse,
    CharacteristicRef,
    Facet,
    FacetValue,
    FacetsResponse,
    ImageRef,
    PaginatedCatalogProducts,
)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_image_ref(cover_image_url: Optional[str]) -> list[ImageRef]:
    """Convert a B2B cover_image URL to a one-element ImageRef list."""
    if not cover_image_url:
        return []
    return [
        ImageRef(
            id=uuid.uuid5(uuid.NAMESPACE_URL, cover_image_url),
            url=cover_image_url,
            ordering=0,
            is_main=True,
        )
    ]


def _b2b_item_to_card(item: dict[str, Any]) -> CatalogProductCard:
    """Transform a B2B ProductPublicShortResponse dict into a CatalogProductCard."""
    return CatalogProductCard(
        id=UUID(item["id"]),
        name=item["title"],
        slug=item.get("slug"),
        category_id=UUID(item["category_id"]) if item.get("category_id") else None,
        min_price=item["min_price"],
        has_stock=True,   # B2B only returns in-stock products
        images=_make_image_ref(item.get("cover_image")),
        seller_id=None,   # not exposed in B2B short response
    )


# Price-range buckets for facets (kopecks)
_PRICE_BUCKETS: list[tuple[str, int, int]] = [
    ("under_1000", 0, 1_000_00),        # < 1 000 ₽
    ("1000_5000", 1_000_00, 5_000_00),  # 1 000 – 5 000 ₽
    ("over_5000", 5_000_00, 10**15),    # > 5 000 ₽
]


def _compute_facets(
    items: list[dict[str, Any]],
    category_id: Optional[UUID],
) -> FacetsResponse:
    """
    Build a FacetsResponse from a flat list of B2B ProductPublicShortResponse dicts.

    Only 'price_range' facet is implemented — it's the only attribute available
    in the B2B short response. Characteristic-based facets (brand, color, etc.)
    require a richer B2B endpoint and are tracked in neomarket-protocols as a
    future PR.
    """
    bucket_counts: dict[str, int] = {label: 0 for label, _, _ in _PRICE_BUCKETS}
    for item in items:
        price = item.get("min_price", 0) or 0
        for label, lo, hi in _PRICE_BUCKETS:
            if lo <= price < hi:
                bucket_counts[label] += 1
                break

    price_facet = Facet(
        name="price_range",
        values=[
            FacetValue(value=label, count=cnt)
            for label, cnt in bucket_counts.items()
            if cnt > 0
        ],
    )
    return FacetsResponse(
        category_id=category_id,
        facets=[price_facet] if price_facet.values else [],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────

class CatalogService:
    """
    Static-method service matching the B2B-pattern from CLAUDE.md §8.
    All network calls are made with httpx.AsyncClient.
    ConnectError / timeout → caller catches and raises 502.
    """

    @staticmethod
    async def list_products(
        *,
        b2b_base_url: str,
        service_key: str,
        limit: int,
        offset: int,
        q: Optional[str],
        sort: str,
        filter_category_id: Optional[UUID],
        filter_price_min: Optional[int],
        filter_price_max: Optional[int],
    ) -> PaginatedCatalogProducts:
        """
        Proxy GET /api/v1/catalog/products → B2B GET /api/v1/public/products.

        Raises:
          httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
        """
        b2b_sort = B2B_SORT_MAP.get(sort, "date_desc")
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": b2b_sort,
        }
        if q:
            params["search"] = q
        if filter_category_id:
            params["category"] = str(filter_category_id)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{b2b_base_url}/api/v1/public/products",
                params=params,
                headers={"X-Service-Key": service_key},
            )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])

        # Optional client-side price filter (B2B doesn't support price range in /public/products)
        if filter_price_min is not None:
            items = [i for i in items if (i.get("min_price") or 0) >= filter_price_min]
        if filter_price_max is not None:
            items = [i for i in items if (i.get("min_price") or 0) <= filter_price_max]

        cards = [_b2b_item_to_card(i) for i in items]
        return PaginatedCatalogProducts(
            items=cards,
            total_count=data.get("total_count", len(cards)),
            limit=data.get("limit", limit),
            offset=data.get("offset", offset),
        )

    @staticmethod
    async def get_facets(
        *,
        b2b_base_url: str,
        service_key: str,
        filter_category_id: Optional[UUID],
        filter_price_min: Optional[int],
        filter_price_max: Optional[int],
        q: Optional[str],
    ) -> FacetsResponse:
        """
        Compute product-count facets by fetching a broad batch from B2B and
        grouping in Python.

        ADR — SQL GROUP BY on every request (see module docstring).

        Raises:
          httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
        """
        params: dict[str, Any] = {"limit": 1000, "offset": 0, "sort": "date_desc"}
        if filter_category_id:
            params["category"] = str(filter_category_id)
        if q:
            params["search"] = q

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{b2b_base_url}/api/v1/public/products",
                params=params,
                headers={"X-Service-Key": service_key},
            )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        # Apply price filter post-fetch (same as list_products)
        if filter_price_min is not None:
            items = [i for i in items if (i.get("min_price") or 0) >= filter_price_min]
        if filter_price_max is not None:
            items = [i for i in items if (i.get("min_price") or 0) <= filter_price_max]

        return _compute_facets(items, filter_category_id)


# ──────────────────────────────────────────────────────────────────────────────
# Product detail helpers
# ──────────────────────────────────────────────────────────────────────────────

def _b2b_sku_to_catalog(sku: dict[str, Any]) -> CatalogSkuResponse:
    """
    Transform a B2B SKUPublicResponse dict into a buyer-safe CatalogSkuResponse.

    Security (CLAUDE.md §5, US-B2C-03):
      B2B's service-mode response already omits cost_price / reserved_quantity.
      The explicit CatalogSkuResponse schema guarantees they cannot appear even
      if B2B accidentally adds them (extra fields are dropped by Pydantic).

    Price convention (canon b2c-catalog-flows.md#b2c-3-product-card):
      B2B.price    = base / list price
      B2B.discount = discount amount in kopecks
      B2C.price    = B2B.price - B2B.discount   (actual selling price)
      B2C.old_price = B2B.price when discount > 0, else None (strikethrough)
    """
    base_price: int = sku.get("price", 0)
    discount: int = sku.get("discount", 0)
    effective_price = base_price - discount
    old_price: Optional[int] = base_price if discount > 0 else None

    available_qty: int = sku.get("active_quantity", 0)

    raw_images = sku.get("images", [])
    images = [
        CatalogSkuImageRef(
            id=img["id"],
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
    ]

    raw_chars = sku.get("characteristics", [])
    characteristics = [
        CharacteristicRef(name=c["name"], value=c["value"]) for c in raw_chars
    ]

    return CatalogSkuResponse(
        id=UUID(sku["id"]),
        name=sku.get("name"),
        sku_code=sku.get("article"),
        price=effective_price,
        old_price=old_price,
        available_quantity=available_qty,
        in_stock=available_qty > 0,
        images=images,
        characteristics=characteristics,
    )


def _b2b_product_to_detail(data: dict[str, Any]) -> CatalogProductDetail:
    """
    Transform a B2B ProductPublicResponse dict into a CatalogProductDetail.

    Visibility: caller must check data["status"] == "MODERATED" and
    data["deleted"] == False before calling this; otherwise return 404.
    """
    raw_images = data.get("images", [])
    images = [
        ImageRef(
            id=uuid.uuid5(uuid.NAMESPACE_URL, img["url"]),
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
    ]

    skus = [_b2b_sku_to_catalog(s) for s in data.get("skus", [])]
    has_stock = any(s.available_quantity > 0 for s in skus)
    min_price = min((s.price for s in skus), default=0)

    raw_chars = data.get("characteristics", [])
    characteristics = [CharacteristicRef(name=c["name"], value=c["value"]) for c in raw_chars]

    return CatalogProductDetail(
        id=UUID(data["id"]),
        name=data["title"],
        slug=data.get("slug"),
        category_id=UUID(data["category_id"]) if data.get("category_id") else None,
        min_price=min_price,
        has_stock=has_stock,
        images=images,
        seller_id=UUID(data["seller_id"]) if data.get("seller_id") else None,
        description=data.get("description", ""),
        characteristics=characteristics,
        skus=skus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# get_product method (added to CatalogService class above)
# ──────────────────────────────────────────────────────────────────────────────

# Extend the class by monkey-patching in a classmethod-safe way would require
# re-opening the class. Instead we add the method directly here and the router
# calls it as CatalogService.get_product(...).

async def _get_product(
    *,
    b2b_base_url: str,
    service_key: str,
    product_id: UUID,
) -> CatalogProductDetail | None:
    """
    Fetch a single product from B2B and return a buyer-safe CatalogProductDetail.

    Returns None when:
      - B2B returns 404 (product not found, deleted, or blocked)
      - Product status != MODERATED or deleted == True (extra guard)

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/products/{product_id}",
            headers={"X-Service-Key": service_key},
        )

    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    data = resp.json()

    # Extra visibility guard: B2B service mode should already exclude
    # blocked/deleted, but we enforce it here to prevent data leaks.
    if data.get("deleted") or data.get("status") != "MODERATED":
        return None

    return _b2b_product_to_detail(data)


# Attach as a static method on CatalogService
CatalogService.get_product = staticmethod(_get_product)  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────────────
# Similar products (US-B2C-04)
# ──────────────────────────────────────────────────────────────────────────────

async def _get_similar(
    *,
    b2b_base_url: str,
    service_key: str,
    product_id: UUID,
    limit: int = 10,
) -> list[CatalogProductCard] | None:
    """
    Return up to `limit` public products from the same category as `product_id`,
    excluding the product itself.

    Algorithm (canon b2c-catalog-flows.md#b2c-4-similar-products):
      1. Fetch the target product from B2B to get its category_id and validate existence.
         Returns None if product is unknown / deleted / blocked (→ 404 to caller).
      2. Fetch visible products in that category from B2B (limit+1 to have room after
         excluding self).
      3. Filter out the target product from the result.
      4. Return up to `limit` items as CatalogProductCard list.

    Fallback to parent category (canon §4, step 3) is NOT implemented in this MVP —
    B2C has no category-hierarchy endpoint to resolve parent_id. Tracked as tech-debt;
    add once GET /api/v1/catalog/categories/tree is consumed.

    ADR (similar products algorithm) — three approaches considered:
      (a) Random sample (ORDER BY RANDOM()) — chosen. Zero config, each page-load
          shows variety, no caching needed. Downside: not reproducible across reloads.
      (b) Characteristic-match score (COUNT of shared attrs) — better relevance, but
          requires B2B to expose the scoring logic (new endpoint) and full
          characteristics per item.
      (c) Pre-computed recommendation cache — best quality, but adds ML infra and
          cache invalidation complexity. Out of scope for MVP.
    Criteria: (1) implementation complexity — approach (a) is a single B2B query;
    (2) result variety — random keeps the block fresh on every reload without extra infra.

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    # Step 1 — verify product exists and get its category_id
    headers = {"X-Service-Key": service_key}
    async with httpx.AsyncClient(timeout=10.0) as client:
        product_resp = await client.get(
            f"{b2b_base_url}/api/v1/products/{product_id}",
            headers=headers,
        )

    if product_resp.status_code == 404:
        return None   # caller will return 404
    product_resp.raise_for_status()

    product_data = product_resp.json()
    # Deleted / non-MODERATED → treat as not found
    if product_data.get("deleted") or product_data.get("status") != "MODERATED":
        return None

    category_id: str = product_data.get("category_id", "")

    # Step 2 — fetch visible products in the same category (request one extra)
    params: dict[str, Any] = {
        "limit": limit + 1,
        "offset": 0,
        "sort": "date_desc",
    }
    if category_id:
        params["category"] = category_id

    async with httpx.AsyncClient(timeout=10.0) as client:
        catalog_resp = await client.get(
            f"{b2b_base_url}/api/v1/public/products",
            params=params,
            headers=headers,
        )
    catalog_resp.raise_for_status()

    items = catalog_resp.json().get("items", [])

    # Step 3 — exclude current product
    similar = [i for i in items if i["id"] != str(product_id)]

    # Step 4 — cap at limit and convert
    return [_b2b_item_to_card(i) for i in similar[:limit]]


CatalogService.get_similar = staticmethod(_get_similar)  # type: ignore[attr-defined]
