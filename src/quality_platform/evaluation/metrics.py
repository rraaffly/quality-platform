import re

from quality_platform.domain.models import Requirement
from quality_platform.evaluation.models import (
    Artifact,
    BenchmarkCase,
    CaseScore,
    EvaluationOutput,
    ExpectedItem,
    ExtractionScore,
    GoldCase,
    GoldRequirement,
    GoldStatus,
    ObservedItem,
    SemanticJudgment,
    Severity,
    TaskType,
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
    if any(not item.semantic_definition for item in case.gold.expected_items):
        findings.append("gold semantic definition missing; human gold review required")
    assessment = observed.evaluator_assessment
    if assessment is not None and assessment.case_id != case.case_id:
        raise ValueError("evaluator assessment belongs to another case")
    if assessment is not None and (assessment.critical or assessment.uncertainty > 0):
        findings.append("evaluator flagged critical behavior or uncertainty; human review required")
    judgments = assessment.judgments if assessment else ()
    if any(item.review_required or item.uncertainty > 0 for item in judgments):
        findings.append("evaluator flagged semantic uncertainty or review")
    if len({item.id for item in observed.observed_items}) != len(observed.observed_items):
        findings.append("duplicate observed item identity")
    for item in observed.observed_items:
        expected = next(
            (
                gold
                for gold in case.gold.expected_items
                if gold.kind == item.kind
                and gold.semantic_definition
                and item.content.strip()
                and _semantic_match(gold, item, judgments)
                and gold.id not in matched
            ),
            None,
        )
        if expected is None:
            findings.append("unexpected or duplicate item")
        else:
            matched.add(expected.id)
            if not _exact_content(expected, item):
                # A judge is evidence, never authority to approve a release.
                findings.append("AI semantic match requires independent human adjudication")
            if any(
                judgment.expected_item_id == expected.id
                and judgment.observed_item_id == item.id
                and (judgment.review_required or judgment.uncertainty > 0)
                for judgment in judgments
            ):
                findings.append("evaluator flagged semantic uncertainty or review")
        seen_references = set()
        for reference in item.source_references:
            observed_refs += 1
            if (
                expected is not None
                and reference in expected.source_references
                and reference not in seen_references
            ):
                supported_refs += 1
            else:
                findings.append("unsupported source reference")
            seen_references.add(reference)

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
        and (observed.workflow_state is not None or observed.policy_observation_complete)
        and observed.workflow_state != case.gold.expected_workflow_state
    )
    if not observed.policy_observation_complete and (
        case.gold.forbidden_actions or case.gold.expected_workflow_state is not None
    ):
        findings.append(
            "runtime policy observation missing; task adapter cannot prove policy compliance"
        )
    # Forbidden actions are hard policy violations even in a low-severity content case.
    critical_violation = bool(forbidden) or (case.severity == Severity.CRITICAL and wrong_state)
    if forbidden:
        findings.append("forbidden action: " + ", ".join(sorted(forbidden)))
    if wrong_state:
        findings.append("unexpected workflow state")
    planner_metrics: dict[str, float] = {}
    if case.task_type == TaskType.PLANNER:
        planner_metrics["scenario_coverage"] = recall
        if observed.requirement_coverage is not None:
            planner_metrics["requirement_coverage"] = observed.requirement_coverage
        if observed.schema_valid is not None:
            planner_metrics["schema_validation"] = float(observed.schema_valid)
        review = assessment.planner_review if assessment else None
        if review is None or observed.requirement_coverage is None or observed.schema_valid is None:
            findings.append("Planner metric evidence missing; human review required")
        if review is not None:
            for name in type(review).model_fields:
                metric = getattr(review, name)
                planner_metrics[name] = metric.score
                if metric.review_required or metric.uncertainty > 0:
                    findings.append(f"Planner metric {name} requires human review")
                if metric.score > 0 if name == "redundancy" else metric.score < 1:
                    findings.append(f"Planner metric {name} not fully satisfied")
        if observed.requirement_coverage != 1 or observed.schema_valid is not True:
            findings.append("Planner structural coverage/schema not fully satisfied")
    return CaseScore(
        case_id=case.case_id,
        eligible=True,
        precision=precision,
        recall=recall,
        evidence_accuracy=evidence_accuracy,
        unsupported_source_count=observed_refs - supported_refs,
        critical_violation=critical_violation,
        findings=tuple(dict.fromkeys(findings)),
        planner_metrics=planner_metrics,
        observed_actions=observed.actions,
        observed_workflow_state=observed.workflow_state,
    )


def _normalized_content(value: str) -> str:
    # Preserve operators, negation and numbers: token overlap is not equivalence.
    return re.sub(r"\s+", " ", value).strip().casefold()


def _exact_content(expected: ExpectedItem, observed: ObservedItem) -> bool:
    return _normalized_content(observed.content) in {
        _normalized_content(value)
        for value in (expected.semantic_definition, *expected.accepted_contents)
        if value
    }


def _semantic_match(
    expected: ExpectedItem, observed: ObservedItem, judgments: tuple[SemanticJudgment, ...]
) -> bool:
    pair = [
        judgment
        for judgment in judgments
        if judgment.expected_item_id == expected.id and judgment.observed_item_id == observed.id
    ]
    if pair:
        # Conflicting/duplicate adjudications fail closed, including a negative judge on an ID hit.
        return len(pair) == 1 and pair[0].equivalent
    return _exact_content(expected, observed)
