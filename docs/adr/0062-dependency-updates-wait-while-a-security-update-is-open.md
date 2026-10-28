<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0062: Dependency updates wait while a security update is open

- **Status**: Accepted
- **Date**: 2026-10-28
- **Authors**: Lusoris

## Context

[ADR-0060](0060-the-merge-train-serves-security-updates-first.md) serves a security
update first in every step of the merge train, and
[ADR-0061](0061-a-vulnerability-fix-is-rebased-when-behind.md) has Renovate rebase it
when it falls behind. Neither stops another merge path from moving the default branch
first. On 2026-10-28 Renovate rebased a Go security update in golusoris/golusoris; before
its checks finished, a non-security update merged through the forge's auto-merge, and the
security update stood one commit behind again. Where the default branch demands linear
history, every such merge restarts the wait.

Renovate 44.190 has two ways to merge a pull request it opened:

- With `automerge` and `platformAutomerge` (the default), Renovate turns on the forge's
  native auto-merge when it creates the pull request, and turns it on again only after it
  pushes a new commit.
- With `automerge`, Renovate merges a pull request itself once all its checks pass,
  whether or not native auto-merge is on.

Turning native auto-merge off therefore leaves the second path open. GitHub's
documentation of pull requests states that draft pull requests cannot be merged, which
closes both paths.

The train's token has no right to write commit statuses or check runs, so it cannot hold
a pull request with a failing status.

## Decision

While the train serves a security update in a repository (one it would land, update or
refresh: not a draft and not held by a label), it turns every other open dependency update
to the default branch into a draft and labels it `merge-train: security hold`. Pull
requests from forks, pull requests of a person, other drafts and other security updates
are left alone.

Once no such security update is open, the train marks each labelled pull request ready
again and removes the label. It releases only labelled pull requests, so a draft a person
made stays a draft. A held pull request that a person marks ready keeps the label until
the release and is not held again.

## Consequences

A security update lands on a default branch that no other dependency update moves in the
meantime. Adopters see their dependency updates turn into drafts while a security update
is open: CI stops at the draft step, and the train's refresh starts it again after the
release. Renovate's own merge and native auto-merge resume once the pull request is
ready. A held pull request merges at the earliest one run after the security update.

The hold needs the `pull-requests: write` right the train already holds.

Tests: `test_dependency_update_is_held_as_a_draft_while_a_security_update_is_open` holds
the update and records the draft; `test_planted_hold_that_holds_nothing_lands_the_update_ahead_of_the_fix`
plants an empty hold, and the train lands the update before the fix.
`test_held_update_is_released_when_no_security_update_is_open` and the tests next to it
cover the release and the pull requests the hold leaves alone.
