# GitHub CI-only babysitting - verification

**Verdict**: PASS
**Profile**: light
**Diff range**: d2ee17e..7c5c27a
**Round**: 1 - full
**Verifier**: independent sub-agent (author != verifier)

PASS means every check in `checks.md` is proven as written. It does not mean the feature is safe
to ship as planned: finding F1 is a demonstrated false-`CI_PASSED` that the approved plan and
C6 both specify. Decide F1 before relying on exit 11.

## Findings (ranked)

1. **F1 - a failing check is hidden by a same-named check from another workflow** - C6, AC 5 -
   `scripts/github_ci.py:256-262` (`_run_identity`), `:238-253` (`_newest`). The identity is
   `(app id, name)`. Two GitHub Actions workflows on one sha that each have a job called
   `build` share `(15368, "build")`, so the higher-id run wins whatever its result.
   Reproduction, from `scripts/`:
   ```
   python3 -c "import github_ci as g; cr=lambda c,i,s:{'id':i,'name':'build','status':'completed','conclusion':c,'app':{'id':15368,'slug':'github-actions','name':'GitHub Actions'},'check_suite':{'id':s}}; ci=g.classify([cr('success',2,200),cr('failure',1,100)],[]); print(ci.state, ci.detail)"
   ```
   prints `PASSED 1 result(s), all successful`; the workflow-A failure is gone from the
   report too. Real GitHub returns both runs (distinct `check_suite.id`) under `filter=latest`.
   The code follows the approved plan ("application and name", AC 5) and C6 tests only
   the different-app case, so this is a plan and check gap, not a builder deviation. Fix
   direction (needs a plan amendment): key a rerun on `(app, name, check_suite.id)` or
   workflow, or fail closed (UNKNOWN) when one `(app, name)` has more than one check suite.
2. **F2 - existing Gitea suite is not hermetic (low)** - C15 - `scripts/poll_review.py:1396-1404`.
   `main()` now runs real `git remote -v` for `--provider auto`, and `test_poll_review.py`
   does not patch it. In a checkout whose remote is github.com/o/r, `python3 test_poll_review.py`
   routes `--repo o/r` tests to the GitHub path: it made real requests to api.github.com
   (a 404 surfaced) and failed the review-at-head tests. Repro: clone the repo, `git remote set-url
   origin https://github.com/o/r.git`, run `python3 test_poll_review.py`. With the repo's own
   remotes (`git.marim.dev`, `github.com/mateuscmarim/babysit-pr`) it passes, and with a
   non-matching github remote (`me/app`) it passes. Assertions are untouched; the tests just
   depend on the checkout.
3. **F3 - redirect guard checks host only (informational)** - C14 - `scripts/github_ci.py:91-96`.
   A redirect to `http://api.github.com/...` is followed and would carry the bearer token in
   cleartext. GitHub does not do this; adding a scheme check is cheap.

No other bug found: `link`-header URLs are never fetched (`github_ci.py:172-191`), only GET is
issued, `gh auth token` is the last resort, and the token never appears in any output path
inspected (`describe()` prints status/reason only).

## Checks

