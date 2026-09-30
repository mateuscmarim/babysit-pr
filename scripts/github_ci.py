"""Watch a github.com pull request's CI. The GitHub half of poll_review.py.

CI only, on purpose: the review bot is not on GitHub yet, so there is no
review to wait for and the report says `review: NOT_MONITORED`. A CI that
passed exits 11 CI_PASSED, never 0 REVIEWED: exit 0 means a reviewed PR, and
nobody reviewed this one.

What counts as CI is every check run and every commit status GitHub reports
for the exact head SHA, newest per identity. No workflow or job filter: that
includes external CI, and it says nothing about which checks branch protection
requires. An empty set is NONE and never passes. A set that could not be read
in full is UNKNOWN and never passes either.

Read-only: every request is a GET to https://api.github.com, and the token goes
nowhere else -- not to a `link` header URL, not across a redirect.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import poll_review
from poll_review import EXIT, EXIT_UNREACHABLE, MERGE_CHECK_GRACE_S, SCRIPT

HOST = "github.com"
API_ROOT = "https://api.github.com"
# Pinned so a new API version cannot change a shape under us. Supported to
# March 10, 2028; 2026-03-10 is the newer one.
API_VERSION = "2022-11-28"
PER_PAGE = 100
# 5000 check runs or statuses. Past that the read is incomplete, which is
# UNKNOWN, not "the rest passed".
MAX_PAGES = 50
API_TIMEOUT_S = 30.0
ONCE_API_TIMEOUT_S = 10.0

# The review-side options have nothing to act on here. Refused rather than
# ignored, so nobody reads a CI result as the review they asked to wait for.
REJECTED_WAIT_FOR = ("review", "both")

RUNNING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
FAILED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required",
                      "stale", "startup_failure"}
STATUS_STATES = {"pending": "RUNNING", "success": "PASSED",
                 "failure": "FAILED", "error": "FAILED"}


# ---------------------------------------------------------------- credentials


def github_token() -> str:
    """GH_TOKEN, then GITHUB_TOKEN, then `gh auth token --hostname github.com`.

    Never a Gitea token: the two hosts' credentials do not mix."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if tok := os.environ.get(name, "").strip():
            return tok
    try:
        proc = subprocess.run(
            ["gh", "auth", "token", "--hostname", HOST],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0 and (tok := proc.stdout.strip()):
            return tok
    except (OSError, subprocess.SubprocessError):
        pass
    sys.exit(
        "no GitHub token: set GH_TOKEN or GITHUB_TOKEN, or run "
        f"`gh auth login --hostname {HOST}`"
    )


# ------------------------------------------------------------------ transport


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies the Authorization header onto a redirect, whatever the
    host or scheme. GitHub redirects a renamed repo within
    https://api.github.com; anything else -- another host, another port, or
    plain http, which would carry the token in cleartext -- is refused rather
    than handed the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        to, root = urllib.parse.urlsplit(newurl), urllib.parse.urlsplit(API_ROOT)
        try:
            port = to.port
        except ValueError:
            port = -1
        if to.scheme != "https" or to.hostname != root.hostname or port not in (None, 443):
            raise urllib.error.HTTPError(
                req.full_url, code, f"refused redirect off {API_ROOT}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(SameHostRedirect)


def _urlopen(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


class Unreadable(Exception):
    """A response this reader cannot use: wrong shape, or too many pages."""


def api(path: str, tok: str) -> tuple[object, dict[str, str]]:
    """One GET to api.github.com: (parsed body, lower-cased headers)."""
    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    with _urlopen(req, API_TIMEOUT_S) as resp:
        body = json.loads(resp.read())
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return body, headers


def _header(exc: urllib.error.HTTPError, name: str) -> str | None:
    headers = exc.headers
    if headers is None:
        return None
    if isinstance(headers, dict):
        return next((v for k, v in headers.items() if k.lower() == name), None)
    return headers.get(name)


def rate_limit_wait(exc: BaseException) -> float | None:
    """Seconds GitHub asks us to wait, if `exc` is a rate limit; else None.

    Both limits answer 403 or 429. A 403 is a rate limit only when it says so
    (`retry-after`, or `x-ratelimit-remaining: 0`); otherwise it is a real
    permission error and waiting will not fix it."""
    if not isinstance(exc, urllib.error.HTTPError) or exc.code not in (403, 429):
        return None
    if (after := _header(exc, "retry-after")) is not None:
        try:
            return max(0.0, float(after))
        except ValueError:
            return 60.0
    if _header(exc, "x-ratelimit-remaining") == "0":
        try:
            return max(0.0, float(_header(exc, "x-ratelimit-reset") or 0) - time.time())
        except ValueError:
            return 60.0
    return 60.0 if exc.code == 429 else None


def is_transient(exc: BaseException) -> bool:
    """A failure a later poll could clear: 5xx, rate limits, transport, and a
    response that did not parse. Other 4xx are answers (bad token, no access,
    no such PR) and end the run."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or rate_limit_wait(exc) is not None
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError,
                            http.client.HTTPException, json.JSONDecodeError, Unreadable))


def describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"GitHub API {exc.code} {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def paged(path: str, tok: str, key: str | None = None) -> list:
    """Every page of a list endpoint.

    The next page is requested by our own path and `page=`, only while the
    `link` header says `rel="next"`. The URL inside that header is never
    fetched: the token stays on paths this module built. Any page failing
    raises, so a partial list never reads as the whole."""
    sep = "&" if "?" in path else "?"
    out: list = []
    for page in range(1, MAX_PAGES + 1):
        body, headers = api(f"{path}{sep}per_page={PER_PAGE}&page={page}", tok)
        chunk = body.get(key) if key is not None and isinstance(body, dict) else body
        if not isinstance(chunk, list):
            raise Unreadable(f"{path.split('?')[0]}: page {page} is not a list")
        out.extend(chunk)
        if not re.search(r'rel="next"', headers.get("link", "")):
            return out
    raise Unreadable(f"{path.split('?')[0]}: more than {MAX_PAGES} pages")


# --------------------------------------------------------------------- CI


@dataclass
class Result:
    label: str
    state: str  # PASSED | FAILED | RUNNING | UNKNOWN
    detail: str
    link: str = ""


@dataclass
class CI:
    state: str  # PASSED | FAILED | RUNNING | UNKNOWN | NONE
    detail: str = ""
    results: list[Result] = field(default_factory=list)
    # UNKNOWN only: waiting cannot fix it (401, 403, 404).
    permanent: bool = False
    # UNKNOWN only: GitHub asked for this many seconds before a retry.
    retry_after: float = 0.0


def check_run_state(run: object) -> tuple[str, str]:
    if not isinstance(run, dict):
        return "UNKNOWN", "unreadable check run"
    status, conclusion = run.get("status"), run.get("conclusion")
    if status in RUNNING_STATUSES:
        return "RUNNING", f"status={status}"
    if status != "completed":
        return "UNKNOWN", f"unrecognized status {status!r}"
    if conclusion in SUCCESS_CONCLUSIONS:
        return "PASSED", str(conclusion)
    if conclusion in FAILED_CONCLUSIONS:
        return "FAILED", str(conclusion)
    return "UNKNOWN", f"completed with unrecognized conclusion {conclusion!r}"


def status_state(status: object) -> tuple[str, str]:
    if not isinstance(status, dict):
        return "UNKNOWN", "unreadable commit status"
    state = status.get("state")
    if state in STATUS_STATES:
        return STATUS_STATES[state], str(state)
    return "UNKNOWN", f"unrecognized state {state!r}"


def _newest(items: list, key) -> tuple[list, list]:
    """(newest item per identity, unreadable items). Newest is the highest
    id: a re-run gets a new one. Equal or missing ids keep the first listed,
    which is the newest in GitHub's own ordering."""
    best: dict = {}
    bad: list = []
    for item in items:
        ident = key(item) if isinstance(item, dict) else None
        if ident is None:
            bad.append(item)
            continue
        rid = item.get("id")
        prev = best.get(ident)
        if prev is None or (isinstance(rid, int) and isinstance(prev.get("id"), int) and rid > prev["id"]):
            best[ident] = item
    return list(best.values()), bad


def _run_identity(run: dict):
    """(application, name, check suite): one app's `test` is not another
    app's, and one workflow's `build` is not another workflow's -- each
    workflow run is its own check suite. A re-run replaces a run only inside
    its suite. A run with no suite id is its own identity, so it can add a
    result but never hide one."""
    app = run.get("app") if isinstance(run.get("app"), dict) else {}
    name = run.get("name")
    if not isinstance(name, str) or not name:
        return None
    suite = run.get("check_suite") if isinstance(run.get("check_suite"), dict) else {}
    suite_id = suite.get("id")
    if not isinstance(suite_id, int) or isinstance(suite_id, bool):
        return object()
    return app.get("id") or app.get("slug"), name, suite_id


def _status_identity(status: dict):
    context = status.get("context")
    return context if isinstance(context, str) and context else None


def classify(runs: list, statuses: list) -> CI:
    """Worst-of over the newest result per check (app, name and suite) and per status
    context: FAILED > RUNNING > UNKNOWN > PASSED. Nothing at all is NONE."""
    results: list[Result] = []
    newest_runs, bad_runs = _newest(runs, _run_identity)
    for run in newest_runs:
        state, detail = check_run_state(run)
        app = run.get("app") if isinstance(run.get("app"), dict) else {}
        who = app.get("name") or app.get("slug") or "check"
        results.append(Result(f"{who}/{run['name']}", state, detail,
                              run.get("html_url") or run.get("details_url") or ""))
    newest_statuses, bad_statuses = _newest(statuses, _status_identity)
    for status in newest_statuses:
        state, detail = status_state(status)
        results.append(Result(f"status/{status['context']}", state, detail,
                              status.get("target_url") or ""))
    results += [Result("check run ?", "UNKNOWN", "unreadable check run") for _ in bad_runs]
    results += [Result("status ?", "UNKNOWN", "unreadable commit status") for _ in bad_statuses]
    if not results:
        return CI("NONE", "no check run or commit status reported for this head")
    for state in ("FAILED", "RUNNING", "UNKNOWN"):
        if hits := [r for r in results if r.state == state]:
            return CI(state, ", ".join(r.label for r in hits), results)
    return CI("PASSED", f"{len(results)} result(s), all successful", results)


def read_ci(repo: str, sha: str, tok: str) -> CI:
    """classify over every page of check runs and statuses at `sha`. A read
    that fails anywhere is UNKNOWN, never NONE and never PASSED."""
    at = f"/repos/{repo}/commits/{urllib.parse.quote(sha, safe='')}"
    try:
        runs = paged(f"{at}/check-runs", tok, key="check_runs")
        statuses = paged(f"{at}/statuses", tok)
    except Exception as exc:
        if not is_transient(exc) and not isinstance(exc, urllib.error.HTTPError):
            raise
        wait = rate_limit_wait(exc) or 0.0
        permanent = not is_transient(exc)
        detail = f"could not read CI ({describe(exc)})"
        detail += (
            " -- the token may lack access to checks or statuses on this repo. "
            "This is NOT 'no CI': nobody looked."
            if permanent else "; retrying"
        )
        return CI("UNKNOWN", detail, permanent=permanent, retry_after=wait)
    return classify(runs, statuses)


def decide(ci: CI, *, once: bool, past_deadline: bool) -> str:
    """What one poll's CI verdict means. Pure, like poll_review.decide.

      ci_failed   a check failed (8)
      ci_passed   at least one result, all successful (11)
      unreadable  CI could not be read, and waiting will not help (1)
      pending     --once or a closed PR, CI still open (7)
      timed_out   deadline, CI running or absent (2)
      wait        poll again
    """
    if ci.state == "FAILED":
        return "ci_failed"
    if ci.state == "PASSED":
        return "ci_passed"
    if ci.state == "UNKNOWN" and (ci.permanent or once or past_deadline):
        return "unreadable"
    if once:
        return "pending"
    if past_deadline:
        return "timed_out"
    return "wait"


CODES = {
    "ci_passed": EXIT["CI_PASSED"], "ci_failed": EXIT["CI_FAILED"],
    "pending": EXIT["PENDING"], "timed_out": EXIT["TIMED_OUT"],
    "unreadable": EXIT_UNREACHABLE, "base_moved": EXIT["BASE_MOVED"],
    "not_mergeable": EXIT["NOT_MERGEABLE"],
}
LABELS = {
    "ci_passed": "CI_PASSED", "ci_failed": "CI_FAILED", "pending": "PENDING",
    "timed_out": "TIMED_OUT", "unreadable": "UNREACHABLE",
    "base_moved": "BASE_MOVED", "not_mergeable": "NOT_MERGEABLE",
}


# ----------------------------------------------------------- repo and target


def infer_pr(repos: list[str], ours: list[str], branch: str | None, tok: str) -> tuple[str, int]:
    """The one open PR for `branch` whose head repo is one of `ours`.

    A fork's PR lives in the upstream, so every candidate repo is asked, once
    per owner among `ours`. GitHub's `head=` filter narrows the list, and the
    head repo is checked here too. More than one match is refused: picking
    one would babysit a PR nobody named."""
    if not branch or branch == "HEAD":
        sys.exit("no branch checked out (detached HEAD): pass --repo owner/name --pr N")
    owners = list(dict.fromkeys(r.split("/", 1)[0] for r in ours))
    found: dict[tuple[str, int], None] = {}
    for repo in repos:
        for owner in owners:
            head = urllib.parse.quote(f"{owner}:{branch}", safe="")
            for pr in paged(f"/repos/{repo}/pulls?state=open&head={head}", tok):
                if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                    continue
                h = pr.get("head") or {}
                if h.get("ref") == branch and (h.get("repo") or {}).get("full_name") in ours:
                    found[(repo, pr["number"])] = None
    if len(found) == 1:
        return next(iter(found))
    if found:
        listed = ", ".join(f"{r}#{n}" for r, n in found)
        sys.exit(f"branch {branch!r} has several open PRs ({listed}): pass --repo owner/name --pr N")
    sys.exit(
        f"no open PR found for branch {branch!r} in {', '.join(repos)}: "
        "pass --repo owner/name --pr N"
    )


def read_target(pr: dict, repo: str, tok: str) -> tuple[str, str, str] | None:
    """The live target tip, or None when it cannot be read (or the PR is
    closed). None blocks merge readiness, never the CI verdict."""
    if pr.get("state") == "closed":
        return None
    base = pr.get("base") or {}
    name = (base.get("repo") or {}).get("full_name") or repo
    ref = base.get("ref")
    if not isinstance(ref, str) or not ref:
        return None
    try:
        body, _ = api(f"/repos/{name}/git/ref/heads/{urllib.parse.quote(ref, safe='/')}", tok)
    except urllib.error.HTTPError:
        return None
    except Exception as exc:
        if is_transient(exc):
            return None
        raise
    sha = (body.get("object") or {}).get("sha") if isinstance(body, dict) else None
    return (name, ref, sha) if isinstance(sha, str) and sha else None


# ---------------------------------------------------------------------- report


def render_ci(ci: CI) -> None:
    print(f"\n--- CI: {ci.state} ---")
    if ci.detail:
        print(ci.detail)
    for r in ci.results:
        print(f"  {r.state:<8} {r.label}: {r.detail}" + (f" -- {r.link}" if r.link else ""))


def render_readiness(pr: dict, target: tuple[str, str, str] | None, *, moved: bool) -> list[str]:
    if pr.get("state") == "closed":
        print("Merge readiness: NOT APPLICABLE (PR closed)")
        return []
    mergeable = pr.get("mergeable")
    label = "YES" if mergeable is True else "NO" if mergeable is False else "UNKNOWN"
    tip = f"{target[0]}:{target[1]} @ {target[2][:8]}" if target else "target tip UNKNOWN"
    print(f"Merge readiness: {label} | {tip}")
    if moved:
        print("  Target moved during this wait: CI at the head did not test the new combined tree.")
    if not target:
        return ["Target branch could not be read. Do not merge until its live tip and mergeability can be checked."]
    if mergeable is False:
        return ["GitHub reports this PR NOT MERGEABLE right now (a conflict or another block). Check the reason before merging."]
    if mergeable is not True:
        return ["Mergeability is UNKNOWN (GitHub may still be computing it); do not infer a pass. Recheck the PR before merging."]
    return ["Immediately before merging, refresh the PR and target tip and check mergeability again. "
            "Head-SHA CI does not prove the combined tree passes, and CI_PASSED does not evaluate "
            "branch protection or required checks. The merge itself is the final guard."]


def next_steps(action: str, ci: CI, *, closed: bool, rerun: str) -> list[str]:
    again = f"run `{rerun}` in the background, as before"
    if action == "ci_passed":
        return ["CI_PASSED: every check run and commit status GitHub reports for this head "
                "succeeded. It is NOT a review and NOT a merge approval: no reviewer was "
                "monitored, and branch protection was not evaluated."]
    if action == "ci_failed":
        return [f"CI_FAILED: fix the failing check(s) named above and push, or re-run them on "
                f"GitHub, then {again}. Nothing was reviewed: this is not a pass."]
    if action == "pending":
        if closed:
            return [f"PENDING: the PR is closed, so this run did not wait. CI is {ci.state}. "
                    "It is NOT a pass."]
        return [f"PENDING: CI has not settled ({ci.state}) and this run did not wait. It is NOT "
                f"a pass: to wait for it, {again}."]
    if action == "timed_out":
        why = (" No check run or commit status appeared for this head: a repo with no CI "
               "never passes here." if ci.state == "NONE" else "")
        return [f"TIMED_OUT: CI did not settle ({ci.state}) within the budget. It is NOT a pass."
                f"{why} Check the runs on GitHub, or {again}."]
    if action == "unreadable":
        fix = ("Fix the token or its access to this repo's checks and statuses, then "
               if ci.permanent else "Retry once GitHub answers: ")
        return [f"UNREACHABLE: CI could not be read. It is NOT a pass. {fix}{again}."]
    if action == "base_moved":
        return [f"BASE_MOVED: the target changed during this wait. Update/rebase if needed, run "
                f"checks against the new base, then {again}."]
    if action == "not_mergeable":
        return [f"NOT_MERGEABLE: GitHub reports mergeable: false. Clear the conflict or block, "
                f"then {again}."]
    return []


# ------------------------------------------------------------------------ main


def reject_review_options(args) -> None:
    """Exit 1 on anything that asks for a review or a Gitea CI filter."""
    why = "GitHub support monitors CI only; no review is watched"
    if args.wait_for in REJECTED_WAIT_FOR:
        sys.exit(f"--wait-for {args.wait_for}: {why}. Use --wait-for ci (the default here).")
    if args.no_fail_fast:
        sys.exit(f"--no-fail-fast: {why}, so there is nothing to keep waiting for after a failure.")
    if args.full:
        sys.exit(f"--full: {why}, so there is no review body to print.")
    for name in ("CI_WORKFLOW_FILE", "CI_JOB_NAME"):
        if getattr(poll_review, name):
            sys.exit(f"{name}: Gitea CI filters do not apply on GitHub, which reads every "
                     f"check at the head. Unset {name}.")


def resolve(args, tok: str) -> tuple[str, int]:
    if args.repo and args.pr:
        return args.repo, args.pr
    branch = poll_review._git("rev-parse", "--abbrev-ref", "HEAD")
    remotes = poll_review._git("remote", "-v")
    tracking = poll_review._git("config", "--get", f"branch.{branch}.remote") if branch else None
    ours = poll_review.remote_repos(
        (remotes or "") + "\n", f"https://{HOST}", prefer=(tracking,) if tracking else ()
    )
    if args.repo:
        repos = [args.repo]
    elif remotes is None:
        sys.exit("not a git checkout: pass --repo owner/name --pr N")
    elif not (repos := ours):
        sys.exit(f"no {HOST} remote found: pass --repo owner/name --pr N")
    if args.pr:
        return repos[0], args.pr
    return infer_pr(repos, list(dict.fromkeys([*repos, *ours])), branch, tok)


def main(args) -> int:
    reject_review_options(args)
    if args.once:
        global API_TIMEOUT_S
        API_TIMEOUT_S = ONCE_API_TIMEOUT_S
    tok = github_token()
    try:
        repo, pr_num = resolve(args, tok)
    except SystemExit:
        raise
    except Exception as exc:
        sys.exit(f"could not find the PR on {HOST} ({describe(exc)}) -- no verdict reached")
    return watch(repo, pr_num, tok, once=args.once,
                 timeout_s=args.timeout_minutes * 60, interval=args.interval)


def watch(repo: str, pr_num: int, tok: str, *, once: bool, timeout_s: float, interval: float) -> int:
    rerun = f"python3 {SCRIPT} --provider github --repo {repo} --pr {pr_num}"
    started = time.monotonic()
    deadline = started + timeout_s
    head: str | None = None
    target: tuple[str, str, str] | None = None
    false_since: float | None = None
    closed = False
    last_progress: tuple = ()
    ci = CI("NONE")

    while True:
        try:
            body, _ = api(f"/repos/{repo}/pulls/{pr_num}", tok)
            if not isinstance(body, dict) or not isinstance((body.get("head") or {}).get("sha"), str):
                raise Unreadable(f"/repos/{repo}/pulls/{pr_num}: no head sha")
            pr = body
            current = pr["head"]["sha"]
            closed = pr.get("state") == "closed"
            current_target = read_target(pr, repo, tok)
            if head is None:
                head, target = current, current_target
                print(f"babysitting {repo}#{pr_num} @ {head[:8]} (provider: github, CI only)")
                print("review: NOT_MONITORED (GitHub support watches CI only; nobody reviews this PR here)")
                if current_target is None and not closed:
                    print("  target tip unreadable; merge readiness unknown until rechecked")
                if closed:
                    print(f"!! this PR is already {'MERGED' if pr.get('merged') else 'CLOSED'} "
                          "— reporting what is known now, not waiting")
            base_moved = target is not None and current_target is not None and current_target != target
            if target is None:
                target = current_target
            if current != head:
                # CI of the old head says nothing about what would merge now.
                print(f"\n>> head moved {head[:8]} -> {current[:8]}; restarting the wait")
                head = current
                deadline = time.monotonic() + timeout_s
                false_since = None
            ci = read_ci(repo, head, tok)
        except Exception as exc:
            # A 4xx is an answer and is reported below; anything else that is
            # not transient is a bug, and a traceback says so best.
            if not is_transient(exc) and not isinstance(exc, urllib.error.HTTPError):
                raise
            waited = int(time.monotonic() - started)
            where = f"{repo}#{pr_num}" + (f" @ {head[:8]}" if head else "")
            if not is_transient(exc) or once or closed or time.monotonic() >= deadline:
                print(f"\n=== UNREACHABLE — {where} (waited {waited}s) ===\n"
                      f"GitHub could not be read ({describe(exc)}).")
                poll_review.render_next([
                    "No verdict was reached. This is NOT a pass: fix the token, repo or PR "
                    f"if the error is a 4xx, then run `{rerun}`."
                ])
                return EXIT_UNREACHABLE
            if (key := ("unreachable", describe(exc))) != last_progress:
                last_progress = key
                print(f"  [{waited}s] could not read GitHub ({describe(exc)}); trying again each interval")
            time.sleep(_pause(interval, rate_limit_wait(exc), deadline))
            continue

        now = time.monotonic()
        past = now >= deadline
        action = decide(ci, once=once or closed, past_deadline=past)
        if pr.get("mergeable") is False:
            false_since = now if false_since is None else false_since
        else:
            false_since = None
        if not closed:
            if base_moved:
                action = "base_moved"
            elif pr.get("mergeable") is False:
                if once or now - false_since >= MERGE_CHECK_GRACE_S:
                    action = "not_mergeable"
                elif action in ("ci_passed", "ci_failed") and not past:
                    # GitHub may still be settling mergeability. Readiness
                    # outranks CI, so hold a CI answer through the grace.
                    action = "wait"

        waited = int(now - started)
        if action == "wait":
            if (key := ("ci", ci.state, ci.detail)) != last_progress:
                last_progress = key
                print(f"  [{waited}s] ci={ci.state}" + (f": {ci.detail}" if ci.detail else ""))
            time.sleep(_pause(interval, ci.retry_after or None, deadline))
            continue
        if base_moved:
            print(f">> target moved {target[0]}:{target[1]} {target[2][:8]} -> "
                  f"{current_target[0]}:{current_target[1]} {current_target[2][:8]}")
        print(f"\n=== {LABELS[action]} — {repo}#{pr_num} @ {head[:8]} (waited {waited}s) ===")
        print("review: NOT_MONITORED — CI only; this is not a review")
        render_ci(ci)
        readiness = render_readiness(pr, current_target, moved=base_moved)
        poll_review.render_next(next_steps(action, ci, closed=closed, rerun=rerun) + readiness)
        return CODES[action]


def _pause(interval: float, wait: float | None, deadline: float) -> float:
    """The interval, or longer when GitHub asked for it -- but not past the
    deadline, where the run reports instead."""
    return max(0.0, min(max(interval, wait or 0.0), deadline - time.monotonic()))
