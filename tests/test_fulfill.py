"""
Tests for US-B2C-13 (Fulfill / Deliver).

Spec: b2c/openapi.yaml
  POST /api/v1/orders/{order_id}/deliver
    Header: X-Service-Key (admin)
    200:    OrderResponse (status=DELIVERED)
    401:    missing/invalid X-Service-Key
    404:    order not found
    409:    DELIVER_NOT_ALLOWED (CANCELLED / CANCEL_PENDING)

Canon: b2c-orders-flows.md#b2c-13-fulfill

DoD test names (exact):
  delivered_status_triggers_fulfill_to_b2b
  fulfill_failure_retried_asynchronously
  repeated_fulfill_idempotent

B2B inventory/fulfill is mocked via backend.modules.orders.service.httpx.AsyncClient.
"""
from __future__ import annotations

import os
from uuid import uuid4, UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from unittest.mock import patch

from backend.main import app
from backend.auth import create_test_token
from backend.modules.orders.models import Order, OrderItem

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c_test",
)

_ADMIN_KEY = os.getenv("B2C_ADMIN_KEY", "dev-service-key")


# ──────────────────────────────────────────────────────────────────────────────
# DB seed helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _seed_order(
    db: AsyncSession,
    *,
    buyer_id: UUID,
    status: str = "DELIVERING",
    delivery_address: str = "ул. Тестовая, 1",
    items: list[dict] | None = None,
) -> Order:
    """Insert Order + OrderItems directly (no B2B calls)."""
    if items is None:
        items = [{"sku_id": uuid4(), "product_id": uuid4(), "name": "Item", "quantity": 2, "unit_price": 500, "line_total": 1000}]

    subtotal = sum(it["line_total"] for it in items)
    order = Order(
        id=uuid4(),
        buyer_id=buyer_id,
        idempotency_key=str(uuid4()),
        status=status,
        delivery_address=delivery_address,
        subtotal=subtotal,
        total=subtotal,
    )
    db.add(order)
    await db.flush()

    for it in items:
        db.add(OrderItem(
            order_id=order.id,
            sku_id=it.get("sku_id", uuid4()),
            product_id=it.get("product_id", uuid4()),
            name=it.get("name", "Test Product"),
            quantity=it.get("quantity", 1),
            unit_price=it.get("unit_price", 1000),
            line_total=it.get("line_total", 1000),
        ))

    await db.commit()
    return order


async def _get_order_from_db(order_id: UUID) -> Order | None:
    """Read order state directly from DB for assertions."""
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        from sqlalchemy import select
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
    await engine.dispose()
    return order


# ──────────────────────────────────────────────────────────────────────────────
# B2B mock helpers
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResp:
    """httpx response stub — raise_for_status without requiring request attribute."""
    def __init__(self, data=None, status_code: int = 200):
        self._data = data or {}
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            from httpx import HTTPStatusError, Request as Req, Response as Resp
            req = Req("POST", "http://b2b/")
            raw = Resp(self.status_code, request=req)
            raise HTTPStatusError(f"HTTP {self.status_code}", request=req, response=raw)


class _CountingMockClient:
    """
    Mock for httpx.AsyncClient that counts post() calls and records their args.
    Use as a context manager replacer for httpx.AsyncClient.
    Pass raise_on_enter=True to simulate ConnectError.
    """
    def __init__(self, response: _FakeResp | None = None, raise_on_enter: bool = False):
        self._response = response or _FakeResp({"status": "FULFILLED"}, status_code=200)
        self._raise = raise_on_enter
        self.post_calls: list[dict] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        if self._raise:
            raise httpx.ConnectError("B2B fulfill unreachable")
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, url: str, *, json=None, headers=None, **kw) -> _FakeResp:
        self.post_calls.append({"url": url, "json": json})
        return self._response

    async def get(self, *a, **kw) -> _FakeResp:
        return _FakeResp()


def _admin_headers() -> dict:
    return {"X-Service-Key": _ADMIN_KEY}


