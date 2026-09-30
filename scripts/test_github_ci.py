#!/usr/bin/env python3
"""Tests for GitHub CI-only babysitting. Run: python3 test_github_ci.py [-k C5:]

Written from .specs/features/github-ci/checks.md: each section is named after
the check it proves. `-k` runs only the sections whose title contains the
pattern. Same harness as test_poll_review.py: plain Python, a failing check is
recorded rather than raised, and exits 1 if anything failed.
"""

import contextlib
import http.client
import io
import json
import os
import re
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from pathlib import Path

import github_ci
import poll_review

HEAD = "a7c5d6c7" + "0" * 32
NEW_HEAD = "c0ffee00" + "0" * 32
BASE = "ba5e0001" + "0" * 32
MOVED = "ba5e0002" + "0" * 32
TOKEN = "SECRET-TOKEN-123"
SKILL_DIR = Path(__file__).resolve().parent.parent

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


@contextlib.contextmanager
def environ(**values):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def http_error(code, reason="x", headers=None):
    return urllib.error.HTTPError("u", code, reason, headers or {}, None)  # pyright: ignore[reportArgumentType]


def forbidden(what):
    def call(*_a, **_k):
        raise AssertionError(f"{what} must not be called on the GitHub path")

    return call


def cr(name, status="completed", conclusion="success", *, rid=1, app=15368, suite=100, url=None):
    """One check run as GET .../check-runs lists it. `suite=None` leaves out
    `check_suite`."""
    run = {"id": rid, "name": name, "status": status, "conclusion": conclusion,
           "app": {"id": app, "slug": "github-actions", "name": "GitHub Actions"},
           "html_url": url or f"https://github.com/o/r/runs/{rid}",
           "details_url": f"https://github.com/o/r/runs/{rid}/details"}
    if suite is not None:
        run["check_suite"] = {"id": suite}
    return run


def st(context, state="success", *, sid=1, url=None):
    """One commit status as GET .../statuses lists it."""
    return {"id": sid, "context": context, "state": state,
            "target_url": url or f"https://ci.example.org/{context}/{sid}"}


PASSED = [cr("test")]
RUNNING = [cr("test", "in_progress", None)]
FAILED = [cr("test", "completed", "failure")]


class FakeGitHub:
    """Answers github_ci.api by path, as (body, headers). Callables take the
    round `n` (counts PR fetches, 1 is the first) and the sha asked about.
    Lists are paged at `page_size`, with a `link` header pointing at a host
    the code must never follow."""

    def __init__(self, *, head=None, pr=None, runs=None, statuses=None,
                 target=None, fail=None, page_size=100):
        self.head = head or (lambda n: HEAD)
        self.pr = pr or (lambda n: {})
        self.runs = runs or (lambda n, sha: PASSED)
        self.statuses = statuses or (lambda n, sha: [])
        self.target = target or (lambda n: BASE)
        self.fail = fail or (lambda path, n: None)
        self.page_size = page_size
        self.n = 0
        self.calls: list[str] = []

    def _page(self, items, query):
        page = int(query.get("page", "1"))
        size = self.page_size
        chunk = items[(page - 1) * size: page * size]
        headers = {}
        if page * size < len(items):
            headers["link"] = f'<https://evil.example/next?page={page + 1}>; rel="next"'
        return chunk, headers

    def __call__(self, path, tok):
        self.calls.append(path)
        base, _, query = path.partition("?")
        q = dict(urllib.parse.parse_qsl(query))
        if base == "/repos/o/r/pulls/7":
            self.n += 1
        if (exc := self.fail(base, self.n)) is not None:
            raise exc
        if base == "/repos/o/r/pulls/7":
            return ({"number": 7, "state": "open", "merged": False, "mergeable": True,
                     "head": {"sha": self.head(self.n), "ref": "feat",
                              "repo": {"full_name": "o/r"}},
                     "base": {"ref": "main", "repo": {"full_name": "o/r"}},
                     **self.pr(self.n)}, {})
        if base == "/repos/o/r/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"sha": self.target(self.n), "type": "commit"}}, {}
        if m := re.fullmatch(r"/repos/o/r/commits/(\w+)/check-runs", base):
            runs = self.runs(self.n, m.group(1))
            if not isinstance(runs, list):
                return runs, {}
            chunk, headers = self._page(runs, q)
            return {"total_count": len(runs), "check_runs": chunk}, headers
        if m := re.fullmatch(r"/repos/o/r/commits/(\w+)/statuses", base):
            return self._page(self.statuses(self.n, m.group(1)), q)
        raise AssertionError(f"unexpected path {path}")


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def time(self):
        return 1_800_000_000.0 + self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def status_of(exc: SystemExit) -> int:
    return exc.code if isinstance(exc.code, int) else 1


def run_gh(fake, *args, repo_args=("--repo", "o/r", "--pr", "7"), provider=("--provider", "github")):
    """main() on the GitHub path against a fake API and clock:
    (exit code, stdout + exit message, clock). Every Gitea entry point raises."""
    clock = FakeClock()
    buf = io.StringIO()
    with patched(github_ci, api=fake, time=clock, github_token=lambda: TOKEN,
                 API_TIMEOUT_S=github_ci.API_TIMEOUT_S), \
            patched(poll_review, token=forbidden("gitea token()"), agent_state=forbidden("agent_state"),
                    health=forbidden("health"), api=forbidden("gitea api"),
                    _git=lambda *a: None, CI_WORKFLOW_FILE=None, CI_JOB_NAME=None), \
            contextlib.redirect_stdout(buf):
        try:
            code = poll_review.main([*provider, *repo_args,
                                     "--timeout-minutes", "5", "--interval", "30", *args])
        except SystemExit as exc:
            code = status_of(exc)
            print(f"[exit] {exc.code}")
    return code, buf.getvalue(), clock