All proofs run at HEAD `7c5c27a` from `scripts/`, each `-k "C<n>:"` invocation exit 0.
Each named section ran (its `Cn:` header and `ok` lines are in the output); none was a
filter matching nothing. Every section is new in `test_github_ci.py`, added by this feature.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | GitHub path is GET-only, api.github.com-only, no Gitea calls or review paths | `python3 test_github_ci.py -k C1:` exit 0 | `test_github_ci.py:240` - `all(r.get_method() == "GET" for r in sent)`; `:241` - `r.full_url.startswith("https://api.github.com/repos/o/r/")`; `:244` - no `/reviews`/`/comments` | PASS |
| C2 | auto selection: tracking, origin, single, ambiguous, none, legacy | `python3 test_github_ci.py -k C2:` exit 0 | `:277` - `choose([...], tracking="gh")` == `"github"`; `:287` - `amb.startswith("exit 1") and "ambiguous" in amb`; `:290` none exits 1; `:320` `reached == ["gitea"]` | PASS |
| C3 | fork PR match, foreign ignored, ambiguous, none, detached | `python3 test_github_ci.py -k C3:` exit 0 | `:368` - `== ("up/app", 12)`; `:372` foreign ignored exit 1; `:381` two matches name `--repo`/`--pr`; `:392` detached exits 1 with no calls | PASS |
| C4 | NOT_MONITORED; six options rejected before token or API; ci/any accepted | `python3 test_github_ci.py -k C4:` exit 0 | `:399` - `"review: NOT_MONITORED" in out`; `:421` - `got.startswith("exit 1") and name in got`; `:422` - `not token_reads and not fake.calls`; `:426` accepted -> 11 | PASS |
| C5 | status/conclusion classification, all members | `python3 test_github_ci.py -k C5:` exit 0 | `:453` - `check_run_state(run)[0] == want` over 18 table rows; `:456` - `status_state(...)[0] == want` over 5; `:457` non-object; `:460` NONE | PASS |
| C6 | newest per (app,name); different app kept; status newest both ways | `python3 test_github_ci.py -k C6:` exit 0 | `:471` - rerun higher id -> `"PASSED"`; `:477` - different app -> `"FAILED"`; `:480`/`:482` status both directions. Passes as written; see F1 for the gap in what it claims | PASS |
| C7 | exact-sha, per_page=100, paging only on rel="next", page-2 failure exits 8 | `python3 test_github_ci.py -k C7:` exit 0 | `:493` - `code == 8`; `:495` - `f"/commits/{HEAD}/" in c`; `:496` `per_page=100`; `:497` `== 2` pages; `:505` no link -> one page | PASS |
| C8 | FAILED -> 8; all successful -> 11; no REVIEWED, never 0 | `python3 test_github_ci.py -k C8:` exit 0 | `:511` - `code == 8`; `:517` - `code == 11`; `:519` - `"REVIEWED" not in out`; `:525` - `0 not in codes` | PASS |
| C9 | unreadable CI never passes; 401 no sleep; 403/429 retried; deadline -> 1; --once; non-object; PR unreachable | `python3 test_github_ci.py -k C9:` exit 0 | `:543` - page-2 timeout `state == "UNKNOWN"`; `:548`/`:549` 401 -> 1, `sleeps == []`; `:558` 403/429 -> 11; `:563` -> 1; `:569` --once; `:575` non-object UNKNOWN; `:579` PR unreachable -> 1 | PASS |
| C10 | wait, timeout, --once, closed | `python3 test_github_ci.py -k C10:` exit 0 | `:590`/`:591` - 11 after `len(sleeps) == 2`; `:594` -> 2; `:599` RUNNING -> 2; `:603` - `(code, sleeps) == (7, [])`; `:608` closed `(7, [])` | PASS |
| C11 | head move discards old CI, reads new sha, restarts deadline | `python3 test_github_ci.py -k C11:` exit 0 | `:616`/`:617` new head -> 11, CI read at `NEW_HEAD`; `:626`-`:627` old-head failure seen, run reports 11; `:634`/`:635` -> 2 with `sum(sleeps) > 300` | PASS |
| C12 | BASE_MOVED, 90s NOT_MERGEABLE, --once, false->true, null, unreadable target | `python3 test_github_ci.py -k C12:` exit 0 | `:643` -> 9 (passed and failed); `:647`/`:648` -> 10, `sum(sleeps) >= 90`; `:650` overrides CI_PASSED; `:653`; `:656` -> 11; `:659` `"Merge readiness: UNKNOWN"`; `:664`/`:665` `target tip UNKNOWN`, `Do not merge` | PASS |
| C13 | report lines and pinned NEXT | `python3 test_github_ci.py -k C13:` exit 0 | `:673` - `f"o/r#7 @ {HEAD[:8]}" in out`; `:675` names and links; `:678` `Merge readiness: YES`; `:680`/`:681` `NOT a review`, `NOT a merge approval`; `:690` `pin in NEXT` for 3 cases | PASS |
| C14 | credential order, Bearer only to api.github.com, never printed, redirect refusal, no link URL | `python3 test_github_ci.py -k C14:` exit 0 | `:704`/`:706`/`:708` order; `:715` none -> exit 1; `:728`-`:730` URL/Bearer/version; `:738` off-host redirect refused; `:750` - `TOKEN not in out`; `:751` no `evil` request | PASS |
| C15 | Gitea codes unchanged, CI_PASSED 11, existing suites green unedited | `python3 test_github_ci.py -k C15:`; `python3 test_poll_review.py`; `python3 test_reply_finding.py` - all exit 0 | `test_github_ci.py:759` - `poll_review.EXIT == want` (0-10 plus 11); `:760` `EXIT_UNREACHABLE == 1`; `git diff d2ee17e..HEAD --stat -- scripts/test_poll_review.py scripts/test_reply_finding.py` is empty; `test_poll_review.py:462` exit-code uniqueness. The two suites print `all passed` (379 and 17 `ok` lines). Caveat: F2 | PASS |
| C16 | four docs name CI_PASSED/11 as not a review; refresh rule kept | `python3 test_github_ci.py -k C16:` exit 0 | `:768` - line with `CI_PASSED` and `11`; `:769` - `"not a review" in text.lower()`; `:771` - `"Immediately before merging"` in `SKILL.md:72` and `references/verdicts.md:67` | PASS |

C11's fixture change (`:622-628`: old-head failure held by `mergeable: false` inside the grace, then
head moves) is the builder's disclosed edit. The claim is unchanged and still asserted. It does
show the implementation holds a CI answer under a `mergeable: false` grace (`github_ci.py:602-604`),
which is the plan's "readiness overrides CI".

## Coverage

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| check-run status (6) | `checks.md` API contract (GitHub docs) | queued, in_progress, waiting, requested, pending C5 `:432-436`; completed C5 `:437-447` | - |
| check-run conclusion + null + unrecognized (10) | `checks.md` API contract | success/neutral/skipped/failure/cancelled/timed_out/action_required/stale/startup_failure/null C5 `:437-446`, unknown value `:447` | - |
| commit status state (5) | `checks.md` API contract | pending/success/failure/error/bogus C5 `:454-455` | - |
| Exit codes 0-11 (12 incl. 1) | `poll_review.py:601-606` | C15 `:756-760` | - |
| Rejected GitHub options (6) | `github_ci.py:reject_review_options` | C4 `:400-407`, all 6 | - |
| provider values (3) | `poll_review.py:PROVIDERS` | auto C2 `:330`; gitea C2 `:320`; github C1 `:237` | - |
| auto rules (5) | `choose_provider` | C2 `:277`, `:280`, `:284`, `:287`, `:291` | - |
| credential sources (4) | `github_token` | C14 `:704`-`:715` | - |
| CI read failures (5) | plan AC 7 | authn `:548`, pagination `:543`, transport `:561`, rate limit `:556`, parsing `:572` | - |

The `Coverage` section is recomputed from the contract and code although `light` does not owe it.
Gap outside the checks: none of the above covers workflow identity (F1).

## Gate

`python3 test_github_ci.py` - all passed; `python3 test_poll_review.py` - all passed;
`python3 test_reply_finding.py` - all passed

---

# GitHub CI-only babysitting - verification, round 2

**Verdict**: PASS
**Profile**: light
**Diff range**: d2ee17e..5e05d13
**Round**: 2 - full
**Verifier**: independent sub-agent (author != verifier)

