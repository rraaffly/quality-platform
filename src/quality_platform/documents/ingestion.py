import re
from pathlib import Path
from uuid import uuid4

from quality_platform.documents.markdown import parse_markdown
from quality_platform.domain.models import SourceDocument
from quality_platform.storage.artifacts import publish_source_snapshot
from quality_platform.storage.repositories import QaRepositories


def _document_type(path: Path) -> str:
    tokens = set(re.split(r"[-_.\s]+", path.stem.lower()))
    for name in ("brd", "prd", "srs"):
        if name in tokens:
            return name.upper()
    if "story" in tokens:
        return "USER_STORY"
    return "MARKDOWN"


def ingest_markdown(
    project_id: str, paths: list[Path], repo: QaRepositories, workspace: Path
) -> list[SourceDocument]:
    workspace_root = workspace.resolve()
    project_root = repo.read_project_record(project_id, project_id).root.resolve()
    if not project_root.is_relative_to(workspace_root):
        raise ValueError("project root is outside workspace")

    prepared: list[tuple[Path, bytes, str]] = []
    for path in paths:
        if ".." in path.parts:
            raise ValueError("path traversal is not allowed")
        candidate = path if path.is_absolute() else workspace_root / path
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(project_root):
            raise ValueError("source is outside project root")
        if resolved.suffix.lower() != ".md":
            raise ValueError("only Markdown .md input is supported")
        raw = resolved.read_bytes()
        content = raw.decode("utf-8")
        prepared.append((resolved, raw, content))

    documents: list[SourceDocument] = []
    for source_path, raw, content in prepared:
        snapshot_path, digest = publish_source_snapshot(project_root, raw)
        source_id = str(uuid4())
        blocks = parse_markdown(content, source_id)
        document = SourceDocument(
            id=source_id,
            project_id=project_id,
            input_type=_document_type(source_path),
            content_hash=digest,
            source_path=source_path,
            snapshot_path=snapshot_path,
            line_count=len(content.splitlines()),
            headings=tuple(block.headings[-1] for block in blocks if block.kind == "heading"),
        )
        repo.save_source_document(document)
        documents.append(document)
    return documents
