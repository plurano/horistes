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
- The train reads `status { contexts { context state } }` on the head commit of
  every open pull request.
- A head commit without that context holds the pull request with the reason
  `status <name> absent: held`; a context in any state other than `SUCCESS`
  (`PENDING`, `FAILURE`, `ERROR`, `EXPECTED`) holds it with
  `status <name> <state>: held` (`required_status_hold`).
- The hold comes after the code-scanning hold and before the land and behind
  verdicts, so a held pull request is neither landed, armed nor updated.
- An armed pull request held this way is disarmed, and a retitled one is not
  armed again (`disarm_red_scanning`, `re_arm_drifted` in
  `tools/merge_train.py`).

With `required_status` empty the train reads the status but no rule uses it, so
every verdict is the one it was before.

## Consequences

- A caller opts in by setting `required_status`; callers that leave it unset
  keep their behaviour.
- A caller sets it only once a writer reports that status for every pull
  request the train serves, bot and person ones alike; otherwise every pull
  request without the status waits forever.
- Each run reads one more field per pull request in the same query.
- Tests in `tests/test_merge_train.py` hold the planted cases: absent and
  non-success held, success landed, unset unchanged, armed disarmed, retitled
  not re-armed.