Round 1 above is preserved as history. This round re-runs every proof at `5e05d13`, re-verifies all
16 checks, and adversarially re-tests the fixes for F1, F2 and F3. The working tree was clean apart
from the untracked `.marim/` and `verification.md`, before and after; no implementation, check or plan
file was changed by this round.

## Findings (ranked)

None open. Round 1 findings, each re-tested:

1. **F1 (hidden failing check) - fixed, C6.** `scripts/github_ci.py:263-278` keys a run by
   `(app id or slug, name, check_suite.id)`; a missing, non-integer or boolean suite id returns `object()`,
   a unique identity, so it is never merged (and only ever adds a result). Round 1's reproduction now
   prints `FAILED GitHub Actions/build` in both listing orders. Variants I ran, all `FAILED`: suite ids
   `True`, `"100"`, `None` and `1.0` on both runs. Mutation: dropping `suite_id` from the returned tuple in a
   scratch worktree makes `-k C6:` exit 1 (20 FAIL lines), so the new assertions bind.
2. **F2 (Gitea suite depended on the checkout) - fixed, C15.** `test_poll_review.py:1038` patches
   `_git=lambda *a: None` in `run_main`; the only change to that file is that line plus a comment (two
   lines in the diff), no assertion edited. In a scratch worktree with `origin` set to
   `https://github.com/o/r.git`: the fixed `test_poll_review.py` exits 0, the round-1 version of the file
   (`git show 7c5c27a:`) exits 1, and the full `test_github_ci.py` and `test_reply_finding.py` exit 0 there.
3. **F3 (scheme-blind redirect guard) - fixed, C14.** `github_ci.py:92-103` refuses unless scheme is
   `https`, host equals `api.github.com` and port is absent or 443, and treats an unparseable port as
   refused. I exercised 12 redirect targets directly: `http://`, `:8443`, `:bad`, `api.github.com@evil.com`,
   `evil.com`, `api.github.com.evil.com`, `//api.github.com`, and `ftp://` are refused; `https://api.github.com`,
   `:443`, upper-case host and `x@api.github.com` (userinfo on the right host) are followed. Mutations in
   a scratch worktree: removing the scheme test makes `-k C14:` exit 1 with `api() raises on a redirect
   to http://` and `sends nothing over http://` failing (the real-opener test, `:781-807`, catches the
   request reaching the http handler); removing the port test makes it exit 1.

Observations, not findings: `test_github_ci.py:809` has a cosmetic typo (`same =handler`); it runs. The
C6 rerun rule assumes a workflow re-run stays in its check suite, which is what `checks.md` states;
I did not verify that against live GitHub.

## Checks

