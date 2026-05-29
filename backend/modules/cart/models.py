"""
SQLAlchemy model for the cart_items table (US-B2C-08 / US-B2C-12).

Schema (canon b2c-cart-flows.md#b2c-8-cart):
  cart_items (id, user_id, session_id, sku_id, product_id, quantity,
              unavailable_reason, created_at, updated_at)

Identity rules:
  - Authenticated users: user_id from JWT claims (NOT NULL), session_id = NULL.
  - Guests: session_id from X-Session-Id header (NOT NULL), user_id = NULL.
  - At least one must be non-NULL (CHECK constraint).

Unique indexes prevent duplicate SKUs per cart identity:
  - (user_id, sku_id) WHERE user_id IS NOT NULL
  - (session_id, sku_id) WHERE session_id IS NOT NULL

product_id is stored alongside sku_id (discovered from B2B on add) to allow
efficient batch enrichment via POST /api/v1/public/products/batch on GET /cart.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(PG_UUID(as_uuid=True), nullable=True)
    session_id = Column(String, nullable=True)
    sku_id = Column(PG_UUID(as_uuid=True), nullable=False)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False)
    quantity = Column(Integer, nullable=False)
    # Set by US-B2C-12 (handle B2B events): PRODUCT_BLOCKED, PRODUCT_HARD_BLOCKED,
    # PRODUCT_DELETED, OUT_OF_STOCK. NULL = item is available.
    unavailable_reason = Column(Text, nullable=True, default=None)
    # Snapshot of effective unit price (kopecks) at the time the item was added.
    # Used by POST /cart/validate to detect PRICE_CHANGED. NULL for old rows.
    unit_price_snapshot = Column(Integer, nullable=True, default=None)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # At least one identity must be present
        CheckConstraint(
            "user_id IS NOT NULL OR session_id IS NOT NULL",
            name="cart_identity",
        ),
        # One SKU per authenticated user
        Index(
            "idx_cart_user_sku",
            "user_id",
            "sku_id",
            unique=True,
            postgresql_where="user_id IS NOT NULL",
        ),
        # One SKU per guest session
        Index(
            "idx_cart_session_sku",
            "session_id",
            "sku_id",
            unique=True,
            postgresql_where="session_id IS NOT NULL",
        ),
    )
