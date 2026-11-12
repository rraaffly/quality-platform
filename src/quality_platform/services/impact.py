import hashlib
import json

from pydantic import Field, model_validator

from quality_platform.domain.models import ArtifactStaleness, Record


class ArtifactDependency(Record):
    project_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    requirement_ids: tuple[str, ...] = Field(min_length=1)


class SemanticDependency(Record):
    project_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    confidence: float = Field(gt=0, le=1)


class RequirementChange(Record):
    project_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=2)
    before_statement: str = Field(min_length=1)
    after_statement: str = Field(min_length=1)
    changed_fields: tuple[str, ...] = ()
    traced_artifacts: tuple[ArtifactDependency, ...] = ()
    semantic_dependencies: tuple[SemanticDependency, ...] = ()

    @model_validator(mode="after")
    def version_advances(self) -> "RequirementChange":
        if self.to_version <= self.from_version:
            raise ValueError("requirement version must advance")
        if self.before_statement == self.after_statement and not self.changed_fields:
            raise ValueError("requirement change needs different statements")
        return self


class ImpactFinding(Record):
    artifact_id: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)
    match_type: str = Field(pattern="^(TRACEABILITY|SEMANTIC)$")
    reasoning: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    uncertain: bool


class ImpactReport(Record):
    id: str = Field(min_length=1)
    changed_requirement_id: str = Field(min_length=1)
    changed_requirement_version: int = Field(ge=1)
    affected_artifact_ids: tuple[str, ...]
    affected_scope_ids: tuple[str, ...]
    findings: tuple[ImpactFinding, ...]
    reasoning: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    manual_disposition: str | None = None
    staleness: ArtifactStaleness = ArtifactStaleness.POTENTIALLY_STALE


def detect_impact(change: RequirementChange) -> ImpactReport:
    if any(
        candidate.project_id != change.project_id for candidate in change.traced_artifacts
    ) or any(
        candidate.project_id != change.project_id for candidate in change.semantic_dependencies
    ):
        raise ValueError("cross-project impact data is not allowed")

    direct = {
        artifact.artifact_id: ImpactFinding(
            artifact_id=artifact.artifact_id,
            scope_id=artifact.scope_id,
            match_type="TRACEABILITY",
            reasoning=(
                f"Artifact explicitly traces requirement {change.requirement_id} version "
                f"{change.from_version}."
            ),
            confidence=1,
            uncertain=False,
        )
        for artifact in change.traced_artifacts
        if change.requirement_id in artifact.requirement_ids
    }
    semantic = {
        candidate.artifact_id: ImpactFinding(
            artifact_id=candidate.artifact_id,
            scope_id=candidate.scope_id,
            match_type="SEMANTIC",
            reasoning=candidate.reason,
            confidence=candidate.confidence,
            uncertain=True,
        )
        for candidate in change.semantic_dependencies
        if candidate.artifact_id not in direct
    }
    findings = tuple(
        sorted((*direct.values(), *semantic.values()), key=lambda item: item.artifact_id)
    )
    reasoning = (
        f"Requirement {change.requirement_id} changed from "
        f"{change.before_statement!r} to {change.after_statement!r}. "
        "Affected artifacts are marked potentially stale; no artifact content was revised."
    )
    canonical = json.dumps(
        {
            "project_id": change.project_id,
            "requirement_id": change.requirement_id,
            "from_version": change.from_version,
            "to_version": change.to_version,
            "before": change.before_statement,
            "after": change.after_statement,
            "findings": [finding.model_dump(mode="json") for finding in findings],
        },
        sort_keys=True,
    )
    return ImpactReport(
        id=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        changed_requirement_id=change.requirement_id,
        changed_requirement_version=change.to_version,
        affected_artifact_ids=tuple(finding.artifact_id for finding in findings),
        affected_scope_ids=tuple(dict.fromkeys(finding.scope_id for finding in findings)),
        findings=findings,
        reasoning=reasoning,
        confidence=min((finding.confidence for finding in findings), default=1),
    )