# ------------------------------------------------------------------- checks


@section("C1: GitHub path reads only GitHub, only GET")
def _():
    fake = FakeGitHub(statuses=lambda n, sha: [st("ci/jenkins")])
    sent: list[urllib.request.Request] = []

    def urlopen(req, timeout):
        sent.append(req)
        path = req.full_url.removeprefix(github_ci.API_ROOT)
        body, headers = fake(path, "unused")
        resp = io.BytesIO(json.dumps(body).encode())
        resp.headers = headers  # type: ignore[attr-defined]
        return contextlib.nullcontext(resp)

    clock = FakeClock()
    buf = io.StringIO()
    with patched(github_ci, _urlopen=urlopen, time=clock, github_token=lambda: TOKEN), \
            patched(poll_review, token=forbidden("gitea token()"), agent_state=forbidden("agent_state"),
                    health=forbidden("health"), api=forbidden("gitea api"), _git=lambda *a: None,
                    CI_WORKFLOW_FILE=None, CI_JOB_NAME=None), \
            contextlib.redirect_stdout(buf):
        code = poll_review.main(["--provider", "github", "--repo", "o/r", "--pr", "7", "--once"])
    out = buf.getvalue()
    check("runs to a verdict without touching Gitea", code, 11)
    ok("  every request is a GET", sent and all(r.get_method() == "GET" for r in sent), sent)
    ok("  every request goes to https://api.github.com/repos/o/r/",
       all(r.full_url.startswith("https://api.github.com/repos/o/r/") for r in sent),
       [r.full_url for r in sent])
    ok("  no review or comment endpoint is read",
       not any("/reviews" in r.full_url or "/comments" in r.full_url for r in sent),
       [r.full_url for r in sent])
    ok("  reads the PR, check runs, statuses and target ref",
       {p for p in ("/pulls/7", "/check-runs", "/statuses", "/git/ref/heads/main")
        if any(p in r.full_url for r in sent)} == {"/pulls/7", "/check-runs", "/statuses", "/git/ref/heads/main"},
       [r.full_url for r in sent])
    ok("  the output names no Gitea bot", "review-bot" not in out, out)


GITEA = "https://gitea.example.com"
REMOTES = {
    "origin-gitea": "origin\thttps://gitea.example.com/me/app.git (fetch)\n",
    "origin-github": "origin\tgit@github.com:me/app.git (fetch)\n",
    "gh": "gh\thttps://github.com/me/app.git (fetch)\n",
    "gt": "gt\thttps://gitea.example.com/me/app.git (fetch)\n",
    "up": "up\thttps://github.com/up/app.git (fetch)\n",
    "lab": "lab\thttps://gitlab.com/me/app.git (fetch)\n",
}


def choose(names, tracking=None, repo=None):
    remotes = None if names is None else "".join(REMOTES[n] for n in names)
    with patched(poll_review, BASE_URL=GITEA):
        try:
            return poll_review.choose_provider(remotes, tracking, repo)
        except SystemExit as exc:
            return f"exit {status_of(exc)}: {exc.code}"


@section("C2: provider auto-selection")
def _():
    check("tracking remote's provider wins over origin",
          choose(["origin-gitea", "gh"], tracking="gh"), "github")
    check("tracking remote on gitea wins over a github origin",
          choose(["origin-github", "gt"], tracking="gt"), "gitea")
    check("no tracking remote: origin's provider", choose(["origin-gitea", "gh"]), "gitea")
    check("no tracking remote: a github origin", choose(["origin-github", "gt"]), "github")
    check("tracking on an unsupported host falls back to origin",
          choose(["origin-github", "lab"], tracking="lab"), "github")
    check("only github.com remotes -> github", choose(["gh", "up"]), "github")
    check("only Gitea remotes -> gitea", choose(["gt"]), "gitea")
    amb = choose(["gt", "gh"])
    ok("remotes on both, no tracking or origin -> exit 1 ambiguous",
       amb.startswith("exit 1") and "ambiguous" in amb and "--provider" in amb, amb)
    none = choose(["lab"])
    ok("no supported remote -> exit 1", none.startswith("exit 1") and "--provider" in none, none)
    check("not a checkout -> legacy gitea", choose(None, repo="me/app"), "gitea")
    check("explicit repo on a github remote only -> github", choose(["gt", "up"], repo="up/app"), "github")
    check("explicit repo on no remote -> legacy gitea", choose(["gh"], repo="some/other"), "gitea")
    amb = choose(["gt", "gh"], repo="me/app")
    ok("explicit repo on remotes of both -> exit 1 ambiguous",
       amb.startswith("exit 1") and "ambiguous" in amb, amb)

    # End to end: explicit repo and PR outside a checkout take the Gitea path.
    reached = []

    def gitea_token():
        reached.append("gitea")
        raise SystemExit("stop: gitea token read")

    for argv, label in (
        (["--repo", "o/r", "--pr", "7", "--once"], "auto, outside a checkout"),
        (["--provider", "gitea", "--repo", "me/app", "--pr", "7", "--once"], "--provider gitea in a github checkout"),
    ):
        reached.clear()
        remotes = REMOTES["origin-github"]
        git = (lambda *a: None) if label.startswith("auto") else \
            (lambda *a: remotes if a[:2] == ("remote", "-v") else None)
        with patched(poll_review, token=gitea_token, _git=git), \
                patched(github_ci, github_token=forbidden("github token")), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                poll_review.main(argv)
            except SystemExit:
                pass
        check(f"{label} -> Gitea path", reached, ["gitea"])

    # And auto in a checkout whose only remote is github.com reaches GitHub.
    buf = io.StringIO()
    with patched(poll_review, BASE_URL=GITEA, token=forbidden("gitea token()"),
                 CI_WORKFLOW_FILE=None, CI_JOB_NAME=None,
                 _git=lambda *a: "origin\thttps://github.com/o/r.git (fetch)\n"
                 if a[:2] == ("remote", "-v") else None), \
            patched(github_ci, api=FakeGitHub(), time=FakeClock(), github_token=lambda: TOKEN), \
            contextlib.redirect_stdout(buf):
        code = poll_review.main(["--repo", "o/r", "--pr", "7", "--once"])
    check("auto with a github.com checkout -> GitHub path", code, 11)


