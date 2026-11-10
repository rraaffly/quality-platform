# CLI workflows

Run `uv run qa-orchestrator --help` for the current command surface. Every command acts on explicit
local paths; there is no daemon or background worker.

## Projects and workflows

```bash
uv run qa-orchestrator project create \
  --database ./qa.sqlite --project-id P1 --name "Login" --root ./workspace/P1
uv run qa-orchestrator project select --database ./qa.sqlite --project-id P1
uv run qa-orchestrator workflow start \
  --database ./qa.sqlite --workflow-id W1 --goal "Design login tests"
uv run qa-orchestrator workflow list --database ./qa.sqlite
```

Project selection is stored beside the domain database. Reads and writes remain project-scoped;
selecting another project does not grant access to the first project's sources.

## Human gates

```bash
uv run qa-orchestrator clarification list \
  --database ./qa.sqlite --workflow-id W1
uv run qa-orchestrator clarification answer \
  --database ./qa.sqlite --workflow-id W1 --clarification-id C1 \
  --author "Product Owner" --text "Lock after three attempts" \
  --recorded-by QA --source "decision meeting"
uv run qa-orchestrator scope review \
  --database ./qa.sqlite --workflow-id W1 --scope-id LOGIN \
  --action approve --actor QA
```

Clarification answers need provenance. A scope affected by an unresolved question cannot be
approved, while an independent scope may continue to draft work.

Artifact approval is scoped to an exact version, source baseline, content hash, and checkpoint
thread. Resume a waiting review with the same workflow and checkpoint database:

```bash
uv run qa-orchestrator workflow resume \
  --database ./qa.sqlite --workflow-id W1 --artifact-id CASES-LOGIN \
  --checkpoint-database ./checkpoints.sqlite --actor QA --approve
```

Replaying the same approval is idempotent. A later requirement version makes the old baseline stale
and blocks finalization even when the human response is "approve".

## Artifacts

```bash
uv run qa-orchestrator artifact show \
  --database ./qa.sqlite --artifact-id CASES-LOGIN
uv run qa-orchestrator artifact review \
  --database ./qa.sqlite --workflow-id W1 --artifact-id CASES-LOGIN \
  --actor QA --approve
```

Canonical test cases are JSON. Detailed Markdown and Gherkin are deterministic projections of the
same case set. Targeted revisions create a new draft version and require approval of the exact diff;
impact detection reports potentially stale artifacts but never rewrites them automatically.

## Benchmarks

Tier 1 is the normal offline test suite. Live Tier 2 and full Tier 3 are explicit paid commands and
require a cost confirmation. See [benchmarking.md](benchmarking.md) for complete examples, metadata,
privacy rules, productivity measurement, and release-readiness limitations.
