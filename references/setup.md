# Flags, environment and backgrounding

## Flags

Repo and PR are inferred from the checkout's Gitea remotes and branch. The
branch's tracking remote is tried first, then `origin`, then the rest, so a
PR opened from a fork is found in the upstream.

| flag | default | |
|---|---|---|
| `--repo owner/name --pr N` | inferred | override the inference |
| `--wait-for` | `any` | `any`: return when the review has news or CI finishes, whichever is first. `review` / `ci`: wait for that side only. `both`: only once both have settled |
| `--once` | off | check now and report; don't wait. A stuck Gitea call gives up after ~20s (10s per attempt, 2 attempts) instead of ~100s |
| `--timeout-minutes` | 35 | the review budget. It restarts when the head moves |
| `--interval` | 30 | seconds between polls |
| `--no-fail-fast` | off | keep waiting for the review after CI has failed. Implies `--wait-for review` |
| `--full` | off | keep the per-file table the bot puts in the review body |

## Environment

| env | default | |
|---|---|---|
| `GITEA_BASE_URL` | `https://gitea.example.com` | your instance |
| `GITEA_TOKEN` | the `tea` login whose `url` equals `GITEA_BASE_URL` | never another host's login |
| `REVIEW_AGENT_STATE_URL` / `_HEALTH_URL` | unset | the agent's own endpoints. Unset, the poller is Gitea-only and says `NOT CONFIGURED` |
| `REVIEW_AGENT_STATE_TOKEN` | unset | only if the deployment sets `STATE_TOKEN` |
| `REVIEW_BOT_USERNAME` | `review-bot` | |
| `CI_WORKFLOW_FILE` / `CI_JOB_NAME` | unset (every run at head) | narrow CI to the one workflow or job that gates. The file matches by name (`ci.yml`) |

## Backgrounding

Redirect to a file and read it after the process exits. The exit code is the
signal. Start it, end your turn, and let the exit notification wake you. Do
**not** poll the file or set up a monitor while you wait, and never follow it with a
bare `tail -f`: it outlives the poller, and one per round piles up as orphaned
processes. If you want live output, bound it:
`timeout 40m tail -n +1 -f "$OUT"`.

While waiting, the poller prints a status line only when the review or CI
state changes, so the file stays short. Run one poller per PR, and stop it if
the PR closes (a closed PR is reported once and never waited on anyway).
