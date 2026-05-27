"""
SQLAlchemy models for banners and CTR analytics (US-B2C-14).

Schema driven by spec b2c/openapi.yaml (Banner) + canon b2c-cart-flows.md#b2c-14-banners.

Field name resolution (spec > canon):
  ordering  (spec)  ← priority (canon)
  active_from (spec) ← start_at (canon)
  active_to   (spec) ← end_at   (canon)

Banners are created via Django Admin (not via API). The table definition
here matches what Django's ORM would produce for a standard model.
For this FastAPI service the table is created via SQLAlchemy's create_all.

ADR — click analytics storage:
  Chose row-per-event write with client-side batching (POST /banner-events
  accepts an array of events). Three options considered:
  1. Row per event, immediate write — simplest, full auditability,
     CTR = SUM(click)/SUM(impression) with one GROUP BY query.
     Risk: high insert rate on popular home pages (10k+ visits/min).
  2. Batched write (this choice) — client accumulates events client-side and
     POSTs batches every N seconds or on page unload. Reduces insert pressure
     by 10-50×. CTR aggregation stays trivial (same GROUP BY). Keeps the DB
     as the single source of truth with no external infrastructure.
  3. External analytics system (ClickHouse / Kafka) — best throughput,
     cost-effective at huge scale. Overkill for current traffic; adds ops
     complexity (separate cluster, schema sync).
  Decision: Option 2 — client-batched inserts into a local PostgreSQL table.
  Criteria satisfied: (1) acceptable DB load via batching; (2) CTR aggregation
  is a single SQL query; (3) no additional infrastructure needed.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Banner(Base):
    __tablename__ = "banners"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=True)
    image_url = Column(String(500), nullable=False)
    link = Column(String(500), nullable=False)
    ordering = Column(Integer, nullable=False, default=0, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    # Schedule: NULL means "always / no limit"
    active_from = Column(DateTime(timezone=True), nullable=True)
    active_to = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class BannerEvent(Base):
    """
    One row per analytics event (impression or click).

    Client batches events and POSTs them via POST /api/v1/banner-events.
    user_id is NULL for unauthenticated visitors.
    CTR = COUNT(*) FILTER (WHERE event='click') /
          COUNT(*) FILTER (WHERE event='impression')
    GROUP BY banner_id, date_trunc('day', timestamp)
    """
    __tablename__ = "banner_events"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    banner_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("banners.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(PG_UUID(as_uuid=True), nullable=True)
    event = Column(String(20), nullable=False)       # 'impression' | 'click'
    timestamp = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
