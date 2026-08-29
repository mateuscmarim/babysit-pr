#!/usr/bin/env python3
"""Wait for gitea.example.com's review-bot to finish reviewing a pull request.

Prints one structured verdict and exits. Never merges, never edits anything.

The bot posts `event: COMMENT` reviews only, so Gitea cannot gate a merge on
it. That makes this poller the only thing standing between "I opened a PR" and
"I merged before the review landed".

The agent now answers for itself. `GET /state/{owner}/{repo}/{pr}?sha=<head>`
reports whether a job for that commit is queued, running, finished, or never
coming -- and when no review is coming, which of worker.py's decline paths it
hit, as a closed enum reason code. This poller consults it every interval and
stops on a terminal answer instead of burning the whole budget.

Precedence is deliberate: Gitea stays the source of truth for anything the bot
actually posted, and the endpoint speaks only where Gitea is silent. An
unreachable endpoint degrades to the Gitea-only behaviour this script had
before it existed -- never to a verdict of its own. Silence from BOTH is still
reported as TIMED_OUT, never as a pass.

It also watches CI for the same head SHA, independently of the review verdict
above -- see CIVerdict / classify_ci. By default it does not assume a workflow
file or job name: it discovers every workflow run at the head SHA and
aggregates every job in them, worst-of (one FAILED or RUNNING job holds the
verdict open even if the rest already passed). Set CI_WORKFLOW_FILE and/or
CI_JOB_NAME to pin it to one workflow/job instead -- necessary evil for a repo
that runs unrelated workflows on every PR and only one of them gates merges.
None of the jobs set `timeout-minutes`, so a hang reports as RUNNING with a
growing elapsed time forever, not as a failure: nasa-agent run 2462
(2026-08-27) sat silent for 28 minutes on three concurrent `uv run`/`uvx`
invocations before finishing on its own, and nothing on the Gitea side would
ever have said so. This poller does not wait for review-bot alone anymore; it
holds the terminal report until CI has an answer too (bounded by the same
--timeout-minutes), so "babysit this PR" cannot hand back a clean REVIEWED
while a job is still quietly running.

Exit codes are unchanged and still driven by the review verdict alone: 0
REVIEWED, 1 usage/transport error, 2 TIMED_OUT, 3 FAILED, 4 SKIPPED, 5 STALE
(--once only), 6 DECLINED. CI status is reported, never folded into these --
the two checks fail in unrelated ways and conflating them would make "what do
you do" ambiguous for both.

A FAILED CI verdict does, however, stop the *wait* (--no-fail-fast keeps the
old behaviour). That is a scheduling decision, not an exit-code one, and the
rule above survives it intact: the code returned is still whatever the review
verdict was, PENDING falling back to 2 exactly as under --once. The
justification is that FAILED is already terminal -- classify_ci checks failed
before running, so a completed non-success job cannot un-fail -- and the fix
you must now push invalidates any review still in flight. On
gitea-review-agent#34 this cost 269s of waiting to report a failure already
visible at 66s. A STALE review's inline findings are fetched before bailing,
so the early return never swallows them.
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
from pathlib import Path

BASE_URL = os.environ.get("GITEA_BASE_URL", "https://gitea.example.com").rstrip("/")
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
TEA_CONFIG = Path.home() / ".config" / "tea" / "config.yml"

# Unset by default -- ci_jobs() then discovers every workflow run at the head
# SHA and reports every job in it, which is what "works across different
# repos" requires: milex-scopeline-server ships tests.yml + eval.yml, not
# quality-gate.yml, and a hardcoded default silently read as CI: NONE for it
# even though both had already passed. Set either to narrow the check to one
# workflow file / job name.
CI_WORKFLOW_FILE = os.environ.get("CI_WORKFLOW_FILE")
CI_JOB_NAME = os.environ.get("CI_JOB_NAME")

# Observed passing durations: mdbin PR #105 ran 6-7m, nasa-agent PR #59 ran
# 11m. Past this many seconds RUNNING gets flagged as possibly hung rather
# than just slow -- the job itself sets no timeout-minutes, so nothing else
# will ever say so.
CI_SLOW_THRESHOLD_S = 15 * 60

# How long a CI verdict of NONE has to hold before it counts as settled --
# see `ci_settled`, which is where the reasoning lives.
#
# Measured from when THIS POLLER started watching the head, deliberately not
# from the head commit's date. A commit's date is when it was authored, which
# can be arbitrarily earlier than when it was pushed: a rebase rewrites it, and
# a branch committed to for an hour and pushed once carries an hour-old date on
# a head that reached the server seconds ago. Dating the grace off the commit
# would read that head as long-settled and put the race straight back.
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
    state: str  # PASSED | FAILED | RUNNING | NONE
    detail: str = ""


def _job_state(job: dict, now: datetime) -> tuple[str, str]:
    """(state, detail) for one job object -- PASSED | FAILED | RUNNING.

    RUNNING covers queued, waiting and in_progress alike -- the reader does
    not need those distinguished, only "not decided yet" vs "decided"."""
    status = job.get("status")
    conclusion = job.get("conclusion")
    if status == "completed":
        if conclusion == "success":
            return "PASSED", "succeeded"
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

    `jobs` is every job in every workflow run at the head SHA, already
    filtered to CI_WORKFLOW_FILE/CI_JOB_NAME by `ci_jobs` when either is set.
    An empty list covers both "no run for this commit yet" and "this repo
    triggered nothing matching" -- the list alone cannot tell those apart,
    and both are exactly "nothing to report", so both read the same way here.

    Worst-of ordering: one FAILED job fails the whole verdict even if every
    other job passed; short of that, one RUNNING job keeps it open even if
    the rest already finished -- a job still in flight is still a reason not
    to merge on the assumption everything passed.
    """
    if not jobs:
        scope = CI_WORKFLOW_FILE or "workflow"
        return CIVerdict(state="NONE", detail=f"no {scope} run found for this commit")
    labeled = [
        (f"{j.get('_workflow', '?')}/{j.get('name', '?')}", *_job_state(j, now))
        for j in jobs
        if isinstance(j, dict)
    ]
    if not labeled:
        scope = CI_WORKFLOW_FILE or "workflow"
        return CIVerdict(state="NONE", detail=f"no {scope} run found for this commit")
    failed = [(label, d) for label, s, d in labeled if s == "FAILED"]
    running = [(label, d) for label, s, d in labeled if s == "RUNNING"]
    if failed:
        return CIVerdict(
            state="FAILED", detail="; ".join(f"{l}: {d}" for l, d in failed)
        )
    if running:
        return CIVerdict(
            state="RUNNING", detail="; ".join(f"{l}: {d}" for l, d in running)
        )
    return CIVerdict(
        state="PASSED", detail="; ".join(f"{l}: {d}" for l, _s, d in labeled)
    )


