# GitHub CI-only babysitting - checks

Profile: light
Plan: `.specs/features/github-ci/plan.md`
Feature base: `d2ee17e`

16 checks in 1 slice · 3 one-way doors · 0 open

Proofs run from `scripts/`. `test_github_ci.py -k <id>:` runs only the sections whose title
starts with that check id; the colon keeps `C1:` from matching `C10:`.

GitHub API contract, checked against docs.github.com (API version `2022-11-28`, supported to
March 10, 2028):

- `GET /repos/{owner}/{repo}/pulls/{n}`: `state` open|closed, `merged`, `mergeable`
  true|false|null (null = background job still computing), `head.sha`, `head.ref`,
  `head.repo.full_name`, `base.ref`, `base.repo.full_name`.
- `GET /repos/{owner}/{repo}/pulls?state=open&head=<owner>:<ref>`, `per_page` max 100.
- `GET /repos/{owner}/{repo}/commits/{sha}/check-runs`: `{total_count, check_runs}`, `per_page`
  max 100, `filter` default `latest`; `status` queued|in_progress|completed|waiting|requested|pending,
  `conclusion` success|failure|neutral|cancelled|skipped|timed_out|action_required (plus `stale`
  and `startup_failure` from the check-suite vocabulary) or null; `name`, `app`, `html_url`,
  `details_url`.
- `GET /repos/{owner}/{repo}/commits/{sha}/statuses`: array, reverse chronological, `per_page`
  max 100; `state` error|failure|pending|success, `context`, `target_url`.
- `GET /repos/{owner}/{repo}/git/ref/heads/{branch}`: `object.sha`; 404 when absent.
- Pagination: a `link` header with `rel="next"` when more pages exist; omitted when all results
  fit on one page.
- Rate limits: `403` or `429`; `x-ratelimit-remaining`, `x-ratelimit-reset`, `retry-after`.
- Auth header `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28`. `gh auth token --hostname github.com` prints the token.

## Checks

### S1 - Monitor a GitHub PR's CI · 9 files · 189 KB · ~47k

**C1** - `--provider github --repo o/r --pr 7` issues only `GET` requests, all to `https://api.github.com/repos/o/r/...` paths for the PR, check runs, statuses and the target ref; it never calls the Gitea `token()`, `agent_state` or `health`, and never requests a `/reviews` or `/comments` path (GHCI-01, AC 1)
Proof: `python3 test_github_ci.py -k C1:`

**C2** - Under `--provider auto` in a checkout: the tracking remote's provider wins, then `origin`'s; only github.com remotes selects github; only Gitea remotes selects gitea; with no tracking/origin match and remotes of both providers it exits 1 with `ambiguous`; with no supported remote it exits 1; explicit `--repo o/r --pr 7` outside a checkout runs the Gitea path; `--provider gitea` never selects github (GHCI-01, AC 2)
Proof: `python3 test_github_ci.py -k C2:`

**C3** - GitHub PR inference for branch `feat`: a fork PR in the upstream whose `head.repo.full_name` is a checkout remote and `head.ref` is `feat` is selected; a same-named branch from a repo that is not a checkout remote is ignored; two matching open PRs exit 1 naming `--repo`/`--pr`; zero matches exit 1; detached `HEAD` exits 1 (GHCI-01, AC 3)
Proof: `python3 test_github_ci.py -k C3:`

**C4** - A GitHub run prints `review: NOT_MONITORED`; each of `--wait-for review`, `--wait-for both`, `--no-fail-fast`, `--full`, `CI_WORKFLOW_FILE` and `CI_JOB_NAME` exits 1 with a message naming that option, before any token is read or API called; `--wait-for ci` and `--wait-for any` are accepted (GHCI-01, AC 4)
Proof: `python3 test_github_ci.py -k C4:`

