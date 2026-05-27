"""
EventsService — incoming B2B event handler (US-B2C-12).

Canon: b2c-orders-flows.md#b2c-12-handle-events
Spec:  b2c/openapi.yaml — POST /api/v1/b2b/events

Event processing rules:
  PRODUCT_BLOCKED / PRODUCT_HARD_BLOCKED / PRODUCT_DELETED
    → UPDATE cart_items SET unavailable_reason = <event_type>
       WHERE product_id = payload.product_id
    → Orders NOT touched (prices fixed, seller obligated to deliver).

  SKU_OUT_OF_STOCK
    → UPDATE cart_items SET unavailable_reason = 'OUT_OF_STOCK'
       WHERE sku_id = payload.sku_id

  SKU_BACK_IN_STOCK / PRICE_CHANGED
    → No cart mutation (future: trigger notifications). Logged for idempotency.

Idempotency:
  On duplicate idempotency_key → return immediately (no side effects).
  A B2BEventLog row is inserted AFTER successful processing (write-last pattern).
  Race condition: two simultaneous identical events → one inserts, the other
  catches IntegrityError → treats as duplicate, returns 202.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.modules.cart.models import CartItem
from backend.modules.events.models import B2BEventLog
from backend.modules.events.schemas import B2BEventRequest, B2BEventType

logger = logging.getLogger(__name__)

# Event types that map to product_id in payload
_PRODUCT_LEVEL_EVENTS = {
    B2BEventType.PRODUCT_BLOCKED,
    B2BEventType.PRODUCT_HARD_BLOCKED,
    B2BEventType.PRODUCT_DELETED,
}


class EventsService:
    @staticmethod
    async def handle_b2b_event(
        db: AsyncSession,
        *,
        event: B2BEventRequest,
    ) -> bool:
        """
        Process an incoming B2B event.

        Returns True if processed (new event), False if skipped (duplicate).
        Both cases → 202 to caller (idempotency is transparent to B2B).

        Algorithm:
          1. Check B2BEventLog for idempotency_key.
          2. If found → return False (already processed, skip).
          3. Apply cart mutations based on event_type.
          4. Insert B2BEventLog row.
          5. On IntegrityError → race with duplicate → return False.
        """
        key_str = str(event.idempotency_key)

        # Step 1 — fast idempotency check
        exists = await db.execute(
            select(B2BEventLog).where(B2BEventLog.idempotency_key == key_str)
        )
        if exists.scalar_one_or_none() is not None:
            logger.info(
                "Duplicate B2B event skipped: key=%s type=%s",
                key_str,
                event.event_type,
            )
            return False

        # Step 2 — apply cart mutations
        await _apply_cart_update(db, event)

        # Step 3 — log for idempotency (write last)
        db.add(
            B2BEventLog(
                idempotency_key=key_str,
                event_type=event.event_type.value,
                processed_at=datetime.now(timezone.utc),
            )
        )

        try:
            await db.commit()
        except IntegrityError:
            # Concurrent duplicate event won the race; treat as already processed.
            await db.rollback()
            logger.info(
                "Concurrent duplicate B2B event (IntegrityError): key=%s",
                key_str,
            )
            return False

        return True


async def _apply_cart_update(db: AsyncSession, event: B2BEventRequest) -> None:
    """
    Batch-update cart_items based on event_type.

    Uses a single UPDATE statement (not N individual UPDATEs) for efficiency.
    Orders are intentionally NOT touched — canon rule: "заказы не трогаем".
    """
    if event.event_type in _PRODUCT_LEVEL_EVENTS:
        product_id_raw = event.payload.get("product_id")
        if not product_id_raw:
            logger.warning(
                "B2B event %s missing product_id in payload", event.event_type
            )
            return
        product_id = UUID(str(product_id_raw))

        await db.execute(
            update(CartItem)
            .where(CartItem.product_id == product_id)
            .values(unavailable_reason=event.event_type.value)
        )
        logger.info(
            "Marked cart items unavailable for product_id=%s reason=%s",
            product_id,
            event.event_type.value,
        )

    elif event.event_type == B2BEventType.SKU_OUT_OF_STOCK:
        sku_id_raw = event.payload.get("sku_id")
        if not sku_id_raw:
            logger.warning("B2B event SKU_OUT_OF_STOCK missing sku_id in payload")
            return
        sku_id = UUID(str(sku_id_raw))

        await db.execute(
            update(CartItem)
            .where(CartItem.sku_id == sku_id)
            .values(unavailable_reason="OUT_OF_STOCK")
        )
        logger.info("Marked cart item unavailable for sku_id=%s", sku_id)

    else:
        # SKU_BACK_IN_STOCK, PRICE_CHANGED — log only; no cart mutation yet
        logger.info(
            "B2B event %s received — no cart mutation (future: notifications)",
            event.event_type,
        )
