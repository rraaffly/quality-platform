"""The distributable archive carries the Web app and offline evidence."""

from pathlib import Path
from zipfile import ZipFile

from scripts.package_release import build_archive


def test_archive_contains_runtime_and_evidence_without_private_files(tmp_path: Path) -> None:
    root = tmp_path / "source"
    required = (
        "pyproject.toml",
        "uv.lock",
        "config.yaml",
        "README.md",
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
