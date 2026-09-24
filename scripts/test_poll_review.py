#!/usr/bin/env python3
"""Tests for poll_review. Run: python3 test_poll_review.py

Plain Python, no pytest -- this has to run anywhere the skill runs. A failing
check is recorded, not raised, and a crashing section is reported and skipped,
so one failure never hides the rest. Exits 1 if anything failed.
"""

import contextlib
import io
import re
import traceback
import types
import urllib.error
from datetime import datetime, timezone

import gitea_auth
import poll_review
from poll_review import (
    CI_NONE_GRACE_S,
    CI_SLOW_THRESHOLD_S,
    CIVerdict,
    EPOCH,
    EXIT,
    classify,
    classify_ci,
    ci_settled,
    decide,
    extract_notes,
    parse_ts,
    signal_floor,
)

HEAD = "a7c5d6c7" + "0" * 32
OLD = "bd27caf1" + "0" * 32
BOT = "review-bot"
NOW = datetime(2026, 8, 27, 22, 0, 0, tzinfo=timezone.utc)

FAILURES: list[str] = []
SECTIONS: list = []


def check(name, got, want):
    if got == want:
        print(f"  ok  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}: expected {want!r}, got {got!r}")


def ok(name, cond, context: object = ""):
    if cond:
        print(f"  ok  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}" + (f"\n{context}" if context else ""))


def section(title):
    def register(fn):
        SECTIONS.append((title, fn))
        return fn

    return register


