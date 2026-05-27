"""
Subscriptions router — US-B2C-07.

Spec: b2c/openapi.yaml (neomarket-protocols)
  POST   /api/v1/favorites/{product_id}/subscribe  → 201 SubscriptionResponse
  DELETE /api/v1/favorites/{product_id}/subscribe  → 204 No Content

Auth: Bearer JWT (user_id extracted from sub claim only — IDOR prevention).
Validation errors (invalid notify_on) → 400 INVALID_REQUEST via global handler.
"""
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.subscriptions.schemas import SubscribeRequest, SubscriptionResponse
from backend.modules.subscriptions.service import SubscriptionsService

router = APIRouter(prefix="/api/v1", tags=["Subscriptions"])


def _upstream_error(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": f"B2B catalog is not available: {exc}",
        },
    )


@router.post(
    "/favorites/{product_id}/subscribe",
    status_code=201,
    response_model=SubscriptionResponse,
)
async def subscribe(
    product_id: UUID,
    body: SubscribeRequest,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
):
    """
    Subscribe to product notifications.

    Returns 201 with SubscriptionResponse on success.
    Returns 400 if notify_on is empty or contains invalid values.
    Returns 404 if product does not exist in B2B catalog.
    Returns 409 if subscription already exists for this user+product pair.
    """
    try:
        result = await SubscriptionsService.subscribe(
            db,
            user_id=user_id,
            product_id=product_id,
            notify_on=body.notify_on,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "PRODUCT_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Product not found"},
            )
        if code == "DUPLICATE_SUBSCRIPTION":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DUPLICATE_SUBSCRIPTION",
                    "message": "Subscription already exists for this product",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": code},
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(exc)

    return result


@router.delete(
    "/favorites/{product_id}/subscribe",
    status_code=204,
)
async def unsubscribe(
    product_id: UUID,
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    Unsubscribe from product notifications (idempotent).

    Always returns 204 No Content, even if subscription does not exist.
    """
    await SubscriptionsService.unsubscribe(db, user_id=user_id, product_id=product_id)
    return Response(status_code=204)
