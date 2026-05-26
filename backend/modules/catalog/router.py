"""
Catalog router — B2C proxy for the public NeoMarket product listing.

Paths (spec b2c/openapi.yaml):
  GET /api/v1/catalog/products  — filtered/sorted product listing with full-text search
  GET /api/v1/catalog/facets    — facet counts for current filter set

Contract notes (CLAUDE.md §1 checklists):
  • sort enum strictly from spec: [price_asc, price_desc, popularity, new].
    Invalid value → 400 INVALID_REQUEST listing allowed values.
  • Search param `q` (spec field): min 3 chars, max 200 chars (spec maxLength).
    Violations → 400 INVALID_REQUEST (not 422) — canon b2c-catalog-flows.md#b2c-2-search.
    Special chars (%, _, ') are safe — B2B escapes them before SQL LIKE.
  • Filter params use deepObject style in spec (?filter[key]=val). FastAPI doesn't
    parse deepObject natively, so we parse them from request.query_params.
  • B2B unavailable → 502 {"code":"UPSTREAM_UNAVAILABLE","message":"..."}.
  • All endpoints are public (security: []) per spec.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from backend import config
from backend.modules.catalog.schemas import (
    ALLOWED_SORT_VALUES,
    BreadcrumbResponse,
    CatalogProductCard,
    CatalogProductDetail,
    CategoryRef,
    CategoryTreeNode,
    ErrorResponse,
    FacetsResponse,
    PaginatedCatalogProducts,
)
from backend.modules.catalog.service import CatalogService, OrphanNodeError

router = APIRouter(prefix="/api/v1", tags=["Catalog"])


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

_SEARCH_MIN_LEN = 3
_SEARCH_MAX_LEN = 200  # spec b2c/openapi.yaml q.maxLength


def _validate_q(q: Optional[str]) -> Optional[JSONResponse]:
    """
    Validate the search query string `q`.

    Returns a 400 JSONResponse if q violates length constraints,
    otherwise returns None (caller continues normally).

    canon b2c-catalog-flows.md#b2c-2-search edge cases:
      < 3 chars → 400 INVALID_REQUEST "Search query must be at least 3 characters"
      > 200 chars (spec maxLength) → 400 INVALID_REQUEST
    """
    if q is None:
        return None
    stripped = q.strip()
    if len(stripped) < _SEARCH_MIN_LEN:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "Search query must be at least 3 characters",
            },
        )
    if len(q) > _SEARCH_MAX_LEN:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": f"Search query must be at most {_SEARCH_MAX_LEN} characters",
            },
        )
    return None


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
        400: {"model": ErrorResponse, "description": "Невалидный параметр (sort / q)"},
        502: {"model": ErrorResponse, "description": "B2B недоступен"},
    },
    summary="Публичный листинг товаров с фильтрами и поиском (US-B2C-01/02)",
    description="""
    Прокси к B2B-каталогу (GET /api/v1/public/products) через X-Service-Key.
    Видимость: только MODERATED товары с active_quantity>0 — фильтрует B2B.

    Поиск (US-B2C-02):
    - `?q=текст` — полнотекстовый поиск по title/description (выполняется B2B).
    - Минимум 3 символа, максимум 200 (spec b2c/openapi.yaml).
    - Спецсимволы (%, _, ') безопасны — B2B экранирует перед SQL LIKE.
    - Пустой результат → 200 с items:[].

    Фильтры (deepObject): `?filter[price_min]=...&filter[category_id]=<uuid>`
    Сортировка: price_asc | price_desc | popularity | new

    Ошибки:
    - q < 3 символов → 400 INVALID_REQUEST
    - невалидный sort → 400 INVALID_REQUEST с перечислением допустимых
    - B2B недоступен → 502 UPSTREAM_UNAVAILABLE
    """,
)
async def list_catalog_products(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: Optional[str] = Query(None, description="Полнотекстовый поиск (мин. 3 символа)"),
    sort: str = Query("popularity"),
) -> PaginatedCatalogProducts | JSONResponse:
    """
    GET /api/v1/catalog/products — public catalog + search (US-B2C-01/02).

    Canon test scenarios:
    - catalog_returns_filtered_sorted_products
    - search_returns_matching_products
    - short_query_returns_400
    - special_chars_do_not_break_query
    - empty_results_returns_200
    - invalid_sort_returns_400
    - b2b_unavailable_returns_502
    """
    # Validate search query length (canon b2c-catalog-flows.md#b2c-2-search)
    q_err = _validate_q(q)
    if q_err is not None:
        return q_err

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
    q: Optional[str] = Query(None, description="Полнотекстовый поиск (мин. 3 символа)"),
) -> FacetsResponse | JSONResponse:
    """
    GET /api/v1/catalog/facets — facets for current filter set (US-B2C-01/02).

    Canon test scenarios:
    - facets_return_counts_per_filter_value
    - b2b_unavailable_returns_502
    """
    q_err = _validate_q(q)
    if q_err is not None:
        return q_err
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


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/products/{product_id}
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/products/{product_id}",
    response_model=CatalogProductDetail,
    responses={
        200: {"description": "Карточка товара"},
        404: {"model": ErrorResponse, "description": "Товар не найден / заблокирован / удалён"},
        502: {"model": ErrorResponse, "description": "B2B недоступен"},
    },
    summary="Карточка товара (US-B2C-03)",
    description="""
    Возвращает полную публичную карточку товара: описание, изображения,
    характеристики, список SKU с ценами и остатками.

    Прокси к B2B GET /api/v1/products/{id} (X-Service-Key).

    Поля cost_price и reserved_quantity ВСЕГДА отсутствуют в ответе —
    это внутренние данные продавца (ADR: отдельная Pydantic-схема на
    каждое представление, поля не передаются даже как null).

    Ценообразование (canon b2c-catalog-flows.md#b2c-3-product-card):
      price     = фактическая цена (base - discount), копейки
      old_price = base price, когда discount > 0 (зачёркнутая), иначе null

    Видимость:
      - deleted=true или status != MODERATED → 404
      - SKU с нулевым остатком показываются с in_stock=false
      - Товар с has_stock=false (все SKU нулевые) всё равно отдаётся 200

    Ошибки:
      404 NOT_FOUND    — товар отсутствует, удалён или заблокирован
      502 UPSTREAM_UNAVAILABLE — B2B недоступен
    """,
)
async def get_catalog_product(
    product_id: UUID = Path(..., description="UUID товара"),
) -> CatalogProductDetail | JSONResponse:
    """
    GET /api/v1/catalog/products/{product_id} — product card (US-B2C-03).

    Canon test scenarios:
    - product_card_returns_full_data_with_skus
    - cost_price_absent_in_response
    - blocked_product_returns_404
    - sku_without_stock_is_shown_as_unavailable
    - b2b_unavailable_returns_502
    """
    try:
        detail = await CatalogService.get_product(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            product_id=product_id,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "message": "Product not found"},
            )
        return _upstream_error(f"B2B returned {exc.response.status_code}")

    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "Product not found"},
        )

    return detail


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/products/{product_id}/similar
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/products/{product_id}/similar",
    response_model=list[CatalogProductCard],
    responses={
        200: {"description": "Список похожих товаров (может быть пустым)"},
        404: {"model": ErrorResponse, "description": "Товар не найден"},
        502: {"model": ErrorResponse, "description": "B2B недоступен"},
    },
    summary="Похожие товары (US-B2C-04)",
    description="""
    Возвращает массив похожих товаров из той же категории.
    Текущий товар исключён из результата.
    Нет похожих → пустой список [].

    Алгоритм (canon b2c-catalog-flows.md#b2c-4-similar-products):
      1. GET product из B2B → извлечь category_id (404 если не найден)
      2. GET /api/v1/public/products?category={category_id} → случайная выборка
      3. Исключить текущий товар из результата
      4. Вернуть до limit элементов

    Fallback на родительскую категорию не реализован в MVP
    (B2C не имеет эндпоинта для получения иерархии категорий).

    ADR (выборка): случайная (ORDER BY RANDOM() в B2B) vs характеристики vs кэш.
    Выбрана случайная: нет инфры, результат свежий при каждом запросе.

    Spec b2c/openapi.yaml: limit min=1 max=50, default=10.
    """,
)
async def get_similar_products(
    product_id: UUID = Path(..., description="UUID текущего товара"),
    limit: int = Query(10, ge=1, le=50, description="Максимальное число похожих"),
) -> list[CatalogProductCard] | JSONResponse:
    """
    GET /api/v1/catalog/products/{product_id}/similar — similar products (US-B2C-04).

    Canon test scenarios:
    - similar_returns_up_to_8_from_same_category
    - empty_category_returns_200_empty_list
    - unknown_product_returns_404
    - similar_excludes_current_product
    - b2b_unavailable_returns_502
    """
    try:
        result = await CatalogService.get_similar(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            product_id=product_id,
            limit=limit,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return JSONResponse(
                status_code=404,
                content={"code": "NOT_FOUND", "message": "Product not found"},
            )
        return _upstream_error(f"B2B returned {exc.response.status_code}")

    if result is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "Product not found"},
        )

    return result


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/categories  (US-B2C-05, spec b2c/openapi.yaml)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/categories",
    summary="Flat category list",
    response_model=list[CategoryRef],
    tags=["Catalog"],
)
async def list_categories() -> list[CategoryRef] | JSONResponse:
    """
    GET /api/v1/catalog/categories — flat list of all categories.
    spec b2c/openapi.yaml: response array of CategoryRef (no auth required).

    DoD (US-B2C-05):
    - category_tree_returns_nested_structure (tree endpoint)
    - orphan_node_returns_422

    B2B source: GET /api/v1/categories (read-open endpoint).
    """
    try:
        return await CatalogService.list_categories(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except OrphanNodeError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": "ORPHAN_NODE", "message": f"category hierarchy is broken: {exc}"},
        )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/categories/tree  (US-B2C-05, spec b2c/openapi.yaml)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/categories/tree",
    summary="Nested category tree",
    response_model=list[CategoryTreeNode],
    tags=["Catalog"],
)
async def get_category_tree() -> list[CategoryTreeNode] | JSONResponse:
    """
    GET /api/v1/catalog/categories/tree — full nested category tree.
    spec b2c/openapi.yaml: response array of CategoryTreeNode.

    DoD (US-B2C-05):
    - category_tree_returns_nested_structure
    - orphan_node_returns_422

    Algorithm: fetch flat list from B2B, detect orphan nodes, build tree in-memory.
    Cache-Control: max-age=3600 — categories change rarely.
    """
    try:
        tree = await CatalogService.get_category_tree(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except OrphanNodeError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": "ORPHAN_NODE", "message": f"category hierarchy is broken: {exc}"},
        )

    from fastapi.responses import Response
    import json

    # Return with Cache-Control header (categories change rarely)
    content = json.dumps([node.model_dump(mode="json") for node in tree])
    return Response(
        content=content,
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/categories/{category_id}  (US-B2C-05)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/categories/{category_id}",
    summary="Category detail with direct children",
    response_model=CategoryTreeNode,
    tags=["Catalog"],
)
async def get_category(
    category_id: UUID = Path(..., description="Category UUID"),
) -> CategoryTreeNode | JSONResponse:
    """
    GET /api/v1/catalog/categories/{category_id} — category detail + direct children.

    DoD (US-B2C-05):
    - unknown_category_returns_404

    B2B source: GET /api/v1/categories/{category_id} → CategoryWithChildrenResponse.
    """
    try:
        result = await CatalogService.get_category(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            category_id=category_id,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")

    if result is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "Category not found"},
        )
    return result


# ──────────────────────────────────────────────────────────────────────────────
# GET /api/v1/catalog/breadcrumbs  (US-B2C-05)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/catalog/breadcrumbs",
    summary="Navigation breadcrumb chain",
    response_model=BreadcrumbResponse,
    tags=["Catalog"],
)
async def get_breadcrumbs(
    category_id: Optional[UUID] = Query(
        None, description="UUID of the target category"
    ),
    product_id: Optional[UUID] = Query(
        None, description="UUID of the product (resolves its category)"
    ),
) -> BreadcrumbResponse | JSONResponse:
    """
    GET /api/v1/catalog/breadcrumbs?category_id=|product_id=

    Exactly ONE parameter required (canon b2c-catalog-flows.md#b2c-5-category-nav §5d).

    DoD (US-B2C-05):
    - breadcrumbs_return_path_from_root
    - ambiguous_params_returns_400  (both params supplied → 400)
    - orphan_node_returns_422        (broken hierarchy → 422)

    Error codes:
      400 INVALID_REQUEST  — both or neither param supplied.
      404 NOT_FOUND        — unknown category / product.
      422 ORPHAN_NODE      — category has no valid path to root.
      502 UPSTREAM_UNAVAILABLE — B2B network error.
    """
    # Validate: exactly one of category_id / product_id must be provided
    if category_id is not None and product_id is not None:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "only one of category_id or product_id must be provided",
            },
        )
    if category_id is None and product_id is None:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "category_id or product_id must be provided",
            },
        )

    try:
        result = await CatalogService.get_breadcrumbs(
            b2b_base_url=config.B2B_BASE_URL,
            service_key=config.B2C_TO_B2B_KEY,
            category_id=category_id,
            product_id=product_id,
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        return _upstream_error(f"B2B service unavailable: {exc}")
    except OrphanNodeError as exc:
        return JSONResponse(
            status_code=422,
            content={"code": "ORPHAN_NODE", "message": f"category hierarchy is broken: {exc}"},
        )

    if result is None:
        return JSONResponse(
            status_code=404,
            content={"code": "NOT_FOUND", "message": "Category or product not found"},
        )

    return result
