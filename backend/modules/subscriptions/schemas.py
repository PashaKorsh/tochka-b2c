"""
Pydantic schemas for subscriptions endpoints (US-B2C-07).

Spec: b2c/openapi.yaml (neomarket-protocols)
  notify_on enum: [BACK_IN_STOCK, PRICE_DROP]
  POST /api/v1/favorites/{product_id}/subscribe → 201 SubscriptionResponse
  DELETE /api/v1/favorites/{product_id}/subscribe → 204 No Content
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NotifyOnEvent(str, Enum):
    """
    Notification trigger events.
    Values are from b2c/openapi.yaml spec (not canon IN_STOCK / PRICE_DOWN).
    """
    BACK_IN_STOCK = "BACK_IN_STOCK"
    PRICE_DROP = "PRICE_DROP"


class SubscribeRequest(BaseModel):
    """
    Request body for POST /api/v1/favorites/{product_id}/subscribe.
    notify_on must be a non-empty list of valid events.
    """
    notify_on: List[NotifyOnEvent] = Field(..., min_length=1)

    @field_validator("notify_on")
    @classmethod
    def deduplicate(cls, v: list[NotifyOnEvent]) -> list[NotifyOnEvent]:
        """Remove duplicates while preserving order."""
        seen: set[NotifyOnEvent] = set()
        result: list[NotifyOnEvent] = []
        for item in v:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class SubscriptionResponse(BaseModel):
    """
    Response body for 201 Created on POST subscribe.
    """
    product_id: UUID
    user_id: UUID
    notify_on: List[str]
    created_at: datetime
