#!/usr/bin/env python3
"""Tests for reply_finding.py. Run: python3 test_reply_finding.py

Plain asserts, no pytest -- same convention as test_poll_review.py.
"""

import io
import json

import reply_finding


def check(name, got, want):
    assert got == want, f"{name}: expected {want!r}, got {got!r}"
    print(f"  ok  {name}")


class _FakeResponse(io.BytesIO):
    """urlopen's result is used as a context manager; BytesIO is not one."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


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

# A reply body with quotes/backslashes/newlines must round-trip through
# json.dumps unbroken -- this is the exact failure mode a hand-typed curl
# with escaped shell quoting runs into.
tricky = "Fixed in abc1234 -- see `x['y']` and \"quoted\" text\nsecond line"
body = reply_finding.reply_body("a.py", 1, tricky)
check("tricky text survives dumps+loads", json.loads(json.dumps(body))["comments"][0]["body"], tricky)

print("post transport:")

calls = []


def with_urlopen(fake, fn):
    real = reply_finding.urllib.request.urlopen
    reply_finding.urllib.request.urlopen = fake
    try:
        return fn()
    finally:
        reply_finding.urllib.request.urlopen = real


def capture(req, timeout=None):
    calls.append(req)
    return _FakeResponse(b"{}")


with_urlopen(capture, lambda: reply_finding.post("/repos/o/r/pulls/1/reviews", "tok", {"a": 1}))
req = calls[-1]
check("posts to the right path", req.full_url, f"{reply_finding.BASE_URL}/api/v1/repos/o/r/pulls/1/reviews")
check("method is POST", req.get_method(), "POST")
check("carries the token as a Gitea-style header", req.get_header("Authorization"), "token tok")
check("body is the JSON-encoded dict", json.loads(req.data), {"a": 1})

print("\nall passed")
