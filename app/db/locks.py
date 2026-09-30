"""Session advisory locks that remain held across durable checkpoints."""

from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_engine


@asynccontextmanager
async def locked_session(namespace: int, resource_id: int):
    """One pinned connection serves both locks and all checkpoint transactions."""
    async with get_engine().connect() as connection:
        acquired = []

        async def acquire(lock_namespace: int, lock_resource: int) -> None:
            # Hash collisions only serialize unrelated resources.
            params = {"namespace": lock_namespace, "resource": lock_resource % (2**31)}
            acquired.append(params)
            await connection.execute(text("SELECT pg_advisory_lock(:namespace, :resource)"), params)

        try:
            await acquire(namespace, resource_id)
            await connection.commit()
            async with AsyncSession(bind=connection, expire_on_commit=False, autoflush=False) as session:
                try:
                    yield session, acquire
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
        finally:
            await connection.rollback()
            for params in reversed(acquired):
                await connection.execute(text("SELECT pg_advisory_unlock(:namespace, :resource)"), params)
            await connection.commit()
