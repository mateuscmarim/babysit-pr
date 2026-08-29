#!/usr/bin/env python3
"""Tests for poll_review's classification. Run: python3 test_poll_review.py

Plain asserts, no pytest — this has to run anywhere the skill runs.
"""

import contextlib
import inspect
import io
import urllib.error
from datetime import datetime, timezone

from poll_review import (
    CI_NONE_GRACE_S,
    CI_SLOW_THRESHOLD_S,
    CIVerdict,
    EPOCH,
    EXIT,
    classify,
    classify_ci,
    ci_settled,
    extract_notes,
    parse_ts,
    signal_floor,
)

import poll_review

HEAD = "a7c5d6c7" + "0" * 32
OLD = "bd27caf1" + "0" * 32
BOT = "review-bot"


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


def check(name, got, want):
    assert got == want, f"{name}: expected {want!r}, got {got!r}"
    print(f"  ok  {name}")


print("classify:")

# A review anchored to the head SHA is the terminal positive signal.
v = classify(HEAD, [review(HEAD)], [], EPOCH, BOT)
check("review at head -> REVIEWED", v.state, "REVIEWED")
check("  carries the review id", v.review_id, 1)

# Nothing at all yet.
check("no signals -> PENDING", classify(HEAD, [], [], EPOCH, BOT).state, "PENDING")

# A review exists, but for a SHA that is not what would be merged.
v = classify(HEAD, [review(OLD)], [], EPOCH, BOT)
check("review at other sha -> STALE", v.state, "STALE")
assert OLD[:8] in v.detail and HEAD[:8] in v.detail, "STALE detail names both SHAs"
print("  ok  STALE detail names both SHAs")

# A STALE verdict must carry the review's contents, not just a detail line.
# review-bot routinely anchors a review to the head you already pushed past
# (milex-scopeline-server#14: reviews 304, 308, 313, 314), so this is the
# common path, and dropping the body there loses live findings.
stale_body = (
    "## Pull request overview\n\nTwo correctness defects remain.\n\n"
    "**Additional notes**\n\n- the cost columns exclude failed runs\n"
)
v = classify(HEAD, [review(OLD, rid=314, body=stale_body)], [], EPOCH, BOT)
check("  STALE carries the review id", v.review_id, 314)
check("  STALE carries the overview", v.overview, stale_body)
check("  STALE carries the unanchored notes", v.notes, extract_notes(stale_body))
check("  STALE names the sha it reviewed", v.reviewed_sha, OLD)

# ...and render must actually print them. It used to return early for every
# state but REVIEWED, which is what made the carried body invisible.
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    poll_review.render(v, "o/r", 14, HEAD, 30)
out = buf.getvalue()
assert "Two correctness defects remain" in out, out
assert "cost columns exclude failed runs" in out, out
print("  ok  render prints a STALE review's overview and notes")
assert OLD[:8] in out and "NOT the head" in out, out
print("  ok  render says plainly which SHA those findings describe")
# The verdict itself must not be laundered: an older-SHA review is not a
# review of what is about to be merged, and exit 5 is not exit 0.
assert "REVIEWED" not in out, out
check("  STALE still exits 5", EXIT["STALE"], 5)

# Another human's review must not be mistaken for the bot's.
check(
    "human review ignored",
    classify(HEAD, [review(HEAD, user="mateuscmarim")], [], EPOCH, BOT).state,
    "PENDING",
)

# Terminal issue comments, one per worker.py branch.
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
    ("generic", "⚠️ Automated review failed; see service logs."),
]:
    v = classify(HEAD, [], [comment(body)], EPOCH, BOT)
    check(f"{variant} warning -> FAILED", v.state, "FAILED")
    assert v.detail.startswith(f"[{variant}]"), f"{variant}: detail names the variant"
    print(f"  ok  {variant} detail names the variant")

