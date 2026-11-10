from pathlib import Path

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
)
from sqlalchemy.engine import URL, Engine

metadata = MetaData()

schema_versions = Table(
    "schema_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
)

projects = Table(
    "projects",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)

workflows = Table(
    "workflows",
    metadata,
    Column("id", String, primary_key=True),
    Column("project_id", String, ForeignKey("projects.id"), nullable=False),
    Column("thread_id", String, nullable=False),
    Column("payload", Text, nullable=False),
    UniqueConstraint("project_id", "thread_id"),
)

requirement_versions = Table(
    "requirement_versions",
    metadata,
    Column("project_id", String, ForeignKey("projects.id"), primary_key=True),
    Column("requirement_id", String, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("payload", Text, nullable=False),
)

read_grants = Table(
    "read_grants",
    metadata,
    Column("owner_project_id", String, ForeignKey("projects.id"), primary_key=True),
    Column("grantee_project_id", String, ForeignKey("projects.id"), primary_key=True),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("project_id", String, ForeignKey("projects.id"), nullable=False),
    Column("event_type", String, nullable=False),
    Column("record_id", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("idempotency_key", String, nullable=False, unique=True),
)


def open_database(path: Path) -> Engine:
    database_path = path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(database_path)))

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    metadata.create_all(engine)
    with engine.begin() as connection:
        version = connection.execute(select(schema_versions.c.version)).scalar_one_or_none()
        if version is None:
            connection.execute(schema_versions.insert().values(id=1, version=1))
        elif version != 1:
            raise RuntimeError(f"unsupported database schema version: {version}")
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS audit_events_no_update "
            "BEFORE UPDATE ON audit_events BEGIN "
            "SELECT RAISE(ABORT, 'audit events are append-only'); END"
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete "
            "BEFORE DELETE ON audit_events BEGIN "
            "SELECT RAISE(ABORT, 'audit events are append-only'); END"
        )
    return engine
