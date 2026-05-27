"""
B2BEventLog — idempotency log for incoming B2B events (US-B2C-12).

Architecture:
  - idempotency_key is the PRIMARY KEY (no extra id needed).
  - One row per processed event. On duplicate key → skip processing.
  - No TTL enforced at DB level; cleanup can be done by a cron job
    (DELETE WHERE processed_at < NOW() - INTERVAL '24 hours').

ADR — idempotency storage (3 options):
  A) Separate table (chosen):
     Clean separation; SQL DELETE for cleanup; survives service restart.
     Disk growth: O(events/day * TTL_days); easily bounded by a daily cleanup job.
  B) Field on cart_items (e.g., last_event_key):
     Zero extra table, but: only works for events that touch cart items;
     does not cover PRICE_CHANGED, SKU_BACK_IN_STOCK, or future event types.
  C) Redis SET NX with TTL:
     Sub-ms idempotency check; auto-expiry (no cleanup cron).
     Requires extra infra; history is lost on TTL; not durable across Redis restart
     without persistence. Overkill for the current event throughput.
  Criteria: 1) risk of disk/memory leak (A: bounded by DELETE cron; C: auto-expire),
            2) cleanup complexity (A: simple DELETE; C: none; B: N/A).
  Winner: A — straightforward, durable, no extra infra.

Canon: b2c-orders-flows.md#b2c-12-handle-events
Spec:  b2c/openapi.yaml — POST /api/v1/b2b/events (idempotency_key TTL 24h noted)
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String

from backend.database import Base


class B2BEventLog(Base):
    __tablename__ = "b2b_event_logs"

    idempotency_key = Column(String(255), primary_key=True)
    event_type = Column(String(64), nullable=False)
    processed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