def ci_settled(ci: CIVerdict, watched_s: float) -> bool:
    """Whether the CI side has reached a state more waiting cannot change.

    RUNNING never has. PASSED and FAILED always have -- a completed job does
    not un-complete. NONE is the ambiguous one, and used to be read as
    terminal outright.

    NONE conflates two things the jobs list cannot tell apart: "this repo
    triggers no workflow matching CI_WORKFLOW_FILE/CI_JOB_NAME", which waiting
    will never change, and "Gitea has not created the run yet", which waiting
    is precisely what changes. Reading it as terminal picks the unsafe half:
    paired with a review that decided fast (agent_state returns DECLINED for a
    filtered-empty diff or nothing_new_since_last_review almost immediately),
    the poller could return inside its first interval reporting CI: NONE for a
    gate that was seconds from starting. That is a false all-clear -- the same
    "silence is not a pass" failure the TIMED_OUT rule exists to prevent.

    So NONE settles only after the poller has watched this head for
    CI_NONE_GRACE_S. The grace is charged only to repos that genuinely have no
    matching workflow, and only when the review decided first: while the
    review is still pending the loop is waiting on it anyway and the grace
    costs nothing.
    """
    if ci.state == "RUNNING":
        return False
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
    2. Otherwise a terminal comment (skip/fail) counts only if it is newer than
       `floor`, which callers set past the head commit's own timestamp and past
       any earlier review. Issue comments carry no SHA, so without that floor a
       stale warning from a previous push would terminate the wait green-adjacent
       and wrongly.
    3. Otherwise the agent's own state endpoint, if it answered. It speaks only
       where Gitea is silent -- anything the bot actually posted outranks the
       agent's memory of what it meant to post -- but it outranks STALE, because
       a re-push declined as `nothing_new_since_last_review` would otherwise sit
       in STALE until the deadline.
    4. A review at a *different* SHA means the bot has spoken before but not
       about what is about to be merged -- STALE, keep waiting. Its findings
       are still carried on the Verdict and still printed: see the STALE
       branch below for why discarding them was a real bug.
    """
    bot_reviews = [r for r in reviews if (r.get("user") or {}).get("login") == bot]

    for r in bot_reviews:
        if r.get("commit_id") == head_sha:
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
        newest = max(bot_reviews, key=lambda r: parse_ts(r.get("submitted_at")))
        # The findings ride along. STALE used to carry only the detail line,
        # and `render` returned early for any non-REVIEWED state, so a review
        # at an older SHA was reported as a single line and its contents were
        # dropped on the floor. That is not an edge case: review-bot routinely
        # posts a review anchored to the head you have ALREADY pushed past --
        # confirmed four times on milex-scopeline-server#14 (reviews 304, 308,
        # 313, 314), each carrying real findings, each found only by
        # enumerating the PR's reviews by hand afterwards.
        #
        # The verdict stays STALE and the exit code stays 5: those describe an
        # older SHA truthfully and must not be laundered into REVIEWED. Only
        # the silence is fixed.
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

    Limitation, stated rather than hidden: a rebased or cherry-picked commit
    keeps its original committer date, so the floor can sit earlier than the
    push. Taking the max with the newest prior review closes the common case;
    the residual risk is a warning comment from the same window being read as
    current. It is reported with its timestamp so a human can catch it."""
    floors = [head_commit_date or EPOCH]
    floors += [
        parse_ts(r.get("submitted_at"))
        for r in reviews
        if (r.get("user") or {}).get("login") == bot
    ]
    return max(floors)