# The floor guard: a warning that predates the current head commit is about an
# earlier push and must not terminate this wait.
stale_warning = [comment("⚠️ Automated review failed; see service logs.", at=9)]
check(
    "warning below floor ignored",
    classify(HEAD, [], stale_warning, parse_ts(ts(11)), BOT).state,
    "PENDING",
)
check(
    "warning above floor honoured",
    classify(HEAD, [], stale_warning, parse_ts(ts(8)), BOT).state,
    "FAILED",
)

# A completed review at head outranks any later warning comment.
check(
    "review at head beats later warning",
    classify(
        HEAD,
        [review(HEAD, at=12)],
        [comment("⚠️ Automated review failed; see service logs.", at=13)],
        EPOCH,
        BOT,
    ).state,
    "REVIEWED",
)

print("extract_notes:")

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

print("signal_floor:")

check("no data -> epoch", signal_floor(None, [], BOT), EPOCH)
check(
    "takes the head commit date",
    signal_floor(parse_ts(ts(10)), [], BOT),
    parse_ts(ts(10)),
)
check(
    "a newer prior review raises the floor",
    signal_floor(parse_ts(ts(10)), [review(OLD, at=14)], BOT),
    parse_ts(ts(14)),
)
check(
    "a human review does not raise it",
    signal_floor(parse_ts(ts(10)), [review(OLD, at=14, user="someone")], BOT),
    parse_ts(ts(10)),
)

print("classify_ci:")

NOW = datetime(2026, 8, 27, 22, 0, 0, tzinfo=timezone.utc)


def job(status, conclusion=None, *, jid=42, started=None, name="gate", workflow="quality-gate.yml"):
    d = {"id": jid, "status": status, "name": name, "_workflow": workflow}
    if conclusion is not None:
        d["conclusion"] = conclusion
    if started is not None:
        d["started_at"] = started
    return d


check("no jobs -> NONE", classify_ci([], NOW).state, "NONE")
check(
    "malformed entries -> NONE, not a crash",
    classify_ci(["not a dict", None], NOW).state,
    "NONE",
)
check(
    "completed success -> PASSED",
    classify_ci([job("completed", "success")], NOW).state,
    "PASSED",
)

v = classify_ci([job("completed", "failure")], NOW)
check("completed failure -> FAILED", v.state, "FAILED")
assert "failure" in v.detail, "FAILED detail names the conclusion"
print("  ok  FAILED detail names the conclusion")

fresh_started = NOW.isoformat().replace("+00:00", "Z")
v = classify_ci([job("in_progress", started=fresh_started)], NOW)
check("in_progress, just started -> RUNNING", v.state, "RUNNING")
assert "hung" not in v.detail, "a fresh run is not flagged as possibly hung"
print("  ok  fresh run not flagged as possibly hung")

overdue = NOW.timestamp() - CI_SLOW_THRESHOLD_S - 60
overdue_started = (
    datetime.fromtimestamp(overdue, tz=timezone.utc).isoformat().replace("+00:00", "Z")
)
v = classify_ci([job("in_progress", started=overdue_started)], NOW)
check("running past the slow threshold -> still RUNNING", v.state, "RUNNING")
assert "may be hung" in v.detail, "an overdue run is flagged as possibly hung"
print("  ok  overdue run flagged as possibly hung")

# Worst-of aggregation: one FAILED job wins even if others passed; short of
# that, one RUNNING job keeps it open even if the rest already finished.
v = classify_ci(
    [job("completed", "success", name="tests"), job("completed", "failure", name="eval")],
    NOW,
)
check("one failed among several -> FAILED", v.state, "FAILED")
assert "eval" in v.detail and "tests" not in v.detail.split(";")[0], (
    "FAILED detail names the failing job, not the passing one first"
)
print("  ok  FAILED detail names the failing job")

v = classify_ci(
    [job("completed", "success", name="tests"), job("in_progress", name="eval")],
    NOW,
)
check("one running among others passed -> RUNNING", v.state, "RUNNING")

