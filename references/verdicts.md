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
| `STALE` | 5 | The only review is for an **older SHA**, because you pushed since. Its findings are printed in full under a banner naming that SHA. | Read them, since they usually still apply. The poller returns on one when it lands during the wait. One that was already there when the wait started is not news: that wait exits 5 only at the deadline or under `--once`. |
| `DECLINED` | 6 | The agent says no review is coming for this SHA, and why: diff empty after `SKIP_PATHS`, `nothing_new_since_last_review`, superseded, a bot-authored PR, `lost_on_restart`… | Read the reason. `nothing_new…` means the last review stands. `lost_on_restart` means nobody reviewed this, so re-request. |
| `PENDING` | 7 | The run returned before the review said anything: CI finished first, `--once`, or a closed PR. Nothing has landed for this head yet. | **Not a pass.** Run the `--wait-for review` command from NEXT, or run without `--once`. A closed PR gets no new review. |
| `CI_FAILED` | 8 | CI failed before the review decided, so the poller stopped waiting. The review's state is printed: pending, or `STALE` with its findings in full. | Fix CI and push, which restarts the review too. Pass `--no-fail-fast` to wait for the review anyway. |
| `BASE_MOVED` | 9 | The live target branch tip changed during this run. Review and CI results still describe the head, not necessarily the new combined tree. | Update/rebase if needed, run appropriate checks against the new base, and babysit again. |
| `NOT_MERGEABLE` | 10 | Gitea reports `mergeable: false` for 90 seconds, or on a `--once` check. This includes temporary checking and drafts as well as conflicts. | Inspect the cause; do not assume a conflict. Rerun when Gitea finishes checking or the block clears. Review and CI remain visible. |
| `CI_PASSED` | 11 | GitHub only: every check run and commit status at the head succeeded, and there was at least one. This is not a review: no reviewer was watched. | Say "CI passed", never "reviewed" or "clean". Branch protection was not evaluated. Refresh readiness before merging. |

**Once the review has decided, CI never changes the code.** `CI_FAILED` (8)
fires only while the review is still undecided. A `REVIEWED` next to a failed
CI exits 0, and the CI block and the NEXT block say the rest. Target movement
and a non-mergeable PR override that exit with codes 9 and 10 respectively.

## When the wait returns

By default (`--wait-for any`) the poller returns at the first of these:

- **The review decides**: a review of the head, a skip or failure notice, or a
  decline from the agent. It holds for up to 90s only while CI reads `NONE`,
  so the CI line says something.
- **A new bot review lands**, even one of an older head (`STALE`). One that
  was already there when the run started does not count.
- **Except a review with no findings**: no inline comment and no unanchored
  note. It leaves nothing to do while CI runs, so it holds until CI settles
  and returns once, with both. A failed CI still returns at once.
- **CI finishes during the run.** A CI that had already passed when the run
  started is not news, and `NONE` never is. A failed CI always is (exit 8).
- **The target branch moves**, returning promptly regardless of `--wait-for`.
  Gitea may briefly set `mergeable: false` while recalculating, so movement
  takes priority over this field.
- **Gitea continues to report `mergeable: false` for 90 seconds.** A short
  checking state does not interrupt the wait. `--once` reports false at once.

So a `REVIEWED` with findings next to a running CI exits 0 with a "CI has not
settled" step, and a CI that passed first exits 7. In both cases the last NEXT step is
the command that waits for the side still open: `--wait-for ci` returns once
CI has settled, even if it already has; `--wait-for review` ignores CI unless
it fails, and returns on a review with no findings too. `--wait-for both` is the old behavior: return only once both have
settled. `--once` never waits for either.

## Merge readiness

The poller reads the target branch tip separately from the PR's head SHA on
every round. `Merge readiness: YES` only reports Gitea's mergeability field;
it is **not** an approval or proof that head-SHA CI tested the merged tree.
`NO` blocks a merge, but does not diagnose a conflict by itself: it can mean
checking, draft, conflict, or another block. `UNKNOWN` (null or absent field)
is not a pass: retry a fresh PR check. If the target branch cannot be read,
its tip is `UNKNOWN`; the poller can still report review and CI, but not merge
readiness. Closed PRs do not require a live target tip.

Immediately before merging, refresh the PR and target tip, verify Gitea's
mergeability, and check that CI covers the current combined tree. If the
branch moved, update/rebase and rerun the appropriate checks. Only Gitea's
merge operation can reject a last-second race atomically.

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

## GitHub: CI only

On github.com no review is watched, so the review column above does not
apply: the report says `review: NOT_MONITORED` and the code is CI's.

| verdict | exit | meaning |
|---|---|---|
| `CI_PASSED` | 11 | Every check at the head succeeded (`success`, `neutral` or `skipped`). Not a review and not a merge approval. |
| `CI_FAILED` | 8 | A check concluded `failure`, `cancelled`, `timed_out`, `action_required`, `stale` or `startup_failure`, or a status is `failure` or `error`. Returns at once. |
| `PENDING` | 7 | `--once` or a closed PR, with CI running or absent. Not a pass. |
| `TIMED_OUT` | 2 | CI was still running, or nothing reported at all, when the budget ran out. A repo with no CI never passes. |
| `UNREACHABLE` | 1 | The PR or CI could not be read: a 4xx at once, or an outage or rate limit that lasted to the deadline (or `--once`). CI reads `UNKNOWN`, never `NONE`. |
| `BASE_MOVED` | 9 | The target branch moved during the wait. |
| `NOT_MERGEABLE` | 10 | GitHub reports `mergeable: false` for 90 seconds, or under `--once`. A CI result waits out that grace, since readiness outranks it. |

CI is the newest check run per application and name, and the newest status
per context, across every page. A re-run replaces the run it repeats. A push
restarts the wait and drops the old head's CI. `Merge readiness: UNKNOWN`
means GitHub is still computing mergeability. The same rule as on Gitea
holds: immediately before merging, refresh the PR and target tip.

## Reading the output

- **`requested: NO` is not a reason to skip waiting.** The bot reviews on open
  regardless. The header reports it because re-requesting `review-bot` is the
  one lever you have on a `TIMED_OUT`.
- **If you list reviews yourself, walk every page.** The API pages at 50,
  oldest first, so page 1 alone hides the newest reviews.
- **The bot is not deterministic.** A later clean review does not clear an
  earlier finding. Check the earlier finding against the code.
