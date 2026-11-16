<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0091: The train re-arms after a bot force push

- **Status**: Accepted
- **Date**: 2026-11-16
- **Authors**: Lusoris

## Context

A force push can leave a previously armed pull request without auto-merge while
its new head runs checks. GitHub disables auto-merge when an actor without write
permission pushes to the head branch; a bot can also disable its own arm. At
`6b3ac13`, the train re-arms a retitled armed squash pull request under
[ADR-0064](0064-an-armed-pull-request-is-re-armed-when-retitled.md), but an unarmed
BLOCKED or UNSTABLE row waits for a later sweep to find it clean. The existing
retry history reads force-push events to judge failed attempts, without reading
auto-merge enable or disable events.

The GraphQL schema inspected on 2026-11-16 provides auto-merge, squash and rebase
enable events, auto-merge disable events and head force-push events, including
their actors. A bot actor's login is the app slug without the `[bot]` suffix.
A train disarm is deliberate: review holds and other admission checks must
continue to prevent automatic landing.

## Decision

`ARM_HISTORY` in [tools/merge_train.py](../../tools/merge_train.py) reads the head
oid, the last 20 relevant timeline events and the last 20 issue-comment bodies.
The pure `forced_disarm` reader requires an enable followed by a bot force push
and no later enable. A dependency bot identified by `is_bot_dependency` may
push as its author using a person's token. Every subsequent disable must have
no actor, the force-push actor, or another bot actor. A train disable or another
person's disable prevents recovery.

`serve_cycle` tries `re_arm_forced` after `land_clean` and before `serve_pending`,
keeping a clean landing ahead of a recovery arm. Candidates retain serving
order: unarmed rows outside `held`, with exactly `merge state BLOCKED` or
`merge state UNSTABLE`, no red checks and no red code scanning. Existing hold
reasons are ineligible. The recovery path must also honor unanswered review
bodies and incomplete reviews under
[ADR-0090](0090-the-train-holds-on-an-incomplete-review.md).

A history head differing from the page head waits for another sweep. Otherwise
`arm` sends the page's head oid as the expected head and rebuilds the signed
merge message using the existing implementation. The summary records recovery
or the bounded refusal reason. A refusal posts a plain issue comment starting
with `<!-- merge-train:re-arm-failed <headRefOid> -->`; a marker already present
in the queried comments suppresses another post for that head. A refused
comment becomes a summary note and does not escape the step. Unsigned bot
refusals retain their existing skip note.

The step uses the existing train token's `pull_requests: write` permission for
comments. Reads and writes retain the transport deadline in `request_once`.
No permission or credential configuration changes are needed.

## Consequences

At most `REARM_READS = 5` candidates have their arm history read per repository
per sweep. One arm attempt ends the sweep, preserving the train's one-write
scheduling policy; a failed attempt may also post its diagnostic comment.
The bounded timeline requires positive evidence inside its window, and comment
deduplication applies to the last 20 comments. A later push provides a new head
and a new failure marker.

Repository auto-merge disabled and `required_status` mode perform no recovery
reads or arms. The train continues to land required-status pull requests
directly under [ADR-0076](0076-train-holds-on-a-required-status.md).
`test_planted_force_push_rearms_new_head` and the `test_forced_rearm_*` cases in
[tests/test_merge_train.py](../../tests/test_merge_train.py) cover recovery,
deliberate disarms, holds, read bounds, head movement and failure reporting.
