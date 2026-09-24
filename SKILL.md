---
name: babysit-pr
description: Use before merging a pull request on a Gitea instance. It waits until review-bot's automated review and the PR's CI have both finished, reports what each found, and stops. Triggers on "babysit this PR", "wait for the review", "is the bot done", "is CI green on this Gitea PR", or any merge of a Gitea PR opened while the reviewer is live.
---

# Babysit a PR until the reviewer has spoken

`review-bot` (the `gitea-review-agent` companion project) reviews every PR on
open, on push, and when a review is requested. It posts one Gitea review with
inline comments. **It cannot block a merge**: it only ever posts `COMMENT`,
never `APPROVE` or `REQUEST_CHANGES`, so branch protection has nothing to gate
on. A review can take ~30 minutes and often lands after a fast merge. This
skill is what makes you wait.

The rule underneath everything: **silence or an ambiguous answer is never a
pass.** Every verdict below exists so that "nobody answered" and "I could not
look" never get reported as "clean".

## Run it

```bash
python3 ~/.claude/skills/babysit-pr/scripts/poll_review.py > "$OUT" 2>&1; echo "exit $?"
```

Repo and PR are inferred from the checkout's Gitea remote and branch. Flags:

| flag | default | |
|---|---|---|
| `--repo owner/name --pr N` | inferred | override the inference |
| `--once` | off | check now and report; don't wait |
| `--timeout-minutes` | 35 | the review budget. It restarts when the head moves |
| `--interval` | 30 | seconds between polls |
| `--no-fail-fast` | off | keep waiting for the review after CI has failed |
| `--full` | off | keep the per-file table the bot puts in the review body |

| env | default | |
|---|---|---|
| `GITEA_BASE_URL` | `https://gitea.example.com` | your instance |
| `GITEA_TOKEN` | the `tea` login whose `url` equals `GITEA_BASE_URL` | never another host's login |
| `REVIEW_AGENT_STATE_URL` / `_HEALTH_URL` | `http://localhost:8000/state`, `/healthz` | the agent's own endpoints |
| `REVIEW_AGENT_STATE_TOKEN` | unset | only if the deployment sets `STATE_TOKEN` |
| `REVIEW_BOT_USERNAME` | `review-bot` | |
| `CI_WORKFLOW_FILE` / `CI_JOB_NAME` | unset (every run at head) | narrow CI to the one workflow or job that gates |

The poller only reads. It never merges, comments, resolves or edits.

**Backgrounding.** Redirect to a file and read it after the process exits.
The exit code is the signal. Do **not** follow the file with a bare
`tail -f`: it outlives the poller, and one per round piles up as orphaned
processes. If you want live output, bound it:
`timeout 40m tail -n +1 -f "$OUT"`. Run one poller per PR, and stop it if
the PR closes (a closed PR is reported once and never waited on anyway).

## The review verdict: this decides the exit code

| verdict | exit | meaning | what you do |
|---|---|---|---|
| `REVIEWED` | 0 | The newest bot review anchored to the **current head**. Findings are printed with thread id and open/resolved status. | Read the overview, the inline findings **and** the "unanchored finding(s)". Verify each against the code before acting on it. |
| `UNREACHABLE` | 1 | Gitea stayed unreachable through the deadline (or under `--once`). Exit 1 is also a usage error. | No verdict. Not a pass. Retry when Gitea is back. |
| `TIMED_OUT` | 2 | Nothing arrived within the budget, and the agent named no reason. It is still working, or could not be asked. Health and counters are printed. | **Not an approval.** `deliveries: 0` means the webhook never arrived. A nonzero `rejected_signature` means the secret is wrong. Re-request with the printed curl, or merge while saying plainly that it went in unreviewed. |
| `FAILED` | 3 | The bot posted `⚠️`, or the agent reported a failure. `credentials`, `quota`, `oversized` and `backend_rejected` need an operator. `generic`, `backend_error` and `review_timeout` might pass on a fresh review request. | Fix the service, or merge knowing it is unreviewed. Never treat it as clean. |
| `SKIPPED` | 4 | The diff is too large (`MAX_DIFF_LINES` 4000 / `MAX_DIFF_BYTES` 400000). No review is coming. | Split the PR, or review it yourself. |
| `STALE` | 5 | The only review is for an **older SHA**, because you pushed since. Its findings are printed in full under a banner naming that SHA. | Read them, since they usually still apply. The poller keeps waiting and exits 5 only at the deadline or on a CI failure. |
| `DECLINED` | 6 | The agent says no review is coming for this SHA, and why: diff empty after `SKIP_PATHS`, `nothing_new_since_last_review`, superseded, a bot-authored PR, `lost_on_restart`… | Read the reason. `nothing_new…` means the last review stands. `lost_on_restart` means nobody reviewed this, so re-request. |