All proofs run at HEAD `5e05d13` from `scripts/`, each `-k "C<n>:"` exit 0 with the section's `ok` lines
present (C1 6, C2 16, C3 8, C4 15, C5 29, C6 18, C7 7, C8 8, C9 18, C10 11, C11 8, C12 13, C13 9,
C14 21, C15 4, C16 10), so none is a filter matching nothing. Sections are located by
`rg -n '@section'` at `test_github_ci.py:222-851`. Sections C1-C5 and C7-C13 and C16 are unchanged since
round 1, apart from the `cr()` helper gaining a default `suite=100` (`:102`), which does not touch their
assertions; line numbers have moved from round 1 and are refreshed here.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | GET-only, api.github.com-only, no Gitea or review paths | `python3 test_github_ci.py -k C1:` exit 0 | `test_github_ci.py:245` - `all(r.get_method() == "GET" for r in sent)`; `:246` - `https://api.github.com/repos/o/r/` prefix; `:249` no review/comment endpoint; `:244` exit 11 | PASS |
| C2 | auto selection: tracking, origin, single, ambiguous, none, legacy | `-k C2:` exit 0 | `:281` tracking wins; `:285` origin; `:289`/`:290` single provider; `:292` `exit 1 ambiguous`; `:295` none exits 1; `:296` `choose(None, repo="me/app") == "gitea"`; `:325` `reached == ["gitea"]` | PASS |
| C3 | fork match, foreign ignored, ambiguous, none, detached | `-k C3:` exit 0 | `:373` - `got == ("up/app", 12)`; `:377` foreign ignored exit 1; `:386` two matches name `--repo`/`--pr`; `:394` none; `:397` detached exit 1 without asking GitHub | PASS |
| C4 | NOT_MONITORED; six options rejected before token/API; ci/any accepted | `-k C4:` exit 0 | `:404` - `"review: NOT_MONITORED" in out`; `:426` - `got.startswith("exit 1") and name in got`; `:427` - `not token_reads and not fake.calls`; `:431` accepted -> 11 | PASS |
| C5 | classification of every status, conclusion and state | `-k C5:` exit 0 | `:458` - `check_run_state(run)[0] == want` table; `:461` - `status_state(...)[0] == want`; `:462` non-object UNKNOWN; `:465` NONE; `:466`-`:469` worst-of | PASS |
| C6 | identity `(app, name, suite)`; other app kept; other suite kept in both orders; no-suite never merged; status newest both ways | `-k C6:` exit 0 | `:475` rerun same suite -> PASSED; `:481` other app -> FAILED; `:490` - `ci.state == "FAILED"` for both listing orders; `:491` both runs listed (run 1 FAILED, run 2 PASSED, with links); `:495` - `main()` exit `8`; `:502` no-suite failure kept against newer success with and without suite, both orders; `:504` success without suite does not hide failure with one; `:507` non-integer suite id counts as none; `:510`/`:512` status both directions | PASS |
| C7 | exact-sha, per_page=100, paging only on rel="next", page-2 failure exits 8 | `-k C7:` exit 0 | `:524` - `code == 8`; `:526` - `f"/commits/{HEAD}/" in c`; `:527` `per_page=100`; `:528`/`:532` 2 pages each; `:536` no link -> 1 page | PASS |
| C8 | FAILED -> 8; all successful -> 11; no REVIEWED; never 0 | `-k C8:` exit 0 | `:542` exit 8; `:548` exit 11; `:550` - `"REVIEWED" not in out`; `:556` - `0 not in codes` | PASS |
| C9 | unreadable CI never passes; 401; 403/429; deadline; --once; non-object; PR unreachable | `-k C9:` exit 0 | `:574` page-2 failure `UNKNOWN`; `:579`/`:580` 401 -> 1, `sleeps == []`; `:589` 403/429 retried -> 11; `:594` deadline -> 1; `:600`/`:601` --once -> 1, no sleeps; `:606` non-object UNKNOWN; `:610` PR unreachable -> 1 | PASS |
| C10 | waiting, timeout, --once, closed | `-k C10:` exit 0 | `:621`/`:622` 11 after 2 sleeps; `:625` -> 2; `:630` -> 2; `:634` - `(code, sleeps) == (7, [])`; `:639` closed `(7, [])` | PASS |
| C11 | head change discards old CI, new sha, deadline restarts | `-k C11:` exit 0 | `:647`/`:648` new head -> 11, CI read at `NEW_HEAD`; `:657`/`:658` old failure seen, run reports 11; `:665`/`:666` -> 2 with `sum(sleeps) > 300` | PASS |
| C12 | BASE_MOVED, 90 s NOT_MERGEABLE, --once, false->true, null, unreadable | `-k C12:` exit 0 | `:674` -> 9; `:678`/`:679` -> 10 after >= 90 s; `:681` overrides CI_PASSED; `:684` --once; `:687` -> 11; `:690` `Merge readiness: UNKNOWN`; `:695`/`:696` `target tip UNKNOWN`, `Do not merge` | PASS |
| C13 | report lines and pinned NEXT | `-k C13:` exit 0 | `:704` - `f"o/r#7 @ {HEAD[:8]}" in out`; `:705` names with links; `:709` `Merge readiness: YES`; `:711`/`:712` `NOT a review`, `NOT a merge approval`; `:721` `pin in ...` for 3 cases | PASS |
| C14 | credential order; Bearer only to `https://api.github.com`; never printed; redirect off-host, http, off-port refused, https followed; no link-header URL | `-k C14:` exit 0 | `:735`/`:737`/`:739` order; `:746` none -> exit 1; `:759`-`:761` URL, `Bearer`, version; `:769` off-host refused; `:777` http and `:8443` refused; `:806` - `got == "HTTPError 301"` through the real opener; `:807` - `followed == []`; `:810` https redirect allowed; `:820` - `TOKEN not in out`; `:821` no `evil` request | PASS |
| C15 | Gitea codes unchanged, CI_PASSED 11, suites green unedited, Gitea suite ignores a github checkout | `-k C15:`; `python3 test_poll_review.py`; `python3 test_reply_finding.py` - all exit 0 | `:829` - `poll_review.EXIT == want`; `:830` `EXIT_UNREACHABLE == 1`; `:847` - `run_main(...)` code `== 0` with `git remote` reporting `origin https://github.com/o/r.git`; `:848` `REVIEWED` and no `NOT_MONITORED`; `git diff d2ee17e..HEAD -- scripts/test_poll_review.py` adds only the `_git` patch and its comment, `test_reply_finding.py` unchanged | PASS |
| C16 | four docs name CI_PASSED/11 as not a review; refresh rule kept | `-k C16:` exit 0 | `:855` line names `CI_PASSED` and `11`; `:857` - `"not a review" in text.lower()`; `:859` - `"Immediately before merging"` in `SKILL.md` and `references/verdicts.md` | PASS |

## Coverage

`light` does not owe a recompute; recorded for the sets the fixes touched.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| Check-run identity cases (5): same suite rerun, other app, other suite, no suite, non-integer suite | `github_ci.py:263-278` and the AC 5 wording | C6 `:475`, `:481`, `:490`, `:502`/`:504`, `:507` | - |
| Redirect refusals (3): other host, http, other port | `github_ci.py:92-103` | C14 `:769`, `:777` (http and :8443), `:806` | - |

## Gate

At `5e05d13` from `scripts/`: `python3 test_github_ci.py` - all passed; `python3 test_poll_review.py` -
all passed; `python3 test_reply_finding.py` - all passed. Scratch worktrees were removed and the real
tree's `git status --porcelain` matched its baseline.

---

# GitHub CI-only babysitting - verification, round 3

**Verdict**: PASS
**Profile**: light
**Diff range**: d2ee17e..0dcd338
**Round**: 3 - full
**Verifier**: independent sub-agent (author != verifier)

