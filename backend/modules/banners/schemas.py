"""
Pydantic schemas for banners (US-B2C-14).

GET /api/v1/catalog/banners:
  Spec b2c/openapi.yaml Banner schema:
    required: [id, image_url, link]
    optional: title, ordering, active_from, active_to

POST /api/v1/banner-events:
  Not in spec — implemented per canon b2c-cart-flows.md#b2c-14-banners.
  Accepts batch of events (impression | click) per banner.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/banners
# ──────────────────────────────────────────────────────────────────────────────


class BannerResponse(BaseModel):
    """
    Single banner per spec b2c/openapi.yaml#Banner.

    Field names from spec (ordering, active_from, active_to) override
    canon names (priority, start_at, end_at).
    """
    id: UUID
    title: Optional[str] = None
    image_url: str
    link: str
    ordering: Optional[int] = None
    active_from: Optional[datetime] = None
    active_to: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/banner-events
# ──────────────────────────────────────────────────────────────────────────────


class BannerEventType(str, Enum):
    """Allowed event types per canon b2c-cart-flows.md#b2c-14-banners."""
    IMPRESSION = "impression"
    CLICK = "click"


class BannerEventItem(BaseModel):
    """Single analytics event within a batch."""
    banner_id: UUID
    event: BannerEventType
    timestamp: datetime


class BannerEventsRequest(BaseModel):
    """
    Request body for POST /api/v1/banner-events.
    Minimum 1 event per batch (empty → 400 EMPTY_EVENTS per canon).
    """
    events: List[BannerEventItem] = Field(..., min_length=1)


class BannerEventsResponse(BaseModel):
    """Acknowledgement response for accepted events."""
    accepted: int   # number of events persisted
