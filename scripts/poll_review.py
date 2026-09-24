#!/usr/bin/env python3
"""Wait for a Gitea instance's review-bot to finish reviewing a pull request.

Prints one structured verdict and exits. Never merges, never edits anything.

The bot posts `event: COMMENT` reviews only, so Gitea cannot gate a merge on
it. That makes this poller the only thing standing between "I opened a PR" and
"I merged before the review landed".

The agent answers for itself. `GET /state/{owner}/{repo}/{pr}?sha=<head>`
reports whether a job for that commit is queued, running, finished, or never
coming -- and when no review is coming, which of worker.py's decline paths it
hit, as a closed enum reason code. This poller consults it every interval and
stops on a terminal answer instead of burning the whole budget.

Precedence is deliberate: Gitea stays the source of truth for anything the bot
actually posted, and the endpoint speaks only where Gitea is silent. An
unreachable endpoint degrades to Gitea-only behaviour -- never to a verdict of
its own. Silence from BOTH is still reported as TIMED_OUT, never as a pass.

It also watches CI for the same head SHA, independently of the review verdict
-- see CIVerdict / classify_ci / read_ci. By default it assumes no workflow
file or job name: it discovers every workflow run at the head SHA, keeps the
newest run per workflow and event, and aggregates every job in them worst-of.
Set CI_WORKFLOW_FILE and/or CI_JOB_NAME to pin it to one workflow/job instead.
None of the jobs set `timeout-minutes`, so a hang reports as RUNNING with a
growing elapsed time, not as a failure. A CI API that cannot be read is
UNKNOWN, never NONE: "I could not look" is not "there is nothing to see".

Exit codes are driven by the review verdict alone: 0 REVIEWED, 1 usage error
or Gitea unreachable until the deadline, 2 TIMED_OUT, 3 FAILED, 4 SKIPPED,
5 STALE, 6 DECLINED. CI status is reported, never folded into these -- the two
checks fail in unrelated ways and conflating them would make "what do you do"
ambiguous for both.

A FAILED CI verdict does, however, stop the *wait* (--no-fail-fast keeps
waiting). That is a scheduling decision, not an exit-code one: the code
returned is still whatever the review verdict was, PENDING falling back to 2
exactly as under --once. A failed job changes only when someone acts -- pushes
a fix, which also makes any review still in flight stale, or re-runs the job
by hand -- so waiting on it buys nothing. A STALE review's inline findings are
fetched before bailing, so the early return never swallows them.

A closed or merged PR is checked once and reported, not waited on: it will not
get another review.
"""

from __future__ import annotations

import argparse
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
from datetime import datetime, timezone

from gitea_auth import BASE_URL, token

BOT = os.environ.get("REVIEW_BOT_USERNAME", "review-bot")
HEALTH_URL = os.environ.get(
    "REVIEW_AGENT_HEALTH_URL", "http://localhost:8000/healthz"
)
STATE_URL = os.environ.get(
    "REVIEW_AGENT_STATE_URL", "http://localhost:8000/state"
)
# Only needed if the deployment sets STATE_TOKEN. Without it the endpoint 401s,
# agent_state swallows it, and the poller degrades to Gitea-only -- which looks
# exactly like "the agent is unreachable". That ambiguity is the thing this
# endpoint exists to remove, so the header is worth carrying.
STATE_TOKEN = os.environ.get("REVIEW_AGENT_STATE_TOKEN", "")

# Unset by default -- ci_jobs() then discovers every workflow run at the head
# SHA and reports every job in it, which is what working on an unfamiliar repo
# requires. Set either to narrow the check to one workflow file / job name.
CI_WORKFLOW_FILE = os.environ.get("CI_WORKFLOW_FILE")
CI_JOB_NAME = os.environ.get("CI_JOB_NAME")

# Observed passing durations run 6-11m. Past this many seconds RUNNING gets
# flagged as possibly hung rather than just slow -- the job itself sets no
# timeout-minutes, so nothing else will ever say so.
CI_SLOW_THRESHOLD_S = 15 * 60

# How long a CI verdict of NONE has to hold before it counts as settled --
# see `ci_settled`, which is where the reasoning lives.
#
# Measured from when THIS POLLER started watching the head, deliberately not
# from the head commit's date. A commit is dated when it was made, which can be
# arbitrarily earlier than when it was pushed: a branch committed to an hour
# ago and pushed now carries an hour-old date on a head that reached the server
# seconds ago. Dating the grace off the commit would read that head as
# long-settled and put the race straight back.
CI_NONE_GRACE_S = 90

# Terminal issue-comment markers, straight from review_agent/worker.py.
SKIP_MARKER = "This PR's diff is too large"
FAIL_MARKERS = {
    "credentials": "credentials are not working",
    "quota": "run out of model quota",
    # The backend refused the prompt outright (codex's 1MB input cap). Distinct
    # from SKIP_MARKER, which is the agent's own line-count guard declining
    # before it ever calls a backend.
    "oversized": "too large for the reviewer to accept",
    "generic": "Automated review failed",
}

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------- pure logic


@dataclass
class Verdict:
    state: str  # REVIEWED | STALE | SKIPPED | FAILED | DECLINED | PENDING
    detail: str = ""
    overview: str = ""
    inline: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    review_id: int | None = None
    reviewed_sha: str | None = None


