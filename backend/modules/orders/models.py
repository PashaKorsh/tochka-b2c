"""
Order and OrderItem models — US-B2C-09 / US-B2C-10 / US-B2C-13.

Architecture decisions:
  - idempotency_key is a UNIQUE column on orders — DB enforces race-safety atomically.
    Two concurrent POSTs with the same key: one wins INSERT, the other catches
    IntegrityError and reads the committed row. No separate cache table needed.
  - status starts at PAID immediately (mock payment — no real payment gateway).
  - unit_price / line_total are integer cents (snapshot at checkout time).
  - delivery_address is a TEXT snapshot (freeform) — no FK to an address table
    because B2C does not have an address registry yet.
    Spec deviation: spec.OrderResponse.address is AddressResponse (structured object);
    current impl keeps it as a string until the address-book service is built.
  - payment_method_id stored as UUID for future integration; payment is mocked.
  - updated_at tracks the last status transition (PAID→ASSEMBLING→…).
  - fulfill_completed_at (US-B2C-13): set when B2B POST /inventory/fulfill succeeds.
    NULL = fulfill not yet sent or previously failed (retry eligible).
    Non-NULL = fulfill acknowledged by B2B — skip on repeated deliver calls.

Canon: b2c-cart-flows.md#b2c-09-checkout, b2c-orders-flows.md#b2c-10-view-orders,
       b2c-orders-flows.md#b2c-13-fulfill
Spec:  b2c/openapi.yaml — OrderResponse, OrderItem, PaginatedOrders
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    buyer_id = Column(PG_UUID(as_uuid=True), nullable=False)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    status = Column(String(32), nullable=False, default="PAID")
    delivery_address = Column(Text, nullable=False)
    payment_method_id = Column(PG_UUID(as_uuid=True), nullable=True)
    subtotal = Column(Integer, nullable=False)  # sum of line_totals, in cents
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
    # NULL = not yet sent or last attempt failed (eligible for retry).
    fulfill_completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )

    __table_args__ = (
        Index("idx_orders_buyer_created", "buyer_id", "created_at"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    order_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    sku_id = Column(PG_UUID(as_uuid=True), nullable=False)
    product_id = Column(PG_UUID(as_uuid=True), nullable=False)
    name = Column(String(512), nullable=False)   # price-snapshot title (product + sku name)
    quantity = Column(Integer, nullable=False)
    unit_price = Column(Integer, nullable=False)  # effective price at checkout (cents)
    line_total = Column(Integer, nullable=False)  # unit_price * quantity

    __table_args__ = (
        Index("idx_order_items_order", "order_id"),
    )
