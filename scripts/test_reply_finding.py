#!/usr/bin/env python3
"""Tests for reply_finding.py. Run: python3 test_reply_finding.py

Plain Python, no pytest -- same convention as test_poll_review.py. A failing
check is recorded, not raised, so one failure never hides the rest. Exits 1
if anything failed.
"""

import contextlib
import io
import json
import urllib.error

import reply_finding

FAILURES: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL {name}: expected {want!r}, got {got!r}")


class _FakeResponse(io.BytesIO):
    """urlopen's result is used as a context manager; BytesIO is not one."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def with_urlopen(fake, fn):
    real = reply_finding.urllib.request.urlopen
    reply_finding.urllib.request.urlopen = fake
    try:
        return fn()
    finally:
        reply_finding.urllib.request.urlopen = real


calls: list = []


def capture(req, timeout=None):
    calls.append(req)
    return _FakeResponse(b"{}")


def run(*argv, urlopen=capture):
    """main() with the network faked: (exit code or SystemExit message, posted paths)."""
    calls.clear()
    real_token = reply_finding.token
    reply_finding.token = lambda: "tok"
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = with_urlopen(urlopen, lambda: reply_finding.main(list(argv)))
    except SystemExit as exc:
        code = exc.code
    finally:
        reply_finding.token = real_token
    return code, [req.full_url.split("/api/v1")[1] for req in calls]


BASE = ("--repo", "o/r", "--pr", "7", "--path", "a.py", "--body", "Fixed in abc1234")

print("reply_body:")

body = reply_finding.reply_body("app/x.py", 260, "Fixed in abc1234")
check("event is COMMENT, never APPROVE/REQUEST_CHANGES", body["event"], "COMMENT")
check("exactly one comment", len(body["comments"]), 1)
check("comment carries path/position/body", body["comments"][0], {
    "path": "app/x.py",
    "new_position": 260,
    "old_position": 0,
    "body": "Fixed in abc1234",
})
check("an old-side reply sets old_position, not new_position",
      reply_finding.reply_body("a.py", 9, "x", old=True)["comments"][0],
      {"path": "a.py", "new_position": 0, "old_position": 9, "body": "x"})

# A reply body with quotes/backslashes/newlines must round-trip through
# json.dumps unbroken -- this is the exact failure mode a hand-typed curl
# with escaped shell quoting runs into.
tricky = "Fixed in abc1234 -- see `x['y']` and \"quoted\" text\nsecond line"
body = reply_finding.reply_body("a.py", 1, tricky)
check("tricky text survives dumps+loads", json.loads(json.dumps(body))["comments"][0]["body"], tricky)

print("post transport:")

with_urlopen(capture, lambda: reply_finding.post("/repos/o/r/pulls/1/reviews", "tok", {"a": 1}))
req = calls[-1]
check("posts to the right path", req.full_url, f"{reply_finding.BASE_URL}/api/v1/repos/o/r/pulls/1/reviews")
check("method is POST", req.get_method(), "POST")
check("carries the token as a Gitea-style header", req.get_header("Authorization"), "token tok")
check("body is the JSON-encoded dict", json.loads(req.data), {"a": 1})

print("main:")

check("reply without --resolve needs no --comment-id",
      run(*BASE, "--position", "3"), (0, ["/repos/o/r/pulls/7/reviews"]))
check("--resolve posts the reply, then resolves",
      run(*BASE, "--position", "3", "--comment-id", "70", "--resolve"),
      (0, ["/repos/o/r/pulls/7/reviews", "/repos/o/r/pulls/comments/70/resolve"]))
code, posted = run(*BASE, "--position", "3", "--resolve")
check("--resolve without --comment-id is a usage error, before posting anything", (code, posted), (2, []))
code, posted = run(*BASE)
check("neither --position nor --old-position is a usage error", (code, posted), (2, []))
code, posted = run(*BASE, "--position", "3", "--old-position", "9")
check("both --position and --old-position is a usage error", (code, posted), (2, []))
run(*BASE, "--old-position", "9")
check("--old-position replies on the old side",
      json.loads(calls[0].data)["comments"][0]["old_position"], 9)

print("errors:")


def down(req, timeout=None):
    raise urllib.error.URLError("connection refused")


code, _ = run(*BASE, "--position", "3", urlopen=down)
check("an unreachable Gitea exits with a message, not a traceback", code,
      "gitea unreachable (URLError: <urlopen error connection refused>) -- nothing was posted")


def reply_ok_resolve_404(req, timeout=None):
    if req.full_url.endswith("/resolve"):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)  # pyright: ignore[reportArgumentType]
    return _FakeResponse(b"{}")


code, _ = run(*BASE, "--position", "3", "--comment-id", "70", "--resolve", urlopen=reply_ok_resolve_404)
check("a failed resolve says the reply already went out", "the reply was posted" in str(code), True)

if FAILURES:
    print(f"\n{len(FAILURES)} FAILED:\n  " + "\n  ".join(FAILURES))
    raise SystemExit(1)
print("\nall passed")
