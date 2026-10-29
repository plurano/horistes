<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0064: An armed pull request is re-armed when retitled

- **Status**: Accepted
- **Date**: 2026-10-29
- **Authors**: Lusoris

## Context

When `merge_method` is `SQUASH`, the merge train arms auto-merge through
`enablePullRequestAutoMerge` with `commitHeadline` set to `<title> (#<number>)` and
`commitBody` built from the pull request's non-merge commits
([ADR-0037](0037-the-merge-train-writes-the-squash-message.md)). The forge stores that
headline and body on the `AutoMergeRequest` at the moment auto-merge is enabled and uses
them verbatim when it merges the pull request.

The pull request query in `.github/workflows/merge-train.yml` previously read only
`autoMergeRequest { enabledAt }`. When Renovate or a person changed the pull request's
title after auto-merge had been armed, nothing compared the armed `commitHeadline` with
the current title, so the pull request landed on the default branch with the stale
arm-time headline.

## Decision

- The pull request query (`QUERY` in `.github/workflows/merge-train.yml`) reads
  `autoMergeRequest { enabledAt commitHeadline mergeMethod }`.
- `drifted(pull, conf)` in the train rules returns `True` when `conf["method"]` is
  `SQUASH`, the pull request has an `autoMergeRequest` whose `mergeMethod` is `SQUASH`,
  and its `commitHeadline` differs from `<title> (#<number>)`.
- In `tools/merge_train.py`, `run()` checks each served pull request with `drifted(pull,
  conf)` after the security hold and before the `land` loop, skipping pull requests the
  security hold drafted in the same run. When a pull request has drifted, the train
  disables auto-merge via `disablePullRequestAutoMerge` (`DISARM`), calls `arm()` to
  re-enable auto-merge with the headline and body recomputed from the current title and
  commits, records `"re-armed: title changed after arming"` in `notes`, and returns
  `re-armed #<number>` as the repository's one write for that run.
- When `switch` is `off`, the sweep returns before the check and remains read-only.

## Consequences

An armed squash pull request whose title changes before the forge merges it is re-armed on
the next sweep with the current `<title> (#<number>)` headline and current commit body.
Re-arming takes the repository's one write for that run and needs only the
`pull-requests: write` permission the train already holds.

Tests: `test_retitled_armed_pull_request_is_re_armed_with_new_headline` verifies that
`DISARM` then `ARM` is sent with the updated headline;
`test_planted_drifted_that_never_fires_leaves_stale_headline_armed` plants `drifted()`
returning `False` and leaves the stale headline armed;
`test_retitled_armed_pull_request_held_for_security_is_not_re_armed` verifies that an
update drafted by the security hold is not re-armed;
`test_armed_pull_request_with_matching_headline_is_not_re_armed`,
`test_merge_method_merge_does_not_re_arm_retitled_pull_request`,
`test_switched_off_does_not_re_arm_retitled_pull_request` and `test_drifted_rule` cover
the remaining paths.
