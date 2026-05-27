"""
Banners router — US-B2C-14.

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET  /api/v1/catalog/banners  → array of Banner (public, no auth)

Canon: b2c-cart-flows.md#b2c-14-banners (not yet in spec)
  POST /api/v1/banner-events    → {accepted: N} (optional JWT)

Note on path: spec uses /api/v1/catalog/banners (not /api/v1/home/banners as in canon).
Spec > canon for all visible contract artifacts.

Note on response shape: spec returns a bare array of Banner objects.
Canon wraps in {items, total_count}. Spec wins — bare array.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import JWT_ALGORITHM, JWT_SECRET_KEY
from backend.database import get_db
from backend.modules.banners.schemas import (
    BannerEventsRequest,
    BannerEventsResponse,
    BannerResponse,
)
from backend.modules.banners.service import BannersService

router = APIRouter(prefix="/api/v1", tags=["Banners"])

# Optional bearer: does not raise if token absent (banners are public)
_optional_bearer = HTTPBearer(auto_error=False)


def _try_get_user_id(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[UUID]:
    """Extract user_id from JWT if present; return None otherwise."""
    if credentials is None:
        return None
    from jose import JWTError, jwt
    try:
        payload = jwt.decode(
            credentials.credentials, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        sub = payload.get("sub")
        return UUID(str(sub)) if sub else None
    except (JWTError, ValueError, AttributeError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/catalog/banners",
    response_model=List[BannerResponse],
    summary="Active banners for home page slider",
)
async def list_banners(db: AsyncSession = Depends(get_db)) -> List[BannerResponse]:
    """
    Public endpoint — no auth required.
    Returns banners filtered by is_active=True and schedule window,
    sorted by ordering ASC.

    Spec: b2c/openapi.yaml /api/v1/catalog/banners
    """
    return await BannersService.list_active_banners(db)


@router.post(
    "/banner-events",
    response_model=BannerEventsResponse,
    summary="Record banner impression/click events (CTR analytics)",
)
async def record_banner_events(
    body: BannerEventsRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> BannerEventsResponse:
    """
    Accepts a batch of impression/click events for CTR analytics.

    Works for both authenticated (JWT optional) and anonymous visitors.
    Returns 400 if any banner_id does not exist.
    Empty events array → 400 via Pydantic min_length=1 validation.

    Canon: b2c-cart-flows.md#b2c-14-banners (not yet in spec).
    """
    user_id = _try_get_user_id(credentials)

    try:
        accepted = await BannersService.record_events(
            db, events=body.events, user_id=user_id
        )
    except ValueError as exc:
        code = str(exc)
        if code.startswith("BANNER_NOT_FOUND:"):
            banner_id = code.split(":", 1)[1]
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BANNER_NOT_FOUND",
                    "message": f"Banner {banner_id} not found",
                },
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": code},
        )

    return BannerEventsResponse(accepted=accepted)
