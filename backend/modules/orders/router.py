"""
Orders router — US-B2C-09 (checkout) + US-B2C-10 (view) + US-B2C-11 (cancel)
              + US-B2C-13 (deliver / fulfill trigger).

Spec: b2c/openapi.yaml (neomarket-protocols)
  POST /api/v1/orders
    Header: Idempotency-Key (required, UUID)
    Body:   OrderCreateRequest
    201:    OrderResponse (new order created)
    200:    OrderResponse (idempotency replay)
    409:    Error {code: RESERVE_FAILED}
    503:    Error {code: UPSTREAM_UNAVAILABLE}

  GET /api/v1/orders
    Query:  limit, offset, status (optional filter)
    200:    PaginatedOrders {items, total_count, limit, offset}

  GET /api/v1/orders/{order_id}
    200:    OrderResponse
    404:    Error {code: ORDER_NOT_FOUND}

  POST /api/v1/orders/{order_id}/cancel
    Body:   {reason?: string}  (optional)
    200:    OrderResponse (status=CANCELLED or CANCEL_PENDING)
    404:    Error {code: ORDER_NOT_FOUND}   (also for wrong-user IDOR)
    409:    Error {code: CANCEL_NOT_ALLOWED, details: {current_status}}

  POST /api/v1/orders/{order_id}/deliver   (US-B2C-13, admin/service endpoint)
    Header: X-Service-Key (required)
    200:    OrderResponse (status=DELIVERED)
    404:    Error {code: ORDER_NOT_FOUND}
    409:    Error {code: DELIVER_NOT_ALLOWED, details: {current_status}}

Auth: Bearer JWT required on buyer-facing endpoints.
      X-Service-Key required on admin/service endpoints (deliver).

IDOR rule (canon b2c-orders-flows.md#b2c-10-view-orders, #b2c-11-cancel-order):
  Wrong-user order -> 404, never 403.
  Returning 403 would reveal that the order exists, enabling UUID enumeration.
"""
import os
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.orders.schemas import (
    OrderCreateRequest,
    OrderResponse,
    OrderStatus,
    PaginatedOrdersResponse,
)
from backend.modules.orders.service import OrdersService

_ADMIN_SERVICE_KEY = os.getenv("B2C_ADMIN_KEY", "dev-service-key")


class CancelRequest(BaseModel):
    """Optional body for POST /api/v1/orders/{id}/cancel."""
    reason: Optional[str] = None


async def _require_admin_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """Dependency: validate X-Service-Key for internal/admin endpoints."""
    if x_service_key is None or x_service_key != _ADMIN_SERVICE_KEY:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Missing or invalid X-Service-Key",
            },
        )


router = APIRouter(prefix="/api/v1", tags=["Orders"])