@dataclass
class CIVerdict:
    state: str  # PASSED | FAILED | RUNNING | UNKNOWN | NONE
    detail: str = ""
    # UNKNOWN only. True when waiting cannot fix it -- a 4xx from the Actions
    # API means the token cannot read Actions or Actions is off for the repo --
    # so it settles at once instead of holding the wait open to the deadline.
    permanent: bool = False


def _job_state(job: dict, now: datetime) -> tuple[str, str]:
    """(state, detail) for one job -- PASSED | FAILED | SKIPPED | RUNNING | UNKNOWN.

    RUNNING covers queued, waiting and in_progress alike -- the reader does
    not need those distinguished, only "not decided yet" vs "decided".

    SKIPPED is not a failure. A job whose `if:` evaluated false completes with
    conclusion 'skipped', which is the *designed* outcome for a conditional job
    -- e.g. a `promote` job guarded by `github.event_name == 'push'` so a PR run
    never holds a write token is skipped on every PR, forever. Reading it as
    FAILED reported a green gate as a red CI. 'neutral' joins it -- "completed,
    deliberately neither pass nor fail". 'cancelled' deliberately does NOT: an
    aborted job decided nothing about the commit, and anything softer than
    FAILED would let a cancelled run read as clean. (A cancelled run that a
    newer run of the same workflow superseded never reaches here -- ci_jobs
    keeps only the newest run.)

    UNKNOWN is a run whose jobs could not be fetched: `ci_jobs` tags it with
    `_error` rather than dropping it, since the unreadable run could be the
    failing one."""
    if "_error" in job:
        return "UNKNOWN", f"jobs could not be read ({job['_error']})"
    status = job.get("status")
    conclusion = job.get("conclusion")
    if status == "completed":
        if conclusion == "success":
            return "PASSED", "succeeded"
        if conclusion in ("skipped", "neutral"):
            return "SKIPPED", f"concluded {conclusion!r} (not gating)"
        return "FAILED", f"concluded {conclusion!r}"
    started = parse_ts(job.get("started_at"))
    detail = f"status={status or '?'}"
    if started != EPOCH:
        elapsed = int((now - started).total_seconds())
        detail += f", running {elapsed // 60}m{elapsed % 60:02d}s"
        if elapsed > CI_SLOW_THRESHOLD_S:
            detail += (
                " -- longer than any observed passing run (6-11m) and this "
                "job sets no timeout-minutes; it may be hung, not just slow"
            )
    return "RUNNING", detail


def classify_ci(jobs: list[dict], now: datetime) -> CIVerdict:
    """Aggregate every job `ci_jobs` matched into one verdict, worst-of.

    An empty list means the Actions API answered and no run matched -- either
    "nothing has triggered yet" or "this repo triggers nothing matching". A
    failure to ask never reaches here; `read_ci` reports that as UNKNOWN.

    Worst-of ordering: FAILED > RUNNING > UNKNOWN > PASSED. One FAILED job fails
    the whole verdict even if every other job passed; short of that, one
    RUNNING job keeps it open; short of that, one unreadable run keeps it from
    reading as a pass.

    SKIPPED jobs are excluded from that ordering entirely: they neither fail
    the verdict nor hold it open. They are still named in the PASSED detail. If
    *every* matched job was skipped the verdict is NONE, not PASSED.
    """
    labeled = [
        (f"{j.get('_workflow', '?')}/{j.get('name', '?')}", *_job_state(j, now))
        for j in jobs
        if isinstance(j, dict)
    ]
    if not labeled:
        scope = CI_WORKFLOW_FILE or "workflow"
        return CIVerdict(state="NONE", detail=f"no {scope} run found for this commit")

    def of(state: str) -> list[tuple[str, str]]:
        return [(label, d) for label, s, d in labeled if s == state]

    for state in ("FAILED", "RUNNING", "UNKNOWN"):
        if hits := of(state):
            return CIVerdict(
                state=state, detail="; ".join(f"{l}: {d}" for l, d in hits)
            )
    if not of("PASSED"):
        # Nothing failed, nothing is running, and nothing passed either --
        # every matched job was skipped, so no job actually attested to this
        # commit. That is NONE, not a pass: calling it PASSED would manufacture
        # an all-clear out of a workflow that never ran a check.
        return CIVerdict(
            state="NONE", detail="every matched job was skipped; nothing ran"
        )
    # Skipped jobs stay in the detail line -- a job the reader expected to run
    # and that quietly skipped is exactly what they need named.
    return CIVerdict(
        state="PASSED", detail="; ".join(f"{l}: {d}" for l, _s, d in labeled)
    )


