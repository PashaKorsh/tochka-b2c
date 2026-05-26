"""
Favorites router — US-B2C-06.

Paths (canon b2c-cart-flows.md#b2c-6-favorites, spec b2c/openapi.yaml):
  POST   /api/v1/favorites/{product_id} — add (idempotent, 201 first / 200 repeat)
  DELETE /api/v1/favorites/{product_id} — remove (idempotent, 204 always)
  GET    /api/v1/favorites              — paginated + enriched from B2B

Contract notes:
  • user_id — ONLY from JWT claims (Bearer). NEVER from query or body.
    If user_id appears in query params → ignored (IDOR prevention).
    CLAUDE.md §5, canon b2c-cart-flows.md §1.
  • POST idempotency: ON CONFLICT DO NOTHING in DB. 201 = created, 200 = already exists.
  • DELETE idempotency: 204 even if the row doesn't exist.
  • GET enriches via B2B batch: unavailable products silently excluded.
  • Auth: 401 UNAUTHORIZED on missing/invalid JWT.
  • B2B unavailable: 503 UPSTREAM_UNAVAILABLE (canon uses 503 for cart/favorites
    vs 502 for catalog; following canon distinction here).

ADR — user identification (in PR description):
  See backend/auth.py.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse, Response

from backend import config
from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.favorites.schemas import (
    FavoriteMutationResponse,
    FavoritesListResponse,
)
from backend.modules.favorites.service import FavoritesService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1", tags=["Favorites"])


def _upstream_error(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"code": "UPSTREAM_UNAVAILABLE", "message": detail},
    )


# ──────────────────────────────────────────────────────────────────────────────
# POST /api/v1/favorites/{product_id}
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/favorites/{product_id}",
    summary="Add product to favorites (idempotent)",
    status_code=201,
    response_model=None,
    responses={
        201: {"description": "Added for the first time"},
        200: {"description": "Already in favorites (idempotent)"},
        401: {"description": "Unauthorized"},
    },
)
async def add_to_favorites(
    product_id: UUID = Path(..., description="Product UUID"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> JSONResponse:
    """
    POST /api/v1/favorites/{product_id} — add to favorites.

    DoD (US-B2C-06):
    - add_to_favorites_returns_201        — first add → 201
    - repeat_add_returns_200_not_duplicate — repeat → 200, no DB duplicate

    user_id extracted from JWT claims only (IDOR prevention).
    Any user_id in query params is silently ignored.
    """
    response, created = await FavoritesService.add_favorite(
        db,
        user_id=user_id,
        product_id=product_id,
    )
    status_code = 201 if created else 200
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# DELETE /api/v1/favorites/{product_id}
# ──────────────────────────────────────────────────────────────────────────────

@router.delete(
    "/favorites/{product_id}",
    summary="Remove product from favorites (idempotent)",
    status_code=204,
    responses={
        204: {"description": "Removed (or was not in favorites)"},
        401: {"description": "Unauthorized"},
    },
)
async def remove_from_favorites(
    product_id: UUID = Path(..., description="Product UUID"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    DELETE /api/v1/favorites/{product_id} — remove from favorites.
    Idempotent: 204 even if the product was not in the list.

    user_id extracted from JWT claims only.
    """
    await FavoritesService.remove_favorite(
        db,
        user_id=user_id,
        product_id=product_id,
    )
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/favorites
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/favorites",
    summary="Get favorites list (enriched from B2B)",
    response_model=FavoritesListResponse,
    response_model_exclude_none=True,
    responses={
        200: {"description": "Favorites list with enriched product data"},
        401: {"description": "Unauthorized"},
        503: {"description": "B2B service unavailable"},
    },
)
async def get_favorites(
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    offset: int = Query(0, ge=0, description="Page offset"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> FavoritesListResponse | JSONResponse:
    """
    GET /api/v1/favorites — paginated favorites enriched with B2B product data.

    DoD (US-B2C-06):
    - get_favorites_enriched_from_b2b   — products come from B2B batch call
    - blocked_product_excluded_from_list — unavailable in B2B → silently excluded

    Algorithm: select product_ids from DB → POST /api/v1/public/products/batch
    → map to CatalogProductCard → build FavoriteItem[].
    Products absent from B2B response (deleted/blocked) are silently excluded.

    user_id extracted from JWT claims only (IDOR prevention).
    """
    try:
        result = await FavoritesService.list_favorites(
            db,
            user_id=user_id,
            limit=limit,
            offset=offset,
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")

    return result
