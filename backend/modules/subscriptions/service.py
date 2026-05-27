"""
SubscriptionsService — CRUD for product_subscriptions (US-B2C-07).

Canon: b2c-cart-flows.md#b2c-7-subscriptions
Spec:  b2c/openapi.yaml (neomarket-protocols)

Rules:
  - user_id ALWAYS from JWT claims (never from query/body) — IDOR prevention.
  - notify_on validated against NotifyOnEvent enum (400 INVALID_NOTIFY_ON).
  - 404 if product does not exist in B2B catalog.
  - 409 DUPLICATE_SUBSCRIPTION if subscription already exists for this pair.
  - DELETE is idempotent: 204 even if subscription does not exist.
  - Notification sending is OUT OF SCOPE — store only.

ADR (storage decision for notify_on):
  Chose PostgreSQL TEXT[] over a separate event rows table or JSONB:
  - Two possible values (BACK_IN_STOCK, PRICE_DROP) → no need for join overhead.
  - TEXT[] natively supported by asyncpg / SQLAlchemy ARRAY(TEXT).
  - GIN index available if needed for future "find all subscribers of event X".
  - Application validates list on write; DB carries no enum constraint (simpler
    migration path if spec adds new event types).
"""
from __future__ import annotations

from typing import List
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_BASE_URL, B2C_TO_B2B_KEY
from backend.modules.subscriptions.models import ProductSubscription
from backend.modules.subscriptions.schemas import NotifyOnEvent, SubscriptionResponse


# ──────────────────────────────────────────────────────────────────────────────
# Internal B2B helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _product_exists_in_b2b(
    product_id: UUID,
    b2b_base_url: str,
    service_key: str,
) -> bool:
    """
    Check product existence via B2B batch endpoint.

    POST /api/v1/public/products/batch {product_ids: [id]}
    Returns True if B2B includes the product in its response (any status),
    False if the product is absent (unknown/deleted/blocked).

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/public/products/batch",
            json={"product_ids": [str(product_id)]},
            headers={"X-Service-Key": service_key},
        )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    items: list[dict] = resp.json()
    return any(item.get("id") == str(product_id) for item in items)


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────


class SubscriptionsService:
    """
    Static-method service for product subscriptions CRUD.
    All data stored in the local PostgreSQL (product_subscriptions table).
    """

    @staticmethod
    async def subscribe(
        db: AsyncSession,
        *,
        user_id: UUID,
        product_id: UUID,
        notify_on: List[NotifyOnEvent],
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> SubscriptionResponse:
        """
        Create a product notification subscription.

        Guardrail order (canon b2c-cart-flows.md#b2c-7):
          1. Validate notify_on (already done by Pydantic schema → 400).
          2. Check product exists in B2B → 404 if not.
          3. INSERT into product_subscriptions → 409 on duplicate.

        Returns SubscriptionResponse for 201 Created.
        Raises:
          ValueError("PRODUCT_NOT_FOUND")       → 404
          ValueError("DUPLICATE_SUBSCRIPTION")  → 409
          httpx.ConnectError/TimeoutException   → 502
        """
        # Step 2 — verify product exists in B2B
        exists = await _product_exists_in_b2b(product_id, b2b_base_url, service_key)
        if not exists:
            raise ValueError("PRODUCT_NOT_FOUND")

        # Step 3 — insert; catch unique violation → 409
        notify_on_values = [e.value for e in notify_on]
        sub = ProductSubscription(
            user_id=user_id,
            product_id=product_id,
            notify_on=notify_on_values,
        )
        db.add(sub)
        try:
            await db.commit()
            await db.refresh(sub)
        except IntegrityError:
            await db.rollback()
            raise ValueError("DUPLICATE_SUBSCRIPTION")

        return SubscriptionResponse(
            product_id=product_id,
            user_id=user_id,
            notify_on=notify_on_values,
            created_at=sub.created_at,
        )

    @staticmethod
    async def unsubscribe(
        db: AsyncSession,
        *,
        user_id: UUID,
        product_id: UUID,
    ) -> None:
        """
        Remove a product subscription (idempotent).

        Canon b2c-cart-flows.md#b2c-7: DELETE always 204, even if not subscribed.
        No B2B check needed — just delete the local row if it exists.
        """
        await db.execute(
            delete(ProductSubscription).where(
                ProductSubscription.user_id == user_id,
                ProductSubscription.product_id == product_id,
            )
        )
        await db.commit()
