from quality_platform.domain.models import Requirement
from quality_platform.evaluation.models import (
    Artifact,
    BenchmarkCase,
    CaseScore,
    EvaluationOutput,
    ExtractionScore,
    GoldCase,
    GoldRequirement,
    GoldStatus,
    Severity,
    TraceabilityScore,
)


def score_extraction(gold: list[GoldRequirement], observed: list[Requirement]) -> ExtractionScore:
    matched: list[str] = []
    review_required: list[str] = []
    used_observed: set[int] = set()
    for expected in gold:
        match = next(
            (
                (index, item)
                for index, item in enumerate(observed)
                if index not in used_observed
                and item.id in (expected.id, *expected.semantic_alternatives)
            ),
            None,
        )
        if match is None:
            continue
        index, item = match
        used_observed.add(index)
        matched.append(expected.id)
        if item.id != expected.id:
            review_required.append(expected.id)

    matched_set = set(matched)
    missed = tuple(item.id for item in gold if item.id not in matched_set)
    unexpected = tuple(item.id for index, item in enumerate(observed) if index not in used_observed)
    return ExtractionScore(
        matched_requirement_ids=tuple(matched),
        missed_requirement_ids=missed,
        unexpected_requirement_ids=unexpected,
        review_required_requirement_ids=tuple(review_required),
        precision=len(matched) / len(observed) if observed else float(not gold),
        recall=len(matched) / len(gold) if gold else 1.0,
    )


def score_traceability(gold: GoldCase, observed: Artifact) -> TraceabilityScore:
    matched: set[str] = set()
    semantic_matches: set[str] = set()
    unexpected: list[str] = []
    for requirement_id in observed.requirement_ids:
        expected = next(
            (
                item
                for item in gold.requirements
                if item.id not in matched
                and requirement_id in (item.id, *item.semantic_alternatives)
            ),
            None,
        )
        if expected is None:
            unexpected.append(requirement_id)
            continue
        matched.add(expected.id)
        if requirement_id != expected.id:
            semantic_matches.add(expected.id)

    missing = tuple(item.id for item in gold.requirements if item.id not in matched)
    supported_references = {
        reference for item in gold.requirements for reference in item.source_references
    }
    supported_count = sum(
        reference in supported_references for reference in observed.source_references
    )
    unsupported_count = len(observed.source_references) - supported_count
    evidence_denominator = max(len(observed.source_references), len(supported_references))
    evidence_accuracy = supported_count / evidence_denominator if evidence_denominator else 1.0
    return TraceabilityScore(
        requirement_coverage=(len(matched) / len(gold.requirements) if gold.requirements else 1.0),
        evidence_accuracy=evidence_accuracy,
        missing_requirement_ids=missing,
        unexpected_requirement_ids=tuple(unexpected),
        unsupported_source_count=unsupported_count,
        review_required=bool(
            missing
            or unexpected
            or unsupported_count
            or semantic_matches
            or supported_count < len(supported_references)
        ),
    )


def evaluate_case(case: BenchmarkCase, observed: EvaluationOutput) -> CaseScore:
    if case.gold.status != GoldStatus.APPROVED:
        return CaseScore(
            case_id=case.case_id,
            eligible=False,
            precision=0,
            recall=0,
            evidence_accuracy=0,
            unsupported_source_count=0,
            critical_violation=False,
            findings=("gold not approved by a human QA reviewer",),
        )

    matched: set[str] = set()
    supported_refs = 0
    observed_refs = 0
    required_refs = sum(len(item.source_references) for item in case.gold.expected_items)
    findings: list[str] = []
    for item in observed.observed_items:
        expected = next(
            (
                gold
                for gold in case.gold.expected_items
                if gold.kind == item.kind
                and (
                    item.id in (gold.id, *gold.semantic_alternatives)
                    or (
                        set(item.source_references) == set(gold.source_references)
                        and bool(gold.source_references)
                    )
                )
                and gold.id not in matched
            ),
            None,
        )
        if expected is None:
            findings.append("unexpected or duplicate item")
        else:
            matched.add(expected.id)
        for reference in item.source_references:
            observed_refs += 1
            if expected is not None and reference in expected.source_references:
                supported_refs += 1
            else:
                findings.append("unsupported source reference")

    expected_count = len(case.gold.expected_items)
    observed_count = len(observed.observed_items)
    evidence_accuracy = (
        supported_refs / max(observed_refs, required_refs)
        if observed_refs or required_refs
        else 1.0
    )
    raw_precision = len(matched) / observed_count if observed_count else float(expected_count == 0)
    precision = raw_precision * evidence_accuracy
    recall = len(matched) / expected_count if expected_count else 1.0
    forbidden = set(case.gold.forbidden_actions).intersection(observed.actions)
    wrong_state = (
        case.gold.expected_workflow_state is not None
        and observed.workflow_state != case.gold.expected_workflow_state
    )
    critical_violation = case.severity == Severity.CRITICAL and bool(forbidden or wrong_state)
    if forbidden:
        findings.append("forbidden action: " + ", ".join(sorted(forbidden)))
    if wrong_state:
        findings.append("unexpected workflow state")
    return CaseScore(
        case_id=case.case_id,
        eligible=True,
        precision=precision,
        recall=recall,
        evidence_accuracy=evidence_accuracy,
        unsupported_source_count=observed_refs - supported_refs,
        critical_violation=critical_violation,
        findings=tuple(findings),
    )
