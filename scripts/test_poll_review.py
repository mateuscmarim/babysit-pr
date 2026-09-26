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
        # Worded apart from "generic" by the agent, and matched nothing here:
        # the wait ran to TIMED_OUT on a bot that had already said it failed.
        ("undetermined", "⚠️ The automated review did not complete, and the reviewer could not determine why."),
        ("unrecognized", "⚠️ Some failure wording this poller has never seen."),
    ]:
        v = classify(HEAD, [], [comment(body)], EPOCH, BOT)
        check(f"{variant} warning -> FAILED", v.state, "FAILED")
        ok(f"  {variant} detail names the variant", v.detail.startswith(f"[{variant}]"), v.detail)

    check("a bot comment without the warning sign is not a failure",
          classify(HEAD, [], [comment("ℹ️ something informational")], EPOCH, BOT).state, "PENDING")

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

    # A job no runner picks up never gets a started_at. Without a clock of its
    # own it sat at "status=queued" to the deadline, never flagged.
    long_queued = datetime.fromtimestamp(
        NOW.timestamp() - poll_review.CI_QUEUED_THRESHOLD_S - 60, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    v = classify_ci([{**job("queued"), "created_at": long_queued}], NOW)
    check("queued past the threshold -> still RUNNING", v.state, "RUNNING")
    ok("  but says no runner has picked it up", "no runner" in v.detail and "queued" in v.detail, v.detail)
    v = classify_ci([{**job("queued"), "created_at": fresh}], NOW)
    ok("  a freshly queued job is not flagged", "no runner" not in v.detail, v.detail)
    v = classify_ci([job("queued", started="0001-01-01T00:00:00Z")], NOW)
    ok("a zero started_at is not 'running since year 1'", "hung" not in v.detail, v.detail)

    # twm-android#18: Gitea reported a gate job's started_at ahead of now, and
    # the poller printed "running -1m34s".
    def ahead(seconds):
        return datetime.fromtimestamp(NOW.timestamp() + seconds, tz=timezone.utc
                                      ).isoformat().replace("+00:00", "Z")
    v = classify_ci([job("in_progress", started=ahead(94))], NOW)
    ok("a started_at in the future is never a negative time",
       not re.search(r"-\d", v.detail) and "just started" in v.detail
   and "1m34s ahead" in v.detail, v.detail)
    v = classify_ci([{**job("queued"), "created_at": ahead(94)}], NOW)
    ok("  nor is a created_at in the future",
       not re.search(r"-\d", v.detail) and "just queued" in v.detail, v.detail)
    check("  and the ticking gap does not make a new status line",
          poll_review.progress_key(poll_review.Verdict("PENDING"), v),
          poll_review.progress_key(poll_review.Verdict("PENDING"),
                                   classify_ci([{**job("queued"), "created_at": ahead(119)}], NOW)))

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

    def d(review_state, ci, *, watched=0, past=False, once=False, fail_fast=True,
          wait_for="both", new=False, was_open=False):
        return decide(review_state, ci, watched_s=watched, past_deadline=past,
                      once=once, fail_fast=fail_fast, wait_for=wait_for,
                      new_review=new, ci_was_open=was_open)

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

    # --wait-for any, the default: return on whichever side has news first.
    for terminal in poll_review.REVIEW_DONE:
        check(f"any: {terminal} while CI runs -> review", d(terminal, RUNNING, wait_for="any"), "review")
    check("any: a new STALE review while CI runs -> review",
          d("STALE", RUNNING, wait_for="any", new=True), "review")
    check("any: a STALE review already there -> wait", d("STALE", RUNNING, wait_for="any"), "wait")
    check("any: REVIEWED + NONE inside the grace -> wait it out",
          d("REVIEWED", NONE, wait_for="any"), "wait")
    check("any: CI finished during the run, review pending -> ci",
          d("PENDING", PASSED, wait_for="any", was_open=True), "ci")
    check("any: CI already passed at the start is not news -> wait",
          d("PENDING", PASSED, wait_for="any"), "wait")
    check("any: NONE past the grace is not news -> wait",
          d("PENDING", NONE, watched=CI_NONE_GRACE_S, wait_for="any", was_open=True), "wait")
    check("any: CI failed, review pending -> ci_failed, not ci",
          d("PENDING", FAILED, wait_for="any", was_open=True), "ci_failed")
    check("any: nothing yet -> wait", d("PENDING", RUNNING, wait_for="any"), "wait")
    check("any: deadline, nothing -> timed_out", d("PENDING", PASSED, wait_for="any", past=True), "timed_out")
    check("ci: settled already counts, it is what was asked for",
          d("PENDING", PASSED, wait_for="ci"), "ci")
    check("ci: a decided review alone -> wait", d("REVIEWED", RUNNING, wait_for="ci"), "wait")
    check("ci: a new STALE review alone -> wait", d("STALE", RUNNING, wait_for="ci", new=True), "wait")
    check("review: CI finishing alone -> wait",
          d("PENDING", PASSED, wait_for="review", was_open=True), "wait")
    check("review: a decided review -> review", d("FAILED", RUNNING, wait_for="review"), "review")
    check("both: a decided review alone -> wait", d("REVIEWED", RUNNING, wait_for="both"), "wait")
    check("exit: an early CI return with no review -> PENDING's 7", poll_review.exit_code("ci", "PENDING"), 7)
    check("exit: an early review return keeps the review's code", poll_review.exit_code("review", "FAILED"), 3)
    check("exit: a new STALE review -> 5", poll_review.exit_code("review", "STALE"), 5)

    check("exit: done uses the review's code", poll_review.exit_code("done", "FAILED"), 3)
    check("exit: PENDING from a run that did not wait -> 7, not TIMED_OUT's 2", poll_review.exit_code("once", "PENDING"), 7)
    check("exit: ci_failed -> 8 over a STALE review", poll_review.exit_code("ci_failed", "STALE"), 8)
    check("exit: ci_failed -> 8 over a PENDING review", poll_review.exit_code("ci_failed", "PENDING"), 8)
    check("exit: every code means one thing", len(set(EXIT.values()) | {poll_review.EXIT_UNREACHABLE}),
          len(EXIT) + 1)
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

    # A Gitea that ignores ?head_sha= lists every run in the repo. A newer run
    # from another commit must neither count nor shadow the head's own run.
    got = ci_jobs(runs_then_jobs(
        [{"id": 5, "path": "ci.yml@x", "event": "pull_request", "head_sha": OLD},
         {"id": 4, "path": "ci.yml@x", "event": "pull_request", "head_sha": HEAD}],
        {5: [{"name": "t", "status": "completed", "conclusion": "failure"}],
         4: [{"name": "t", "status": "completed", "conclusion": "success"}]},
    ))
    check("a run at another SHA is ignored, not newest-wins", classify_ci(got, NOW).state, "PASSED")
    check("only runs at another SHA -> []",
          ci_jobs(runs_then_jobs([{"id": 5, "path": "ci.yml@x", "head_sha": OLD}], {})), [])

    # CI_WORKFLOW_FILE matches by file name whichever side carries a directory.
    one_job = {6: [{"name": "t", "status": "completed", "conclusion": "success"}]}
    check("filter given as a full path matches a bare run path",
          len(ci_jobs(runs_then_jobs([{"id": 6, "path": "ci.yml@x"}], one_job),
                      workflow_file=".gitea/workflows/ci.yml")), 1)
    check("filter given as a file name matches a full run path",
          len(ci_jobs(runs_then_jobs([{"id": 6, "path": ".gitea/workflows/ci.yml@x"}], one_job),
                      workflow_file="ci.yml")), 1)

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
    # Unset (the default) means "not configured": nothing is asked, and it
    # must not be some unrelated service answering on localhost.
    asked: list = []
    with patched(poll_review, STATE_URL="", HEALTH_URL=""):
        check("STATE_URL unset -> None", with_urlopen(lambda *a, **k: asked.append(a), lambda: poll_review.agent_state("owner/name", 4, HEAD)), None)
        check("  without a request", asked, [])
        ok("HEALTH_URL unset -> 'not configured'", "not configured" in poll_review.health(), poll_review.health())

    def get():
        with patched(poll_review, STATE_URL="http://agent.test/state"):
            return poll_review.agent_state("owner/name", 4, HEAD)

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

    got, _ = run(flaky([TimeoutError("read timed out")] * 5, b"[]"))
    check("a give-up names the endpoint that failed, without the query",
          poll_review.describe(got), "TimeoutError: read timed out on /x")
    check("an error from elsewhere is described without one",
          poll_review.describe(TimeoutError("t")), "TimeoutError: t")

    timeouts = []

    def record(req, timeout=None):
        timeouts.append(timeout)
        return _FakeResponse(b"[]")
    run(record)
    check("each attempt uses API_TIMEOUT_S", timeouts, [poll_review.API_TIMEOUT_S])


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
    check("a host that merely ends in ours -> None",
          poll_review.repo_from_remotes("origin\thttps://notgit.example.com/owner/repo.git (fetch)\n", base), None)

    # A fork and its upstream, both on this Gitea. `git remote -v` sorts by
    # name, so taking the first match picked `fork` over `origin`.
    both = (
        "fork\thttps://git.example.com/me/repo.git (fetch)\n"
        "fork\thttps://git.example.com/me/repo.git (push)\n"
        "origin\thttps://git.example.com/team/repo.git (fetch)\n"
        "origin\thttps://git.example.com/team/repo.git (push)\n"
        "zzz\thttps://github.com/team/repo.git (fetch)\n"
    )
    check("origin outranks an alphabetically earlier remote",
          poll_review.remote_repos(both, base), ["team/repo", "me/repo"])
    check("the branch's tracking remote outranks origin",
          poll_review.remote_repos(both, base, prefer=("fork",)), ["me/repo", "team/repo"])


@section("infer_pr")
def _():
    prs = {
        "me/repo": [],
        "team/repo": [
            {"number": 3, "head": {"ref": "fix", "repo": {"full_name": "stranger/repo"}}},
            {"number": 8, "head": {"ref": "fix", "repo": {"full_name": "me/repo"}}},
        ],
    }

    def fake(path, tok):
        repo = path.split("/repos/")[1].split("/pulls")[0]
        return prs[repo] if "page=1&" in path else []

    with patched(poll_review, api=fake):
        check("a fork's PR is found in the next remote's repo",
              poll_review.infer_pr(["me/repo", "team/repo"], "t", "fix"), ("team/repo", 8))
        check("  and a stranger's same-named branch, listed first, is not it",
              poll_review.infer_pr(["team/repo", "me/repo"], "t", "fix"), ("team/repo", 8))
        check("--repo upstream still finds the PR from our own fork",
              poll_review.infer_pr(["team/repo"], "t", "fix", ["me/repo", "team/repo"]), ("team/repo", 8))
        try:
            poll_review.infer_pr(["me/repo"], "t", "fix")
            ok("no PR anywhere -> exits", False)
        except SystemExit as exc:
            ok("no PR anywhere -> exits naming the repos searched", "me/repo" in str(exc.code), exc.code)


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


@section("next_steps")
def _():
    V, PASSED = poll_review.Verdict, CIVerdict("PASSED")

    def steps(action, verdict, ci=PASSED, closed=False):
        return " | ".join(poll_review.next_steps(action, verdict, ci, watched_s=600, closed=closed))

    finding = [{"id": 1}]
    ok("REVIEWED with findings -> verify each, and where the reply rules live",
       "Verify each finding" in (s := steps("done", V("REVIEWED", inline=finding)))
       and "replying.md" in s, s)
    ok("REVIEWED with nothing -> still read the overview, no reply pointer",
       "read the overview" in (s := steps("done", V("REVIEWED"))) and "replying.md" not in s, s)
    ok("an unanchored note alone still counts as a finding",
       "Verify each finding" in (s := steps("done", V("REVIEWED", notes=["n"]))), s)
    ok("STALE names the SHA its findings describe",
       "bd27caf1" in (s := steps("stale", V("STALE", reviewed_sha=OLD, inline=finding))), s)
    ok("FAILED on credentials -> an operator, not a retry",
       "only an operator" in (s := steps("done", V("FAILED", detail="[credentials] x"))), s)
    ok("FAILED on a generic error -> a fresh request may succeed",
       "may succeed" in (s := steps("done", V("FAILED", detail="[generic] x"))), s)
    for reason, want in [("nothing_new_since_last_review", "that review stands"),
                         ("lost_on_restart", "nobody reviewed this"),
                         ("superseded", "unreviewed")]:
        ok(f"DECLINED {reason} -> {want!r}",
           want in (s := steps("done", V("DECLINED", detail=f"[agent] no review is coming: {reason}"))), s)
    ok("SKIPPED -> split the PR", "Split" in (s := steps("done", V("SKIPPED"))), s)
    ok("TIMED_OUT -> never 'no issues found', and no review-state line",
       "NOT an" in (s := steps("timed_out", V("PENDING"))) and "PENDING means" not in s, s)
    ok("PENDING under --once says to run without it",
       "without --once" in (s := steps("once", V("PENDING"))) and "NOT a pass" in s, s)
    ok("PENDING on a closed PR says none is coming",
       "PR is closed" in (s := steps("once", V("PENDING"), closed=True)), s)
    ok("a failed CI next to a clean review is named, though the code is 0",
       "CI FAILED" in (s := steps("done", V("REVIEWED"), CIVerdict("FAILED"))), s)
    ok("  but not twice under ci_failed",
       (s := steps("ci_failed", V("PENDING"), CIVerdict("FAILED"))).count("CI FAILED") == 1, s)
    ok("CI still running at the deadline -> did not settle",
       "CI did not settle" in (s := steps("ci_open", V("REVIEWED"), CIVerdict("RUNNING"))), s)
    ok("CI passed -> no CI step at all", "CI" not in steps("done", V("REVIEWED")), None)

    def early(action, verdict, ci):
        return " | ".join(poll_review.next_steps(action, verdict, ci, watched_s=600,
                                                 closed=False, rerun="poll --repo o/r --pr 7"))
    ok("review first, CI running -> not a pass on CI, and how to wait for it",
       "CI has not settled (RUNNING)" in (s := early("review", V("FAILED", detail="[credentials] x"),
                                                      CIVerdict("RUNNING")))
       and "`poll --repo o/r --pr 7 --wait-for ci`" in s, s)
    ok("CI first, review pending -> NOT a pass, and how to wait for the review",
       "NOT a pass" in (s := early("ci", V("PENDING"), PASSED))
       and "--wait-for review" in s and "without --once" not in s, s)
    ok("a new STALE review while CI runs -> wait for both",
       "--wait-for any" in (s := early("review", V("STALE", reviewed_sha=OLD, inline=finding),
                                       CIVerdict("RUNNING"))) and "bd27caf1" in s, s)
    ok("nothing left open -> no re-run step", "--wait-for" not in steps("done", V("REVIEWED")), None)

    running = CIVerdict("RUNNING", "ci.yml/test: status=in_progress, running 3m12s")
    later = CIVerdict("RUNNING", "ci.yml/test: status=in_progress, running 3m42s")
    check("progress_key ignores the ticking clock",
          poll_review.progress_key(V("PENDING"), running), poll_review.progress_key(V("PENDING"), later))
    ok("  but not a change of state",
       poll_review.progress_key(V("PENDING"), running) != poll_review.progress_key(V("PENDING"), PASSED), None)


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
    # --once sets the module's API limits for the rest of the process. Patching
    # them to their own values restores them, so the next test starts clean.
    with patched(poll_review, api=gitea, time=clock, token=lambda: "t",
                 agent_state=lambda repo, pr, sha: agent, health=lambda: "up (test)",
                 API_TIMEOUT_S=poll_review.API_TIMEOUT_S, API_RETRIES=poll_review.API_RETRIES), \
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
    check("  printing the unchanged status line once, not every round", out.count("review=PENDING"), 1)
    ok("  and ends with what to do next", ">> NEXT:" in out.split("=== REVIEWED")[-1], out)

    gitea = FakeGitea(
        reviews=lambda n: [review(HEAD, rid=5)],
        jobs=lambda n: RUNNING_JOB if n < 5 else [{"name": "test", "status": "completed", "conclusion": "success"}],
        inline=lambda rid: [{"id": 70, "path": "a.py", "position": 3, "body": "leaks", "resolver": None}],
    )
    code, out, clock = run_main(gitea, "--wait-for", "both")
    check("--wait-for both: REVIEWED holds open while CI runs, then exits 0", code, 0)
    check("  inline comments fetched once, not every interval", gitea.inline_calls, 1)
    ok("  and printed with their thread id", "[#70 open] a.py:3" in out, out)

    # A comment on a removed line has position 0 and the line in
    # original_position. Printing `a.py:0` handed reply_finding a bad line.
    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD, rid=5)],
        inline=lambda rid: [{"id": 71, "path": "a.py", "position": 0, "original_position": 9,
                             "body": "gone", "resolver": None}]))
    ok("an old-side comment prints its original line, marked",
       "a.py:9 (old side" in out and "a.py:0" not in out, out)

    code, out, clock = run_main(
        FakeGitea(head=lambda n: OLD if n <= 2 else HEAD,
                  reviews=lambda n: [review(HEAD)] if n >= 3 else []))
    check("head moves mid-wait -> the new head's review answers", code, 0)
    ok("  says so", ">> head moved" in out and f"@ {HEAD[:8]}" in out, out)

    # Everything HeadWatch tracks restarts with the head.
    code, out, clock = run_main(FakeGitea(head=lambda n: OLD if n <= 8 else HEAD),
                                "--wait-for", "review")
    check("a head move restarts the deadline -> TIMED_OUT", code, 2)
    check("  5m after the move, not 5m after the start", len(clock.sleeps), 17)
    code, out, clock = run_main(FakeGitea(head=lambda n: OLD if n <= 3 else HEAD,
                                          jobs=lambda n: RUNNING_JOB if n <= 3 else
                                          [{"name": "test", "status": "completed",
                                            "conclusion": "success"}]))
    ok("  and forgets the old head's open CI: the new head's, passed at first "
       "sight, is not news", code == 2 and len(clock.sleeps) > 10, (code, len(clock.sleeps)))


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
        fail=lambda path, n: http_error(502) if path == "/repos/o/r/actions/runs" and n < 4 else None),
        "--wait-for", "both")
    check("a transient CI API error holds the wait until it clears", (code, len(clock.sleeps)), (0, 2))
    ok("  and ends PASSED", "CI: PASSED" in out, out)

    gitea = FakeGitea(
        reviews=lambda n: [review(OLD, rid=9, body=STALE_BODY)],
        jobs=lambda n: FAILED_JOB,
        inline=lambda rid: [{"id": 77, "path": "a.py", "position": 3, "body": "old finding", "resolver": None}],
    )
    code, out, clock = run_main(gitea)
    check("CI failed + STALE review -> stops at once with 8", (code, clock.sleeps), (8, []))
    ok("  prints the stale review's findings, inline ones included",
       "cost columns" in out and "old finding" in out, out)
    ok("  and the fail-fast banner", "CI FAILED" in out and "re-run the job" in out, out)

    code, out, clock = run_main(FakeGitea(jobs=lambda n: FAILED_JOB))
    check("CI failed + no review -> exit 8 at once, not TIMED_OUT's 2", (code, clock.sleeps), (8, []))

    code, out, clock = run_main(FakeGitea(jobs=lambda n: FAILED_JOB), "--no-fail-fast")
    check("--no-fail-fast waits the review out", code, 2)
    ok("  to the TIMED_OUT block", "=== TIMED_OUT" in out and len(clock.sleeps) > 0, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD)], jobs=lambda n: RUNNING_JOB),
                                "--wait-for", "ci")
    check("deadline, review decided, CI running -> review's code", code, 0)
    ok("  with the did-not-settle note", "CI did not settle" in out, out)


