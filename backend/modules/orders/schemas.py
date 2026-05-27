"""
Pydantic schemas for orders (US-B2C-09).

Spec: b2c/openapi.yaml
  POST /api/v1/orders — OrderCreateRequest → 201 OrderResponse
  OrderItem required: [sku_id, product_id, name, quantity, unit_price, line_total]
  OrderResponse required: [id, buyer_id, status, items, subtotal, total, address, created_at]
  status enum: CREATED | PAID | ASSEMBLING | DELIVERING | DELIVERED | CANCELLED | CANCEL_PENDING

Canon: b2c-cart-flows.md#b2c-09-checkout
  - idempotency via Idempotency-Key header (spec) not body field (canon)
  - status immediately PAID (mock payment)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    PAID = "PAID"
    ASSEMBLING = "ASSEMBLING"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    CANCEL_PENDING = "CANCEL_PENDING"


# ──────────────────────────────────────────────────────────────────────────────
# Request
# ──────────────────────────────────────────────────────────────────────────────


class OrderItemRequest(BaseModel):
    """Single line item in the checkout request."""
    sku_id: UUID
    quantity: int = Field(..., ge=1, description="Must be at least 1")


class OrderCreateRequest(BaseModel):
    """
    POST /api/v1/orders request body.

    items — at least one SKU is required.
    delivery_address — freeform delivery address string (snapshot).
    payment_method_id — UUID of the payment method (mocked, always succeeds).
    """
    items: List[OrderItemRequest] = Field(..., min_length=1)
    delivery_address: str = Field(..., min_length=1)
    payment_method_id: Optional[UUID] = None


# ──────────────────────────────────────────────────────────────────────────────
# Response
# ──────────────────────────────────────────────────────────────────────────────


class OrderItemSchema(BaseModel):
    """
    OrderItem as returned in OrderResponse.
    All price fields are a historical snapshot taken at checkout time.
    """
    sku_id: UUID
    product_id: UUID
    name: str
    quantity: int
    unit_price: int   # effective price per unit at checkout (price - discount), cents
    line_total: int   # unit_price * quantity

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    """
    201 response for POST /api/v1/orders.

    Spec: b2c/openapi.yaml#OrderResponse
      required: [id, buyer_id, status, items, subtotal, total, address, created_at]
    """
    id: UUID
    buyer_id: UUID
    status: OrderStatus
    items: List[OrderItemSchema]
    subtotal: int
    total: int
    address: str          # spec field name (maps from delivery_address in DB)
    created_at: datetime

    model_config = {"from_attributes": True}
