# GitHub CI-only babysitting

Sources: conversation (2026-09-30): GitHub support initially monitors only CI while the user's bot is being ported. Existing `scripts/poll_review.py`, `references/setup.md` and `README.md` define Gitea compatibility.

## Problem

The skill only monitors Gitea. GitHub PR authors cannot use its bounded background wait, CI reporting or target-movement warnings. GitHub review monitoring is not yet useful because the bot is still being ported.

## Out of scope

| Excluded | Why |
| --- | --- |
| GitHub reviews, replies and thread resolution | Bot port is unfinished |
| Auto-merge, pushes or workflow reruns | Read-only monitoring |
| GitHub Enterprise and merge queues | Initial support targets github.com and ordinary PR checks |
| Certifying branch protection or combined-tree correctness | CI success alone cannot certify merge readiness |

## Assumptions

| Assumption | Chosen default | Rationale | Confirmed? |
| --- | --- | --- | --- |
| Hosting scope | github.com only initially | Keeps credentials and API routing unambiguous | n |
| CI selection | All visible check runs and commit-status contexts for the current head; no workflow/job filters on GitHub | Includes external CI without promising branch-protection evaluation | n |
| Empty CI | NONE remains non-success through timeout | No checks is not evidence of passing checks | n |
| Authentication | GH_TOKEN, then GITHUB_TOKEN, then gh auth token --hostname github.com | Familiar GitHub credential sources, separate from Gitea | n |
| CI success exit | New code 11 CI_PASSED for GitHub; never REVIEWED or exit 0 | Preserves the existing exit-0 review contract | n |

**Open questions:** none - defaults above are proposed for approval.

## Criteria

### S1: Monitor a GitHub PR without waiting for a reviewer (P1)

**Acceptance Criteria**

1. WHEN invoked with `--provider github` and `--repo owner/name --pr N` THEN the system SHALL monitor that GitHub PR without reading any Gitea credentials, bot state, reviews or comments.
2. WHEN provider selection is automatic THEN the system SHALL select a supported provider from checkout remotes, prefer the tracking remote then origin, and reject unresolved ambiguity; explicit repo and PR without a checkout SHALL retain the legacy Gitea default.
3. WHEN a GitHub PR is inferred THEN the system SHALL match the local branch and source repository to exactly one open PR, including fork PRs, or request explicit repo and PR rather than choose an ambiguous candidate.
4. WHEN GitHub monitoring starts THEN the system SHALL print `review: NOT_MONITORED`, default to CI-only waiting, and reject explicit review/both waiting, `--no-fail-fast`, `--full`, or configured Gitea CI filters with an actionable argument error.
5. WHEN reading GitHub CI THEN the system SHALL paginate check runs and commit statuses for the exact current head, use the newest result per check identity (application and name) and status context, and classify pending/in-progress as RUNNING; success/neutral/skipped check conclusions and success statuses as successful; failure/cancelled/timed_out/action_required/stale/startup_failure check conclusions and failure/error statuses as FAILED; unrecognized data as UNKNOWN.
6. IF any selected check fails THEN the system SHALL return CI_FAILED (8); otherwise it SHALL return CI_PASSED (11) only when at least one result exists and all selected results are successful, never calling the PR reviewed or ready to merge.
7. IF API authentication, pagination, transport, rate limits or response parsing prevent complete CI inspection THEN the system SHALL report UNKNOWN or UNREACHABLE rather than success or NONE, retry transient errors within the bounded wait, and return error (1) if still unreadable at the deadline; --once SHALL return without sleeping.
8. WHILE CI is running or absent THEN the system SHALL wait within the configured budget; --once or a closed PR SHALL report once with PENDING (7), and expiry of the wait SHALL return TIMED_OUT (2), neither a pass.
9. WHEN the head changes THEN the system SHALL discard previous-head CI evidence and restart the timeout; WHEN the live target branch tip changes THEN the system SHALL return BASE_MOVED (9); IF mergeable remains false for 90 seconds or is false under --once THEN the system SHALL return NOT_MERGEABLE (10). Unknown mergeability or an unreadable target SHALL be printed as unknown, not permission to merge. Readiness overrides CI results.
10. WHEN reporting a GitHub result THEN the system SHALL print the PR/head, check names and available links, CI state, readiness caveats and a NEXT block with a provider-pinned rerun command where waiting remains useful; credentials SHALL never appear in output or be sent to another host.
11. WHEN existing Gitea commands and tests run THEN the system SHALL preserve their behavior and exit meanings; documentation SHALL distinguish GitHub CI-only success from a reviewed Gitea PR and preserve the final pre-merge refresh requirement.

