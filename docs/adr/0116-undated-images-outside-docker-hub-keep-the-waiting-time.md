<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0116: Undated images outside Docker Hub keep the waiting time

- **Status**: Accepted
- **Date**: 2026-11-23
- **Authors**: Lusoris

## Context

The family preset sets `minimumReleaseAge` to three days in
[`default.json`](../../default.json) ([ADR-0019](0019-update-proposals-from-renovate-configured-here.md),
[ADR-0020](0020-shared-update-bot-values-as-a-preset.md)). Renovate 44.257.9
defaults `minimumReleaseAgeBehaviour` to `timestamp-required` (see
`.lane-input/preset/options-index.ts` (default `timestamp-required`, line 2502)
and `.lane-input/preset/configuration-options.md` around the
`minimumReleaseAgeBehaviour` option). Under that behaviour, an update without a
`releaseTimestamp` is treated as not yet past the age
(`.lane-input/preset/minimum-release-age.md`, lines 523–540).

### Where Docker timestamps come from

The docker datasource sets `releaseTimestampSupport` to true but documents that
only Docker Hub supplies a timestamp, from `tag_last_pushed` on tag results
(`.lane-input/preset/renovate-src/lib_modules_datasource_docker_index.ts`,
lines 107–109 and 1206–1212). GHCR, Quay, ECR, and other non–Docker Hub
registries are listed as unsupported in Renovate’s minimum-release-age
documentation (`.lane-input/preset/minimum-release-age.md`, lines 778–781).

### What happens to an undated update under the preset

When `internalChecksFilter` is `strict` (Renovate’s default; see
`.lane-input/preset/minimum-release-age.md`, lines 546–575), Renovate does not
open a branch or pull request while `renovate/stability-days` is pending.
Undated candidates are marked pending under `timestamp-required`
(`.lane-input/preset/renovate-src/lib_workers_repository_process_lookup_filter-checks.ts`,
lines 178–202 and 225–234). They appear under Pending Status Checks on the
Dependency Dashboard; a maintainer can request the update there (or use
self-hosted `checkedBranches`) after judging release age by other means.

The preset does not set `minimumReleaseAgeBehaviour` or `internalChecksFilter`,
so these defaults apply to every consumer.

### Registry mirror path (Docker Hub timestamps)

Renovate’s docs describe prepending a timestamped registry URL via
`packageRules.registryUrls` for Maven and PyPI
(`.lane-input/preset/minimum-release-age.md`, lines 1065–1070). The same page
gives no docker example. A search of the Renovate 44.257.9 docker datasource
sources under `.lane-input/preset/renovate-src/lib_modules_datasource_docker_*`
shows no `registryUrls` handling tied to release timestamps for images mirrored
on Docker Hub. **Unverified**: whether aiming a package at Docker Hub through
`registryUrls` or `registryAliases` would attach `tag_last_pushed` to a copy
published on GHCR or Quay. Consumers may try that only where they can confirm
timestamps in Renovate debug logs
(`.lane-input/preset/minimum-release-age.md`, “packageFiles with updates”).

Upstream work is tracked in renovatebot/renovate issues 38656, 39064, 38572,
and 38659 (names only; see docker `releaseTimestampNote` in the datasource
source).

## Decision

Keep the preset at `minimumReleaseAge: "3 days"` with Renovate’s default
`timestamp-required` behaviour. Do not add `timestamp-optional` to the shared
preset or as a blanket policy. Per-package `timestamp-optional` is out of scope
for this record.

For container images on registries that provide no release timestamp, the
wait-keeping path under the preset is: the update stays on the Dependency
Dashboard until a person requests it after checking the release age outside
Renovate, plus the Docker Hub mirror path only where logs prove a timestamp.
Digest updates of outside images on Docker Hub remain covered by
[ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md).

## Consequences

- **Positive**: Undated non–Docker Hub images do not auto-merge during the
  three-day window; behaviour matches Renovate’s secure default since version 42.
- **Negative**: Those updates need dashboard approval or an external age check;
  they do not open branches under `internalChecksFilter: strict` on their own.
- **Neutral**: Own-image and vulnerability exceptions unchanged. Issue titles
  above name upstream gaps; this repository does not depend on their resolution
  for the preset default.

## References

- [ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md): digest
  exception on Docker Hub.
- [`default.json`](../../default.json), [`tests/test_update_bot.py`](../../tests/test_update_bot.py).
- Renovate 44.257.9 inputs under `.lane-input/preset/` (tag noted in
  `.lane-input/preset/notes.md`).