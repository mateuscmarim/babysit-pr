---
name: babysit-pr
description: Use before merging a pull request on a Gitea instance. It waits for review-bot's automated review and the PR's CI, returns as soon as either one has news, and reports what each found. Triggers on "babysit this PR", "wait for the review", "is the bot done", "is CI green on this Gitea PR", or any merge of a Gitea PR opened while the reviewer is live.
---

# Babysit a PR until the reviewer has spoken

`review-bot` reviews every Gitea PR, but it only ever posts `COMMENT`, so
nothing stops a merge before its review lands (often ~30 minutes). This skill
makes you wait for it and for CI. It hands control back as soon as either one
has news, so you can act on it while the other is still running.

**Silence or an ambiguous answer is never a pass.** Never report a run that
did not end in `REVIEWED` as "clean" or "no issues found".

## Run it

```bash
python3 ~/.claude/skills/babysit-pr/scripts/poll_review.py > /tmp/babysit-REPO-PR.out 2>&1; echo "exit $?"
```

Use the Bash tool's `run_in_background: true`: the wait runs up to 35
minutes, and you are notified when it exits. It exits at the first of: the
review decides (a review, a skip or failure notice, a decline), a new bot
review lands (even one of an older head), or CI finishes. A review with no
findings is the exception: it waits for CI, since there is nothing to act on
before CI answers. After starting it, **end your
turn** and wait for that notification. Don't monitor, poll or `tail -f` the
file, and don't make placeholder calls (`echo waiting`, `true`, `sleep`) to
pass the time. When notified, read the output file once. `--once` checks
without waiting and can run in the foreground.

Repo and PR are inferred from the checkout. Pass `--repo owner/name --pr N`
from anywhere else. Other flags, the environment variables and live-output
tips are in [references/setup.md](references/setup.md).

## Read the result

The output ends with a **`>> NEXT:`** block that says what to do for this
result. Follow it. When one side is still open, its last step is the command
that waits for just that side (`--wait-for review` or `--wait-for ci`). Act
on what came back first, then start that command the same way. The exit code tells you which case you are in:

| exit | verdict | in short |
|---|---|---|
| 0 | `REVIEWED` | a review of the current head. Verify its findings |
| 1 | `UNREACHABLE` / error | no verdict at all |
| 2 | `TIMED_OUT` | nothing from the bot. **Not an approval** |
| 3 | `FAILED` | the bot failed. The PR is unreviewed |
| 4 | `SKIPPED` | the diff is too large to review |
| 5 | `STALE` | only a review of an older head. Its findings still count |
| 6 | `DECLINED` | no review is coming, for the reason printed |
| 7 | `PENDING` | CI finished first, `--once`, or a closed PR: no review yet. **Not a pass** |
| 8 | `CI_FAILED` | CI failed before the review decided |

The code is the review's. CI is reported in its own block, and a failed or
still-running CI next to exit 0 appears as its own step under NEXT. Exit 0
with CI still running is not a merge signal.

Three rules the output cannot enforce:

- **Zero inline comments is not zero findings.** Read the "unanchored
  finding(s)" and the overview prose too.
- **Verify every finding against the code** before acting on it or repeating
  it. The bot is not deterministic, and a later clean review does not clear
  an earlier finding.
- **Say the verdict by its name.** `TIMED_OUT` is `TIMED_OUT`, not "clean".

The full verdict and CI tables are in
[references/verdicts.md](references/verdicts.md). Before replying to a
finding or resolving a thread, read
[references/replying.md](references/replying.md): resolving tells the bot to
stop checking that finding for good.

## Changing the skill

Run the tests after any change, and read [HISTORY.md](HISTORY.md) before
loosening a rule. It holds the incidents behind each one.

```bash
cd ~/.claude/skills/babysit-pr/scripts && python3 test_poll_review.py && python3 test_reply_finding.py
```
