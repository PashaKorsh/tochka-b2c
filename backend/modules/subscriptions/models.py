"""
SQLAlchemy model for the product_subscriptions table (US-B2C-07).

Schema (canon b2c-cart-flows.md#b2c-7-subscriptions):
  product_subscriptions (id, user_id, product_id, notify_on, created_at)
  UNIQUE(user_id, product_id)   — one subscription per user+product pair.

notify_on is stored as PostgreSQL TEXT[] with validated values:
  BACK_IN_STOCK — notify when product comes back in stock.
  PRICE_DROP    — notify when price drops.

(Values per b2c/openapi.yaml spec, which overrides canon IN_STOCK / PRICE_DOWN.)

ADR: TEXT[] vs separate table vs JSONB
  - Separate rows table: overkill for 2 events; complicates upsert idempotency.
  - JSONB: no indexed enum constraint, over-engineered for a small set.
  - TEXT[]: lean, native PG, supports GIN index, aligns with canon schema.
  - Decision: TEXT[] with application-level validation against enum values.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, TEXT, UUID as PG_UUID

from backend.database import Base


class ProductSubscription(Base):
    __tablename__ = "product_subscriptions"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False, index=True)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False)
    notify_on = Column(ARRAY(TEXT), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "product_id", name="uq_subscriptions_user_product"
        ),
    )
