import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class Database:
    def __init__(self, path):
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", connect_args={"timeout": 5})
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.write_lock = asyncio.Lock()

        @event.listens_for(self.engine.sync_engine, "connect")
        def pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=FULL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.write_lock:
            async with self.session() as session:
                async with session.begin():
                    yield session

    async def close(self):
        await self.engine.dispose()
