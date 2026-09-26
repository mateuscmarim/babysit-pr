# babysit-pr

A Claude Code skill. Waits for `review-bot` (the `gitea-review-agent` companion
project) to review a PR on your Gitea instance and for that PR's CI to reach a
final state. It returns as soon as **either** has news, reports what each
found, and says how to wait for the one still open.

It exists because the bot cannot block a merge: `build_review_payload`
hardcodes `"event": "COMMENT"`, never `APPROVE` or `REQUEST_CHANGES`, so branch
protection has nothing to gate on. A review can take ~30 minutes and routinely
lands after a fast merge. This skill is the only thing that makes you wait.

`SKILL.md` is what the model reads when the skill triggers, and it is kept
short on purpose. The verdict and CI tables are in `references/verdicts.md`,
flags and environment in `references/setup.md`, and the reply/resolve
contract in `references/replying.md`, each read only when needed.
`HISTORY.md` has the incidents behind each rule.

## The flow

The review and CI are two independent verdicts. One loop polls both, and
neither waits on the other. By default (`--wait-for any`) the loop returns on
whichever side has news first, so the reader can act on a finding while CI is
still running, or fix a red CI before the review lands. `--wait-for review`,
`ci` or `both` narrow that; `both` is the old "return only once both settled".

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
    J -->|yes| JX(["report what is known now, do not wait<br/>the review's code, or PENDING · exit 7<br/>+ a NEXT step if CI has not settled"])
    J -->|no| RV{"review has news?<br/>decided, or a new bot review,<br/>with findings under any<br/>(--wait-for any / review)"}
    RV -->|yes| RVX(["the review's code, or STALE 5<br/>+ NEXT: CI has not settled,<br/>re-run with --wait-for ci"])
    RV -->|no| K{"CI FAILED?<br/>unless --no-fail-fast"}
    K -->|yes| KX(["CI_FAILED · exit 8<br/>bail early; a STALE review's<br/>findings are still fetched first"])
    K -->|no| CV{"CI finished during this run?<br/>(--wait-for any / ci)"}
    CV -->|yes| CVX(["PENDING 7, or STALE 5<br/>+ NEXT: NOT a pass,<br/>re-run with --wait-for review"])
    CV -->|no| L{"past the deadline?"}
    L -->|no| M["print a status line if anything changed,<br/>sleep --interval"]
    M --> D

    L -->|yes| N{"review decided?"}
    N -->|yes| NX(["the review's exit code<br/>+ a 'CI did not settle' NEXT step"])
    N -->|no| O{"review STALE?"}
    O -->|yes| OX(["STALE · exit 5<br/>findings printed in full"])
    O -->|no| PX(["TIMED_OUT · exit 2<br/>NOT an approval"])

    I -.- Z["ci_settled: RUNNING never settles.<br/>PASSED and FAILED settle at once.<br/>UNKNOWN settles only on a 4xx;<br/>a transport error holds the wait.<br/>NONE only after 90s watching this head,<br/>since Gitea may not have created the run yet."]
```

Six things this shape depends on:

- **Control comes back on the first news, not the last.** Waiting for both
  sides held a bot failure posted at 32s until CI finished at 2184s. Each early
  return names the one command that waits for the side still open. A CI that
  had already passed when the run began, a STALE review that was already
  there, and a review with no findings are not news: returning on them would
  send the reader straight back.
- **Once the review decides, the code is the review's.** CI gets its own
  block. `PASSED` CI next to a `FAILED` review is still exit 3, and a failed
  CI next to a `REVIEWED` is still 0, so read both blocks, not the number.
- **`TIMED_OUT` is the last branch, not the default.** The decline paths now
  report as `DECLINED` with a reason, and a Gitea outage reports as
  `UNREACHABLE`. Reaching `TIMED_OUT` means none of them applied.
- **"Could not look" is never "nothing there".** An unreadable Actions API is
  `UNKNOWN`, not `NONE`. A Gitea that stays down is `UNREACHABLE`, not
  `TIMED_OUT`.
- **Every code means one thing.** Stopping early has codes of its own:
  `PENDING` (7) when the run returned before the review said anything,
  `CI_FAILED` (8) when CI failed before the review decided. They used to borrow 2 and 5, so exit 2 could
  mean `TIMED_OUT`, "did not wait" or "CI failed first".
- **The output says what to do next.** Every exit path ends with a `>> NEXT:`
  block written for that result: verify these findings, fix CI first, this
  is not a pass. The reader follows the step in front of it instead of
  keeping a table of nine codes in mind through a 35-minute wait.

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