v = classify_ci(
    [
        job("completed", "success", name="tests", workflow="tests.yml"),
        job("completed", "success", name="eval", workflow="eval.yml"),
    ],
    NOW,
)
check("multiple workflows, all passed -> PASSED", v.state, "PASSED")
assert "tests.yml/tests" in v.detail and "eval.yml/eval" in v.detail, (
    "PASSED detail names every job across every workflow"
)
print("  ok  PASSED detail names every job across every workflow")

print("ci_settled:")

# RUNNING is never settled, no matter how long it has been watched -- the
# deadline is what ends that wait, not this function.
check("RUNNING -> not settled", ci_settled(CIVerdict("RUNNING"), 0), False)
check(
    "RUNNING stays unsettled past the grace",
    ci_settled(CIVerdict("RUNNING"), CI_NONE_GRACE_S * 10),
    False,
)

# A completed job cannot un-complete, so both decided states settle at once.
check("PASSED -> settled immediately", ci_settled(CIVerdict("PASSED"), 0), True)
check("FAILED -> settled immediately", ci_settled(CIVerdict("FAILED"), 0), True)

# The regression this function exists for: NONE inside the grace window is a
# run Gitea may not have created yet, and reading it as settled next to an
# already-decided review returns a false all-clear.
check("NONE inside grace -> not settled", ci_settled(CIVerdict("NONE"), 0), False)
check(
    "NONE just short of the grace -> not settled",
    ci_settled(CIVerdict("NONE"), CI_NONE_GRACE_S - 1),
    False,
)
check(
    "NONE at the grace -> settled",
    ci_settled(CIVerdict("NONE"), CI_NONE_GRACE_S),
    True,
)
check(
    "NONE past the grace -> settled",
    ci_settled(CIVerdict("NONE"), CI_NONE_GRACE_S + 1),
    True,
)

# The grace is only worth having if it is long enough to cover the gap
# between a push and Gitea creating the run. A grace shorter than one poll
# interval would be no grace at all: the first NONE reading would already be
# past it.
assert CI_NONE_GRACE_S >= 60, "grace must outlast the default 30s poll interval"
print("  ok  grace outlasts the default poll interval")

# The loop measures the grace from when it started watching the head, never
# from the head commit's date -- an authored-long-before-pushed commit would
# read as already settled. Guard the wiring, not just the pure function.
loop_src = inspect.getsource(poll_review.main)
assert "ci_settled(ci_verdict, time.monotonic() - watching_since)" in loop_src, (
    "ci_done must come from ci_settled measured against watching_since"
)
assert "watching_since = time.monotonic()" in loop_src, (
    "watching_since must reset on head move, like the deadline"
)
assert loop_src.count("watching_since = time.monotonic()") == 2, (
    "watching_since is set once at start and once on head move"
)
assert "commit_date" not in loop_src.split("ci_settled")[1][:200], (
    "the grace must not be dated off the head commit"
)
print("  ok  grace is dated off watching_since, and resets on head move")

# Inline comments are fetched once per review id. Re-fetching them every
# interval while CI keeps the loop open is pure waste, but the cache must be
# keyed by id so a genuinely newer review at the same head is still fetched.
assert "inline_cache" in loop_src, "inline comments must be cached per review id"
assert "if verdict.review_id not in inline_cache:" in loop_src, (
    "the inline fetch must be guarded by the cache"
)
print("  ok  inline comments fetched once per review id")

print("ci_jobs transport:")


def with_api(fake, **kwargs):
    """Run ci_jobs against a stubbed poll_review.api.

    Same reasoning as with_urlopen: ci_jobs' whole contract is that a
    transport failure or an unmatched run/job degrades to [], never to a
    verdict, and that cannot be exercised through classify_ci alone."""
    real = poll_review.api
    poll_review.api = fake
    try:
        return poll_review.ci_jobs("owner/name", HEAD, "tok", **kwargs)
    finally:
        poll_review.api = real


def runs_then_jobs(runs_body, jobs_body=None):
    def fake(path, tok):
        if "actions/runs?" in path:
            return runs_body
        if "/jobs" in path:
            return jobs_body
        raise AssertionError(f"unexpected path {path}")

    return fake


