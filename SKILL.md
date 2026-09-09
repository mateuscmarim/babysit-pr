---
name: babysit-pr
description: Use before merging a pull request on gitea.example.com — waits for review-bot's automated review AND the quality-gate CI run to both finish, reports what each found, and stops. Triggers on "babysit this PR", "wait for the review", "is the bot done", "is CI green", or any merge of a gitea PR opened while the reviewer is live.
---

# Babysit a PR until the reviewer has spoken

`review-bot` (the [gitea-review-agent](https://gitea.example.com/mateuscmarim/gitea-review-agent),
deployed on `localhost:8000`) reviews every PR on open, on push, and on
an explicit review request. It posts a single Gitea review with inline comments.

**It cannot stop you merging.** `build_review_payload` hardcodes
`"event": "COMMENT"` — never `APPROVE`, never `REQUEST_CHANGES` — so branch
protection has nothing to gate on. A review takes up to ~30 minutes in the worst
case (`REVIEW_TIMEOUT_SECONDS=600` × `REVIEW_MAX_ATTEMPTS=3` plus backoff) and
routinely lands after a fast merge. This skill is the only thing that makes you
wait.

## Run it

```bash
python3 ~/.claude/skills/babysit-pr/scripts/poll_review.py
```

Repo and PR are inferred from the current checkout's `gitea.example.com` remote and
branch. Override with `--repo owner/name --pr N`. Other flags: `--once` (check
now, don't wait), `--timeout-minutes` (default 35), `--interval` (default 30s),
`--full` (keep the per-file table, which is trimmed by default).

Two environment variables point it at the agent's state endpoint:
`REVIEW_AGENT_STATE_URL` (default `http://localhost:8000/state`) and
`REVIEW_AGENT_STATE_TOKEN`, needed only if that deployment sets `STATE_TOKEN`.
Neither is required — an agent that cannot be reached is reported as
unreachable, and the poller falls back to reading Gitea alone.

Gitea reads retry. A 35-minute wait at a 30s interval is on the order of
seventy API calls, and one timeout used to end the whole run with a traceback
where the verdict should be — `milex-scopeline-server#26` died that way twice
in one session, both times on `/pulls/N/reviews` with the review still
pending. `api()` now retries transport failures and 5xx up to `API_RETRIES`
(3) with a growing backoff; a 4xx is an answer (wrong token, wrong repo) and
is raised at once.

The script only reads. It never merges, comments, resolves, or edits — see
[Replying to findings](#replying-to-findings-and-resolving-threads) for the
write contract it hands downstream.

### Backgrounding it without leaking followers

The poller blocks for up to 35 minutes, so it usually gets backgrounded, and
this skill is designed to be re-run every round — which means a per-invocation
leak compounds. On `milex-scopeline-server#14` it compounded into 13 orphaned
processes, the oldest 12.5 hours old, across 13 rounds.

**The poller was not the leak.** It ran and exited cleanly every time. The
leak was the wrapper watching its output:

```bash
# LEAKS: tail -f outlives the script it is following, forever.
zsh -c 'tail -n +1 -f babysit.out | grep -E --line-buffered "^=== |REVIEWED"'
```

`tail -f` has no idea the poller exited. It follows a file nobody will ever
write to again, and the pipeline it anchors never returns.

- **Redirect to a file and read it after the poller exits.** The exit code is
  the signal; the file is the whole transcript. No follower needed.
- If you genuinely want live output, **bound the follower**:
  `timeout 40m tail -n +1 -f "$OUT" | grep ...` — longer than
  `--timeout-minutes`, so it never truncates a real run, but finite.
- **One poller per PR at a time.** A second one on the same PR tells you
  nothing new and doubles the cleanup.
- **Stop it when the PR closes.** A merged PR will never produce another
  review; a poller still waiting on one is pure background noise.

If background tasks have already piled up: they are `tail`/`grep`, not
`poll_review.py`. Check with `ps` before assuming the skill misbehaved.

## It also watches CI

`review-bot` posting a review says nothing about whether the PR's own CI
actually passed — nasa-agent PR #59 hung silently for 25+ minutes mid-run on
2026-08-27 (a `uv`/`uvx` lock contention bug in a ported concurrency change)
with nothing in the review-bot report to catch it, since the bot only reacts
to the diff, not to CI. The poller now waits for **both** the review and
CI at the PR's head SHA before returning.

By default it does not assume a workflow file or job name: `ci_jobs()`
discovers every workflow run at the head SHA and pulls every job out of each
one, so it works unmodified on a repo it has never seen before —
`milex-scopeline-server` ships `tests.yml` + `eval.yml`, not
`quality-gate.yml`, and a hardcoded default would have silently read that as
`CI: NONE` even though both had already passed. `classify_ci` then aggregates
all of them worst-of: one `FAILED` job fails the whole verdict even if
everything else passed; short of that, one `RUNNING` job keeps it open even
if the rest already finished. Set `CI_WORKFLOW_FILE` and/or `CI_JOB_NAME`
(both unset by default) to narrow the check to one workflow file or job name
instead — for a repo that runs unrelated workflows on every PR and only one
of them actually gates merges.

CI is reported as a fourth block, printed under the review verdict at every
return point:

| state | what it means |
|---|---|
| `PASSED` | Every matched job completed with a successful conclusion. |
| `FAILED` | At least one matched job completed without succeeding — which one, and its conclusion, are named in the detail line. `cancelled` counts: an aborted job decided nothing, so it is not softer than a failure. `skipped` does **not** — see below. |
| `RUNNING` | At least one matched job is still queued or in progress (with everything else already passed or nothing failed yet). Past `CI_SLOW_THRESHOLD_S` (15 minutes — set from mdbin's 6-7m and nasa-agent's 11m observed passing durations) the detail flags it as **possibly hung**, since none of these jobs set `timeout-minutes` and nothing else will ever say so. |
| `NONE` | No run matched at this head SHA — or every job that did match was skipped, so nothing actually attested to the commit — covers both "nothing has triggered yet" and "this repo triggered nothing matching `CI_WORKFLOW_FILE`/`CI_JOB_NAME`". Indistinguishable from the jobs list, but **not** equally safe to act on, so `NONE` does not end the wait for the first 90s (`CI_NONE_GRACE_S`). |

**A `NONE` that outlasts the grace window means "nothing to report". A `NONE`
seen immediately does not.** `ci_settled` is the difference, and the trap it
closes is this: `review_done` includes `DECLINED`, and `agent_state` returns
`DECLINED` almost instantly for a diff that filtered empty under `SKIP_PATHS`,
or for `nothing_new_since_last_review`. Pair that with a first poll landing
before Gitea has created the workflow run — a few seconds after a push — and
the old `ci_done = state != "RUNNING"` returned inside the first interval
reporting `CI: NONE` for a gate that was seconds from starting. A false
all-clear, the same failure the `TIMED_OUT` rule exists to prevent, wearing a
clean early exit.

The grace is dated from **when the poller started watching this head**, and
resets on a head move. Deliberately not from the head commit's date: a commit
is dated when it was *authored*, which a rebase rewrites and which can precede
the push by an hour — so a head that reached the server seconds ago can carry
an hour-old date and would read as long-settled, putting the race straight
back.

The cost is bounded and lands in the right place: only repos with genuinely no
matching workflow pay it, and only when the review decided first. While the
review is still pending the loop is waiting on it anyway, so the grace is free.
`--once` is unaffected — it returns immediately by design.

If the review finishes first and CI is still `RUNNING` at the timeout deadline,
the poller prints a short "review decided, CI still open" line instead of the
full `TIMED_OUT` block — the review verdict is real and does not need to be
re-litigated just because CI is slow.

**A skipped job is not a failed one.** A job whose `if:` evaluated false
completes with conclusion `skipped`, and that is the *designed* outcome for a
conditional job — Actions has no way to say "do not create this job at all", so
a skipped job is the only shape the feature has. `marim-harness`'s quality gate
splits baseline promotion into a `promote` job guarded by
`github.event_name == 'push'`, precisely so a `pull_request` run never holds a
`contents: write` token; that job is skipped on every PR run and always will be.
Counting it as `FAILED` reported run 2649 — `gate` succeeded, `report` succeeded,
overall conclusion `success` — as a red CI, and because `FAILED` is terminal it
bailed out before the review could land. That false red would have recurred on
every PR in the repo forever. `skipped` and `neutral` are now excluded from the
worst-of ordering: they neither fail the verdict nor hold it open, and they are
still named in the `PASSED` detail with `(not gating)` so a job you expected to
run cannot vanish quietly. If *every* matched job was skipped the verdict is
`NONE`, not `PASSED` — nothing ran, and that is not an all-clear.

**A `FAILED` CI stops the wait immediately.** `classify_ci` checks failed before
running, so `FAILED` is terminal the moment it appears — a completed non-success
job cannot un-fail. Waiting out the rest of the review budget after that buys
nothing: you have to push a fix regardless, and that push makes any review
landing in the meantime stale anyway. On `gitea-review-agent#34` the old
behaviour cost 269 seconds of waiting to report a failure already visible at 66.
A `STALE` review's inline findings are still fetched before bailing, so the
early return does not swallow them. Pass `--no-fail-fast` to wait anyway.

**Exit codes are unchanged and still driven only by the review verdict** — CI
status is additive and informational, never gating. A `PASSED` CI block next
to a `FAILED` review is still a `FAILED` exit; read both blocks, not just the
exit code. The fail-fast return above is a *scheduling* change and does not
touch this: it returns the review's own code (`PENDING` falls back to 2, as
under `--once`), so read the banner and the CI block, not the number. An exit 2
from a fail-fast return means "CI failed, review never decided" — not the
"review-bot went silent" that a plain `TIMED_OUT` means.

## The six verdicts

| verdict | exit | what it means | what you do |
|---|---|---|---|
| `REVIEWED` | 0 | A review anchored to the **current head SHA**. Findings printed with thread id and open/resolved status. | Read them. Findings → `superpowers:receiving-code-review`, then reply per the contract below. Clean → merge is yours to make. |
| `FAILED` | 3 | Bot posted `⚠️`, or the endpoint named a failure. Variants that need an operator and **will not self-heal on the next push**: `credentials`/`backend_auth`, `quota`/`backend_quota`, `oversized`/`backend_input_too_large`, `backend_rejected` (the backend itself declared the failure permanent — a retired model, a rotated-out key). Variants that might: `generic`, `backend_error`, `review_timeout` — the agent already spent `REVIEW_MAX_ATTEMPTS` on those, so a fresh review request is the retry, not a longer wait. | Fix the service or merge knowingly unreviewed. Never treat as clean. |
| `SKIPPED` | 4 | The diff exceeded `MAX_DIFF_LINES` (4000) or `MAX_DIFF_BYTES` (400000). No review is coming for this SHA, ever. | Split the PR, or review it yourself. |
| `DECLINED` | 6 | The agent's `/state` endpoint says no review is coming for this SHA, and named the reason: the diff filtered to empty under `SKIP_PATHS`, nothing new since the last review, a superseded push, a declined event, a PR the bot authored, or a job lost to a container restart. | Not an approval, and not a failure either. Read the reason. `nothing_new_since_last_review` means the previous review still stands. `lost_on_restart` means nobody reviewed this — re-request the bot. |
| `STALE` | 5 | A review exists but for an older SHA — you pushed since. **Its findings are printed in full**, under a banner naming the SHA they describe. | Keep waiting (the poller does this automatically) — but read the findings, they are usually about code you still have. See [the previous-head trap](#the-previous-head-trap) below. |
| `TIMED_OUT` | 2 | Nothing after the budget, and the endpoint did not name a reason either — so the agent is still working, or it could not be asked. Prints `/healthz`, the agent's answer for this SHA, and the service counters. | **Not an approval.** A `deliveries` counter of 0 means the webhook never arrived; a nonzero `rejected_signature` means the secret is wrong. Check the service logs before merging. |

## The previous-head trap

**review-bot routinely reviews the head you have already pushed past.** A
review takes up to ~30 minutes; if you push again in that window, the review
lands anchored to the *old* SHA. Confirmed four times on
`milex-scopeline-server#14` — reviews 304, 308, 313 and 314 — each carrying
real findings. Three of them were found only by enumerating the PR's reviews
by hand, long after the fact.

Anything that filters reviews on the current head silently drops these. That
includes the obvious one-liner:

```bash
# WRONG: throws away most of what the bot actually said.
[ "$(jq -r .commit_id <<<"$review")" = "$HEAD" ] || continue
```

The poller no longer does this. `STALE` carries the review's overview, inline
comments and unanchored notes, and prints them under a banner naming the SHA
they describe. The verdict stays `STALE` and the exit code stays 5 — an
older-SHA review is *not* a verdict on what you are about to merge, and
promoting it to `REVIEWED` would be the same laundering the `TIMED_OUT` rule
exists to prevent. Only the silence was fixed.

**The list endpoint paginates at 50, oldest first** — and returns page 1
silently if you do not ask. That is the worst possible default here, because
the reviews you need are the newest ones. The poller had this bug: on PR 14,
which reached **78 reviews**, it saw ids 223–290 and reported review 267 as
the latest, while 304, 308, 313, 314 and 318 — every review from the rounds
being worked on — sat on page 2, invisible. Nothing in the output looked
wrong. `api_paged` now walks pages until one comes back short or empty.

If you enumerate reviews yourself for any reason, paginate. A PR only has to
survive 50 reviews before an unpaginated fetch starts quietly lying.

**The bot is not deterministic.** Review 316 traced a tree that still
contained both defects review 314 had just flagged, and reported no confirmed
defects. A clean review is **not** retroactive clearance for an earlier
finding — verify the earlier finding against the code, not against the newer
review.

## Was the reviewer actually requested?

The header line reports it (`requested: yes` / `NO`), but it is **not** a gate,
and a `NO` is not a reason to skip waiting. `parse_event` enqueues on `opened`
regardless of who was requested — `review_requested` is a third trigger, not a
precondition.

It is reported because it is the one lever available on a `TIMED_OUT`: adding
`review-bot` as a reviewer fires `review_requested` and enqueues a fresh job.
The script prints that curl rather than running it, since it stays read-only.

Two facts worth not re-deriving:

- **Only the single-PR endpoint populates `requested_reviewers`.** `/pulls?state=all`
  returns `[]` for every PR, which reads as "nobody was asked" and is wrong.
- **A `COMMENT` review does not clear the request.** `milex-scopeline-server#15`
  still lists `review-bot` after being reviewed, so the field stays readable
  after the fact rather than flipping when the review lands.

Whether the `opened` trigger is currently firing at all is **unverified**. The
source says it does; the only two PRs with no bot output were opened before the
agent was re-enabled on 2026-08-26, so request-status and open-date are
confounded in the available evidence.

A PR **authored by the bot** is the one true never-reviewed case — `parse_event`
returns `None` to prevent loops. The script detects that and exits immediately
instead of waiting out the full budget.

Draft status is *not* an exclusion: `nasa-agent#45` is a draft and the bot
picked it up.

## The rule that matters

**Silence is still never a pass — but there is much less of it.**

`worker.py` has three paths that return without posting anything (the diff
filtering to empty under `SKIP_PATHS`, "nothing new to review since last
review" on a re-push, a declined webhook event), and its queue is in-process,
so a container restart drops in-flight jobs. Those used to be indistinguishable
from "still thinking". They now come back as `DECLINED` with the reason named,
usually within one poll interval.

What survives is the residue: the agent is genuinely still working, or this
poller could not reach it. Both report `TIMED_OUT`, and `TIMED_OUT` still gets
reported as `TIMED_OUT`. Do not summarize it as "no issues found", "review came
back clean", or "nothing flagged". If you merge on a `TIMED_OUT`, say in the
same breath that the PR went in unreviewed.

`DECLINED` is not silence and not a pass either. It says a specific thing did
not happen and why — read the reason before deciding it does not matter.

## Two traps in reading a REVIEWED result

**`0 inline comments` does not mean `0 findings`.** `validate_comments` drops
any finding it cannot anchor to a changed diff line, and those get rendered into
an **Additional notes** section of the review *body* instead. The script parses
the body and prints them under "unanchored finding(s)" — read that section, not
just the inline count.

**The overview can outrank both.** On `milex-scopeline-server#15` the prose
overview said *"Three correctness defects remain"* while only one comment
anchored. The overview is printed in full for exactly this reason.

## Replying to findings, and resolving threads

This skill does not write. It prints each finding's comment id and whether the
thread is open, so the step that actually evaluates the finding —
`superpowers:receiving-code-review` — can act on it. That is the right seam: the
poller has never judged a finding, so it must not be the thing that publishes a
verdict about one.

When that downstream step does respond, two rules:

**Reply to every finding you acted on.** A reply is the record of the decision —
"fixed in `<sha>`" or "declined: <reason>". Gitea has no reply endpoint;
`CreatePullReviewComment` carries no `in_reply_to`. A reply is a **new review
comment at the same `(path, new_position)`**, which Gitea groups into the
existing thread. Use `reply_finding.py` instead of hand-typing this — a reply
body with a quote, backslash, or apostrophe is exactly the kind of thing that
breaks a one-line curl with escaped shell quoting:

```bash
python3 ~/.claude/skills/babysit-pr/scripts/reply_finding.py \
  --repo OWNER/REPO --pr N --comment-id COMMENT_ID \
  --path app/services/x.py --position 355 \
  --body "Fixed in abc1234 — ..." [--resolve]
```

`--path`, `--position` and `--comment-id` come straight out of
`poll_review.py`'s inline-comment output. The script does exactly two things —
POST the reply, then POST the resolve if `--resolve` is passed — and decides
nothing: whether to resolve is still yours to judge, not the script's. It is
the same two calls as the raw API, only with `json.dumps` doing the escaping:

```bash
curl -X POST -H "Authorization: token $GITEA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"event":"COMMENT","body":"","comments":[
        {"path":"app/services/x.py","new_position":355,"old_position":0,
         "body":"Fixed in abc1234 — ..."}]}' \
  https://gitea.example.com/api/v1/repos/OWNER/REPO/pulls/N/reviews

curl -X POST -H "Authorization: token $GITEA_TOKEN" \
  https://gitea.example.com/api/v1/repos/OWNER/REPO/pulls/comments/COMMENT_ID/resolve
```

**Resolve only what you actually fixed.** A resolved thread drops out of the
default PR view, so resolving is the half that can hide a live problem:

- Fixed, with a commit that exists and touches that file → reply naming the SHA,
  then `--resolve` (or the second curl). The fix is in the diff, so the claim
  is checkable.
- **Disagreed with, or still under discussion → reply and LEAVE IT OPEN** (omit
  `--resolve`). Resolving a disagreement hides it, and a PR that looks clean
  because a thread was closed is the same failure the `TIMED_OUT` rule exists
  to prevent. Closing a disagreement is the human's call, not the agent's.
  **Do not file an issue for these** — an issue asserts "this is a real defect
  we intend to fix", which is precisely what you just argued against or have
  not yet settled. Filing one misrepresents your own position.
- **Verified real but out of scope for this PR → file an issue, then resolve.**
  See below; this is the one case where resolving a thread you did not fix is
  correct.
- Never resolve a finding you have not verified. "Looks wrong to me" is a reply,
  not a resolution.

### Deferred findings need somewhere to live

"Deferred" used to fall under leave-it-open, which quietly loses the finding.
An open thread on a **merged** PR is not a tracker: it is off the default view,
nothing lists it, and no one returns to it. The reply promises a follow-up that
nothing records.

So when a finding is real, you verified it, and it does not belong in this PR:

1. **Search existing issues first.** The bot dedups on comment body text, not
   thread state, so the same finding re-flags on every subsequent push. Without
   this step one finding becomes one issue per round.
   ```bash
   tea issues --repo OWNER/REPO --state all
   ```
   Already filed → reply linking the existing issue, resolve, stop.
2. **File it**, with the analysis in the body — not just a pointer back to a
   thread that is about to become invisible:
   ```bash
   tea issues create --repo OWNER/REPO \
     --title "..." --description "$(cat body.md)"
   ```
   The body carries: the PR number and comment id it came from, why it was out
   of scope here, the defect and its impact, a suggested fix, **what you
   actually verified, and what you did not**. Write the description to a file
   and `cat` it in — a body with backticks, quotes and code fences will not
   survive being inlined.
3. **Link it back**, then resolve — the reply is what connects the two, so say
   plainly that you are resolving because it is tracked, not because it was
   dismissed:
   ```bash
   python3 scripts/reply_finding.py --repo OWNER/REPO --pr N \
     --comment-id ID --path p.py --position 149 \
     --body "Tracked as issue #37: <url> ..." --resolve
   ```

**Ask before filing.** Creating an issue publishes a judgment, and everything
else this skill does is either read-only or scoped to a thread the bot already
opened. It is also worth a moment's honesty about the destination: if the repo's
tracker is not actually triaged, an issue is a tidier landfill, not a fix. Say
what you intend to file and let the human decide.

Verified 2026-08-28 on `mateuscmarim/gitea-review-agent#34`: finding #3128
(`extract_global_duplication` defaulting a missing percentage to `0.0`) was real
on `main` but untouched by that PR's diff. Filed as
[issue #37](https://gitea.example.com/mateuscmarim/gitea-review-agent/issues/37) and
the thread resolved with a link. It was the repo's **first ever issue** — which
is the caveat above in one data point.

Run `python3 scripts/test_reply_finding.py` from the scripts directory after
any change to `reply_finding.py`.

Safe to know: replies cannot corrupt the bot. `_collect_seen_keys` filters to the
bot's own reviews, so comments by anyone else never enter its dedup set, and a
reply alone changes nothing on the next push. **Resolving a thread does** —
since `gitea-review-agent#65` (2026-09-09) a resolved conversation is an
acknowledgment: the resolution pass stops re-checking that finding, posts no
"bad fix" on it, and still matches a reworded repeat against it so neither a
verbatim nor a restated copy is posted again. That is the one way to tell the
bot "won't fix", and it is why the resolve rule above matters both ways —
resolving a disagreement does not just hide it from the PR view, it also
silences the bot on that defect for good.

## Verified behaviour

Checked against live PRs on 2026-08-26:

- `REVIEWED` — `trainwithme/milex-scopeline-server#15`, review 139 at head, 1 inline comment
- `FAILED` — `trainwithme/nasa-agent#45`, generic `⚠️` posted 22:23:12Z
- `TIMED_OUT` — `trainwithme/twm#13` and `milex-scopeline-server#14`, both reporting the agent up and silent

`SKIPPED` has unit coverage but has not been observed in the wild.

`STALE` has been, repeatedly: `milex-scopeline-server#14` produced four
previous-head reviews (304, 308, 313, 314) over a long review loop on
2026-08-27/28. It is a common verdict, not an exotic one — which is why it
now prints its findings instead of one line, and why a `STALE` at the
timeout deadline returns 5 rather than being reported as `TIMED_OUT`.

Pagination and the STALE rendering verified 2026-08-28 against
`milex-scopeline-server#14` (`--once`): before the fix the poller reported
`STALE` off review 267 with a single detail line; after it, `REVIEWED` off
review 318 at the head, with its overview and one open inline finding
printed. Same PR, same command, one page-2 fetch apart.

CI check verified 2026-08-27 against `trainwithme/nasa-agent#58` (`--once`):
job 5076 already `completed`/`success` at poll time, reported as `CI: PASSED`
alongside a clean `REVIEWED` verdict, both in one run. (At the time of that
run `ci_jobs` still defaulted to `quality-gate.yml`/`gate`; it has since been
generalized to discover every workflow run at the head SHA by default, see
above — the PASSED result itself is unaffected since nasa-agent's only
workflow is `quality-gate.yml`.)

Run `python3 scripts/test_poll_review.py` from the scripts directory after any
change to the classification logic.