def ci_settled(ci: CIVerdict, watched_s: float) -> bool:
    """Whether the CI side has reached a state more waiting cannot change.

    RUNNING never has. PASSED and FAILED have: a completed job changes only if
    someone re-runs it, and waiting for that is not this poller's job. UNKNOWN
    has only when it is permanent (a 4xx); a transport blip may clear on the
    next poll, so it holds the wait open like RUNNING.

    NONE is the ambiguous one. It conflates "this repo triggers no matching
    workflow", which waiting will never change, and "Gitea has not created the
    run yet", which waiting is precisely what changes. Reading it as terminal
    picks the unsafe half: paired with a review that decided fast (agent_state
    returns DECLINED for a filtered-empty diff almost immediately), the poller
    could return inside its first interval reporting CI: NONE for a gate that
    was seconds from starting -- a false all-clear.

    So NONE settles only after the poller has watched this head for
    CI_NONE_GRACE_S. The grace costs nothing while the review is still pending.
    """
    if ci.state == "RUNNING":
        return False
    if ci.state == "UNKNOWN":
        return ci.permanent
    if ci.state == "NONE":
        return watched_s >= CI_NONE_GRACE_S
    return True


def parse_ts(value: str | None) -> datetime:
    """Gitea hands back ISO-8601. A missing/unparseable stamp sorts oldest so it
    can never win a most-recent-signal comparison."""
    if not value:
        return EPOCH
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return EPOCH


def extract_notes(body: str) -> list[str]:
    """Findings the bot could not anchor to a changed line.

    validate_comments() drops those from the inline set and build_review_payload
    renders them into an "Additional notes" section of the review BODY instead.
    A review with comments_count == 0 can still carry real findings here, which
    is why the body is parsed rather than trusting the count."""
    marker = "**Additional notes**"
    idx = body.find(marker)
    if idx == -1:
        return []
    tail = body[idx + len(marker) :]
    # The section runs to the footer's horizontal rule or end of body.
    tail = re.split(r"\n---\n", tail)[0]
    return [
        line.strip()[2:].strip()
        for line in tail.splitlines()
        if line.strip().startswith("- ")
    ]


# Agent verdicts that mean "stop waiting". `in_progress`, `posted` and `unknown`
# all mean "keep looking at Gitea" -- `posted` in particular is only the agent's
# claim that it wrote a review, which the review list confirms or denies far
# better than the agent's own memory does.
TERMINAL_AGENT_VERDICTS = {"declined", "failed", "lost"}

# Reasons that already have a verdict of their own. Reported as SKIPPED so that
# one input yields one exit code: the same oversized diff exits 4 whether this
# poller learned it from the agent's `ℹ️ diff is too large` comment or from the
# endpoint, rather than 4 or 6 depending on comment timing.
SKIPPED_REASONS = {"diff_too_many_lines", "diff_too_many_bytes"}


def agent_verdict(state: dict | None, head_sha: str) -> Verdict | None:
    """Translate agent state into a terminal verdict, or None to keep waiting.

    Every field is re-checked rather than trusted. The endpoint's contract says
    `verdict` is a string and `outcomes` a newest-first list of objects, but an
    exception raised here ends the babysit as surely as a wrong verdict does,
    and this poller's whole job is to keep waiting when it cannot get a straight
    answer. A response it cannot read is a response it did not get.

    Returns None for everything that is not over: no state, a job still queued
    or running, and a terminal verdict whose reason cannot be named -- with
    nothing to report to the reader there is nothing to stop the wait with.

    `retryable` is deliberately NOT a reason to keep waiting. It means the
    reason could plausibly succeed on a fresh attempt, not that one is in
    flight: _run_review_bounded exhausts REVIEW_MAX_ATTEMPTS inside a single
    job and queue.py does not retry, so while the agent is still trying the
    live job makes the endpoint answer `in_progress`. By the time a record
    exists at all, the agent has stopped."""
    if not isinstance(state, dict):
        return None
    verdict = state.get("verdict")
    if not isinstance(verdict, str) or verdict not in TERMINAL_AGENT_VERDICTS:
        return None
    outcomes = state.get("outcomes")
    top = outcomes[0] if isinstance(outcomes, list) and outcomes else None
    if not isinstance(top, dict):
        return None
    sha = top.get("sha")
    if isinstance(sha, str) and sha and sha != head_sha:
        # The endpoint filters outcomes by ?sha=, so this should not happen. If
        # it does, the record answers for a different commit than the one about
        # to be merged -- which is the STALE case, not an answer. A null sha is
        # legitimate (an ingress decline filed before the head sha was known)
        # and still counts.
        return None
    reason = top.get("reason")
    if not isinstance(reason, str) or not reason:
        return None
    hint = " (a fresh review request may succeed)" if top.get("retryable") else ""
    if reason in SKIPPED_REASONS:
        return Verdict(state="SKIPPED", detail=f"[agent] {reason}")
    if verdict == "failed":
        return Verdict(state="FAILED", detail=f"[agent] {reason}{hint}")
    return Verdict(
        state="DECLINED", detail=f"[agent] no review is coming: {reason}{hint}"
    )


def _newest(reviews: list[dict]) -> dict:
    # Id breaks a same-second tie: Gitea ids only grow.
    return max(
        reviews,
        key=lambda r: (parse_ts(r.get("submitted_at")), r.get("id") or 0),
    )


