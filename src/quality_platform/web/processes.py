"""Cross-platform background worker and loopback server lifecycle."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import psutil
from filelock import FileLock

from quality_platform.evaluation.reporting import _atomic_write, _restrict_private_path


def _marker(state: Path) -> Path:
    return state.resolve() / "local-processes.json"


def _lock(state: Path) -> FileLock:
    return FileLock(state.resolve() / "dashboard.lock", timeout=20)


def _process(entry: dict[str, Any]) -> psutil.Process | None:
    try:
        process = psutil.Process(int(entry["pid"]))
        created = float(entry["created"])
        if abs(process.create_time() - created) > 0.01:
            raise ValueError("Recorded PID creation time changed; refusing to stop another process")
        return process if process.is_running() else None
    except psutil.NoSuchProcess:
        return None


def _healthy(url: str) -> bool:
    try:
        with urlopen(url + "/api/session", timeout=1) as response:
            return bool(json.load(response).get("worker_healthy"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def _status_unlocked(state: Path) -> dict[str, object]:
    path = _marker(state)
    if not path.exists():
        return {"state": "STOPPED", "logs": str(state / "logs")}
    record = json.loads(path.read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{record['port']}"
    try:
        worker = _process(record["worker"])
        server = _process(record["server"])
    except ValueError:
        return {"state": "STALE", "url": url, "logs": str(state / "logs")}
    running = worker is not None and server is not None and _healthy(url)
    return {
        "state": "RUNNING" if running else "DEGRADED",
        "url": url,
        "worker_alive": worker is not None,
        "server_alive": server is not None,
        "logs": str(state / "logs"),
    }


def status(state: Path) -> dict[str, object]:
    state = state.resolve()
    if not (state / "control.sqlite").exists():
        return {"state": "NOT_SETUP"}
    with _lock(state):
        return _status_unlocked(state)


def _launch(state: Path, role: str, port: int, logs: Path) -> subprocess.Popen[bytes]:
    command = [sys.executable, "-m", "quality_platform.web.cli", role, "--state", str(state)]
    if role == "serve":
        command.extend(["--host", "127.0.0.1", "--port", str(port)])
    options: dict[str, Any] = {"start_new_session": os.name != "nt"}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with (logs / f"{role}.log").open("ab") as output:
        return subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **options,
        )


def _terminate(process: psutil.Process) -> None:
    if not process.is_running():
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def start(state: Path, port: int = 8000) -> str:
    state = state.resolve()
    if not (state / "control.sqlite").is_file():
        raise ValueError("Quality Platform is not initialized; run quality-platform setup")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    frontend = Path(__file__).resolve().parents[3] / "web" / "dist" / "index.html"
    if not frontend.is_file():
        raise ValueError("Frontend build is missing from this installation")
    with _lock(state):
        prior = _status_unlocked(state)
        if prior["state"] == "RUNNING":
            return str(prior["url"])
        if prior["state"] != "STOPPED":
            raise RuntimeError("Recorded service is degraded or stale; run quality-platform stop")
        logs = state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _restrict_private_path(logs, 0o700)
        children: list[subprocess.Popen[bytes]] = []
        path = _marker(state)
        try:
            worker = _launch(state, "worker", port, logs)
            children.append(worker)
            server = _launch(state, "serve", port, logs)
            children.append(server)
            record = {
                "port": port,
                "worker": {"pid": worker.pid, "created": psutil.Process(worker.pid).create_time()},
                "server": {"pid": server.pid, "created": psutil.Process(server.pid).create_time()},
            }
            _atomic_write(path, json.dumps(record, sort_keys=True))
            _restrict_private_path(path, 0o600)
            url = f"http://127.0.0.1:{port}"
            for _ in range(80):
                if any(child.poll() is not None for child in children):
                    break
                if _healthy(url):
                    return url
                time.sleep(0.25)
            raise RuntimeError(f"Dashboard did not become healthy; inspect logs in {logs}")
        except Exception:
            for child in reversed(children):
                if child.poll() is None:
                    _terminate(psutil.Process(child.pid))
            path.unlink(missing_ok=True)
            raise


def stop(state: Path) -> bool:
    state = state.resolve()
    path = _marker(state)
    if not (state / "control.sqlite").exists():
        raise ValueError("Quality Platform is not initialized")
    with _lock(state):
        if not path.exists():
            return False
        record = json.loads(path.read_text(encoding="utf-8"))
        # Validate every live identity before terminating either child.
        processes = [_process(record[name]) for name in ("server", "worker")]
        for process in processes:
            if process is not None:
                _terminate(process)
        path.unlink()
        return True
