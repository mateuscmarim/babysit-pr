# babysit-pr: why it is shaped this way

The incidents behind the rules in `SKILL.md`. SKILL.md says what to do; this
file says what went wrong when it was not done. Read it before loosening a
rule. Newest first.

## 2026-09-24: `--once` exited 0 on a running CI, and another commit's run counted

A second review pass. Each bug was reproduced against the `main()` harness
before it was fixed.

- **`--once` was silent about unsettled CI.** A `REVIEWED` next to a running
  CI hit the `once` action, which printed no epilogue: exit 0 and a
  `CI: RUNNING` block, nothing else. `--once` is the "can I merge now?" check,
  so that was the shortest path to a false all-clear. `once_notes` now says
  CI has not settled.
- **`PENDING` was undocumented.** `--once` or a closed PR with no review
  printed `=== PENDING` and exited 2, the code SKILL.md gave only to
  `TIMED_OUT`. It now has a row in the table and a banner saying it is not a
  pass.
- **CI trusted the server's `?head_sha=` filter.** A Gitea that ignores the
  parameter lists every run in the repo. The newest run per workflow, from
  any commit, then became this head's CI verdict, and it could shadow the
  head's own run. `ci_jobs` now drops runs whose `head_sha` differs.
- **SKILL.md's run command could not work as written.** `$OUT` was never
  set, and a 35-minute wait outlives the Bash tool's foreground timeout. It
  now names a file and says to use `run_in_background`.

## 2026-09-24: an unreadable CI read as "no CI", and other ambiguities

This came out of a review of the whole skill. Every fix here closes a path
where an answer nobody had actually checked could still exit 0.

- **A CI API error read as `NONE`.** `ci_jobs` swallowed every exception and
  returned `[]`. `classify_ci` then reported that as "no workflow run found",
  which settles after the 90s grace. So a token that could not read Actions
  (a 403), next to a clean review, exited 0 on a gate nobody had seen.
  `read_ci` now returns `UNKNOWN`. A 4xx is permanent and gets reported. A
  transport error or 5xx holds the wait open until it clears. A single run
  whose jobs cannot be fetched is kept as an `_error` entry rather than
  dropped, since it could be the failing one.
- **The oldest review at head won.** Gitea lists reviews oldest first and
  `classify` took the first match. So when a review of the same commit was
  requested again, the poller reported the superseded review. It now takes
  the newest by `(submitted_at, id)`, and `STALE` does the same.
- **A superseded cancelled run failed CI forever.** A concurrency group that
  cancels an earlier run of the same workflow at the same SHA left a
  `cancelled` job in the worst-of. `ci_jobs` now keeps only the newest run per
  `(workflow file, event)`. A push run and a pull_request run of the same
  commit both still count.
- **Jobs were read from page 1 only.** A matrix build with more than 50 jobs
  lost the rest. The runs and jobs listings are both paged now.
- **An ssh remote with a port was not recognised**
  (`ssh://git@host:2222/owner/repo.git`), and the https match was a substring
  check against the base URL. `repo_from_remotes` now matches on the hostname.
- **The tea token came from the first `token:` line in the config**, whatever
  host that login was for. With two logins configured, that sent one server's
  credential to the other. `gitea_auth.tea_token` now picks the login whose
  `url` is `GITEA_BASE_URL`, and `reply_finding.py` shares it.
- **An outage inside the loop ended in a traceback.** `api()` retried a single
  call, but a Gitea that stayed down through the retries killed the run. Now
  the round is skipped. If Gitea is still down at the deadline, or under
  `--once`, the poller prints `UNREACHABLE` and exits 1. It never reports
  TIMED_OUT, since that would claim the bot was the silent one.
- **A merged PR was waited on for 35 minutes.** A closed PR is now checked
  once and reported. A PR closed mid-wait stops the wait.
- **Docs claimed things the code did not do.** The docs said "a completed job
  cannot un-fail", but a manual re-run does exactly that. They said "5 STALE
  (--once only)", but a deadline STALE also exits 5. The CI grace comment
  blamed rebases for rewriting the *author* date. Rebases rewrite the
  committer date, and the poller reads that one. The fail-fast message
  called every workflow "quality-gate".
- **The tests failed fatally and tested source text.** The first failed
  `assert` hid every check after it. Three tests grepped `poll_review.py` for
  variable names. They now record failures and keep going, and a `main()`
  harness with a fake clock and a fake Gitea covers the loop end to end.
  That includes the head moving, outages, the deadline and a closed PR.
  Each of the fixes above was checked by reintroducing the bug and watching a
  test fail.

## 2026-09-09: resolving a thread became an acknowledgment

Since `gitea-review-agent#65`, a resolved conversation tells the bot to stop.
The resolution pass stops re-checking that finding. It posts no "bad fix" on
it. It still matches a reworded repeat against it, so neither a verbatim copy
nor a restated one is posted again. Resolving is the one way to tell the bot
"won't fix". That is why resolving a disagreement does more than hide it: it
also silences the bot on that defect for good.

## 2026-08-28: stale reviews carried real findings, and page 2 was invisible

**The previous-head trap.** A review takes up to ~30 minutes. Push in that
window and the review lands anchored to the old SHA. On
`milex-scopeline-server#14` that happened four times, with reviews 304, 308,
313 and 314, and each one carried real findings. Three were found only by
listing the PR's reviews by hand, long after the fact. The poller used to
print `STALE` as a single detail line. It now prints the stale review's
overview, inline comments and unanchored notes under a banner naming the SHA
they describe. The verdict stays `STALE`, exit 5. Promoting it to `REVIEWED`
would be laundering.