def classify(
    head_sha: str,
    reviews: list[dict],
    comments: list[dict],
    floor: datetime,
    bot: str = BOT,
    state: dict | None = None,
) -> Verdict:
    """Decide what the bot has said about `head_sha`, if anything.

    Precedence is deliberate:

    1. A review anchored to head_sha is the strongest positive signal and wins
       outright -- an older warning comment cannot demote a completed review.
       If there are several (a re-requested review of the same commit), the
       NEWEST wins: Gitea lists reviews oldest first, and taking the first one
       reported the superseded review's findings instead of the current ones.
    2. Otherwise a terminal comment (skip/fail) counts only if it is newer than
       `floor`, which callers set past the head commit's own timestamp and past
       any earlier review. Issue comments carry no SHA, so without that floor a
       stale warning from a previous push would terminate the wait wrongly.
    3. Otherwise the agent's own state endpoint, if it answered. It speaks only
       where Gitea is silent -- anything the bot actually posted outranks the
       agent's memory of what it meant to post -- but it outranks STALE, because
       a re-push declined as `nothing_new_since_last_review` would otherwise sit
       in STALE until the deadline.
    4. A review at a *different* SHA means the bot has spoken before but not
       about what is about to be merged -- STALE, keep waiting. Its findings
       are still carried on the Verdict and printed: review-bot routinely lands
       a review on the head you have already pushed past, and those findings
       are usually about code you still have.
    """
    bot_reviews = [r for r in reviews if (r.get("user") or {}).get("login") == bot]

    at_head = [r for r in bot_reviews if r.get("commit_id") == head_sha]
    if at_head:
        r = _newest(at_head)
        body = r.get("body") or ""
        return Verdict(
            state="REVIEWED",
            detail=f"review {r.get('id')} at {head_sha[:8]}",
            overview=body,
            notes=extract_notes(body),
            review_id=r.get("id"),
            reviewed_sha=head_sha,
        )

    bot_comments = [
        c
        for c in comments
        if (c.get("user") or {}).get("login") == bot
        and parse_ts(c.get("created_at")) > floor
    ]
    bot_comments.sort(key=lambda c: parse_ts(c.get("created_at")))
    for c in reversed(bot_comments):
        body = c.get("body") or ""
        if SKIP_MARKER in body:
            return Verdict(state="SKIPPED", detail=body.strip())
        for variant, marker in FAIL_MARKERS.items():
            if marker in body:
                return Verdict(state="FAILED", detail=f"[{variant}] {body.strip()}")

    if (from_agent := agent_verdict(state, head_sha)) is not None:
        return from_agent

    if bot_reviews:
        newest = _newest(bot_reviews)
        # The verdict stays STALE and the exit code stays 5: those describe an
        # older SHA truthfully and must not be laundered into REVIEWED. Only
        # the findings ride along.
        body = newest.get("body") or ""
        return Verdict(
            state="STALE",
            detail=(
                f"last review {newest.get('id')} is anchored to "
                f"{(newest.get('commit_id') or '?')[:8]}, head is {head_sha[:8]}"
            ),
            overview=body,
            notes=extract_notes(body),
            review_id=newest.get("id"),
            reviewed_sha=newest.get("commit_id"),
        )

    return Verdict(state="PENDING")


def signal_floor(head_commit_date: datetime | None, reviews: list[dict], bot: str) -> datetime:
    """Earliest moment a bot signal could plausibly be about the current head.

    Limitation, stated rather than hidden: the head commit's committer date is
    when the commit was made, not when it was pushed, so a commit made an hour
    before its push puts the floor an hour early. Taking the max with the newest
    prior review closes the common case; the residual risk is a warning comment
    from that window being read as current. It is reported with its timestamp
    so a human can catch it."""
    floors = [head_commit_date or EPOCH]
    floors += [
        parse_ts(r.get("submitted_at"))
        for r in reviews
        if (r.get("user") or {}).get("login") == bot
    ]
    return max(floors)


REVIEW_DONE = ("REVIEWED", "SKIPPED", "FAILED", "DECLINED")


def decide(
    review_state: str,
    ci: CIVerdict,
    *,
    watched_s: float,
    past_deadline: bool,
    once: bool,
    fail_fast: bool,
) -> str:
    """What the loop does with one poll's two verdicts. Pure, so every branch
    of the loop is testable without a clock or a network.

      done       both sides settled -- exit with the review's code
      once       --once, or a closed PR -- report what is known now
      ci_failed  CI FAILED while the review is undecided -- stop waiting
      ci_open    deadline; review decided, CI not settled
      stale      deadline; only an older-SHA review exists
      timed_out  deadline; nothing at all
      wait       poll again
    """
    review_done = review_state in REVIEW_DONE
    if review_done and ci_settled(ci, watched_s):
        return "done"
    if once:
        return "once"
    if fail_fast and ci.state == "FAILED":
        return "ci_failed"
    if past_deadline:
        if review_done:
            return "ci_open"
        if review_state == "STALE":
            return "stale"
        return "timed_out"
    return "wait"


EXIT = {"REVIEWED": 0, "TIMED_OUT": 2, "FAILED": 3, "SKIPPED": 4, "STALE": 5,
        "DECLINED": 6}
EXIT_UNREACHABLE = 1


def exit_code(action: str, review_state: str) -> int:
    """The review's own code; PENDING has none, so it reads as 2 (TIMED_OUT)."""
    if action == "timed_out":
        return EXIT["TIMED_OUT"]
    if action == "stale":
        return EXIT["STALE"]
    return EXIT.get(review_state, EXIT["TIMED_OUT"])


# ------------------------------------------------------------------- transport