def pulls_fake(prs_by_repo):
    calls = []

    def api(path, tok):
        calls.append(path)
        base, _, query = path.partition("?")
        m = re.fullmatch(r"/repos/([\w.-]+/[\w.-]+)/pulls", base)
        if not m:
            raise AssertionError(f"unexpected path {path}")
        q = dict(urllib.parse.parse_qsl(query))
        assert q.get("state") == "open", path
        # Ignores ?head= on purpose: the code must check head.repo itself.
        return (prs_by_repo.get(m.group(1), []) if q.get("page", "1") == "1" else []), {}

    return api, calls


def pull(number, head_repo, ref="feat"):
    return {"number": number, "head": {"ref": ref, "repo": {"full_name": head_repo} if head_repo else None}}


def infer(prs_by_repo, repos, ours, branch="feat"):
    api, calls = pulls_fake(prs_by_repo)
    with patched(github_ci, api=api):
        try:
            return github_ci.infer_pr(repos, ours, branch, TOKEN), calls
        except SystemExit as exc:
            return f"exit {status_of(exc)}: {exc.code}", calls


@section("C3: GitHub PR inference")
def _():
    got, calls = infer({"up/app": [pull(12, "me/app"), pull(13, "stranger/app")]},
                       ["me/app", "up/app"], ["me/app", "up/app"])
    check("fork PR in the upstream, head repo a checkout remote", got, ("up/app", 12))
    ok("  asks GitHub with a head filter", any("head=me%3Afeat" in c or "head=me:feat" in c for c in calls), calls)

    got, _ = infer({"up/app": [pull(13, "stranger/app")]}, ["me/app", "up/app"], ["me/app", "up/app"])
    ok("same-named branch from a repo that is not ours is ignored -> exit 1",
       isinstance(got, str) and got.startswith("exit 1") and "--repo" in got, got)

    got, _ = infer({"up/app": [pull(12, "me/app"), pull(13, "stranger/app", ref="other")]},
                   ["up/app"], ["me/app", "up/app"])
    check("another branch in the same repo is ignored", got, ("up/app", 12))

    got, _ = infer({"up/app": [pull(12, "me/app")], "me/app": [pull(3, "me/app")]},
                   ["me/app", "up/app"], ["me/app", "up/app"])
    ok("two matching open PRs -> exit 1 naming --repo and --pr",
       isinstance(got, str) and got.startswith("exit 1") and "--repo" in got and "--pr" in got, got)

    got, _ = infer({"up/app": [pull(12, None)]}, ["up/app"], ["me/app", "up/app"])
    ok("a PR whose head repo was deleted is not ours",
       isinstance(got, str) and got.startswith("exit 1"), got)

    got, _ = infer({}, ["me/app"], ["me/app"])
    ok("no match -> exit 1", isinstance(got, str) and got.startswith("exit 1") and "--pr" in got, got)

    got, calls = infer({}, ["me/app"], ["me/app"], branch="HEAD")
    ok("detached HEAD -> exit 1 without asking GitHub",
       isinstance(got, str) and got.startswith("exit 1") and not calls, got)


@section("C4: review options are rejected on GitHub")
def _():
    code, out, _ = run_gh(FakeGitHub(), "--once")
    ok("prints review: NOT_MONITORED", "review: NOT_MONITORED" in out, out)
    for extra, name, env in (
        (["--wait-for", "review"], "--wait-for review", {}),
        (["--wait-for", "both"], "--wait-for both", {}),
        (["--no-fail-fast"], "--no-fail-fast", {}),
        (["--full"], "--full", {}),
        ([], "CI_WORKFLOW_FILE", {"CI_WORKFLOW_FILE": "ci.yml"}),
        ([], "CI_JOB_NAME", {"CI_JOB_NAME": "test"}),
    ):
        fake = FakeGitHub()
        token_reads = []
        buf = io.StringIO()
        with patched(github_ci, api=fake, time=FakeClock(),
                     github_token=lambda: token_reads.append(1) or TOKEN), \
                patched(poll_review, token=forbidden("gitea token()"), _git=lambda *a: None,
                        CI_WORKFLOW_FILE=env.get("CI_WORKFLOW_FILE"), CI_JOB_NAME=env.get("CI_JOB_NAME")), \
                contextlib.redirect_stdout(buf):
            try:
                poll_review.main(["--provider", "github", "--repo", "o/r", "--pr", "7", "--once", *extra])
                got = "no exit"
            except SystemExit as exc:
                got = f"exit {status_of(exc)}: {exc.code}"
        ok(f"{name} -> exit 1 naming it", got.startswith("exit 1") and name in got, got)
        ok(f"  {name}: before any token or API call", not token_reads and not fake.calls,
           (token_reads, fake.calls))
    for mode in ("ci", "any"):
        code, out, _ = run_gh(FakeGitHub(), "--once", "--wait-for", mode)
        check(f"--wait-for {mode} is accepted", code, 11)


