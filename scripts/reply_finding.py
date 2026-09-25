#!/usr/bin/env python3
"""Reply to one review-bot finding and, optionally, resolve its thread.

Wraps the two-call pattern from SKILL.md's "Replying to findings" section so
the JSON body gets built with `json.dumps` instead of hand-typed shell
quoting -- a reply body containing a quote, backslash, or apostrophe is
exactly the kind of thing that breaks a one-line curl with escaped JSON.

This script never decides anything: it takes an already-chosen comment id,
path, position and reply text as arguments and posts exactly that. Whether a
finding was fixed, declined, or deferred -- and whether resolving it is safe
-- is still the caller's judgment call, per the same seam `poll_review.py`
documents for itself: the tooling that inspects the diff and reasons about a
finding must not be the same tooling that publishes a verdict about one.

Gitea has no reply endpoint (`CreatePullReviewComment` carries no
`in_reply_to`); a reply is a new review comment at the same
(path, new_position), which Gitea groups into the existing thread.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from gitea_auth import BASE_URL, token


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


def reply_body(path: str, position: int, text: str, *, old: bool = False) -> dict:
    """The request body for one reply -- split out so tests can check the
    shape without a network call. `old` puts it on the removed side of the
    diff, for a finding poll_review.py prints as "(old side)"."""
    return {
        "event": "COMMENT",
        "body": "",
        "comments": [
            {
                "path": path,
                "new_position": 0 if old else position,
                "old_position": position if old else 0,
                "body": text,
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument(
        "--comment-id",
        type=int,
        help="the finding's comment id, from poll_review.py's output "
        "(required with --resolve)",
    )
    ap.add_argument("--path", required=True, help="the finding's file path")
    side = ap.add_mutually_exclusive_group(required=True)
    side.add_argument(
        "--position",
        type=int,
        help="the finding's line, from poll_review.py's output",
    )
    side.add_argument(
        "--old-position",
        type=int,
        help="the finding's line on the removed side, for a finding "
        "poll_review.py marks '(old side)'",
    )
    ap.add_argument("--body", required=True, help="the reply text")
    ap.add_argument(
        "--resolve",
        action="store_true",
        help="also resolve the thread -- which tells the bot to stop re-checking "
        "the finding. See SKILL.md's 'Replying to findings' table for when",
    )
    args = ap.parse_args(argv)
    # Checked before anything is posted: failing here after the reply went
    # out would leave half the job done.
    if args.resolve and args.comment_id is None:
        ap.error("--resolve needs --comment-id")

    old = args.old_position is not None
    line = args.old_position if old else args.position
    tok = token()
    try:
        post(
            f"/repos/{args.repo}/pulls/{args.pr}/reviews",
            tok,
            reply_body(args.path, line, args.body, old=old),
        )
    except urllib.error.HTTPError as exc:
        sys.exit(f"gitea API {exc.code}: {exc.reason} ({exc.url}) -- nothing was posted")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        # Not retried: a timeout after Gitea accepted the POST would post the
        # reply twice. Check the thread before running this again.
        sys.exit(f"gitea unreachable ({type(exc).__name__}: {exc}) -- nothing was posted")
    print(f"replied at {args.path}:{line}{' (old side)' if old else ''}")

    if args.resolve:
        try:
            post(
                f"/repos/{args.repo}/pulls/comments/{args.comment_id}/resolve",
                tok,
                {},
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # HTTPError is a URLError, so this covers both.
            sys.exit(
                f"the reply was posted, but resolving comment {args.comment_id} "
                f"failed ({type(exc).__name__}: {exc}). Re-run with --resolve "
                "only after checking the thread, or the reply goes out twice."
            )
        print(f"resolved comment {args.comment_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