def is_transient(exc: BaseException) -> bool:
    """A failure a later attempt could clear: transport errors and 5xx.

    A 401, a 404 or any other 4xx is an answer -- the wrong token, the wrong
    repo -- and retrying it just delays the report of a real problem. Note that
    HTTPError subclasses URLError, so it has to be checked first."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    return isinstance(
        exc,
        (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError),
    )


# Short in-call retries for a single bad read. A longer outage is the poll
# loop's problem: it skips the round and tries again next interval, so one
# broken minute cannot end a 35-minute wait with a traceback.
API_RETRIES = 3
API_RETRY_BACKOFF = 2.0


def api(path: str, tok: str):
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1{path}",
        headers={"Authorization": f"token {tok}", "Accept": "application/json"},
    )
    last: Exception | None = None
    for attempt in range(API_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            if not is_transient(exc):
                raise
            last = exc
        if attempt < API_RETRIES - 1:
            time.sleep(API_RETRY_BACKOFF * (attempt + 1))
    raise last if last else RuntimeError(f"api({path}) failed with no error recorded")


PAGE_LIMIT = 50


def api_paged(path: str, tok: str, key: str | None = None) -> list:
    """`api`, but walks every page of a list endpoint.

    Gitea paginates list endpoints at 50, **oldest first**, and returns the
    first page silently when you do not ask for one. That is the worst
    possible default here: the reviews that matter are the newest, so a
    single unpaginated fetch returns exactly the ones you do not need and
    hides the ones you do. A PR only has to survive 50 reviews for that to
    start quietly lying.

    `key` names the list inside an object response, for endpoints that wrap
    it (`workflow_runs`, `jobs`).

    Stops on a short or empty page. A page that is not a list (an error
    object) also stops, rather than raising: degrading to what was collected
    beats crashing a poller mid-wait.
    """
    out: list = []
    sep = "&" if "?" in path else "?"
    page = 1
    while True:
        resp = api(f"{path}{sep}page={page}&limit={PAGE_LIMIT}", tok)
        chunk = resp.get(key) if key is not None and isinstance(resp, dict) else resp
        if not isinstance(chunk, list) or not chunk:
            return out
        out.extend(chunk)
        if len(chunk) < PAGE_LIMIT:
            return out
        page += 1


def health() -> str:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=8) as resp:
            return f"up ({resp.read().decode().strip()})"
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot confirm up"
        return f"DOWN or unreachable ({type(exc).__name__}: {exc})"


def agent_state(repo: str, pr: int, sha: str) -> dict | None:
    """Ask the agent what it knows about this commit.

    `repo` is already `owner/name`, which is exactly the endpoint's
    `/state/{owner}/{repo}/{pr}` shape -- so the slash inside it is a path
    separator on purpose, and quote() keeps it.

    Returns None on any failure. An unreachable, unauthorised or
    not-yet-deployed agent must degrade to Gitea-only behaviour, never turn
    into a verdict of its own."""
    url = (
        f"{STATE_URL}/{urllib.parse.quote(repo)}/{pr}"
        f"?sha={urllib.parse.quote(sha, safe='')}"
    )
    req = urllib.request.Request(url)
    if STATE_TOKEN:
        req.add_header("Authorization", f"Bearer {STATE_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - any failure means "cannot ask"
        return None
    return body if isinstance(body, dict) else None


def ci_jobs(
    repo: str,
    sha: str,
    tok: str,
    workflow_file: str | None = CI_WORKFLOW_FILE,
    job_name: str | None = CI_JOB_NAME,
) -> list[dict]:
    """Every job matching `workflow_file`/`job_name` for `sha`, each tagged
    with its workflow file as `_workflow`. Neither filter set (the default)
    returns every job in every workflow run at this head SHA.

    Only the NEWEST run per (workflow, event) counts. A push can leave several
    runs of one workflow at the same SHA -- a re-trigger, or a concurrency
    group cancelling the earlier one -- and worst-of over all of them let a
    superseded `cancelled` run fail the verdict forever, next to the newer run
    that passed. Different events (push vs pull_request) stay separate: both
    tested this commit, and either failing is news.

    Two calls per run, not one: the runs listing finds candidates; each run's
    `/jobs` then gets JOB-level status, because a run's own conclusion is an
    aggregate across every job inside it. Both are paged -- a matrix build can
    exceed one page of jobs.

    A failure listing the runs RAISES: `read_ci` turns it into UNKNOWN. It
    used to return [], which classify_ci read as "no run found" -- a 403 from
    a token that cannot read Actions reported as "this repo has no CI". A run
    whose jobs cannot be fetched comes back as one `_error` entry rather than
    being dropped, since the unreadable run could be the failing one."""
    runs = api_paged(
        f"/repos/{repo}/actions/runs?head_sha={urllib.parse.quote(sha, safe='')}",
        tok,
        key="workflow_runs",
    )
    latest: dict[tuple[str, object], dict] = {}
    for r in runs:
        if not isinstance(r, dict) or not isinstance(r.get("id"), int):
            continue
        path = (r.get("path") or "").split("@", 1)[0]
        if workflow_file is not None and path != workflow_file:
            continue
        k = (path, r.get("event"))
        if k not in latest or r["id"] > latest[k]["id"]:
            latest[k] = r
    jobs: list[dict] = []
    for run in sorted(latest.values(), key=lambda r: r["id"]):
        workflow = ((run.get("path") or "?").split("@", 1)[0]).rsplit("/", 1)[-1]
        try:
            listed = api_paged(
                f"/repos/{repo}/actions/runs/{run['id']}/jobs", tok, key="jobs"
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            jobs.append({
                "name": f"run {run['id']}",
                "_workflow": workflow,
                "_error": f"{type(exc).__name__}: {exc}",
            })
            continue
        for j in listed:
            if not isinstance(j, dict):
                continue
            if job_name is not None and j.get("name") != job_name:
                continue
            jobs.append({**j, "_workflow": workflow})
    return jobs


def read_ci(repo: str, sha: str, tok: str, now: datetime) -> CIVerdict:
    """classify_ci over ci_jobs, with a failure to ask reported as UNKNOWN.

    Never NONE on an error: NONE settles after the grace window, so an
    unreadable CI next to a clean review would exit 0 on a gate nobody saw."""
    try:
        jobs = ci_jobs(repo, sha, tok)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return CIVerdict(
                state="UNKNOWN",
                detail=(
                    f"the Actions API answered {exc.code} {exc.reason} -- the "
                    "token may not be able to read Actions, or Actions is off "
                    "for this repo. This is NOT 'no CI': nobody looked."
                ),
                permanent=True,
            )
        return CIVerdict(
            state="UNKNOWN",
            detail=f"the Actions API answered {exc.code} {exc.reason}; retrying",
        )
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        return CIVerdict(
            state="UNKNOWN",
            detail=f"could not read CI ({type(exc).__name__}: {exc}); retrying",
        )
    return classify_ci(jobs, now)


def repo_from_remotes(remotes: str, base_url: str) -> str | None:
    """owner/name from `git remote -v` output, for the remote on base_url's host.

    Matches on the hostname alone, with an optional port, so an https remote,
    an scp-style `git@host:owner/name` remote and an `ssh://git@host:2222/...`
    remote all resolve."""
    host = urllib.parse.urlsplit(base_url).hostname or base_url.split("://", 1)[-1]
    pattern = rf"{re.escape(host)}(?::\d+)?[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s"
    match = re.search(pattern, remotes)
    return match.group(1) if match else None


def infer_repo() -> str:
    """owner/name from the current checkout's Gitea remote."""
    try:
        out = subprocess.run(
            ["git", "remote", "-v"], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("not a git checkout: pass --repo owner/name")
    if repo := repo_from_remotes(out, BASE_URL):
        return repo
    sys.exit(f"no remote on {BASE_URL} found: pass --repo owner/name")


def infer_pr(repo: str, tok: str) -> int:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Paged for the same reason the reviews list is: an unpaged fetch silently
    # gives up on PR #51 onward and reports "no open PR found", which reads as
    # "you have no PR" rather than "I stopped looking".
    for pr in api_paged(f"/repos/{repo}/pulls?state=open", tok):
        if (pr.get("head") or {}).get("ref") == branch:
            return pr["number"]
    sys.exit(f"no open PR found for branch {branch!r}: pass --pr N")


def head_commit_date(repo: str, sha: str, tok: str) -> datetime | None:
    try:
        commit = api(f"/repos/{repo}/git/commits/{sha}", tok)
    except Exception:  # noqa: BLE001 - falling back to the PR's own clock is fine
        return None
    if not isinstance(commit, dict):
        return None
    inner = (commit.get("commit") or {}).get("committer") or {}
    return parse_ts(inner.get("date")) if inner.get("date") else None


# ---------------------------------------------------------------------- report


def trim_overview(body: str) -> str:
    """Drop the per-file "Reviewed changes" table.

    It is one row per changed file inside a <details> block -- 40 rows on a
    normal PR, which buries the findings the reader is here for. The prose
    overview above it and the Additional-notes section below it both survive."""
    return re.sub(
        r"\n?-*\n?## Reviewed changes.*?</details>\n?",
        "\n",
        body,
        flags=re.DOTALL,
    )


def render(
    verdict: Verdict, repo: str, pr: int, head: str, waited: int, full: bool = False
) -> None:
    print(f"\n=== {verdict.state} — {repo}#{pr} @ {head[:8]} (waited {waited}s) ===")
    if verdict.detail:
        print(verdict.detail)
    if verdict.state == "STALE":
        print(
            f"\n!! These findings describe {(verdict.reviewed_sha or '?')[:8]}, "
            f"NOT the head {head[:8]}.\n"
            "!! That is the normal case, not a leftover: review-bot routinely "
            "lands a review\n"
            "!! anchored to the head you already pushed past. Read them — they "
            "were written\n"
            "!! about code you probably still have — then verify each one "
            "against the current\n"
            "!! tree before acting on it."
        )
    elif verdict.state != "REVIEWED":
        return
    body = verdict.overview if full else trim_overview(verdict.overview)
    print(f"\n{body.strip()}")
    if verdict.inline:
        open_n = sum(1 for c in verdict.inline if not c.get("resolver"))
        print(
            f"\n--- {len(verdict.inline)} inline comment(s), {open_n} unresolved ---"
        )
        for c in verdict.inline:
            # `resolver` is null while the thread is open and names the user who
            # closed it otherwise. Printing the comment id makes each thread
            # addressable for a reply or a resolve downstream.
            who = (c.get("resolver") or {}).get("login")
            status = f"RESOLVED by {who}" if who else "open"
            print(
                f"\n[#{c.get('id')} {status}] "
                f"{c.get('path')}:{c.get('new_position') or c.get('position')}"
            )
            print((c.get("body") or "").strip())
    else:
        print("\n--- 0 inline comments ---")
    if verdict.notes:
        print(f"\n--- {len(verdict.notes)} unanchored finding(s) from the body ---")
        for note in verdict.notes:
            print(f"  - {note}")


def render_ci(verdict: CIVerdict) -> None:
    print(f"\n--- CI: {verdict.state} ---")
    if verdict.detail:
        print(verdict.detail)


def report_timed_out(
    repo: str, pr_num: int, head: str, waited: int, requested: bool, ci: CIVerdict
) -> None:
    print(f"\n=== TIMED_OUT — {repo}#{pr_num} @ {head[:8]} (waited {waited}s) ===")
    print(f"review agent health: {health()}")
    last = agent_state(repo, pr_num, head)
    if last is None:
        print(
            "review agent state: UNREACHABLE — this poller could not ask "
            "the agent what happened, so nothing below is decidable from "
            "here."
        )
    else:
        svc = last.get("service")
        if not isinstance(svc, dict):
            svc = {}
        print(
            f"review agent state: verdict={last.get('verdict')} "
            f"live={last.get('live')}"
        )
        print(f"  queue={svc.get('queue')} counters={svc.get('counters')}")
    print(
        "\nNo review, no skip notice, no failure notice.\n"
        "This is NOT an approval. It means one of:\n"
        "  - the agent is still working (it allows up to ~30 min),\n"
        "  - the webhook never arrived — a `deliveries` counter of 0 "
        "above says so, and a nonzero `rejected_signature` means the\n"
        "    WEBHOOK_SECRET is wrong,\n"
        "  - the agent could not be asked at all (see the state line).\n"
        "The decline paths — an empty diff after SKIP_PATHS, nothing new since "
        "the last review, a declined event,\n"
        "a job lost to a restart — report as DECLINED before the deadline. "
        "Reaching this message means it was none of them."
    )
    if not requested:
        # Not a diagnosis: parse_event enqueues on `opened` regardless of
        # who was requested, so an unrequested PR should still have been
        # reviewed. But `review_requested` is a live trigger, so asking
        # is the one lever available from here.
        print(
            f"\n{BOT} is not a requested reviewer on this PR. That should "
            "not matter (the bot reviews on open too), but requesting it "
            "fires the review_requested trigger and enqueues a fresh job:\n"
            f"  curl -X POST -H \"Authorization: token $GITEA_TOKEN\" \\\n"
            f"    -H 'Content-Type: application/json' \\\n"
            f"    -d '{{\"reviewers\":[\"{BOT}\"]}}' \\\n"
            f"    {BASE_URL}/api/v1/repos/{repo}/pulls/{pr_num}/requested_reviewers"
        )
    render_ci(ci)


EPILOGUE = {
    "ci_failed": (
        "\n>> CI FAILED and the review is still {state}. Stopping early: a "
        "failed job stays failed\n   until someone acts — push a fix (which "
        "makes any review landing meanwhile stale)\n   or re-run the job — "
        "then re-run this poller.\n   The review above (if any) has NOT "
        "decided anything — do not read this as a pass.\n   Pass "
        "--no-fail-fast to wait for review-bot anyway."
    ),
    "ci_open": (
        "\nReview finished, but CI did not settle within the timeout budget — "
        "still running, or it\ncould not be read (see the CI block). Not a "
        "failure — but do not merge on the assumption\nit passed; check the "
        "run directly."
    ),
    "stale": (
        "\nThe review above is real but anchored to an older SHA, so it is not "
        "a verdict on\nwhat you are about to merge. It is also not nothing: "
        "read it. Re-request the bot\nif you need a review of the current head."
    ),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="owner/name (default: infer from git remote)")
    ap.add_argument("--pr", type=int, help="PR number (default: infer from branch)")
    ap.add_argument("--timeout-minutes", type=float, default=35.0)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="check once, do not wait")
    ap.add_argument("--no-fail-fast", action="store_true",
                    help="keep waiting for the review even after CI has failed")
    ap.add_argument("--full", action="store_true",
                    help="keep the per-file Reviewed changes table in the body")
    args = ap.parse_args(argv)

    tok = token()
    repo = args.repo or infer_repo()
    pr_num = args.pr or infer_pr(repo, tok)

    pr = api(f"/repos/{repo}/pulls/{pr_num}", tok)
    head = pr["head"]["sha"]
    commit_date = head_commit_date(repo, head, tok)
    # NB: only the single-PR endpoint populates requested_reviewers. The list
    # endpoint returns [] for every PR, which reads as "nobody was asked".
    requested = BOT in [
        r.get("login") for r in (pr.get("requested_reviewers") or [])
    ]
    print(
        f"babysitting {repo}#{pr_num} @ {head[:8]} (bot: {BOT}, "
        f"requested: {'yes' if requested else 'NO'})"
    )
    closed = pr.get("state") == "closed"
    if closed:
        print(
            f"!! this PR is already {'MERGED' if pr.get('merged') else 'CLOSED'} "
            "— reporting what is known now, not waiting: it gets no new review"
        )
    if (pr.get("user") or {}).get("login") == BOT:
        # DECLINED, not TIMED_OUT: nothing timed out. parse_event files this as
        # `bot_authored_pr`.
        print(f"!! authored by {BOT} — parse_event skips these; no review will come")
        render_ci(read_ci(repo, head, tok, datetime.now(timezone.utc)))
        return EXIT["DECLINED"]

    deadline = time.monotonic() + args.timeout_minutes * 60
    started = time.monotonic()
    # Reset on every head move, same as the deadline: the CI grace window asks
    # "has THIS head had time to trigger a run", so a new head restarts it.
    watching_since = time.monotonic()

    while True:
        try:
            pr = api(f"/repos/{repo}/pulls/{pr_num}", tok)
            current = pr["head"]["sha"]
            if current != head:
                # A push landed mid-wait. A review anchored to the old SHA says
                # nothing about what would now be merged, so restart the clock.
                print(f"\n>> head moved {head[:8]} -> {current[:8]}; restarting the wait")
                head = current
                commit_date = head_commit_date(repo, head, tok)
                deadline = time.monotonic() + args.timeout_minutes * 60
                watching_since = time.monotonic()
            if pr.get("state") == "closed" and not closed:
                closed = True
                print(">> the PR was closed mid-wait — reporting what is known now")

            reviews = api_paged(f"/repos/{repo}/pulls/{pr_num}/reviews", tok)
            comments = api_paged(f"/repos/{repo}/issues/{pr_num}/comments", tok)
            floor = signal_floor(commit_date, reviews, BOT)
            # Consulted every interval rather than once: a job can be enqueued,
            # superseded and declined between two polls.
            verdict = classify(
                head, reviews, comments, floor, BOT, agent_state(repo, pr_num, head)
            )
            ci_verdict = read_ci(repo, head, tok, datetime.now(timezone.utc))

            now = time.monotonic()
            action = decide(
                verdict.state,
                ci_verdict,
                watched_s=now - watching_since,
                past_deadline=now >= deadline,
                once=args.once or closed,
                fail_fast=not args.no_fail_fast,
            )
            # Every action but waiting renders the verdict, and a REVIEWED or
            # STALE review's inline comments are the half a reader most needs.
            # Fetched here, once, rather than every interval while CI holds the
            # loop open.
            if (
                action not in ("wait", "timed_out")
                and verdict.review_id is not None
                and verdict.state in ("REVIEWED", "STALE")
            ):
                verdict.inline = api_paged(
                    f"/repos/{repo}/pulls/{pr_num}/reviews/"
                    f"{verdict.review_id}/comments",
                    tok,
                )
        except Exception as exc:
            if not is_transient(exc):
                raise
            # A Gitea outage longer than api()'s own retries. Skip the round
            # rather than die: a traceback where the verdict should be is the
            # wait ending with nothing decided.
            waited = int(time.monotonic() - started)
            reason = f"{type(exc).__name__}: {exc}"
            # --once (or a closed PR) was never going to wait, so it does not
            # start now.
            if args.once or closed or time.monotonic() >= deadline:
                print(
                    f"\n=== UNREACHABLE — {repo}#{pr_num} @ {head[:8]} "
                    f"(waited {waited}s) ===\nGitea could not be read ({reason}).\n"
                    "No verdict was reached. This is NOT "
                    "a pass — re-run once Gitea answers."
                )
                return EXIT_UNREACHABLE
            print(f"  [{waited}s] could not read Gitea ({reason}); trying again next interval")
            time.sleep(args.interval)
            continue

        waited = int(time.monotonic() - started)
        if action == "wait":
            print(f"  [{waited}s] review={verdict.state}"
                  f"{': ' + verdict.detail if verdict.detail else ''}"
                  f" | ci={ci_verdict.state}"
                  f"{': ' + ci_verdict.detail if ci_verdict.detail else ''}")
            time.sleep(args.interval)
            continue
        if action == "timed_out":
            report_timed_out(repo, pr_num, head, waited, requested, ci_verdict)
            return exit_code(action, verdict.state)

        render(verdict, repo, pr_num, head, waited, args.full)
        render_ci(ci_verdict)
        if epilogue := EPILOGUE.get(action):
            print(epilogue.format(state=verdict.state))
        return exit_code(action, verdict.state)


if __name__ == "__main__":
    # Redirected to a file (this poller is meant to be backgrounded), stdout is
    # block-buffered by default, so the per-interval progress line would not
    # land until the process exits. Line-buffer it so the file shows progress.
    sys.stdout.reconfigure(line_buffering=True)  # pyright: ignore[reportAttributeAccessIssue]
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        sys.exit(f"gitea API {exc.code}: {exc.reason} ({exc.url})")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        # Only reachable before the loop starts -- inside it, an outage skips
        # the round instead.
        sys.exit(f"gitea unreachable ({type(exc).__name__}: {exc}) — no verdict reached")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted — no verdict reached; do not treat this as a pass")
