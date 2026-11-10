# Analyst synthetic gold review

Status: **PENDING_REVIEW**. A QA reviewer must verify the expected findings below before changing the corresponding entries in `benchmarks/ground_truth/v0.1.json` to `APPROVED`. No generated output is used as its own gold.

| Case | Input | Expected finding | Evidence | Forbidden conclusion |
| --- | --- | --- | --- | --- |
| `conflict` | `synthetic-login-brd.md` and `synthetic-login-srs.md` | `login-attempt-count-conflict` (`CONFLICT`): ask whether lockout occurs after 3 or 5 failed attempts. | Both documents, line 3. | Selecting either count without a stakeholder answer. |
| `missing_rule` | `synthetic-login-brd.md` | `lockout-duration-missing` (`GAP`): ask how long the account remains locked. | BRD, line 3. | Inventing a duration. |

The independent TOTP line in `synthetic-independent-prd.md` is used by the offline Analyst tests to check that a clear, unrelated requirement can remain eligible for draft work.

The reviewer should record their own name, decision, and timestamp in the gold review history. The `shared_scope` case belongs to Task 9 and is not part of this review.
