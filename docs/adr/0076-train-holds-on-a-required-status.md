<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0076: Train holds on a required status

- **Status**: Accepted
- **Date**: 2026-11-10
- **Authors**: Lusoris

## Context

The merge train lands a clean pull request and updates a behind one once its
own rules pass: merge state, check suites, draft state, labels, minimum age,
review settling, code scanning and review bodies. A fleet can need one more
precondition the train cannot judge itself, for example that another system has
accepted the change. Such a system can report its verdict as a commit status
on the head commit; the train had no way to require one.

## Decision

The merge-train reusable workflow (`.github/workflows/merge-train.yml`) takes an
opt-in input `required_status` (string, default empty, which turns the check
off).

When `required_status` names a commit status context:

- The name must match `^[A-Za-z0-9][A-Za-z0-9._/:-]{0,99}$`; any other value
  stops the run with a setting error before the first forge call
  (`check_required_status` in the train rules module).
- The query declares `$status: String!` and reads
  `status { context(name: $status) { state } }` on the head commit of every
  open pull request (`pull_query` in the train rules module). The token the
  train mints then also carries Commit statuses read (`STATUS_RIGHTS` in
  `tools/merge_train.py`).
- A head commit without that context holds the pull request with the reason
  `status <name> absent: held`; a context in any state other than `SUCCESS`
  (`PENDING`, `FAILURE`, `ERROR`, `EXPECTED`) holds it with
  `status <name> <state>: held` (`required_status_hold`).
- The hold comes after the code-scanning hold and before the land and behind
  verdicts, so a held pull request is neither landed nor updated.
- The train arms no pull request for auto-merge. `updatable` picks the BEHIND
  pull requests decision record 0044 allows and arms none of them; the train
  lands each one itself once it is CLEAN. Where the repository allows no
  auto-merge, an unarmed BEHIND pull request is still left as it is.
- Each run reads every open pull request's own status rather than its verdict
  reason, so an earlier hold cannot hide a missing status. An armed pull
  request the status holds is disarmed, and an armed pull request whose title
  changed is disarmed and not armed again (`disarm_red_scanning`,
  `re_arm_drifted` in `tools/merge_train.py`).
- A pull request in the merge queue that the status holds is taken out of the
  queue with the `dequeuePullRequest` mutation in the same step.
- A dependency pull request the status holds still gets its generated files
  committed (`is_candidate`): that commit is part of the head the status writer
  judges, so skipping it would need a second status round.

When `required_status` is empty, neither the query nor the token rights carry
the status, and every verdict is the one it was before.

## Consequences

- A caller opts in by setting `required_status`; callers that leave it unset
  keep their behaviour.
- A caller sets it only once a writer reports that status for every pull
  request the train serves, bot and person ones alike; otherwise every pull
  request without the status waits forever.
- Opting in needs `statuses: read` in the app manifest
  (`apps/merge-train.json`) and each installation accepting it. An
  installation token cannot carry a right the app was not granted, so until
  then the run fails for that repository.
- Auto-merge that a person arms between train runs does not wait for the
  train, so a hard gate also needs the context required in the repository's
  branch protection.
- Tests in `tests/test_merge_train.py` hold the planted cases: absent and
  non-success held, success landed, unset unchanged; the status selected only
  when set and only on the head commit; Commit statuses read minted only when
  set, and a status read refused without it; a BEHIND pull request updated
  without arming; a retitled armed pull request disarmed and not armed again;
  an armed or queued pull request with an earlier hold and no status disarmed
  or dequeued; a held dependency pull request given its generated files.