@section("C5: check-run and status classification")
def _():
    table = [
        (cr("x", "queued", None), "RUNNING"),
        (cr("x", "in_progress", None), "RUNNING"),
        (cr("x", "waiting", None), "RUNNING"),
        (cr("x", "requested", None), "RUNNING"),
        (cr("x", "pending", None), "RUNNING"),
        (cr("x", "completed", "success"), "PASSED"),
        (cr("x", "completed", "neutral"), "PASSED"),
        (cr("x", "completed", "skipped"), "PASSED"),
        (cr("x", "completed", "failure"), "FAILED"),
        (cr("x", "completed", "cancelled"), "FAILED"),
        (cr("x", "completed", "timed_out"), "FAILED"),
        (cr("x", "completed", "action_required"), "FAILED"),
        (cr("x", "completed", "stale"), "FAILED"),
        (cr("x", "completed", "startup_failure"), "FAILED"),
        (cr("x", "completed", None), "UNKNOWN"),
        (cr("x", "completed", "weird_new_conclusion"), "UNKNOWN"),
        (cr("x", "mystery_status", None), "UNKNOWN"),
        ("not an object", "UNKNOWN"),
    ]
    for run, want in table:
        label = run if isinstance(run, str) else f"{run['status']}/{run['conclusion']}"
        check(f"check run {label} -> {want}", github_ci.check_run_state(run)[0], want)
    for state, want in (("pending", "RUNNING"), ("success", "PASSED"), ("failure", "FAILED"),
                        ("error", "FAILED"), ("bogus", "UNKNOWN")):
        check(f"status {state} -> {want}", github_ci.status_state(st("c", state))[0], want)
    check("status that is not an object -> UNKNOWN", github_ci.status_state(None)[0], "UNKNOWN")

    V = github_ci.classify
    check("no results -> NONE", V([], []).state, "NONE")
    check("any FAILED wins", V([cr("a"), cr("b", "in_progress", None)], [st("c", "error")]).state, "FAILED")
    check("RUNNING over UNKNOWN", V([cr("a", "in_progress", None), cr("b", "completed", None, rid=2)], []).state, "RUNNING")
    check("UNKNOWN keeps it from passing", V([cr("a"), cr("b", "completed", None, rid=2)], []).state, "UNKNOWN")
    check("all successful -> PASSED", V([cr("a"), cr("b", "completed", "skipped", rid=2)], [st("c")]).state, "PASSED")


@section("C6: newest result per identity")
def _():
    V = github_ci.classify
    check("a rerun (higher id, same app and name) replaces an older failure",
          V([cr("test", "completed", "failure", rid=1), cr("test", "completed", "success", rid=2)], []).state,
          "PASSED")
    check("  in either listing order",
          V([cr("test", "completed", "success", rid=2), cr("test", "completed", "failure", rid=1)], []).state,
          "PASSED")
    check("the same name from another app is its own result",
          V([cr("test", "completed", "failure", rid=1, app=1), cr("test", "completed", "success", rid=2, app=2)],
            []).state, "FAILED")

    # Two workflows, one job name each: same app and name, distinct suites.
    fail_a = cr("build", "completed", "failure", rid=1, suite=100)
    pass_b = cr("build", "completed", "success", rid=2, suite=200)
    for label, runs in (("failure listed first", [fail_a, pass_b]), ("success listed first", [pass_b, fail_a])):
        ci = V(runs, [])
        check(f"a failed build in another suite is not replaced ({label})", ci.state, "FAILED")
        check(f"  both runs are listed ({label})",
              sorted((r.state, r.link) for r in ci.results),
              [("FAILED", "https://github.com/o/r/runs/1"), ("PASSED", "https://github.com/o/r/runs/2")])
    code, out, _ = run_gh(FakeGitHub(runs=lambda n, sha: [pass_b, fail_a]))
    check("  main() exits 8 CI_FAILED", code, 8)
    ok("  and reports the failed run", "CI_FAILED" in out and "https://github.com/o/r/runs/1" in out, out)

    no_suite_fail = cr("build", "completed", "failure", rid=1, suite=None)
    for label, newer in (("with a suite", cr("build", "completed", "success", rid=2, suite=200)),
                         ("without a suite", cr("build", "completed", "success", rid=2, suite=None))):
        for order in ((no_suite_fail, newer), (newer, no_suite_fail)):
            check(f"a failure without a suite is not replaced by a newer success {label}",
                  V(list(order), []).state, "FAILED")
    check("a success without a suite does not hide an older failure with one",
          V([cr("build", "completed", "failure", rid=1, suite=100),
             cr("build", "completed", "success", rid=2, suite=None)], []).state, "FAILED")
    check("a check_suite without an integer id counts as no suite",
          V([cr("build", "completed", "failure", rid=1, suite="x"),
             cr("build", "completed", "success", rid=2, suite="x")], []).state, "FAILED")
    check("a newer status success replaces an older failure",
          V([], [st("ci/jenkins", "success", sid=9), st("ci/jenkins", "failure", sid=3)]).state, "PASSED")
    check("an older status success does not replace a newer failure",
          V([], [st("ci/jenkins", "failure", sid=9), st("ci/jenkins", "success", sid=3)]).state, "FAILED")
    check("distinct contexts both count",
          V([], [st("a", "success", sid=9), st("b", "failure", sid=3)]).state, "FAILED")