check(
    "no workflow_runs at all -> []",
    with_api(runs_then_jobs({"workflow_runs": []})),
    [],
)
check(
    "a run for a different workflow file, filter set -> []",
    with_api(
        runs_then_jobs(
            {"workflow_runs": [{"id": 1, "path": "ci.yml@refs/heads/main"}]}
        ),
        workflow_file="quality-gate.yml",
    ),
    [],
)

matching_run = {
    "workflow_runs": [{"id": 7, "path": "quality-gate.yml@refs/pull/1/head"}]
}
check(
    "matching run but no 'gate' job, filter set -> []",
    with_api(
        runs_then_jobs(
            matching_run, {"jobs": [{"id": 100, "name": "changes"}]}
        ),
        workflow_file="quality-gate.yml",
        job_name="gate",
    ),
    [],
)

gate_job = {"id": 200, "name": "gate", "status": "in_progress"}
jobs_with_gate = {"jobs": [{"id": 100, "name": "changes"}, gate_job]}
check(
    "matching run, gate job found, filter set -> that job, tagged with its workflow",
    with_api(
        runs_then_jobs(matching_run, jobs_with_gate),
        workflow_file="quality-gate.yml",
        job_name="gate",
    ),
    [{**gate_job, "_workflow": "quality-gate.yml"}],
)
check(
    "no filters -> every job in the run, not just 'gate'",
    with_api(runs_then_jobs(matching_run, jobs_with_gate)),
    [
        {"id": 100, "name": "changes", "_workflow": "quality-gate.yml"},
        {**gate_job, "_workflow": "quality-gate.yml"},
    ],
)


def two_workflow_runs(path, tok):
    if "actions/runs?" in path:
        return {
            "workflow_runs": [
                {"id": 5, "path": "tests.yml@refs/pull/1/head"},
                {"id": 9, "path": "eval.yml@refs/pull/1/head"},
            ]
        }
    if "/runs/5/jobs" in path:
        return {"jobs": [{"id": 50, "name": "test", "status": "completed", "conclusion": "success"}]}
    if "/runs/9/jobs" in path:
        return {"jobs": [{"id": 90, "name": "eval", "status": "completed", "conclusion": "success"}]}
    raise AssertionError(f"unexpected path {path}")


check(
    "no filters, two workflow files at head -> jobs from both, tagged",
    with_api(two_workflow_runs),
    [
        {"id": 50, "name": "test", "status": "completed", "conclusion": "success", "_workflow": "tests.yml"},
        {"id": 90, "name": "eval", "status": "completed", "conclusion": "success", "_workflow": "eval.yml"},
    ],
)


def one_run_fails_to_fetch_jobs(path, tok):
    if "actions/runs?" in path:
        return {
            "workflow_runs": [
                {"id": 5, "path": "tests.yml@refs/pull/1/head"},
                {"id": 9, "path": "eval.yml@refs/pull/1/head"},
            ]
        }
    if "/runs/5/jobs" in path:
        raise OSError("boom")
    if "/runs/9/jobs" in path:
        return {"jobs": [{"id": 90, "name": "eval", "status": "completed", "conclusion": "success"}]}
    raise AssertionError(f"unexpected path {path}")


check(
    "one run's jobs call fails -> the other run's jobs still come back",
    with_api(one_run_fails_to_fetch_jobs),
    [{"id": 90, "name": "eval", "status": "completed", "conclusion": "success", "_workflow": "eval.yml"}],
)


def raises_transport_error(path, tok):
    raise OSError("boom")


check("transport failure listing runs -> [], not a crash", with_api(raises_transport_error), [])

print("agent state:")


