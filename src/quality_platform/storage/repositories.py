import json
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.engine import Engine

from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactStaleness,
    ArtifactStatus,
    ArtifactVersion,
    Clarification,
    ProjectRecord,
    Requirement,
    SourceDocument,
    WorkflowRecord,
    WorkflowStatus,
)
from quality_platform.domain.policies import Decision, PolicyContext, validate_transition
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

    def list_projects(self) -> list[ProjectRecord]:
        with self.engine.connect() as connection:
            payloads = connection.execute(
                select(projects.c.payload).order_by(projects.c.id)
            ).scalars()
            return [ProjectRecord.model_validate_json(payload) for payload in payloads]

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

    def list_workflows(
        self, project_id: str, *, actor_project_id: str | None = None
    ) -> list[WorkflowRecord]:
        self._authorize(actor_project_id or project_id, project_id, "read")
        with self.engine.connect() as connection:
            payloads = connection.execute(
                select(workflows.c.payload)
                .where(workflows.c.project_id == project_id)
                .order_by(workflows.c.id)
            ).scalars()
            return [WorkflowRecord.model_validate_json(payload) for payload in payloads]

    def update_workflow_status(
        self,
        project_id: str,
        workflow_id: str,
        status: WorkflowStatus,
        *,
        actor_project_id: str | None = None,
    ) -> WorkflowRecord:
        self._authorize(actor_project_id or project_id, project_id, "write")
        with self.engine.begin() as connection:
            row = connection.execute(
                select(workflows.c.project_id, workflows.c.payload).where(
                    workflows.c.id == workflow_id
                )
            ).one()
            if row.project_id != project_id:
                raise PermissionError("workflow belongs to another project")
            current = WorkflowRecord.model_validate_json(row.payload)
            if current.status == status:
                return current
            decision = validate_transition("workflow", current.status, status, PolicyContext())
            if not decision.allowed:
                raise ValueError("; ".join(decision.reasons))
            updated = current.model_copy(update={"status": status})
            connection.execute(
                workflows.update()
                .where(workflows.c.id == workflow_id)
                .values(payload=updated.model_dump_json())
            )
            return updated

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
            previous_payload = connection.execute(
                select(requirement_versions.c.payload)
                .where(
                    requirement_versions.c.project_id == project_id,
                    requirement_versions.c.requirement_id == requirement.id,
                )
                .order_by(requirement_versions.c.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            if previous_payload is not None:
                previous = Requirement.model_validate_json(previous_payload)
                if requirement.version > previous.version + 1:
                    raise ValueError("requirement version must advance by one")
            connection.execute(
                requirement_versions.insert().values(
                    project_id=project_id,
                    requirement_id=requirement.id,
                    version=requirement.version,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
            )
            if previous_payload is not None:
                from quality_platform.services.impact import (
                    ArtifactDependency,
                    RequirementChange,
                    detect_impact,
                )

                events = connection.execute(
                    select(audit_events)
                    .where(audit_events.c.project_id == project_id)
                    .order_by(audit_events.c.seq)
                ).all()
                baselines = {
                    row.record_id: json.loads(row.payload)["requirement_versions"]
                    for row in events
                    if row.event_type == "baseline"
                }
                traces = {
                    (row.record_id, json.loads(row.payload)["version"]): json.loads(row.payload)[
                        "requirement_versions"
                    ]
                    for row in events
                    if row.event_type == "artifact_trace"
                }
                artifacts = {
                    row.record_id: ArtifactVersion.model_validate_json(row.payload)
                    for row in events
                    if row.event_type == "artifact"
                }
                affected = tuple(
                    ArtifactDependency(
                        project_id=project_id,
                        artifact_id=item.id,
                        scope_id=item.scope_id,
                        requirement_ids=tuple(versions),
                    )
                    for item in artifacts.values()
                    if requirement.id
                    in (
                        versions := traces.get(
                            (item.id, item.version), baselines.get(item.requirement_baseline_id, {})
                        )
                    )
                    and versions[requirement.id] < requirement.version
                )
                # Readiness, evidence and dependency edits also invalidate old baselines.
                report = detect_impact(
                    RequirementChange(
                        project_id=project_id,
                        requirement_id=requirement.id,
                        from_version=previous.version,
                        to_version=requirement.version,
                        before_statement=previous.statement,
                        after_statement=requirement.statement,
                        changed_fields=tuple(
                            name
                            for name, value in previous.model_dump().items()
                            if requirement.model_dump()[name] != value
                        ),
                        traced_artifacts=affected,
                    )
                )
                connection.execute(
                    audit_events.insert().values(
                        project_id=project_id,
                        event_type="impact",
                        record_id=report.id,
                        payload=report.model_dump_json(),
                        idempotency_key=f"impact:{project_id}:{requirement.id}:{requirement.version}",
                    )
                )
        return requirement

    def list_requirements(self, project_id: str) -> list[Requirement]:
        self._authorize(project_id, project_id, "read")
        with self.engine.connect() as connection:
            payloads = connection.execute(
                select(requirement_versions.c.payload)
                .where(requirement_versions.c.project_id == project_id)
                .order_by(requirement_versions.c.version)
            ).scalars()
            latest: dict[str, Requirement] = {}
            for payload in payloads:
                item = Requirement.model_validate_json(payload)
                latest[item.id] = item
        return list(latest.values())

    def configure_workflow(
        self,
        project_id: str,
        workflow_id: str,
        *,
        source_snapshot_ids: tuple[str, ...],
        model_config_snapshot_id: str | None = None,
    ) -> WorkflowRecord:
        current = self.read_workflow(project_id, workflow_id)
        if current.project_id != project_id:
            raise PermissionError("cross-project workflow mutation denied")
        if model_config_snapshot_id is not None and current.model_config_snapshot_id not in (
            None,
            model_config_snapshot_id,
        ):
            raise ValueError("workflow model configuration is immutable; start a new workflow")
        for source_id in source_snapshot_ids:
            self.read_source_document(project_id, source_id)
        updated = current.model_copy(
            update={
                "source_snapshot_ids": source_snapshot_ids,
                "model_config_snapshot_id": model_config_snapshot_id
                or current.model_config_snapshot_id,
            }
        )
        with self.engine.begin() as connection:
            connection.execute(
                workflows.update()
                .where(workflows.c.id == workflow_id)
                .values(payload=updated.model_dump_json())
            )
        return updated

    def append_workflow_event(
        self, project_id: str, workflow_id: str, event_type: str, payload: str, *, key: str
    ) -> int:
        workflow = self.read_workflow(project_id, workflow_id)
        if workflow.project_id != project_id:
            raise PermissionError("cross-project workflow mutation denied")
        if event_type not in {
            "workflow_config",
            "analysis",
            "task_plan",
            "task_result",
            "revision_draft",
        }:
            raise ValueError("unsupported workflow event")
        seq = self._append_domain_event(
            project_id,
            event_type,
            workflow_id,
            payload,
            f"{project_id}:{workflow_id}:{event_type}:{key}",
        )
        if event_type == "task_plan":
            plan = json.loads(payload)
            with self.engine.begin() as connection:
                current = WorkflowRecord.model_validate_json(
                    connection.execute(
                        select(workflows.c.payload).where(workflows.c.id == workflow_id)
                    ).scalar_one()
                )
                task_ids = tuple(
                    dict.fromkeys(
                        (
                            *current.task_ids,
                            *(
                                f"{plan['request_id']}:{task['id']}"
                                for task in plan["proposal"]["tasks"]
                            ),
                        )
                    )
                )
                connection.execute(
                    workflows.update()
                    .where(workflows.c.id == workflow_id)
                    .values(
                        payload=current.model_copy(update={"task_ids": task_ids}).model_dump_json()
                    )
                )
        return seq

    def save_artifact_trace(
        self,
        project_id: str,
        artifact: ArtifactVersion,
        versions: dict[str, int],
        *,
        workflow_id: str | None = None,
    ) -> None:
        if workflow_id is not None:
            workflow = self.read_workflow(project_id, workflow_id)
            if workflow.project_id != project_id:
                raise PermissionError("cross-project artifact trace denied")
            owners = {
                json.loads(event.payload).get("workflow_id")
                for event in self.list_decisions(project_id)
                if event.event_type == "artifact_trace" and event.record_id == artifact.id
            } - {None}
            if owners and owners != {workflow_id}:
                raise PermissionError("artifact belongs to another workflow")
        payload = json.dumps(
            {
                "version": artifact.version,
                "requirement_versions": versions,
                **({"workflow_id": workflow_id} if workflow_id is not None else {}),
            },
            sort_keys=True,
        )
        self._append_domain_event(
            project_id,
            "artifact_trace",
            artifact.id,
            payload,
            f"trace:{project_id}:{artifact.id}:{artifact.version}",
        )

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
        self,
        project_id: str,
        artifact: ArtifactVersion,
        *,
        idempotency_key: str,
        revision_review: ApprovalRecord | None = None,
    ) -> int:
        if artifact.status == ArtifactStatus.APPROVED:
            raise ValueError("approved artifact requires transactional human approval")
        if revision_review is not None and (
            artifact.status != ArtifactStatus.DRAFT
            or revision_review.target_id != artifact.id
            or revision_review.target_version != artifact.version
            or revision_review.scope_id != artifact.scope_id
            or revision_review.baseline_id != artifact.requirement_baseline_id
            or not revision_review.reviewed_evidence
        ):
            raise ValueError("revision review does not match the draft artifact")
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
                if revision_review is not None:
                    review_payload = connection.execute(
                        select(audit_events.c.payload).where(
                            audit_events.c.idempotency_key
                            == f"revision-review:{revision_review.id}"
                        )
                    ).scalar_one()
                    previous_review = ApprovalRecord.model_validate_json(review_payload)
                    if previous_review.model_dump(
                        exclude={"approved_at"}
                    ) != revision_review.model_dump(exclude={"approved_at"}):
                        raise ValueError("revision replay differs from the reviewed approval")
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
            if revision_review is not None:
                connection.execute(
                    audit_events.insert().values(
                        project_id=project_id,
                        event_type="revision_review",
                        record_id=revision_review.id,
                        payload=revision_review.model_dump_json(),
                        idempotency_key=f"revision-review:{revision_review.id}",
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
        artifact = ArtifactVersion.model_validate_json(payload)
        versions: dict[str, int] = {}
        trace: dict[str, int] | None = None
        from quality_platform.services.impact import ImpactReport

        impacts: list[ImpactReport] = []
        for event in self.list_decisions(project_id):
            data = json.loads(event.payload)
            if (
                event.event_type == "baseline"
                and event.record_id == artifact.requirement_baseline_id
            ):
                versions = data["requirement_versions"]
            elif (
                event.event_type == "artifact_trace"
                and event.record_id == artifact_id
                and data["version"] == artifact.version
            ):
                trace = data["requirement_versions"]
            elif event.event_type == "impact" and artifact_id in data["affected_artifact_ids"]:
                impacts.append(ImpactReport.model_validate(data))
        versions = trace if trace is not None else versions
        if any(
            impact.changed_requirement_version > versions.get(impact.changed_requirement_id, 0)
            for impact in impacts
        ):
            return artifact.model_copy(update={"staleness": ArtifactStaleness.POTENTIALLY_STALE})
        return artifact

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
