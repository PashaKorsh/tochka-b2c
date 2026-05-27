"""
CartService — CRUD for cart_items (US-B2C-08).

Canon: b2c-cart-flows.md#b2c-8-cart
Spec:  b2c/openapi.yaml (neomarket-protocols)

Architecture:
  - B2C stores only (sku_id, product_id, quantity, identity).
    Prices and availability are NEVER stored — always fetched from B2B.
  - product_id is cached on add (via GET /api/v1/public/skus/{sku_id})
    to enable efficient batch enrichment on GET /cart.
  - Enrichment on GET /cart uses POST /api/v1/public/products/batch.
    B2B returns only MODERATED, not-deleted products. Missing ones → PRODUCT_DELETED.
  - Lazy reserve: cart does NOT reserve stock. Reserve happens at checkout (POST /orders).
  - unavailable_reason is computed at enrichment time, never stored in DB.

IDOR prevention:
  - user_id ALWAYS from JWT claims (never from query/body).
  - session_id ALWAYS from X-Session-Id header (never from query/body).
  - All DB queries filter by identity automatically.
  - Access to item not belonging to caller → treated as 404 (enumeration-safe).

Merge strategy (canon B2C-8):
  For each guest cart_item:
    - If sku_id already in user cart → quantity = MAX(guest_qty, auth_qty)
    - Else → reassign to user (user_id=user_id, session_id=None)
  Then delete remaining guest cart_items.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import B2B_BASE_URL, B2C_TO_B2B_KEY
from backend.modules.cart.models import CartItem
from backend.modules.cart.schemas import (
    CartItemSchema,
    CartResponseSchema,
    ImageRef,
)


# ──────────────────────────────────────────────────────────────────────────────
# B2B helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _fetch_sku_from_b2b(
    sku_id: UUID,
    b2b_base_url: str,
    service_key: str,
) -> dict[str, Any]:
    """
    GET /api/v1/public/skus/{sku_id} — validate SKU and get product_id.

    Returns the SKUPublicResponse dict.
    Raises ValueError("SKU_NOT_FOUND") if 404.
    Raises httpx errors on network failure.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/public/skus/{sku_id}",
            headers={"X-Service-Key": service_key},
        )
    if resp.status_code == 404:
        raise ValueError("SKU_NOT_FOUND")
    resp.raise_for_status()
    return resp.json()


