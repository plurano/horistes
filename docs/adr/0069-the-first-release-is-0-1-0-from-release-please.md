<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0069: The first release is 0.1.0 from release-please

- **Status**: Accepted
- **Date**: 2026-11-08
- **Authors**: Lusoris

## Context

[ADR-0024](0024-public-before-the-first-release.md) left the first release open, to be
decided on its own later. Until then callers pin a full commit
([ADR-0002](0002-versioning-and-pinning.md)). `interface.lock.json:50` records
`interface_major` 0.

Under [ADR-0031](0031-the-merge-train-app-runs-release-please.md), the merge train opens
release pull requests for a repository that holds both `release-please-config.json` and
`.release-please-manifest.json` on its default branch.

## Decision

- Commit `release-please-config.json` and `.release-please-manifest.json` to configure
  release-please for the merge train.
- Set `release-please-config.json` with strategy `"simple"` for package `"."` with these
  values:
  - `"package-name": "horistes"`, naming the repository's component.
  - `"changelog-path": "CHANGELOG.md"`, directing release-please to write
    `CHANGELOG.md`.
  - `"initial-version": "0.1.0"`, because the interface is below 1.0
    (`interface.lock.json:50` records `interface_major` 0).
  - `"bump-minor-pre-major": true`, so breaking changes bump the minor version while
    below 1.0.0.
  - `"include-component-in-tag": false`, so the tag is `v0.1.0` per
    [ADR-0002](0002-versioning-and-pinning.md).
- Set `.release-please-manifest.json` holding `{}` so release-please discovers package
  `"."` as new and proposes `0.1.0`.
- The train opens the release pull request, which is labelled `hold` once it is open
  and stays unmerged until milestone "0.1.0 — Public path" closes.
- While held the train does not update it
  ([ADR-0049](0049-the-merge-train-leaves-a-frozen-release-pull-request-alone.md)).
- When the milestone closes a person removes `hold`, the train refreshes the pull
  request, and a person merges it, which is the release act, after which the train
  creates the release ([ADR-0031](0031-the-merge-train-app-runs-release-please.md),
  [ADR-0040](0040-the-merge-train-leaves-a-release-to-its-writer.md)).
- No tag is made by hand, because a second release path next to
  [ADR-0031](0031-the-merge-train-app-runs-release-please.md) would not move the release
  labels.
- `extra-label` is not set, because it would hold every later release pull request too.

## Consequences

- **Positive**:
  - The release pull request carries `CHANGELOG.md` and the manifest update.
  - The release pull request and the release both come from the train's release job;
    the one step a person takes is the merge.
  - `tests/test_release_config.py` holds both files to the values above.
- **Negative**:
  - Four badge criteria (`version_unique`, `version_semver`, `version_tags`,
    `release_notes`) stay `Unmet` until the merge.
  - Callers keep pinning a full commit until `v0.1.0` exists.
