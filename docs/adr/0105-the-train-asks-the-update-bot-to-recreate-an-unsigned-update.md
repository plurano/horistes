<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0105: The train asks the update bot to recreate an unsigned update

- **Status**: Accepted
- **Date**: 2026-11-21
- **Authors**: Lusoris

## Context

The merge train writes the squash message from each pull request's commits
([ADR-0037](0037-the-merge-train-writes-the-squash-message.md)). When those
commits carry no `Signed-off-by` trailer, `signed_message` raises
`NoSignOffError`. On a dependency branch the train has skipped the pull request
with a note that a person lands it, while the run stays green.

Many aged bot dependency updates stay in that state because the update bot
opened them before the repository adopted commit sign-off, or without the preset
that adds it. Renovate recreates a pull request when a configured label (by
default `rebase`) is present and removes that label after rebasing (Renovate
docs, configuration options, `rebaseLabel` and `addLabels`,
<https://docs.renovatebot.com/configuration-options/#rebaselabel>); the new
commit uses the repository's current commit configuration. Onboarding pull
requests on heads such as `renovate/configure` remain a person's act.

## Decision

- **Settings.** The reusable workflow
  `.github/workflows/merge-train.yml` gains `regenerate_label` (default
  `rebase`, empty turns the request off) and `onboarding_branches` (default
  `renovate/configure`, space-separated exact head names). They are passed as
  `TRAIN_REGENERATE_LABEL` and `TRAIN_ONBOARDING_BRANCHES` and validated in
  `settings` in `tools/merge_train.py` (`check_regenerate_label` refuses commas,
  newlines, or more than 50 characters).

- **Guard label.** `REGENERATION_REQUESTED_LABEL` (`merge-train: regeneration
  requested`) lives in `tools/merge_train.py` beside `URGENT_LABEL`, not in the
  inline `train_rules.py` block: refresh and hold labels are shared with the
  workflow rules step, while regeneration is train-owned and only used when
  checking sign-off on dependency heads.

- **`unsigned_update_note`.** On `NoSignOffError` for a dependency head,
  `signed_message` calls `unsigned_update_note` so every caller (`land`,
  `arm`, `enqueue`, `re_arm_after_review`) shares one path. Onboarding heads,
  heads that fail the ADR-0093 `is_bot_dependency` rule (including a person's
  commit on the head), or an empty `regenerate_label` keep the previous
  `SKIP_UNSIGNED` note and write nothing. Otherwise:
  - Guard absent with a complete label read (`totalCount` matches every
    returned node): one POST of `[regenerate_label, guard]` when `switch` is
    `on`, then the asked note; when `switch` is `off`, the `would ask` note and
    no write. When the guard is absent but the label connection is incomplete,
    the train records that the labels were not fully read and asks nothing.
    The guard is added to the in-memory pull so a second call in the same sweep
    writes nothing.
  - Guard present and `regenerate_label` still on the pull: the waiting note, no
    write.
  - Guard present and `regenerate_label` gone after the bot recreated the head,
    still unsigned: the still-unsigned note, no write.
  - A person removing the guard allows one more request; no write while the
    guard remains.
  - A signed head lands as before; the train does not remove the guard label (it
    records the one request).
  - When `switch` is `off`, `dry_note_unsigned_dependencies` still notes `land`
    rows as before; for `behind` dependency heads it follows the same
    `updatable` gate the live `advance` path uses, so a pull the train would not
    update gets that skip note and no `would ask` note.

- **Tests.** `tests/test_merge_train.py` covers ask, wait, still unsigned, land
  after sign-off, one label write per sweep
  (`test_unsigned_dependency_regenerate_labels_once_when_signed_message_runs_twice`),
  person author,
  empty label, invalid setting, dry switch, next clean pull
  (`test_after_an_asked_unsigned_dependency_update_the_next_clean_pull_lands`),
  and a planted case without the guard check
  (`test_planted_unsigned_regenerate_without_guard_check_relabels`), a person
  commit on the head (`test_planted_person_commit_on_bot_update_gets_no_recreate_ask`),
  the bot-commit read bound (`test_bot_commit_count_boundary_for_recreate_ask`),
  incomplete and complete label reads
  (`test_planted_guard_beyond_label_page_gets_no_ask`,
  `test_complete_label_read_of_fifty_asks_once`), and dry reporting behind
  rows through `updatable`
  (`test_switch_off_linear_history_behind_unsigned_update_reports_no_ask`). Onboarding
  tests on `renovate/configure` stay unchanged.

## Consequences

- **Positive:** Unsigned aged dependency updates get one automated recreate
  request instead of waiting indefinitely; min-age still counts from the pull
  request's opening.
- **Positive:** The guard label documents that the train already asked once.
- **Negative:** Repositories must configure the update bot's recreate label to
  match `regenerate_label` (default `rebase`).
- **Negative:** A bot that recreates without adding sign-off keeps the
  still-unsigned note until a person fixes the bot configuration or lands the
  pull request.
- **Negative:** Dependabot reads no recreate label, so a Dependabot update
  keeps the waiting note until a person asks it to recreate the pull request
  or lands it.

## Revisit trigger

Reconsider when the update bot gains a distinct label or API that does not
remove the recreate label after rebasing, or when the fleet standardizes another
default head for onboarding.