@section("C7: pagination at the exact head sha")
def _():
    runs = [cr(f"job{i}", rid=i) for i in range(1, 4)] + [cr("late", "completed", "failure", rid=9)]
    fake = FakeGitHub(runs=lambda n, sha: runs, statuses=lambda n, sha: [st("a", sid=1), st("b", sid=2), st("c", sid=3)],
                      page_size=2)
    code, out, _ = run_gh(fake, "--once")
    check("a failure only on page 2 exits 8", code, 8)
    ci_calls = [c for c in fake.calls if "/commits/" in c]
    ok("every CI read is at the exact head sha", ci_calls and all(f"/commits/{HEAD}/" in c for c in ci_calls), ci_calls)
    ok("per_page=100 on every CI read", all("per_page=100" in c for c in ci_calls), ci_calls)
    check("check-runs pages requested", [c for c in ci_calls if "check-runs" in c and "page=" in c].__len__(), 2)
    ok("  page 2 requested by our own path, not the link URL",
       any(re.search(r"check-runs\?.*\bpage=2\b", c) for c in ci_calls) and not any("evil" in c for c in fake.calls),
       fake.calls)
    check("statuses pages requested", len([c for c in ci_calls if "/statuses" in c]), 2)

    fake = FakeGitHub(runs=lambda n, sha: [cr("only")], page_size=100)
    run_gh(fake, "--once")
    check("no link header -> exactly one page", len([c for c in fake.calls if "check-runs" in c]), 1)


@section("C8: CI_FAILED and CI_PASSED exits")
def _():
    code, out, _ = run_gh(FakeGitHub(runs=lambda n, sha: FAILED))
    check("a failed check exits 8", code, 8)
    ok("  labelled CI_FAILED", "=== CI_FAILED" in out, out)
    code, out, _ = run_gh(FakeGitHub(runs=lambda n, sha: [cr("a")], statuses=lambda n, sha: [st("ci/x", "error")]))
    check("a failed commit status exits 8", code, 8)
    code, out, _ = run_gh(FakeGitHub(runs=lambda n, sha: [cr("a"), cr("b", "completed", "skipped", rid=2)],
                                     statuses=lambda n, sha: [st("ci/x")]))
    check("all successful exits 11", code, 11)
    ok("  labelled CI_PASSED", "=== CI_PASSED" in out, out)
    ok("  never says REVIEWED", "REVIEWED" not in out, out)
    check("EXIT has CI_PASSED = 11", poll_review.EXIT.get("CI_PASSED"), 11)
    codes = set()
    for runs in (PASSED, FAILED, RUNNING, []):
        for extra in ((), ("--once",)):
            codes.add(run_gh(FakeGitHub(runs=lambda n, sha, r=runs: r), *extra)[0])
    ok("no GitHub outcome exits 0", 0 not in codes, codes)


@section("C9: unreadable CI is never a pass")
def _():
    runs = [cr("a", rid=1), cr("b", rid=2), cr("c", rid=3)]
    fake = FakeGitHub(runs=lambda n, sha: runs, page_size=2,
                      fail=lambda path, n: None)
    orig = fake.__call__

    def flaky(path, tok):
        if "check-runs" in path and re.search(r"\bpage=2\b", path):
            fake.calls.append(path)
            raise TimeoutError("page 2 timed out")
        return orig(path, tok)

    with patched(github_ci, api=flaky, github_token=lambda: TOKEN):
        v = github_ci.read_ci("o/r", HEAD, TOKEN)
    check("a transient failure on page 2 -> UNKNOWN", v.state, "UNKNOWN")
    check("  and not permanent", v.permanent, False)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: http_error(401, "Unauthorized")
                                         if path.endswith("/check-runs") else None))
    check("401 on check runs exits 1", code, 1)
    check("  without sleeping", clock.sleeps, [])
    ok("  and says CI is UNKNOWN, not a pass", "UNKNOWN" in out and "NOT a pass" in out, out)

    for exc, label in (
        (http_error(403, "rate limit exceeded", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}), "403 rate limit"),
        (http_error(429, "Too Many Requests", {"retry-after": "1"}), "429"),
    ):
        code, out, clock = run_gh(FakeGitHub(fail=lambda path, n, e=exc: e
                                             if path.endswith("/check-runs") and n <= 3 else None))
        check(f"{label} is retried and a later success exits 11", code, 11)
        check(f"  {label}: after waiting 3 rounds", len(clock.sleeps), 3)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: urllib.error.URLError("down")
                                         if path.endswith("/statuses") else None))
    check("a statuses failure lasting to the deadline exits 1", code, 1)
    ok("  having waited the budget", sum(clock.sleeps) >= 300, clock.sleeps)
    ok("  reported as not a pass", "NOT a pass" in out, out)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: TimeoutError("slow")
                                         if path.endswith("/check-runs") else None), "--once")
    check("--once with a transient failure exits 1", code, 1)
    check("  with zero sleeps", clock.sleeps, [])

    fake = FakeGitHub(runs=lambda n, sha: ["not", "an", "object"])
    with patched(github_ci, api=lambda p, t: (["a list"], {}) if "check-runs" in p else fake(p, t)):
        v = github_ci.read_ci("o/r", HEAD, TOKEN)
    check("a check-runs body that is not an object -> UNKNOWN", v.state, "UNKNOWN")

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: urllib.error.URLError("down")
                                         if path == "/repos/o/r/pulls/7" else None))
    check("a PR that stays unreachable exits 1", code, 1)
    ok("  labelled UNREACHABLE", "=== UNREACHABLE" in out, out)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: http_error(404, "Not Found")
                                         if path == "/repos/o/r/pulls/7" else None))
    check("a 404 on the PR exits 1 at once", (code, clock.sleeps), (1, []))

    # A body cut short mid-read raises http.client.IncompleteRead, which is
    # neither an OSError nor a URLError.
    def cut(*_a):
        return http.client.IncompleteRead(b"{\"check_ru", 200)

    ok("IncompleteRead is transient", github_ci.is_transient(cut()))
    with patched(github_ci, api=FakeGitHub(fail=lambda path, n: cut()
                                           if path.endswith("/check-runs") else None),
                 github_token=lambda: TOKEN):
        v = github_ci.read_ci("o/r", HEAD, TOKEN)
    check("a truncated check-runs body -> UNKNOWN", (v.state, v.permanent), ("UNKNOWN", False))

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: cut()
                                         if path.endswith("/statuses") and n <= 2 else None))
    check("a truncated statuses body is retried and a later success exits 11", code, 11)
    check("  after waiting 2 rounds", len(clock.sleeps), 2)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: cut()
                                         if path == "/repos/o/r/pulls/7" and n <= 2 else None))
    check("a truncated PR body is retried and a later success exits 11", code, 11)
    ok("  without a traceback", "Traceback" not in out, out)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: cut()
                                         if path == "/repos/o/r/pulls/7" else None))
    check("a PR body truncated to the deadline exits 1", code, 1)
    ok("  labelled UNREACHABLE", "=== UNREACHABLE" in out, out)

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: cut()
                                         if path == "/repos/o/r/pulls/7" else None), "--once")
    check("--once with a truncated PR body exits 1 with zero sleeps", (code, clock.sleeps), (1, []))

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: cut()
                                         if path.endswith("/git/ref/heads/main") else None))
    check("a truncated target-tip body blocks nothing: CI still exits 11", code, 11)

    def resolve_cut(*_a):
        raise cut()

    with patched(github_ci, resolve=resolve_cut):
        code, out, clock = run_gh(FakeGitHub())
    check("a truncated body while finding the PR exits 1", code, 1)
    ok("  saying no verdict was reached", "no verdict reached" in out, out)


