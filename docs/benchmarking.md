# Benchmarking and release readiness

Evaluation separates offline safety checks, production model task evaluation, and actual workflow E2E evidence. A passing task benchmark is not proof that workflow human gates, recovery, or parallel isolation ran in that benchmark.

| Tier | Trigger | Implemented scope |
| --- | --- | --- |
| 1 | `uv run pytest` | Offline policy/schema/persistence checks and fake-model contracts; no paid models. |
| 2 | Explicit `benchmark run --tier 2` | Approved representative cases for one production agent role, then the static evaluator role. |
| 3 | Explicit `benchmark run --tier 3` | All supported approved cases, task-specific Analyst/Planner dispatch, per-repeat scores, population variance, usage and readiness labels. Explicitly executed offline workflow suites can be attached separately. |

Ordinary tests never start paid evaluation. Regression tests use fake models, including the production Planner and evaluator contract paths. They establish no live quality, productivity, calibration, or release-readiness result.

## Gold and semantic correctness

Gold is separate from candidates, versioned, and usable only after explicit human approval. The checked-in `v0.1` approvals and pending statuses have **not** been changed to manufacture passing scores. Its two approved Analyst cases lack explicit semantic definitions and therefore require human gold review. Matching IDs, issue kinds, or source lines is insufficient for a semantic hit.

`ExpectedItem.semantic_definition` describes human-approved meaning; `accepted_contents` contains human-approved equivalent wording. `semantic_alternatives` retains the legacy identity-alias contract, not a content-matching shortcut. `ObservedItem.content` retains the statement, clarification question, or canonical Planner scenario. Analyst semantic context also retains the complete requirement or clarification and affected requirements. The judge sees this content and the pinned source documents.

An exact normalized approved-content match can be scored deterministically. A nonexact match requires an evaluator judgment and always carries a human-adjudication finding, even when the evaluator reports confidence. Numbers, operators, negation and question subject are preserved. A button-color question cannot obtain a semantic hit for a lockout-duration gap merely by using `MISSING_RULE` and BRD line 3. Missing gold definitions fail closed; the evaluator cannot invent or approve them.

`score_extraction` and `score_traceability` remain structural ID/coverage helpers, not replacements for the semantic release scorer `evaluate_case`.

## Production adapters and paid-run configuration

The Analyst adapter uses the production structured draft, instruction builder and source/conflict validators. The Planner adapter calls production `design_test_cases` in a disposable project/workflow/scope database, exercising actual expected-result grounding and coverage validation. Planner manifest inputs must provide `planner_requirements` with validated statements, source quotes, pinned source references and an optional `planner_scope_id`. These are benchmark inputs, not generated gold or approval records.

Tier 3 routes each approved model-task case to Analyst or Planner. Approved POLICY cases require pinned `policy_test_ids` and are measured by the executed offline suite, not by invented model semantic scores. They remain `POLICY_SUITE_EVIDENCE_MISSING` until the referenced tests actually execute and pass. Missing POLICY test IDs or Planner validated input requirements fail explicitly rather than being silently skipped. The bundled dataset has no approved Planner/POLICY case; contract tests use test-only mixed-task cases without modifying benchmark approval manifests.

Planner reports distinguish structural requirement/scenario coverage and schema validity from evaluator-reviewed expected-result correctness, executability, redundancy and detailed↔Gherkin agreement. Task adapters cannot observe workflow approval, mutation or dependency-bypass events; when gold requires those observations, missing runtime evidence forces review rather than inventing compliance. An actually observed forbidden action blocks the benchmark regardless of the content case's severity.

A paid run requires human-approved credentials, `--config`, declared candidate input/output token rates, `--confirm-budget`, and `--max-cost-usd` within the YAML cap. Additionally set:

- Tier 2: `QA_TIER2_EVALUATOR_INPUT_USD_PER_MILLION` and `QA_TIER2_EVALUATOR_OUTPUT_USD_PER_MILLION`.
- Tier 3: the corresponding `QA_TIER3_EVALUATOR_*` variables.
- Mixed-task Tier 3: `QA_TIER3_ANALYST_INPUT_USD_PER_MILLION`, `QA_TIER3_ANALYST_OUTPUT_USD_PER_MILLION` and the corresponding `QA_TIER3_PLANNER_*` variables. Different models must not silently inherit another role's prices.

Rates are human-declared cost estimates, not hard-coded provider prices. Model routing is static, with one configured evaluator and no fallback provider. Each case reserves candidate and evaluator calls before invocation. `max_case_cost_usd` is a conservative bound **per provider call**; a shared ledger spans repeats. The current two-case Tier 2 needs a USD 5 reservation; two-case Tier 3 needs USD 4 per repeat. Enlarging the dataset requires reconsidering the confirmed budget. Never raise a cap or start an API run merely to make a test pass.

