"""
Favorites router — US-B2C-06.

Paths (spec b2c/openapi.yaml — source of truth per CLAUDE.md §3):
  PUT    /api/v1/favorites/{product_id} — add (idempotent, 204 always)
  DELETE /api/v1/favorites/{product_id} — remove (idempotent, 204 always)
  GET    /api/v1/favorites              — paginated + enriched from B2B

Contract notes:
  • user_id — ONLY from JWT claims (Bearer). NEVER from query or body (IDOR prevention).
  • PUT idempotency: ON CONFLICT DO NOTHING in DB. 204 regardless of first/repeat.
  • DELETE idempotency: 204 even if the row doesn't exist.
  • GET returns PaginatedCatalogProducts shape: {items, total_count, limit, offset}.
  • Auth: 401 UNAUTHORIZED on missing/invalid JWT.
  • B2B unavailable: 503 UPSTREAM_UNAVAILABLE.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from fastapi.responses import Response

from backend import config
from backend.auth import get_current_user_id
from backend.database import get_db
from backend.modules.favorites.schemas import FavoritesListResponse
from backend.modules.favorites.service import FavoritesService
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1", tags=["Favorites"])


def _upstream_error(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"code": "UPSTREAM_UNAVAILABLE", "message": detail},
    )


# ──────────────────────────────────────────────────────────────────────────────
# PUT /api/v1/favorites/{product_id}
# ──────────────────────────────────────────────────────────────────────────────

@router.put(
    "/favorites/{product_id}",
    summary="Add product to favorites (idempotent)",
    status_code=204,
    response_model=None,
    responses={
        204: {"description": "Added (or already in favorites — idempotent)"},
        401: {"description": "Unauthorized"},
    },
)
async def add_to_favorites(
    product_id: UUID = Path(..., description="Product UUID"),
    db: AsyncSession = Depends(get_db),
    user_id: UUID = Depends(get_current_user_id),
) -> Response:
    """
    PUT /api/v1/favorites/{product_id} — add to favorites (spec b2c/openapi.yaml:557-565).

    204 No Content always (first add and repeat are indistinguishable to the client).
    Idempotency guaranteed by ON CONFLICT DO NOTHING at DB level.
    user_id from JWT only — IDOR prevention.
    """
    await FavoritesService.add_favorite(
        db,
        user_id=user_id,
        product_id=product_id,
    )
    return Response(status_code=204)


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
