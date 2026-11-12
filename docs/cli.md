# CLI workflows

Run `uv run qa-orchestrator --help` for the current command surface. Every command acts on explicit
local paths; there is no daemon or background worker.

## Projects and workflows

Create `config.yaml` first. Paths are relative to that file; use provider/model IDs you have
explicitly selected and credentials from environment variables, never in the YAML. The values
below are placeholders, not working model defaults:

```yaml
database_path: ./qa.sqlite
workspace_root: ./workspace
max_concurrency: 3
test_case_style: both
agent_models:
  orchestrator: {provider: openai, model_id: replace-with-selected-model}
  analyst: {provider: anthropic, model_id: replace-with-selected-model}
  planner: {provider: google, model_id: replace-with-selected-model}
  evaluator: {provider: openai, model_id: replace-with-selected-model}
```

```bash
uv run qa-orchestrator project create \
  --database ./qa.sqlite --project-id P1 --name "Login" --root ./workspace/P1
uv run qa-orchestrator project select --database ./qa.sqlite --project-id P1
uv run qa-orchestrator workflow start \
  --database ./qa.sqlite --workflow-id W1 --goal "Design login tests" \
  --config ./config.yaml --source ./workspace/P1/brd.md --source ./workspace/P1/srs.md
uv run qa-orchestrator workflow run \
  --database ./qa.sqlite --workflow-id W1 --checkpoint-database ./checkpoints.sqlite \
  --request-id initial --instruction "Analyze these documents and draft plans and cases for eligible scopes" \
  --style both
uv run qa-orchestrator workflow list --database ./qa.sqlite
uv run qa-orchestrator workflow inspect --database ./qa.sqlite --workflow-id W1
```

Project selection is stored beside the domain database. Reads and writes remain project-scoped;
selecting another project does not grant access to the first project's sources.

`start` snapshots inputs and pins configuration; it does not call a model. `run` invokes the
statically configured Orchestrator, Analyst and Planner as needed and can incur provider charges.
Typed task proposals are checked by deterministic scope/dependency/approval services before writes.
Independent eligible scopes may proceed while another scope is blocked. Canonical JSON is always
written for test cases; `--style` changes projections only, never the model.

An unconfigured workflow can be configured, or additional sources imported, explicitly:

```bash
uv run qa-orchestrator workflow import \
  --database ./qa.sqlite --workflow-id W1 --config ./config.yaml --source ./workspace/P1/prd.md
```

Import creates immutable source snapshots without changing originals. After importing new sources,
use a **new request ID** to analyze them before drafting. Model/config changes require a new workflow.
For a provider failure or process restart, resume the saved request with the same checkpoint database:

```bash
uv run qa-orchestrator workflow resume \
  --database ./qa.sqlite --workflow-id W1 --checkpoint-database ./checkpoints.sqlite --request-id initial
```

Completed tasks and published artifacts are reused without double publication. You can alternatively
repeat `workflow run` with the exact same request ID, instruction and rendering style. A new intent
requires a new request ID. If the process stopped before its plan was saved, rerun the original `run`
command. There is no provider fallback or model migration during resume.

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
uv run qa-orchestrator scope baseline \
  --database ./qa.sqlite --workflow-id W1 --scope-id LOGIN --actor QA
```

Clarification answers need provenance. A scope affected by an unresolved question cannot be
approved, while an independent scope may continue to draft work. Answering records `ANSWERED`,
not an automatic resolution: run a new analysis intent to let the Analyst assess sufficiency and
revalidate the requirements. Accepted stakeholder rules become separately attributed immutable
source snapshots; old contradictory snapshots remain intact. Insufficient answers reopen the question.

Scope proposal review and requirement-baseline approval are separate gates. Use the baseline ID
returned by `scope baseline` and an artifact ID returned by `workflow run` to submit a specific
plan or case artifact for review:

```bash
uv run qa-orchestrator artifact submit \
  --database ./qa.sqlite --workflow-id W1 --artifact-id CASES-ID \
  --baseline-id BASELINE-ID --checkpoint-database ./checkpoints.sqlite
```

`CASES-ID` and `BASELINE-ID` here and below are placeholders for those actual IDs. Submission checks
the exact requirement versions used to generate the content, binds the approved baseline in a new
version if necessary, and pauses at a human interrupt. Plans and cases are reviewed separately.

Artifact approval is scoped to an exact version, source baseline, content hash, and checkpoint
thread. Resume a waiting review with the same workflow and checkpoint database:

```bash
uv run qa-orchestrator workflow resume \
  --database ./qa.sqlite --workflow-id W1 --artifact-id CASES-ID \
  --checkpoint-database ./checkpoints.sqlite --actor QA --approve
```

Replaying the same approval is idempotent. A later requirement version makes the old baseline stale
and blocks finalization even when the human response is "approve".

## Natural-language scope changes

```bash
uv run qa-orchestrator scope propose-edit \
  --database ./qa.sqlite --workflow-id W1 \
  --instruction "Split the proposed login scope into desktop and mobile journeys" --output ./scope-edit.json
# Inspect the proposal before explicitly applying it:
uv run qa-orchestrator scope apply-edit \
  --database ./qa.sqlite --workflow-id W1 --proposal ./scope-edit.json --actor QA
```

Proposal generation invokes the configured Orchestrator but cannot approve itself. Merge/split is
limited to proposed scopes; replacements keep canonical requirement membership, exclusions and
dependencies and remain proposals needing separate approval. The same requirement may be shared
without cloning it. For manually prepared changes, `scope review` also accepts `--action merge`
with repeated `--merge-with`, or `--action split`, plus `--replacements` pointing to a JSON
`ScopeProposalSet`.

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

```bash
uv run qa-orchestrator artifact revision \
  --database ./qa.sqlite --workflow-id W1 --artifact-id CASES-ID \
  --instruction "Change only TC-012 title to Successful authenticator enrollment" --output ./revision.json
# Review the displayed diff and supply its exact hash, not a guessed value:
uv run qa-orchestrator artifact commit-revision \
  --database ./qa.sqlite --workflow-id W1 --draft ./revision.json \
  --reviewed-diff-hash REVIEWED-HASH --actor QA
uv run qa-orchestrator workflow impact --database ./qa.sqlite
```

The Planner may propose changes to the selected case's title, steps or other content, not just its
priority. Only one named case ID is permitted; stable IDs and requirement membership cannot change.
Source-grounded expected results are revalidated before commit. Diff approval is durably recorded
with the new **draft**, not an implicit final artifact approval. Previous versions stay immutable.

Requirement version writes automatically persist an impact report in the same database transaction.
`artifact show` overlays `POTENTIALLY_STALE` when the current artifact's trace predates the changed
requirement. No automatic revision or republishing happens; historical approval events remain intact.

## Benchmarks

Tier 1 is the normal offline test suite. Live Tier 2 and full Tier 3 are explicit paid commands and
require a cost confirmation. See [benchmarking.md](benchmarking.md) for complete examples, metadata,
privacy rules, productivity measurement, and release-readiness limitations.
