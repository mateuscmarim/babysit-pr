# babysit-pr

A Claude Code skill. Waits for `review-bot` (the `gitea-review-agent` companion
project) to finish reviewing a PR on your Gitea instance **and** for that PR's
CI to reach a final state, reports what each found, and stops.

It exists because the bot cannot block a merge: `build_review_payload`
hardcodes `"event": "COMMENT"`, never `APPROVE` or `REQUEST_CHANGES`, so branch
protection has nothing to gate on. A review can take ~30 minutes and routinely
lands after a fast merge. This skill is the only thing that makes you wait.

`SKILL.md` is the real documentation: the verdict tables, the rules, and the
reply/resolve contract all live there. `HISTORY.md` has the incidents behind
each rule.

## The flow

The review and CI are two independent verdicts. One loop polls both, and
neither waits on the other. The loop returns only when **both** have settled,
or when one of the escape hatches below fires first.

```mermaid
flowchart TD
    A(["poll_review.py"]) --> B["resolve repo + PR, read head SHA"]
    B --> C{"PR authored by review-bot?"}
    C -->|yes| CX(["DECLINED · exit 6<br/>parse_event skips these to avoid loops"])
    C -->|no| CL{"PR closed or merged?"}
    CL -->|yes| CLX["check once, do not wait"]
    CLX --> D
    CL -->|no| D

    D["poll: fetch the PR"] --> R{"Gitea answered?"}
    R -->|"no: timeout / 5xx"| RX{"--once, closed,<br/>or past the deadline?"}
    RX -->|yes| RXX(["UNREACHABLE · exit 1<br/>no verdict, NOT a pass"])
    RX -->|no| M
    R -->|yes| E{"head moved?"}
    E -->|yes| F["restart the wait:<br/>reset deadline + watching_since"]
    F --> G
    E -->|no| G

    G["classify → review verdict<br/>newest bot review at head wins<br/>REVIEWED · STALE · SKIPPED<br/>FAILED · DECLINED · PENDING"]
    G --> H["read_ci → CI verdict<br/>newest run per workflow + event<br/>PASSED · FAILED · RUNNING<br/>UNKNOWN · NONE"]
    H --> I{"review decided<br/>AND ci_settled?"}

    I -->|yes| IX(["the review's own exit code<br/>REVIEWED 0 · FAILED 3<br/>SKIPPED 4 · DECLINED 6"])
    I -->|no| J{"--once or PR closed?"}
    J -->|yes| JX(["report what is known now,<br/>do not wait"])
    J -->|no| K{"CI FAILED?<br/>unless --no-fail-fast"}
    K -->|yes| KX(["bail early · review's code<br/>PENDING falls back to 2<br/>a STALE review's findings<br/>are still fetched first"])
    K -->|no| L{"past the deadline?"}
    L -->|no| M["print progress, sleep --interval"]
    M --> D

    L -->|yes| N{"review decided?"}
    N -->|yes| NX(["the review's exit code<br/>+ 'CI did not settle' note"])
    N -->|no| O{"review STALE?"}
    O -->|yes| OX(["STALE · exit 5<br/>findings printed in full"])
    O -->|no| PX(["TIMED_OUT · exit 2<br/>NOT an approval"])

    I -.- Z["ci_settled: RUNNING never settles.<br/>PASSED and FAILED settle at once.<br/>UNKNOWN settles only on a 4xx;<br/>a transport error holds the wait.<br/>NONE only after 90s watching this head,<br/>since Gitea may not have created the run yet."]
```

Four things this shape depends on:

- **Exit codes come from the review side only.** CI gets its own block and is
  never folded into the code. `PASSED` CI next to a `FAILED` review is still
  exit 3, so read both blocks, not the number.
- **`TIMED_OUT` is the last branch, not the default.** The decline paths now
  report as `DECLINED` with a reason, and a Gitea outage reports as
  `UNREACHABLE`. Reaching `TIMED_OUT` means none of them applied.
- **"Could not look" is never "nothing there".** An unreadable Actions API is
  `UNKNOWN`, not `NONE`. A Gitea that stays down is `UNREACHABLE`, not
  `TIMED_OUT`.
- **The CI fail-fast escape returns the *review's* code.** So an undecided
  review bails with exit 2, the same code as `TIMED_OUT`, which means
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
owner/name --pr N`. Needs a Gitea token from `$GITEA_TOKEN`, or from the `tea`
login whose url matches `GITEA_BASE_URL`. No credentials are stored here.

## Tests

Plain Python, no pytest, so they run anywhere the skill runs. A failing check
is reported without stopping the run. `main()` runs end to end against a fake
Gitea and a fake clock. Required after any change:

```bash
cd scripts && python3 test_poll_review.py && python3 test_reply_finding.py
```
