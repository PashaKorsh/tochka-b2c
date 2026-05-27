"""
Tests for US-B2C-07 — Product notification subscriptions.

Spec: b2c/openapi.yaml (neomarket-protocols)
  POST   /api/v1/favorites/{product_id}/subscribe → 201 or 409/404/400
  DELETE /api/v1/favorites/{product_id}/subscribe → 204 (idempotent)

notify_on valid values: BACK_IN_STOCK, PRICE_DROP

DoD test names (exact):
  subscribe_returns_201_with_notify_on
  duplicate_subscription_returns_409
  invalid_notify_on_returns_400
  subscribe_to_unknown_product_returns_404

Auth: Bearer JWT with user_id from JWT sub claim.
B2B is mocked via backend.modules.subscriptions.service.httpx.AsyncClient.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from backend.auth import create_test_token
from backend.main import app

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

SUBSCRIBE_PATH = "/api/v1/favorites/{product_id}/subscribe"


def _auth(user_id) -> dict:
    return {"Authorization": f"Bearer {create_test_token(user_id)}"}


@asynccontextmanager
async def _make_client(user_id=None):
    headers = _auth(user_id) if user_id else {}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers,
    ) as client:
        yield client


class _FakeResp:
    """
    Stub for httpx.Response used in mock httpx.AsyncClient.
    raise_for_status() is implemented manually to avoid httpx's requirement
    that request must be set before calling raise_for_status().
    """
    def __init__(self, data=None, status_code: int = 200):
        self._data = data if data is not None else []
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError, Request as HttpxRequest, Response as HttpxResponse
            req = HttpxRequest("POST", "http://b2b/")
            raw = HttpxResponse(self.status_code, request=req)
            raise HTTPStatusError(f"HTTP {self.status_code}", request=req, response=raw)


def _b2b_found(product_id) -> _FakeResp:
    """Simulate B2B batch returning the product (exists)."""
    return _FakeResp(data=[{"id": str(product_id), "title": "Test Product"}])


def _b2b_not_found() -> _FakeResp:
    """Simulate B2B batch returning empty list (product unknown/deleted/blocked)."""
    return _FakeResp(data=[])


class _MockClient:
    """
    Drop-in replacement for httpx.AsyncClient used as async context manager.
    Used as side_effect=mock so each `httpx.AsyncClient(...)` call returns
    a new context-manager instance backed by the same _FakeResp.
    """
    def __init__(self, response: _FakeResp):
        self._response = response

    def __call__(self, **kwargs):
        return _MockClientInstance(self._response)


class _MockClientInstance:
    def __init__(self, response: _FakeResp):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def get(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact function names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_returns_201_with_notify_on():
    """
    POST /api/v1/favorites/{product_id}/subscribe with valid notify_on
    → 201 Created with SubscriptionResponse body.
    """
    product_id = uuid4()
    user_id = uuid4()

    mock = _MockClient(_b2b_found(product_id))

    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=mock,
    ):
        async with _make_client(user_id) as client:
            resp = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["BACK_IN_STOCK", "PRICE_DROP"]},
            )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["product_id"] == str(product_id)
    assert body["user_id"] == str(user_id)
    assert set(body["notify_on"]) == {"BACK_IN_STOCK", "PRICE_DROP"}
    assert "created_at" in body


@pytest.mark.asyncio
async def test_duplicate_subscription_returns_409():
    """
    Two identical subscribe requests for the same user+product
    → first 201, second 409 DUPLICATE_SUBSCRIPTION.
    """
    product_id = uuid4()
    user_id = uuid4()

    # First request — should succeed
    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_id) as client:
            resp1 = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["BACK_IN_STOCK"]},
            )
    assert resp1.status_code == 201, resp1.text

    # Second request — should be rejected
    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_id) as client:
            resp2 = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["PRICE_DROP"]},
            )

    assert resp2.status_code == 409, resp2.text
    body = resp2.json()
    assert body["code"] == "DUPLICATE_SUBSCRIPTION"


@pytest.mark.asyncio
async def test_invalid_notify_on_returns_400():
    """
    POST with notify_on containing an invalid event name → 400/422.
    Empty notify_on list is also rejected.
    """
    product_id = uuid4()
    user_id = uuid4()

    # Invalid event name (canon value not in spec enum)
    async with _make_client(user_id) as client:
        resp = await client.post(
            SUBSCRIBE_PATH.format(product_id=product_id),
            json={"notify_on": ["IN_STOCK"]},
        )

    assert resp.status_code in (400, 422), resp.text
    body = resp.json()
    assert body.get("code") in ("VALIDATION_ERROR", "INVALID_REQUEST"), body

    # Empty notify_on — min_length=1 validator
    async with _make_client(user_id) as client:
        resp2 = await client.post(
            SUBSCRIBE_PATH.format(product_id=product_id),
            json={"notify_on": []},
        )

    assert resp2.status_code in (400, 422), resp2.text


@pytest.mark.asyncio
async def test_subscribe_to_unknown_product_returns_404():
    """
    POST for a product_id not present in B2B catalog → 404 NOT_FOUND.
    """
    product_id = uuid4()
    user_id = uuid4()

    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_not_found()),
    ):
        async with _make_client(user_id) as client:
            resp = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["BACK_IN_STOCK"]},
            )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# Extra quality tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unsubscribe_returns_204():
    """DELETE subscribe → 204 No Content."""
    product_id = uuid4()
    user_id = uuid4()

    # Subscribe first
    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_id) as client:
            r = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["PRICE_DROP"]},
            )
    assert r.status_code == 201

    # Unsubscribe
    async with _make_client(user_id) as client:
        resp = await client.delete(
            SUBSCRIBE_PATH.format(product_id=product_id),
        )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_unsubscribe_idempotent_returns_204():
    """
    DELETE subscribe when not subscribed → still 204 (idempotent).
    """
    product_id = uuid4()
    user_id = uuid4()

    async with _make_client(user_id) as client:
        resp = await client.delete(
            SUBSCRIBE_PATH.format(product_id=product_id),
        )

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_user_id_from_jwt_not_query():
    """
    IDOR: user_id must come from JWT, not from any query parameter.
    Two users can subscribe to the same product independently (no 409 between them).
    """
    product_id = uuid4()
    user_a = uuid4()
    user_b = uuid4()

    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_a) as client:
            resp_a = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["BACK_IN_STOCK"]},
            )
        assert resp_a.status_code == 201

    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_b) as client:
            resp_b = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["PRICE_DROP"]},
            )
        assert resp_b.status_code == 201

    assert resp_a.json()["user_id"] == str(user_a)
    assert resp_b.json()["user_id"] == str(user_b)
    assert resp_a.json()["user_id"] != resp_b.json()["user_id"]


@pytest.mark.asyncio
async def test_subscribe_requires_authentication():
    """No JWT → 401 UNAUTHORIZED."""
    product_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            SUBSCRIBE_PATH.format(product_id=product_id),
            json={"notify_on": ["BACK_IN_STOCK"]},
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unsubscribe_requires_authentication():
    """No JWT → 401 UNAUTHORIZED for DELETE."""
    product_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.delete(
            SUBSCRIBE_PATH.format(product_id=product_id),
        )

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_subscribe_single_event():
    """Single valid event → 201 with exactly one element in notify_on."""
    product_id = uuid4()
    user_id = uuid4()

    with patch(
        "backend.modules.subscriptions.service.httpx.AsyncClient",
        side_effect=_MockClient(_b2b_found(product_id)),
    ):
        async with _make_client(user_id) as client:
            resp = await client.post(
                SUBSCRIBE_PATH.format(product_id=product_id),
                json={"notify_on": ["PRICE_DROP"]},
            )

    assert resp.status_code == 201, resp.text
    assert resp.json()["notify_on"] == ["PRICE_DROP"]
