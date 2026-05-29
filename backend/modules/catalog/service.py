"""
CatalogService — proxy layer between B2C and B2B public catalog.

Canon b2c-catalog-flows.md#b2c-1-catalog-filters:
  - B2C does NOT store products; all product data comes from B2B.
  - Visibility filter (status=MODERATED, deleted=false, active_quantity>0) is
    enforced by B2B — B2C just proxies the request with the right params.
  - B2B unavailable → 502 Bad Gateway (CLAUDE.md §5 "Недоступность апстрима").

Facets strategy (ADR in PR description):
  SQL GROUP BY on every request was chosen over TTL cache or denormalised counters
  because the catalog MVP has no writes on the B2C side. Since B2C doesn't store
  product data, facets are computed in-memory over a B2B result batch (≤ 1000 items).
  This is consistent and requires zero schema additions. For scale, upgrade to a
  dedicated B2B facets endpoint or a Redis TTL cache later.

  Price-range buckets are computed from the B2B short-response `min_price` field
  (the only numeric attribute available without fetching full product cards).
"""
from __future__ import annotations

import random
import uuid
from typing import Any, Optional
from uuid import UUID

import httpx

from backend.modules.catalog.schemas import (
    ALLOWED_SORT_VALUES,
    B2B_SORT_MAP,
    BreadcrumbItem,
    BreadcrumbMeta,
    BreadcrumbResponse,
    CatalogProductCard,
    CatalogProductDetail,
    CatalogSkuImageRef,
    CatalogSkuResponse,
    CategoryRef,
    CategoryTreeNode,
    CharacteristicRef,
    Facet,
    FacetValue,
    FacetsResponse,
    ImageRef,
    PaginatedCatalogProducts,
    SellerRef,
)


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_image_ref(cover_image_url: Optional[str]) -> list[ImageRef]:
    """Convert a B2B cover_image URL to a one-element ImageRef list."""
    if not cover_image_url:
        return []
    return [
        ImageRef(
            id=uuid.uuid5(uuid.NAMESPACE_URL, cover_image_url),
            url=cover_image_url,
            ordering=0,
            is_main=True,
        )
    ]


def _b2b_item_to_card(item: dict[str, Any]) -> CatalogProductCard:
    """Transform a B2B ProductPublicShortResponse dict into a CatalogProductCard.

    category: B2B short response only provides category_id (UUID). We build a
    partial CategoryRef with id=category_id and empty name/path — the detail
    endpoint does a full category fetch to populate those fields.

    has_stock: derived from B2B data. B2B only returns products with
    active_quantity > 0 (filtered before response), so any item with min_price > 0
    is in stock. Falls back gracefully if B2B adds explicit has_stock / in_stock.
    """
    cat_id = item.get("category_id")
    category = (
        CategoryRef(id=UUID(cat_id), name="", level=0, path=[]) if cat_id else None
    )

    has_stock = bool(
        item.get("has_stock",
        item.get("in_stock",
        (item.get("min_price") or 0) > 0))
    )

    return CatalogProductCard(
        id=UUID(item["id"]),
        name=item["title"],
        slug=item.get("slug"),
        category=category,
        min_price=item["min_price"],
        has_stock=has_stock,
        images=_make_image_ref(item.get("cover_image")),
        seller=None,   # seller info not in B2B short response
    )


# Price-range buckets for facets (kopecks)
_PRICE_BUCKETS: list[tuple[str, int, int]] = [
    ("under_1000", 0, 1_000_00),        # < 1 000 ₽
    ("1000_5000", 1_000_00, 5_000_00),  # 1 000 – 5 000 ₽
    ("over_5000", 5_000_00, 10**15),    # > 5 000 ₽
]