def ts(hour: int) -> str:
    return datetime(2026, 8, 26, hour, 0, 0, tzinfo=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def review(sha, *, rid=1, at=12, body="## Pull request overview\n\nlooks fine", user=BOT):
    return {
        "id": rid,
        "user": {"login": user},
        "commit_id": sha,
        "submitted_at": ts(at),
        "body": body,
    }


def comment(body, *, at=12, user=BOT):
    return {"user": {"login": user}, "created_at": ts(at), "body": body}


def job(status, conclusion=None, *, jid=42, started=None, name="gate", workflow="quality-gate.yml"):
    d = {"id": jid, "status": status, "name": name, "_workflow": workflow}
    if conclusion is not None:
        d["conclusion"] = conclusion
    if started is not None:
        d["started_at"] = started
    return d


def state(verdict, reason=None, *, retryable=False, sha=None):
    """One /state/{owner}/{repo}/{pr}?sha= response, trimmed to what classify
    reads. outcomes is newest-first and `verdict` is derived from outcomes[0]."""
    outcomes = (
        [{"reason": reason, "retryable": retryable, "outcome": verdict, "sha": sha}]
        if reason
        else []
    )
    return {"verdict": verdict, "live": None, "outcomes": outcomes}


STALE_BODY = (
    "## Pull request overview\n\nTwo correctness defects remain.\n\n"
    "**Additional notes**\n\n- the cost columns exclude failed runs\n"
)
FAILED_WARNING = "⚠️ Automated review failed; see service logs."


def http_error(code, reason="x"):
    return urllib.error.HTTPError("u", code, reason, {}, None)  # pyright: ignore[reportArgumentType]


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class _FakeResponse(io.BytesIO):
    """urlopen's result is used as a context manager; BytesIO is not one."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ------------------------------------------------------------- pure functions


@section("classify")
def _():
    v = classify(HEAD, [review(HEAD)], [], EPOCH, BOT)
    check("review at head -> REVIEWED", v.state, "REVIEWED")
    check("  carries the review id", v.review_id, 1)

    check("no signals -> PENDING", classify(HEAD, [], [], EPOCH, BOT).state, "PENDING")

    v = classify(HEAD, [review(OLD)], [], EPOCH, BOT)
    check("review at other sha -> STALE", v.state, "STALE")
    ok("STALE detail names both SHAs", OLD[:8] in v.detail and HEAD[:8] in v.detail)

    # A STALE verdict must carry the review's contents, not just a detail line:
    # review-bot routinely anchors a review to the head you already pushed past.
    v = classify(HEAD, [review(OLD, rid=314, body=STALE_BODY)], [], EPOCH, BOT)
    check("  STALE carries the review id", v.review_id, 314)
    check("  STALE carries the overview", v.overview, STALE_BODY)
    check("  STALE carries the unanchored notes", v.notes, extract_notes(STALE_BODY))
    check("  STALE names the sha it reviewed", v.reviewed_sha, OLD)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        poll_review.render(v, "o/r", 14, HEAD, 30)
    out = buf.getvalue()
    ok("render prints a STALE review's overview and notes",
       "Two correctness defects remain" in out and "cost columns exclude failed runs" in out, out)
    ok("render says plainly which SHA those findings describe",
       OLD[:8] in out and "NOT the head" in out, out)
    ok("render does not launder STALE into REVIEWED", "REVIEWED" not in out, out)
    check("  STALE still exits 5", EXIT["STALE"], 5)

    # Two reviews at the same head -- a re-requested review of one commit. Gitea
    # lists reviews oldest first; the first-found one is the superseded review.
    v = classify(
        HEAD,
        [review(HEAD, rid=10, at=9, body="old: clean"),
         review(HEAD, rid=11, at=12, body="new: two defects")],
        [], EPOCH, BOT,
    )
    check("two reviews at head -> the newest wins", v.review_id, 11)
    check("  ...with its own overview", v.overview, "new: two defects")
    v = classify(
        HEAD,
        [review(HEAD, rid=20, at=12), review(HEAD, rid=21, at=12)],
        [], EPOCH, BOT,
    )
    check("  a same-second tie goes to the higher id", v.review_id, 21)
    v = classify(HEAD, [review(OLD, rid=7, at=14), review(OLD, rid=8, at=10)], [], EPOCH, BOT)
    check("STALE picks the newest review by time, not list order", v.review_id, 7)

    check(
        "human review ignored",
        classify(HEAD, [review(HEAD, user="someone")], [], EPOCH, BOT).state,
        "PENDING",
    )

    check(
        "diff-too-large -> SKIPPED",
        classify(
            HEAD, [], [comment("ℹ️ This PR's diff is too large to review automatically (9000 lines > 4000 limit).")], EPOCH, BOT
        ).state,
        "SKIPPED",
    )
    for variant, body in [
        ("credentials", "⚠️ The reviewer's credentials are not working, so this pull request was not reviewed."),
        ("quota", "⚠️ The reviewer has run out of model quota, so this pull request was not reviewed."),
        ("generic", FAILED_WARNING),
        ("oversized", "⚠️ This pull request is too large for the reviewer to accept, so it was not reviewed."),
    ]:
        v = classify(HEAD, [], [comment(body)], EPOCH, BOT)
        check(f"{variant} warning -> FAILED", v.state, "FAILED")
        ok(f"  {variant} detail names the variant", v.detail.startswith(f"[{variant}]"), v.detail)

    # The floor guard: a warning that predates the current head commit is about
    # an earlier push and must not terminate this wait.
    stale_warning = [comment(FAILED_WARNING, at=9)]
    check("warning below floor ignored",
          classify(HEAD, [], stale_warning, parse_ts(ts(11)), BOT).state, "PENDING")
    check("warning above floor honoured",
          classify(HEAD, [], stale_warning, parse_ts(ts(8)), BOT).state, "FAILED")
    check(
        "review at head beats later warning",
        classify(HEAD, [review(HEAD, at=12)], [comment(FAILED_WARNING, at=13)], EPOCH, BOT).state,
        "REVIEWED",
    )


@section("extract_notes")
def _():
    body_with_notes = (
        "## Pull request overview\n\nStuff.\n\n---\n## Reviewed changes\n\n"
        "table\n\n---\n**Additional notes** (could not be anchored to changed lines):\n"
        "- `src/a.py:10` — leaks a handle\n"
        "- `src/b.py:22` — off-by-one\n"
        "\n---\n_footer_"
    )
    notes = extract_notes(body_with_notes)
    check("finds both unanchored findings", len(notes), 2)
    check("first note text", notes[0], "`src/a.py:10` — leaks a handle")
    check("no section -> empty", extract_notes("## Pull request overview\n\nclean"), [])
    # The whole point: zero inline comments does not mean zero findings.
    v = classify(HEAD, [review(HEAD, body=body_with_notes)], [], EPOCH, BOT)
    check("REVIEWED with 0 inline still surfaces notes", len(v.notes), 2)


@section("signal_floor")
def _():
    check("no data -> epoch", signal_floor(None, [], BOT), EPOCH)
    check("takes the head commit date", signal_floor(parse_ts(ts(10)), [], BOT), parse_ts(ts(10)))
    check("a newer prior review raises the floor",
          signal_floor(parse_ts(ts(10)), [review(OLD, at=14)], BOT), parse_ts(ts(14)))
    check("a human review does not raise it",
          signal_floor(parse_ts(ts(10)), [review(OLD, at=14, user="someone")], BOT), parse_ts(ts(10)))


@section("classify_ci")
def _():
    check("no jobs -> NONE", classify_ci([], NOW).state, "NONE")
    malformed: list = ["not a dict", None]
    check("malformed entries -> NONE, not a crash", classify_ci(malformed, NOW).state, "NONE")
    check("completed success -> PASSED", classify_ci([job("completed", "success")], NOW).state, "PASSED")

    v = classify_ci([job("completed", "failure")], NOW)
    check("completed failure -> FAILED", v.state, "FAILED")
    ok("  FAILED detail names the conclusion", "failure" in v.detail, v.detail)

    fresh = NOW.isoformat().replace("+00:00", "Z")
    v = classify_ci([job("in_progress", started=fresh)], NOW)
    check("in_progress, just started -> RUNNING", v.state, "RUNNING")
    ok("  fresh run not flagged as possibly hung", "hung" not in v.detail, v.detail)

    overdue = datetime.fromtimestamp(
        NOW.timestamp() - CI_SLOW_THRESHOLD_S - 60, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    v = classify_ci([job("in_progress", started=overdue)], NOW)
    check("running past the slow threshold -> still RUNNING", v.state, "RUNNING")
    ok("  overdue run flagged as possibly hung", "may be hung" in v.detail, v.detail)

    v = classify_ci([job("completed", "success", name="tests"), job("completed", "failure", name="eval")], NOW)
    check("one failed among several -> FAILED", v.state, "FAILED")
    ok("  FAILED detail names only the failing job", "eval" in v.detail and "tests" not in v.detail, v.detail)

    check("one running among others passed -> RUNNING",
          classify_ci([job("completed", "success", name="tests"), job("in_progress", name="eval")], NOW).state,
          "RUNNING")
    check("a failed job outranks a still-running one",
          classify_ci([job("completed", "failure", name="a"), job("in_progress", name="b")], NOW).state,
          "FAILED")

    v = classify_ci(
        [job("completed", "success", name="tests", workflow="tests.yml"),
         job("completed", "success", name="eval", workflow="eval.yml")],
        NOW,
    )
    check("multiple workflows, all passed -> PASSED", v.state, "PASSED")
    ok("  PASSED detail names every job across every workflow",
       "tests.yml/tests" in v.detail and "eval.yml/eval" in v.detail, v.detail)

    # A skipped job is a designed outcome, not a failure.
    v = classify_ci(
        [job("completed", "success", name="gate"), job("completed", "success", name="report"),
         job("completed", "skipped", name="promote")],
        NOW,
    )
    check("a skipped job among passing ones -> PASSED", v.state, "PASSED")
    ok("  skipped job named but not gating", "promote" in v.detail and "not gating" in v.detail, v.detail)
    check("'neutral' is not a failure either",
          classify_ci([job("completed", "success", name="a"), job("completed", "neutral", name="b")], NOW).state,
          "PASSED")
    check("a skipped job does not rescue a real failure",
          classify_ci([job("completed", "skipped", name="p"), job("completed", "failure", name="g")], NOW).state,
          "FAILED")
    check("a skipped job does not settle a still-running one",
          classify_ci([job("completed", "skipped", name="p"), job("in_progress", name="g")], NOW).state,
          "RUNNING")
    check("'cancelled' stays FAILED -- an aborted job decided nothing",
          classify_ci([job("completed", "cancelled")], NOW).state, "FAILED")
    v = classify_ci([job("completed", "skipped", name="a"), job("completed", "skipped", name="b")], NOW)
    check("every job skipped -> NONE, not PASSED", v.state, "NONE")
    ok("  all-skipped detail explains itself", "skipped" in v.detail, v.detail)

    # An unreadable run must not let the rest read as a pass -- it could be the
    # failing one -- but it must not outrank a failure or a running job either.
    unreadable = {"name": "run 9", "_workflow": "eval.yml", "_error": "TimeoutError: x"}
    v = classify_ci([job("completed", "success"), unreadable], NOW)
    check("an unreadable run among passing ones -> UNKNOWN", v.state, "UNKNOWN")
    ok("  UNKNOWN detail says why", "could not be read" in v.detail, v.detail)
    check("  a failure still outranks it",
          classify_ci([job("completed", "failure"), unreadable], NOW).state, "FAILED")
    check("  a running job still outranks it",
          classify_ci([job("in_progress"), unreadable], NOW).state, "RUNNING")
    check("  a transport UNKNOWN is not permanent",
          classify_ci([unreadable], NOW).permanent, False)


@section("ci_settled")
def _():
    check("RUNNING -> not settled", ci_settled(CIVerdict("RUNNING"), 0), False)
    check("RUNNING stays unsettled past the grace", ci_settled(CIVerdict("RUNNING"), CI_NONE_GRACE_S * 10), False)
    check("PASSED -> settled immediately", ci_settled(CIVerdict("PASSED"), 0), True)
    check("FAILED -> settled immediately", ci_settled(CIVerdict("FAILED"), 0), True)
    check("NONE inside grace -> not settled", ci_settled(CIVerdict("NONE"), 0), False)
    check("NONE just short of the grace -> not settled", ci_settled(CIVerdict("NONE"), CI_NONE_GRACE_S - 1), False)
    check("NONE at the grace -> settled", ci_settled(CIVerdict("NONE"), CI_NONE_GRACE_S), True)
    check("transient UNKNOWN -> never settles", ci_settled(CIVerdict("UNKNOWN"), CI_NONE_GRACE_S * 10), False)
    check("permanent UNKNOWN -> settles at once", ci_settled(CIVerdict("UNKNOWN", permanent=True), 0), True)
    # A grace shorter than one poll interval would be no grace at all.
    ok("grace outlasts the default 30s poll interval", CI_NONE_GRACE_S >= 60)


@section("decide")
def _():
    PASSED, FAILED, RUNNING, NONE = (CIVerdict(s) for s in ("PASSED", "FAILED", "RUNNING", "NONE"))

    def d(review_state, ci, *, watched=0, past=False, once=False, fail_fast=True):
        return decide(review_state, ci, watched_s=watched, past_deadline=past,
                      once=once, fail_fast=fail_fast)

    for terminal in poll_review.REVIEW_DONE:
        ok(f"{terminal} has an exit code", terminal in EXIT)
        check(f"{terminal} + CI settled -> done", d(terminal, PASSED), "done")
    check("PENDING -> wait", d("PENDING", PASSED), "wait")
    check("STALE is not done -> wait", d("STALE", PASSED), "wait")
    check("REVIEWED + CI running -> wait", d("REVIEWED", RUNNING), "wait")
    check("REVIEWED + fresh NONE -> wait (grace)", d("REVIEWED", NONE), "wait")
    check("REVIEWED + NONE past grace -> done", d("REVIEWED", NONE, watched=CI_NONE_GRACE_S), "done")
    check("--once reports undecided", d("PENDING", RUNNING, once=True), "once")
    check("CI failed, review pending -> ci_failed", d("PENDING", FAILED), "ci_failed")
    check("  ...not with --no-fail-fast", d("PENDING", FAILED, fail_fast=False), "wait")
    check("REVIEWED + CI failed -> done, not ci_failed", d("REVIEWED", FAILED), "done")
    check("deadline, review decided, CI open -> ci_open", d("REVIEWED", RUNNING, past=True), "ci_open")
    check("deadline, STALE -> stale", d("STALE", PASSED, past=True), "stale")
    check("deadline, nothing -> timed_out", d("PENDING", PASSED, past=True), "timed_out")

    check("exit: done uses the review's code", poll_review.exit_code("done", "FAILED"), 3)
    check("exit: PENDING falls back to 2", poll_review.exit_code("once", "PENDING"), 2)
    check("exit: ci_failed keeps a STALE review's 5", poll_review.exit_code("ci_failed", "STALE"), 5)
    check("exit: stale -> 5", poll_review.exit_code("stale", "STALE"), 5)
    check("exit: timed_out -> 2", poll_review.exit_code("timed_out", "PENDING"), 2)


@section("agent state")
def _():
    # Gitea stays the source of truth for review CONTENT.
    check("review at head beats a terminal agent verdict",
          classify(HEAD, [review(HEAD)], [], EPOCH, BOT, state("declined", "superseded")).state,
          "REVIEWED")
    check("posted warning beats a terminal agent verdict",
          classify(HEAD, [], [comment(FAILED_WARNING)], EPOCH, BOT,
                   state("declined", "nothing_new_since_last_review")).state,
          "FAILED")

    v = classify(HEAD, [], [], EPOCH, BOT, state("declined", "nothing_new_since_last_review"))
    check("agent decline -> DECLINED", v.state, "DECLINED")
    ok("  DECLINED detail names the reason", "nothing_new_since_last_review" in v.detail, v.detail)
    check("agent decline outranks STALE",
          classify(HEAD, [review(OLD)], [], EPOCH, BOT,
                   state("declined", "nothing_new_since_last_review")).state,
          "DECLINED")

    # `retryable` is advice about whether a fresh request could work, not a
    # claim that an attempt is in flight.
    v = classify(HEAD, [], [], EPOCH, BOT, state("failed", "backend_error", retryable=True))
    check("retryable agent failure still ends the wait", v.state, "FAILED")
    ok("  retryable detail says a new request may work", "fresh review request" in v.detail, v.detail)
    check("permanent agent failure -> FAILED",
          classify(HEAD, [], [], EPOCH, BOT, state("failed", "backend_quota")).state, "FAILED")
    check("a size decline reports as SKIPPED",
          classify(HEAD, [], [], EPOCH, BOT, state("declined", "diff_too_many_lines")).state, "SKIPPED")
    check("a lost job -> DECLINED",
          classify(HEAD, [], [], EPOCH, BOT, state("lost", "lost_on_restart")).state, "DECLINED")
    for verdict_name in ("in_progress", "posted", "unknown"):
        check(f"{verdict_name} keeps waiting",
              classify(HEAD, [], [], EPOCH, BOT, state(verdict_name)).state, "PENDING")
    check("no agent state -> Gitea-only behaviour",
          classify(HEAD, [], [], EPOCH, BOT, None).state, "PENDING")
    check("DECLINED has its own exit code", EXIT["DECLINED"], 6)

    # A response this poller cannot read is a response it did not get.
    for label, bad in [
        ("outcomes is a string", {"verdict": "declined", "outcomes": "nothing_new"}),
        ("outcomes is an object", {"verdict": "declined", "outcomes": {"reason": "x"}}),
        ("outcomes holds null", {"verdict": "declined", "outcomes": [None]}),
        ("outcomes holds a string", {"verdict": "declined", "outcomes": ["nothing_new"]}),
        ("verdict is a list", {"verdict": ["declined"]}),
        ("reason is null", {"verdict": "declined", "outcomes": [{"reason": None}]}),
        ("reason is empty", {"verdict": "declined", "outcomes": [{"reason": ""}]}),
        ("outcomes is empty", {"verdict": "declined", "outcomes": []}),
        ("outcomes is missing", {"verdict": "declined"}),
        ("body is empty", {}),
    ]:
        check(f"{label} -> keeps waiting", classify(HEAD, [], [], EPOCH, BOT, bad).state, "PENDING")

    check("outcome for another sha -> keeps waiting",
          classify(HEAD, [], [], EPOCH, BOT, state("declined", "superseded", sha=OLD)).state, "PENDING")
    check("outcome for this sha answers",
          classify(HEAD, [], [], EPOCH, BOT, state("declined", "superseded", sha=HEAD)).state, "DECLINED")
    check("outcome with no sha still answers",
          classify(HEAD, [], [], EPOCH, BOT, state("declined", "bot_authored_pr")).state, "DECLINED")


# ------------------------------------------------------------------ transport


def with_api(fake, fn):
    with patched(poll_review, api=fake):
        return fn()


def runs_then_jobs(runs, jobs_by_run):
    """A fake api() for ci_jobs: `runs` is the workflow_runs list, and
    `jobs_by_run` maps run id -> a jobs list, or an exception to raise."""
    def fake(path, tok):
        if "actions/runs?" in path:
            return {"workflow_runs": runs if "page=1&" in path else []}
        m = re.search(r"/runs/(\d+)/jobs", path)
        if m:
            got = jobs_by_run[int(m.group(1))]
            if isinstance(got, Exception):
                raise got
            return {"jobs": got if "page=1&" in path else []}
        raise AssertionError(f"unexpected path {path}")

    return fake


def ci_jobs(fake, **kwargs):
    return with_api(fake, lambda: poll_review.ci_jobs("owner/name", HEAD, "tok", **kwargs))


@section("ci_jobs")
def _():
    check("no workflow_runs at all -> []", ci_jobs(runs_then_jobs([], {})), [])
    check(
        "a run for a different workflow file, filter set -> []",
        ci_jobs(runs_then_jobs([{"id": 1, "path": "ci.yml@refs/heads/main"}], {}),
                workflow_file="quality-gate.yml"),
        [],
    )
    matching = [{"id": 7, "path": "quality-gate.yml@refs/pull/1/head"}]
    gate = {"id": 200, "name": "gate", "status": "in_progress"}
    both = {7: [{"id": 100, "name": "changes"}, gate]}
    check("matching run but no 'gate' job, filter set -> []",
          ci_jobs(runs_then_jobs(matching, {7: [{"id": 100, "name": "changes"}]}),
                  workflow_file="quality-gate.yml", job_name="gate"),
          [])
    check("matching run, gate job found, filter set -> that job, tagged",
          ci_jobs(runs_then_jobs(matching, both), workflow_file="quality-gate.yml", job_name="gate"),
          [{**gate, "_workflow": "quality-gate.yml"}])
    check("no filters -> every job in the run",
          ci_jobs(runs_then_jobs(matching, both)),
          [{"id": 100, "name": "changes", "_workflow": "quality-gate.yml"},
           {**gate, "_workflow": "quality-gate.yml"}])

    two = [{"id": 5, "path": "tests.yml@refs/pull/1/head"}, {"id": 9, "path": "eval.yml@refs/pull/1/head"}]
    passed = lambda name, jid: {"id": jid, "name": name, "status": "completed", "conclusion": "success"}
    check("two workflow files at head -> jobs from both, tagged",
          ci_jobs(runs_then_jobs(two, {5: [passed("test", 50)], 9: [passed("eval", 90)]})),
          [{**passed("test", 50), "_workflow": "tests.yml"}, {**passed("eval", 90), "_workflow": "eval.yml"}])

    got = ci_jobs(runs_then_jobs(two, {5: OSError("boom"), 9: [passed("eval", 90)]}))
    check("one run's jobs call fails -> the other run's jobs still come back",
          got[1], {**passed("eval", 90), "_workflow": "eval.yml"})
    check("  ...and the failed run is reported, not dropped",
          (got[0]["_workflow"], "boom" in got[0]["_error"]), ("tests.yml", True))
    check("  ...so the whole verdict is UNKNOWN, not PASSED", classify_ci(got, NOW).state, "UNKNOWN")

    # A superseded run of the same workflow at the same SHA -- cancelled by a
    # concurrency group -- must not outvote the newer run that passed.
    superseded = [
        {"id": 2, "path": "ci.yml@x", "event": "pull_request"},
        {"id": 1, "path": "ci.yml@x", "event": "pull_request"},
    ]
    got = ci_jobs(runs_then_jobs(superseded, {
        1: [{"name": "test", "status": "completed", "conclusion": "cancelled"}],
        2: [{"name": "test", "status": "completed", "conclusion": "success"}],
    }))
    check("only the newest run per workflow counts", [j["conclusion"] for j in got], ["success"])
    check("  ...so a superseded cancel does not fail CI", classify_ci(got, NOW).state, "PASSED")

    # ...but a push run and a pull_request run both tested the commit.
    got = ci_jobs(runs_then_jobs(
        [{"id": 3, "path": "ci.yml@x", "event": "push"}, {"id": 4, "path": "ci.yml@x", "event": "pull_request"}],
        {3: [{"name": "t", "status": "completed", "conclusion": "failure"}],
         4: [{"name": "t", "status": "completed", "conclusion": "success"}]},
    ))
    check("different events at the same SHA both count", classify_ci(got, NOW).state, "FAILED")

    # Jobs are paged: a matrix build can exceed one page.
    many = [{"id": i, "name": f"m{i}", "status": "completed", "conclusion": "success"} for i in range(51)]

    def paged_jobs(path, tok):
        if "actions/runs?" in path:
            return {"workflow_runs": matching if "page=1&" in path else []}
        page = int(path.split("page=")[1].split("&")[0])
        return {"jobs": many[(page - 1) * 50: page * 50]}

    check("jobs beyond the first page are read", len(ci_jobs(paged_jobs)), 51)


@section("read_ci")
def _():
    def raising(exc):
        def fake(path, tok):
            raise exc
        return fake

    def read(fake):
        return with_api(fake, lambda: poll_review.read_ci("o/r", HEAD, "t", NOW))

    # The bug this exists for: an unreadable Actions API used to read as NONE,
    # "no workflow run found", and settle after the grace -- exit 0 on a CI
    # nobody looked at.
    v = read(raising(http_error(403, "Forbidden")))
    check("a 403 -> UNKNOWN, not NONE", v.state, "UNKNOWN")
    ok("  says plainly it is not 'no CI'", "NOT 'no CI'" in v.detail, v.detail)
    check("  a 4xx is permanent, so it settles", ci_settled(v, 0), True)
    v = read(raising(http_error(502, "Bad Gateway")))
    check("a 5xx -> UNKNOWN", v.state, "UNKNOWN")
    check("  ...transient, so it holds the wait", ci_settled(v, CI_NONE_GRACE_S * 10), False)
    v = read(raising(TimeoutError("slow")))
    check("a timeout -> UNKNOWN, transient", (v.state, v.permanent), ("UNKNOWN", False))
    check("an answered empty listing is still NONE",
          read(runs_then_jobs([], {})).state, "NONE")


@section("api_paged")
def _():
    calls: list[str] = []

    def fake_api(pages):
        def _api(path, tok):
            calls.append(path)
            n = int(path.split("page=")[1].split("&")[0])
            return pages[n - 1] if n <= len(pages) else []
        return _api

    with patched(poll_review, api=fake_api([[{"id": i} for i in range(50)], [{"id": 50}]])):
        check("walks past the first full page", len(poll_review.api_paged("/x", "t")), 51)
    check("  stops on the short page", len(calls), 2)
    ok("  asks for page 1 then page 2", "page=1&limit=50" in calls[0] and "page=2" in calls[1], calls)

    calls.clear()
    with patched(poll_review, api=fake_api([[{"id": i} for i in range(50)]])):
        check("empty page ends the walk", len(poll_review.api_paged("/x", "t")), 50)
    check("  ...and costs one extra call", len(calls), 2)

    calls.clear()
    with patched(poll_review, api=fake_api([[{"id": 1}]])):
        poll_review.api_paged("/x?state=open", "t")
    check("appends with & when the path already has a query", calls[0], "/x?state=open&page=1&limit=50")

    with patched(poll_review, api=lambda path, tok: {"message": "nope"}):
        check("a non-list page degrades to []", poll_review.api_paged("/x", "t"), [])
    with patched(poll_review, api=lambda path, tok: {"jobs": [{"id": 1}]}):
        check("key= unwraps an object response", poll_review.api_paged("/x", "t", key="jobs"), [{"id": 1}])


def with_urlopen(fake, fn):
    with patched(poll_review.urllib.request, urlopen=fake):
        return fn()


def returns(body: bytes):
    return lambda req, timeout=None: _FakeResponse(body)


def raises(exc):
    def fake(req, timeout=None):
        raise exc
    return fake


@section("agent_state transport")
def _():
    get = lambda: poll_review.agent_state("owner/name", 4, HEAD)
    check("a JSON object comes back as a dict", with_urlopen(returns(b'{"verdict":"unknown"}'), get),
          {"verdict": "unknown"})
    for label, fake in [
        ("connection refused", raises(OSError("refused"))),
        ("401", raises(http_error(401, "Unauthorized"))),
        ("500", raises(http_error(500, "Server Error"))),
        ("timeout", raises(TimeoutError())),
        ("non-JSON body", returns(b"<html>nope</html>")),
        ("a JSON list", returns(b"[1,2,3]")),
        ("a JSON string", returns(b'"nope"')),
    ]:
        check(f"{label} -> None", with_urlopen(fake, get), None)

    seen: list[str] = []

    def capture(req, timeout=None):
        seen.append(req.full_url)
        return _FakeResponse(b"{}")

    with_urlopen(capture, get)
    ok("builds /state/{owner}/{repo}/{pr}?sha=", seen[0].endswith(f"/state/owner/name/4?sha={HEAD}"), seen)


@section("api retries")
def _():
    def run(fake):
        sleeps: list[float] = []
        clock = types.SimpleNamespace(sleep=sleeps.append, monotonic=lambda: 0.0)
        with patched(poll_review.urllib.request, urlopen=fake), patched(poll_review, time=clock):
            try:
                return poll_review.api("/x", "t"), sleeps
            except Exception as exc:
                return exc, sleeps

    def flaky(failures, then):
        queue = list(failures)

        def fake(req, timeout=None):
            if queue:
                raise queue.pop(0)
            return _FakeResponse(then)
        return fake

    got, sleeps = run(flaky([TimeoutError()], b'{"ok":1}'))
    check("a timeout is retried and the next read answers", got, {"ok": 1})
    check("  backed off once before it", sleeps, [poll_review.API_RETRY_BACKOFF])
    got, _ = run(flaky([http_error(502)], b"[]"))
    check("a 5xx is retried", got, [])
    got, sleeps = run(flaky([urllib.error.URLError("refused")] * 5, b"[]"))
    ok("gives up after API_RETRIES with the last error", isinstance(got, urllib.error.URLError), repr(got))
    check("  backoff grows with the attempt", sleeps, [2.0, 4.0])
    got, sleeps = run(flaky([http_error(401)], b"[]"))
    ok("a 401 is raised at once", isinstance(got, urllib.error.HTTPError) and got.code == 401, repr(got))
    check("  ...not retried", sleeps, [])


@section("is_transient")
def _():
    check("5xx", poll_review.is_transient(http_error(503)), True)
    check("4xx is an answer, even though HTTPError is a URLError", poll_review.is_transient(http_error(404)), False)
    check("URLError", poll_review.is_transient(urllib.error.URLError("x")), True)
    check("TimeoutError", poll_review.is_transient(TimeoutError()), True)
    check("KeyError is a bug, not an outage", poll_review.is_transient(KeyError("head")), False)


@section("repo_from_remotes")
def _():
    base = "https://git.example.com"
    for label, remote in [
        ("https", "https://git.example.com/owner/repo.git"),
        ("https without .git", "https://git.example.com/owner/repo"),
        ("scp-style ssh", "git@git.example.com:owner/repo.git"),
        ("ssh with a port", "ssh://git@git.example.com:2222/owner/repo.git"),
        ("https with a port", "https://git.example.com:3000/owner/repo.git"),
    ]:
        check(label, poll_review.repo_from_remotes(f"origin\t{remote} (fetch)\n", base), "owner/repo")
    check("base url with a port still matches by host",
          poll_review.repo_from_remotes("origin\tgit@git.example.com:owner/repo.git (fetch)\n",
                                        "https://git.example.com:3000"),
          "owner/repo")
    check("another host -> None",
          poll_review.repo_from_remotes("origin\thttps://github.com/owner/repo.git (fetch)\n", base), None)


@section("tea_token")
def _():
    config = (
        "logins:\n"
        "    - name: other\n"
        "      url: https://other.example.com\n"
        "      token: OTHER\n"
        "      default: true\n"
        "    - name: mine\n"
        "      url: \"https://git.example.com/\"\n"
        "      token: 'MINE'\n"
        "preferences:\n"
        "    editor: false\n"
    )
    check("picks the login for this host, not the first one",
          gitea_auth.tea_token(config, "https://git.example.com"), "MINE")
    check("no login for this host -> None, never another host's token",
          gitea_auth.tea_token(config, "https://third.example.com"), None)
    check("a single matching login", gitea_auth.tea_token(
        "logins:\n    - name: a\n      url: https://git.example.com\n      token: T\n",
        "https://git.example.com"), "T")


# ----------------------------------------------------------------- main loop


class FakeGitea:
    """Answers poll_review.api by path. Each callable takes the poll round `n`,
    which counts single-PR fetches: 1 is main's initial fetch, 2 the first loop
    round. `fail(path, n)` may return an exception to raise instead."""

    def __init__(self, *, head=None, pr=None, reviews=None, comments=None,
                 runs=None, jobs=None, inline=None, fail=None, author="someone"):
        self.head = head or (lambda n: HEAD)
        self.pr = pr or (lambda n: {})
        self.reviews = reviews or (lambda n: [])
        self.comments = comments or (lambda n: [])
        self.runs = runs or (lambda n: [{"id": 1, "path": "ci.yml@x", "event": "pull_request"}])
        self.jobs = jobs or (lambda n: [{"name": "test", "status": "completed", "conclusion": "success"}])
        self.inline = inline or (lambda rid: [])
        self.fail = fail or (lambda path, n: None)
        self.author = author
        self.n = 0
        self.inline_calls = 0

    def __call__(self, path, tok):
        base = path.split("?")[0]
        if base == "/repos/o/r/pulls/7":
            self.n += 1
        if (exc := self.fail(base, self.n)) is not None:
            raise exc
        first_page = "page=" not in path or "page=1&" in path
        if base == "/repos/o/r/pulls/7":
            return {"head": {"sha": self.head(self.n)}, "user": {"login": self.author},
                    "requested_reviewers": [], "state": "open", **self.pr(self.n)}
        if base == "/repos/o/r/pulls/7/reviews":
            return self.reviews(self.n) if first_page else []
        if m := re.fullmatch(r"/repos/o/r/pulls/7/reviews/(\d+)/comments", base):
            self.inline_calls += 1
            return self.inline(int(m.group(1))) if first_page else []
        if base == "/repos/o/r/issues/7/comments":
            return self.comments(self.n) if first_page else []
        if base.startswith("/repos/o/r/git/commits/"):
            return {"commit": {"committer": {"date": ts(1)}}}
        if base == "/repos/o/r/actions/runs":
            return {"workflow_runs": self.runs(self.n) if first_page else []}
        if re.fullmatch(r"/repos/o/r/actions/runs/\d+/jobs", base):
            return {"jobs": self.jobs(self.n) if first_page else []}
        raise AssertionError(f"unexpected path {path}")


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def run_main(gitea, *args, agent=None):
    """main() against a fake Gitea and clock: (exit code, stdout, clock)."""
    clock = FakeClock()
    buf = io.StringIO()
    with patched(poll_review, api=gitea, time=clock, token=lambda: "t",
                 agent_state=lambda repo, pr, sha: agent, health=lambda: "up (test)"), \
            contextlib.redirect_stdout(buf):
        code = poll_review.main(["--repo", "o/r", "--pr", "7",
                                 "--timeout-minutes", "5", "--interval", "30", *args])
    return code, buf.getvalue(), clock


RUNNING_JOB = [{"name": "test", "status": "in_progress"}]
FAILED_JOB = [{"name": "test", "status": "completed", "conclusion": "failure"}]


@section("main: settling")
def _():
    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD, rid=5)]))
    check("review at head + CI passed -> exit 0", code, 0)
    ok("  reports both blocks", "=== REVIEWED" in out and "CI: PASSED" in out, out)
    check("  without waiting", clock.sleeps, [])

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD)] if n >= 4 else []))
    check("review lands on the third round -> exit 0", code, 0)
    check("  after two intervals", len(clock.sleeps), 2)

    gitea = FakeGitea(
        reviews=lambda n: [review(HEAD, rid=5)],
        jobs=lambda n: RUNNING_JOB if n < 5 else [{"name": "test", "status": "completed", "conclusion": "success"}],
        inline=lambda rid: [{"id": 70, "path": "a.py", "position": 3, "body": "leaks", "resolver": None}],
    )
    code, out, clock = run_main(gitea)
    check("REVIEWED holds open while CI runs, then exits 0", code, 0)
    check("  inline comments fetched once, not every interval", gitea.inline_calls, 1)
    ok("  and printed with their thread id", "[#70 open] a.py:3" in out, out)

    code, out, clock = run_main(
        FakeGitea(head=lambda n: OLD if n <= 2 else HEAD,
                  reviews=lambda n: [review(HEAD)] if n >= 3 else []))
    check("head moves mid-wait -> the new head's review answers", code, 0)
    ok("  says so", ">> head moved" in out and f"@ {HEAD[:8]}" in out, out)


@section("main: CI")
def _():
    # The NONE grace: an instant DECLINED next to a run Gitea has not created yet.
    code, out, clock = run_main(FakeGitea(runs=lambda n: []),
                                agent=state("declined", "nothing_new_since_last_review"))
    check("DECLINED + no CI run -> exit 6", code, 6)
    ok("  but only after the grace window", sum(clock.sleeps) >= CI_NONE_GRACE_S, clock.sleeps)

    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD)],
        fail=lambda path, n: http_error(403, "Forbidden") if path == "/repos/o/r/actions/runs" else None))
    check("unreadable Actions API (403) + clean review -> review's exit 0", code, 0)
    ok("  CI is UNKNOWN, not NONE", "CI: UNKNOWN" in out and "NOT 'no CI'" in out, out)

    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD)],
        fail=lambda path, n: http_error(502) if path == "/repos/o/r/actions/runs" and n < 4 else None))
    check("a transient CI API error holds the wait until it clears", (code, len(clock.sleeps)), (0, 2))
    ok("  and ends PASSED", "CI: PASSED" in out, out)

    gitea = FakeGitea(
        reviews=lambda n: [review(OLD, rid=9, body=STALE_BODY)],
        jobs=lambda n: FAILED_JOB,
        inline=lambda rid: [{"id": 77, "path": "a.py", "position": 3, "body": "old finding", "resolver": None}],
    )
    code, out, clock = run_main(gitea)
    check("CI failed + STALE review -> stops at once with 5", (code, clock.sleeps), (5, []))
    ok("  prints the stale review's findings, inline ones included",
       "cost columns" in out and "old finding" in out, out)
    ok("  and the fail-fast banner", "CI FAILED" in out and "re-run the job" in out, out)

    code, out, clock = run_main(FakeGitea(jobs=lambda n: FAILED_JOB))
    check("CI failed + no review -> exit 2 at once", (code, clock.sleeps), (2, []))

    code, out, clock = run_main(FakeGitea(jobs=lambda n: FAILED_JOB), "--no-fail-fast")
    check("--no-fail-fast waits the review out", code, 2)
    ok("  to the TIMED_OUT block", "=== TIMED_OUT" in out and len(clock.sleeps) > 0, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD)], jobs=lambda n: RUNNING_JOB))
    check("deadline, review decided, CI running -> review's code", code, 0)
    ok("  with the did-not-settle note", "CI did not settle" in out, out)


@section("main: deadline and outages")
def _():
    code, out, clock = run_main(FakeGitea())
    check("nothing at all -> TIMED_OUT, exit 2", code, 2)
    ok("  not an approval, with health and CI", "NOT an approval" in out and "up (test)" in out
       and "CI: PASSED" in out, out)
    ok("  offers the review-request lever", "requested_reviewers" in out, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(OLD)]))
    check("deadline with only a STALE review -> 5, not TIMED_OUT", code, 5)
    ok("  says it is real but old", "anchored to an older SHA" in out and "TIMED_OUT" not in out, out)

    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD)],
        fail=lambda path, n: TimeoutError("read timed out") if path.endswith("/reviews") and n == 2 else None))
    check("one Gitea outage round is skipped, not fatal", code, 0)
    ok("  and reported", "could not read Gitea" in out, out)

    code, out, clock = run_main(FakeGitea(
        fail=lambda path, n: urllib.error.URLError("down") if n >= 2 and path.endswith("/reviews") else None))
    check("Gitea down until the deadline -> exit 1", code, 1)
    ok("  UNREACHABLE, not a pass", "=== UNREACHABLE" in out and "NOT a pass" in out, out)

    code, out, clock = run_main(FakeGitea(
        fail=lambda path, n: urllib.error.URLError("down") if n >= 2 and path.endswith("/reviews") else None),
        "--once")
    check("--once with Gitea down -> exit 1 without waiting", (code, clock.sleeps), (1, []))

    try:
        run_main(FakeGitea(fail=lambda path, n: http_error(401) if path.endswith("/reviews") else None))
        ok("a 401 mid-wait is raised, not retried forever", False)
    except urllib.error.HTTPError as exc:
        check("a 401 mid-wait is raised, not retried forever", exc.code, 401)


@section("main: PR state")
def _():
    code, out, clock = run_main(FakeGitea(pr=lambda n: {"state": "closed", "merged": True}))
    check("merged PR -> reported once, no waiting", (code, clock.sleeps), (2, []))
    ok("  says so", "already MERGED" in out, out)

    code, out, clock = run_main(FakeGitea(pr=lambda n: {"state": "closed"} if n >= 3 else {}))
    check("closed mid-wait -> stops", (code, len(clock.sleeps)), (2, 1))
    ok("  says so", "closed mid-wait" in out, out)

    code, out, clock = run_main(FakeGitea(author=BOT))
    check("bot-authored PR -> DECLINED at once", (code, clock.sleeps), (6, []))

    code, out, clock = run_main(FakeGitea(jobs=lambda n: RUNNING_JOB), "--once")
    check("--once reports without waiting", (code, clock.sleeps), (2, []))


if __name__ == "__main__":
    for title, fn in SECTIONS:
        print(f"{title}:")
        try:
            fn()
        except Exception:
            FAILURES.append(f"{title} (crashed)")
            traceback.print_exc()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:\n  " + "\n  ".join(FAILURES))
        raise SystemExit(1)
    print("\nall passed")