@section("main: return on either side")
def _():
    # twm-android#17: the credentials warning landed at 32s, and the poller
    # held it until CI finished at 2184s.
    warning = "⚠️ The reviewer's credentials are not working, so this pull request was not reviewed."
    code, out, clock = run_main(FakeGitea(
        comments=lambda n: [comment(warning, at=13)] if n >= 3 else [],
        jobs=lambda n: RUNNING_JOB))
    check("a bot failure while CI runs -> returns on it, exit 3", (code, len(clock.sleeps)), (3, 1))
    ok("  says CI is still open and how to wait for it",
       "CI: RUNNING" in out and "--repo o/r --pr 7 --wait-for ci`" in out, out)

    code, out, clock = run_main(FakeGitea(
        comments=lambda n: [comment("ℹ️ This PR's diff is too large to review automatically.", at=13)],
        jobs=lambda n: RUNNING_JOB))
    check("SKIPPED already there, CI running -> returns at once, exit 4", (code, clock.sleeps), (4, []))

    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD, rid=5)], jobs=lambda n: RUNNING_JOB,
        inline=lambda rid: [{"id": 70, "path": "a.py", "position": 3, "body": "leaks", "resolver": None}]))
    check("REVIEWED while CI runs -> returns at once, exit 0", (code, clock.sleeps), (0, []))
    ok("  with the findings", "[#70 open] a.py:3" in out, out)

    code, out, clock = run_main(FakeGitea(jobs=lambda n: RUNNING_JOB if n < 4 else
                                          [{"name": "test", "status": "completed", "conclusion": "success"}]))
    check("CI passes first -> returns on it, PENDING's 7", (code, len(clock.sleeps)), (7, 2))
    ok("  NOT a pass, and how to wait for the review",
       "NOT a pass" in out and "--wait-for review" in out and "CI: PASSED" in out, out)

    code, out, clock = run_main(FakeGitea(jobs=lambda n: RUNNING_JOB if n < 4 else FAILED_JOB))
    check("CI fails first -> 8, as before", (code, len(clock.sleeps)), (8, 2))

    gitea = FakeGitea(
        reviews=lambda n: [review(OLD, rid=9, body=STALE_BODY)] if n >= 4 else [],
        jobs=lambda n: RUNNING_JOB,
        inline=lambda rid: [{"id": 77, "path": "a.py", "position": 3, "body": "old finding", "resolver": None}])
    code, out, clock = run_main(gitea)
    check("a review of a head pushed past lands mid-wait -> returns, 5", (code, len(clock.sleeps)), (5, 2))
    ok("  with its findings, and how to keep waiting for both",
       "old finding" in out and "--wait-for any" in out, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(OLD, rid=9)], jobs=lambda n: RUNNING_JOB))
    check("a STALE review already there is not news -> waits to the deadline",
          (code, sum(clock.sleeps) >= 5 * 60), (5, True))

    code, out, clock = run_main(FakeGitea(
        reviews=lambda n: [review(HEAD, rid=5)],
        jobs=lambda n: RUNNING_JOB if n < 4 else [{"name": "test", "status": "completed", "conclusion": "success"}]),
        "--wait-for", "ci")
    check("--wait-for ci skips the review it was sent back for", (code, len(clock.sleeps)), (0, 2))

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD)] if n >= 4 else []),
                                "--wait-for", "review")
    check("--wait-for review with CI done waits for the review", (code, len(clock.sleeps)), (0, 2))


