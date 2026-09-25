# Replying to findings and resolving threads

The poller never judges a finding. Whoever evaluates it decides, and then
uses `reply_finding.py` to act. Gitea has no reply endpoint. A reply is a new
review comment at the same `(path, position)`, and the script does the JSON
escaping that a hand-typed curl gets wrong:

```bash
python3 ~/.claude/skills/babysit-pr/scripts/reply_finding.py \
  --repo OWNER/REPO --pr N --path app/x.py --position 355 \
  --body "Fixed in abc1234: ..." [--comment-id ID --resolve]
```

`--path`, `--position` and `--comment-id` come straight from the poller's
inline output: `[#ID open] path:position`. A finding it marks `(old side)`
sits on a removed line: pass that number as `--old-position` instead.

If the resolve fails after the reply went out, the script says so. Check the
thread before re-running, or the reply posts twice.

**Resolving is an acknowledgment to the bot.** A resolved thread drops out of
the PR view, and the bot stops re-checking that finding for good. So:

| the finding is… | do |
|---|---|
| fixed by a commit that touches the file | reply with the SHA, then `--resolve` |
| disagreed with, or still under discussion | reply, **leave it open**, and file no issue. Closing it is the human's call |
| verified real, but out of scope for this PR | search issues first (`tea issues --repo R --state all`), because the bot re-flags every push. **Ask before filing.** File it with the analysis in the body (`--description "$(cat body.md)"`), reply linking the issue, then `--resolve` |
| not yet verified | reply only. "Looks wrong" is not a resolution |

An unanchored finding (from the review body) has no thread. Fix it, or file
an issue for it under the same out-of-scope rule.

Replies by anyone other than the bot never enter its dedup set, so replying
alone changes nothing on the next push.
