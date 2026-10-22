from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection]:
        """A connection inside a transaction: commits on success, rolls back on error."""
        async with self.engine.begin() as conn:
            yield conn

    async def dispose(self) -> None:
        await self.engine.dispose()
