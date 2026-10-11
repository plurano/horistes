<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0002: Versions, pins and the recorded interface

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

A caller depends on a workflow's inputs, secrets, outputs, job identifiers and
check names. Required status checks are matched by name, so a renamed job
silently leaves a required check without a result. A moving tag changes what a
caller runs without a change in the caller.

## Decision

- Releases carry semantic version tags. No moving major tag is offered.
- Callers, copies and every outside action used here are pinned to a full
  commit, with the version as a trailing comment.
- Outside actions and tools follow the fleet's minimum age of three days
  before adoption; a security fix is taken at once. Releases of this
  repository are adopted by its consumers without a waiting period.
- The interface of every reusable workflow and composite action is recorded in
  `interface.lock.json`. `tools/interface.py check` fails when the record does
  not describe the tree, and when a change against the base commit breaks a
  caller while `interface_major` was not raised. A break is: a removed file,
  input, secret, output or job; a changed input type or check name; an input or
  secret that becomes or arrives required; a new write scope.
- A raised `interface_major` is released as a new major version.
- Tools of the gate are installed from `requirements/tools.txt` with hashes;
  actionlint is installed at one asserted version with a checksum.

## Consequences

- **Positive**: a break cannot be merged unnoticed; what a caller runs changes
  only with a change in the caller; the update pull request can show the
  interface difference.
- **Negative**: every interface change needs the record regenerated
  (`make interface-snapshot`); every release needs pins moved in the copies and
  callers.

## References

- Fleet answer ledger, ids fleet-update-min-age and own-release-age-rule
  (paraphrased): outside updates wait three days, own releases do not.
