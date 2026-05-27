"""
BannersService — CRUD for banners and CTR analytics (US-B2C-14).

Canon: b2c-cart-flows.md#b2c-14-banners
Spec:  b2c/openapi.yaml (neomarket-protocols)

Rules:
  GET /api/v1/catalog/banners:
    - Filter: is_active=True AND (active_from IS NULL OR active_from <= now())
                               AND (active_to   IS NULL OR active_to   >= now())
    - Sort by ordering ASC (lower = higher priority in slider)
    - No auth required (public endpoint)
    - Response: array of Banner objects (spec schema, NOT wrapped in {items, total_count})

  POST /api/v1/banner-events:
    - Accepts batch of {banner_id, event, timestamp} objects
    - Validates all banner_ids exist → 400 BANNER_NOT_FOUND if any missing
    - Empty events array → 400 EMPTY_EVENTS (enforced by Pydantic min_length=1)
    - user_id optional (works for unauthenticated visitors)
    - Returns {accepted: N} count
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.banners.models import Banner, BannerEvent
from backend.modules.banners.schemas import (
    BannerEventItem,
    BannerResponse,
)


class BannersService:
    @staticmethod
    async def list_active_banners(db: AsyncSession) -> list[BannerResponse]:
        """
        Return all banners that are active right now.

        Filter (canon B2C-14 §filtering):
          is_active = True
          AND (active_from IS NULL OR active_from <= now())
          AND (active_to   IS NULL OR active_to   >= now())
        Sorted by ordering ASC.
        """
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(Banner)
            .where(
                Banner.is_active == True,  # noqa: E712
                (Banner.active_from == None) | (Banner.active_from <= now),
                (Banner.active_to == None) | (Banner.active_to >= now),
            )
            .order_by(Banner.ordering.asc())
        )
        rows = result.scalars().all()
        return [BannerResponse.model_validate(row) for row in rows]

    @staticmethod
    async def record_events(
        db: AsyncSession,
        *,
        events: list[BannerEventItem],
        user_id: Optional[UUID] = None,
    ) -> int:
        """
        Persist a batch of banner analytics events.

        Validates that all referenced banner_ids exist.
        Raises ValueError("BANNER_NOT_FOUND:<id>") for unknown banner.

        Returns the count of persisted events.
        """
        # Collect unique banner_ids to validate existence
        banner_ids = {e.banner_id for e in events}
        result = await db.execute(
            select(Banner.id).where(Banner.id.in_(banner_ids))
        )
        found_ids = {row for row in result.scalars()}
        missing = banner_ids - found_ids
        if missing:
            bad = next(iter(missing))
            raise ValueError(f"BANNER_NOT_FOUND:{bad}")

        # Insert all events
        now = datetime.now(timezone.utc)
        for evt in events:
            db.add(
                BannerEvent(
                    banner_id=evt.banner_id,
                    user_id=user_id,
                    event=evt.event.value,
                    timestamp=evt.timestamp,
                    created_at=now,
                )
            )
        await db.commit()
        return len(events)
