"""
Pydantic schemas for orders (US-B2C-09 / US-B2C-10 / US-B2C-11 / US-B2C-13).

Spec: b2c/openapi.yaml
  POST /api/v1/orders — OrderCreateRequest → 201 OrderResponse
  GET  /api/v1/orders — → 200 PaginatedOrdersResponse
  GET  /api/v1/orders/{id} — → 200 OrderResponse | 404

OrderCreateRequest (spec b2c/openapi.yaml#OrderCreateRequest):
  required: [address_id, payment_method_id]
  Items come from the buyer's cart — NOT from the request body.
  items_snapshot (optional) for idempotency race-condition protection (skipped in MVP).

OrderResponse.address = AddressResponse object (spec b2c/openapi.yaml#AddressResponse).
  Address data is snapshotted at checkout time from the buyer's address book.

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders
  - idempotency via Idempotency-Key header
  - status immediately PAID (mock payment)
  - IDOR: wrong-user order → 404 (not 403)
  - Cart cleared after successful reserve
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from backend.modules.addresses.schemas import AddressResponse


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


class OrderCreateRequest(BaseModel):
    """
    spec b2c/openapi.yaml#OrderCreateRequest
    required: [address_id, payment_method_id]
    Items are taken from the buyer's active cart — not from this request.
    """
    address_id: UUID
    payment_method_id: UUID
    comment: Optional[str] = Field(None, max_length=1000)


# ──────────────────────────────────────────────────────────────────────────────
# Response
# ──────────────────────────────────────────────────────────────────────────────


class PaymentMethodResponse(BaseModel):
    """Minimal PaymentMethodResponse (mock payment — no real gateway)."""
    id: UUID
    type: str = "CARD"


class OrderItemSchema(BaseModel):
    """
    OrderItem as returned in OrderResponse.
    All price fields are a historical snapshot taken at checkout time.
    """
    sku_id: UUID
    product_id: UUID
    name: str
    quantity: int
    unit_price: int   # effective price per unit at checkout (price - discount), kopecks
    line_total: int   # unit_price * quantity

    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    """
    spec b2c/openapi.yaml#OrderResponse
    required: [id, buyer_id, status, items, subtotal, total, address, created_at]
    """
    id: UUID
    buyer_id: UUID
    status: OrderStatus
    items: List[OrderItemSchema]
    subtotal: int
    total: int
    address: AddressResponse       # snapshot of buyer's address at checkout time
    payment_method: PaymentMethodResponse
    comment: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PaginatedOrdersResponse(BaseModel):
    """
    spec b2c/openapi.yaml#PaginatedOrders
    required: [items, total_count, limit, offset]
    """
    items: List[OrderResponse]
    total_count: int
    limit: int
    offset: int
