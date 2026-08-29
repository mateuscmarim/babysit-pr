# babysit-pr

A Claude Code skill. Waits for [`review-bot`](https://gitea.example.com/mateuscmarim/gitea-review-agent)
to finish reviewing a PR on `gitea.example.com` **and** for that PR's CI to reach a
final state, reports what each found, and stops.

It exists because the bot cannot block a merge: `build_review_payload`
hardcodes `"event": "COMMENT"`, never `APPROVE` or `REQUEST_CHANGES`, so branch
protection has nothing to gate on. A review can take ~30 minutes and routinely
lands after a fast merge. This skill is the only thing that makes you wait.

`SKILL.md` is the real documentation — the verdict table, the traps, and the
reply/resolve contract all live there.

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

Repo and PR are inferred from the current checkout's `gitea.example.com` remote and
branch; override with `--repo owner/name --pr N`. Needs a Gitea token from
`$GITEA_TOKEN` or the `tea` config — no credentials are stored here.

## Tests

Plain asserts, no pytest, so they run anywhere the skill runs. Required after
any change to the classification logic:

```bash
cd scripts && python3 test_poll_review.py && python3 test_reply_finding.py
```