def state(verdict, reason=None, *, retryable=False, sha=None):
    """One /state/{owner}/{repo}/{pr}?sha= response, trimmed to what classify
    reads. outcomes is newest-first and `verdict` is derived from outcomes[0],
    so the two always agree here too."""
    outcomes = (
        [{"reason": reason, "retryable": retryable, "outcome": verdict, "sha": sha}]
        if reason
        else []
    )
    return {"verdict": verdict, "live": None, "outcomes": outcomes}


# Gitea stays the source of truth for review CONTENT: a completed review at head
# cannot be demoted by whatever the agent's own memory says.
check(
    "review at head beats a terminal agent verdict",
    classify(
        HEAD, [review(HEAD)], [], EPOCH, BOT, state("declined", "superseded")
    ).state,
    "REVIEWED",
)

# ...and so does anything the bot actually posted on the PR. The endpoint speaks
# only where Gitea is silent.
check(
    "posted warning beats a terminal agent verdict",
    classify(
        HEAD,
        [],
        [comment("⚠️ Automated review failed; see service logs.")],
        EPOCH,
        BOT,
        state("declined", "nothing_new_since_last_review"),
    ).state,
    "FAILED",
)

# The headline case: a re-push the agent found nothing new in. Today this waits
# out the whole budget and reports TIMED_OUT.
v = classify(
    HEAD, [], [], EPOCH, BOT, state("declined", "nothing_new_since_last_review")
)
check("agent decline -> DECLINED", v.state, "DECLINED")
assert "nothing_new_since_last_review" in v.detail, "DECLINED detail names the reason"
print("  ok  DECLINED detail names the reason")

# A stale review plus a decline on the new head must stop the wait, not sit in
# STALE until the deadline.
check(
    "agent decline outranks STALE",
    classify(
        HEAD,
        [review(OLD)],
        [],
        EPOCH,
        BOT,
        state("declined", "nothing_new_since_last_review"),
    ).state,
    "DECLINED",
)

# `retryable` is advice about whether a fresh request could work, not a claim
# that an attempt is in flight: _run_review_bounded exhausts every attempt
# inside one job and the queue does not retry, so while the agent is still
# trying the endpoint answers `in_progress`. A record existing at all means it
# has stopped.
v = classify(
    HEAD, [], [], EPOCH, BOT, state("failed", "backend_error", retryable=True)
)
check("retryable agent failure still ends the wait", v.state, "FAILED")
assert "fresh review request" in v.detail, "retryable detail says a new request may work"
print("  ok  retryable detail says a new request may work")
check(
    "permanent agent failure -> FAILED",
    classify(HEAD, [], [], EPOCH, BOT, state("failed", "backend_quota")).state,
    "FAILED",
)

# One input, one exit code: a size decline reports as SKIPPED whether the poller
# learned it from the agent's comment or from the endpoint.
check(
    "a size decline reports as SKIPPED",
    classify(HEAD, [], [], EPOCH, BOT, state("declined", "diff_too_many_lines")).state,
    "SKIPPED",
)
check(
    "a lost job -> DECLINED",
    classify(HEAD, [], [], EPOCH, BOT, state("lost", "lost_on_restart")).state,
    "DECLINED",
)

# in_progress, posted and unknown are not answers -- keep looking at Gitea.
for verdict_name in ("in_progress", "posted", "unknown"):
    check(
        f"{verdict_name} keeps waiting",
        classify(HEAD, [], [], EPOCH, BOT, state(verdict_name)).state,
        "PENDING",
    )

# An unreachable agent is exactly the pre-change code path.
check(
    "no agent state -> pre-change behaviour",
    classify(HEAD, [], [], EPOCH, BOT, None).state,
    "PENDING",
)
check("DECLINED has its own exit code", EXIT["DECLINED"], 6)

# The oversized-prompt warning: a fourth terminal comment the poller did not
# know about, so it read as silence and burned the whole budget.
v = classify(
    HEAD,
    [],
    [
        comment(
            "⚠️ This pull request is too large for the reviewer to accept, so it "
            "was not reviewed."
        )
    ],
    EPOCH,
    BOT,
)
check("oversized warning -> FAILED", v.state, "FAILED")
assert v.detail.startswith("[oversized]"), "oversized detail names the variant"
print("  ok  oversized detail names the variant")

