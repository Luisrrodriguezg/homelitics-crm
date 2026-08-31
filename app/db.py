"""Async engine and session factory.

The engine is created lazily. Building it at import time would make the whole
package unimportable without a live DATABASE_URL — which breaks test collection,
`--help`, and OpenAPI generation for no benefit.
"""
from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,      # pooled connections get culled server-side; re-check them
        connect_args={
            # NON-NEGOTIABLE on Supabase's transaction pooler (6543).
            #
            # asyncpg prepares every statement by default and caches it against the
            # connection. Transaction-mode pooling hands you a different backend per
            # transaction, so the cached prepared statement usually is not there:
            # you get "prepared statement __asyncpg_stmt_x__ does not exist", often
            # only under load. Disabling the cache is the documented fix.
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "server_settings": {"application_name": "homelitics-api"},
        },
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(), class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the pool on shutdown (called from the lifespan)."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
