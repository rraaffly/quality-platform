# CLI workflows

Run `uv run qa-orchestrator --help` for the current command surface. Every command acts on explicit
local paths; there is no daemon or background worker.

## Live model settings without Python changes

The current configuration surface is split between YAML, environment variables and CLI flags:

| Setting | Where to configure it |
| --- | --- |
| Provider/model per role | `agent_models.<role>.provider` and `model_id` in the application YAML passed with `--config`. Providers: `openai`, `anthropic`, `google`. |
| API keys | Process environment: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`. Never put keys in model YAML. `.env` is not automatically loaded. |
| Parallel workflow tasks | `max_concurrency` in application YAML; this is not a token or monetary cap. |
| Candidate input/output price | Benchmark flags `--input-usd-per-million` and `--output-usd-per-million`, or `QA_TIER2_INPUT_USD_PER_MILLION` / `QA_TIER2_OUTPUT_USD_PER_MILLION` (use `TIER3` for Tier 3). |
| Evaluator input/output price | `QA_TIER2_EVALUATOR_INPUT_USD_PER_MILLION` and `QA_TIER2_EVALUATOR_OUTPUT_USD_PER_MILLION` (use `TIER3` for Tier 3). |
| Mixed-task Tier 3 prices | `QA_TIER3_ANALYST_INPUT_USD_PER_MILLION`, `QA_TIER3_ANALYST_OUTPUT_USD_PER_MILLION`, and corresponding `QA_TIER3_PLANNER_*` variables. |
| Benchmark run budget | `--confirm-budget --max-cost-usd`; cost cap must also fit the tier YAML limit. |
| Reservation/case selection/repeat limits | `benchmarks/configs/tier2.yaml` or `tier3.yaml`: `max_budget_usd`, `max_case_cost_usd` per provider call, representative case IDs, and Tier 3 `max_repeats`. |
| Benchmark size | `--max-cases` for Tier 2; `--repeats` for Tier 3. Tier 3 uses all supported approved cases, not the Tier 2 case limit. |

Prices are human-declared USD per **one million tokens**, not fetched from the provider. They must
match the selected models; do not reuse another model's rates. YAML limit changes are configuration
changes, not Python edits, but raising a budget still requires an explicit decision. Tier config
files are currently read from the repository's `benchmarks/configs/`, not selected by `--config`.

There is no application YAML field/CLI flag yet for temperature, maximum output tokens, timeout,
retry policy or custom API base URL. Unknown fields are rejected, not silently applied. These
require exposing supported options in the application once before they can be managed via config.
Model/config changes require a new workflow; existing workflows pin the original configuration.

**`workflow run` has no application cost cap yet.** Benchmark reservations and post-call usage
checks also do not enforce a provider billing cap: an over-budget response can already have incurred
cost before the application detects it. Set provider-side limits where available, start small and
do not confuse concurrency with cost control.

### Windows PowerShell

After installing the updated dependencies with `uv sync --dev`, the CLI uses a cross-platform
file lock for baseline promotion instead of importing Unix-only `fcntl`. Do not try to install
`fcntl` on Windows. The Bash examples elsewhere in this document require adapting shell syntax.

Read the API key privately into the current PowerShell process (not a persistent setting):

```powershell
$key = Read-Host "OPENAI_API_KEY" -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new("", $key).Password
Remove-Variable key
```

For a single-role Tier 2 run, declare the candidate and evaluator prices separately, then run:

```powershell
$inputRate = Read-Host "Candidate input USD per million tokens"
$outputRate = Read-Host "Candidate output USD per million tokens"
$env:QA_TIER2_EVALUATOR_INPUT_USD_PER_MILLION = Read-Host "Evaluator input USD per million tokens"
$env:QA_TIER2_EVALUATOR_OUTPUT_USD_PER_MILLION = Read-Host "Evaluator output USD per million tokens"
uv run qa-orchestrator benchmark run --tier 2 --dataset-version v0.1 --agent-role analyst --max-cases 1 --config .\config.yaml --input-usd-per-million $inputRate --output-usd-per-million $outputRate --confirm-budget --max-cost-usd 2.50 --output-directory .\benchmarks\reports\live
```

This command is **paid and optional**; `config.yaml` must already contain your selected model IDs.
USD 2.50 covers the current one-case reservation (candidate plus evaluator), not an estimate of the
actual bill. The bundled gold still needs semantic curation, so this is not production quality
evidence. See [benchmarking.md](benchmarking.md) before interpreting the scores or promoting a baseline.

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