**C5** - Classification: check-run status `queued`, `in_progress`, `waiting`, `requested`, `pending` -> RUNNING; `completed` with `success`, `neutral`, `skipped` -> success; with `failure`, `cancelled`, `timed_out`, `action_required`, `stale`, `startup_failure` -> FAILED; an unrecognized status or conclusion, a null conclusion on `completed`, or a non-object entry -> UNKNOWN; commit status `pending` -> RUNNING, `success` -> success, `failure`, `error` -> FAILED, anything else -> UNKNOWN (GHCI-02, AC 5)
Proof: `python3 test_github_ci.py -k C5:`

**C6** - Newest result per identity: a check run with a higher id and the same app and name replaces an older `failure` (verdict PASSED); the same name from a different app is kept as its own result; for statuses the newest per context wins in both directions (a newer `success` replaces an older `failure`, an older `success` does not replace a newer `failure`) (GHCI-02, AC 5)
Proof: `python3 test_github_ci.py -k C6:`

**C7** - Check runs and statuses are read at `/commits/<exact head sha>/...` with `per_page=100&page=N`; page N+1 is requested only when the response's `link` header has `rel="next"`, and a `failure` that exists only on page 2 makes the run exit 8 (GHCI-02, AC 5)
Proof: `python3 test_github_ci.py -k C7:`

**C8** - Any FAILED result exits 8 `CI_FAILED`; one or more results, all successful, exit 11 `CI_PASSED`; neither prints `REVIEWED`, and no GitHub run exits 0 (GHCI-02, AC 6)
Proof: `python3 test_github_ci.py -k C8:`

**C9** - Unreadable CI: a transient failure on page 2 reads UNKNOWN, never PASSED or NONE; a `401` on check runs exits 1 without sleeping; a `403` with `x-ratelimit-remaining: 0` and a `429` are retried next interval and a later readable success exits 11; a failure lasting to the deadline exits 1; `--once` with a transient failure exits 1 with zero sleeps; a check-runs body that is not an object reads UNKNOWN; a PR fetch that stays unreachable exits 1 `UNREACHABLE` (GHCI-02, AC 7)
Proof: `python3 test_github_ci.py -k C9:`

**C10** - Waiting: RUNNING for two polls then PASSED exits 11 after 2 intervals; no results through the 5-minute budget exits 2 `TIMED_OUT`; RUNNING through the budget exits 2; `--once` with RUNNING or with no results exits 7 `PENDING` with zero sleeps; a closed PR with RUNNING exits 7 with zero sleeps (GHCI-02, AC 8)
Proof: `python3 test_github_ci.py -k C10:`

**C11** - When the head changes mid-wait, the old head's FAILED CI is not reported and CI is read at the new sha; the new head passing exits 11; the deadline restarts, so a head change at 4 minutes lets the run keep waiting past the original 5-minute budget (GHCI-03, AC 9)
Proof: `python3 test_github_ci.py -k C11:`

**C12** - A target-ref sha change exits 9 `BASE_MOVED` even when CI passed or failed; `mergeable: false` held 90 seconds exits 10 `NOT_MERGEABLE`; `false` under `--once` exits 10; one `false` followed by `true` does not exit 10; `mergeable: null` prints `Merge readiness: UNKNOWN`; an unreadable target prints `target tip UNKNOWN` and a do-not-merge step (GHCI-03, AC 9)
Proof: `python3 test_github_ci.py -k C12:`

**C13** - The report prints `o/r#7 @ <head[:8]>`, each check's name with its `html_url` or `target_url`, the CI state and a `Merge readiness:` line; the `>> NEXT:` block contains `--provider github --repo o/r --pr 7` for `--once` with RUNNING, `TIMED_OUT` and `BASE_MOVED`, and for `CI_PASSED` says it is not a review and not a merge approval (GHCI-03, AC 10)
Proof: `python3 test_github_ci.py -k C13:`

**C14** - Credentials: `GH_TOKEN` is used over `GITHUB_TOKEN`, which is used over `gh auth token --hostname github.com`; none of the three exits 1; the token is sent only as `Authorization: Bearer` to `https://api.github.com`, never printed (including on error paths), never sent on a redirect to another host, and pagination never requests a URL taken from a `link` header (GHCI-03, AC 10)
Proof: `python3 test_github_ci.py -k C14:`

