# Quality Platform

Local-first QA orchestration for requirement analysis, scope review, test planning, canonical
test cases, approval workflows, targeted revision, impact detection, and evaluation.

## Status

The MVP workflow and offline safety suite are implemented. The bundled benchmark is intentionally
synthetic-first: no private real-world fixtures are committed, numeric quality gates remain
illustrative, and release readiness is not claimed until approved real cases and calibrated human
review evidence exist.

Evaluation now preserves semantic content, rejects source/ID-only semantic matches, runs production
Analyst/Planner task adapters with a static evaluator role, and saves per-repeat scores and variance.
Explicit offline CLI commands capture independent human ratings, calculate evaluator calibration,
compare minimum gates/latest-approved baseline, record release review, and promote only explicitly
approved evidence. Equivalent-task productivity requires acceptance identities and review/rework time.
The existing gold approvals were not changed: approved `v0.1` cases without semantic definitions
fail closed for human review. Tier 3 remains a task benchmark, not per-case full workflow E2E.
`benchmark capture-workflow-evidence` actually runs the offline workflow/policy suites;
`benchmark attach-workflow-evidence` binds their JUnit results and current source fingerprint to
the report. Release/promotion revalidate this evidence: stale or incomplete results cannot clear
`E2E_WORKFLOW_EVIDENCE_MISSING`, and failed policy tests are `BLOCKED`. See
[benchmarking and explicit release commands](docs/benchmarking.md) for the evidence path and limits.

## Setup

```bash
uv sync --dev
uv run qa-orchestrator --help
uv run pytest -q
```

Configuration fixes one provider/model per agent role (`orchestrator`, `analyst`, `planner`, and
`evaluator`). Provider credentials stay in environment variables and are never written to model
snapshots or benchmark reports.

## Documentation

- [CLI workflows](docs/cli.md)
- [Benchmarking and release readiness](docs/benchmarking.md)

Tier 2 and Tier 3 invoke paid model APIs only through explicit commands with a confirmed cost cap.
Ordinary `pytest` remains offline and uses fake models.
