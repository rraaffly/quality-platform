import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field
from sqlalchemy.exc import NoResultFound

from quality_platform.domain.dependencies import DependencyResolver
from quality_platform.domain.models import (
    ApprovalRecord,
    ArtifactStatus,
    ArtifactVersion,
    Record,
    Requirement,
    RequirementReadiness,
)
from quality_platform.domain.policies import Decision
from quality_platform.services.scopes import ScopeService
from quality_platform.storage.repositories import QaRepositories


class BaselineSelection(Record):
    requirement_versions: dict[str, int] = Field(min_length=1)
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    human_reviewed: bool = False


class ScopeBaseline(Record):
    id: str
    workflow_id: str
    scope_id: str
    requirement_versions: dict[str, int]
    source_snapshot_ids: tuple[str, ...]
    actor: str
    approved_at: AwareDatetime


class ApprovalResult(Record):
    approved: bool
    reasons: tuple[str, ...] = ()
    baseline: ScopeBaseline | None = None


class ApprovalInput(Record):
    approved: bool
    actor: str = Field(min_length=1)
    artifact_version: int = Field(ge=1)
    scope_id: str = Field(min_length=1)
    baseline_id: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    reviewed_evidence: tuple[str, ...] = ()


class ApprovalService:
    def __init__(
        self,
        repo: QaRepositories,
        scopes: ScopeService,
        *,
        project_id: str,
        workflow_id: str,
    ) -> None:
        self.repo = repo
        self.scopes = scopes
        self.project_id = project_id
        self.workflow_id = workflow_id
        if scopes.project_id != project_id or scopes.workflow_id != workflow_id:
            raise ValueError("scope service does not match approval workflow")
        workflow = repo.read_workflow(project_id, workflow_id)
        if workflow.project_id != project_id:
            raise PermissionError("workflow belongs to another project")

    def _requirements(self, versions: dict[str, int]) -> tuple[dict[str, Requirement], list[str]]:
        requirements: dict[str, Requirement] = {}
        reasons: list[str] = []
        for requirement_id, version in versions.items():
            if version < 1:
                reasons.append(f"invalid requirement version: {requirement_id}")
                continue
            try:
                requirement = self.repo.read_requirement_version(
                    self.project_id, requirement_id, version
                )
            except NoResultFound:
                reasons.append(f"unknown requirement version: {requirement_id} v{version}")
                continue
            requirements[requirement_id] = requirement
            if requirement.readiness != RequirementReadiness.VALIDATED:
                reasons.append(f"requirement not validated: {requirement_id}")
            if self.repo.latest_requirement_version(self.project_id, requirement_id) != version:
                reasons.append(f"stale requirement version: {requirement_id}")
        return requirements, reasons

    def _sources(
        self, selection: BaselineSelection, requirements: dict[str, Requirement]
    ) -> list[str]:
        reasons: list[str] = []
        source_lines: dict[str, list[str]] = {}
        workflow = self.repo.read_workflow(self.project_id, self.workflow_id)
        selected = set(selection.source_snapshot_ids)
        if len(selected) != len(selection.source_snapshot_ids) or selected != set(
            workflow.source_snapshot_ids
        ):
            reasons.append("source baseline differs from workflow snapshots")
        referenced = {
            reference.document_id
            for requirement in requirements.values()
            for reference in requirement.source_references
        }
        if not referenced.issubset(selected):
            reasons.append("requirement source is outside baseline")
        for source_id in selected:
            try:
                source = self.repo.read_source_document(self.project_id, source_id)
                owner = self.repo.read_project_record(self.project_id, source.project_id)
                snapshot = Path(source.snapshot_path).resolve(strict=True)
                if not snapshot.is_relative_to(owner.root.resolve()):
                    reasons.append(f"source snapshot outside owner project: {source_id}")
                elif hashlib.sha256(snapshot.read_bytes()).hexdigest() != source.content_hash:
                    reasons.append(f"source snapshot hash mismatch: {source_id}")
                else:
                    lines = snapshot.read_text(encoding="utf-8").splitlines()
                    if len(lines) != source.line_count:
                        reasons.append(f"source line count mismatch: {source_id}")
                    else:
                        source_lines[source_id] = lines
            except (NoResultFound, FileNotFoundError, PermissionError):
                reasons.append(f"source snapshot unavailable: {source_id}")
        for requirement in requirements.values():
            for reference in requirement.source_references:
                cited_lines = source_lines.get(reference.document_id)
                if cited_lines is None:
                    continue
                if reference.end_line > len(cited_lines) or not any(
                    line.strip()
                    for line in cited_lines[reference.start_line - 1 : reference.end_line]
                ):
                    reasons.append(f"source reference invalid: {requirement.id}")
        return reasons

    def _evaluate(
        self, scope_id: str, selection: BaselineSelection, *, require_approved_scope: bool
    ) -> list[str]:
        reasons: list[str] = []
        try:
            scope = self.scopes.get(scope_id)
        except KeyError:
            return ["unknown scope"]
        if require_approved_scope and scope.status != "APPROVED":
            reasons.append("approved scope required")
        requirements, requirement_reasons = self._requirements(selection.requirement_versions)
        reasons.extend(requirement_reasons)
        reasons.extend(self._sources(selection, requirements))
        resolver = DependencyResolver(
            scopes={item.id: item for item in self.scopes.list_scopes()},
            requirements=requirements,
        )
        report = resolver.compute_dependency_closure(scope_id, "selection")
        unresolved = self.scopes.unresolved_clarifications(report.required_requirement_ids)
        if unresolved:
            reasons.append("unresolved clarifications: " + ", ".join(sorted(unresolved)))
        reasons.extend(
            reason
            for reason in report.reasons
            if require_approved_scope or not reason.startswith("scope ")
        )
        if set(selection.requirement_versions) != set(report.required_requirement_ids):
            reasons.append("selection must equal dependency-closed requirement set")
        if not requirements:
            reasons.append("requirement baseline is empty")
        return reasons

    def authorize_draft(self, scope_id: str, requirement_versions: dict[str, int]) -> Decision:
        workflow = self.repo.read_workflow(self.project_id, self.workflow_id)
        selection = BaselineSelection(
            requirement_versions=requirement_versions,
            source_snapshot_ids=workflow.source_snapshot_ids,
        )
        reasons = self._evaluate(scope_id, selection, require_approved_scope=False)
        return Decision(allowed=not reasons, reasons=reasons)

    def _baseline_id(self, scope_id: str, selection: BaselineSelection) -> str:
        scope = self.scopes.get(scope_id)
        canonical = json.dumps(
            {
                "workflow_id": self.workflow_id,
                "scope": scope.model_dump(mode="json"),
                "versions": sorted(selection.requirement_versions.items()),
                "source_ids": sorted(selection.source_snapshot_ids),
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _get_baseline(self, baseline_id: str) -> ScopeBaseline | None:
        for event in self.repo.list_decisions(self.project_id):
            if event.event_type == "baseline" and event.record_id == baseline_id:
                baseline = ScopeBaseline.model_validate_json(event.payload)
                if baseline.workflow_id == self.workflow_id:
                    return baseline
        return None

    def approve_scope_baseline(
        self, scope_id: str, selection: BaselineSelection, actor: str
    ) -> ApprovalResult:
        reasons = self._evaluate(scope_id, selection, require_approved_scope=True)
        if not selection.human_reviewed or not actor.strip():
            reasons.append("human baseline review required")
        if reasons:
            return ApprovalResult(approved=False, reasons=tuple(reasons))
        baseline_id = self._baseline_id(scope_id, selection)
        existing = self._get_baseline(baseline_id)
        if existing is not None:
            return ApprovalResult(approved=True, baseline=existing)
        baseline = ScopeBaseline(
            id=baseline_id,
            workflow_id=self.workflow_id,
            scope_id=scope_id,
            requirement_versions=dict(selection.requirement_versions),
            source_snapshot_ids=tuple(selection.source_snapshot_ids),
            actor=actor,
            approved_at=datetime.now(UTC),
        )
        self.repo.append_baseline(self.project_id, baseline.id, baseline.model_dump_json())
        return ApprovalResult(approved=True, baseline=baseline)

    def authorize_artifact_review(self, artifact_id: str, baseline_id: str) -> Decision:
        baseline = self._get_baseline(baseline_id)
        if baseline is None:
            return Decision(allowed=False, reasons=["approved baseline not found"])
        selection = BaselineSelection(
            requirement_versions=baseline.requirement_versions,
            source_snapshot_ids=baseline.source_snapshot_ids,
            human_reviewed=True,
        )
        reasons = self._evaluate(baseline.scope_id, selection, require_approved_scope=True)
        if self._baseline_id(baseline.scope_id, selection) != baseline.id:
            reasons.append("scope changed since baseline approval")
        try:
            artifact = self.repo.read_artifact_version(self.project_id, artifact_id)
        except NoResultFound:
            reasons.append("artifact not found")
        else:
            if artifact.status != ArtifactStatus.READY_FOR_REVIEW:
                reasons.append("artifact not ready for review")
            if artifact.requirement_baseline_id != baseline_id:
                reasons.append("artifact baseline mismatch")
            if artifact.scope_id != baseline.scope_id:
                reasons.append("artifact scope mismatch")
            if artifact.staleness != "CURRENT":
                reasons.append("artifact is stale")
        return Decision(allowed=not reasons, reasons=reasons)

    def commit_artifact_approval(
        self, artifact_id: str, human_decision: ApprovalInput
    ) -> ArtifactVersion:
        if not human_decision.approved:
            raise PermissionError("human rejected artifact approval")
        artifact = self.repo.read_artifact_version(self.project_id, artifact_id)
        if (
            artifact.version != human_decision.artifact_version
            or artifact.scope_id != human_decision.scope_id
            or artifact.requirement_baseline_id != human_decision.baseline_id
            or artifact.content_hash != human_decision.content_hash
        ):
            raise ValueError("human approval target does not match artifact")
        if artifact.status == ArtifactStatus.APPROVED:
            return artifact
        if not human_decision.reviewed_evidence:
            raise ValueError("reviewed evidence is required for final approval")
        decision = self.authorize_artifact_review(artifact_id, human_decision.baseline_id)
        if not decision.allowed:
            raise PermissionError("artifact review is blocked: " + "; ".join(decision.reasons))
        approval_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{self.project_id}:{self.workflow_id}:{artifact.scope_id}:{artifact.id}:"
                f"{artifact.version}:{human_decision.baseline_id}",
            )
        )
        approval = ApprovalRecord(
            id=approval_id,
            actor=human_decision.actor,
            target_id=artifact.id,
            target_version=artifact.version,
            scope_id=artifact.scope_id,
            baseline_id=human_decision.baseline_id,
            reviewed_evidence=human_decision.reviewed_evidence,
            approved_at=datetime.now(UTC),
        )
        approved = artifact.model_copy(
            update={"status": ArtifactStatus.APPROVED, "approval_id": approval_id}
        )
        return self.repo.commit_artifact_approval(
            self.project_id,
            approved,
            approval,
            validate_eligibility=lambda: self.authorize_artifact_review(
                artifact_id, human_decision.baseline_id
            ),
        )