# ──────────────────────────────────────────────────────────────────────────────
# DoD tests (exact names required)
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delivered_status_triggers_fulfill_to_b2b():
    """
    POST /api/v1/orders/{id}/deliver → order becomes DELIVERED and
    POST /api/v1/inventory/fulfill is called exactly once with correct payload.

    DoD: delivered_status_triggers_fulfill_to_b2b
    """
    buyer_id = uuid4()
    sku_id = uuid4()
    mock_client = _CountingMockClient()

    # Seed DELIVERING order with known items
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(
            db,
            buyer_id=buyer_id,
            status="DELIVERING",
            items=[{"sku_id": sku_id, "product_id": uuid4(), "name": "Widget", "quantity": 3, "unit_price": 200, "line_total": 600}],
        )
    await engine.dispose()

    with patch("backend.modules.orders.service.httpx.AsyncClient", mock_client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/deliver",
                headers=_admin_headers(),
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DELIVERED"
    assert body["id"] == str(order.id)

    # B2B fulfill was called once
    assert len(mock_client.post_calls) == 1
    call = mock_client.post_calls[0]
    assert "/inventory/fulfill" in call["url"]
    assert call["json"]["order_id"] == str(order.id)
    assert len(call["json"]["items"]) == 1
    assert call["json"]["items"][0]["sku_id"] == str(sku_id)
    assert call["json"]["items"][0]["quantity"] == 3

    # fulfill_completed_at is set in DB
    db_order = await _get_order_from_db(order.id)
    assert db_order is not None
    assert db_order.status == "DELIVERED"
    assert db_order.fulfill_completed_at is not None


@pytest.mark.asyncio
async def test_fulfill_failure_retried_asynchronously():
    """
    When B2B inventory/fulfill is unreachable, the order still becomes DELIVERED
    (buyer gets their goods) but fulfill_completed_at stays NULL — marking the order
    as eligible for async retry (Celery worker scaffold).

    DoD: fulfill_failure_retried_asynchronously
    """
    buyer_id = uuid4()
    # Mock raises ConnectError on B2B call
    mock_client = _CountingMockClient(raise_on_enter=True)

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(db, buyer_id=buyer_id, status="DELIVERING")
    await engine.dispose()

    with patch("backend.modules.orders.service.httpx.AsyncClient", mock_client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/deliver",
                headers=_admin_headers(),
            )

    # Deliver succeeds from the caller's perspective (order confirmed DELIVERED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "DELIVERED"

    # fulfill_completed_at is NULL → retry pending
    db_order = await _get_order_from_db(order.id)
    assert db_order is not None
    assert db_order.status == "DELIVERED"
    assert db_order.fulfill_completed_at is None, (
        "fulfill_completed_at should be NULL after B2B failure — marks retry eligibility"
    )


@pytest.mark.asyncio
async def test_repeated_fulfill_idempotent():
    """
    Calling POST .../deliver twice with B2B succeeding on the first call:
    the second call detects fulfill_completed_at is already set and skips B2B.
    B2B post() is called exactly once, not twice.

    DoD: repeated_fulfill_idempotent
    """
    buyer_id = uuid4()
    mock_client = _CountingMockClient()

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(db, buyer_id=buyer_id, status="DELIVERING")
    await engine.dispose()

    with patch("backend.modules.orders.service.httpx.AsyncClient", mock_client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # First call — transitions to DELIVERED, calls B2B
            r1 = await client.post(
                f"/api/v1/orders/{order.id}/deliver",
                headers=_admin_headers(),
            )
            assert r1.status_code == 200
            assert r1.json()["status"] == "DELIVERED"

            # Second call — order already DELIVERED with fulfill_completed_at set → skip B2B
            r2 = await client.post(
                f"/api/v1/orders/{order.id}/deliver",
                headers=_admin_headers(),
            )
            assert r2.status_code == 200
            assert r2.json()["status"] == "DELIVERED"

    # B2B fulfill called exactly once despite two deliver requests
    assert len(mock_client.post_calls) == 1, (
        f"Expected 1 B2B fulfill call but got {len(mock_client.post_calls)}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Additional guard tests
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_without_service_key_returns_401():
    """Missing X-Service-Key → 401 UNAUTHORIZED."""
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(db, buyer_id=uuid4(), status="DELIVERING")
    await engine.dispose()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/v1/orders/{order.id}/deliver")  # no key

    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_deliver_nonexistent_order_returns_404():
    """Order UUID that doesn't exist → 404 ORDER_NOT_FOUND."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{uuid4()}/deliver",
            headers=_admin_headers(),
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "ORDER_NOT_FOUND"


@pytest.mark.asyncio
async def test_deliver_cancelled_order_returns_409():
    """CANCELLED order cannot be delivered → 409 DELIVER_NOT_ALLOWED."""
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(db, buyer_id=uuid4(), status="CANCELLED")
    await engine.dispose()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/orders/{order.id}/deliver",
            headers=_admin_headers(),
        )

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "DELIVER_NOT_ALLOWED"
    assert body["details"]["current_status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_deliver_retry_after_failure_calls_b2b_again():
    """
    If a previous deliver attempt failed (fulfill_completed_at is NULL),
    a subsequent deliver call retries the B2B fulfill.
    This validates that the retry path works (not just that the first call sets the flag).
    """
    buyer_id = uuid4()

    # Seed a DELIVERED order with fulfill_completed_at = NULL (failed first attempt)
    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await _seed_order(db, buyer_id=buyer_id, status="DELIVERED")
        # fulfill_completed_at is NULL by default → retry eligible
    await engine.dispose()

    mock_client = _CountingMockClient()

    with patch("backend.modules.orders.service.httpx.AsyncClient", mock_client):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/orders/{order.id}/deliver",
                headers=_admin_headers(),
            )

    assert resp.status_code == 200
    # B2B was called because fulfill_completed_at was NULL
    assert len(mock_client.post_calls) == 1

    # Now fulfill_completed_at is set
    db_order = await _get_order_from_db(order.id)
    assert db_order.fulfill_completed_at is not None
