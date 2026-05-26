"""
Catalog router — B2C proxy for the public NeoMarket product listing.

Paths (spec b2c/openapi.yaml):
  GET /api/v1/catalog/products  — filtered/sorted product listing
  GET /api/v1/catalog/facets    — facet counts for current filter set

Contract notes (CLAUDE.md §1 checklists):
  • sort enum strictly from spec: [price_asc, price_desc, popularity, new].
    Invalid value → 400 INVALID_REQUEST listing allowed values.
  • Filter params use deepObject style in spec (?filter[key]=val). FastAPI doesn't
    parse deepObject natively, so we parse them from request.query_params.
  • B2B unavailable → 502 {"code":"UPSTREAM_UNAVAILABLE","message":"..."}.
  • All endpoints are public (security: []) per spec.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend import config
from backend.modules.catalog.schemas import (
    ALLOWED_SORT_VALUES,
    ErrorResponse,
    FacetsResponse,
    PaginatedCatalogProducts,
)
from backend.modules.catalog.service import CatalogService

router = APIRouter(prefix="/api/v1", tags=["Catalog"])


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _upstream_error(detail: str = "B2B service unavailable") -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"code": "UPSTREAM_UNAVAILABLE", "message": detail},
    )


def _parse_deep_object_filters(request: Request) -> dict:
    """
    Parse ?filter[key]=value query parameters (OpenAPI deepObject style).
    Returns a plain dict {key: value}. Duplicate keys: last value wins.
    """
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key.startswith("filter[") and key.endswith("]"):
            filter_key = key[7:-1]
            result[filter_key] = value
    return result


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/products
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/products",
    response_model=PaginatedCatalogProducts,
    responses={
        200: {"description": "Страница товаров"},
        400: {"model": ErrorResponse, "description": "Невалидный параметр (sort)"},
        502: {"model": ErrorResponse, "description": "B2B недоступен"},
    },
    summary="Публичный листинг товаров с фильтрами (US-B2C-01)",
    description="""
    Прокси к B2B-каталогу (GET /api/v1/public/products) через X-Service-Key.
    Видимость: только MODERATED товары с active_quantity>0 — фильтрует B2B.

    Фильтры передаются в deepObject-стиле:
    `?filter[price_min]=10000&filter[price_max]=50000&filter[category_id]=<uuid>`

    Сортировка (spec b2c/openapi.yaml): price_asc | price_desc | popularity | new

    Ошибки:
    - невалидный sort → 400 INVALID_REQUEST с перечислением допустимых (CLAUDE.md §5)
    - B2B недоступен → 502 UPSTREAM_UNAVAILABLE (CLAUDE.md §5)
    """,
)
async def list_catalog_products(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, max_length=200),
    sort: str = Query("popularity"),
) -> PaginatedCatalogProducts | JSONResponse:
    """
    GET /api/v1/catalog/products — public catalog (US-B2C-01).

    Canon test scenarios:
    - catalog_returns_filtered_sorted_products
    - invalid_sort_returns_400
    - b2b_unavailable_returns_502
    """
    # Validate sort
    if sort not in ALLOWED_SORT_VALUES:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": (
                    f"sort must be one of: {', '.join(ALLOWED_SORT_VALUES)}"
                ),
            },
        )

    # Parse deepObject filter params
    filters = _parse_deep_object_filters(request)
    filter_category_id: Optional[UUID] = None
    filter_price_min: Optional[int] = None
    filter_price_max: Optional[int] = None

    if "category_id" in filters:
        try:
            filter_category_id = UUID(filters["category_id"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[category_id] must be a UUID"},
            )
    if "price_min" in filters:
        try:
            filter_price_min = int(filters["price_min"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[price_min] must be an integer"},
            )
    if "price_max" in filters:
        try:
            filter_price_max = int(filters["price_max"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[price_max] must be an integer"},
            )

    try:
        result = await CatalogService.list_products(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            limit=limit,
            offset=offset,
            q=q,
            sort=sort,
            filter_category_id=filter_category_id,
            filter_price_min=filter_price_min,
            filter_price_max=filter_price_max,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except httpx.HTTPStatusError as exc:
        # Propagate B2B 4xx/5xx as 502 to shield B2C from internal details
        return _upstream_error(f"B2B returned {exc.response.status_code}")

    return result


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/facets
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/facets",
    response_model=FacetsResponse,
    responses={
        200: {"description": "Фасеты с подсчётами"},
        502: {"model": ErrorResponse, "description": "B2B недоступен"},
    },
    summary="Фасеты каталога (US-B2C-01)",
    description="""
    Возвращает фасеты — число товаров для каждого значения фильтра.
    Поддерживается фасет price_range (копейки): under_1000 / 1000_5000 / over_5000.

    Используйте те же deepObject-фильтры что и в /catalog/products:
    `?filter[category_id]=<uuid>&filter[price_min]=...`

    canon: b2c-catalog-flows.md#b2c-1-catalog-filters (facets response shape)
    ADR: in-memory GROUP BY поверх batch от B2B (≤1000 items), без кэша —
    консистентно, нет допнагрузки на схему. Переход на B2B facets-endpoint
    при росте каталога > 1000 видимых товаров.
    """,
)
async def get_catalog_facets(
    request: Request,
    q: Optional[str] = Query(None, max_length=200),
) -> FacetsResponse | JSONResponse:
    """
    GET /api/v1/catalog/facets — facets for current filter set (US-B2C-01).

    Canon test scenarios:
    - facets_return_counts_per_filter_value
    - b2b_unavailable_returns_502
    """
    filters = _parse_deep_object_filters(request)
    filter_category_id: Optional[UUID] = None
    filter_price_min: Optional[int] = None
    filter_price_max: Optional[int] = None

    if "category_id" in filters:
        try:
            filter_category_id = UUID(filters["category_id"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[category_id] must be a UUID"},
            )
    if "price_min" in filters:
        try:
            filter_price_min = int(filters["price_min"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[price_min] must be an integer"},
            )
    if "price_max" in filters:
        try:
            filter_price_max = int(filters["price_max"])
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"code": "INVALID_REQUEST", "message": "filter[price_max] must be an integer"},
            )

    try:
        result = await CatalogService.get_facets(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            filter_category_id=filter_category_id,
            filter_price_min=filter_price_min,
            filter_price_max=filter_price_max,
            q=q,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except httpx.HTTPStatusError as exc:
        return _upstream_error(f"B2B returned {exc.response.status_code}")

    return result
