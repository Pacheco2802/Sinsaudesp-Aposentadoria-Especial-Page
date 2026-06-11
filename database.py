import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

_raw_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://localhost/sinsaudesp")
# Support both Railway's bare postgresql:// and explicit driver URLs
if _raw_url.startswith("postgresql://") and "+asyncpg" not in _raw_url and "+psycopg" not in _raw_url:
    DATABASE_URL = _raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
elif _raw_url.startswith("postgresql+asyncpg://"):
    DATABASE_URL = _raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
else:
    DATABASE_URL = _raw_url

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, echo=False)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
