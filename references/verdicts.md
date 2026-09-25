# Verdicts in detail

Every run ends with a `>> NEXT:` block that says what to do for that result.
This file is the long form: read it when the output leaves you unsure, or
when you have to explain a verdict to someone.

## Review verdicts

| verdict | exit | meaning | what you do |
|---|---|---|---|
| `REVIEWED` | 0 | The newest bot review anchored to the **current head**. Findings are printed with thread id and open/resolved status. | Read the overview, the inline findings **and** the "unanchored finding(s)". Verify each against the code before acting on it. |
| `UNREACHABLE` | 1 | Gitea stayed unreachable through the deadline (or under `--once`). Exit 1 is also every error with no verdict at all: a usage error, a 401 or 404 from Gitea, Ctrl-C. | No verdict. Not a pass. Read the last line: retry when Gitea is back, or fix the token or `--repo`. |
| `TIMED_OUT` | 2 | Nothing arrived within the budget, and the agent named no reason. It is still working, or could not be asked. Health and counters are printed. | **Not an approval.** `deliveries: 0` means the webhook never arrived. A nonzero `rejected_signature` means the secret is wrong. Re-request with the printed curl, or merge while saying plainly that it went in unreviewed. |
| `FAILED` | 3 | The bot posted `⚠️`, or the agent reported a failure. `credentials`, `quota`, `oversized` and `backend_rejected` need an operator. `generic`, `backend_error` and `review_timeout` might pass on a fresh review request. | Fix the service, or merge knowing it is unreviewed. Never treat it as clean. |
| `SKIPPED` | 4 | The diff is too large (`MAX_DIFF_LINES` 4000 / `MAX_DIFF_BYTES` 400000). No review is coming. | Split the PR, or review it yourself. |
| `STALE` | 5 | The only review is for an **older SHA**, because you pushed since. Its findings are printed in full under a banner naming that SHA. | Read them, since they usually still apply. The poller keeps waiting and exits 5 only at the deadline or under `--once`. |
| `DECLINED` | 6 | The agent says no review is coming for this SHA, and why: diff empty after `SKIP_PATHS`, `nothing_new_since_last_review`, superseded, a bot-authored PR, `lost_on_restart`… | Read the reason. `nothing_new…` means the last review stands. `lost_on_restart` means nobody reviewed this, so re-request. |
| `PENDING` | 7 | Only from a run that did not wait: `--once`, or a closed PR. Nothing has landed for this head yet. | **Not a pass.** Run without `--once` to wait. A closed PR gets no new review. |
| `CI_FAILED` | 8 | CI failed before the review decided, so the poller stopped waiting. The review's state is printed: pending, or `STALE` with its findings in full. | Fix CI and push, which restarts the review too. Pass `--no-fail-fast` to wait for the review anyway. |

**Once the review has decided, CI never changes the code.** `CI_FAILED` (8)
fires only while the review is still undecided. A `REVIEWED` next to a failed
CI exits 0, and the CI block and the NEXT block say the rest. `--once` does
not wait for CI either: a `REVIEWED` next to a running CI exits 0 with a "CI
has not settled" step.

## CI verdicts

The poller reads every workflow run at the head SHA, keeps the **newest run
per workflow and event**, and takes the worst result across all their jobs.

| CI | meaning |
|---|---|
| `PASSED` | Every job succeeded. `skipped` and `neutral` jobs are listed as `(not gating)`. |
| `FAILED` | At least one job completed without succeeding (`cancelled` included). It is named in the detail. If the review is still undecided, the wait stops with exit 8. |
| `RUNNING` | Something is queued or running. Running past 15 minutes is flagged as **possibly hung**. Queued past 5 minutes is flagged as **no runner has picked it up**, which usually means no online runner has the job's labels. |
| `UNKNOWN` | **The poller could not read CI.** A 4xx (the token cannot read Actions, or Actions is off) is reported as-is. A 5xx or a timeout is retried until it clears. This is *not* "no CI". |
| `NONE` | No run at this head, or every job was skipped. It counts as settled only after 90s of watching this head, because Gitea may not have created the run yet. |

## Reading the output

- **`requested: NO` is not a reason to skip waiting.** The bot reviews on open
  regardless. The header reports it because re-requesting `review-bot` is the
  one lever you have on a `TIMED_OUT`.
- **If you list reviews yourself, walk every page.** The API pages at 50,
  oldest first, so page 1 alone hides the newest reviews.
- **The bot is not deterministic.** A later clean review does not clear an
  earlier finding. Check the earlier finding against the code.
