from quality_platform.evaluation.models import (
    BenchmarkCase,
    CaseScore,
    EvaluationOutput,
    GoldStatus,
    Severity,
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
                and item.id in (gold.id, *gold.semantic_alternatives)
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