def _compute_facets(
    items: list[dict[str, Any]],
    category_id: Optional[UUID],
) -> FacetsResponse:
    """
    Build a FacetsResponse from a flat list of B2B ProductPublicShortResponse dicts.

    Only 'price_range' facet is implemented — it's the only attribute available
    in the B2B short response. Characteristic-based facets (brand, color, etc.)
    require a richer B2B endpoint and are tracked in neomarket-protocols as a
    future PR.
    """
    bucket_counts: dict[str, int] = {label: 0 for label, _, _ in _PRICE_BUCKETS}
    for item in items:
        price = item.get("min_price", 0) or 0
        for label, lo, hi in _PRICE_BUCKETS:
            if lo <= price < hi:
                bucket_counts[label] += 1
                break

    price_facet = Facet(
        name="price_range",
        values=[
            FacetValue(value=label, count=cnt)
            for label, cnt in bucket_counts.items()
            if cnt > 0
        ],
    )
    return FacetsResponse(
        category_id=category_id,
        facets=[price_facet] if price_facet.values else [],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public service
# ──────────────────────────────────────────────────────────────────────────────

class CatalogService:
    """
    Static-method service matching the B2B-pattern from CLAUDE.md §8.
    All network calls are made with httpx.AsyncClient.
    ConnectError / timeout → caller catches and raises 502.
    """

    @staticmethod
    async def list_products(
        *,
        b2b_base_url: str,
        service_key: str,
        limit: int,
        offset: int,
        q: Optional[str],
        sort: str,
        filter_category_id: Optional[UUID],
        filter_price_min: Optional[int],
        filter_price_max: Optional[int],
    ) -> PaginatedCatalogProducts:
        """
        Proxy GET /api/v1/catalog/products → B2B GET /api/v1/public/products.

        Raises:
          httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
        """
        b2b_sort = B2B_SORT_MAP.get(sort, "date_desc")
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": b2b_sort,
        }
        if q:
            params["search"] = q
        if filter_category_id:
            params["category"] = str(filter_category_id)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{b2b_base_url}/api/v1/public/products",
                params=params,
                headers={"X-Service-Key": service_key},
            )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])

        # Optional client-side price filter (B2B doesn't support price range in /public/products)
        if filter_price_min is not None:
            items = [i for i in items if (i.get("min_price") or 0) >= filter_price_min]
        if filter_price_max is not None:
            items = [i for i in items if (i.get("min_price") or 0) <= filter_price_max]

        cards = [_b2b_item_to_card(i) for i in items]
        return PaginatedCatalogProducts(
            items=cards,
            total_count=data.get("total_count", len(cards)),
            limit=data.get("limit", limit),
            offset=data.get("offset", offset),
        )

    @staticmethod
    async def get_facets(
        *,
        b2b_base_url: str,
        service_key: str,
        filter_category_id: Optional[UUID],
        filter_price_min: Optional[int],
        filter_price_max: Optional[int],
        q: Optional[str],
    ) -> FacetsResponse:
        """
        Compute product-count facets by fetching a broad batch from B2B and
        grouping in Python.

        ADR — SQL GROUP BY on every request (see module docstring).

        Raises:
          httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
        """
        params: dict[str, Any] = {"limit": 1000, "offset": 0, "sort": "date_desc"}
        if filter_category_id:
            params["category"] = str(filter_category_id)
        if q:
            params["search"] = q

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{b2b_base_url}/api/v1/public/products",
                params=params,
                headers={"X-Service-Key": service_key},
            )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])

        # Apply price filter post-fetch (same as list_products)
        if filter_price_min is not None:
            items = [i for i in items if (i.get("min_price") or 0) >= filter_price_min]
        if filter_price_max is not None:
            items = [i for i in items if (i.get("min_price") or 0) <= filter_price_max]

        return _compute_facets(items, filter_category_id)


# ──────────────────────────────────────────────────────────────────────────────
# Product detail helpers
# ──────────────────────────────────────────────────────────────────────────────