def _upstream_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": f"B2B service is not available: {exc}",
        },
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/orders  (US-B2C-09)
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/orders",
    response_model=OrderResponse,
    summary="Create order (checkout)",
    status_code=201,
)
async def create_order(
    payload: OrderCreateRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        description="UUID idempotency key — repeating the same key replays the response",
    ),
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    Checkout: create an order from cart items.

    All-or-nothing: either ALL items are reserved or the request fails with 409.
    Prices are fixed at checkout time (snapshot from B2B at request time).
    Payment is mocked — status is immediately PAID.

    Sending the same Idempotency-Key twice returns the original response (200).
    """
    try:
        order_resp, is_new = await OrdersService.create_order(
            db,
            buyer_id=buyer_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "ADDRESS_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ADDRESS_NOT_FOUND",
                    "message": "Address not found or does not belong to this buyer",
                },
            )
        if msg == "CART_EMPTY":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CART_EMPTY",
                    "message": "Cart is empty — add items before checking out",
                },
            )
        if msg == "CART_HAS_UNAVAILABLE_ITEMS":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CART_HAS_UNAVAILABLE_ITEMS",
                    "message": "One or more cart items are unavailable. Please validate cart before checkout.",
                },
            )
        if msg.startswith("SKU_NOT_FOUND:"):
            sku_id = msg.split(":", 1)[1]
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": f"SKU not found or unavailable: {sku_id}",
                },
            )
        if msg.startswith("RESERVE_FAILED:"):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RESERVE_FAILED",
                    "message": "Could not reserve one or more items — insufficient stock",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": msg},
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)

    status_code = 201 if is_new else 200
    return JSONResponse(
        content=order_resp.model_dump(mode="json"),
        status_code=status_code,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/orders  (US-B2C-10)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/orders",
    response_model=PaginatedOrdersResponse,
    summary="List buyer's own orders with pagination",
)
async def list_orders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(
        default=None,
        description="Filter by order status",
        enum=[s.value for s in OrderStatus],
    ),
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> PaginatedOrdersResponse:
    """
    Return paginated history of orders belonging to the authenticated buyer.

    buyer_id is extracted from the JWT — never from query params (IDOR-safe).
    Sorted by created_at DESC (most recent first).

    Spec: b2c/openapi.yaml GET /api/v1/orders -> PaginatedOrders
    Canon: b2c-orders-flows.md#b2c-10-view-orders
    """
    return await OrdersService.list_orders(
        db,
        buyer_id=buyer_id,
        status=status,
        limit=limit,
        offset=offset,
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/orders/{order_id}  (US-B2C-10)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/orders/{order_id}",
    response_model=OrderResponse,
    summary="Order detail with fixed prices",
)
async def get_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> OrderResponse:
    """
    Return full order detail for the authenticated buyer.

    Prices come from OrderItem.unit_price (fixed at checkout) — not from B2B.
    A seller changing a SKU price after checkout does NOT affect this response.

    IDOR: if the order exists but belongs to a different buyer, returns 404
    (not 403) — preventing an attacker from inferring order existence by UUID.

    Spec: b2c/openapi.yaml GET /api/v1/orders/{order_id}
    Canon: b2c-orders-flows.md#b2c-10-view-orders §Authorization
    """
    try:
        return await OrdersService.get_order(
            db,
            order_id=order_id,
            buyer_id=buyer_id,
        )
    except ValueError as exc:
        if str(exc) == "ORDER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ORDER_NOT_FOUND",
                    "message": "Order not found",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/orders/{order_id}/cancel  (US-B2C-11)
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/orders/{order_id}/cancel",
    response_model=OrderResponse,
    summary="Cancel an order (CREATED or PAID only)",
)
async def cancel_order(
    order_id: UUID,
    body: CancelRequest = CancelRequest(),
    db: AsyncSession = Depends(get_db),
    buyer_id: UUID = Depends(get_current_user_id),
) -> OrderResponse:
    """
    Cancel an order and release its stock reservation in B2B.

    Cancellable statuses: CREATED, PAID.
    Other statuses (ASSEMBLING, DELIVERING, DELIVERED, CANCEL_PENDING, CANCELLED)
    → 409 CANCEL_NOT_ALLOWED with details.current_status.

    If B2B unreserve succeeds → status becomes CANCELLED.
    If B2B unreserve fails (network error) → status becomes CANCEL_PENDING.
    In both cases the response is 200 (the cancellation intent is accepted).

    IDOR: wrong-user order → 404 ORDER_NOT_FOUND (not 403).

    Spec: b2c/openapi.yaml POST /api/v1/orders/{order_id}/cancel
    Canon: b2c-orders-flows.md#b2c-11-cancel-order
    """
    try:
        return await OrdersService.cancel_order(
            db,
            order_id=order_id,
            buyer_id=buyer_id,
            reason=body.reason,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "ORDER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ORDER_NOT_FOUND",
                    "message": "Order not found",
                },
            )
        if msg.startswith("CANCEL_NOT_ALLOWED:"):
            current_status = msg.split(":", 1)[1]
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANCEL_NOT_ALLOWED",
                    "message": f"Cannot cancel order in status {current_status}",
                    "details": {"current_status": current_status},
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": msg},
        )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/orders/{order_id}/deliver  (US-B2C-13)
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/orders/{order_id}/deliver",
    response_model=OrderResponse,
    summary="Mark order DELIVERED and trigger B2B inventory/fulfill (admin)",
)
async def deliver_order(
    order_id: UUID,
    _: None = Depends(_require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    """
    Admin/service endpoint: called by the logistics system when delivery is confirmed.

    Transitions the order to DELIVERED and calls POST /api/v1/inventory/fulfill in B2B
    to deduct reserved_quantity from the warehouse.

    Idempotent: if the order is already DELIVERED and fulfill was acknowledged,
    returns the order immediately without calling B2B again.

    On B2B fulfill failure: order stays DELIVERED, error is logged.
    Retry eligibility: orders WHERE status='DELIVERED' AND fulfill_completed_at IS NULL.
    A Celery worker should pick these up (scaffold — not implemented in first iteration).

    Auth: X-Service-Key (not buyer JWT — this is an internal/admin operation).

    Spec: b2c/openapi.yaml POST /api/v1/orders/{order_id}/deliver
    Canon: b2c-orders-flows.md#b2c-13-fulfill
    """
    try:
        return await OrdersService.deliver_order(
            db,
            order_id=order_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg == "ORDER_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "ORDER_NOT_FOUND",
                    "message": "Order not found",
                },
            )
        if msg.startswith("DELIVER_NOT_ALLOWED:"):
            current_status = msg.split(":", 1)[1]
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DELIVER_NOT_ALLOWED",
                    "message": f"Cannot deliver order in status {current_status}",
                    "details": {"current_status": current_status},
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": msg},
        )
