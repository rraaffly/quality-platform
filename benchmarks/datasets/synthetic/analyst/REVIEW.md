# Analyst synthetic gold review

Status: **APPROVED** for `conflict` and `missing_rule`. Rafly Rafsanjani approved the expected findings and source evidence on 2026-11-10 at 07:27:32 UTC. The decision is recorded in `benchmarks/ground_truth/v0.1.json`. No generated output is used as its own gold.

| Case | Input | Expected finding | Evidence | Forbidden conclusion |
| --- | --- | --- | --- | --- |
| `conflict` | `synthetic-login-brd.md` and `synthetic-login-srs.md` | `login-attempt-count-conflict` (`CONFLICT`): ask whether lockout occurs after 3 or 5 failed attempts. | Both documents, line 3. | Selecting either count without a stakeholder answer. |
| `missing_rule` | `synthetic-login-brd.md` | `lockout-duration-missing` (`GAP`): ask how long the account remains locked. | BRD, line 3. | Inventing a duration. |

The independent TOTP line in `synthetic-independent-prd.md` is used by the offline Analyst tests to check that a clear, unrelated requirement can remain eligible for draft work.

The `shared_scope` case belongs to Task 9 and is not part of this review. It and the other non-Analyst cases remain `PENDING_REVIEW`.
