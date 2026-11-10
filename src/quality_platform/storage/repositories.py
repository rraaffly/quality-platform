from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.engine import Engine

from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ProjectRecord,
    Requirement,
    SourceDocument,
    WorkflowRecord,
)
from quality_platform.domain.policies import Decision
from quality_platform.storage.database import (
    audit_events,
    projects,
    read_grants,
    requirement_versions,
    source_documents,
    workflows,
)


class Grant(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_project_id: str
    grantee_project_id: str


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    event_type: str
    record_id: str
    payload: str


def check_project_access(
    actor_project_id: str, target_project_id: str, operation: str, grant: Grant | None
) -> Decision:
    if operation not in {"read", "write"}:
        return Decision(allowed=False, reasons=[f"unsupported operation: {operation}"])
    if actor_project_id == target_project_id:
        return Decision(allowed=True, reasons=[])
    if (
        operation == "read"
        and grant is not None
        and grant.owner_project_id == target_project_id
        and grant.grantee_project_id == actor_project_id
    ):
        return Decision(allowed=True, reasons=[])
    return Decision(allowed=False, reasons=["cross-project access denied"])


class QaRepositories:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _authorize(self, actor_project_id: str, target_project_id: str, operation: str) -> None:
        grant = None
        if operation == "read" and actor_project_id != target_project_id:
            with self.engine.connect() as connection:
                row = connection.execute(
                    select(read_grants).where(
                        read_grants.c.owner_project_id == target_project_id,
                        read_grants.c.grantee_project_id == actor_project_id,
                    )
                ).first()
                if row is not None:
                    grant = Grant(
                        owner_project_id=target_project_id, grantee_project_id=actor_project_id
                    )
        decision = check_project_access(actor_project_id, target_project_id, operation, grant)
        if not decision.allowed:
            raise PermissionError("; ".join(decision.reasons))

    def create_project(self, project: ProjectRecord) -> ProjectRecord:
        with self.engine.begin() as connection:
            connection.execute(
                projects.insert().values(id=project.id, payload=project.model_dump_json())
            )
        return project

    def read_project_record(self, actor_project_id: str, target_project_id: str) -> ProjectRecord:
        self._authorize(actor_project_id, target_project_id, "read")
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(projects.c.payload).where(projects.c.id == target_project_id)
            ).scalar_one()
        return ProjectRecord.model_validate_json(payload)

    def create_workflow(
        self, workflow: WorkflowRecord, *, actor_project_id: str | None = None
    ) -> WorkflowRecord:
        self._authorize(actor_project_id or workflow.project_id, workflow.project_id, "write")
        with self.engine.begin() as connection:
            connection.execute(
                workflows.insert().values(
                    id=workflow.id,
                    project_id=workflow.project_id,
                    thread_id=workflow.thread_id,
                    payload=workflow.model_dump_json(),
                )
            )
        return workflow

    def read_workflow(self, actor_project_id: str, workflow_id: str) -> WorkflowRecord:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(workflows.c.project_id, workflows.c.payload).where(
                    workflows.c.id == workflow_id
                )
            ).one()
        self._authorize(actor_project_id, row.project_id, "read")
        return WorkflowRecord.model_validate_json(row.payload)

    def save_requirement_version(
        self,
        project_id: str,
        requirement: Requirement,
        *,
        idempotency_key: str,
        actor_project_id: str | None = None,
    ) -> Requirement:
        self._authorize(actor_project_id or project_id, project_id, "write")
        payload = requirement.model_dump_json()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(requirement_versions.c.project_id, requirement_versions.c.payload).where(
                    requirement_versions.c.idempotency_key == idempotency_key
                )
            ).first()
            if existing is not None:
                if existing.project_id != project_id or existing.payload != payload:
                    raise ValueError("idempotency key reused with different requirement")
                return requirement
            connection.execute(
                requirement_versions.insert().values(
                    project_id=project_id,
                    requirement_id=requirement.id,
                    version=requirement.version,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
            )
        return requirement

    def read_requirement_version(
        self,
        actor_project_id: str,
        requirement_id: str,
        version: int,
        *,
        target_project_id: str | None = None,
    ) -> Requirement:
        project_id = target_project_id or actor_project_id
        self._authorize(actor_project_id, project_id, "read")
        with self.engine.connect() as connection:
            row = connection.execute(
                select(requirement_versions.c.project_id, requirement_versions.c.payload).where(
                    requirement_versions.c.project_id == project_id,
                    requirement_versions.c.requirement_id == requirement_id,
                    requirement_versions.c.version == version,
                )
            ).one()
        return Requirement.model_validate_json(row.payload)

    def latest_requirement_version(self, project_id: str, requirement_id: str) -> int | None:
        self._authorize(project_id, project_id, "read")
        with self.engine.connect() as connection:
            return connection.execute(
                select(requirement_versions.c.version)
                .where(
                    requirement_versions.c.project_id == project_id,
                    requirement_versions.c.requirement_id == requirement_id,
                )
                .order_by(requirement_versions.c.version.desc())
                .limit(1)
            ).scalar_one_or_none()

    def append_decision(
        self,
        project_id: str,
        event: ApprovalRecord | Clarification,
        *,
        idempotency_key: str,
        actor_project_id: str | None = None,
    ) -> int:
        self._authorize(actor_project_id or project_id, project_id, "write")
        payload = event.model_dump_json()
        event_type = "approval" if isinstance(event, ApprovalRecord) else "clarification"
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.event_type != event_type
                    or existing.record_id != event.id
                    or existing.payload != payload
                ):
                    raise ValueError("idempotency key reused with different decision")
                return existing.seq
            result = connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type=event_type,
                    record_id=event.id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError("audit event insert did not return a primary key")
            return int(primary_key[0])

    def list_decisions(
        self, project_id: str, *, actor_project_id: str | None = None
    ) -> list[AuditEvent]:
        self._authorize(actor_project_id or project_id, project_id, "read")
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(audit_events)
                .where(audit_events.c.project_id == project_id)
                .order_by(audit_events.c.seq)
            ).all()
        return [
            AuditEvent(
                seq=row.seq,
                event_type=row.event_type,
                record_id=row.record_id,
                payload=row.payload,
            )
            for row in rows
        ]

    def append_scope_change(
        self,
        project_id: str,
        change_id: str,
        payload: str,
        *,
        idempotency_key: str,
        actor_project_id: str | None = None,
    ) -> int:
        self._authorize(actor_project_id or project_id, project_id, "write")
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.event_type != "scope_change"
                    or existing.record_id != change_id
                    or existing.payload != payload
                ):
                    raise ValueError("idempotency key reused with different scope change")
                return int(existing.seq)
            result = connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type="scope_change",
                    record_id=change_id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError("scope change insert did not return a primary key")
            return int(primary_key[0])

    def _append_domain_event(
        self,
        project_id: str,
        event_type: str,
        record_id: str,
        payload: str,
        idempotency_key: str,
    ) -> int:
        self._authorize(project_id, project_id, "write")
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == idempotency_key)
            ).first()
            if existing is not None:
                if (
                    existing.project_id != project_id
                    or existing.event_type != event_type
                    or existing.record_id != record_id
                    or existing.payload != payload
                ):
                    raise ValueError("idempotency key reused with different domain event")
                return int(existing.seq)
            result = connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type=event_type,
                    record_id=record_id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError("domain event insert did not return a primary key")
            return int(primary_key[0])

    def append_baseline(self, project_id: str, baseline_id: str, payload: str) -> int:
        return self._append_domain_event(
            project_id, "baseline", baseline_id, payload, f"baseline:{project_id}:{baseline_id}"
        )

    def save_artifact_version(
        self, project_id: str, artifact: ArtifactVersion, *, idempotency_key: str
    ) -> int:
        if artifact.status == ArtifactStatus.APPROVED:
            raise ValueError("approved artifact requires transactional human approval")
        self._authorize(project_id, project_id, "write")
        payload = artifact.model_dump_json()
        with self.engine.begin() as connection:
            previous_key = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == idempotency_key)
            ).first()
            if previous_key is not None:
                if (
                    previous_key.project_id != project_id
                    or previous_key.event_type != "artifact"
                    or previous_key.record_id != artifact.id
                    or previous_key.payload != payload
                ):
                    raise ValueError("idempotency key reused with different artifact")
                return int(previous_key.seq)
            current = connection.execute(
                select(audit_events)
                .where(
                    audit_events.c.project_id == project_id,
                    audit_events.c.event_type == "artifact",
                    audit_events.c.record_id == artifact.id,
                )
                .order_by(audit_events.c.seq.desc())
                .limit(1)
            ).first()
            if current is None:
                if artifact.version != 1:
                    raise ValueError("first artifact version must be 1")
            else:
                previous = ArtifactVersion.model_validate_json(current.payload)
                if artifact.version < previous.version or artifact.version > previous.version + 1:
                    raise ValueError("artifact version must advance by one")
                if artifact.version == previous.version:
                    if artifact.content_hash != previous.content_hash:
                        raise ValueError("artifact content is immutable within a version")
                    if (
                        artifact.kind != previous.kind
                        or artifact.scope_id != previous.scope_id
                        or artifact.requirement_baseline_id != previous.requirement_baseline_id
                    ):
                        raise ValueError("artifact target is immutable within a version")
                    if previous.status == ArtifactStatus.APPROVED:
                        raise ValueError("approved artifact version is immutable")
                    if (
                        previous.status == ArtifactStatus.READY_FOR_REVIEW
                        and artifact.status != ArtifactStatus.READY_FOR_REVIEW
                    ):
                        raise ValueError("ready artifact cannot return to draft")
                    if payload == current.payload:
                        return int(current.seq)
            result = connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type="artifact",
                    record_id=artifact.id,
                    payload=payload,
                    idempotency_key=idempotency_key,
                )
            )
            primary_key = result.inserted_primary_key
            if primary_key is None:
                raise RuntimeError("artifact insert did not return a primary key")
            return int(primary_key[0])

    def read_artifact_version(self, project_id: str, artifact_id: str) -> ArtifactVersion:
        self._authorize(project_id, project_id, "read")
        with self.engine.connect() as connection:
            payload = connection.execute(
                select(audit_events.c.payload)
                .where(
                    audit_events.c.project_id == project_id,
                    audit_events.c.event_type == "artifact",
                    audit_events.c.record_id == artifact_id,
                )
                .order_by(audit_events.c.seq.desc())
                .limit(1)
            ).scalar_one()
        return ArtifactVersion.model_validate_json(payload)

    def commit_artifact_approval(
        self,
        project_id: str,
        artifact: ArtifactVersion,
        approval: ApprovalRecord,
        *,
        validate_eligibility: Callable[[], Decision],
    ) -> ArtifactVersion:
        self._authorize(project_id, project_id, "write")
        if (
            artifact.status != ArtifactStatus.APPROVED
            or artifact.approval_id != approval.id
            or artifact.id != approval.target_id
            or artifact.version != approval.target_version
            or artifact.scope_id != approval.scope_id
            or artifact.requirement_baseline_id != approval.baseline_id
        ):
            raise ValueError("artifact and approval target mismatch")
        approval_key = f"approval:{approval.id}"
        artifact_key = (
            f"artifact-approved:{project_id}:{artifact.id}:{artifact.version}:{approval.id}"
        )
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            previous_approval = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == approval_key)
            ).first()
            previous_artifact = connection.execute(
                select(audit_events).where(audit_events.c.idempotency_key == artifact_key)
            ).first()
            if previous_approval is not None or previous_artifact is not None:
                if previous_approval is None or previous_artifact is None:
                    raise ValueError("approval replay differs from committed version")
                persisted_approval = ApprovalRecord.model_validate_json(previous_approval.payload)
                persisted_artifact = ArtifactVersion.model_validate_json(previous_artifact.payload)
                if (
                    persisted_approval.target_id != approval.target_id
                    or persisted_approval.target_version != approval.target_version
                    or persisted_approval.scope_id != approval.scope_id
                    or persisted_approval.baseline_id != approval.baseline_id
                    or persisted_artifact.id != artifact.id
                    or persisted_artifact.version != artifact.version
                    or persisted_artifact.content_hash != artifact.content_hash
                    or persisted_artifact.requirement_baseline_id
                    != artifact.requirement_baseline_id
                    or persisted_artifact.status != ArtifactStatus.APPROVED
                ):
                    raise ValueError("approval replay differs from committed target")
                return persisted_artifact
            ready_payload = connection.execute(
                select(audit_events.c.payload)
                .where(
                    audit_events.c.project_id == project_id,
                    audit_events.c.event_type == "artifact",
                    audit_events.c.record_id == artifact.id,
                )
                .order_by(audit_events.c.seq.desc())
                .limit(1)
            ).scalar_one()
            ready = ArtifactVersion.model_validate_json(ready_payload)
            if (
                ready.status != ArtifactStatus.READY_FOR_REVIEW
                or ready.version != artifact.version
                or ready.content_hash != artifact.content_hash
                or ready.scope_id != artifact.scope_id
                or ready.requirement_baseline_id != artifact.requirement_baseline_id
            ):
                raise ValueError("artifact review target changed")
            decision = validate_eligibility()
            if not decision.allowed:
                raise PermissionError("artifact review is blocked: " + "; ".join(decision.reasons))
            connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type="approval",
                    record_id=approval.id,
                    payload=approval.model_dump_json(),
                    idempotency_key=approval_key,
                )
            )
            connection.execute(
                audit_events.insert().values(
                    project_id=project_id,
                    event_type="artifact",
                    record_id=artifact.id,
                    payload=artifact.model_dump_json(),
                    idempotency_key=artifact_key,
                )
            )
            return artifact

    def grant_cross_project_read(self, owner_project_id: str, grantee_project_id: str) -> Grant:
        if owner_project_id == grantee_project_id:
            raise ValueError("cross-project grant requires two different projects")
        grant = Grant(owner_project_id=owner_project_id, grantee_project_id=grantee_project_id)
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(read_grants).where(
                    read_grants.c.owner_project_id == owner_project_id,
                    read_grants.c.grantee_project_id == grantee_project_id,
                )
            ).first()
            if existing is None:
                connection.execute(
                    read_grants.insert().values(
                        owner_project_id=owner_project_id, grantee_project_id=grantee_project_id
                    )
                )
        return grant

    def save_source_document(
        self, document: SourceDocument, *, actor_project_id: str | None = None
    ) -> SourceDocument:
        self._authorize(actor_project_id or document.project_id, document.project_id, "write")
        with self.engine.begin() as connection:
            connection.execute(
                source_documents.insert().values(
                    id=document.id,
                    project_id=document.project_id,
                    content_hash=document.content_hash,
                    payload=document.model_dump_json(),
                )
            )
        return document

    def read_source_document(self, actor_project_id: str, source_id: str) -> SourceDocument:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(source_documents.c.project_id, source_documents.c.payload).where(
                    source_documents.c.id == source_id
                )
            ).one()
        self._authorize(actor_project_id, row.project_id, "read")
        return SourceDocument.model_validate_json(row.payload)