**Pagination.** The reviews listing pages at 50, oldest first, and returns
page 1 silently if you do not ask for more. On PR 14, with 78 reviews, the
poller saw ids 223–290 and reported review 267 as the latest. Every review
from the rounds actually being worked on sat on page 2. Nothing in the output
looked wrong. The same PR and the same `--once` command, one page-2 fetch
apart: before the fix, `STALE` off review 267; after it, `REVIEWED` off
review 318 at the head, with one open inline finding.

**The bot is not deterministic.** Review 316 traced a tree that still had both
defects review 314 had just flagged, and reported no confirmed defects. A clean
review does not clear an earlier finding after the fact.

**Deferred findings.** An open thread on a merged PR is not a tracker. On the
`gitea-review-agent` repo's PR #34, finding #3128 (`extract_global_duplication`
defaulting a missing percentage to `0.0`) was real on `main` but not in that
PR's diff. It was filed as issue #37 and the thread resolved with a link. It
was the repo's first issue ever, which is why the skill says to ask before
filing: an untriaged tracker is a tidier landfill, not a fix.

**Fail-fast.** On `gitea-review-agent#34` the poller waited 269 seconds to
report a CI failure that was already visible at 66. A failed CI now ends the
wait (`--no-fail-fast` to override).

## 2026-08-27: CI hung while the review said nothing

`nasa-agent` PR #59 hung silently for over 25 minutes mid-run. The cause was
`uv`/`uvx` lock contention in a ported concurrency change. review-bot reacts
to the diff, not to CI, so nothing in its report caught this. The poller now
waits for CI at the head SHA too. None of those jobs set `timeout-minutes`, so
a run past `CI_SLOW_THRESHOLD_S` (15 minutes) is flagged as possibly hung.
That threshold is set from observed passing durations: 6–7 minutes on mdbin,
11 on nasa-agent.

**A hardcoded workflow read as no CI.** `ci_jobs` used to default to
`quality-gate.yml`/`gate`. `milex-scopeline-server` ships `tests.yml` and
`eval.yml`, so it read as `CI: NONE` even though both had passed. It now
discovers every workflow run at the head. Verified against
`nasa-agent#58` (`--once`), where job 5076 reported `CI: PASSED` next to a
clean `REVIEWED`.

**A skipped job read as a failure.** `marim-harness`'s quality gate has a
`promote` job guarded by `github.event_name == 'push'`, so that a
pull_request run never holds a `contents: write` token. That job is skipped
on every PR. Counting it as failed turned run 2649 (gate succeeded, report
succeeded, overall `success`) into a red CI. Fail-fast then bailed before the
review landed. This would have recurred on every PR in the repo. `skipped` and
`neutral` are now non-gating. They are named in the detail with
`(not gating)`, and a run where every job was skipped is `NONE`, not
`PASSED`.

**The NONE race.** `agent_state` returns `DECLINED` almost instantly for a
diff that filtered to empty, or for `nothing_new_since_last_review`. If the
first poll lands before Gitea has created the workflow run, a few seconds
after a push, the old `ci_done = state != "RUNNING"` returned inside the first
interval with `CI: NONE` for a gate that was seconds from starting. That is
the same false all-clear the `TIMED_OUT` rule exists to prevent. `NONE` now
settles only after 90s spent watching this head. The clock starts when the
poller first sees the head, not at the commit's date: a rebased or
cherry-picked commit can carry a committer date that is hours older than the
push.

## 2026-08-26: the first live checks, and the backgrounding leak

The first live checks:

- `REVIEWED` on `milex-scopeline-server#15`: review 139 at head, 1 inline
  comment.
- `FAILED` on `nasa-agent#45`: a generic `⚠️` posted at 22:23:12Z.
- `TIMED_OUT` on `twm#13` and `milex-scopeline-server#14`: both reported the
  agent up and silent.

`SKIPPED` has unit coverage but has not been observed in the wild. `STALE`
is common; see 2026-08-28.

**Zero inline comments did not mean zero findings.** On
`milex-scopeline-server#15` the prose overview said *"Three correctness
defects remain"* while only one comment anchored. `validate_comments` moves
unanchorable findings into an **Additional notes** section of the body. The
poller now prints that section and the full overview.

**`requested_reviewers`.** Only the single-PR endpoint populates it;
`/pulls?state=all` returns `[]` for every PR. A `COMMENT` review does not
clear the request (`milex-scopeline-server#15` still lists `review-bot`).
Whether the `opened` trigger fires at all is unverified: the only two PRs with
no bot output were opened before the agent was re-enabled that day. Draft
status does not exclude a PR, since `nasa-agent#45` is a draft and was
reviewed.

**The leak.** On `milex-scopeline-server#14`, 13 rounds left 13 orphaned
processes, the oldest 12.5 hours old. The poller exited cleanly every time.
The leak was the wrapper watching its output:
`tail -n +1 -f babysit.out | grep ...`. `tail -f` does not know the poller
exited, so the pipeline never returns. The fix is to redirect to a file and
read it after exit, or to bound the follower with `timeout`.

**Retries.** `milex-scopeline-server#26` died twice in one session on a single
`/pulls/N/reviews` timeout, with the review still pending. `api()` now retries
transport failures and 5xx up to 3 times with a growing backoff.