@section("C10: waiting, once and closed")
def _():
    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: RUNNING if n <= 2 else PASSED))
    check("RUNNING twice then PASSED exits 11", code, 11)
    check("  after 2 intervals", len(clock.sleeps), 2)

    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: []))
    check("no results through the budget exits 2", code, 2)
    ok("  labelled TIMED_OUT", "=== TIMED_OUT" in out, out)
    ok("  having waited the 5-minute budget", sum(clock.sleeps) >= 300, clock.sleeps)

    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: RUNNING))
    check("RUNNING through the budget exits 2", code, 2)

    for runs, label in ((RUNNING, "RUNNING"), ([], "no results")):
        code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha, r=runs: r), "--once")
        check(f"--once with {label} exits 7", (code, clock.sleeps), (7, []))
        ok(f"  --once {label}: labelled PENDING", "=== PENDING" in out, out)

    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: RUNNING,
                                         pr=lambda n: {"state": "closed", "mergeable": None}))
    check("a closed PR with RUNNING exits 7 without sleeping", (code, clock.sleeps), (7, []))

    # No sleep outlasts the budget, whatever the interval or GitHub asks for.
    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: RUNNING),
                              "--timeout-minutes", "1", "--interval", "400")
    check("a 1-minute budget with a 400s interval times out", code, 2)
    check("  after one sleep of the 60s left", clock.sleeps, [60.0])

    code, out, clock = run_gh(FakeGitHub(runs=lambda n, sha: RUNNING), "--interval", "70")
    check("a 70s interval in a 5-minute budget times out", code, 2)
    check("  the final sleep is the 20s left", clock.sleeps, [70.0, 70.0, 70.0, 70.0, 20.0])

    code, out, clock = run_gh(FakeGitHub(fail=lambda path, n: http_error(429, "Too Many Requests",
                                                                         {"retry-after": "1000"})
                                         if path.endswith("/check-runs") else None))
    check("a retry-after past the deadline exits 1", code, 1)
    check("  after one sleep of the 300s left", clock.sleeps, [300.0])

    clock = FakeClock()
    with patched(github_ci, time=clock):
        check("an ordinary pause at an expired deadline is 0", github_ci._pause(30, None, clock.t - 5), 0.0)
        check("a rate-limit pause at an expired deadline is 0", github_ci._pause(30, 120, clock.t - 5), 0.0)
        check("a rate-limit pause inside the budget is what GitHub asked", github_ci._pause(30, 120, clock.t + 300), 120)
        check("a rate-limit pause with 10s left is 10, not the interval", github_ci._pause(30, 120, clock.t + 10), 10)


@section("C11: head movement discards old CI")
def _():
    fake = FakeGitHub(head=lambda n: HEAD if n <= 2 else NEW_HEAD,
                      runs=lambda n, sha: RUNNING if sha == HEAD else (RUNNING if n <= 4 else PASSED))
    code, out, clock = run_gh(fake)
    check("new head passing exits 11", code, 11)
    ok("  CI read at the new sha", any(f"/commits/{NEW_HEAD}/check-runs" in c for c in fake.calls), fake.calls)
    ok("  reports head movement", "head moved" in out, out)

    # A FAILED CI ends the wait at once, so to see one mid-wait it is held by
    # a mergeable: false inside its grace; then the head moves.
    fake = FakeGitHub(head=lambda n: HEAD if n <= 1 else NEW_HEAD,
                      pr=lambda n: {"mergeable": False} if n <= 1 else {},
                      runs=lambda n, sha: FAILED if sha == HEAD else PASSED)
    code, out, clock = run_gh(fake)
    ok("  (the old head's failure was seen)", any(f"/commits/{HEAD}/check-runs" in c for c in fake.calls), fake.calls)
    check("the old head's FAILED CI is not what the run reports", code, 11)
    ok("  the report is about the new head", f"@ {NEW_HEAD[:8]}" in out.split("===")[-2], out)

    # Head moves on round 9 (240s in); with a 5-minute budget the original
    # deadline is 300s, the restarted one 540s.
    fake = FakeGitHub(head=lambda n: HEAD if n <= 8 else NEW_HEAD, runs=lambda n, sha: RUNNING)
    code, out, clock = run_gh(fake)
    check("a moved head restarts the deadline, then times out", code, 2)
    ok("  waiting past the original budget", sum(clock.sleeps) > 300, clock.sleeps)


