<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0049: The merge train leaves a frozen release pull request alone

- **Status**: Accepted
- **Date**: 2026-10-20
- **Authors**: Lusoris

## Context

The release job runs release-please with the merge-train app's token on every
sweep (decision records 0031, 0040). Its "Open or update the release pull
request" step rewrites the head of the open release pull request to match the
default branch, whatever labels a person set on it.

On 2026-10-20 the release pull request `VMAFx/vmafx#2411` carried
`autorelease: cut` and, from 11:54Z, `hold`. The step still rewrote its head four
times (10:38Z, 10:54Z, 11:38Z, 11:54Z), each as `plurano-merge-train[bot]`, and
replaced the cut a person had prepared. The sweep itself left the pull request
alone only because the caller kept `release-please--` in `person_branches`; a
caller that emptied that input would have let the sweep update it too.

## Decision

- **Frozen release pull request.** An open pull request whose head starts with
  `release-please--` is frozen when it carries a label from `hold_labels` or an
  `autorelease:` label other than `autorelease: pending`. The settings step reads
  the open pull requests, names each frozen one in a notice, and writes
  `frozen=true`; the update step runs only on `frozen == 'false'`.
- **The sweep never updates a release.** A head starting with `release-please--`
  or a pull request carrying any `autorelease:` label is left to a person,
  independent of `person_branches`.
- Release creation for merged release pull requests and the label moves keep
  running; only the rewrite of an open release pull request stops.

## Consequences

- A person freezes a release pull request by setting `hold` or a release label
  such as `autorelease: cut`, and unfreezes it by removing that label.
- The settings step makes one more forge read, the open pull requests (one page
  of 100).
- Planted cases in `tests/test_release_labels.py` and `tests/test_merge_train.py`
  run a cut and held release pull request behind the default branch; the mutants
  without the checks update it.
