"""
Pydantic schemas for orders (US-B2C-09 / US-B2C-10).

Spec: b2c/openapi.yaml
  POST /api/v1/orders — OrderCreateRequest → 201 OrderResponse
  GET  /api/v1/orders — → 200 PaginatedOrdersResponse
  GET  /api/v1/orders/{id} — → 200 OrderResponse | 404
  OrderItem required: [sku_id, product_id, name, quantity, unit_price, line_total]
  OrderResponse required: [id, buyer_id, status, items, subtotal, total, address, created_at]
  PaginatedOrders required: [items, total_count, limit, offset]
  status enum: CREATED | PAID | ASSEMBLING | DELIVERING | DELIVERED | CANCELLED | CANCEL_PENDING

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders
  - idempotency via Idempotency-Key header (spec) not body field (canon)
  - status immediately PAID (mock payment)
  - IDOR: wrong-user order → 404 (not 403) — must not reveal existence

Spec deviation note:
  OrderResponse.address is spec'd as AddressResponse (structured object with country/city/…).
  Current implementation stores delivery_address as a plain string because the B2C address
  registry (POST /api/v1/addresses) is not yet implemented. The field is returned as a string
  for backward compat; migrate to AddressResponse once the address service is built.
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
    Response for POST /api/v1/orders (201) and GET /api/v1/orders/{id} (200).

    Spec: b2c/openapi.yaml#OrderResponse
      required: [id, buyer_id, status, items, subtotal, total, address, created_at]

    Note: `address` is a string (see module docstring for spec-deviation rationale).
    """
    id: UUID
    buyer_id: UUID
    status: OrderStatus
    items: List[OrderItemSchema]
    subtotal: int
    total: int
    address: str           # delivery_address snapshot (see spec deviation note above)
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PaginatedOrdersResponse(BaseModel):
    """
    GET /api/v1/orders response.

    Spec: b2c/openapi.yaml#PaginatedOrders
      required: [items, total_count, limit, offset]

    Spec note: items is an array of full OrderResponse objects (not summaries).
    Canon note: canon lists only items_count — spec wins for contract shape.
    """
    items: List[OrderResponse]
    total_count: int
    limit: int
    offset: int
