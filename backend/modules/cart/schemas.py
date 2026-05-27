"""
Pydantic schemas for cart endpoints (US-B2C-08).

Spec: b2c/openapi.yaml (neomarket-protocols)
  CartItemAddRequest  — POST /api/v1/cart/items request body
  CartItem            — enriched item in GET /api/v1/cart response
  CartResponse        — full cart response (items, summary, is_valid)

Notes:
  - unit_price / line_total / available_quantity are computed from B2B at read time.
  - unavailable_reason is an extension beyond spec minimum (spec only has is_available bool).
    Included per DoD "unavailable_sku_shown_with_reason" and canon flow §unavailable-reasons.
  - Prices are in kopecks (integer).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# Request schemas
# ──────────────────────────────────────────────────────────────────────────────


class CartItemAddRequest(BaseModel):
    """POST /api/v1/cart/items request body."""
    sku_id: UUID
    quantity: int = Field(..., ge=1)


class CartItemUpdateRequest(BaseModel):
    """PATCH /api/v1/cart/items/{sku_id} request body."""
    quantity: int = Field(..., ge=1)


# ──────────────────────────────────────────────────────────────────────────────
# Response schemas
# ──────────────────────────────────────────────────────────────────────────────


class ImageRef(BaseModel):
    url: str


class CartItemSchema(BaseModel):
    """
    Single cart line item — enriched from B2B at read time.

    Per spec b2c/openapi.yaml CartItem.
    Added unavailable_reason (optional) per DoD and canon.
    """
    sku_id: UUID
    product_id: UUID
    name: str
    sku_code: Optional[str] = None
    quantity: int
    unit_price: int
    unit_price_at_add: Optional[int] = None
    line_total: int  # 0 for unavailable items
    available_quantity: int
    is_available: bool
    unavailable_reason: Optional[str] = None  # extension: OUT_OF_STOCK, PRODUCT_DELETED, etc.
    image: Optional[ImageRef] = None


class CartSummary(BaseModel):
    """Aggregated totals for the cart."""
    total_amount: int      # sum of line_total for available items
    total_items: int       # count of distinct SKUs
    unavailable_count: int
    checkout_ready: bool   # True if all items are available


class CartResponseSchema(BaseModel):
    """
    Full cart response per spec b2c/openapi.yaml CartResponse.

    Required fields: items, items_count, subtotal, is_valid.
    """
    items: List[CartItemSchema] = Field(default_factory=list)
    items_count: int        # sum of quantity across all rows
    subtotal: int           # sum of line_total (available items only), kopecks
    is_valid: bool          # True if all items are available
    updated_at: Optional[datetime] = None