# A response this poller cannot read is a response it did not get. None of
# these may raise: an exception mid-wait ends the babysit as surely as a wrong
# verdict does.
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
    check(
        f"{label} -> keeps waiting",
        classify(HEAD, [], [], EPOCH, BOT, bad).state,
        "PENDING",
    )

# An outcome answering for a different commit is the STALE case, not an answer.
check(
    "outcome for another sha -> keeps waiting",
    classify(HEAD, [], [], EPOCH, BOT, state("declined", "superseded", sha=OLD)).state,
    "PENDING",
)
check(
    "outcome for this sha answers",
    classify(
        HEAD, [], [], EPOCH, BOT, state("declined", "superseded", sha=HEAD)
    ).state,
    "DECLINED",
)
# ...but a decline filed before the head sha was known still counts.
check(
    "outcome with no sha still answers",
    classify(HEAD, [], [], EPOCH, BOT, state("declined", "bot_authored_pr")).state,
    "DECLINED",
)

# Every terminal state classify can return needs an exit code AND a branch in
# main, or the poller would keep polling something it has already decided.
main_src = inspect.getsource(poll_review.main)
for terminal in ("REVIEWED", "SKIPPED", "FAILED", "DECLINED"):
    assert terminal in EXIT, f"{terminal} has no exit code"
    assert f'"{terminal}"' in main_src, f"{terminal} is not handled in main"
print("  ok  every terminal state has an exit code and a branch in main")

print("CI fail-fast:")

# FAILED must stay terminal-on-sight for the early return to be sound: if
# classify_ci ever checked running before failed, a failed job could report
# RUNNING and the bail-out would fire on a verdict that is not final.
check(
    "a failed job outranks a still-running one",
    classify_ci(
        [
            {"name": "a", "status": "completed", "conclusion": "failure"},
            {"name": "b", "status": "in_progress", "conclusion": None},
        ],
        NOW,
    ).state,
    "FAILED",
)

# The early return happens mid-wait, when neither --once nor the deadline has
# fired. Those two are what normally trigger the STALE inline fetch, so
# ci_failed_early has to join them or bailing on a CI failure would silently
# drop a stale review's findings -- the previous-head trap by another route.
assert "ci_failed_early" in main_src, "fail-fast branch is gone from main"
_stale_expr = main_src.split("rendering_stale = ")[1].split(")")[0]
assert "ci_failed_early" in _stale_expr, (
    "ci_failed_early must gate the STALE inline fetch, else the early return "
    f"swallows findings; got: {_stale_expr!r}"
)
print("  ok  a CI-failure bail-out still fetches STALE findings")

# ci_verdict feeds ci_failed_early feeds rendering_stale -- an edit that moves
# the CI poll back below the fetch would raise NameError only on the CI-failed
# path, which is exactly the path least likely to be exercised by hand.
assert main_src.index("ci_verdict = classify_ci") < main_src.index(
    "rendering_stale = "
), "classify_ci must run before rendering_stale is computed"
print("  ok  CI is classified before the STALE fetch decision")

# The docstring's standing rule: CI status is reported, never folded into the
# exit code. The bail-out returns the *review's* code, so a grep for a
# CI-specific exit here is a regression, not a feature.
_ff = main_src.split("if ci_failed_early:")[1].split("return ")[1].split("\n")[0]
assert _ff.startswith("EXIT.get(verdict.state"), (
    f"fail-fast must return the review verdict's code, not a CI one; got {_ff!r}"
)
print("  ok  fail-fast returns the review's exit code, not a CI-specific one")

assert "--no-fail-fast" in inspect.getsource(poll_review.main), "opt-out flag is gone"
print("  ok  --no-fail-fast opt-out exists")

print("api_paged:")

# The reviews that matter are the newest, and Gitea returns oldest first at 50
# per page. milex-scopeline-server#14 reached 78 reviews and the unpaginated
# call reported review 267 as the latest while 304-318 sat unseen on page 2.
_calls: list[str] = []


