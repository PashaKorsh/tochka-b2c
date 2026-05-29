"""
B2C Catalog Pydantic schemas — aligned with b2c/openapi.yaml.

Key types:
  CategoryRef              — spec §CategoryRef: {id, name, parent_id, level, path}
  SellerRef                — spec §CatalogProductCard.seller: {id, display_name}
  CatalogProductCard       — item in the product listing (required: id, name, min_price, has_stock, images).
  PaginatedCatalogProducts — paged response for GET /api/v1/catalog/products.
  CatalogSkuResponse       — SKU in the product detail (NO cost_price / reserved_quantity).
  CatalogProductDetail     — full product card for GET /api/v1/catalog/products/{id}.
  FacetValue               — single bucket in a facet (value + count).
  Facet                    — named group of facet buckets.
  FacetsResponse           — response for GET /api/v1/catalog/facets.
  ErrorResponse            — unified {code, message, details?} error body.

Sort enum (spec b2c/openapi.yaml#/paths/~1api~1v1~1catalog~1products/get):
  price_asc | price_desc | popularity | new

Security note — FORBIDDEN fields in any buyer-facing response:
  cost_price, reserved_quantity — internal seller data; must NEVER appear in B2C output.
  ADR: explicit Pydantic schema per representation (seller vs buyer) rather than runtime
  field-exclusion: a new field added to a shared model cannot accidentally leak because
  only the whitelisted schema fields are serialised.
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


# ────────────────────────── Categories ──────────────────────────

class CategoryRef(BaseModel):
    """
    spec b2c/openapi.yaml#CategoryRef — flat category representation.
    required: [id, name, level, path]
    path: array of strings from root to current (breadcrumb names or slugs).

    Note on list-endpoint population: B2B's ProductPublicShortResponse only
    provides category_id (UUID). The service builds a partial CategoryRef with
    id=category_id and name="" / level=0 / path=[] for list items; the detail
    endpoint does a separate category fetch to populate full fields.
    """
    id: UUID
    name: str
    parent_id: Optional[UUID] = None
    level: int
    path: List[str] = Field(default_factory=list, description="Names from root to current")


class CategoryTreeNode(CategoryRef):
    """
    spec b2c/openapi.yaml#CategoryTreeNode — nested category node.
    allOf: CategoryRef + {children: [CategoryTreeNode]}
    Used in GET /api/v1/catalog/categories/tree.
    """
    children: List["CategoryTreeNode"] = []

    model_config = {"from_attributes": True}


CategoryTreeNode.model_rebuild()


# ────────────────────────── Seller ──────────────────────────

class SellerRef(BaseModel):
    """
    spec b2c/openapi.yaml#CatalogProductCard.seller — inline seller object.
    {id, display_name}

    Note: B2B's ProductPublicResponse only exposes seller_id (UUID).
    display_name is populated as "" until B2B adds a public seller-profile endpoint.
    """
    id: UUID
    display_name: str = ""


# ────────────────────────── Product card ──────────────────────────

class CatalogProductCard(BaseModel):
    """
    spec b2c/openapi.yaml#CatalogProductCard
    required: [id, name, min_price, has_stock, images]
    optional: category (CategoryRef), seller ({id, display_name})
    """
    id: UUID
    name: str
    slug: Optional[str] = None
    category: Optional[CategoryRef] = None
    min_price: int = Field(..., description="Минимальная цена среди доступных SKU, копейки")
    old_price: Optional[int] = None
    has_stock: bool
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    images: List[ImageRef]
    seller: Optional[SellerRef] = None


class PaginatedCatalogProducts(BaseModel):
    """spec b2c/openapi.yaml#PaginatedCatalogProducts"""
    items: List[CatalogProductCard]
    total_count: int
    limit: int
    offset: int


# ────────────────────────── Product detail ──────────────────────────

class CharacteristicRef(BaseModel):
    """Characteristic name/value pair — same shape as B2B CharacteristicResponse."""
    name: str
    value: str


class CatalogSkuImageRef(BaseModel):
    """Image attached to a SKU."""
    id: UUID
    url: str
    ordering: int = 0

    class Config:
        from_attributes = True


class CatalogSkuResponse(BaseModel):
    """
    spec b2c/openapi.yaml#CatalogSku — buyer-safe SKU representation.
    required: [id, price, available_quantity]

    Security: MUST NOT contain cost_price or reserved_quantity.
    These fields are never populated from B2B's ProductPublicResponse / SKUPublicResponse
    (B2B already strips them in service-key mode), and are not declared here, so they
    cannot accidentally appear in the serialised JSON.

    Price convention (canon b2c-catalog-flows.md#b2c-3-product-card):
      price    = effective selling price = B2B.price - B2B.discount
      old_price = B2B.price when discount > 0, else None (strikethrough display)
    """
    id: UUID
    name: Optional[str] = None
    sku_code: Optional[str] = None   # B2B article field
    price: int                        # effective price (after discount), kopecks
    old_price: Optional[int] = None  # original price when discount > 0
    available_quantity: int           # B2B active_quantity (stock - reserved)
    in_stock: bool                    # computed: available_quantity > 0
    images: List[CatalogSkuImageRef] = []
    characteristics: List[CharacteristicRef] = []


class CatalogProductDetail(BaseModel):
    """
    spec b2c/openapi.yaml#CatalogProductDetail — full public product card.
    allOf: CatalogProductCard + {description, skus}

    Returned by GET /api/v1/catalog/products/{product_id} (US-B2C-03).
    """
    # CatalogProductCard fields
    id: UUID
    name: str
    slug: Optional[str] = None
    category: Optional[CategoryRef] = None
    min_price: int
    old_price: Optional[int] = None
    has_stock: bool
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    images: List[ImageRef]
    seller: Optional[SellerRef] = None
    # Detail-only fields
    description: str
    characteristics: List[CharacteristicRef] = []
    skus: List[CatalogSkuResponse]


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


# ────────────────────────── Breadcrumbs (US-B2C-05) ──────────────────────────

class BreadcrumbItem(BaseModel):
    """
    canon b2c-catalog-flows.md#b2c-5-category-nav / b2c/catalog/openapi.yaml#breadcrumb_item.
    required: [id, slug, name, level]
    """
    id: UUID
    slug: str
    name: str
    url: Optional[str] = None
    level: int
    is_current: bool = False


class BreadcrumbMeta(BaseModel):
    """
    canon b2c/catalog/openapi.yaml#breadcrumb_meta.
    resolved_via: "category_id" | "product_id"
    """
    resolved_via: str
    category_id: Optional[UUID] = None
    product_id: Optional[UUID] = None


class BreadcrumbResponse(BaseModel):
    """
    Response for GET /api/v1/catalog/breadcrumbs.
    canon b2c-catalog-flows.md#b2c-5-category-nav (breadcrumb response shape).
    """
    data: List[BreadcrumbItem]
    meta: BreadcrumbMeta


# ────────────────────────── Error ──────────────────────────

class ErrorResponse(BaseModel):
    """Unified error body — spec b2c/openapi.yaml#Error."""
    code: str
    message: str
    details: Optional[dict] = None