Rounds 1 and 2 above are preserved as history. This round re-runs every proof at `0dcd338` (the
fix for Gitea review finding #10042, `_pause` overshooting the wait deadline) and re-verifies all 16
checks. The only changes since `5e05d13` are `scripts/github_ci.py:640-643` (`_pause`),
`scripts/test_github_ci.py:641-662` (23 added lines in C10) and the C10 text in `checks.md`. No
implementation, check or plan file was changed by this round, and no git remote was touched; the
mutation work ran on a copy under the scratchpad.

## Findings (ranked)

None open. Finding #10042 re-tested and confirmed fixed:

1. **`_pause` no longer outlasts the deadline - fixed, C10.** `github_ci.py:643` is now
   `max(0.0, min(max(interval, wait or 0.0), deadline - time.monotonic()))`. Called directly with the
   real clock (`deadline` given as now + remaining): ordinary, 300s left, interval 30 -> 30;
   10s left -> 10.0; expired -> 0.0; rate-limit wait 120, 300s left -> 120; 10s left -> 10.0; expired -> 0.0;
   wait 10 below interval 30 -> 30 (the interval still floors a short `retry-after`); interval 400 with
   60s left -> 60.0; `wait=0` behaves as no wait. Both callers (`github_ci.py:601` transient read,
   `:627` ordinary poll) pass the result to `time.sleep`; at an expired deadline the pause is 0 and the
   loop reports (`:590` checks `>= deadline` first; `:605` sets `past`), so a 0 pause does not spin.
2. **Mutations in a scratch copy (not the real tree)**, each applied to `_pause`'s return line, `-k C10:`:
   `return interval` -> 7 FAIL; `max(interval, wait or 0)` (no clamp) -> 6 FAIL; dropping the outer
   `max(0.0, ...)` -> 2 FAIL (expired-deadline pauses return -5.0); ignoring `wait` (`min(interval, left)`)
   -> 2 FAIL; clamp against a huge constant -> 6 FAIL; the old shape (clamp only the rate-limit branch) -> 2 FAIL.
   Every mutant is caught, so the new assertions bind each clause of the clamp.

Observation, not a finding: the expired-deadline and 10s-left rate-limit cases are proven only at
`_pause` level (`test_github_ci.py:656-661`); the end-to-end cases cover the 400s interval, the 70s
interval and the `retry-after: 1000` path. That matches what C10 claims.

## Checks

All proofs run at HEAD `0dcd338` from `scripts/`, each `-k "C<n>:"` exit 0 with the section's `ok`
lines present and no `FAIL` line (C1 6, C2 16, C3 8, C4 15, C5 29, C6 18, C7 7, C8 8, C9 18, C10 21,
C11 8, C12 13, C13 9, C14 21, C15 4, C16 10), so none is a filter matching nothing. Only C10 grew
(11 -> 21 `ok` lines); C1-C9 and C11-C16 are textually unchanged since round 2, and their round-2
evidence (assertion lines and claims) still holds; it is carried by reference to the round 2 table, with
these proofs re-run fresh.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | GET-only, api.github.com-only, no Gitea or review paths | `python3 test_github_ci.py -k C1:` exit 0 | `test_github_ci.py:245` - `all(r.get_method() == "GET" for r in sent)`; `:246` host prefix; `:249` no review endpoint | PASS |
| C2 | auto selection | `-k C2:` exit 0 | `:281` tracking wins; `:292` ambiguous exits 1; `:325` `reached == ["gitea"]` | PASS |
| C3 | fork match, foreign ignored, ambiguous, none, detached | `-k C3:` exit 0 | `:373` `got == ("up/app", 12)`; `:386` ambiguous; `:397` detached exit 1 | PASS |
| C4 | NOT_MONITORED; six options rejected before token/API | `-k C4:` exit 0 | `:404` `"review: NOT_MONITORED" in out`; `:427` `not token_reads and not fake.calls` | PASS |
| C5 | classification of every status, conclusion and state | `-k C5:` exit 0 | `:458` `check_run_state(run)[0] == want`; `:461` `status_state(...)[0] == want` | PASS |
| C6 | identity `(app, name, suite)` | `-k C6:` exit 0 | `:475` same-suite rerun PASSED; `:490` `ci.state == "FAILED"` both orders; `:502` no-suite kept | PASS |
| C7 | exact sha, per_page=100, paging on rel="next" | `-k C7:` exit 0 | `:524` `code == 8`; `:527` `per_page=100`; `:536` one page without link | PASS |
| C8 | FAILED -> 8; passed -> 11; never 0, no REVIEWED | `-k C8:` exit 0 | `:542` exit 8; `:548` exit 11; `:556` `0 not in codes` | PASS |
| C9 | unreadable CI never passes; 401; 403/429; deadline; --once | `-k C9:` exit 0 | `:574` UNKNOWN; `:579`/`:580` 401 -> 1, `sleeps == []`; `:594` deadline -> 1; `:600`/`:601` --once | PASS |
| C10 | waiting, timeout, --once, closed; no sleep passes the deadline (400s interval in 1 min -> `[60.0]`, 70s interval -> last sleep 20s, `retry-after: 1000` -> `[300.0]` exit 1, rate-limit pause with 10s left 10, expired pauses 0) | `-k C10:` exit 0 | `test_github_ci.py:641` exit 2 and `:642` `clock.sleeps == [60.0]`; `:645` exit 2 and `:646` `[70.0, 70.0, 70.0, 70.0, 20.0]`; `:652` exit 1 and `:653` `[300.0]`; `:658` `_pause(30, None, clock.t - 5) == 0.0`; `:659` `_pause(30, 120, clock.t - 5) == 0.0`; `:660` `== 120` inside budget; `:661` `_pause(30, 120, clock.t + 10) == 10`; earlier C10 assertions `:621`-`:639` (round 2) intact | PASS |
| C11 | head change discards old CI; deadline restarts | `-k C11:` exit 0 | `:647`/`:648` new head -> 11, read at `NEW_HEAD`; `:665`/`:666` -> 2 with `sum(sleeps) > 300` (unaffected by the clamp) | PASS |
| C12 | BASE_MOVED, 90 s NOT_MERGEABLE, --once, false->true, null, unreadable | `-k C12:` exit 0 | `:674` -> 9; `:678`/`:679` -> 10 after >= 90 s; `:695`/`:696` `target tip UNKNOWN`, `Do not merge` | PASS |
| C13 | report lines and pinned NEXT | `-k C13:` exit 0 | `:704` `o/r#7 @ ...`; `:711`/`:712` `NOT a review`, `NOT a merge approval`; `:721` pins | PASS |
| C14 | credential order; Bearer only to https api.github.com; redirects refused | `-k C14:` exit 0 | `:759`-`:761` URL, Bearer, version; `:777` http/:8443 refused; `:806` `got == "HTTPError 301"`; `:820` `TOKEN not in out` | PASS |
| C15 | Gitea codes unchanged, CI_PASSED 11, suites green, Gitea suite ignores a github checkout | `-k C15:`; `python3 test_poll_review.py`; `python3 test_reply_finding.py` - all exit 0 | `:829` `poll_review.EXIT == want`; `:847` `run_main(...)` code `== 0` with a github `origin`; `git diff d2ee17e..HEAD --stat` on the two suites shows only the round-2 `_git` patch in `test_poll_review.py` | PASS |
| C16 | docs name CI_PASSED/11 as not a review; refresh rule kept | `-k C16:` exit 0 | `:855` line names `CI_PASSED` and `11`; `:857` `"not a review" in text.lower()`; `:859` `"Immediately before merging"` | PASS |

## Coverage

`light` does not owe a recompute; recorded for the set this round touched.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| Pause inputs (4): ordinary with time left, ordinary at/over the deadline, rate-limit wait inside the budget, rate-limit wait past it | `github_ci.py:640-643` and its two callers `:601`, `:627` | C10 `:641`-`:646` (ordinary, end to end), `:652`-`:653` (rate-limit past deadline, end to end), `:658`-`:661` (expired both kinds, inside, 10s left) | - |

## Gate

At `0dcd338` from `scripts/`: `python3 test_github_ci.py` - all passed (211 `ok` lines);
`python3 test_poll_review.py` - all passed (379); `python3 test_reply_finding.py` - all passed (17).
The real tree's `git status --porcelain` matched its baseline (only `.marim/` untracked before this
report was appended), and `git remote -v` is unchanged.

