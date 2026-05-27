"""
Pydantic schemas for collections (US-B2C-15).

Spec: b2c/openapi.yaml Collection schema:
  required: [id, name, products]
  properties: id, name, description, products (CatalogProductCard[])

GET /api/v1/catalog/collections → array of CollectionMeta (products: [] in list)
GET /api/v1/catalog/collections/{id}/products → CollectionProductsResponse
  (not in spec; canon b2c-cart-flows.md#b2c-15-collections)
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel

from backend.modules.catalog.schemas import CatalogProductCard


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/collections  (spec endpoint)
# ──────────────────────────────────────────────────────────────────────────────


class CollectionMeta(BaseModel):
    """
    Collection metadata per spec b2c/openapi.yaml#Collection.

    In the list endpoint products[] is always empty (metadata-only response).
    The spec includes products in the schema but the DoD requires metadata-only
    for the list. We satisfy both: the field is present but empty.
    """
    id: UUID
    name: str
    description: Optional[str] = None
    products: List[CatalogProductCard] = []  # empty in list view, populated in detail

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/collections/{id}/products  (canon endpoint, DoD required)
# ──────────────────────────────────────────────────────────────────────────────


class CollectionProductsResponse(BaseModel):
    """
    Response for GET /api/v1/catalog/collections/{id}/products.

    Canon b2c-cart-flows.md#b2c-15-collections §enrichment:
      items       — products found in B2B batch response (available)
      unavailable_ids — product_ids absent from B2B (deleted / blocked in B2B)
    """
    collection_id: UUID
    name: str
    items: List[CatalogProductCard]
    unavailable_ids: List[UUID]
    total_products: int   # total in collection (items + unavailable)
