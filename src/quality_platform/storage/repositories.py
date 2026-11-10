from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.engine import Engine

from quality_platform.domain.models import (
    ApprovalRecord,
    Clarification,
    ProjectRecord,
    Requirement,
    WorkflowRecord,
)
from quality_platform.domain.policies import Decision
from quality_platform.storage.database import (
    audit_events,
    projects,
    read_grants,
    requirement_versions,
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