---

# GitHub CI-only babysitting - verification, round 4

**Verdict**: PASS
**Profile**: light
**Diff range**: d2ee17e..445b010
**Round**: 4 - full
**Verifier**: independent sub-agent (author != verifier)

Rounds 1-3 above are preserved as history. This round re-runs every proof at `445b010` (the fix for
Gitea review finding #10052, a truncated body escaping as `http.client.IncompleteRead`) and
re-verifies all 16 checks. The only changes since `cfcf24c` are `scripts/github_ci.py` (`import
http.client`; `http.client.HTTPException` added to `is_transient`, line 171), 44 added lines in
`scripts/test_github_ci.py` (C9, `:618-659`) and the C9 text in `checks.md`. No implementation,
check, plan or remote was changed by this round; the mutation ran on a copy under the scratchpad.

## Findings (ranked)

None open. Finding #10052 re-tested and confirmed fixed:

1. **A truncated body is transient on every path - fixed, C9.** `is_transient` (`github_ci.py:164-171`)
   now includes `http.client.HTTPException`, which `IncompleteRead` subclasses. Every path routes
   through it:
   - CI read: `read_ci` (`:319-323`) returns `UNKNOWN, permanent=False` -> `:628` asserts `("UNKNOWN", False)`.
   - Startup/PR poll and statuses: `watch` (`:584-591`) retries while transient; asserted at `:632`/`:636` (later success exits 11) and `:640-641` (to the deadline exits 1, `=== UNREACHABLE`), `:645` (`--once` exits 1, zero sleeps).
   - Target tip: `:416-421` returns `None` for a transient error, so nothing blocks -> `:649` exits 11.
   - PR discovery: `main` (`:537-540`) catches `Exception` -> `could not find the PR ... no verdict reached`, exit 1 -> `:656`/`:657`, no traceback.
2. **Mutation in a scratch copy:** removing `http.client.HTTPException` from the `is_transient` tuple
   makes `-k C9:` fail (`is_transient` assertion FAIL, then the section crashes with a
   `Traceback` from the escaping exception), so the new assertions bind the fix.

Observation, not a finding: the "no traceback" assertion on the discovery path is checked via
exit code and the `no verdict reached` text, and the retry cases check `"Traceback" not in out`
(`:638`); the discovery case does not assert absence of a traceback textually, but an uncaught
exception would have exited non-1 through the harness.

## Checks

All proofs run at HEAD `445b010` from `scripts/`, each `-k "C<n>:"` exit 0 with the section's `ok`
lines present (C1 6, C2 16, C3 8, C4 15, C5 29, C6 18, C7 7, C8 8, C9 30, C10 21, C11 8, C12 13,
C13 9, C14 21, C15 4, C16 10), so none is a filter matching nothing. A `FAIL` grep matches only
check titles containing the word `FAILED`; no `FAIL` result line appears. Only C9 grew (18 -> 30);
the other fifteen are textually unchanged since round 3 and their round-3 evidence still holds,
carried by reference with these proofs re-run fresh.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | GET-only, api.github.com-only, no Gitea or review paths | `python3 test_github_ci.py -k C1:` exit 0 | `test_github_ci.py:245` - `all(r.get_method() == "GET" for r in sent)`; `:246` host prefix; `:249` no review endpoint | PASS |
| C2 | auto selection | `-k C2:` exit 0 | `:281` tracking wins; `:292` ambiguous exits 1; `:325` `reached == ["gitea"]` | PASS |
| C3 | fork match, foreign ignored, ambiguous, none, detached | `-k C3:` exit 0 | `:373` `got == ("up/app", 12)`; `:386` ambiguous; `:397` detached exit 1 | PASS |
| C4 | NOT_MONITORED; six options rejected before token/API | `-k C4:` exit 0 | `:404` `"review: NOT_MONITORED" in out`; `:427` `not token_reads and not fake.calls` | PASS |
| C5 | classification of every status, conclusion and state | `-k C5:` exit 0 | `:458` `check_run_state(run)[0] == want`; `:461` `status_state(...)[0] == want` | PASS |
| C6 | identity `(app, name, suite)` | `-k C6:` exit 0 | `:475` same-suite rerun PASSED; `:490` `ci.state == "FAILED"` both orders; `:502` no-suite kept | PASS |
| C7 | exact sha, per_page=100, paging on rel="next" | `-k C7:` exit 0 | `:524` `code == 8`; `:527` `per_page=100`; `:536` one page without link | PASS |
| C8 | FAILED -> 8; passed -> 11; never 0, no REVIEWED | `-k C8:` exit 0 | `:542` exit 8; `:548` exit 11; `:556` `0 not in codes` | PASS |
| C9 | unreadable CI never passes; 401; 403/429; deadline; --once; truncated body transient on CI, statuses, PR, deadline, --once, target tip, discovery | `-k C9:` exit 0 | `:574` UNKNOWN; `:579`/`:580` 401 -> 1, `sleeps == []`; `:623` `is_transient(cut())`; `:628` `(v.state, v.permanent) == ("UNKNOWN", False)`; `:632` statuses retried -> 11, `:633` 2 sleeps; `:636` PR retried -> 11; `:641` `"=== UNREACHABLE"`; `:645` `--once` `(1, [])`; `:649` target tip -> 11; `:656`/`:657` discovery exit 1, `no verdict reached` | PASS |
| C10 | waiting, timeout, --once, closed; no sleep passes the deadline | `-k C10:` exit 0 | `:641`/`:642` `clock.sleeps == [60.0]`; `:645`/`:646` `[70.0, 70.0, 70.0, 70.0, 20.0]`; `:652`/`:653` `[300.0]`; `:658`-`:661` `_pause` cases | PASS |
| C11 | head change discards old CI; deadline restarts | `-k C11:` exit 0 | `:647`/`:648` new head -> 11; `:665`/`:666` -> 2 with `sum(sleeps) > 300` | PASS |
| C12 | BASE_MOVED, 90 s NOT_MERGEABLE, --once, false->true, null, unreadable | `-k C12:` exit 0 | `:674` -> 9; `:678`/`:679` -> 10; `:695`/`:696` `target tip UNKNOWN`, `Do not merge` | PASS |
| C13 | report lines and pinned NEXT | `-k C13:` exit 0 | `:704` `o/r#7 @ ...`; `:711`/`:712` `NOT a review`, `NOT a merge approval`; `:721` pins | PASS |
| C14 | credential order; Bearer only to https api.github.com; redirects refused | `-k C14:` exit 0 | `:759`-`:761` URL, Bearer, version; `:777` http/:8443 refused; `:806` `got == "HTTPError 301"`; `:820` `TOKEN not in out` | PASS |
| C15 | Gitea codes unchanged, CI_PASSED 11, suites green | `-k C15:`; `python3 test_poll_review.py`; `python3 test_reply_finding.py` - all exit 0 | `:829` `poll_review.EXIT == want`; `:847` `run_main(...)` code `== 0` with a github `origin`; the two Gitea suites are untouched by `445b010` | PASS |
| C16 | docs name CI_PASSED/11 as not a review; refresh rule kept | `-k C16:` exit 0 | `:855` names `CI_PASSED` and `11`; `:857` `"not a review" in text.lower()`; `:859` `"Immediately before merging"` | PASS |

Line numbers for C10-C16 are the round-3 citations; the 44 lines added at `:618-659` shift later
sections, so confirm them by the quoted expression rather than the number.

## Coverage

`light` does not owe a recompute; recorded for the set this round touched.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| Truncated-body read sites (5): CI check-runs/statuses, PR fetch, target tip, PR discovery, `--once` | `github_ci.py:319`, `:416`, `:537`, `:584-591` | C9 `:628`, `:632`/`:636`, `:640`-`:645`, `:649`, `:656`/`:657` | - |

## Gate

At `445b010` from `scripts/`: `python3 test_github_ci.py` - all passed (223 `ok` lines);
`python3 test_poll_review.py` - all passed (380); `python3 test_reply_finding.py` - all passed (17).
The real tree's `git status --porcelain` shows only `.marim/` untracked (before this report was
appended), and `git remote -v` is unchanged (`origin`, `github`). Nothing was pushed.