def _b2b_sku_to_catalog(sku: dict[str, Any]) -> CatalogSkuResponse:
    """
    Transform a B2B SKUPublicResponse dict into a buyer-safe CatalogSkuResponse.

    Security (CLAUDE.md §5, US-B2C-03):
      B2B's service-mode response already omits cost_price / reserved_quantity.
      The explicit CatalogSkuResponse schema guarantees they cannot appear even
      if B2B accidentally adds them (extra fields are dropped by Pydantic).

    Price convention (canon b2c-catalog-flows.md#b2c-3-product-card):
      B2B.price    = base / list price
      B2B.discount = discount amount in kopecks
      B2C.price    = B2B.price - B2B.discount   (actual selling price)
      B2C.old_price = B2B.price when discount > 0, else None (strikethrough)
    """
    base_price: int = sku.get("price", 0)
    discount: int = sku.get("discount", 0)
    effective_price = base_price - discount
    old_price: Optional[int] = base_price if discount > 0 else None

    available_qty: int = sku.get("active_quantity", 0)

    raw_images = sku.get("images", [])
    images = [
        CatalogSkuImageRef(
            id=img["id"],
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
    ]

    raw_chars = sku.get("characteristics", [])
    characteristics = [
        CharacteristicRef(name=c["name"], value=c["value"]) for c in raw_chars
    ]

    return CatalogSkuResponse(
        id=UUID(sku["id"]),
        name=sku.get("name"),
        sku_code=sku.get("article"),
        price=effective_price,
        old_price=old_price,
        available_quantity=available_qty,
        in_stock=available_qty > 0,
        images=images,
        characteristics=characteristics,
    )


def _b2b_product_to_detail(data: dict[str, Any]) -> CatalogProductDetail:
    """
    Transform a B2B ProductPublicResponse dict into a CatalogProductDetail.

    Visibility: caller must check data["status"] == "MODERATED" and
    data["deleted"] == False before calling this; otherwise return 404.
    """
    raw_images = data.get("images", [])
    images = [
        ImageRef(
            id=uuid.uuid5(uuid.NAMESPACE_URL, img["url"]),
            url=img["url"],
            ordering=img.get("ordering", 0),
        )
        for img in raw_images
    ]

    skus = [_b2b_sku_to_catalog(s) for s in data.get("skus", [])]
    has_stock = any(s.available_quantity > 0 for s in skus)
    min_price = min((s.price for s in skus), default=0)

    raw_chars = data.get("characteristics", [])
    characteristics = [CharacteristicRef(name=c["name"], value=c["value"]) for c in raw_chars]

    cat_id = data.get("category_id")
    category = (
        CategoryRef(id=UUID(cat_id), name="", level=0, path=[]) if cat_id else None
    )

    seller_id = data.get("seller_id")
    seller = SellerRef(id=UUID(seller_id), display_name="") if seller_id else None

    return CatalogProductDetail(
        id=UUID(data["id"]),
        name=data["title"],
        slug=data.get("slug"),
        category=category,
        min_price=min_price,
        has_stock=has_stock,
        images=images,
        seller=seller,
        description=data.get("description", ""),
        characteristics=characteristics,
        skus=skus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# get_product method (added to CatalogService class above)
# ──────────────────────────────────────────────────────────────────────────────

# Extend the class by monkey-patching in a classmethod-safe way would require
# re-opening the class. Instead we add the method directly here and the router
# calls it as CatalogService.get_product(...).

async def _get_product(
    *,
    b2b_base_url: str,
    service_key: str,
    product_id: UUID,
) -> CatalogProductDetail | None:
    """
    Fetch a single product from B2B and return a buyer-safe CatalogProductDetail.

    Returns None when:
      - B2B returns 404 (product not found, deleted, or blocked)
      - Product status != MODERATED or deleted == True (extra guard)

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/products/{product_id}",
            headers={"X-Service-Key": service_key},
        )

    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    data = resp.json()

    # Extra visibility guard: B2B service mode should already exclude
    # blocked/deleted, but we enforce it here to prevent data leaks.
    if data.get("deleted") or data.get("status") != "MODERATED":
        return None

    return _b2b_product_to_detail(data)


# Attach as a static method on CatalogService
CatalogService.get_product = staticmethod(_get_product)  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────────────
# Similar products (US-B2C-04)
# ──────────────────────────────────────────────────────────────────────────────

async def _get_similar(
    *,
    b2b_base_url: str,
    service_key: str,
    product_id: UUID,
    limit: int = 10,
) -> list[CatalogProductCard] | None:
    """
    Return up to `limit` public products from the same category as `product_id`,
    excluding the product itself.  Results are randomly shuffled so each page-load
    shows variety without relying on B2B ordering.

    Algorithm (canon b2c-catalog-flows.md#b2c-4-similar-products):
      1. Fetch the target product from B2B → validate visibility, get category_id.
         Returns None if product is unknown / deleted / blocked (→ 404).
      2. Fetch a wider batch (max(limit*3, 30) items) from B2B in the same category.
         Requesting a larger batch compensates for self-exclusion and gives enough
         variety for the random shuffle.
      3. Exclude the target product from the result.
      4. If len(similar) < limit → try parent-category fallback (canon §4 step 3):
           a. GET /api/v1/categories/{category_id} → resolve parent_id.
           b. Fetch up to max(limit*3, 30) products from parent category.
           c. Merge (deduplicated) into `similar`.
         Fallback failures (network, no parent_id) are swallowed — return whatever
         we have from the primary category rather than erroring out.
      5. Shuffle `similar` in-place for variety, cap at `limit`, convert and return.

    ADR (similar products algorithm) — three approaches considered:
      (a) B2B random sort: B2B's /api/v1/public/products has no `sort=random` param.
          Not available without B2B changes.
      (b) Random offset before fetch: requires a separate total-count call to compute
          a safe offset range → 2 extra B2B requests per similar-block load.
      (c) Wider fetch + B2C shuffle (chosen): fetch limit*3 items, shuffle on B2C.
          One extra HTTP call vs the minimal case; gives variety for typical category
          sizes (>10 visible products). For sparse categories (<limit visible), the
          parent-category fallback adds items from a broader pool.
    Criteria: (1) no B2B changes; (2) one extra B2B call at most (vs 2 for option b);
    (3) random.shuffle is deterministically seedable in tests.

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502 (step 1 & 2 only;
      fallback swallows network errors to stay non-blocking).
    """
    headers = {"X-Service-Key": service_key}

    # Step 1 — verify product exists and get its category_id
    async with httpx.AsyncClient(timeout=10.0) as client:
        product_resp = await client.get(
            f"{b2b_base_url}/api/v1/products/{product_id}",
            headers=headers,
        )

    if product_resp.status_code == 404:
        return None
    product_resp.raise_for_status()

    product_data = product_resp.json()
    if product_data.get("deleted") or product_data.get("status") != "MODERATED":
        return None

    category_id: str = product_data.get("category_id", "")

    # Step 2 — fetch a wider batch for variety + self-exclusion headroom
    fetch_count = max(limit * 3, 30)
    params: dict[str, Any] = {
        "limit": fetch_count,
        "offset": 0,
        "sort": "date_desc",
    }
    if category_id:
        params["category"] = category_id

    async with httpx.AsyncClient(timeout=10.0) as client:
        catalog_resp = await client.get(
            f"{b2b_base_url}/api/v1/public/products",
            params=params,
            headers=headers,
        )
    catalog_resp.raise_for_status()

    items = catalog_resp.json().get("items", [])

    # Step 3 — exclude current product
    similar = [i for i in items if i["id"] != str(product_id)]

    # Step 4 — parent-category fallback when primary category is sparse
    if len(similar) < limit and category_id:
        parent_id: str | None = None
        try:
            async with httpx.AsyncClient(timeout=5.0) as cat_client:
                cat_resp = await cat_client.get(
                    f"{b2b_base_url}/api/v1/categories/{category_id}",
                    headers=headers,
                )
            if cat_resp.status_code == 200:
                parent_id = cat_resp.json().get("parent_id")
        except (httpx.ConnectError, httpx.TimeoutException):
            pass  # fallback skipped gracefully

        if parent_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as parent_client:
                    parent_resp = await parent_client.get(
                        f"{b2b_base_url}/api/v1/public/products",
                        params={
                            "limit": fetch_count,
                            "offset": 0,
                            "sort": "date_desc",
                            "category": parent_id,
                        },
                        headers=headers,
                    )
                parent_resp.raise_for_status()
                parent_items = parent_resp.json().get("items", [])

                # Merge: add parent items not already present and not the target product
                existing_ids = {i["id"] for i in similar} | {str(product_id)}
                for item in parent_items:
                    if item["id"] not in existing_ids:
                        similar.append(item)
                        existing_ids.add(item["id"])
            except (httpx.ConnectError, httpx.TimeoutException):
                pass  # return primary results if parent fetch fails

    # Step 5 — shuffle for variety, cap at limit
    random.shuffle(similar)
    return [_b2b_item_to_card(i) for i in similar[:limit]]


CatalogService.get_similar = staticmethod(_get_similar)  # type: ignore[attr-defined]


# ──────────────────────────────────────────────────────────────────────────────
# Category navigation helpers (US-B2C-05)
# ──────────────────────────────────────────────────────────────────────────────

class OrphanNodeError(Exception):
    """Raised when a category references a parent_id that doesn't exist."""


def _b2b_cat_to_ref(item: dict[str, Any]) -> CategoryRef:
    """
    Convert a B2B CategoryResponse dict → B2C CategoryRef.

    B2B path is a materialized path string ("electronics/phones").
    B2C path is an array of strings — we split on "/".
    """
    raw_path: str = item.get("path") or ""
    path_parts = [p for p in raw_path.split("/") if p]
    return CategoryRef(
        id=UUID(item["id"]),
        name=item["name"],
        parent_id=UUID(item["parent_id"]) if item.get("parent_id") else None,
        level=item.get("level", 0),
        path=path_parts,
    )


def _check_orphans(flat: list[dict[str, Any]]) -> None:
    """
    Raise OrphanNodeError if any category references a parent_id that is not
    in the flat list.

    Algorithm: adjacency-list orphan detection (O(n)):
      1. Collect all IDs into a set.
      2. For each item, if parent_id is non-null and not in the set → orphan.
    """
    id_set = {item["id"] for item in flat}
    for item in flat:
        parent_id = item.get("parent_id")
        if parent_id and parent_id not in id_set:
            raise OrphanNodeError(
                f"category {item['id']} references non-existent parent {parent_id}"
            )


def _build_tree(flat: list[dict[str, Any]]) -> list[CategoryTreeNode]:
    """
    Build a nested CategoryTreeNode tree from a flat B2B category list.

    Pre-condition: _check_orphans(flat) must be called first.

    ADR — adjacency list chosen over:
      (a) ltree PostgreSQL — needs DB schema on B2C; B2C has no local category store.
      (b) Materialized path — B2B already stores this; B2C would duplicate it.
      (c) Adjacency list with in-memory traversal (chosen) — zero schema changes,
          works with B2B's flat API response, O(n) tree build.
    """
    nodes: dict[str, CategoryTreeNode] = {}
    for item in flat:
        raw_path: str = item.get("path") or ""
        path_parts = [p for p in raw_path.split("/") if p]
        nodes[item["id"]] = CategoryTreeNode(
            id=UUID(item["id"]),
            name=item["name"],
            parent_id=UUID(item["parent_id"]) if item.get("parent_id") else None,
            level=item.get("level", 0),
            path=path_parts,
            children=[],
        )

    roots: list[CategoryTreeNode] = []
    for item in flat:
        node = nodes[item["id"]]
        parent_id = item.get("parent_id")
        if parent_id and parent_id in nodes:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    return roots


def _build_breadcrumb_chain(
    flat: list[dict[str, Any]],
    target_id: str,
) -> list[dict[str, Any]]:
    """
    Walk up the adjacency list from target_id to root.

    Returns: list of CategoryResponse dicts ordered root → current.

    Raises:
      OrphanNodeError — if the chain reaches a parent_id not in the flat list
                        (broken hierarchy) or a cycle is detected.
    """
    by_id = {item["id"]: item for item in flat}

    if target_id not in by_id:
        return []   # caller interprets as 404

    chain: list[dict[str, Any]] = []
    current_id: Optional[str] = target_id
    visited: set[str] = set()

    while current_id is not None:
        if current_id in visited:
            raise OrphanNodeError(f"cycle detected at category {current_id}")
        visited.add(current_id)

        node = by_id.get(current_id)
        if node is None:
            raise OrphanNodeError(
                f"parent category {current_id} referenced but not in flat list"
            )
        chain.append(node)
        current_id = node.get("parent_id")

    chain.reverse()  # root → current

    # Extra guard: root must have level == 0
    if chain and chain[0].get("level", 0) != 0:
        raise OrphanNodeError(
            "breadcrumb chain does not start from a root category (level 0)"
        )

    return chain


# ──────────────────────────────────────────────────────────────────────────────
# Category service methods
# ──────────────────────────────────────────────────────────────────────────────

async def _list_categories(
    *,
    b2b_base_url: str,
    service_key: str,
) -> list[CategoryRef]:
    """
    Return flat list of all categories from B2B.

    B2B endpoint: GET /api/v1/categories (read-open, no auth required).

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
      OrphanNodeError — caller maps to 422.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/categories",
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()
    flat: list[dict[str, Any]] = resp.json()

    _check_orphans(flat)
    return [_b2b_cat_to_ref(item) for item in flat]


async def _get_category_tree(
    *,
    b2b_base_url: str,
    service_key: str,
) -> list[CategoryTreeNode]:
    """
    Return full nested category tree, built in-memory from B2B flat list.

    ADR (tree construction strategy):
      Three approaches considered:
        (a) Use B2B /api/v1/categories/tree directly — simplest, but returns
            CategoryTreeResponse (id, name, children only; no level/parent_id
            for orphan detection).
        (b) Fetch flat list + build in-memory (chosen) — enables orphan detection
            via parent_id validation before tree construction. O(n) build.
        (c) ltree PostgreSQL — requires a local B2C schema for categories, violating
            the B2C-is-proxy principle.
      Criteria: (1) orphan detection is a hard DoD requirement → flat + in-memory
      wins; (2) minimal infra — no extra B2C DB table needed.

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
      OrphanNodeError — caller maps to 422.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/categories",
            headers={"X-Service-Key": service_key},
        )
    resp.raise_for_status()
    flat: list[dict[str, Any]] = resp.json()

    _check_orphans(flat)
    return _build_tree(flat)


async def _get_category(
    *,
    b2b_base_url: str,
    service_key: str,
    category_id: UUID,
) -> CategoryTreeNode | None:
    """
    Return a single category with its direct children.

    B2B endpoint: GET /api/v1/categories/{category_id} →
        CategoryWithChildrenResponse (allOf CategoryResponse + children[]).

    Returns None if B2B returns 404.

    Raises:
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{b2b_base_url}/api/v1/categories/{category_id}",
            headers={"X-Service-Key": service_key},
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()

    data = resp.json()
    raw_path: str = data.get("path") or ""
    path_parts = [p for p in raw_path.split("/") if p]

    children = [
        CategoryTreeNode(
            id=UUID(child["id"]),
            name=child["name"],
            parent_id=UUID(child["parent_id"]) if child.get("parent_id") else None,
            level=child.get("level", 0),
            path=[p for p in (child.get("path") or "").split("/") if p],
            children=[],
        )
        for child in data.get("children", [])
    ]

    return CategoryTreeNode(
        id=UUID(data["id"]),
        name=data["name"],
        parent_id=UUID(data["parent_id"]) if data.get("parent_id") else None,
        level=data.get("level", 0),
        path=path_parts,
        children=children,
    )


async def _get_breadcrumbs(
    *,
    b2b_base_url: str,
    service_key: str,
    category_id: Optional[UUID],
    product_id: Optional[UUID],
) -> BreadcrumbResponse | None:
    """
    Build breadcrumb chain for a category or product.

    Algorithm (canon b2c-catalog-flows.md#b2c-5-category-nav §5d):
      1. If product_id given: fetch product to resolve category_id.
         Returns None if product not found / deleted / non-MODERATED.
      2. Fetch flat category list from B2B.
      3. Walk adjacency list from target category to root.
      4. Detect orphan: if chain reaches a missing parent → 422.
      5. Map to BreadcrumbItem list (root first, current last with is_current=True).

    Returns:
      BreadcrumbResponse — success.
      None — category / product not found (caller returns 404).

    Raises:
      OrphanNodeError — caller returns 422.
      httpx.ConnectError / httpx.TimeoutException — caller maps to 502.
    """
    resolved_via: str
    resolved_cat_id: Optional[UUID]
    resolved_prod_id: Optional[UUID] = product_id

    if product_id is not None:
        # Step 1 — resolve product → category_id
        async with httpx.AsyncClient(timeout=10.0) as client:
            prod_resp = await client.get(
                f"{b2b_base_url}/api/v1/products/{product_id}",
                headers={"X-Service-Key": service_key},
            )
        if prod_resp.status_code == 404:
            return None
        prod_resp.raise_for_status()
        prod_data = prod_resp.json()
        if prod_data.get("deleted") or prod_data.get("status") != "MODERATED":
            return None
        raw_cat = prod_data.get("category_id")
        resolved_cat_id = UUID(raw_cat) if raw_cat else None
        resolved_via = "product_id"
    else:
        resolved_cat_id = category_id
        resolved_via = "category_id"

    if resolved_cat_id is None:
        return None  # no category available

    # Step 2 — fetch flat category list
    async with httpx.AsyncClient(timeout=10.0) as client:
        cat_resp = await client.get(
            f"{b2b_base_url}/api/v1/categories",
            headers={"X-Service-Key": service_key},
        )
    cat_resp.raise_for_status()
    flat: list[dict[str, Any]] = cat_resp.json()

    # Step 3-4 — walk chain (raises OrphanNodeError on broken hierarchy)
    chain = _build_breadcrumb_chain(flat, str(resolved_cat_id))
    if not chain:
        return None  # category not found in flat list → 404

    # Step 5 — map to BreadcrumbItem[]
    items: list[BreadcrumbItem] = []
    for i, node in enumerate(chain):
        raw_path = node.get("path") or ""
        slug = raw_path.split("/")[-1] if raw_path else node["name"].lower().replace(" ", "-")
        url = f"/catalog/{raw_path}" if raw_path else None
        is_current = (i == len(chain) - 1)
        items.append(BreadcrumbItem(
            id=UUID(node["id"]),
            slug=slug,
            name=node["name"],
            url=url,
            level=node.get("level", i),
            is_current=is_current,
        ))

    meta = BreadcrumbMeta(
        resolved_via=resolved_via,
        category_id=resolved_cat_id,
        product_id=resolved_prod_id,
    )
    return BreadcrumbResponse(data=items, meta=meta)


# Attach to CatalogService
CatalogService.list_categories = staticmethod(_list_categories)        # type: ignore[attr-defined]
CatalogService.get_category_tree = staticmethod(_get_category_tree)    # type: ignore[attr-defined]
CatalogService.get_category = staticmethod(_get_category)              # type: ignore[attr-defined]
CatalogService.get_breadcrumbs = staticmethod(_get_breadcrumbs)        # type: ignore[attr-defined]
