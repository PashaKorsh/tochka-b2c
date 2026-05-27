"""
Cart router — US-B2C-08.

Spec: b2c/openapi.yaml (neomarket-protocols)
  GET    /api/v1/cart                   → CartResponse (200)
  POST   /api/v1/cart/items             → CartResponse (200)
  PATCH  /api/v1/cart/items/{sku_id}    → CartResponse (200)
  DELETE /api/v1/cart/items/{sku_id}    → CartResponse (200)
  DELETE /api/v1/cart                   → 204 No Content
  POST   /api/v1/cart/merge             → CartResponse (200, requires JWT + X-Session-Id)

Identity resolution:
  - JWT present → user_id from claims, X-Session-Id ignored
  - No JWT, X-Session-Id present → guest cart
  - Neither → 400 MISSING_CART_IDENTITY

ADR — Guest cart identity:
  Chose X-Session-Id (opaque UUID header) over cookies and temporary JWT:
  1. Mobile compatibility: native clients control headers directly, avoid cookie
     restrictions (SameSite, Secure flags) that complicate cross-origin mobile apps.
  2. Forgery risk acceptable: UUID is unguessable (128 bits). No server-side
     state needed for validation unlike signed tokens. The cart is low-stakes
     (no payment data). IDOR prevention: guest can only access their own session
     because they must know the UUID — same-origin guessing is infeasible.
  Discarded alternatives:
  - Cookies: cross-origin issues, SameSite restrictions on mobile, require CORS
    credentials which conflict with wildcard allow_origins in main.py.
  - Temporary JWT: overkill, requires token issuance endpoint, adds auth complexity.
"""
from typing import Optional
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import JWT_ALGORITHM, JWT_SECRET_KEY
from backend.database import get_db
from backend.modules.cart.schemas import (
    CartItemAddRequest,
    CartItemUpdateRequest,
    CartResponseSchema,
)
from backend.modules.cart.service import CartService

router = APIRouter(prefix="/api/v1", tags=["Cart"])

# Optional bearer — does not raise if token absent
_optional_bearer = HTTPBearer(auto_error=False)


def _decode_optional_jwt(
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[UUID]:
    """Try to extract user_id from JWT; return None if absent or invalid."""
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


async def _get_cart_identity(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        _optional_bearer
    ),
) -> tuple[Optional[UUID], Optional[str]]:
    """
    Resolve cart identity from request:
      - JWT present (and valid) → (user_id, None)
      - X-Session-Id present, no JWT → (None, session_id)
      - Neither → 400 MISSING_CART_IDENTITY

    Per spec: JWT takes priority; X-Session-Id is ignored when JWT is valid.
    """
    user_id = _decode_optional_jwt(credentials)
    if user_id:
        return user_id, None

    session_id: Optional[str] = request.headers.get("X-Session-Id")
    if session_id:
        return None, session_id

    raise HTTPException(
        status_code=400,
        detail={
            "code": "MISSING_CART_IDENTITY",
            "message": "Provide Authorization header (JWT) or X-Session-Id header",
        },
    )


def _upstream_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": f"B2B catalog is not available: {exc}",
        },
    )


def _value_error_response(exc: ValueError) -> HTTPException:
    code = str(exc)
    if code == "SKU_NOT_FOUND":
        return HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "SKU not found or product unavailable"},
        )
    if code == "ITEM_NOT_FOUND":
        return HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Cart item not found"},
        )
    if code.startswith("INSUFFICIENT_STOCK:"):
        available = code.split(":")[1]
        return HTTPException(
            status_code=409,
            detail={
                "code": "INSUFFICIENT_STOCK",
                "message": f"Not enough stock, available: {available}",
            },
        )
    return HTTPException(
        status_code=400,
        detail={"code": "INVALID_REQUEST", "message": code},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────────────────────


@router.get("/cart", response_model=CartResponseSchema)
async def get_cart(
    identity: tuple = Depends(_get_cart_identity),
    db: AsyncSession = Depends(get_db),
):
    """Return enriched cart for the current user or guest session."""
    user_id, session_id = identity
    try:
        return await CartService.get_cart(db, user_id=user_id, session_id=session_id)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)


@router.post("/cart/items", response_model=CartResponseSchema)
async def add_to_cart(
    body: CartItemAddRequest,
    identity: tuple = Depends(_get_cart_identity),
    db: AsyncSession = Depends(get_db),
):
    """
    Add a SKU to the cart. If already present — increment quantity.
    Always returns 200 with full CartResponse per spec.
    """
    user_id, session_id = identity
    try:
        return await CartService.add_to_cart(
            db,
            user_id=user_id,
            session_id=session_id,
            sku_id=body.sku_id,
            quantity=body.quantity,
        )
    except ValueError as exc:
        raise _value_error_response(exc)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)


@router.patch("/cart/items/{sku_id}", response_model=CartResponseSchema)
async def update_cart_item(
    sku_id: UUID,
    body: CartItemUpdateRequest,
    identity: tuple = Depends(_get_cart_identity),
    db: AsyncSession = Depends(get_db),
):
    """Set the quantity of an existing cart item."""
    user_id, session_id = identity
    try:
        return await CartService.update_cart_item(
            db,
            user_id=user_id,
            session_id=session_id,
            sku_id=sku_id,
            quantity=body.quantity,
        )
    except ValueError as exc:
        raise _value_error_response(exc)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)


@router.delete("/cart/items/{sku_id}", response_model=CartResponseSchema)
async def remove_cart_item(
    sku_id: UUID,
    identity: tuple = Depends(_get_cart_identity),
    db: AsyncSession = Depends(get_db),
):
    """Remove a single SKU from the cart. Returns updated CartResponse."""
    user_id, session_id = identity
    try:
        return await CartService.remove_cart_item(
            db,
            user_id=user_id,
            session_id=session_id,
            sku_id=sku_id,
        )
    except ValueError as exc:
        raise _value_error_response(exc)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)


@router.delete("/cart", status_code=204)
async def clear_cart(
    identity: tuple = Depends(_get_cart_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Clear all items from the cart. Returns 204 No Content."""
    user_id, session_id = identity
    await CartService.clear_cart(db, user_id=user_id, session_id=session_id)
    return Response(status_code=204)


@router.post("/cart/merge", response_model=CartResponseSchema)
async def merge_cart(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=True)
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Explicit merge of guest cart (X-Session-Id) into authenticated user cart.

    Requires JWT (user_id) + X-Session-Id header.
    Per spec: X-Session-Id is required (not optional) for this endpoint.
    """
    user_id = _decode_optional_jwt(credentials)
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Valid JWT required for cart merge"},
        )

    session_id: Optional[str] = request.headers.get("X-Session-Id")
    if not session_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_CART_IDENTITY",
                "message": "X-Session-Id header is required for cart merge",
            },
        )

    try:
        return await CartService.merge_cart(
            db, user_id=user_id, session_id=session_id
        )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
        raise _upstream_error(exc)
