"""
Order and OrderItem models — US-B2C-09 / US-B2C-10 / US-B2C-11 / US-B2C-13.

Checkout flow (spec b2c/openapi.yaml):
  - Items come from the buyer's active cart (not from request body).
  - address_id references a saved buyer address (addresses table).
  - Address data is snapshotted as JSON into address_snapshot at checkout time,
    so subsequent address edits don't retroactively change order history.
  - payment_method_id is stored as UUID (mock payment — no real gateway).
  - status starts at PAID immediately.
  - idempotency_key enforced as UNIQUE to prevent double-checkout.
  - unit_price / line_total in OrderItem are fixed at checkout time.
  - fulfill_completed_at (US-B2C-13): set when B2B inventory/fulfill succeeds.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    status = Column(String(32), nullable=False, default="PAID")

    # Address: reference + JSON snapshot (prevents edits from affecting history)
    address_id = Column(PG_UUID(as_uuid=True), nullable=False)
    address_snapshot = Column(Text, nullable=False, default="{}")  # JSON

    payment_method_id = Column(PG_UUID(as_uuid=True), nullable=False)
    comment = Column(Text, nullable=True)

    subtotal = Column(Integer, nullable=False)  # sum of line_totals, kopecks
    total = Column(Integer, nullable=False)     # same as subtotal (no extra fees yet)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    # US-B2C-13: set when B2B inventory/fulfill is successfully acknowledged.
    fulfill_completed_at = Column(DateTime(timezone=True), nullable=True, default=None)

    __table_args__ = (
        Index("idx_orders_buyer_created", "buyer_id", "created_at"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    sku_id = Column(PG_UUID(as_uuid=True), nullable=False)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False)
    name = Column(String(512), nullable=False)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Integer, nullable=False)  # effective price at checkout, kopecks
    line_total = Column(Integer, nullable=False)   # unit_price * quantity

    __table_args__ = (
        Index("idx_order_items_order", "order_id"),
    )
