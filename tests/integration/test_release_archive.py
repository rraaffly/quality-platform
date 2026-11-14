"""Release artifacts carry public runtime resources, never private local data."""

import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.package_release import build_archive


def test_archive_contains_runtime_and_evidence_without_private_files(tmp_path: Path) -> None:
    root = tmp_path / "source"
    required = (
        "pyproject.toml",
        "uv.lock",
        "config.yaml",
        "README.md",
        "hatch_build.py",
        "src/quality_platform/web/launcher.py",
        "tests/fixtures/srs.md",
        "benchmarks/rubrics/v0.1.json",
        "web/dist/index.html",
        "web/dist/static/js/app.js",
        "scripts/install.ps1",
        "scripts/install.sh",
        "scripts/package_release.py",
    )
    for relative in required:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(relative, encoding="utf-8")
    for relative in (
        ".env",
        ".venv/private.txt",
        "src/__pycache__/cache.pyc",
        "benchmarks/datasets/real_world/secret.json",
        "benchmarks/reports/private.json",
        "web/node_modules/package/index.js",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")

    archive = tmp_path / "quality-platform.zip"
    build_archive(root, archive)
    with ZipFile(archive) as package:
        names = set(package.namelist())
        assert {f"quality-platform/{entry}" for entry in required} <= names
        assert not any("secret" in name or "__pycache__" in name for name in names)
        assert not any("node_modules" in name or "real_world" in name for name in names)
        assert not any(".venv" in name or "/.env" in name for name in names)


def test_wheel_contains_public_resources_and_rejects_missing_frontend(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to exercise the release build")
    checkout = Path(__file__).resolve().parents[2]
    root = tmp_path / "source"
    root.mkdir()
    for name in ("pyproject.toml", "README.md", "hatch_build.py"):
        shutil.copy2(checkout / name, root / name)
    (root / ".gitignore").write_text("dist/\n.env*\nreal_world/\n", encoding="utf-8")
    required = {
        "src/quality_platform/__init__.py": "",
        "web/dist/index.html": '<div id="app"></div>',
        "web/dist/static/js/app.js": "console.log('bundled')",
        "benchmarks/configs/tier2.yaml": "candidate_id: release",
        "benchmarks/rubrics/v0.1.json": '{"version": "v0.1"}',
        "benchmarks/ground_truth/v0.1.json": '{"cases": []}',
        "benchmarks/datasets/synthetic/analyst/input.md": "# Public input",
    }
    private = (
        "src/quality_platform/__pycache__/cache.pyc",
        "src/quality_platform/.env",
        "benchmarks/ground_truth/real_world/secret.json",
        "benchmarks/datasets/real_world/secret.json",
        "benchmarks/reports/private.json",
        "benchmarks/datasets/synthetic/.env.local",
        "web/dist/.env.local",
        "web/node_modules/private.js",
        "tests/private.py",
    )
    for name, contents in {**required, **dict.fromkeys(private, "secret")}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    built = subprocess.run(
        [uv, "build", "--wheel"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    (wheel,) = (root / "dist").glob("*.whl")
    with ZipFile(wheel) as package:
        names = set(package.namelist())
        for name, contents in required.items():
            installed = (
                name.removeprefix("src/")
                if name.startswith("src/")
                else "quality_platform/_data/" + name
            )
            assert package.read(installed).decode() == contents
        assert not any(
            "real_world" in name
            or "__pycache__" in name
            or "/.env" in name
            or "private" in name
            or "node_modules" in name
            for name in names
        )
        assert not any(name.startswith("tests/") for name in names)

    (root / "web/dist/index.html").unlink()
    incomplete = subprocess.run(
        [uv, "build", "--wheel"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert incomplete.returncode != 0
    assert "web/dist/index.html is missing" in incomplete.stderr
