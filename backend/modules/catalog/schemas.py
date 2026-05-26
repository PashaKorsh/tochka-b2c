"""
B2C Catalog Pydantic schemas — aligned with b2c/openapi.yaml.

Key types:
  CatalogProductCard       — item in the product listing (required: id, name, min_price, has_stock, images).
  PaginatedCatalogProducts — paged response for GET /api/v1/catalog/products.
  FacetValue               — single bucket in a facet (value + count).
  Facet                    — named group of facet buckets.
  FacetsResponse           — response for GET /api/v1/catalog/facets.
  ErrorResponse            — unified {code, message, details?} error body.

Sort enum (spec b2c/openapi.yaml#/paths/~1api~1v1~1catalog~1products/get):
  price_asc | price_desc | popularity | new
"""
from typing import Optional, List
from uuid import UUID
from datetime import datetime

from pydantic import BaseModel, Field


# ────────────────────────── Sort ──────────────────────────

ALLOWED_SORT_VALUES = ["price_asc", "price_desc", "popularity", "new"]

# Map B2C sort → B2B sort param
B2B_SORT_MAP: dict[str, str] = {
    "price_asc": "price_asc",
    "price_desc": "price_desc",
    "popularity": "date_desc",   # B2B has no popularity; fall back to newest
    "new": "date_desc",
}


# ────────────────────────── Images ──────────────────────────

class ImageRef(BaseModel):
    """spec b2c/openapi.yaml#ImageRef — required: id, url, ordering."""
    id: UUID
    url: str
    alt: Optional[str] = None
    ordering: int = 0
    is_main: Optional[bool] = None


# ────────────────────────── Product card ──────────────────────────

class CatalogProductCard(BaseModel):
    """
    spec b2c/openapi.yaml#CatalogProductCard
    required: [id, name, min_price, has_stock, images]
    """
    id: UUID
    name: str
    slug: Optional[str] = None
    # category is optional in spec — we only have category_id from B2B short response
    category_id: Optional[UUID] = None
    min_price: int = Field(..., description="Минимальная цена среди доступных SKU, копейки")
    old_price: Optional[int] = None
    has_stock: bool = True
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    images: List[ImageRef]
    seller_id: Optional[UUID] = None


class PaginatedCatalogProducts(BaseModel):
    """spec b2c/openapi.yaml#PaginatedCatalogProducts"""
    items: List[CatalogProductCard]
    total_count: int
    limit: int
    offset: int


# ────────────────────────── Facets ──────────────────────────

class FacetValue(BaseModel):
    """One bucket in a named facet."""
    value: str
    count: int


class Facet(BaseModel):
    """Named group of facet values (e.g. price_range)."""
    name: str
    values: List[FacetValue]


class FacetsResponse(BaseModel):
    """
    Response for GET /api/v1/catalog/facets.
    canon b2c-catalog-flows.md#b2c-1-catalog-filters (facet response shape).
    """
    category_id: Optional[UUID] = None
    facets: List[Facet]


# ────────────────────────── Error ──────────────────────────

class ErrorResponse(BaseModel):
    """Unified error body — spec b2c/openapi.yaml#Error."""
    code: str
    message: str
    details: Optional[dict] = None
