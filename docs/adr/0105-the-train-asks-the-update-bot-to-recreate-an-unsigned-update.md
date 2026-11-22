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
commit uses the repository's current commit configuration. On onboarding heads
such as `renovate/configure`, an unsigned head is never asked to be recreated,
while a signed head lands on the normal path (`person_branches` in
`.github/workflows/merge-train.yml` defaults to `release-please--` only).

## Decision

- **Settings.** The reusable workflow
  `.github/workflows/merge-train.yml` gains `regenerate_label` (default
  `rebase`, empty turns the request off) and `onboarding_branches` (default
  `renovate/configure`, space-separated exact head names; an unsigned head of
  these is never asked to be recreated, while a signed head lands on the normal
  path). They are passed as
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
    When the label read is complete but the head commit `pull_commits` read in
    `tools/merge_train.py` differs from `pull["headRefOid"]`, the train returns
    `SKIP_UNSIGNED_HEAD_MOVED` (`skipped: dependency update without
    Signed-off-by, head moved since the sweep read, no recreate asked`) in both
    dry and live runs and writes nothing.
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
    update gets that skip note and no `would ask` note. Dry reporting of
    `behind` rows in `dry_note_unsigned_dependencies` covers only rows
    `updatable` in `.github/workflows/merge-train.yml` arms and skips rows
    where `arming` is false, reporting `would ask` only for rows the live run
    would arm.

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
  `test_complete_label_read_of_fifty_asks_once`), moved heads and check
  ordering (`test_unsigned_dependency_update_head_moved_skips_recreate_ask`,
  `test_unsigned_dependency_update_incomplete_labels_precede_head_moved`,
  `test_planted_unsigned_head_moved_without_head_check_labels`), and dry
  reporting behind rows through `updatable` and its `arming` flag
  (`test_switch_off_linear_history_behind_unsigned_update_reports_no_ask`,
  `test_behind_unsigned_dependency_non_arming_reports_no_would_ask_and_writes_no_label`,
  `test_planted_dry_unsigned_behind_without_arming_skip_reports_would_ask`). Onboarding
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
- **Negative:** The add-labels endpoint takes only `labels` (GitHub REST docs,
  "Add labels to an issue", <https://docs.github.com/en/rest/issues/labels>) and
  Renovate recreates or rebases on the label, losing commits it did not write
  (Renovate configuration options, `rebaseLabel`,
  <https://docs.renovatebot.com/configuration-options/#rebaselabel>; Renovate
  source `lib/workers/repository/update/branch/handle-existing.ts` line 62 at
  commit `8172379176b12fb88b42de178fc1d97b87550ab6`); a commit a person pushes
  after `pull_commits` in `tools/merge_train.py` read the head commit, and
  before the bot acts on the label, can be lost; the comparison of that read
  with the sweep snapshot in `unsigned_update_note` narrows the window to that
  span; the train accepts the rest because the forge offers no head
  precondition on a label request.

## Revisit trigger

Reconsider when the update bot gains a distinct label or API that does not
remove the recreate label after rebasing, or when the fleet standardizes another
default head for onboarding. Reconsider when the forge's label request or the
update bot's recreate request takes an expected head, or when a person's commit
is reported lost after a recreate request.