---

# GitHub CI-only babysitting - verification, round 5

**Verdict**: PASS
**Profile**: light
**Diff range**: d2ee17e..54a1853
**Round**: 5 - full
**Verifier**: independent sub-agent (author != verifier)

Rounds 1-4 above are preserved as history. This round re-runs every proof at `54a1853` (the fix for
Gitea review finding #10058: an explicit `--repo` was also admitted as a head source) and re-verifies
all 16 checks. Since `aa3d770` only `scripts/github_ci.py` (one call, `:526-528`) and 48 added lines in
`scripts/test_github_ci.py` (C3, `:401-448`) changed. No implementation, check, plan or remote was
changed by this round; the mutation and the independent scenarios ran on a copy under the scratchpad.

## Findings (ranked)

None open. Finding #10058 re-tested and confirmed fixed:

1. **Explicit `--repo` cannot enter the head allowlist - fixed, C3.** `resolve` (`github_ci.py:509-528`)
   now ends `return infer_pr(repos, ours, branch, tok)`; `ours` is built only from `remote_repos(git remote -v)`
   (`:515-517`), `repos` is `[args.repo]` (`:519`) and feeds only the search list in `infer_pr` (`:384`).
   `infer_pr` accepts a PR only when `head.repo.full_name in ours` (`:391`) and derives owners from `ours` (`:382`).
2. **Independent scenarios** (own fake API over `github_ci.resolve`, sole checkout remote `fork -> me/r`,
   branch `feat`, upstream `o/r`):
   - explicit `--repo o/r`, only upstream's own `feat` PR 5 open -> `exit`, `no open PR found ... pass --repo owner/name --pr N`;
     the only request was `head=me%3Afeat` (never `o%3Afeat`).
   - explicit `--repo o/r`, PR 5 (head `o/r`) and PR 7 (head `me/r`) -> `('o/r', 7)`, no ambiguity.
   - explicit `--repo o/r --pr 9`, no checkout (`git remote -v` -> None) -> `('o/r', 9)`, zero API calls.
   - explicit `--repo o/r`, no `--pr`, no checkout -> exit 1 asking for `--pr`; zero API calls.
3. **Mutation in a scratch copy:** restoring the old call `infer_pr(repos, list(dict.fromkeys([*repos, *ours])), ...)`
   makes `-k C3:` fail 4 assertions (both PR-5-only cases, the unambiguous fork case with
   `several open PRs (o/r#5, o/r#7)`, and the "watches PR 7, never PR 5" case), so the new tests bind the fix.

## Checks

All proofs run at HEAD `54a1853` from `scripts/`, each `-k "C<n>:"` exit 0 with the section's `ok`
lines present and no `FAIL` line (C1 6, C2 16, C3 12, C4 15, C5 29, C6 18, C7 7, C8 8, C9 30, C10 21,
C11 8, C12 13, C13 9, C14 21, C15 4, C16 10), so none is a filter matching nothing. Only C3 grew (8 -> 12); the
other fifteen are textually unchanged since round 4 and their round-4 evidence still holds, carried by
reference with these proofs re-run fresh.

| Check | Claim | Proof run | Evidence | Result |
| --- | --- | --- | --- | --- |
| C1 | GET-only, api.github.com-only, no Gitea or review paths | `python3 test_github_ci.py -k C1:` exit 0 | `test_github_ci.py:246` - `all(r.get_method() == "GET" for r in sent)` | PASS |
| C2 | auto selection | `-k C2:` exit 0 | `:281` tracking wins; `:292` ambiguous exits 1; `:325` `reached == ["gitea"]` | PASS |
| C3 | fork match, foreign ignored, ambiguous, none, detached, explicit --repo not a head source | `-k C3:` exit 0 | `:373` `got == ("up/app", 12)`; `:386` ambiguous; `:397` detached exit 1; `:438-440` `got.startswith("exit 1") and "--pr" in got and "/repos/o/r/pulls/5" not in calls`; `:442-443` every `/pulls?` call has `head=me%3Afeat`; `:445` `check(..., got, 11)`; `:446-447` PR 7 watched, never PR 5 | PASS |
| C4 | NOT_MONITORED; six options rejected before token/API | `-k C4:` exit 0 | `"review: NOT_MONITORED" in out`; `not token_reads and not fake.calls` | PASS |
| C5 | classification of every status, conclusion and state | `-k C5:` exit 0 | `check_run_state(run)[0] == want`; `status_state(...)[0] == want` | PASS |
| C6 | identity `(app, name, suite)` | `-k C6:` exit 0 | `ci.state == "FAILED"` in both orders; no-suite run kept | PASS |
| C7 | exact sha, per_page=100, paging on rel="next" | `-k C7:` exit 0 | `code == 8` for a page-2 failure; `per_page=100`; one page without link | PASS |
| C8 | FAILED -> 8; passed -> 11; never 0, no REVIEWED | `-k C8:` exit 0 | exit 8; exit 11; `0 not in codes` | PASS |
| C9 | unreadable CI never passes; 401; 403/429; deadline; --once; truncated body | `-k C9:` exit 0 | `(v.state, v.permanent) == ("UNKNOWN", False)`; 401 -> 1 with `sleeps == []`; `"=== UNREACHABLE"` | PASS |
| C10 | waiting, timeout, --once, closed; no sleep passes the deadline | `-k C10:` exit 0 | `clock.sleeps == [60.0]`; `[70.0, 70.0, 70.0, 70.0, 20.0]`; `[300.0]` | PASS |
| C11 | head change discards old CI; deadline restarts | `-k C11:` exit 0 | new head -> 11; `sum(sleeps) > 300` -> 2 | PASS |
| C12 | BASE_MOVED, 90 s NOT_MERGEABLE, --once, false->true, null, unreadable | `-k C12:` exit 0 | -> 9; -> 10; `target tip UNKNOWN`, `Do not merge` | PASS |
| C13 | report lines and pinned NEXT | `-k C13:` exit 0 | `o/r#7 @ ...`; `NOT a review`, `NOT a merge approval` | PASS |
| C14 | credential order; Bearer only to https api.github.com; redirects refused | `-k C14:` exit 0 | Bearer and version headers; http/:8443 refused; `TOKEN not in out` | PASS |
| C15 | Gitea codes unchanged, CI_PASSED 11, suites green | `-k C15:`; `python3 test_poll_review.py`; `python3 test_reply_finding.py` - all exit 0 | `poll_review.EXIT == want`; `run_main(...)` code `== 0` with a github `origin`; both Gitea suites untouched by `54a1853` | PASS |
| C16 | docs name CI_PASSED/11 as not a review; refresh rule kept | `-k C16:` exit 0 | `"not a review" in text.lower()`; `"Immediately before merging"` | PASS |

Line numbers outside C3 are round-4 citations; the C3 additions at `:401-448` shift later sections,
so confirm them by the quoted expression rather than the number.

## Coverage

`light` does not owe a recompute; recorded for the set this round touched.

| Set (size) | Recomputed from | Member -> proof | Unproven |
| --- | --- | --- | --- |
| inference outcomes (4) plus explicit-repo handling | `github_ci.py:373-401`, `:509-528` | fork match C3 `:373` · foreign ignored C3 · ambiguous C3 `:386` · none C3 · explicit `--repo` not a head source C3 `:438`/`:445` | - |

## Gate

At `54a1853` from `scripts/`: `python3 test_github_ci.py` - all passed (227 `ok` lines);
`python3 test_poll_review.py` - all passed; `python3 test_reply_finding.py` - all passed.
The real tree's `git status --porcelain` shows only `.marim/` untracked (before this report was
appended), and `git remote -v` is unchanged (`origin`, `github`). Nothing was pushed.
