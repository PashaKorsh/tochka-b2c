"""
Favorites Pydantic schemas (US-B2C-06).

Contract (spec b2c/openapi.yaml, source of truth per CLAUDE.md §3):
  PUT    /api/v1/favorites/{product_id} → 204 No Content (no body)
  DELETE /api/v1/favorites/{product_id} → 204 No Content
  GET    /api/v1/favorites              → FavoritesListResponse (PaginatedCatalogProducts shape)

FavoritesListResponse mirrors PaginatedCatalogProducts exactly:
  items: List[CatalogProductCard]  — flat product cards (no added_at wrapper)
  total_count: int
  limit: int
  offset: int

Note: the canon openapi (neomarket-canon/apis/b2c/cart/openapi.yaml) uses a richer
FavoriteItem{product, added_at} wrapper. The protocols spec (neomarket-protocols/b2c/openapi.yaml)
defines GET /favorites as returning PaginatedCatalogProducts. Per CLAUDE.md §3 conflict-resolution
rule, protocols wins for API contract. added_at extension should be proposed as a separate PR
to neomarket-protocols once the base contract is accepted.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel

from backend.modules.catalog.schemas import CatalogProductCard


class FavoritesListResponse(BaseModel):
    """
    spec b2c/openapi.yaml — response for GET /api/v1/favorites.
    Matches PaginatedCatalogProducts: items + total_count + limit + offset.
    """
    items: List[CatalogProductCard]
    total_count: int
    limit: int
    offset: int
