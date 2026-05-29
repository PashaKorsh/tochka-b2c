"""
CollectionsService — reads collections and enriches products from B2B (US-B2C-15).

Canon: b2c-cart-flows.md#b2c-15-collections
Spec:  b2c/openapi.yaml (neomarket-protocols)

Architecture:
  - B2C stores only collection metadata + product_id references (no product data).
  - Enrichment via POST /api/v1/public/products/batch (same as favorites/cart).
  - B2B returns only MODERATED, not-deleted products.
  - Products absent from B2B response → unavailable_ids (not an error).
  - All items unavailable → {items: [], unavailable_ids: [...]} — valid 200.

List endpoint (GET /catalog/collections):
  - Filter: is_active=True AND (start_date IS NULL OR start_date <= today())
  - Sort by priority ASC

Detail endpoint (GET /catalog/collections/{id}/products):
  - Returns CollectionProductsResponse with enriched items + unavailable_ids
  - Raises ValueError("COLLECTION_NOT_FOUND") → 404
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_BASE_URL, B2C_TO_B2B_KEY
from backend.modules.catalog.schemas import CatalogProductCard, CategoryRef, ImageRef, SellerRef
from backend.modules.collections.models import Collection, CollectionProduct
from backend.modules.collections.schemas import (
    CollectionMeta,
    CollectionProductsResponse,
)


# ──────────────────────────────────────────────────────────────────────────────
# B2B helpers (reuses the same batch endpoint used in favorites/cart)
# ──────────────────────────────────────────────────────────────────────────────


async def _batch_fetch_products(
    product_ids: list[UUID],
    b2b_base_url: str,
    service_key: str,
) -> dict[str, dict[str, Any]]:
    """
    POST /api/v1/public/products/batch → map of product_id → ProductPublicResponse.

    B2B returns only MODERATED, not-deleted products with active_quantity > 0.
    Missing IDs are absent — treated as unavailable by the caller.
    """
    if not product_ids:
        return {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/public/products/batch",
            json={"product_ids": [str(pid) for pid in product_ids]},
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()
    return {item["id"]: item for item in items}


def _b2b_to_catalog_card(data: dict[str, Any]) -> CatalogProductCard:
    """Convert B2B ProductPublicResponse dict → CatalogProductCard."""
    skus = data.get("skus", [])
    active_skus = [s for s in skus if (s.get("active_quantity") or 0) > 0]
    min_price = min(
        (s.get("price", 0) - s.get("discount", 0) for s in active_skus),
        default=0,
    )
    has_stock = len(active_skus) > 0

    raw_images = data.get("images", [])
    images = [
        ImageRef(
            id=__import__("uuid").uuid5(
                __import__("uuid").NAMESPACE_URL, img["url"]
            ),
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
        if isinstance(img, dict) and img.get("url")
    ]

    cat_id = data.get("category_id")
    category = CategoryRef(id=UUID(cat_id), name="", level=0, path=[]) if cat_id else None
    seller_id = data.get("seller_id")
    seller = SellerRef(id=UUID(seller_id), display_name="") if seller_id else None

    return CatalogProductCard(
        id=UUID(data["id"]),
        name=data.get("title", ""),
        slug=data.get("slug"),
        category=category,
        min_price=max(0, min_price),
        has_stock=has_stock,
        images=images,
        seller=seller,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────


# spec b2c/openapi.yaml#Collection requires products[] populated in list response.
# We limit to this many products per collection to keep the list endpoint fast.
_COLLECTION_PREVIEW_LIMIT = 10


class CollectionsService:
    @staticmethod
    async def list_collections(
        db: AsyncSession,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> list[CollectionMeta]:
        """
        Return active collections sorted by priority ASC, with products enriched
        from B2B (up to COLLECTION_PREVIEW_LIMIT per collection).

        Filter: is_active=True AND (start_date IS NULL OR start_date <= today())
        One B2B batch call covers all product_ids across all collections.

        Spec: GET /api/v1/catalog/collections → array of Collection.
        b2c/openapi.yaml#Collection required: [id, name, products]
        """
        today = date.today()
        result = await db.execute(
            select(Collection)
            .where(
                Collection.is_active == True,  # noqa: E712
                (Collection.start_date == None) | (Collection.start_date <= today),
            )
            .order_by(Collection.priority.asc())
        )
        rows = result.scalars().all()
        if not rows:
            return []

        # Load product_ids per collection (limited to preview count)
        col_product_ids: dict[UUID, list[UUID]] = {}
        for row in rows:
            pid_result = await db.execute(
                select(CollectionProduct.product_id)
                .where(CollectionProduct.collection_id == row.id)
                .order_by(CollectionProduct.ordering.asc())
                .limit(_COLLECTION_PREVIEW_LIMIT)
            )
            col_product_ids[row.id] = [r[0] for r in pid_result.all()]

        # One batch call for all unique product_ids across all collections
        all_pids = list(
            {pid for pids in col_product_ids.values() for pid in pids}
        )
        b2b_products = await _batch_fetch_products(all_pids, b2b_base_url, service_key)

        # Build response, preserving per-collection ordering
        output: list[CollectionMeta] = []
        for row in rows:
            pids = col_product_ids.get(row.id, [])
            products: list[CatalogProductCard] = []
            for pid in pids:
                data = b2b_products.get(str(pid))
                if data is not None:
                    products.append(_b2b_to_catalog_card(data))
            output.append(CollectionMeta(
                id=row.id,
                name=row.name,
                description=row.description,
                products=products,
            ))

        return output

    @staticmethod
    async def get_collection_products(
        db: AsyncSession,
        *,
        collection_id: UUID,
        limit: int = 20,
        offset: int = 0,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CollectionProductsResponse:
        """
        Return enriched product list for a collection.

        Algorithm (canon B2C-15 §enrichment):
          1. Find collection → 404 if not found.
          2. SELECT product_ids from collection_products ORDER BY ordering.
          3. Apply offset/limit.
          4. POST /api/v1/public/products/batch → available products.
          5. Split: found → items; missing → unavailable_ids.
          6. total_products = total count of product_ids in collection.

        Raises:
          ValueError("COLLECTION_NOT_FOUND") → 404
          httpx errors → 502
        """
        # Step 1 — find collection
        coll_result = await db.execute(
            select(Collection).where(Collection.id == collection_id)
        )
        collection = coll_result.scalar_one_or_none()
        if collection is None:
            raise ValueError("COLLECTION_NOT_FOUND")

        # Step 2 — fetch ordered product_ids
        cp_result = await db.execute(
            select(CollectionProduct.product_id)
            .where(CollectionProduct.collection_id == collection_id)
            .order_by(CollectionProduct.ordering.asc())
        )
        all_product_ids: list[UUID] = list(cp_result.scalars().all())
        total_products = len(all_product_ids)

        if total_products == 0:
            return CollectionProductsResponse(
                collection_id=collection_id,
                name=collection.name,
                items=[],
                unavailable_ids=[],
                total_products=0,
            )

        # Step 3 — paginate
        page_ids = all_product_ids[offset: offset + limit]

        # Step 4 — batch enrichment from B2B
        b2b_products = await _batch_fetch_products(
            page_ids, b2b_base_url, service_key
        )

        # Step 5 — split into available items + unavailable_ids
        items: list[CatalogProductCard] = []
        unavailable_ids: list[UUID] = []
        for pid in page_ids:
            data = b2b_products.get(str(pid))
            if data is not None:
                items.append(_b2b_to_catalog_card(data))
            else:
                unavailable_ids.append(pid)

        return CollectionProductsResponse(
            collection_id=collection_id,
            name=collection.name,
            items=items,
            unavailable_ids=unavailable_ids,
            total_products=total_products,
        )
