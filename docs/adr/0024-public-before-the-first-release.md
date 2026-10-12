<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0024: The repository turns public before its first release

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

[ADR-0022](0022-public-consumers-call-this-repository-directly.md) fixed the
order in which this repository becomes public: its real name, a proof that the
history holds no private content, the first release, then the change of
visibility. Steps 1 and 2 are done: the name is Horistes
([ADR-0023](0023-the-name-is-horistes.md)), and the history was replayed into
a new repository with the old one kept private as `plurano/horistes-archive`.

No release exists. The interface record stands at `interface_major` 0, and
there is no release workflow in the tree. A release now would fix a version
before any public consumer has called a workflow from here.

## Decision

- The change of visibility comes before the first release. The order of
  ADR-0022 becomes: name, proof of the history, change of visibility (the
  operator's act), first release.
- The first release is decided on its own, later.
- Until the first release, a caller pins a full commit of the default branch
  and has no version to name in its trailing comment
  ([ADR-0002](0002-versioning-and-pinning.md)).

This replaces step 3 and step 4 of the order in ADR-0022. Its other decisions
stand.

## Consequences

- **Positive**:
  - Public repositories of the family can call the shared jobs without waiting
    for a release.
  - The first version is chosen after public callers have used the interface.
- **Negative**:
  - The name is final with the change of visibility, not with the first
    release: from then on a public caller can name this repository.
  - A preset reference follows the default branch until a release exists to
    name.
- **Neutral**: the release read-back
  ([ADR-0014](0014-release-read-back-without-credentials.md)) and the
  interface check apply unchanged once a release is made.

## References

- Fleet answer ledger, 2026-10-12 (paraphrased). horistes-first-release: no
  release before the repository is public; it turns public without a tag; a
  short superseding record changes the order of ADR-0022; the first release
  is decided later as its own question.
