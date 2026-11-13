import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from quality_platform.config.models import AgentModels, AppConfig, ModelSpec
from quality_platform.web.control import Control

ROOT = Path(__file__).resolve().parents[2]
START = ROOT / "scripts" / "start-local.ps1"
STOP = ROOT / "scripts" / "stop-local.ps1"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell launcher is Windows native")
def test_local_launcher_rejects_uninitialized_state_and_missing_build(tmp_path: Path) -> None:
    empty_state = tmp_path / "empty"
    missing_state = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(START), "-State", str(empty_state)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert missing_state.returncode != 0
    assert "not initialized" in missing_state.stderr

    fake_root = tmp_path / "empty checkout"
    scripts = fake_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(START, scripts / START.name)
    empty_state.mkdir()
    (empty_state / "control.sqlite").touch()
    missing_build = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-File",
            str(scripts / START.name),
            "-State",
            str(empty_state),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert missing_build.returncode != 0
    assert "Python environment is missing" in missing_build.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell launcher is Windows native")
def test_local_launcher_starts_one_worker_and_server_without_model_calls(tmp_path: Path) -> None:
    if not (ROOT / "web" / "dist").is_dir():
        pytest.skip("frontend build is required for launcher smoke test")
    model = ModelSpec(provider="openai", model_id="fake-offline")
    config = AppConfig(
        database_path=tmp_path / "domain.sqlite",
        workspace_root=tmp_path / "workspace",
        agent_models=AgentModels(orchestrator=model, analyst=model, planner=model, evaluator=model),
    )
    state = tmp_path / "my state"
    Control(state).initialize(config, "qa", "test-only-password")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    try:
        with (tmp_path / "start.out").open("w") as out, (tmp_path / "start.err").open("w") as err:
            started = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(START),
                    "-State",
                    str(state),
                    "-Port",
                    str(port),
                ],
                stdout=out,
                stderr=err,
                check=False,
                timeout=40,
            )
        assert started.returncode == 0, (tmp_path / "start.err").read_text()
        assert (state / "local-processes.json").is_file()
        assert "127.0.0.1" in (tmp_path / "start.out").read_text()
        with urlopen(f"http://127.0.0.1:{port}/api/session", timeout=5) as response:
            assert response.status == 200
        with (
            (tmp_path / "duplicate.out").open("w") as out,
            (tmp_path / "duplicate.err").open("w") as err,
        ):
            duplicate = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(START),
                    "-State",
                    str(state),
                    "-Port",
                    str(port),
                ],
                stdout=out,
                stderr=err,
                check=False,
                timeout=20,
            )
        assert duplicate.returncode == 0
        assert json.loads((state / "local-processes.json").read_text())["port"] == port
    finally:
        with (tmp_path / "stop.out").open("w") as out, (tmp_path / "stop.err").open("w") as err:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-File", str(STOP), "-State", str(state)],
                stdout=out,
                stderr=err,
                check=False,
                timeout=20,
            )
    for _ in range(20):
        if not (state / "local-processes.json").exists():
            break
        time.sleep(0.1)
    assert not (state / "local-processes.json").exists()