Input paths are confined to the dataset root and pinned SHA-256 values are checked before prompting. JSON/Markdown reports retain input/output fingerprints, evaluated case IDs, per-case failures, per-repeat scores, population variance, tokens, declared-rate costs, model/config/prompt identities, latency and missing-evidence labels. Raw private documents and semantic output content are not copied into score reports.

Writing a report also saves `<output-directory>/.private/<run-id>.review.json`: the exact semantic candidate outputs and rendered Planner projections for every repeat, without evaluator ratings/judgments or raw provider responses. The directory is created with mode `0700` and files with mode `0600`. `review_packet_path` and `review_packet_sha256` identify it in the score report. Keep this packet local: it can contain confidential domain content, is not a scrubbed public export, and must not be committed or shared without approval. The default `benchmarks/reports/` output directory is Git-ignored; use an equally protected location for custom outputs. Human-review capture and release recheck its SHA-256, run/case/repeat identities and semantic output fingerprint; a missing or altered packet cannot supply valid release evidence. Existing reports without a packet need a fresh benchmark run.

## Independent review, calibration and explicit baseline promotion

The following commands are offline and never invoke models. They are registered through `register_evaluation_commands(benchmark)` in `cli/evaluation.py`.

Before capturing reviews, attach productivity evidence if available, then execute and attach the workflow evidence described below. Use the resulting immutable candidate JSON for every subsequent command: attaching evidence changes the report identity and invalidates earlier calibration/release records.

1. Give the reviewer only the private review packet, pinned input documents and approved gold/rubric—not the score report, evaluator assessments or calibration. Have the human rate every evaluated case without seeing AI ratings. Prepare a JSON array of `Review` records: `case_id`, ordinal `rating` (1–5), `critical`, `reviewer`, timezone-aware `reviewed_at`, `blind: true`, `model_snapshot_id: null` and `uncertainty` (0–1). Review actual candidate content, not only score summaries. For multirun reports, review all repeat outputs as one case cluster and rate the worst correctness, any critical violation and maximum uncertainty; repeats are not independent cases.

2. Capture the ratings, then disclose evaluator ratings and calculate calibration:

   ```bash
   uv run qa-orchestrator benchmark capture-human-review \
     --candidate candidate.json --ratings human-ratings.json \
     --confirm-independent --output human-review.json
   uv run qa-orchestrator benchmark calibrate \
     --candidate candidate.json --human-reviews human-review.json \
     --output calibration.json
   ```

   Capture prints a receipt/digest, not AI scores. Add `--confirm-all-repeats-reviewed` for multirun capture; every repeat must have complete evaluator provenance. Calibration records exact agreement, quadratic weighted Cohen's kappa, MAE, critical disagreement rate, sample size, human uncertainty, static evaluator snapshot and disagreement IDs. Duplicate, nonblind, unmatched or model-authored human ratings are rejected. Identity/blinding are human declarations, not an authenticated annotation service. Multirun calibration clusters repeats by case using the worst evaluator rating, any critical violation and maximum uncertainty, compared with one independent human cluster rating. Sample size counts distinct cases, never cases × repeats. Every per-repeat disagreement still requires explicit human adjudication.

3. Compare against the managed latest-approved baseline and minimum thresholds:

   ```bash
   uv run qa-orchestrator benchmark compare --candidate candidate.json \
     --thresholds thresholds.yaml --baseline-directory benchmarks/reports/baselines \
     --output comparison.json
   uv run qa-orchestrator benchmark evidence-hashes --candidate candidate.json \
     --thresholds thresholds.yaml --human-reviews human-review.json --calibration calibration.json
   ```

   Dataset/rubric/input/case-set changes require comparator re-evaluation. Critical failures remain `BLOCKED` regardless of averages or exceptions. Illustrative targets are not represented as calibrated release gates.

4. A human authors `BaselineApproval`: matching `candidate_run_id`, `status` (`APPROVED` or `REJECTED`), `reviewer`, timezone-aware `reviewed_at` and `reviewed_evidence`. Evidence must include all canonical identities printed above (`report`, `thresholds`, `human-review`, `calibration`, and `workflow-suite`) and `regression-decision:<decision>`. Explicit `case-adjudication:<case-id>` entries record adjudication of AI-semantic matches and disagreement examples. A sample below 30 is marked small and requires `small-sample:acknowledged`; this does not calibrate thresholds or repair absent gold. An exception additionally requires `exception:approved` and cannot bypass a critical hard gate.

   ```bash
   uv run qa-orchestrator benchmark release-review --candidate candidate.json \
     --thresholds thresholds.yaml --human-reviews human-review.json --calibration calibration.json \
     --approval approval.json --baseline-directory benchmarks/reports/baselines --output release.json
   uv run qa-orchestrator benchmark promote-baseline --candidate candidate.json \
     --thresholds thresholds.yaml --human-reviews human-review.json --calibration calibration.json \
     --release release.json --baseline-directory benchmarks/reports/baselines --confirm-promotion
   ```

   The first baseline requires explicit `release-review --bootstrap`; human approval, calibrated thresholds and complete evidence are still mandatory. Review never promotes implicitly. Promotion revalidates exact evidence, refuses changed comparators/reports, records approval history and atomically advances `latest-approved.json` under a file lock. Failed/review-required releases do not replace the comparator. Review records are create-only and cannot silently overwrite sign-offs.

