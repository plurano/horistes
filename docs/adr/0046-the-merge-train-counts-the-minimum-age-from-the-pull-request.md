<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0046: The merge train counts the minimum age from the pull request

- **Status**: Accepted
- **Date**: 2026-10-19
- **Authors**: Lusoris

## Context

The merge train waits for a minimum age (`min_age_days`, default 3 days)
before landing dependency updates, to give upstream advisories and package
retractions time to surface (decision record 0029).

Previously, `update_made(pull)` calculated the update's age from the
`committedDate` of the oldest non-merge commit of the last 20 commits queried
via GraphQL (`history: commits(last: 20)`). When `main` moves, Renovate
rebases its update branch. The rebase rewrites the commit, resetting both
`authoredDate` and `committedDate` to the rebase time.

On 2026-10-19, measurements across the fleet revealed that this holds updates back
indefinitely on active repositories:
- `golusoris/sveltesentio#381` was created on 2026-10-18T13:03:57Z. After five
  force-pushes from rebases on 2026-10-19 (11:47..17:08Z), its commit dates
  reset to 17:08:25Z.
- `cordanaLLM/nucleus#230` was created at 00:56Z, received four force-pushes,
  and had its commit date reset to 09:00Z.

In a repository with a busy `main` branch, frequent rebases ensure the
commit date never reaches 3 days, so the update never lands.

## Decision

- **Count age from pull request creation.** The minimum age is measured
  against the pull request's `createdAt` timestamp rather than commit
  timestamps.
- **Drop commit history query.** The `history: commits(last: 20)` field is
  removed from the repository GraphQL query, and `createdAt` is read directly
  from the pull request node. `update_made` is replaced by this check.
- **Reporting.** The summary report reason text is updated to reflect opening
  time: `dependency update opened {created:%Y-%m-%d %H:%M}Z, younger than the minimum age`.
- **Security bypass unchanged.** Dependency updates with a security label or
  `[SECURITY]` in the title continue to skip the age wait.

## Consequences

- Unrelated Renovate rebases against `main` no longer hold updates forever.
- A newer version pushed onto an open update branch inherits the pull request's
  age. The update bot's own minimum release age setting covers release age
  where a repository sets it.
- The GraphQL query requests fewer fields from the forge.