**Independent test:** fake GitHub API and clock demonstrate pending-to-success, failure, reruns, absent checks, inaccessible checks, head/base movement and a closed PR without live credentials; existing Gitea suites remain green.

## Traceability

| ID | Slice | Criteria | Status |
| --- | --- | --- | --- |
| GHCI-01 | S1 | 1, 2, 3, 4 | Pending |
| GHCI-02 | S1 | 5, 6, 7, 8 | Pending |
| GHCI-03 | S1 | 9, 10, 11 | Pending |

## Observable

| Surface | Decision | Landing |
| --- | --- | --- |
| CLI provider and repo selection | Defaults, forks, ambiguity and unsupported hosts | AC 1, 2, 3 |
| CLI flags/environment | Unsupported review options/filters rejected; host-scoped credentials | AC 4, 10 |
| CI report | Empty, pending, successful, failed, unreadable, unknown conclusions | AC 5, 6, 7, 8 |
| Background process | Timeout, head restart, closed PR and once mode | AC 7, 8, 9 |
| Merge readiness | Moving target, false/unknown mergeability | AC 9 |
| NEXT and docs | Explicit non-review semantics and safe continuation | AC 10, 11 |
| Persistence and writes | n/a - read-only process; no state migration | n/a - no stored data |

## Flow

1. `poll_review.py` (exists) parses provider selection before obtaining credentials; Gitea retains its existing path (AC 1–4, 11).
2. GitHub monitor (new, no door - placement per conventions) resolves credentials, repository and PR, then snapshots head and live target branch (AC 1–4, 10).
3. GitHub API reader (new, no door - placement per conventions) paginates current-head checks/statuses, normalizes newest results and refreshes PR/target on each bounded poll (AC 5–9).
4. GitHub monitor reports separate CI and readiness blocks plus NOT_MONITORED review and provider-pinned NEXT commands; no mutation API is called (AC 6–11).

## Relations

None - no stored entities. A PR has a current head and a target branch; each head has zero or more check runs and commit statuses. Evidence from one head cannot certify another.

## Surface

| Route | In | Out | Statuses |
| --- | --- | --- | --- |
| poll_review.py --provider auto/gitea/github | Existing flags, repo/PR, provider and host-scoped credentials | Text report and exit code | `ci_passed`, `ci_failed`, `pending`, `timed_out`, `unreachable`, `base_moved`, `not_mergeable`, `argument_error` |
| Existing Gitea CLI | Existing arguments | Existing report | `reviewed`, `unreachable`, `timed_out`, `failed`, `skipped`, `stale`, `declined`, `pending`, `ci_failed`, `base_moved`, `not_mergeable` |

Status names above are semantic states; printed verdict labels remain uppercase.

## Landing

| Door | Literal shape | Rejected alternative |
| --- | --- | --- |
| Provider selector | `--provider {auto,gitea,github}`, default auto; legacy Gitea fallback outside checkout | Requiring all existing callers to add a provider |
| CI-only success contract | Exit `11`, label `CI_PASSED`; review `NOT_MONITORED` | Reusing exit 0/REVIEWED for an unreviewed PR |
| Credential boundary | github.com API only; GH_TOKEN > GITHUB_TOKEN > gh auth token --hostname github.com | Reusing Gitea tokens or following arbitrary authenticated pagination URLs |

## Impact

- No stored-data changes. Existing Gitea terminology and exit codes remain intact. Add GitHub CI-only vocabulary, a success code, setup/verdict documentation and skill trigger text. The GitHub API shape must be checked against official documentation before writing checks; fixtures must model pagination, reruns and rate limits. Verification profile: light (repository has no declared override); independent verification is required. No publishing or pushes are included.
