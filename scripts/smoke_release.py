"""Exercise a fresh release installation using the public command contract."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


def run(command: list[str], *, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command, check=False, input=input_text, text=True, capture_output=True, timeout=180, env=env
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stdout}\n{result.stderr}")
    return result.stdout


def smoke(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="quality platform release ") as temporary:
        base = Path(temporary)
        with ZipFile(archive) as package:
            package.extractall(base)
        root = base / "quality-platform"
        bin_dir = base / "bin"
        environment = {**os.environ, "QUALITY_PLATFORM_BIN_DIR": str(bin_dir)}
        if sys.platform == "win32":
            run(["powershell", "-NoProfile", "-File", str(root / "scripts/install.ps1")], env=environment)
            command = [str(bin_dir / "quality-platform.cmd")]
        else:
            run(["sh", str(root / "scripts/install.sh")], env=environment)
            command = [str(bin_dir / "quality-platform")]
        help_text = run([*command, "--help"])
        for name in ("setup", "dashboard", "status", "stop"):
            assert name in help_text, name
        data = base / "private data"
        config = base / "test config.yaml"
        config.write_text(
            "database_path: " + str(data / "domain.sqlite").replace("\\", "/") + "\n"
            "workspace_root: " + str(data / "workspace").replace("\\", "/") + "\n"
            "max_concurrency: 1\nagent_models:\n"
            "  orchestrator: {provider: openai, model_id: offline}\n"
            "  analyst: {provider: openai, model_id: offline}\n"
            "  planner: {provider: openai, model_id: offline}\n"
            "  evaluator: {provider: openai, model_id: offline}\n",
            encoding="utf-8",
        )
        run(
            [*command, "setup", "--data-dir", str(data), "--config", str(config), "--owner", "qa", "--password-stdin"],
            input_text="private-test-password\n",
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            run([*command, "dashboard", "--data-dir", str(data), "--port", str(port), "--no-open"])
            assert "RUNNING" in run([*command, "status", "--data-dir", str(data)])
        finally:
            run([*command, "stop", "--data-dir", str(data)])
        assert "STOPPED" in run([*command, "status", "--data-dir", str(data)])
        try:
            run([*command, "dashboard", "--data-dir", str(data), "--port", str(port), "--no-open"])
            assert "RUNNING" in run([*command, "status", "--data-dir", str(data)])
        finally:
            run([*command, "stop", "--data-dir", str(data)])
        print("Fresh release setup/dashboard/status/stop/restart smoke passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    smoke(parser.parse_args().archive.resolve())