@section("main: deadline and outages")
def _():
    code, out, clock = run_main(FakeGitea())
    check("nothing at all -> TIMED_OUT, exit 2", code, 2)
    ok("  not an approval, with health and CI", "NOT an approval" in out and "up (test)" in out
       and "CI: PASSED" in out, out)
    ok("  offers the review-request lever", "requested_reviewers" in out, out)

    # The curl uses $GITEA_TOKEN, which is unset when the token came from tea.
    with patched(poll_review, os=types.SimpleNamespace(environ={})):
        code, out, clock = run_main(FakeGitea())
    ok("  says GITEA_TOKEN must be exported when it is not set", "GITEA_TOKEN is not set" in out, out)
    with patched(poll_review, os=types.SimpleNamespace(environ={"GITEA_TOKEN": "x"})):
        code, out, clock = run_main(FakeGitea())
    ok("  and not when it is", "GITEA_TOKEN is not set" not in out, out)

    with patched(poll_review, STATE_URL=""):
        code, out, clock = run_main(FakeGitea())
    ok("an unconfigured agent reads as not configured, not unreachable",
       "NOT CONFIGURED" in out and "UNREACHABLE" not in out, out)

    # Requested mid-wait: the TIMED_OUT hint must use what Gitea says now.
    code, out, clock = run_main(FakeGitea(
        pr=lambda n: {"requested_reviewers": [{"login": BOT}]} if n >= 2 else {}))
    ok("requested_reviewers is re-read every round", "not a requested reviewer" not in out, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(OLD)]))
    check("deadline with only a STALE review -> 5, not TIMED_OUT", code, 5)
    ok("  says it is real but old", "an older head" in out and "TIMED_OUT" not in out, out)

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
    check("merged PR -> PENDING, exit 7, no waiting", (code, clock.sleeps), (7, []))
    ok("  says so", "already MERGED" in out, out)
    ok("  and that exit 2 is 'nothing yet', not a pass",
       "PENDING" in out and "NOT a pass" in out and "PR is closed" in out, out)

    code, out, clock = run_main(FakeGitea(pr=lambda n: {"state": "closed"} if n >= 3 else {}))
    check("closed mid-wait -> stops with 7", (code, len(clock.sleeps)), (7, 1))
    ok("  says so", "closed mid-wait" in out, out)

    code, out, clock = run_main(FakeGitea(author=BOT))
    check("bot-authored PR -> DECLINED at once", (code, clock.sleeps), (6, []))
    ok("  and says the PR is unreviewed", ">> NEXT:" in out and "unreviewed" in out, out)

    code, out, clock = run_main(FakeGitea(jobs=lambda n: RUNNING_JOB), "--once")
    check("--once reports without waiting, exit 7", (code, clock.sleeps), (7, []))
    ok("  names PENDING as not a pass", "PENDING" in out and "NOT a pass" in out, out)
    ok("  and says CI has not settled", "CI has not settled" in out, out)

    # The likeliest "can I merge now?" check: a clean review next to a CI that
    # is still running. The code stays the review's, but it must not be silent.
    code, out, clock = run_main(
        FakeGitea(reviews=lambda n: [review(HEAD, rid=5)], jobs=lambda n: RUNNING_JOB), "--once")
    check("--once, REVIEWED + CI running -> review's exit 0", (code, clock.sleeps), (0, []))
    ok("  with a CI-not-settled note", "CI has not settled" in out and "CI: RUNNING" in out, out)
    ok("  and no PENDING banner", "NOT a pass" not in out, out)

    code, out, clock = run_main(FakeGitea(reviews=lambda n: [review(HEAD, rid=5)]), "--once")
    check("--once, REVIEWED + CI passed -> exit 0", code, 0)

    # Live, one stuck endpoint cost a --once run 101s: 3 attempts at 30s.
    limits = []
    gitea = FakeGitea(fail=lambda path, n: limits.append(
        (poll_review.API_TIMEOUT_S, poll_review.API_RETRIES)) or None)
    run_main(gitea, "--once")
    check("--once asks Gitea with the short limits",
          set(limits), {(poll_review.ONCE_API_TIMEOUT_S, poll_review.ONCE_API_RETRIES)})
    limits.clear()
    run_main(gitea)
    check("  a waiting run keeps the long ones, and --once did not leak into it",
          set(limits), {(30.0, 3)})

    stuck = TimeoutError("The read operation timed out")
    stuck.gitea_path = "/repos/o/r/actions/runs"  # pyright: ignore[reportAttributeAccessIssue]
    code, out, clock = run_main(FakeGitea(
        fail=lambda path, n: stuck if path == "/repos/o/r/pulls/7/reviews" else None), "--once")
    ok("UNREACHABLE names the endpoint that timed out",
       code == 1 and "timed out on /repos/o/r/actions/runs" in out, out)
    ok("  with no CI note", "CI has not settled" not in out, out)


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
