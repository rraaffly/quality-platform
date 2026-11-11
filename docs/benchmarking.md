# Benchmarking and release readiness

Quality Platform separates fast offline policy checks from cost-bearing model evaluation.

| Tier | Trigger | Scope |
| --- | --- | --- |
| 1 | `uv run pytest` | Offline schemas, policies, persistence, isolation, rendering, and fake-model agent behavior. |
| 2 | Explicit `benchmark run --tier 2` | Small approved representative subset for a selected agent role. |
| 3 | Explicit `benchmark run --tier 3` | Available approved dataset, bounded repeated runs, productivity evidence, and release-readiness inputs. |

No test command starts Tier 2 or Tier 3. Live runs require provider credentials, an application
model configuration, explicit token rates, `--confirm-budget`, and `--max-cost-usd`.

## Tier 2

```bash
uv run qa-orchestrator benchmark run \
  --tier 2 \
  --dataset-version v0.1 \
  --agent-role analyst \
  --max-cases 2 \
  --config ./config.yaml \
  --input-usd-per-million 1.00 \
  --output-usd-per-million 4.00 \
  --max-cost-usd 1.00 \
  --confirm-budget \
  --output-directory benchmarks/reports/tier2
```

The token prices are run inputs, not hard-coded pricing claims. Their values, the application
configuration, prompt snapshot, model snapshot, dataset/rubric version, and Git commit contribute
to reproducible report metadata.

## Tier 3 and productivity experiment

```bash
uv run qa-orchestrator benchmark run \
  --tier 3 \
  --dataset-version v0.1 \
  --repeats 3 \
  --config ./config.yaml \
  --input-usd-per-million 1.00 \
  --output-usd-per-million 4.00 \
  --max-cost-usd 5.00 \
  --active-qa-minutes 25 \
  --manual-baseline-minutes 60 \
  --confirm-budget \
  --output-directory benchmarks/reports/tier3
```

`benchmarks/configs/tier3.yaml` bounds repeats, total cost, and the conservative maximum cost of one
case. One shared ledger reserves that per-case bound before every provider call across all repeats,
so a call is never started when it could exceed the user-confirmed cap. Non-finite prices, budgets,
and productivity values are rejected. JSON and Markdown reports record:

- per-case precision, recall, evidence accuracy, findings, and critical violations;
- every failed case ID, so aggregate scores cannot hide the failing sample;
- provider/model usage, tokens, cost, repeats, and wall-clock latency;
- active QA review time and the manual comparison time supplied for the equivalent task;
- readiness labels describing missing evidence.

Tier 3 selects every case whose gold is `APPROVED`; `representative_case_ids` applies only to Tier 2.
It uses the same Analyst schema, prompt rules, and evidence/conflict validators as the production
agent. If an approved task type has no production evaluation adapter, the full run fails explicitly
instead of producing an incomplete readiness report.

Every live input snapshot is confined to its synthetic or real-world dataset directory and verified
against the SHA-256 pinned in the manifest before its contents enter a prompt. Reports carry the
verified input fingerprint and exact evaluated case IDs; changing either requires comparator
re-evaluation before baseline promotion.

The productivity comparison is valid only when manual and AI-assisted runs use equivalent source
documents, requested artifacts, reviewer expectations, and stopping conditions. Count all active QA
review and rework time. A faster agent run is not evidence of quality if the human reviewer has not
accepted the artifact.

## Current evidence limitations

The bundled `v0.1` dataset does not contain the planned minimum of approved real-world cases, and
`benchmarks/rubrics/quality-gates.yaml` is marked `ILLUSTRATIVE`. Tier 3 therefore emits:

- `INSUFFICIENT_REAL_CASES` until QA supplies and approves enough real cases;
- `UNCALIBRATED_GATES` until thresholds are calibrated against human annotations;
- `PRODUCTIVITY_EVIDENCE_MISSING` when either active-QA or manual-baseline time is absent.

These labels force `REVIEW_REQUIRED`; they must not be removed merely to obtain a passing release.
Private real-world inputs belong in the Git-ignored `benchmarks/datasets/real_world/` area and may
enter scoring only with immutable human-approved gold metadata. A versioned real-world manifest is
loaded only when the matching `benchmarks/ground_truth/real_world/` manifest is also present.

## Release readiness

Release assembly combines the benchmark report with an independent `HumanReview`. Critical policy
violations always return `BLOCKED`. Noncritical readiness requires human approval, review coverage
for every failed case, enough approved real cases, calibrated thresholds, and no unresolved
readiness labels. `APPROVED_EXCEPTION` cannot override a critical hard gate.

Reports are local artifacts under the Git-ignored `benchmarks/reports/` directory. Inspect a saved
report with:

```bash
uv run qa-orchestrator benchmark report --report benchmarks/reports/tier3/<run-id>.json
```
