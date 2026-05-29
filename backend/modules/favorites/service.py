"""
FavoritesService — CRUD for the favorites table (US-B2C-06).

Canon flow: b2c-cart-flows.md#b2c-6-favorites
Security: user_id ALWAYS from JWT claims, never from query/body (IDOR prevention).

Idempotency:
  add_favorite   — UNIQUE(user_id, product_id) in DB.
                   ON CONFLICT DO NOTHING; returns (row, created: bool).
  remove_favorite — DELETE WHERE user_id=X AND product_id=Y.
                    No error if row absent (idempotent).

Enrichment (GET /favorites):
  1. SELECT product_id FROM favorites WHERE user_id=X LIMIT/OFFSET.
  2. POST /api/v1/public/products/batch → B2B returns only available products.
  3. Map B2B ProductPublicResponse → CatalogProductCard.
  4. Build FavoriteItem[] preserving added_at from local DB.
  5. Products missing from B2B response (deleted/blocked) are silently excluded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.catalog.schemas import CatalogProductCard, CategoryRef, ImageRef, SellerRef
from backend.modules.catalog.service import _make_image_ref, _b2b_sku_to_catalog
from backend.modules.favorites.models import Favorite
from backend.modules.favorites.schemas import FavoritesListResponse


# ──────────────────────────────────────────────────────────────────────────────
# Internal B2B helpers
# ──────────────────────────────────────────────────────────────────────────────

def _b2b_full_to_card(data: dict[str, Any]) -> CatalogProductCard:
    """
    Convert a B2B ProductPublicResponse (full card) → CatalogProductCard.

    ProductPublicResponse has full SKUs; min_price is computed from SKUs.
    has_stock = any SKU has active_quantity > 0.
    """
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
            id=__import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, img["url"]),
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
    ]

    cat_id = data.get("category_id")
    category = CategoryRef(id=UUID(cat_id), name="", level=0, path=[]) if cat_id else None
    seller_id = data.get("seller_id")
    seller = SellerRef(id=UUID(seller_id), display_name="") if seller_id else None

    return CatalogProductCard(
        id=UUID(data["id"]),
        name=data["title"],
        slug=data.get("slug"),
        category=category,
        min_price=min_price,
        has_stock=has_stock,
        images=images,
        seller=seller,
    )


async def _batch_fetch_products(
    product_ids: list[UUID],
    b2b_base_url: str,
    service_key: str,
) -> dict[str, dict[str, Any]]:
    """
    POST /api/v1/public/products/batch → map of product_id → ProductPublicResponse.

    B2B only returns available products (MODERATED, not deleted, active_quantity > 0).
    Missing IDs are silently absent — B2C treats them as unavailable.

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 503.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/public/products/batch",
            json={"product_ids": [str(pid) for pid in product_ids]},
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()
    return {item["id"]: item for item in items}


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────

class FavoritesService:
    """
    Static-method service for favorites CRUD.
    All writes go to the local PostgreSQL (favorites table).
    Reads enrich from B2B via batch endpoint.
    """

    @staticmethod
    async def add_favorite(
        db: AsyncSession,
        *,
        user_id: UUID,
        product_id: UUID,
    ) -> None:
        """
        Idempotently add product to user's favorites.

        Spec: PUT /api/v1/favorites/{product_id} → 204 No Content (always).
        Idempotency: ON CONFLICT DO NOTHING — repeat calls are silent no-ops.
        """
        stmt = (
            pg_insert(Favorite)
            .values(
                user_id=user_id,
                product_id=product_id,
                added_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                constraint="uq_favorites_user_product"
            )
        )
        await db.execute(stmt)
        await db.commit()

    @staticmethod
    async def remove_favorite(
        db: AsyncSession,
        *,
        user_id: UUID,
        product_id: UUID,
    ) -> None:
        """
        Idempotently remove a product from user's favorites.
        No error if the row doesn't exist (canon: 204 always).
        """
        await db.execute(
            delete(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.product_id == product_id,
            )
        )
        await db.commit()

    @staticmethod
    async def list_favorites(
        db: AsyncSession,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        b2b_base_url: str,
        service_key: str,
    ) -> FavoritesListResponse:
        """
        Return paginated list of favorites enriched with B2B product data.

        Algorithm (canon b2c-cart-flows.md#b2c-6-favorites §enrichment):
          1. SELECT product_ids from favorites WHERE user_id=X (all, for total count).
          2. Apply offset/limit to get the current page of product_ids.
          3. POST /api/v1/public/products/batch → only available products.
          4. Build flat CatalogProductCard list; missing IDs silently excluded.
          5. Products absent from B2B response (deleted/blocked) → silently excluded.
          6. total = count of DB rows (regardless of B2B availability).

        Raises:
          httpx.ConnectError / httpx.TimeoutException — caller maps to 503.
        """
        # Step 1 — count + paginate in one query
        from sqlalchemy import func
        count_result = await db.execute(
            select(func.count()).select_from(Favorite).where(Favorite.user_id == user_id)
        )
        total_count: int = count_result.scalar_one()

        if total_count == 0:
            return FavoritesListResponse(items=[], total_count=0, limit=limit, offset=offset)

        page_result = await db.execute(
            select(Favorite.product_id)
            .where(Favorite.user_id == user_id)
            .order_by(Favorite.added_at.desc())
            .limit(limit)
            .offset(offset)
        )
        page_product_ids: list[UUID] = [row[0] for row in page_result.all()]

        if not page_product_ids:
            return FavoritesListResponse(items=[], total_count=total_count, limit=limit, offset=offset)

        # Step 2 — batch fetch from B2B (returns only available products)
        b2b_products = await _batch_fetch_products(
            page_product_ids, b2b_base_url, service_key
        )

        # Step 3 — build flat CatalogProductCard list; missing IDs silently excluded
        cards: list[CatalogProductCard] = []
        for pid in page_product_ids:
            data = b2b_products.get(str(pid))
            if data is None:
                continue   # deleted/blocked in B2B — exclude silently
            cards.append(_b2b_full_to_card(data))

        return FavoritesListResponse(items=cards, total_count=total_count, limit=limit, offset=offset)