# ------------------------------------------------------------------- transport


def token() -> str:
    if env := os.environ.get("GITEA_TOKEN"):
        return env
    if not TEA_CONFIG.exists():
        sys.exit(f"no token: set GITEA_TOKEN or configure {TEA_CONFIG}")
    for line in TEA_CONFIG.read_text().splitlines():
        if line.strip().startswith("token:"):
            return line.split(":", 1)[1].strip()
    sys.exit(f"no token: found no 'token:' line in {TEA_CONFIG}")


def api(path: str, tok: str):
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1{path}",
        headers={"Authorization": f"token {tok}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


PAGE_LIMIT = 50


def api_paged(path: str, tok: str) -> list:
    """`api`, but walks every page of a list endpoint.

    Gitea paginates list endpoints at 50, **oldest first**, and returns the
    first page silently when you do not ask for one. That is the worst
    possible default here: the reviews that matter are the newest, so a
    single unpaginated fetch returns exactly the ones you do not need and
    hides the ones you do.

    Not hypothetical. `milex-scopeline-server#14` accumulated 78 reviews over
    a long review loop; the unpaginated call saw ids 223-290 and the poller
    reported review 267 as the latest, while 304, 308, 313, 314 and 316 --
    every review from the rounds actually being worked on -- were on page 2
    and invisible. A PR only has to survive 50 reviews for this to start
    quietly lying, and nothing about the output looks wrong when it does.

    Stops on a short or empty page. A page that is not a list (an error
    object) also stops, rather than raising: degrading to what was collected
    beats crashing a poller mid-wait.
    """
    out: list = []
    sep = "&" if "?" in path else "?"
    page = 1
    while True:
        chunk = api(f"{path}{sep}page={page}&limit={PAGE_LIMIT}", tok)
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
    not-yet-deployed agent must degrade to the Gitea-only behaviour this script
    had before the endpoint existed, never turn into a verdict of its own."""
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
    returns every job in every workflow run at this head SHA -- the thing
    that lets this poller work on a repo it has never seen a CI setup for.

    Two calls per run, not one: `/actions/runs?head_sha=` finds candidate
    runs (a single push can trigger more than one workflow file), optionally
    filtered to `workflow_file`; each run's `/jobs` then gets JOB-level
    status, because the run's own status/conclusion is its aggregate across
    every job inside it (e.g. "changes" + "gate"), not one job's alone.

    A run whose jobs call fails is skipped, not fatal to the whole request --
    one broken run should not hide every other run's answer. A totally
    unreachable/malformed `runs` listing returns [] -- same contract as
    agent_state: a question this poller could not ask is a question it did
    not get answered, never a verdict of its own."""
    try:
        runs = api(
            f"/repos/{repo}/actions/runs?head_sha={urllib.parse.quote(sha, safe='')}",
            tok,
        )
    except Exception:  # noqa: BLE001 - unreachable/malformed means "cannot ask"
        return []
    if not isinstance(runs, dict):
        return []
    candidates = [
        r
        for r in (runs.get("workflow_runs") or [])
        if isinstance(r, dict)
        and (workflow_file is None or (r.get("path") or "").split("@", 1)[0] == workflow_file)
    ]
    jobs: list[dict] = []
    for run in candidates:
        try:
            resp = api(f"/repos/{repo}/actions/runs/{run['id']}/jobs", tok)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(resp, dict):
            continue
        workflow = (run.get("path") or "?").split("@", 1)[0].rsplit("/", 1)[-1]
        for j in resp.get("jobs") or []:
            if not isinstance(j, dict):
                continue
            if job_name is not None and j.get("name") != job_name:
                continue
            jobs.append({**j, "_workflow": workflow})
    return jobs


def infer_repo() -> str:
    """owner/name from a gitea.example.com remote in the current checkout."""
    try:
        out = subprocess.run(
            ["git", "remote", "-v"], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("not a git checkout: pass --repo owner/name")
    host = BASE_URL.split("://", 1)[-1]
    for match in re.finditer(rf"{re.escape(host)}[:/]([\w.-]+/[\w.-]+?)(?:\.git)?\s", out):
        return match.group(1)
    sys.exit(f"no {host} remote found: pass --repo owner/name")


def infer_pr(repo: str, tok: str) -> int:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Paged for the same reason the reviews list is: the old `limit=50` here
    # silently gave up on repo #51 onward and reported "no open PR found",
    # which reads as "you have no PR" rather than "I stopped looking".
    for pr in api_paged(f"/repos/{repo}/pulls?state=open", tok):
        if (pr.get("head") or {}).get("ref") == branch:
            return pr["number"]
    sys.exit(f"no open PR found for branch {branch!r}: pass --pr N")


def head_commit_date(repo: str, sha: str, tok: str) -> datetime | None:
    try:
        commit = api(f"/repos/{repo}/git/commits/{sha}", tok)
    except Exception:  # noqa: BLE001 - falling back to the PR's own clock is fine
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


EXIT = {"REVIEWED": 0, "TIMED_OUT": 2, "FAILED": 3, "SKIPPED": 4, "STALE": 5,
        "DECLINED": 6}


def main() -> int:
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
    args = ap.parse_args()

    # Redirected to a file (this poller is meant to be backgrounded), stdout
    # is block-buffered by default, so the per-interval progress line below
    # would not land until the process exits. Line-buffer it explicitly so a
    # `tail -f` on the output file actually shows progress.
    sys.stdout.reconfigure(line_buffering=True)

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
    if pr.get("merged"):
        print("!! this PR is ALREADY MERGED — any verdict below is retrospective")
    if (pr.get("user") or {}).get("login") == BOT:
        # DECLINED, not TIMED_OUT: nothing timed out. parse_event files this as
        # `bot_authored_pr`, and TIMED_OUT now means "still working, or the
        # agent could not be asked" -- which this is not.
        print(f"!! authored by {BOT} — parse_event skips these; no review will come")
        render_ci(classify_ci(ci_jobs(repo, head, tok), datetime.now(timezone.utc)))
        return EXIT["DECLINED"]

    deadline = time.monotonic() + args.timeout_minutes * 60
    started = time.monotonic()
    # Reset on every head move, same as the deadline: the CI grace window asks
    # "has THIS head had time to trigger a run", so a new head restarts it.
    watching_since = time.monotonic()
    # Inline comments keyed by review id. Without this the REVIEWED branch
    # below re-fetches the same comments every interval for as long as CI
    # keeps the loop open -- 30+ wasted round trips on a slow gate. Keyed by
    # id rather than cached on the verdict because `classify` builds a fresh
    # Verdict each pass: a genuinely newer review at the same head gets a new
    # id and is fetched fresh, so this saves calls without freezing anything.
    inline_cache: dict[int, list[dict]] = {}

    while True:
        current = api(f"/repos/{repo}/pulls/{pr_num}", tok)["head"]["sha"]
        if current != head:
            # A push landed mid-wait. A review anchored to the old SHA says
            # nothing about what would now be merged, so restart the clock.
            print(f"\n>> head moved {head[:8]} -> {current[:8]}; restarting the wait")
            head = current
            commit_date = head_commit_date(repo, head, tok)
            deadline = time.monotonic() + args.timeout_minutes * 60
            watching_since = time.monotonic()

        reviews = api_paged(f"/repos/{repo}/pulls/{pr_num}/reviews", tok)
        comments = api_paged(f"/repos/{repo}/issues/{pr_num}/comments", tok)
        floor = signal_floor(commit_date, reviews, BOT)
        # Consulted every interval rather than once: a job can be enqueued,
        # superseded and declined between two polls.
        verdict = classify(
            head, reviews, comments, floor, BOT, agent_state(repo, pr_num, head)
        )

        # Polled every interval, same as the review side: a job that is
        # RUNNING now can finish, or a run that does not exist yet can start,
        # between two checks. Computed before the STALE fetch below because
        # a CI failure is one of the things that can trigger that render.
        ci_verdict = classify_ci(
            ci_jobs(repo, head, tok), datetime.now(timezone.utc)
        )

        # A FAILED CI verdict is already final -- classify_ci checks failed
        # before running, and a completed non-success job cannot un-fail. So
        # once it appears, the remaining wait for review-bot buys nothing: you
        # have to push a fix regardless, and that push makes any review landing
        # in the meantime stale anyway. Observed on gitea-review-agent#34: CI
        # went FAILED at 66s, the poller then waited to 335s to say so.
        ci_failed_early = ci_verdict.state == "FAILED" and not args.no_fail_fast

        # STALE too: its review has inline comments like any other, and they
        # are the half a reader most needs -- an older-SHA finding is usually
        # still live code. Fetched only when the verdict is about to be
        # rendered, since STALE is the loop's waiting state and re-fetching
        # the same comments every 30s would be pure noise. `ci_failed_early`
        # belongs in this list: without it, bailing on a CI failure would drop
        # a STALE review's findings silently -- the previous-head trap, but
        # sprung by the fail-fast path instead of by a bad SHA filter.
        rendering_stale = verdict.state == "STALE" and (
            args.once or ci_failed_early or time.monotonic() >= deadline
        )
        if verdict.review_id is not None and (
            verdict.state == "REVIEWED" or rendering_stale
        ):
            if verdict.review_id not in inline_cache:
                inline_cache[verdict.review_id] = api_paged(
                    f"/repos/{repo}/pulls/{pr_num}/reviews/"
                    f"{verdict.review_id}/comments",
                    tok,
                )
            verdict.inline = inline_cache[verdict.review_id]

        waited = int(time.monotonic() - started)
        review_done = verdict.state in ("REVIEWED", "SKIPPED", "FAILED", "DECLINED")
        ci_done = ci_settled(ci_verdict, time.monotonic() - watching_since)
        if review_done and ci_done:
            render(verdict, repo, pr_num, head, waited, args.full)
            render_ci(ci_verdict)
            return EXIT[verdict.state]

        if args.once:
            render(verdict, repo, pr_num, head, waited, args.full)
            render_ci(ci_verdict)
            return EXIT.get(verdict.state, 2)

        if ci_failed_early:
            # Reached only with the review still undecided -- the both-done
            # branch above already returned otherwise. The exit code stays the
            # review's (PENDING has none, so 2 like the --once path): CI is
            # still reported, never folded into the code. The banner carries
            # the actual instruction, because an exit 2 here would otherwise
            # read as "the agent went silent" when CI failing is the real news.
            render(verdict, repo, pr_num, head, waited, args.full)
            render_ci(ci_verdict)
            print(
                f"\n>> CI FAILED at {waited}s and the review is still "
                f"{verdict.state}. Stopping early:\n   a completed job does "
                "not un-fail, and the fix you push next makes any\n   review "
                "landing meanwhile stale. Fix CI, push, re-run this poller.\n"
                "   The review above (if any) has NOT decided anything — do "
                "not read this as a pass.\n   Pass --no-fail-fast to wait for "
                "review-bot anyway."
            )
            return EXIT.get(verdict.state, 2)

        if time.monotonic() >= deadline and review_done:
            # The review side is already decided; only CI is still open. The
            # full TIMED_OUT block below is about review-bot going silent,
            # which is not what happened here, so it gets its own short report
            # instead of that one.
            render(verdict, repo, pr_num, head, waited, args.full)
            render_ci(ci_verdict)
            print(
                "\nReview finished, but the quality-gate job did not reach a "
                "final state within the timeout budget. Not a failure — but "
                "do not merge on the assumption it passed; check the run "
                "directly."
            )
            return EXIT[verdict.state]

        if time.monotonic() >= deadline and verdict.state == "STALE":
            # Not TIMED_OUT. The block below opens with "No review, no skip
            # notice, no failure notice", and a review demonstrably exists --
            # it just describes an older SHA. Reporting this as TIMED_OUT both
            # says something false and buries findings that are usually still
            # live. STALE (exit 5) is what a review-at-an-older-SHA is.
            render(verdict, repo, pr_num, head, waited, args.full)
            render_ci(ci_verdict)
            print(
                "\nThe review above is real but anchored to an older SHA, so "
                "it is not a verdict on\nwhat you are about to merge. It is "
                "also not nothing: read it. Re-request the bot\nif you need a "
                "review of the current head."
            )
            return EXIT["STALE"]

        if time.monotonic() >= deadline:
            print(f"\n=== TIMED_OUT — {repo}#{pr_num} @ {head[:8]} "
                  f"(waited {waited}s) ===")
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
                print(
                    f"  queue={svc.get('queue')} counters={svc.get('counters')}"
                )
            print(
                "\nNo review, no skip notice, no failure notice.\n"
                "This is NOT an approval. It means one of:\n"
                "  - the agent is still working (it allows up to ~30 min),\n"
                "  - the webhook never arrived — a `deliveries` counter of 0 "
                "above says so, and a nonzero `rejected_signature` means the\n"
                "    WEBHOOK_SECRET is wrong,\n"
                "  - the agent could not be asked at all (see the state line).\n"
                "The decline paths that used to be indistinguishable here — an "
                "empty diff after SKIP_PATHS, nothing new since the last\n"
                "review, a declined event, a job lost to a restart — now report "
                "as DECLINED before the deadline. Reaching this message means\n"
                "it was none of them."
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
            render_ci(ci_verdict)
            return EXIT["TIMED_OUT"]

        print(f"  [{waited}s] review={verdict.state}"
              f"{': ' + verdict.detail if verdict.detail else ''}"
              f" | ci={ci_verdict.state}"
              f"{': ' + ci_verdict.detail if ci_verdict.detail else ''}")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        sys.exit(f"gitea API {exc.code}: {exc.reason} ({exc.url})")
    except KeyboardInterrupt:
        sys.exit("\ninterrupted — no verdict reached; do not treat this as a pass")
