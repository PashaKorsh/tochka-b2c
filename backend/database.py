"""
Async SQLAlchemy engine — used for cart, orders, favourites (future).
Catalog endpoints are pure B2B proxies and do not touch the local DB.
"""
import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL: str = os.getenv(
    "TEST_DATABASE_URL",  # test env takes priority
    os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5433/tochkab2c",
    ),
)

engine: AsyncEngine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def create_tables() -> None:
    # Import all models so Base.metadata knows about them before create_all
    from backend.modules.favorites import models as _fav_models  # noqa: F401
    from backend.modules.subscriptions import models as _sub_models  # noqa: F401
    from backend.modules.cart import models as _cart_models  # noqa: F401
    from backend.modules.banners import models as _banner_models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:  # type: ignore[return]
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
