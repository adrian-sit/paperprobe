from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import event

from app.core.config import get_settings


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create the shared async session factory when a database is configured."""
    dsn = get_settings().postgres_dsn
    if not dsn:
        raise RuntimeError("POSTGRES_DSN must be set before using the database.")
    engine = create_async_engine(dsn, pool_pre_ping=True)
    from pgvector.asyncpg import register_vector

    @event.listens_for(engine.sync_engine, "connect")
    def _register_pgvector(dbapi_connection, _connection_record) -> None:
        dbapi_connection.run_async(register_vector)

    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for routes that need a transactional session."""
    async with get_session_factory()() as session:
        yield session