async def _batch_fetch_products(
    product_ids: list[UUID],
    b2b_base_url: str,
    service_key: str,
) -> dict[str, dict[str, Any]]:
    """
    POST /api/v1/public/products/batch → map of product_id → ProductPublicResponse.

    B2B returns only MODERATED, not-deleted products.
    Missing IDs mean the product is blocked/deleted.
    """
    if not product_ids:
        return {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{b2b_base_url}/api/v1/public/products/batch",
            json={"product_ids": [str(pid) for pid in product_ids]},
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()
    items: list[dict[str, Any]] = resp.json()
    return {item["id"]: item for item in items}


def _enrich_cart_item(
    row: CartItem,
    b2b_products: dict[str, dict[str, Any]],
) -> CartItemSchema:
    """
    Build a CartItemSchema from a DB row + B2B product data.

    Availability rules:
      - Product not in B2B response → PRODUCT_DELETED (blocked/deleted)
      - SKU not in product's skus → PRODUCT_DELETED at SKU level
      - SKU found, active_quantity = 0 → OUT_OF_STOCK
      - SKU found, active_quantity > 0 → available
    """
    product_data = b2b_products.get(str(row.product_id))

    if product_data is None:
        # Product blocked or deleted in B2B
        return CartItemSchema(
            sku_id=row.sku_id,
            product_id=row.product_id,
            name="Товар недоступен",
            quantity=row.quantity,
            unit_price=0,
            line_total=0,
            available_quantity=0,
            is_available=False,
            unavailable_reason="PRODUCT_DELETED",
        )

    # Find the specific SKU within the product
    skus: list[dict[str, Any]] = product_data.get("skus", [])
    sku_data = next(
        (s for s in skus if s.get("id") == str(row.sku_id)),
        None,
    )

    if sku_data is None:
        return CartItemSchema(
            sku_id=row.sku_id,
            product_id=row.product_id,
            name=product_data.get("title", "Товар недоступен"),
            quantity=row.quantity,
            unit_price=0,
            line_total=0,
            available_quantity=0,
            is_available=False,
            unavailable_reason="PRODUCT_DELETED",
        )

    active_qty: int = sku_data.get("active_quantity", 0) or 0
    is_available = active_qty > 0
    unavailable_reason: Optional[str] = None if is_available else "OUT_OF_STOCK"

    # Price: price minus discount
    price: int = sku_data.get("price", 0) or 0
    discount: int = sku_data.get("discount", 0) or 0
    unit_price = max(0, price - discount)
    line_total = unit_price * row.quantity if is_available else 0

    # Name: product title + sku name/article if available
    product_title: str = product_data.get("title", "")
    sku_name: str = sku_data.get("name", "")
    name = f"{product_title} — {sku_name}" if sku_name else product_title

    sku_code: Optional[str] = sku_data.get("article") or sku_data.get("sku_code")

    # Image: first image from SKU or product
    raw_images: list[dict] = sku_data.get("images", []) or product_data.get("images", [])
    image: Optional[ImageRef] = None
    if raw_images:
        first = raw_images[0]
        url = first.get("url") if isinstance(first, dict) else first
        if url:
            image = ImageRef(url=url)

    return CartItemSchema(
        sku_id=row.sku_id,
        product_id=row.product_id,
        name=name,
        sku_code=sku_code,
        quantity=row.quantity,
        unit_price=unit_price,
        line_total=line_total,
        available_quantity=active_qty,
        is_available=is_available,
        unavailable_reason=unavailable_reason,
        image=image,
    )


def _build_cart_response(
    rows: list[CartItem],
    b2b_products: dict[str, dict[str, Any]],
) -> CartResponseSchema:
    """Build a CartResponseSchema from DB rows and B2B enrichment data."""
    items: list[CartItemSchema] = [
        _enrich_cart_item(row, b2b_products) for row in rows
    ]

    subtotal = sum(item.line_total for item in items)
    items_count = sum(item.quantity for item in items)
    is_valid = all(item.is_available for item in items)

    updated_at = max(
        (row.updated_at for row in rows), default=None
    ) if rows else None

    return CartResponseSchema(
        items=items,
        items_count=items_count,
        subtotal=subtotal,
        is_valid=is_valid,
        updated_at=updated_at,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────


class CartService:
    """
    Static-method service for cart CRUD.
    All identity fields (user_id / session_id) come from the caller;
    the service never reads them from request query/body.
    """

    @staticmethod
    async def get_cart(
        db: AsyncSession,
        *,
        user_id: Optional[UUID],
        session_id: Optional[str],
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CartResponseSchema:
        """
        Return enriched cart contents.

        Algorithm (canon B2C-8 §enrichment):
          1. SELECT cart_items by identity.
          2. Collect unique product_ids.
          3. Batch-fetch from B2B (only available products returned).
          4. Enrich each item; missing products → PRODUCT_DELETED.
          5. Compute summary.
        """
        rows = await _select_items(db, user_id, session_id)
        if not rows:
            return CartResponseSchema(
                items=[], items_count=0, subtotal=0, is_valid=True
            )

        product_ids = list({row.product_id for row in rows})
        b2b_products = await _batch_fetch_products(
            product_ids, b2b_base_url, service_key
        )

        return _build_cart_response(rows, b2b_products)

    @staticmethod
    async def add_to_cart(
        db: AsyncSession,
        *,
        user_id: Optional[UUID],
        session_id: Optional[str],
        sku_id: UUID,
        quantity: int,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CartResponseSchema:
        """
        Add or increment a SKU in the cart.

        Guardrails (canon B2C-8 §add):
          1. GET /api/v1/public/skus/{sku_id} → validate SKU, get product_id.
          2. Check active_quantity >= quantity → 409 if insufficient.
          3. UPSERT: if SKU already in cart → quantity += requested.
             Check total quantity <= active_quantity after upsert → 409 if exceeds.
          4. Return enriched cart.

        Raises:
          ValueError("SKU_NOT_FOUND")           → 404
          ValueError("INSUFFICIENT_STOCK:N")    → 409 (N = available qty)
          httpx errors                           → 502
        """
        # Step 1 — validate SKU with B2B
        sku_data = await _fetch_sku_from_b2b(sku_id, b2b_base_url, service_key)
        active_qty: int = sku_data.get("active_quantity", 0) or 0
        product_id = UUID(sku_data["product_id"])

        # Step 2 — check initial availability
        if active_qty < quantity:
            raise ValueError(f"INSUFFICIENT_STOCK:{active_qty}")

        # Step 3 — upsert
        existing = await _find_item(db, user_id, session_id, sku_id)
        if existing is not None:
            new_qty = existing.quantity + quantity
            if active_qty < new_qty:
                raise ValueError(f"INSUFFICIENT_STOCK:{active_qty}")
            from datetime import datetime, timezone
            await db.execute(
                update(CartItem)
                .where(CartItem.id == existing.id)
                .values(quantity=new_qty, updated_at=datetime.now(timezone.utc))
            )
        else:
            db.add(
                CartItem(
                    user_id=user_id,
                    session_id=session_id,
                    sku_id=sku_id,
                    product_id=product_id,
                    quantity=quantity,
                )
            )

        await db.commit()

        # Step 4 — return enriched cart
        return await CartService.get_cart(
            db,
            user_id=user_id,
            session_id=session_id,
            b2b_base_url=b2b_base_url,
            service_key=service_key,
        )

    @staticmethod
    async def update_cart_item(
        db: AsyncSession,
        *,
        user_id: Optional[UUID],
        session_id: Optional[str],
        sku_id: UUID,
        quantity: int,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CartResponseSchema:
        """
        Set the quantity of an existing cart item.

        Raises:
          ValueError("ITEM_NOT_FOUND")           → 404
          ValueError("INSUFFICIENT_STOCK:N")    → 409
          httpx errors                           → 502
        """
        existing = await _find_item(db, user_id, session_id, sku_id)
        if existing is None:
            raise ValueError("ITEM_NOT_FOUND")

        # Validate stock
        sku_data = await _fetch_sku_from_b2b(sku_id, b2b_base_url, service_key)
        active_qty: int = sku_data.get("active_quantity", 0) or 0
        if active_qty < quantity:
            raise ValueError(f"INSUFFICIENT_STOCK:{active_qty}")

        from datetime import datetime, timezone
        await db.execute(
            update(CartItem)
            .where(CartItem.id == existing.id)
            .values(quantity=quantity, updated_at=datetime.now(timezone.utc))
        )
        await db.commit()

        return await CartService.get_cart(
            db,
            user_id=user_id,
            session_id=session_id,
            b2b_base_url=b2b_base_url,
            service_key=service_key,
        )

    @staticmethod
    async def remove_cart_item(
        db: AsyncSession,
        *,
        user_id: Optional[UUID],
        session_id: Optional[str],
        sku_id: UUID,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CartResponseSchema:
        """
        Remove a single SKU from the cart.

        Raises ValueError("ITEM_NOT_FOUND") → 404 if not in cart.
        """
        existing = await _find_item(db, user_id, session_id, sku_id)
        if existing is None:
            raise ValueError("ITEM_NOT_FOUND")

        await db.execute(
            delete(CartItem).where(CartItem.id == existing.id)
        )
        await db.commit()

        return await CartService.get_cart(
            db,
            user_id=user_id,
            session_id=session_id,
            b2b_base_url=b2b_base_url,
            service_key=service_key,
        )

    @staticmethod
    async def clear_cart(
        db: AsyncSession,
        *,
        user_id: Optional[UUID],
        session_id: Optional[str],
    ) -> None:
        """Delete all cart items for the given identity."""
        stmt = _build_identity_delete(user_id, session_id)
        await db.execute(stmt)
        await db.commit()

    @staticmethod
    async def merge_cart(
        db: AsyncSession,
        *,
        user_id: UUID,
        session_id: str,
        b2b_base_url: str = B2B_BASE_URL,
        service_key: str = B2C_TO_B2B_KEY,
    ) -> CartResponseSchema:
        """
        Merge guest cart (session_id) into authenticated user cart (user_id).

        Canon B2C-8 §merge strategy:
          For each guest item:
            - If sku_id already in user cart → quantity = MAX(guest, auth)
            - Else → set user_id, clear session_id
          Then delete any remaining guest items.
        """
        from datetime import datetime, timezone

        # Fetch all guest items
        guest_rows = await _select_items(db, None, session_id)

        for guest in guest_rows:
            existing = await _find_item(db, user_id, None, guest.sku_id)
            if existing is not None:
                # Conflict: take MAX quantity
                merged_qty = max(guest.quantity, existing.quantity)
                await db.execute(
                    update(CartItem)
                    .where(CartItem.id == existing.id)
                    .values(
                        quantity=merged_qty,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                # Delete the guest row
                await db.execute(
                    delete(CartItem).where(CartItem.id == guest.id)
                )
            else:
                # No conflict: move guest row to user
                await db.execute(
                    update(CartItem)
                    .where(CartItem.id == guest.id)
                    .values(
                        user_id=user_id,
                        session_id=None,
                        updated_at=datetime.now(timezone.utc),
                    )
                )

        # Delete any remaining guest items (shouldn't happen, but defensive)
        await db.execute(
            delete(CartItem).where(CartItem.session_id == session_id)
        )
        await db.commit()

        return await CartService.get_cart(
            db,
            user_id=user_id,
            session_id=None,
            b2b_base_url=b2b_base_url,
            service_key=service_key,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Internal DB helpers
# ──────────────────────────────────────────────────────────────────────────────


async def _select_items(
    db: AsyncSession,
    user_id: Optional[UUID],
    session_id: Optional[str],
) -> list[CartItem]:
    """Select all cart items for the given identity, ordered by created_at."""
    if user_id is not None:
        stmt = (
            select(CartItem)
            .where(CartItem.user_id == user_id)
            .order_by(CartItem.created_at.asc())
        )
    else:
        stmt = (
            select(CartItem)
            .where(CartItem.session_id == session_id)
            .order_by(CartItem.created_at.asc())
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _find_item(
    db: AsyncSession,
    user_id: Optional[UUID],
    session_id: Optional[str],
    sku_id: UUID,
) -> Optional[CartItem]:
    """Find a single cart item by identity + sku_id. None if not found."""
    if user_id is not None:
        stmt = select(CartItem).where(
            CartItem.user_id == user_id,
            CartItem.sku_id == sku_id,
        )
    else:
        stmt = select(CartItem).where(
            CartItem.session_id == session_id,
            CartItem.sku_id == sku_id,
        )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _build_identity_delete(
    user_id: Optional[UUID],
    session_id: Optional[str],
):
    """Build a DELETE statement for all items of the given identity."""
    if user_id is not None:
        return delete(CartItem).where(CartItem.user_id == user_id)
    return delete(CartItem).where(CartItem.session_id == session_id)