**The fail-fast exit is not a verdict.** When CI fails first, the poller stops
and returns the review's code as it stands: 2 if the review is still pending,
5 if it is stale. A banner says so. Read the banner, not the number.

## The CI verdict: reported, never folded into the exit code

The poller reads every workflow run at the head SHA, keeps the **newest run
per workflow and event**, and takes the worst result across all their jobs.

| CI | meaning |
|---|---|
| `PASSED` | Every job succeeded. `skipped` and `neutral` jobs are listed as `(not gating)`. |
| `FAILED` | At least one job completed without succeeding (`cancelled` included). It is named in the detail, and the wait stops. |
| `RUNNING` | Something is queued or running. Past 15 minutes it is flagged as **possibly hung**. |
| `UNKNOWN` | **The poller could not read CI.** A 4xx (the token cannot read Actions, or Actions is off) is reported as-is. A 5xx or a timeout is retried until it clears. This is *not* "no CI". |
| `NONE` | No run at this head, or every job was skipped. It counts as settled only after 90s of watching this head, because Gitea may not have created the run yet. |

`PASSED` CI next to a `FAILED` review is still exit 3. If the review decided
but CI is still open at the deadline, you get the review's code plus a "CI
did not settle" note. Check the run yourself before merging.

## Rules when reading the output

- **Zero inline comments is not zero findings.** Findings the bot cannot
  anchor go into the review body under *Additional notes*. The poller prints
  them as "unanchored finding(s)". The overview prose can also outrank both
  ("Three correctness defects remain" with one inline comment).
- **A STALE review is real.** It is not a verdict on what you are merging, but
  its findings usually describe code you still have.
- **The bot is not deterministic.** A later clean review does not clear an
  earlier finding. Check the earlier finding against the code.
- **`requested: NO` is not a reason to skip waiting.** The bot reviews on open
  regardless. The header reports it because re-requesting `review-bot` is the
  one lever you have on a `TIMED_OUT`.
- **If you list reviews yourself, walk every page.** The API pages at 50,
  oldest first, so page 1 alone hides the newest reviews.
- **Say `TIMED_OUT` as `TIMED_OUT`.** Never "no issues found" or "came back
  clean".

## Replying to findings and resolving threads

The poller never judges a finding. Whoever evaluates it decides, and then
uses `reply_finding.py` to act. Gitea has no reply
endpoint. A reply is a new review comment at the same `(path, position)`, and
the script does the JSON escaping that a hand-typed curl gets wrong:

```bash
python3 ~/.claude/skills/babysit-pr/scripts/reply_finding.py \
  --repo OWNER/REPO --pr N --comment-id ID --path app/x.py --position 355 \
  --body "Fixed in abc1234: ..." [--resolve]
```

`--comment-id`, `--path` and `--position` come straight from the poller's
inline output. **Resolving is an acknowledgment to the bot.** A resolved
thread drops out of the PR view, and the bot stops re-checking that finding
for good. So:

| the finding is… | do |
|---|---|
| fixed by a commit that touches the file | reply with the SHA, then `--resolve` |
| disagreed with, or still under discussion | reply, **leave it open**, and file no issue. Closing it is the human's call |
| verified real, but out of scope for this PR | search issues first (`tea issues --repo R --state all`), because the bot re-flags every push. **Ask before filing.** File it with the analysis in the body (`--description "$(cat body.md)"`), reply linking the issue, then `--resolve` |
| not yet verified | reply only. "Looks wrong" is not a resolution |

Replies by anyone other than the bot never enter its dedup set, so replying
alone changes nothing on the next push.

## Tests

Plain Python, no pytest. Run after any change:

```bash
cd ~/.claude/skills/babysit-pr/scripts && python3 test_poll_review.py && python3 test_reply_finding.py
```

The incidents behind these rules, and when each was verified live, are in
[HISTORY.md](HISTORY.md). Read it before loosening a rule.
