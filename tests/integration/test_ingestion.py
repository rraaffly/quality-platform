import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from quality_platform.documents.ingestion import ingest_markdown
from quality_platform.domain.models import ProjectRecord
from quality_platform.storage.database import open_database
from quality_platform.storage.repositories import QaRepositories

FIXTURES = Path(__file__).parents[1] / "fixtures"


def setup_project(tmp_path: Path) -> tuple[Path, QaRepositories]:
    root = tmp_path / "P1"
    root.mkdir()
    repo = QaRepositories(open_database(tmp_path / "qa.sqlite"))
    repo.create_project(ProjectRecord(id="P1", name="Project One", root=root))
    return root, repo


def test_multiple_documents_keep_separate_source_identity_and_metadata(tmp_path: Path) -> None:
    root, repo = setup_project(tmp_path)
    for name in ("brd.md", "srs.md"):
        (root / name).write_bytes((FIXTURES / name).read_bytes())

    sources = ingest_markdown("P1", [root / "brd.md", root / "srs.md"], repo, tmp_path)
    assert len({source.id for source in sources}) == 2
    assert [source.input_type for source in sources] == ["BRD", "SRS"]
    assert [source.headings for source in sources] == [("Login",), ("Login",)]
    assert "3 failed attempts" in sources[0].snapshot_path.read_text(encoding="utf-8")
    assert "5 failed attempts" in sources[1].snapshot_path.read_text(encoding="utf-8")
    assert repo.read_source_document("P1", sources[0].id) == sources[0]


def test_snapshot_survives_source_edit_and_reimport(tmp_path: Path) -> None:
    root, repo = setup_project(tmp_path)
    source_file = root / "brd.md"
    source_file.write_text("# Limit\n\nMaximum 3 attempts.\n", encoding="utf-8")
    first = ingest_markdown("P1", [source_file], repo, tmp_path)[0]
    source_file.write_text("# Limit\n\nMaximum 5 attempts.\n", encoding="utf-8")
    second = ingest_markdown("P1", [source_file], repo, tmp_path)[0]
    assert first.content_hash != second.content_hash
    assert first.snapshot_path != second.snapshot_path
    assert "3 attempts" in first.snapshot_path.read_text(encoding="utf-8")
    assert "5 attempts" in second.snapshot_path.read_text(encoding="utf-8")


def test_rejects_traversal_and_non_markdown_file(tmp_path: Path) -> None:
    root, repo = setup_project(tmp_path)
    (tmp_path / "outside.md").write_text("# Outside", encoding="utf-8")
    (root / "notes.txt").write_text("# Not Markdown", encoding="utf-8")
    with pytest.raises(ValueError, match="outside|traversal"):
        ingest_markdown("P1", [root / ".." / "outside.md"], repo, tmp_path)
    with pytest.raises(ValueError, match="Markdown"):
        ingest_markdown("P1", [root / "notes.txt"], repo, tmp_path)


def test_rejects_symlink_that_escapes_project(tmp_path: Path) -> None:
    root, repo = setup_project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside", encoding="utf-8")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation is unavailable")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "linked.md").write_text("# Outside", encoding="utf-8")
        junction = root / "junction"
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("symlink and junction creation are unavailable")
        link = junction / "linked.md"
    with pytest.raises(ValueError, match="outside"):
        ingest_markdown("P1", [link], repo, tmp_path)


def test_rejects_snapshot_directory_link_outside_project(tmp_path: Path) -> None:
    root, repo = setup_project(tmp_path)
    source = root / "brd.md"
    source.write_text("# Requirement", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = root / ".quality-platform"
    storage.mkdir()
    link = storage / "snapshots"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("symlink creation is unavailable")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("symlink and junction creation are unavailable")
    with pytest.raises(ValueError, match="snapshot.*outside"):
        ingest_markdown("P1", [source], repo, tmp_path)


def test_existing_version_one_database_gains_source_metadata_table(tmp_path: Path) -> None:
    database_path = tmp_path / "qa.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_versions (id INTEGER PRIMARY KEY, version INTEGER NOT NULL)"
        )
        connection.execute("INSERT INTO schema_versions (id, version) VALUES (1, 1)")
    engine = open_database(database_path)
    with engine.connect() as connection:
        version = connection.exec_driver_sql("SELECT version FROM schema_versions").scalar_one()
        table = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='source_documents'"
        ).scalar_one()
    assert version == 2
    assert table == "source_documents"
