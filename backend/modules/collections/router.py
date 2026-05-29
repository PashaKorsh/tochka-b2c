"""
Collections router — US-B2C-15.

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET /api/v1/catalog/collections
    → array of Collection (public, no auth, products:[] in list view)

Canon: b2c-cart-flows.md#b2c-15-collections (detail endpoint not in spec yet)
  GET /api/v1/catalog/collections/{collection_id}/products
    → CollectionProductsResponse {items, unavailable_ids, total_products}

Contract resolution notes:
  - Path: /api/v1/catalog/collections (spec) not /api/v1/main/collections (canon).
  - Field: Collection.name (spec) not Collection.title (canon).
  - List response: bare array [] (spec) not {collections, metadata} wrapper (canon).
  - Detail response: not in spec → follows canon shape with unavailable_ids.
"""
from typing import List
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.collections.schemas import (
    CollectionMeta,
    CollectionProductsResponse,
)
from backend.modules.collections.service import CollectionsService

router = APIRouter(prefix="/api/v1", tags=["Collections"])


def _upstream_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": f"B2B catalog is not available: {exc}",
        },
    )


@router.get(
    "/catalog/collections",
    response_model=List[CollectionMeta],
    summary="List active collections with products (home page)",
)
async def list_collections(
    db: AsyncSession = Depends(get_db),
) -> List[CollectionMeta]:
    """
    Public endpoint — no auth required.

    Returns active collections sorted by priority ASC.
    Each collection's products[] is populated with up to 10 items enriched from B2B
    (spec b2c/openapi.yaml#Collection required: [id, name, products]).

    Spec: b2c/openapi.yaml /api/v1/catalog/collections
    """
    try:
        return await CollectionsService.list_collections(db)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)


@router.get(
    "/catalog/collections/{collection_id}/products",
    response_model=CollectionProductsResponse,
    summary="Products of a collection, enriched from B2B",
)
async def get_collection_products(
    collection_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> CollectionProductsResponse:
    """
    Return products of a collection enriched with B2B data.

    Products missing from B2B (deleted/blocked) → unavailable_ids (not an error).
    All products unavailable → {items: [], unavailable_ids: [...]} — valid 200.

    Canon: b2c-cart-flows.md#b2c-15-collections (not yet in spec).
    """
    try:
        return await CollectionsService.get_collection_products(
            db,
            collection_id=collection_id,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        if str(exc) == "COLLECTION_NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Collection not found"},
            )
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_REQUEST", "message": str(exc)},
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)
