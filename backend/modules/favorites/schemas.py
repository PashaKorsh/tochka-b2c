"""
Favorites Pydantic schemas (US-B2C-06).

Sources:
  canon: b2c-cart-flows.md#b2c-6-favorites
  canon openapi: neomarket-canon/apis/b2c/cart/openapi.yaml
  protocols: neomarket-protocols/b2c/openapi.yaml (PaginatedCatalogProducts)

Response shape reconciliation:
  - canon openapi uses FavoritesResponse {items: FavoriteItem[], total: int}
    where FavoriteItem = {product: Product, added_at: datetime}
  - protocols spec uses PaginatedCatalogProducts for GET /favorites
  - We use the canon shape (richer: includes added_at, full product data)
    since it matches the DoD enrichment requirement.

Security note: FavoriteMutationResponse returns user_id so the client can
confirm which account was affected. The user_id comes from JWT — never from query.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel

from backend.modules.catalog.schemas import CatalogProductCard


class FavoriteMutationResponse(BaseModel):
    """
    Response for POST /api/v1/favorites/{product_id}.
    canon b2c/cart/openapi.yaml#FavoriteMutationResponse
    """
    product_id: UUID
    user_id: UUID
    added_at: datetime
    message: str


class FavoriteItem(BaseModel):
    """
    One item in the favorites list.
    canon b2c/cart/openapi.yaml#FavoriteItem
    product is the enriched B2B card (CatalogProductCard from existing catalog schemas).
    """
    product: CatalogProductCard
    added_at: datetime


class FavoritesListResponse(BaseModel):
    """
    Response for GET /api/v1/favorites.
    Mirrors canon FavoritesResponse: items + total (not paginated offset/limit in body).
    """
    items: List[FavoriteItem]
    total: int
