"""
Pydantic schemas for incoming B2B events (US-B2C-12).

Spec: b2c/openapi.yaml — POST /api/v1/b2b/events
  B2BEvent:
    required: [event_type, idempotency_key, occurred_at, payload]
    event_type enum:
      PRODUCT_BLOCKED | PRODUCT_HARD_BLOCKED | PRODUCT_DELETED
      SKU_OUT_OF_STOCK | SKU_BACK_IN_STOCK | PRICE_CHANGED
  payload (oneOf):
    EventProductRef  { product_id, reason? }       — for product-level events
    EventSkuStock    { sku_id, product_id, available_quantity }
    EventPriceChanged { sku_id, product_id, old_price, new_price }

Canon: b2c-orders-flows.md#b2c-12-handle-events
  Flat format with sku_ids[] array vs spec's oneOf payload.
  Resolution: spec format used (spec > canon for contract shape).

B2C processes:
  PRODUCT_BLOCKED / PRODUCT_HARD_BLOCKED / PRODUCT_DELETED
    → cart_items WHERE product_id = payload.product_id
      SET unavailable_reason = event_type
  SKU_OUT_OF_STOCK
    → cart_items WHERE sku_id = payload.sku_id
      SET unavailable_reason = "OUT_OF_STOCK"
  SKU_BACK_IN_STOCK / PRICE_CHANGED
    → logged only (no cart mutation in this iteration)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class B2BEventType(str, Enum):
    PRODUCT_BLOCKED = "PRODUCT_BLOCKED"
    PRODUCT_HARD_BLOCKED = "PRODUCT_HARD_BLOCKED"
    PRODUCT_DELETED = "PRODUCT_DELETED"
    SKU_OUT_OF_STOCK = "SKU_OUT_OF_STOCK"
    SKU_BACK_IN_STOCK = "SKU_BACK_IN_STOCK"
    PRICE_CHANGED = "PRICE_CHANGED"


class B2BEventRequest(BaseModel):
    """
    Incoming B2B event. Spec: b2c/openapi.yaml#B2BEvent.

    payload is typed as Dict[str, Any] to accommodate oneOf without discriminated
    union complexity. The service parses product_id / sku_id from payload based
    on event_type.
    """
    event_type: B2BEventType
    idempotency_key: UUID
    occurred_at: datetime
    payload: Dict[str, Any] = Field(default_factory=dict)


class B2BEventResponse(BaseModel):
    """202 response body."""
    accepted: bool = True
