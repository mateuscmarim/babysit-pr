#!/usr/bin/env python3
"""Reply to one review-bot finding and, optionally, resolve its thread.

Wraps the two-call pattern from SKILL.md's "Replying to findings" section so
the JSON body gets built with `json.dumps` instead of hand-typed shell
quoting -- a reply body containing a quote, backslash, or apostrophe is
exactly the kind of thing that breaks a one-line curl with escaped JSON.

This script never decides anything: it takes an already-chosen comment id,
path, position and reply text as arguments and posts exactly that. Whether a
finding was fixed, declined, or deferred -- and whether resolving it is safe
-- is still the caller's judgment call (typically
`superpowers:receiving-code-review`), per the same seam `poll_review.py`
documents for itself: the tooling that inspects the diff and reasons about a
finding must not be the same tooling that publishes a verdict about one.

Gitea has no reply endpoint (`CreatePullReviewComment` carries no
`in_reply_to`); a reply is a new review comment at the same
(path, new_position), which Gitea groups into the existing thread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get("GITEA_BASE_URL", "https://gitea.example.com").rstrip("/")
TEA_CONFIG = Path.home() / ".config" / "tea" / "config.yml"


def token() -> str:
    if env := os.environ.get("GITEA_TOKEN"):
        return env
    if not TEA_CONFIG.exists():
        sys.exit(f"no token: set GITEA_TOKEN or configure {TEA_CONFIG}")
    for line in TEA_CONFIG.read_text().splitlines():
        if line.strip().startswith("token:"):
            return line.split(":", 1)[1].strip()
    sys.exit(f"no token: found no 'token:' line in {TEA_CONFIG}")


def post(path: str, tok: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{BASE_URL}/api/v1{path}",
        method="POST",
        headers={
            "Authorization": f"token {tok}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps(body).encode(),
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def reply_body(path: str, position: int, text: str) -> dict:
    """The request body for one reply -- split out so tests can check the
    shape without a network call."""
    return {
        "event": "COMMENT",
        "body": "",
        "comments": [
            {
                "path": path,
                "new_position": position,
                "old_position": 0,
                "body": text,
            }
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument(
        "--comment-id",
        type=int,
        required=True,
        help="the finding's comment id, from poll_review.py's output "
        "(only used if --resolve is also passed)",
    )
    ap.add_argument("--path", required=True, help="the finding's file path")
    ap.add_argument(
        "--position",
        type=int,
        required=True,
        help="the finding's new_position, from poll_review.py's output",
    )
    ap.add_argument("--body", required=True, help="the reply text")
    ap.add_argument(
        "--resolve",
        action="store_true",
        help="also resolve the thread -- only for a confirmed, checkable fix; "
        "see SKILL.md's 'Resolve only what you actually fixed' rules",
    )
    args = ap.parse_args()

    tok = token()
    post(
        f"/repos/{args.repo}/pulls/{args.pr}/reviews",
        tok,
        reply_body(args.path, args.position, args.body),
    )
    print(f"replied at {args.path}:{args.position}")

    if args.resolve:
        post(
            f"/repos/{args.repo}/pulls/comments/{args.comment_id}/resolve",
            tok,
            {},
        )
        print(f"resolved comment {args.comment_id}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        sys.exit(f"gitea API {exc.code}: {exc.reason} ({exc.url})")
