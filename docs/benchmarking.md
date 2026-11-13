# Benchmarking and release evidence

The authenticated Web app owns the evaluation workflow. Open **Reports** to start a confirmed Tier 2 or Tier 3 run, import an existing report for inspection, and open a Web-generated report for evidence capture and release review. Imported reports are summaries and cannot be promoted.

| Tier | Trigger | Scope |
| --- | --- | --- |
| 1 | `uv run pytest -q` | Offline policy, persistence and fake-model checks; no paid provider calls. |
| 2 | Confirmed Web benchmark job | Approved representative cases for one production agent role and a static evaluator. |
| 3 | Confirmed Web benchmark job | All supported approved cases, repeat scores, variance, usage, cost and readiness labels. |

The browser requires the owner to choose the dataset version, pinned settings, repeat count, token rates for Analyst, Planner and evaluator, a maximum USD cost, and paid-run confirmation. Model routing uses one configured model per role, without fallback. The shared cost ledger reserves each candidate and evaluator call before invocation. Credentials stay in the worker environment. Starting or reading the app never initiates a paid benchmark.

Use **Calculate cost reservation** before confirmation. The Web quote reads the same approved dataset and tier configuration as the runner, showing selected cases, provider calls and the required reservation. The confirmed cap must cover that reservation and stay within the tier maximum. The quote is a reservation bound based on configured per-call costs; recorded usage cost uses the declared token rates.

## Approved gold and private packets

Gold is separate from candidates and usable only after explicit approval. A matching ID, issue kind, or source line cannot stand in for a semantic match. `ExpectedItem.semantic_definition` and approved equivalent content define the intended meaning. Nonexact matches require evaluator judgment and human adjudication. The checked-in `v0.1` dataset lacks semantic definitions in its two approved cases; those cases remain review-required.

Input snapshots are confined to the dataset root and checked against pinned SHA-256 values. Reports keep case scores, input/output fingerprints, cost, static model identity and readiness labels, without raw private source content. A private packet contains the exact candidate outputs for blind human review, without AI ratings. The reviewer page also serves the hash-checked input snapshots and approved gold for those cases. The server checks the packet hash, run/case/repeat identity and output fingerprint before serving it to the owner. Keep the state directory private and include it in a consistent backup.

## Evidence and human decision

1. On the latest Web-generated report, run the offline workflow/policy suite. The result is attached as a new immutable report version; use that new report ID. Failed, skipped, incomplete or stale source evidence cannot clear `E2E_WORKFLOW_EVIDENCE_MISSING` and may block release.
2. Attach equivalent-task `ProductivityEvidence` if available. It must identify acceptance criteria, distinct manual and assisted tasks, accepted artifacts, reviewer, evidence references, active QA time, manual baseline time, review time and rework time. This creates another report version.
3. Review the private candidate packet and pinned inputs without seeing AI ratings. Rate every evaluated case. For repeated runs, review all outputs in each case cluster and record the worst correctness, any critical violation and maximum uncertainty. Confirm independent review, then reveal calibration using the saved human review ID. Duplicate, incomplete, nonblind or model-authored ratings are rejected.
4. Supply calibrated quality thresholds and use **Prepare exact evidence hashes**. The job shows the report, thresholds, human review, calibration, regression decision and workflow-suite digests. Record any `case-adjudication:<case-id>`, `small-sample:acknowledged` or `exception:approved` evidence explicitly when applicable. A small sample acknowledgment does not calibrate thresholds or repair missing gold.
5. Record an explicit human release decision with those exact hashes. The first baseline requires **First approved baseline**. Review can return `REVIEW_REQUIRED` or `BLOCKED` even when a human selected `APPROVED`.
6. Promote in a separate confirmed action using the release ID. The worker locks the baseline directory, rechecks the comparator and all evidence, writes immutable history, and atomically advances `latest-approved.json` only for `PASS` or valid `APPROVED_EXCEPTION`. A rejected, stale, critical or incomplete release cannot advance it.

Release review and promotion do not call a model. Report reads do not modify a baseline. The thresholds bundled in `benchmarks/rubrics/quality-gates.yaml` are `ILLUSTRATIVE`; entering `CALIBRATED` values requires a real calibration decision by the owner, not a formatting change.

The offline suite certifies execution of its listed fixed workflow/policy tests against the current source fingerprint. It does not prove that each benchmark case was replayed end to end through the workflow. Source, configuration, rubric, dataset and test changes can stale the evidence. Paid-model quality must be evaluated separately.
