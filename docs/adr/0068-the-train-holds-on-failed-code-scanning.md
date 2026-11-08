<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0068: The merge train holds on failed code scanning

- **Status**: Accepted
- **Date**: 2026-11-08
- **Authors**: Lusoris

## Context

The merge train periodically sweeps every repository the app is installed on, landing
the oldest clean pull request ([ADR-0029](0029-a-forge-app-drives-the-merge-train.md)).
The train's existing `verdict()` checks merge state, labels, draft status, age, and
review settlement, but checks suite redness via `is_red()` only when a pull request
is `BEHIND`. When a pull request is `CLEAN`, `verdict()` returned `"land"` without
verifying whether non-required checks on the head commit failed.

In observed case golusoris/sveltesentio#512 (commit 64f9f1a), the pull request reached
`CLEAN` merge state and merged while its CodeQL code-scanning results check run was red.
Code-scanning checks are reported by the forge's code-scanning service (or tools such
as CodeQL) as check runs or check suites. When not configured as required status checks
in branch protection rules, a failure does not change `mergeStateStatus` from `CLEAN`,
permitting the train to merge a commit with open security alerts.

## Decision

- Add a dedicated code-scanning check in `train_rules.py`: `has_red_code_scanning(pull)`.
- Inspect the head commit's `checkSuites` nodes from the GraphQL query data.
- Check suites from the code-scanning app (slug containing `"code-scanning"` or slug
  equal to `"github"`) are evaluated using `newest_suites()` deduplication.
- If any newest suite from a code-scanning app carries a conclusion in `RED`
  (`FAILURE`, `TIMED_OUT`, `CANCELLED`, `ACTION_REQUIRED`, `STARTUP_FAILURE`),
  `has_red_code_scanning()` returns a hold reason (`"code-scanning check failed"`).
- `verdict()` integrates `has_red_code_scanning()` immediately after `review_settle()`
  and before the accept/behind state checks.
- The hold applies to both `CLEAN` and `BEHIND` pull requests, preventing landing
  when code-scanning results are failing.
- The sweep's `advance()` refuses to arm auto-merge on a `BEHIND` pull request
  whose code-scanning results are red, and `re_arm_drifted()` refuses to re-arm
  a retitled pull request in the same state.

## Consequences

Pull requests with a failing code-scanning results check on their head commit do not
land, even if their forge merge state is `CLEAN`. The train reports the hold reason in
its sweep table and leaves the pull request alone until code scanning succeeds or a new
commit resolves the alerts. A newer successful run from the code-scanning app supersedes
an older failed run. Pull requests without code-scanning suites or with successful
checks continue landing normally.

Tests in `tests/test_merge_train.py` verify that a failed code-scanning suite holds a
`CLEAN` pull request, a successful code-scanning check allows landing, pull requests
without code-scanning suites land unchanged, a newer successful code-scanning suite
supersedes an older failed run, an armed pull request with a red code-scanning check is
not merged, a `BEHIND` pull request is not armed when code scanning is red, and a
retitled armed pull request is not re-armed when code scanning is red.