@section("C12: target movement and mergeability")
def _():
    for runs, label in ((PASSED, "passed"), (FAILED, "failed")):
        code, out, _ = run_gh(FakeGitHub(target=lambda n: BASE if n <= 1 else MOVED,
                                         runs=lambda n, sha, r=runs: RUNNING if n <= 1 else r))
        check(f"target moves while CI {label} -> exit 9", code, 9)
        ok(f"  {label}: labelled BASE_MOVED", "=== BASE_MOVED" in out, out)

    code, out, clock = run_gh(FakeGitHub(pr=lambda n: {"mergeable": False}, runs=lambda n, sha: RUNNING))
    check("mergeable false held 90 seconds -> exit 10", code, 10)
    ok("  not before 90 seconds", sum(clock.sleeps) >= 90, clock.sleeps)
    code, out, clock = run_gh(FakeGitHub(pr=lambda n: {"mergeable": False}, runs=lambda n, sha: PASSED))
    check("mergeable false held 90 seconds overrides CI_PASSED", code, 10)

    code, out, clock = run_gh(FakeGitHub(pr=lambda n: {"mergeable": False}), "--once")
    check("mergeable false under --once -> exit 10", (code, clock.sleeps), (10, []))

    code, out, clock = run_gh(FakeGitHub(pr=lambda n: {"mergeable": False} if n <= 1 else {}))
    check("one false then true does not exit 10", code, 11)

    code, out, _ = run_gh(FakeGitHub(pr=lambda n: {"mergeable": None}), "--once")
    ok("mergeable null prints Merge readiness: UNKNOWN", "Merge readiness: UNKNOWN" in out, out)
    check("  and does not block the CI verdict", code, 11)

    code, out, _ = run_gh(FakeGitHub(fail=lambda path, n: http_error(404, "Not Found")
                                     if "/git/ref/" in path else None), "--once")
    ok("unreadable target prints target tip UNKNOWN", "target tip UNKNOWN" in out, out)
    ok("  and a do-not-merge step", "Do not merge" in out, out)


@section("C13: report and NEXT")
def _():
    code, out, _ = run_gh(FakeGitHub(runs=lambda n, sha: [cr("lint", url="https://github.com/o/r/runs/55")],
                                     statuses=lambda n, sha: [st("ci/jenkins", url="https://jenkins.example.org/9")]),
                          "--once")
    ok("names the PR and head", f"o/r#7 @ {HEAD[:8]}" in out, out)
    ok("names each check with its link",
       "lint" in out and "https://github.com/o/r/runs/55" in out
       and "ci/jenkins" in out and "https://jenkins.example.org/9" in out, out)
    ok("prints the CI state", "CI: PASSED" in out, out)
    ok("prints a Merge readiness line", "Merge readiness: YES" in out, out)
    nxt = out.split(">> NEXT:")[-1]
    ok("CI_PASSED NEXT: not a review", "NOT a review" in nxt, nxt)
    ok("CI_PASSED NEXT: not a merge approval", "NOT a merge approval" in nxt, nxt)

    pin = "--provider github --repo o/r --pr 7"
    for fake, extra, label in (
        (FakeGitHub(runs=lambda n, sha: RUNNING), ("--once",), "--once RUNNING"),
        (FakeGitHub(runs=lambda n, sha: RUNNING), (), "TIMED_OUT"),
        (FakeGitHub(target=lambda n: BASE if n <= 1 else MOVED, runs=lambda n, sha: RUNNING), (), "BASE_MOVED"),
    ):
        code, out, _ = run_gh(fake, *extra)
        ok(f"{label}: NEXT pins the provider in its rerun command", pin in out.split(">> NEXT:")[-1], out)


