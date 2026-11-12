"""Read-only projections; browser state is never authoritative for QA decisions."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from quality_platform.domain.models import ArtifactVersion
from quality_platform.domain.schemas import TestCaseSet
from quality_platform.rendering.test_cases import render_test_cases
from quality_platform.services.clarifications import ClarificationService
from quality_platform.services.scopes import ScopeService
from quality_platform.storage.ownership import open_read_only_database
from quality_platform.storage.repositories import QaRepositories


@contextmanager
def reader(database: Path) -> Iterator[QaRepositories]:
    engine = open_read_only_database(database)
    try:
        yield QaRepositories(engine)
    finally:
        engine.dispose()


def workflow_view(repo: QaRepositories, project: str, workflow: str) -> dict[str, Any]:
    item = repo.read_workflow(project, workflow)
    if item.project_id != project:
        raise PermissionError("Workflow belongs to another project")
    events = repo.list_decisions(project)
    scopes = ScopeService(repo, project_id=project, workflow_id=workflow).list_scopes()
    clarifications = ClarificationService(
        repo, project_id=project, workflow_id=workflow
    ).list_clarifications()
    artifact_ids = list(
        dict.fromkeys(
            event.record_id
            for event in events
            if event.event_type == "artifact_trace"
            and json.loads(event.payload).get("workflow_id") == workflow
        )
    )
    artifacts = [repo.read_artifact_version(project, identity) for identity in artifact_ids]
    requirements = [
        req
        for req in repo.list_requirements(project)
        if any(ref.document_id in item.source_snapshot_ids for ref in req.source_references)
    ]
    baselines = [
        json.loads(event.payload)
        for event in events
        if event.event_type == "baseline" and json.loads(event.payload)["workflow_id"] == workflow
    ]
    sources = [
        repo.read_source_document(project, identity) for identity in item.source_snapshot_ids
    ]
    result = {
        "workflow": item.model_dump(mode="json"),
        "scopes": [scope.model_dump(mode="json") for scope in scopes],
        "clarifications": [question.model_dump(mode="json") for question in clarifications],
        "requirements": [req.model_dump(mode="json") for req in requirements],
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "baselines": baselines,
        "sources": [
            {
                "id": source.id,
                "name": source.source_path.name,
                "hash": source.content_hash,
                "lines": source.line_count,
            }
            for source in sources
        ],
    }
    result["state_hash"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    result["history"] = [
        {
            "seq": event.seq,
            "type": event.event_type,
            "target": event.record_id,
            "data": json.loads(event.payload),
        }
        for event in events
        if (event.record_id == workflow and event.event_type != "workflow_config")
        or (
            event.record_id in artifact_ids
            and event.event_type in {"artifact", "approval", "revision_review"}
        )
        or (
            event.event_type == "impact"
            and set(json.loads(event.payload).get("affected_artifact_ids", [])).intersection(
                artifact_ids
            )
        )
    ]
    return result


def version(
    repo: QaRepositories, project: str, workflow: str, artifact_id: str, number: int
) -> ArtifactVersion:
    view = workflow_view(repo, project, workflow)
    if artifact_id not in {item["id"] for item in view["artifacts"]}:
        raise PermissionError("Artifact is outside this workflow")
    versions = [
        ArtifactVersion.model_validate_json(event.payload)
        for event in repo.list_decisions(project)
        if event.event_type == "artifact"
        and event.record_id == artifact_id
        and json.loads(event.payload)["version"] == number
    ]
    if not versions:
        raise KeyError("Artifact version not found")
    return versions[-1]


def artifact_content(
    repo: QaRepositories, project: str, artifact: ArtifactVersion
) -> dict[str, str]:
    root = repo.read_project_record(project, project).root.resolve()
    canonical = "test-cases.json" if artifact.kind == "TEST_CASES" else "test-plan.md"
    path = (root / "artifacts" / artifact.id / f"v{artifact.version}" / canonical).resolve(
        strict=True
    )
    if not path.is_relative_to(root):
        raise PermissionError("Artifact escapes project root")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact.content_hash:
        raise ValueError("Artifact hash mismatch")
    if artifact.kind == "TEST_CASES":
        cases = TestCaseSet.model_validate_json(raw)
        return render_test_cases(list(cases.cases), "both")
    return {"markdown": raw.decode("utf-8")}


def source_content(repo: QaRepositories, project: str, workflow: str, identity: str) -> str:
    item = repo.read_workflow(project, workflow)
    if identity not in item.source_snapshot_ids:
        raise PermissionError("Source is outside this workflow")
    source = repo.read_source_document(project, identity)
    root = repo.read_project_record(project, project).root.resolve()
    path = source.snapshot_path.resolve(strict=True)
    if not path.is_relative_to(root):
        raise PermissionError("Source escapes project root")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.content_hash:
        raise ValueError("Source hash mismatch")
    return raw.decode("utf-8")
