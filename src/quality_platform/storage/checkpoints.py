from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def create_checkpoint_saver(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    checkpoint_path = path.resolve()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    async with aiosqlite.connect(checkpoint_path) as connection:
        yield AsyncSqliteSaver(connection, serde=serializer)
