"""Local control plane: sessions, immutable settings and durable commands."""

import hashlib
import json
import secrets
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pwdlib import PasswordHash

from quality_platform.config.models import AppConfig, LocalConfig

PASSWORDS = PasswordHash.recommended()
DEFAULT_PASSWORD = "1234567890"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Conflict(ValueError):
    """A command key or reviewed version no longer matches."""


class Control:
    def __init__(self, state: Path):
        self.state = state.resolve()
        self.path = self.state / "control.sqlite"

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(
        self, config: LocalConfig, owner: str = "Owner", password: str = DEFAULT_PASSWORD
    ) -> None:
        if password != DEFAULT_PASSWORD and len(password) < 12:
            raise ValueError("Use a password of at least 12 characters")
        if not owner.strip():
            raise ValueError("Owner is required")
        self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.path.exists():
            raise ValueError("Web state already initialized")
        # Exclusive creation also prevents two initialization processes racing.
        with self.path.open("xb"):
            pass
        self.path.chmod(0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE owner (name TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                                    display_name TEXT NOT NULL, default_password INTEGER NOT NULL);
                CREATE TABLE settings (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                                       created REAL NOT NULL);
                CREATE TABLE settings_requests (actor TEXT NOT NULL, command_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, settings_id TEXT NOT NULL,
                    PRIMARY KEY(actor, command_key));
                CREATE TABLE sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL,
                                       actor TEXT, expires REAL NOT NULL);
                CREATE TABLE login_attempts (client TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE jobs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    scope TEXT NOT NULL, command_key TEXT NOT NULL, payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, actor TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT,
                    created REAL NOT NULL, started REAL, finished REAL,
                    UNIQUE(scope, command_key));
                CREATE TABLE runtime (id INTEGER PRIMARY KEY, heartbeat REAL NOT NULL);
                CREATE TABLE reports (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                                      payload TEXT NOT NULL);
            """)
            db.execute(
                "INSERT INTO owner VALUES (?, ?, ?, ?)",
                (owner, PASSWORDS.hash(password), owner, password == DEFAULT_PASSWORD),
            )
        # The database stays fixed; the versioned workspace is editable for new projects.
        server = self.state / "server.json"
        with server.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "database_path": str(config.database_path.resolve()),
                },
                stream,
            )
        server.chmod(0o600)
        self.save_settings(config.model_dump(mode="json", exclude={"database_path"}))

    def paths(self) -> dict[str, str]:
        return json.loads((self.state / "server.json").read_text(encoding="utf-8"))

    def config(self, settings_id: str | None = None) -> AppConfig:
        config = self.local_config(settings_id)
        if config.agent_models is None:
            raise ValueError(
                "Configure all agent models in Web Settings before starting workflows or benchmarks"
            )
        return AppConfig.model_validate(config.model_dump())

    def local_config(self, settings_id: str | None = None) -> LocalConfig:
        with self.connect() as db:
            row = (
                db.execute("SELECT payload FROM settings WHERE id = ?", (settings_id,)).fetchone()
                if settings_id
                else db.execute(
                    "SELECT payload FROM settings ORDER BY created DESC, rowid DESC LIMIT 1"
                ).fetchone()
            )
        if row is None:
            raise ValueError("Settings version not found")
        return LocalConfig.model_validate({**json.loads(row[0]), **self.paths()})

    def settings_id(self) -> str:
        with self.connect() as db:
            return str(
                db.execute(
                    "SELECT id FROM settings ORDER BY created DESC, rowid DESC LIMIT 1"
                ).fetchone()[0]
            )

    def save_settings(
        self, values: dict[str, Any], *, key: str | None = None, actor: str = ""
    ) -> str:
        allowed = {"agent_models", "max_concurrency", "test_case_style"}
        if set(values) not in (allowed, allowed | {"workspace_root"}, {"workspace_root"}):
            raise ValueError("Only model, workspace, concurrency and rendering settings can change")
        request_hash = digest(json.dumps(values, sort_keys=True, separators=(",", ":")))
        identity = secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                previous = db.execute(
                    "SELECT * FROM settings_requests WHERE actor=? AND command_key=?", (actor, key)
                ).fetchone()
                if previous:
                    if previous["payload_hash"] != request_hash:
                        raise Conflict("Settings key already belongs to another payload")
                    return str(previous["settings_id"])
            previous_settings = db.execute(
                "SELECT payload FROM settings ORDER BY created DESC, rowid DESC LIMIT 1"
            ).fetchone()
            config = LocalConfig.model_validate(
                {
                    **(json.loads(previous_settings[0]) if previous_settings else {}),
                    **values,
                    **self.paths(),
                }
            )
            payload = config.model_dump_json(exclude={"database_path"})
            db.execute("INSERT INTO settings VALUES (?, ?, ?)", (identity, payload, time.time()))
            if key:
                db.execute(
                    "INSERT INTO settings_requests VALUES (?,?,?,?)",
                    (actor, key, request_hash, identity),
                )
        return identity

    def save_workspace(self, path: str, *, key: str | None = None, actor: str = "") -> str:
        if not path.strip():
            raise ValueError("Workspace path is required")
        workspace = Path(path).expanduser()
        if not workspace.is_absolute():
            raise ValueError("Workspace must be an absolute path")
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        # Test actual write access, including ACLs and read-only mounts.
        with tempfile.TemporaryFile(dir=workspace) as probe:
            probe.write(b"workspace")
            probe.flush()
        return self.save_settings({"workspace_root": str(workspace)}, key=key, actor=actor)

    def account(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT name, display_name, default_password FROM owner").fetchone()
        return {**dict(row), "default_password": bool(row["default_password"])}

    def update_account(
        self, display_name: str, current_password: str, new_password: str | None, *, client: str
    ) -> bool:
        display_name = display_name.strip()
        if not display_name or len(display_name) > 200:
            raise ValueError("Owner name must contain 1 to 200 characters")
        if new_password is not None and (
            len(new_password) < 12 or new_password == DEFAULT_PASSWORD
        ):
            raise ValueError("New password must have at least 12 characters")
        if not self.login(client, self.account()["name"], current_password):
            raise PermissionError("Current password is incorrect or temporarily rate-limited")
        password_hash = PASSWORDS.hash(new_password) if new_password is not None else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT password_hash FROM owner").fetchone()
            if not PASSWORDS.verify(current_password, row["password_hash"]):
                raise PermissionError("Password changed; sign in again")
            db.execute("UPDATE owner SET display_name=?", (display_name,))
            if password_hash:
                db.execute("UPDATE owner SET password_hash=?, default_password=0", (password_hash,))
                db.execute("DELETE FROM sessions")
        return password_hash is not None

    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=? AND expires>?", (digest(token), time.time())
            ).fetchone()
        return dict(row) if row else None

    def new_session(self, actor: str | None = None) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                (digest(token), digest(csrf), actor, time.time() + (28800 if actor else 600)),
            )
        return token, csrf

    def logout(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (digest(token),))

    def login(self, client: str, owner: str, password: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE created < ?", (time.time() - 900,))
            count = db.execute(
                "SELECT count(*) FROM login_attempts WHERE client=?", (client,)
            ).fetchone()[0]
            if count >= 8:
                return False
            db.execute("INSERT INTO login_attempts VALUES (?, ?)", (client, time.time()))
            row = db.execute("SELECT name, password_hash FROM owner").fetchone()
        valid = PASSWORDS.verify(password, row["password_hash"])
        return bool(valid and secrets.compare_digest(owner, row["name"]))

    def enqueue(self, command: dict[str, Any], key: str, actor: str) -> dict[str, Any]:
        payload = json.dumps(command, sort_keys=True, separators=(",", ":"))
        scope = json.dumps(
            [actor, command["kind"], command["project_id"], command.get("workflow_id")]
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM jobs WHERE scope=? AND command_key=?", (scope, key)
            ).fetchone()
            if previous:
                if previous["payload_hash"] != digest(payload):
                    raise Conflict("Idempotency key already belongs to another command")
                return dict(previous)
            identity = secrets.token_hex(16)
            db.execute(
                "INSERT INTO jobs (id,scope,command_key,payload,payload_hash,actor,status,created) VALUES (?,?,?,?,?,?,?,?)",
                (identity, scope, key, payload, digest(payload), actor, "QUEUED", time.time()),
            )
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone())

    def jobs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY seq DESC LIMIT 100")
            ]

    def job(self, identity: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError("Job not found")
        return dict(row)

    def claim(self) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE status='QUEUED' ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE jobs SET status='RUNNING', started=? WHERE id=?", (time.time(), row["id"])
            )
            return dict(row)

    def finish(
        self, identity: str, status: str, result: Any = None, error: str | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, result=?, error=?, finished=? WHERE id=?",
                (status, json.dumps(result), error, time.time(), identity),
            )

    def recover(self) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='INTERRUPTED', error='Worker stopped; review state before explicit retry' WHERE status='RUNNING'"
            )

    def retry(self, identity: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None or row[0] not in {"INTERRUPTED", "FAILED"}:
                raise Conflict("Only interrupted or failed jobs can be retried")
            db.execute(
                "UPDATE jobs SET status='QUEUED', error=NULL, finished=NULL WHERE id=?", (identity,)
            )

    def heartbeat(self) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO runtime VALUES (1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=excluded.heartbeat",
                (time.time(),),
            )

    def healthy(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT heartbeat FROM runtime WHERE id=1").fetchone()
        return bool(row and time.time() - row[0] < 15)