## Equivalent-task productivity

Bare active-QA/manual timing flags are descriptive only and do not clear `PRODUCTIVITY_EVIDENCE_MISSING`. Supply `ProductivityEvidence` via `QA_TIER3_PRODUCTIVITY_EVIDENCE` (a JSON file path), or attach it offline **before** capturing human review:

```bash
uv run qa-orchestrator benchmark record-productivity --candidate candidate.json \
  --evidence equivalent-task-evidence.json --output candidate-with-productivity.json
```

Required fields: `equivalent_task_id`, `acceptance_criteria_id`, distinct `manual_task_id` and `assisted_task_id`, `accepted_artifact_ids`, `accepted_by`, `evidence_references`, `active_qa_minutes`, positive `manual_baseline_minutes`, `human_review_minutes` and `rework_minutes`. Active QA time includes review and rework. Reports retain these identities, time reduction and cost per accepted artifact. Attaching evidence never grants approval or clears other missing-evidence labels. Humans must verify equivalent source documents, output scope, stopping conditions and acceptance criteria.

## Executed offline workflow evidence and its boundary

Tier 3 evaluates production task adapters, **not** the full LangGraph workflow for each benchmark case. New reports therefore start with `E2E_WORKFLOW_EVIDENCE_MISSING`. The following explicit offline path removes that label only after the real fixed workflow/policy suites execute and pass on the same source fingerprint:

```bash
uv run qa-orchestrator benchmark capture-workflow-evidence \
  --candidate candidate.json --output workflow-evidence.json
uv run qa-orchestrator benchmark attach-workflow-evidence \
  --candidate candidate.json --evidence workflow-evidence.json \
  --output candidate-with-workflow.json
```

Use `candidate-with-workflow.json` for review/calibration/comparison/release. Capture actually invokes the current Python's `pytest`, with a 600-second timeout, provider credentials/budget confirmations removed, and the following fixed offline suites (fake models, not live providers):

- E2E: `test_cli_workflows.py`, `test_resume_and_conflict.py`, `test_stlc_journey.py`.
- Integration: `test_parallel_scopes.py`, `test_scoped_approval.py`, `test_checkpoints.py`, `test_resume.py`.
- Unit policy: `test_access_control.py`, `test_policies.py`.

Evidence records the command, suite/test manifest, outcomes/durations, timestamps, exit code, Git HEAD, full source-file hash manifest, source fingerprint, exact benchmark binding and JUnit SHA-256. Preserve the adjacent `workflow-evidence.junit.xml` at its recorded absolute path together with the JSON. Attach and release revalidate both the JUnit contents and the current source manifest. Changes to source, tests, benchmark configuration, rubric, gold, synthetic fixtures, `pyproject.toml` or `uv.lock` invalidate evidence; rerun the benchmark and capture after changes. Dirty working-tree content is fingerprinted, not silently represented as the HEAD commit.

Failed/error tests or pytest's failure exit code yield `BLOCKED`, including at release review; an approval or exception cannot override them. Incomplete runs, skips, missing suites or nonzero infrastructure/collection exits cannot count as a pass. Capture prints its outcome rather than turning a failed suite into an absent record. Merely having tests in the repository does not supply evidence. Release approval must explicitly reference `workflow-suite:<digest>` from `evidence-hashes`; promotion revalidates it again. The records are local, hash-checked evidence, not cryptographically signed attestations against a malicious author.

This certifies execution of the listed offline workflow/policy scenarios against the candidate code, not replay of every benchmark case/repeat through a workflow, nor live-model quality. A future per-case runtime adapter would additionally bind run/case/repeat and input fingerprint to project/workflow/execution IDs, journal/checkpoint digests, source snapshots, human approval IDs/actors/timestamps, scope IDs and emitted artifact/version IDs, and verify these against the workflow journal. That runtime adapter is not implemented here; the suite evidence makes no such claim.

Other current blockers are `INSUFFICIENT_REAL_CASES`, `UNCALIBRATED_GATES` and, without accepted equivalent-task records, `PRODUCTIVITY_EVIDENCE_MISSING`. They remain `REVIEW_REQUIRED` even after a passing workflow suite and must not be deleted merely to obtain a passing release.

Private real-world inputs belong in Git-ignored `benchmarks/datasets/real_world/`, alongside matching versioned human-reviewed gold manifests. No real-world approvals, calibrated production thresholds, production human reviews, baseline promotion or live productivity result were manufactured during development.
