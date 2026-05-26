"""
SQLAlchemy model for the favorites table (US-B2C-06).

Schema (canon b2c-cart-flows.md#b2c-6-favorites):
  favorites (id, user_id, product_id, added_at)
  UNIQUE(user_id, product_id)   — enforces idempotency at DB level.

B2C stores ONLY the product_id + user_id + timestamp.
Actual product data comes from B2B on every GET /favorites.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Favorite(Base):
    __tablename__ = "favorites"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PG_UUID(as_uuid=True), nullable=False, index=True)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False)
    added_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_favorites_user_product"),
    )
