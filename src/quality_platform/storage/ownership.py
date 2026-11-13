"""Single-writer ownership and non-mutating SQLite access."""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine


@contextmanager
def writer_lock(database: Path) -> Iterator[None]:
    resolved = database.resolve()
    # The database name may contain '?' or '#', which filelock cannot open as
    # a Windows lock path even though SQLite can address the database.
    identity = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    target = resolved.with_name(f".qp-writer-{identity}.lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(target, timeout=0)
    try:
        lock.acquire()
    except Timeout as error:
        raise RuntimeError(f"database writer already active: {database.resolve()}") from error
    try:
        yield
    finally:
        lock.release()


def open_read_only_database(database: Path) -> Engine:
    # URI mode=ro refuses missing files and never runs schema initialization or migrations.
    resolved = database.resolve()
    uri = resolved.as_uri() + "?mode=ro"
    # A creator preserves URI escaping: SQLAlchemy otherwise unquotes '?' and '#' in paths.
    engine = create_engine(
        URL.create("sqlite+pysqlite", database=str(resolved)),
        creator=lambda: sqlite3.connect(uri, uri=True, check_same_thread=False),
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA query_only = ON")
        cursor.close()

    return engine
