"""Exercise a fresh release installation using the public command contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from functools import partial
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile


def run(
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    result = subprocess.run(
        command,
        check=False,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
        cwd=cwd,
    )
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed: {result.stdout}\n{result.stderr}")
    return result.stdout


def smoke(archive: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="quality platform release ") as temporary:
        base = Path(temporary)
        bin_dir = base / "bin"
        home = base / "home"
        home.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "UV_CACHE_DIR": run(["uv", "cache", "dir"]).strip(),
            "UV_PYTHON_INSTALL_DIR": run(["uv", "python", "dir"]).strip(),
            "QUALITY_PLATFORM_BIN_DIR": str(bin_dir),
            "UV_TOOL_DIR": str(base / "tools"),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        }
        for key in ("PYTHONPATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
            environment.pop(key, None)
        execute = partial(run, env=environment, cwd=base)
        if archive.suffix == ".whl":
            execute(["uv", "tool", "install", "--python", "3.12", str(archive)])
            command = [
                str(bin_dir / ("quality-platform.exe" if os.name == "nt" else "quality-platform"))
            ]
            python = (
                base
                / "tools"
                / "quality-platform"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            execute(
                [
                    str(python),
                    "-I",
                    "-c",
                    """
import hashlib
from quality_platform.evaluation.registry import load_dataset
from quality_platform.evaluation.workflow_evidence import code_fingerprint
from quality_platform.resources import runtime_root
from quality_platform.web.evaluation import EvaluationEstimateQuery, estimate_benchmark

root = runtime_root()
assert root.name == '_data', root
dataset = load_dataset('v0.1', root / 'benchmarks')
assert dataset.cases
for case in dataset.cases:
    for snapshot_id, expected in case.input_snapshot_hashes.items():
        matches = list((root / 'benchmarks/datasets/synthetic').rglob(snapshot_id + '.md'))
        assert len(matches) == 1, snapshot_id
        assert hashlib.sha256(matches[0].read_bytes()).hexdigest() == expected
quote = estimate_benchmark(EvaluationEstimateQuery(tier=2, dataset_version='v0.1'))
assert quote['case_count'] > 0 and quote['reservation_usd'] > 0
assert code_fingerprint() != hashlib.sha256(b'{}').hexdigest()
""",
                ]
            )
        else:
            with ZipFile(archive) as package:
                package.extractall(base)
            root = base / "quality-platform"
            if sys.platform == "win32":
                execute(["powershell", "-NoProfile", "-File", str(root / "scripts/install.ps1")])
                command = [str(bin_dir / "quality-platform.cmd")]
            else:
                execute(["sh", str(root / "scripts/install.sh")])
                command = [str(bin_dir / "quality-platform")]
        help_text = execute([*command, "--help"])
        for name in ("setup", "config", "dashboard", "status", "stop"):
            assert name in help_text, name
        initialized = execute([*command, "setup"])
        assert str(home / ".quality-platform") in initialized
        assert (home / ".quality-platform/state/control.sqlite").is_file()
        assert "already initialized" in execute([*command, "setup"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            execute([*command, "dashboard", "--port", str(port), "--no-open"])
            assert "RUNNING" in execute([*command, "status"])
            url = f"http://127.0.0.1:{port}"
            with urlopen(url + "/login", timeout=5) as response:
                page = response.read().decode()
                assert response.status == 200 and '<div id="app"' in page
            assets = re.findall(r'(?:src|href)="(/static/[^\"]+)"', page)
            assert assets, "Frontend HTML must reference bundled assets"
            for asset in assets:
                with urlopen(url + asset, timeout=5) as response:
                    assert response.status == 200 and response.read()
            with urlopen(url + "/api/session", timeout=5) as response:
                session = json.load(response)
                assert session["worker_healthy"] and session["default_password"]
                assert session["display_name"] == "Owner"
        finally:
            execute([*command, "stop"])
        assert "STOPPED" in execute([*command, "status"])
        execute([*command, "config", "model"], input_text="openai\noffline\n" * 4)
        workspace = base / "custom workspace"
        execute([*command, "config", "workspace", str(workspace)])
        assert workspace.is_dir()
        try:
            execute([*command, "dashboard", "--port", str(port), "--no-open"])
            assert "RUNNING" in execute([*command, "status"])
        finally:
            execute([*command, "stop"])
        print("Fresh release setup/config/dashboard/assets/status/stop/restart smoke passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Release wheel or source ZIP")
    args = parser.parse_args()
    smoke(args.archive.resolve())
