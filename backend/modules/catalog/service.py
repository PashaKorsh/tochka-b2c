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
