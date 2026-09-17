# babysit-pr

A Claude Code skill. Waits for `review-bot` (the `gitea-review-agent` companion
project) to finish reviewing a PR on your Gitea instance **and** for that PR's
CI to reach a final state, reports what each found, and stops.

It exists because the bot cannot block a merge: `build_review_payload`
hardcodes `"event": "COMMENT"`, never `APPROVE` or `REQUEST_CHANGES`, so branch
protection has nothing to gate on. A review can take ~30 minutes and routinely
lands after a fast merge. This skill is the only thing that makes you wait.

`SKILL.md` is the real documentation — the verdict table, the traps, and the
reply/resolve contract all live there.

## The flow

Two independent verdicts — the review and CI — polled in the same loop, neither
waiting on the other. The loop only returns when **both** have settled, or when
one of the four escape hatches below fires first.

```mermaid
flowchart TD
    A(["poll_review.py"]) --> B["resolve repo + PR, read head SHA"]
    B --> C{"PR authored by review-bot?"}
    C -->|yes| CX(["DECLINED · exit 6<br/>parse_event skips these to avoid loops"])
    C -->|no| D

    D["poll: fetch current head"] --> E{"head moved?"}
    E -->|yes| F["restart the wait:<br/>reset deadline + watching_since"]
    F --> G
    E -->|no| G

    G["classify → review verdict<br/>REVIEWED · STALE · SKIPPED<br/>FAILED · DECLINED · PENDING"]
    G --> H["classify_ci → CI verdict<br/>PASSED · FAILED · RUNNING · NONE"]
    H --> I{"review decided<br/>AND ci_settled?"}

    I -->|yes| IX(["the review's own exit code<br/>REVIEWED 0 · FAILED 3<br/>SKIPPED 4 · DECLINED 6"])
    I -->|no| J{"--once?"}
    J -->|yes| JX(["report what is known now,<br/>do not wait"])
    J -->|no| K{"CI FAILED?<br/>unless --no-fail-fast"}
    K -->|yes| KX(["bail early · review's code<br/>PENDING falls back to 2<br/>a STALE review's findings<br/>are still fetched first"])
    K -->|no| L{"past the deadline?"}
    L -->|no| M["print progress, sleep --interval"]
    M --> D

    L -->|yes| N{"review decided?"}
    N -->|yes| NX(["the review's exit code<br/>+ 'CI still open' note"])
    N -->|no| O{"review STALE?"}
    O -->|yes| OX(["STALE · exit 5<br/>findings printed in full"])
    O -->|no| PX(["TIMED_OUT · exit 2<br/>NOT an approval"])

    I -.- Z["ci_settled: RUNNING never settles.<br/>PASSED and FAILED settle at once.<br/>NONE only after 90s watching this head —<br/>it may just be a run Gitea has not created yet."]
```

Three things the shape is load-bearing about:

- **Exit codes come from the review side only.** CI is reported in its own
  block, never folded into the code. A `PASSED` CI next to a `FAILED` review is
  still exit 3 — read both blocks, not the number.
- **`TIMED_OUT` is the last branch, not the default.** The decline paths that
  used to be indistinguishable from silence now report as `DECLINED` with a
  reason. Reaching `TIMED_OUT` means it was none of them.
- **The CI fail-fast escape returns the *review's* code**, so an undecided
  review bails with exit 2 — the same code as `TIMED_OUT`, which means
  something else entirely. That is why it prints an explicit banner.

## This repo *is* the installed skill

It is `git init`-ed in place at `~/.claude/skills/babysit-pr`, so there is no
copy step and no symlink: edit, test, commit, push. What you run is what is
committed.

Cloning it somewhere else gives you the files but not a working skill — Claude
Code discovers skills by directory location. To install on another machine,
clone to `~/.claude/skills/babysit-pr`.

## Usage

```bash
python3 ~/.claude/skills/babysit-pr/scripts/poll_review.py
```

Repo and PR are inferred from the current checkout's Gitea remote (set
`GITEA_BASE_URL` to match your instance) and branch; override with `--repo
owner/name --pr N`. Needs a Gitea token from `$GITEA_TOKEN` or the `tea`
config — no credentials are stored here.

## Tests

Plain asserts, no pytest, so they run anywhere the skill runs. Required after
any change to the classification logic:

```bash
cd scripts && python3 test_poll_review.py && python3 test_reply_finding.py
```