**C15** - Gitea behavior is unchanged: exit codes 0-10 keep their names and values, `CI_PASSED` is 11, and the existing suites pass without edits to their assertions (GHCI-03, AC 11)
Proof: `python3 test_github_ci.py -k C15:`
Proof: `python3 test_poll_review.py`
Proof: `python3 test_reply_finding.py`

**C16** - `SKILL.md`, `README.md`, `references/setup.md` and `references/verdicts.md` each name `CI_PASSED` with exit 11 as not a review, and `SKILL.md` and `references/verdicts.md` keep the "Immediately before merging" refresh rule (GHCI-03, AC 11)
Proof: `python3 test_github_ci.py -k C16:`

## Coverage

| Set (size) | Member -> proof | Unproven |
| --- | --- | --- |
| `poll_review.py --provider auto/gitea/github` statuses (8) | `ci_passed` C8 · `ci_failed` C8 · `pending` C10 · `timed_out` C10 · `unreachable` C9 · `base_moved` C12 · `not_mergeable` C12 · `argument_error` C4 | - |
| Existing Gitea CLI statuses (11) | C15, table-driven over all 11 names and codes, plus the existing suite | - |
| provider values (3) | `auto` C2 · `gitea` C2 · `github` C1 | - |
| auto selection rules (5) | tracking C2 · origin C2 · single provider C2 · ambiguous C2 · legacy outside checkout C2 | - |
| inference outcomes (4) | fork match C3 · foreign same-name ignored C3 · ambiguous C3 · none C3 | - |
| rejected GitHub options (6) | `--wait-for review` C4 · `--wait-for both` C4 · `--no-fail-fast` C4 · `--full` C4 · `CI_WORKFLOW_FILE` C4 · `CI_JOB_NAME` C4 | - |
| check-run status (6) | C5, table-driven over all 6 | - |
| check-run conclusion, with null and unrecognized (10) | C5, table-driven over all 10 | - |
| commit status state, with unrecognized (5) | C5, table-driven over all 5 | - |
| CI read failures (5) | authentication C9 · pagination C9 · transport C9 · rate limit C9 · parsing C9 | - |
| credential sources (4) | `GH_TOKEN` C14 · `GITHUB_TOKEN` C14 · `gh auth token` C14 · none C14 | - |
| Landing doors (3) | provider selector C2 · CI-only success contract C8 · credential boundary C14 | - |

- Claims naming an exit code or printed label: C4, C8, C9, C10, C11, C12, C13 - each proof runs
  `main()` end to end against a fake GitHub API and clock
- No other check claims more than the single case its proof exercises

## Swept

- validation: C4
- failure modes: C9
- idempotency: n/a - read-only monitor; it issues only GET requests and stores nothing
- authorization: C14
- concurrency: C11, C12
- data lifecycle: n/a - no stored data, no state migration
- dependency failure: C9
- state transitions: C10, C11
- observability: C13

## Handoff

Intended split, with the arithmetic, written before any code:

- S1 touches `poll_review.py`, a new GitHub module, two test files and five docs: 189 KB / 4
  = ~47k, under the 150k default budget -> one builder, no handoff

- **Boundary:** C1-C15 closed at `9bcb815`; C16 closes with the docs commit that follows it
- **Settled mid-build:** C11's second fixture let the old head's `FAILED` end the wait before
  the head could move, which is correct behavior but not "mid-wait". The fixture now holds that
  failure with `mergeable: false` inside the 90-second grace and then moves the head; the claim
  and its assertions are unchanged. A CI answer held by `mergeable: false` inside the grace is the
  implementation's reading of "readiness overrides CI" (AC 9).
- **Abandoned:** checking `total_count` against the collected check runs - its meaning under
  `filter=latest` is not documented, and a mismatch would have made every read UNKNOWN; the
  `link` header alone decides paging
