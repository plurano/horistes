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
Every disable clears the enable, including a disable before any force push.
A person's disable followed by a bot push therefore stays off: that push needs
a new enable event to qualify. A forge or bot disable after the qualifying
push preserves recovery evidence. A later force push by a person clears that
evidence, whether auto-merge is still enabled or already off. A later bot force
push while auto-merge is off preserves the evidence of the earlier qualifying push
(`test_forced_disarm_person_disarm_before_push_stays_off`,
`test_forced_rearm_person_disarm_before_push_stays_off`,
`test_forced_disarm_enable_methods_and_bot_disablers`,
`test_forced_disarm_later_force_push[person-push-after-forge-disable]`,
`test_forced_disarm_later_force_push[person-push-while-enabled]`,
`test_forced_disarm_later_force_push[bot-push-after-forge-disable]` and
`test_forced_rearm_without_qualifying_history_stays_off[person-push-after-disable]`).

`serve_cycle` tries `re_arm_forced` after `land_clean` and before `serve_pending`,
keeping a clean landing ahead of a recovery arm. Candidates retain serving
order: unarmed rows outside `held`, with exactly `merge state BLOCKED` or
`merge state UNSTABLE`, no red checks and no red code scanning. Existing hold
reasons are ineligible. The recovery path must also honor unanswered review
bodies and incomplete reviews under
[ADR-0090](0090-the-train-holds-on-an-incomplete-review.md).
The step reads review bodies just before arming and holds as landing does
(`test_forced_rearm_review_hold_stays_off[findings]`,
`test_forced_rearm_review_hold_stays_off[incomplete]`,
`test_forced_rearm_review_hold_continues_to_second_candidate` and
`test_forced_rearm_review_read_failure_holds`); dependency bot pull requests
retain the existing `late_review_hold` exemption.
Recovery waits without reading arm histories while any armed pull request is
in flight, using the same `in_flight` rule as `serve_pending`
(`test_forced_rearm_armed_in_flight_stays_off`).

A history head differing from the page head waits for another sweep. Otherwise
`arm` sends the page's head oid as the expected head and rebuilds the signed
merge message using the existing implementation. The summary records recovery
or the bounded refusal reason. A refusal posts a plain issue comment starting
with `<!-- merge-train:re-arm-failed <headRefOid> -->`; a marker already present
in the queried comments suppresses another post for that head. A refused
comment becomes a summary note and does not escape the step. Unsigned bot
refusals retain their existing skip note and the same comment deduplication,
then leave pending branch updates eligible
(`test_forced_rearm_unsigned_bot_keeps_note`,
`test_forced_rearm_unsigned_bot_leaves_pending_updates`). Other refusals end
the sweep (`test_forced_rearm_refusal_comment_once`).

Within the read window, qualifying heads without their current head's refusal
marker go first. When no unmarked candidate qualifies, marked qualifying heads
take turns in window order: `marked[rearm_turn(env, n) % len(marked)]`, where
`n` is the candidate count. For `n > REARM_READS`, the windows repeat every
`n // gcd(n, REARM_READS)` sweeps; otherwise the cycle is one sweep.
The turn is the nonnegative run number divided by that cycle length using
integer division, so it advances once per full window cycle. The picked marked
head's review hold is read again right before its arm.

The step uses the existing train token's `pull_requests: write` permission for
comments. Reads and writes retain the transport deadline in `request_once`.
No permission or credential configuration changes are needed.

## Consequences

At most `REARM_READS = 5` candidates have their arm history read per repository
per sweep, with at most one review-body read for each qualifying candidate plus
one more for the picked marked head: at most eleven history and review-body
reads in the recovery step. Held unmarked heads retain the ten-read bound
(`test_forced_rearm_review_read_budget`). A successful arm or a refusal other
than an unsigned-bot refusal ends the sweep; an unsigned-bot refusal happens
before the arm mutation and permits a pending branch update. A failed attempt
may also post its diagnostic comment (`test_forced_rearm_pending_states`,
`test_forced_rearm_refusal_comment_once`,
`test_forced_rearm_unsigned_bot_leaves_pending_updates`).
When candidates exceed that budget, the read window rotates by
`(GITHUB_RUN_NUMBER * REARM_READS) % len(candidates)`, wrapping at the end.
Missing, invalid or negative run numbers use zero. For a stable candidate list,
the windows cover later candidates within `ceil(n / REARM_READS)` successive
sweeps for candidates whose arm is not refused. Non-qualifying histories cannot
permanently consume every slot. A refusal marker on the current head makes later
sweeps try unmarked qualifying candidates in that window first. When no unmarked
candidate qualifies, marked qualifying heads take turns once per window cycle.
With a stable candidate list, each marked qualifying head in a window holding
`m` marked qualifying heads is tried at least once every `m` cycles while no
unmarked candidate in that window qualifies. The turn stays constant within a
cycle, preventing read-window rotation from repeatedly selecting the same marked
head. This preserves one recovery arm attempt per sweep and lets a transient
refusal on any marked head heal (`test_forced_rearm_one_arm_per_sweep`,
`test_forced_rearm_marked_head_heals`).
The picked marked head's review hold is read again right before its arm; a new
hold prevents the arm for that sweep
(`test_forced_rearm_refused_head_yields_to_unmarked[run-0]`,
`test_forced_rearm_refused_head_yields_to_unmarked[run-2]`,
`test_forced_rearm_marked_head_retries_refusal`,
`test_forced_rearm_marked_head_heals`,
`test_forced_rearm_marked_heads_rotate[run-0]`,
`test_forced_rearm_marked_heads_rotate[run-1]`,
`test_forced_rearm_marked_heads_rotate_across_windows[run-0]`, `[run-2]`,
`[run-4]`, `[run-6]`, `[run-1]`,
`test_rearm_turn_covers_marked_windows[n-25]`, `[n-10]`, `[n-7]`,
`test_rearm_turn_small_lists_use_run_number[empty]`, `[one]`, `[budget]` and
`test_forced_rearm_marked_head_late_review_hold`).
A re-run uses the same run number and reads the same window. Candidates that
fit the budget keep their serving order
(`test_forced_rearm_history_window_rotates` and
`test_forced_rearm_history_window_preserves_serving_order[empty]`, `[one]`,
`[budget]`).
The bounded timeline requires positive evidence inside its window, and comment
deduplication applies to the last 20 comments. A push provides a new head without
the old head's marker (`test_forced_rearm_other_head_marker_does_not_defer`).
If the marker comment cannot be posted or leaves the last 20 comments, the
refused head counts as unmarked and is tried first again; its next refusal posts
the marker, if the comment write succeeds.

Repository auto-merge disabled, `required_status` mode and non-empty
`review_bots` perform no recovery reads or arms
(`test_forced_rearm_disabled_modes_read_nothing`,
`test_forced_rearm_required_status_stays_off`,
`test_forced_rearm_review_bots_stays_off`). The review-bot restriction follows
[ADR-0092](0092-the-train-requires-a-completed-review-on-the-head.md).
The train continues to land required-status pull requests directly under
[ADR-0076](0076-train-holds-on-a-required-status.md).
`test_planted_force_push_rearms_new_head` and the `test_forced_rearm_*` cases in
[tests/test_merge_train.py](../../tests/test_merge_train.py) cover recovery,
deliberate disarms, holds, read bounds, head movement and failure reporting.
