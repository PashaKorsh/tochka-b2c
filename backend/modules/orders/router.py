"""
Orders router — US-B2C-09.

Spec: b2c/openapi.yaml (neomarket-protocols)
  POST /api/v1/orders
    Header: Idempotency-Key (required, UUID)
    Body:   OrderCreateRequest
    201:    OrderResponse (new order created)
    200:    OrderResponse (idempotency replay — same key, same body)
    409:    Error {code: RESERVE_FAILED, message: ...}
    503:    Error {code: UPSTREAM_UNAVAILABLE, message: ...}

Auth: Bearer JWT required (buyer_id from token sub claim).

Contract resolution notes:
  - Idempotency-Key is an HTTP HEADER (spec), not a body field (canon).
  - Status immediately PAID (mock payment — no real gateway in DoD).
  - Reserve via POST /api/v1/inventory/reserve (spec path, not /reserve).
"""
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.orders.schemas import OrderCreateRequest, OrderResponse
from backend.modules.orders.service import OrdersService

router = APIRouter(prefix="/api/v1", tags=["Orders"])


def _upstream_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": f"B2B service is not available: {exc}",
        },
    )


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
