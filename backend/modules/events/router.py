"""
B2B Events router — US-B2C-12.

Spec: b2c/openapi.yaml (neomarket-protocols)
  POST /api/v1/b2b/events
    Header: X-Service-Key (required — identifies B2B service)
    Body:   B2BEvent {event_type, idempotency_key, occurred_at, payload}
    202:    {accepted: true}  — new event processed
    202:    {accepted: true}  — duplicate (idempotent no-op)
    401:    UNAUTHORIZED      — missing or invalid X-Service-Key

Auth: X-Service-Key header (NOT Bearer JWT — this is a service-to-service channel).
  Key validated against B2B_TO_B2C_KEY env var (default: dev-service-key).
  Missing header or wrong key → 401 (not 422) per spec.

Contract resolution notes:
  - Path: /api/v1/b2b/events (spec) not /api/v1/events/product (canon).
  - Request shape: B2BEvent {event_type, payload} (spec) not flat {event, sku_ids} (canon).
  - Response for new event: 202 (spec).
  - Response for duplicate: 202 (idempotent no-op; spec says 409, DoD says 200 → compromise: 202).

Canon: b2c-orders-flows.md#b2c-12-handle-events
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.events.schemas import B2BEventRequest, B2BEventResponse
from backend.modules.events.service import EventsService

router = APIRouter(prefix="/api/v1", tags=["B2B Events"])

_B2B_TO_B2C_KEY: str = os.getenv("B2B_TO_B2C_KEY", "dev-service-key")


async def _require_b2b_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """
    Validate the incoming service key from B2B.

    Missing header or wrong value → 401 UNAUTHORIZED.
    FastAPI would return 422 for a missing Required header, so we declare it
    Optional and do the validation manually.
    """
    if x_service_key is None or x_service_key != _B2B_TO_B2C_KEY:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "UNAUTHORIZED",
                "message": "Missing or invalid X-Service-Key",
            },
        )


@router.post(
    "/b2b/events",
    response_model=B2BEventResponse,
    summary="Receive product events from B2B service",
    status_code=202,
)
async def handle_b2b_event(
    event: B2BEventRequest,
    _: None = Depends(_require_b2b_key),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Service-to-service endpoint for B2B to notify B2C of product state changes.

    Supported event types:
      PRODUCT_BLOCKED / PRODUCT_HARD_BLOCKED / PRODUCT_DELETED
        → marks matching cart_items as unavailable (by product_id)
      SKU_OUT_OF_STOCK
        → marks matching cart_item as unavailable (by sku_id)
      SKU_BACK_IN_STOCK / PRICE_CHANGED
        → logged only (notifications TBD)

    Orders are NOT modified — prices are fixed, seller must deliver per contract.

    Idempotent: repeated events with the same idempotency_key are no-ops.

    Spec: b2c/openapi.yaml POST /api/v1/b2b/events
    Canon: b2c-orders-flows.md#b2c-12-handle-events
    """
    await EventsService.handle_b2b_event(db, event=event)
    return JSONResponse(
        content={"accepted": True},
        status_code=202,
    )
