"""
Shared pytest fixtures for tests that need the local PostgreSQL database.

Problem: pytest-asyncio 0.23 creates a new event loop per test function.
asyncpg connection pools are bound to the event loop that created them.
Using the default pool across different event loops causes:
  asyncpg.InterfaceError: cannot perform operation: another operation is in progress

Fix: use NullPool (no connection reuse) and override `get_db` with a fresh
session-maker per test. Each test gets its own connection that is created and
destroyed within a single event loop.

Tables are created once using asyncio.run() (synchronous) before tests start.
"""
import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.modules.favorites import models as _fav_models  # noqa: F401 — register Favorite with Base
from backend.database import Base, get_db

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2b_test",
)


@pytest.fixture(scope="session", autouse=True)
def create_tables_sync():
    """
    Create all tables once before the test session (synchronous, no event-loop issues).
    Uses NullPool to avoid asyncpg event-loop binding problems.
    """
    async def _setup():
        engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(_setup())


@pytest.fixture(autouse=True)
async def override_db():
    """
    Override FastAPI's get_db dependency with a test session backed by NullPool.
    Each test gets a fresh connection; no pool reuse across event loops.
    Rolls back all changes after each test for isolation.
    """
    from backend.main import app

    engine = create_async_engine(_TEST_DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _get_test_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)
    await engine.dispose()
