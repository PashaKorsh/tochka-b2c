"""
SQLAlchemy models for product collections (US-B2C-15).

Schema: canon b2c-cart-flows.md#b2c-15-collections
Field names: spec b2c/openapi.yaml Collection schema (name vs canon title).

ADR — storage of collection↔product relationship:
  Three options considered:
  1. UUID[] array in collections table — simple reads, but no per-item ordering
     column without extra complexity; array updates require full-row rewrites.
  2. Separate junction table collection_products (chosen) — supports explicit
     ordering per product, cascade delete when collection is removed, individual
     product add/remove is a simple INSERT/DELETE on the junction row.
  3. Copy product data inline — violates B2C architecture principle "B2C stores
     only references"; creates stale-data risk when B2B prices/titles change.

  Decision: separate junction table collection_products.
  Criteria:
    (1) Simplicity of updating collection contents: add/remove a product_id via
        a single junction-table DML without touching the parent collection row.
    (2) Consistency when product deleted in B2B: product_id remains in the
        junction table; it is silently filtered at enrichment time (missing from
        B2B batch response → goes to unavailable_ids). The collection itself is
        never broken by a B2B-side deletion.

Collections are created via Django Admin (not via API); this FastAPI service
only reads them. create_all handles the DDL in the test environment.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from backend.database import Base


class Collection(Base):
    __tablename__ = "collections"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # spec uses "name"; canon uses "title" — spec wins for API field name
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    cover_image_url = Column(String(500), nullable=True)
    target_url = Column(String(500), nullable=True)
    priority = Column(Integer, nullable=False, default=0, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    start_date = Column(Date, nullable=True)     # NULL = always active
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class CollectionProduct(Base):
    """
    Junction table: ordered list of product_ids per collection.

    product_id is a plain UUID — no FK to a products table (B2C stores
    only references, not product data).  ON DELETE CASCADE on collection_id
    ensures cleanup when a collection is removed.
    """
    __tablename__ = "collection_products"

    collection_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("collections.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    product_id = Column(PG_UUID(as_uuid=True), nullable=False, primary_key=True)
    ordering = Column(Integer, nullable=False, default=0)
