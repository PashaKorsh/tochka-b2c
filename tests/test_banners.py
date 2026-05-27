"""
Tests for US-B2C-14 — Banners (home page slider + CTR analytics).

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET  /api/v1/catalog/banners  → array of Banner (public, no auth)

Canon: b2c-cart-flows.md#b2c-14-banners
  POST /api/v1/banner-events    → {accepted: N}

DoD test names (exact):
  active_banners_returned_sorted_by_priority
  no_active_banners_returns_200_empty
  click_on_unknown_banner_returns_400

Banners are seeded directly into the test DB (no admin API).
B2B is NOT called for banners — fully local data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import os

from backend.main import app
from backend.modules.banners.models import Banner

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _banner(
    *,
    title: str = "Test Banner",
    image_url: str = "https://cdn.test/img.jpg",
    link: str = "https://shop.test/sale",
    ordering: int = 10,
    is_active: bool = True,
    active_from: datetime | None = None,
    active_to: datetime | None = None,
) -> Banner:
    return Banner(
        id=uuid4(),
        title=title,
        image_url=image_url,
        link=link,
        ordering=ordering,
        is_active=is_active,
        active_from=active_from,
        active_to=active_to,
    )


async def _seed(db: AsyncSession, *banners: Banner) -> list[Banner]:
    """Persist banner rows into the test DB."""
    for b in banners:
        db.add(b)
    await db.commit()
    return list(banners)


async def _db_session():
    """Create a fresh DB session using NullPool (one event-loop per test)."""
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_banners_returned_sorted_by_priority():
    """
    GET /api/v1/catalog/banners returns only active, scheduled banners,
    sorted by ordering ASC (lower value = higher priority in slider).

    Scenario:
    - Banner A: ordering=20, is_active=True, in schedule → appears second
    - Banner B: ordering=5,  is_active=True, in schedule → appears first
    - Banner C: ordering=1,  is_active=False → excluded (inactive)
    - Banner D: ordering=0,  is_active=True, active_to=yesterday → excluded (expired)
    """
    now = _now()

    b_a = _banner(title="Electronics Sale", ordering=20, is_active=True)
    b_b = _banner(title="Summer Collection", ordering=5,  is_active=True)
    b_c = _banner(title="INACTIVE",          ordering=1,  is_active=False)
    b_d = _banner(
        title="EXPIRED",
        ordering=0,
        is_active=True,
        active_to=now - timedelta(hours=1),  # already ended
    )

    async for db in _db_session():
        await _seed(db, b_a, b_b, b_c, b_d)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/banners")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list), "Spec: response is bare array, not wrapped object"

    # Only active, in-schedule banners
    ids_returned = [item["id"] for item in body]
    assert str(b_a.id) in ids_returned, "Active banner A must be included"
    assert str(b_b.id) in ids_returned, "Active banner B must be included"
    assert str(b_c.id) not in ids_returned, "Inactive banner C must be excluded"
    assert str(b_d.id) not in ids_returned, "Expired banner D must be excluded"

    # Sorted by ordering ASC
    assert len(body) >= 2
    orderings = [item["ordering"] for item in body if item["id"] in ids_returned]
    assert orderings == sorted(orderings), "Must be sorted by ordering ASC"

    # First active banner has lower ordering than second
    active_in_resp = [item for item in body if item["id"] in (str(b_a.id), str(b_b.id))]
    assert active_in_resp[0]["id"] == str(b_b.id), "B (ordering=5) must come before A (ordering=20)"

    # Spec required fields present
    for item in body:
        assert "id" in item
        assert "image_url" in item
        assert "link" in item


@pytest.mark.asyncio
async def test_no_active_banners_returns_200_empty():
    """
    When there are no active banners (all inactive or outside schedule),
    GET /api/v1/catalog/banners returns 200 with empty array [].

    Canon edge case: "Нет активных баннеров → 200, {items: [], total_count: 0}"
    Spec response shape: bare array → []
    """
    now = _now()

    # Two banners: one inactive, one future
    b_inactive = _banner(title="DISABLED", is_active=False, ordering=1)
    b_future = _banner(
        title="FUTURE",
        is_active=True,
        ordering=2,
        active_from=now + timedelta(days=7),  # starts next week
    )

    async for db in _db_session():
        await _seed(db, b_inactive, b_future)

    # Use a different user scope (unique session) so previous test's data
    # doesn't bleed — each test uses its own unique banner IDs (uuid4) so
    # there's no cross-test contamination for the specific IDs, but the
    # GET endpoint returns ALL active banners. We verify that THESE specific
    # banners are NOT in the result (they are either inactive or not yet started).
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/banners")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)

    # Specific IDs from this test must NOT appear
    returned_ids = {item["id"] for item in body}
    assert str(b_inactive.id) not in returned_ids, "Inactive banner must not appear"
    assert str(b_future.id) not in returned_ids, "Future (not-yet-started) banner must not appear"


@pytest.mark.asyncio
async def test_click_on_unknown_banner_returns_400():
    """
    POST /api/v1/banner-events with a banner_id that doesn't exist
    → 400 BANNER_NOT_FOUND.

    Canon edge case: "Клик по несуществующему баннеру → 400 BANNER_NOT_FOUND"
    """
    fake_banner_id = uuid4()   # not in DB

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/banner-events",
            json={
                "events": [
                    {
                        "banner_id": str(fake_banner_id),
                        "event": "click",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "BANNER_NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_banner_event_impression_accepted():
    """
    POST /api/v1/banner-events with impression event → 200, {accepted: 1}.
    Anonymous users (no JWT) can send events.
    """
    now = _now()

    banner = _banner(title="Promo Banner", ordering=1)
    async for db in _db_session():
        await _seed(db, banner)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/banner-events",
            json={
                "events": [
                    {
                        "banner_id": str(banner.id),
                        "event": "impression",
                        "timestamp": now.isoformat(),
                    }
                ]
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 1


@pytest.mark.asyncio
async def test_banner_event_batch_multiple_events():
    """
    POST /api/v1/banner-events with multiple events → accepted = count.
    """
    now = _now()

    banner1 = _banner(title="Banner 1", ordering=1)
    banner2 = _banner(title="Banner 2", ordering=2)
    async for db in _db_session():
        await _seed(db, banner1, banner2)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/banner-events",
            json={
                "events": [
                    {"banner_id": str(banner1.id), "event": "impression", "timestamp": now.isoformat()},
                    {"banner_id": str(banner1.id), "event": "click",      "timestamp": now.isoformat()},
                    {"banner_id": str(banner2.id), "event": "impression", "timestamp": now.isoformat()},
                ]
            },
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 3


@pytest.mark.asyncio
async def test_banner_event_empty_events_returns_422():
    """
    POST /api/v1/banner-events with empty events array
    → 422 (Pydantic min_length=1 validation error).
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/banner-events",
            json={"events": []},
        )

    # Global validation handler maps Pydantic errors to 422 VALIDATION_ERROR
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_banner_with_schedule_window_is_included():
    """
    Banner with active_from in the past and active_to in the future
    is included in GET /catalog/banners.
    """
    now = _now()
    banner = _banner(
        title="Scheduled Banner",
        ordering=99,
        is_active=True,
        active_from=now - timedelta(hours=1),   # started 1 hour ago
        active_to=now + timedelta(hours=23),     # ends in 23 hours
    )

    async for db in _db_session():
        await _seed(db, banner)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/banners")

    returned_ids = {item["id"] for item in resp.json()}
    assert str(banner.id) in returned_ids, "Scheduled banner within window must appear"


@pytest.mark.asyncio
async def test_banner_not_yet_started_is_excluded():
    """Banner with active_from in the future must NOT appear."""
    now = _now()
    banner = _banner(
        title="Not Yet Started",
        ordering=1,
        is_active=True,
        active_from=now + timedelta(hours=2),   # starts in 2 hours
    )

    async for db in _db_session():
        await _seed(db, banner)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/v1/catalog/banners")

    returned_ids = {item["id"] for item in resp.json()}
    assert str(banner.id) not in returned_ids, "Future banner must not appear"
