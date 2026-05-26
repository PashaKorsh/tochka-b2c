"""
JWT authentication dependency for B2C.

Security (canon b2c-cart-flows.md§1):
  user_id is extracted ONLY from JWT claims (field `sub`).
  NEVER accepted from query params, body, or X-User-Id header.
  Reason: query params can be forged → IDOR (anyone can act as another user).

ADR — user identification approach (US-B2C-06):
  Three options considered:
    (a) user_id from query param — simplest, but IDOR vulnerability.
        Any client can supply any user_id. Rejected.
    (b) X-User-Id from header — still forgeable unless an API Gateway
        strips and re-injects it after JWT validation. No gateway in this MVP.
        Rejected.
    (c) user_id from JWT claims (chosen) — server validates signature,
        extracts sub claim. Client cannot forge user_id without the secret key.
        Standard Bearer token flow.
  Criteria: (1) IDOR prevention — only JWT claims are trustworthy without a gateway;
  (2) implementation simplicity — one dependency, no middleware changes.

Environment:
  JWT_SECRET_KEY  — signing secret (default: dev-secret-key for local/test).
  JWT_ALGORITHM   — signing algorithm (default: HS256).
"""
from __future__ import annotations

import os
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "dev-secret-key")
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

_bearer = HTTPBearer(auto_error=True)


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> UUID:
    """
    Decode Bearer JWT and return `sub` claim as UUID.

    Raises HTTP 401 UNAUTHORIZED if:
      - No Authorization header.
      - Token is expired, malformed, or has invalid signature.
      - `sub` claim is missing or not a valid UUID.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise ValueError("sub claim missing")
        user_id = UUID(str(sub))
    except (JWTError, ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": f"Invalid or expired token: {exc}"},
        )
    return user_id


def create_test_token(user_id: UUID) -> str:
    """
    Create a signed JWT for use in tests.
    Uses the same JWT_SECRET_KEY / JWT_ALGORITHM as the auth dependency.
    """
    payload = {"sub": str(user_id)}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