@section("C14: credentials")
def _():
    def gh_ok(argv, **kw):
        assert argv == ["gh", "auth", "token", "--hostname", "github.com"], argv
        return type("P", (), {"returncode": 0, "stdout": "from-gh\n"})()

    def gh_missing(argv, **kw):
        raise FileNotFoundError("gh")

    with patched(github_ci.subprocess, run=gh_ok):
        with environ(GH_TOKEN="from-gh-token", GITHUB_TOKEN="from-github-token"):
            check("GH_TOKEN first", github_ci.github_token(), "from-gh-token")
        with environ(GH_TOKEN=None, GITHUB_TOKEN="from-github-token"):
            check("then GITHUB_TOKEN", github_ci.github_token(), "from-github-token")
        with environ(GH_TOKEN=None, GITHUB_TOKEN=None):
            check("then gh auth token --hostname github.com", github_ci.github_token(), "from-gh")
    with patched(github_ci.subprocess, run=gh_missing), environ(GH_TOKEN=None, GITHUB_TOKEN=None):
        try:
            github_ci.github_token()
            got = "no exit"
        except SystemExit as exc:
            got = f"exit {status_of(exc)}: {exc.code}"
        ok("none of the three -> exit 1", got.startswith("exit 1") and "GH_TOKEN" in got, got)

    sent = []

    def urlopen(req, timeout):
        sent.append(req)
        resp = io.BytesIO(b"{}")
        resp.headers = {}  # type: ignore[attr-defined]
        return contextlib.nullcontext(resp)

    with patched(github_ci, _urlopen=urlopen):
        github_ci.api("/repos/o/r/pulls/7", TOKEN)
    req = sent[0]
    check("requests go to https://api.github.com", req.full_url, "https://api.github.com/repos/o/r/pulls/7")
    check("  as a Bearer token", req.get_header("Authorization"), f"Bearer {TOKEN}")
    check("  with the pinned API version", req.get_header("X-github-api-version"), "2022-11-28")

    handler = github_ci.SameHostRedirect()
    try:
        handler.redirect_request(req, None, 301, "Moved", {}, "https://evil.example/steal")
        got = "followed"
    except urllib.error.HTTPError as exc:
        got = f"refused {exc.code}"
    ok("a redirect to another host is refused", got.startswith("refused"), got)
    for url in ("http://api.github.com/repositories/1/pulls/7",
                "https://api.github.com:8443/repositories/1/pulls/7"):
        try:
            handler.redirect_request(req, None, 301, "Moved", {}, url)
            got = "followed"
        except urllib.error.HTTPError as exc:
            got = f"refused {exc.code}"
        ok(f"a redirect to {url.split('/repositories')[0]} is refused", got.startswith("refused"), got)

    # End to end through the real opener: a 301 to http:// must not reach the
    # http handler, so the token never goes out in cleartext.
    followed: list[urllib.request.Request] = []

    class Redirecting(urllib.request.BaseHandler):
        handler_order = 100  # ahead of urllib's own handlers: no network

        def https_open(self, r):
            resp = urllib.response.addinfourl(
                io.BytesIO(b""), {"location": "http://api.github.com/repositories/1/pulls/7"},  # type: ignore[arg-type]
                r.full_url, 301)
            resp.msg = "Moved Permanently"  # type: ignore[attr-defined]
            return resp

        def http_open(self, r):
            followed.append(r)
            raise AssertionError("followed a redirect to http://")

    opener = urllib.request.build_opener(github_ci.SameHostRedirect, Redirecting)
    with patched(github_ci, _OPENER=opener):
        try:
            github_ci.api("/repos/o/r/pulls/7", TOKEN)
            got = "no error"
        except urllib.error.HTTPError as exc:
            got = f"HTTPError {exc.code}"
        except AssertionError as exc:
            got = f"followed: {exc}"
    ok("  api() raises on a redirect to http://", got == "HTTPError 301", got)
    check("  and sends nothing over http://", followed, [])

    same =handler.redirect_request(req, None, 301, "Moved", {}, "https://api.github.com/repositories/1/pulls/7")
    ok("a redirect within api.github.com is allowed",
       same is not None and same.full_url.startswith("https://api.github.com/"), same)

    for fake, extra, label in (
        (FakeGitHub(), ("--once",), "a pass"),
        (FakeGitHub(fail=lambda path, n: http_error(401, "Unauthorized")), (), "a 401"),
        (FakeGitHub(fail=lambda path, n: http_error(401, "Unauthorized") if "check-runs" in path else None), (), "a CI 401"),
        (FakeGitHub(fail=lambda path, n: TimeoutError("t")), ("--once",), "an outage"),
    ):
        code, out, _ = run_gh(fake, *extra)
        ok(f"the token is never printed ({label})", TOKEN not in out, out)
        ok(f"  no link-header URL is requested ({label})", not any("evil" in c for c in fake.calls), fake.calls)


@section("C15: Gitea exit codes unchanged")
def _():
    want = {"REVIEWED": 0, "TIMED_OUT": 2, "FAILED": 3, "SKIPPED": 4, "STALE": 5,
            "DECLINED": 6, "PENDING": 7, "CI_FAILED": 8, "BASE_MOVED": 9,
            "NOT_MERGEABLE": 10, "CI_PASSED": 11}
    check("EXIT maps every name to its code", poll_review.EXIT, want)
    check("UNREACHABLE is 1", poll_review.EXIT_UNREACHABLE, 1)

    import test_poll_review as gitea_suite

    git_calls: list[list[str]] = []

    def github_checkout(argv, **kw):
        git_calls.append(argv)
        out = {"remote": "origin\thttps://github.com/o/r.git (fetch)\n"
                         "origin\thttps://github.com/o/r.git (push)",
               "rev-parse": "feat", "config": "origin"}[argv[1]]
        return type("P", (), {"returncode": 0, "stdout": out})()

    with patched(poll_review.subprocess, run=github_checkout), \
            patched(github_ci, main=lambda args: sys.exit("entered the GitHub path")):
        code, out, _ = gitea_suite.run_main(
            gitea_suite.FakeGitea(reviews=lambda n: [gitea_suite.review(gitea_suite.HEAD, rid=5)]))
    check("the Gitea suite's main() ignores a github.com/o/r checkout: exits 0", code, 0)
    ok("  REVIEWED on the Gitea path", "REVIEWED" in out and "NOT_MONITORED" not in out, out)


@section("C16: docs name CI_PASSED as not a review")
def _():
    for rel in ("SKILL.md", "README.md", "references/setup.md", "references/verdicts.md"):
        text = (SKILL_DIR / rel).read_text()
        ok(f"{rel}: a line names CI_PASSED with exit 11",
           any("CI_PASSED" in line and "11" in line for line in text.splitlines()), rel)
        ok(f"{rel}: says it is not a review", "not a review" in text.lower(), rel)
    for rel in ("SKILL.md", "references/verdicts.md"):
        ok(f"{rel}: keeps the pre-merge refresh rule", "Immediately before merging" in (SKILL_DIR / rel).read_text(), rel)


if __name__ == "__main__":
    pattern = None
    if "-k" in sys.argv:
        pattern = sys.argv[sys.argv.index("-k") + 1]
    ran = 0
    for title, fn in SECTIONS:
        if pattern and pattern not in title:
            continue
        ran += 1
        print(f"{title}:")
        try:
            fn()
        except Exception:
            FAILURES.append(f"{title} (crashed)")
            traceback.print_exc()
    if not ran:
        print(f"no section matches {pattern!r}")
        raise SystemExit(1)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:\n  " + "\n  ".join(FAILURES))
        raise SystemExit(1)
    print("\nall passed")
