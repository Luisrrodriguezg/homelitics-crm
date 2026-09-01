"""Async engine and session factory.

The engine is created lazily. Building it at import time would make the whole
package unimportable without a live DATABASE_URL — which breaks test collection,
`--help`, and OpenAPI generation for no benefit.
"""
import uuid
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
            # NON-NEGOTIABLE on Supabase's transaction pooler (6543). All three
            # settings are needed; two out of three still breaks.
            #
            # asyncpg prepares every statement and caches it against the
            # connection. Transaction-mode pooling hands you a different backend
            # per transaction, so those caches are wrong in both directions:
            #
            #   "prepared statement __asyncpg_stmt_N__ does not exist"
            #       -- the cached statement was prepared on a different backend
            #   "prepared statement __asyncpg_stmt_N__ already exists"
            #       -- a *different* session already used that name on this
            #          backend, because the counter restarts per connection
            #
            # Disabling both caches stops the first. The name function stops the
            # second: default names are a per-connection counter, so two pooled
            # sessions collide on __asyncpg_stmt_24__ sooner or later. Making the
            # names unique removes the collision entirely. Both failures show up
            # only under concurrency, which is why this is easy to ship broken.
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__hl_{uuid.uuid4().hex}__",
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