def _fake_api(pages):
    def _api(path, tok):
        _calls.append(path)
        n = int(path.split("page=")[1].split("&")[0])
        return pages[n - 1] if n <= len(pages) else []

    return _api


_real_api = poll_review.api
try:
    poll_review.api = _fake_api([[{"id": i} for i in range(50)], [{"id": 50}]])
    got = poll_review.api_paged("/x", "t")
    check("  walks past the first full page", len(got), 51)
    check("  stops on the short page", len(_calls), 2)
    assert "page=1&limit=50" in _calls[0] and "page=2" in _calls[1], _calls
    print("  ok  asks for page 1 then page 2")

    _calls.clear()
    poll_review.api = _fake_api([[{"id": i} for i in range(50)]])
    check("  empty page ends the walk", len(poll_review.api_paged("/x", "t")), 50)
    check("  ...and costs one extra call", len(_calls), 2)

    _calls.clear()
    poll_review.api = _fake_api([[{"id": 1}]])
    poll_review.api_paged("/x?state=open", "t")
    assert _calls[0] == "/x?state=open&page=1&limit=50", _calls
    print("  ok  appends with & when the path already has a query")

    # An error object where a list was expected must not raise mid-poll.
    poll_review.api = lambda path, tok: {"message": "nope"}
    check("  a non-list page degrades to []", poll_review.api_paged("/x", "t"), [])
finally:
    poll_review.api = _real_api

# The call sites that actually needed it.
main_src_p = inspect.getsource(poll_review.main)
for endpoint in ("/reviews", "/comments"):
    assert f'api_paged(f"/repos/{{repo}}/pulls/{{pr_num}}{endpoint}"' in main_src_p or (
        f"{endpoint}\", tok)" in main_src_p and "api_paged" in main_src_p
    ), f"{endpoint} is not paged in main"
assert "api(f\"/repos/{repo}/pulls/{pr_num}/reviews\"" not in main_src_p, main_src_p
print("  ok  main pages the reviews and comments lists")

print("agent_state transport:")


class _FakeResponse(io.BytesIO):
    """urlopen's result is used as a context manager; BytesIO is not one."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def returns(body: bytes):
    def fake(req, timeout=None):
        return _FakeResponse(body)

    return fake


def raises(exc):
    def fake(req, timeout=None):
        raise exc

    return fake


def with_urlopen(fake):
    """Run agent_state against a stubbed urlopen.

    Its contract -- every failure degrades to None, never to a verdict -- is
    the one this whole script leans on hardest, and it cannot be reached
    through classify()."""
    real = poll_review.urllib.request.urlopen
    poll_review.urllib.request.urlopen = fake
    try:
        return poll_review.agent_state("owner/name", 4, HEAD)
    finally:
        poll_review.urllib.request.urlopen = real


check(
    "a JSON object comes back as a dict",
    with_urlopen(returns(b'{"verdict":"unknown"}')),
    {"verdict": "unknown"},
)
for label, fake in [
    ("connection refused", raises(OSError("refused"))),
    ("401", raises(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))),
    ("500", raises(urllib.error.HTTPError("u", 500, "Server Error", {}, None))),
    ("timeout", raises(TimeoutError())),
    ("non-JSON body", returns(b"<html>nope</html>")),
    ("a JSON list", returns(b"[1,2,3]")),
    ("a JSON string", returns(b'"nope"')),
]:
    check(f"{label} -> None", with_urlopen(fake), None)

# The slash inside owner/name is a path separator, not an escape.
seen: list[str] = []


def capture(req, timeout=None):
    seen.append(req.full_url)
    return _FakeResponse(b"{}")


with_urlopen(capture)
assert seen[0].endswith(f"/state/owner/name/4?sha={HEAD}"), seen[0]
print("  ok  builds /state/{owner}/{repo}/{pr}?sha=")

print("\nall passed")
