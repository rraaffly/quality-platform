# Quality Platform

Local-first QA orchestration for requirement analysis, scope review, test planning, canonical
test cases, approval workflows, targeted revision, impact detection, and evaluation.

## Status

The MVP workflow and offline safety suite are implemented. The bundled benchmark is intentionally
synthetic-first: no private real-world fixtures are committed, numeric quality gates remain
illustrative, and release readiness is not claimed until approved real cases and calibrated human
review evidence exist.

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
