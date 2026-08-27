"""Database engines and session factories.

Two engines share one set of models:
  * async  — the FastAPI request path, so SSE streaming never blocks the loop.
  * sync   — the RQ worker, Alembic and seed scripts, which are plain processes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings


def _async_url(url: str) -> str:
    # psycopg3 serves both sync and async under the same +psycopg driver name.
    return url


async_engine = create_async_engine(
    _async_url(settings.async_database_url),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

sync_engine = create_engine(
    settings.sync_database_url, pool_pre_ping=True, pool_size=5, echo=False
)

SyncSessionLocal = sessionmaker(sync_engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@contextmanager
def sync_session() -> Iterator[Session]:
    """Context manager for worker/CLI code."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
