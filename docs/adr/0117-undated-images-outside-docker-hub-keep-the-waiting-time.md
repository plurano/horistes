<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0117: Undated images outside Docker Hub keep the waiting time

- **Status**: Accepted
- **Date**: 2026-11-23
- **Authors**: Lusoris

## Context

The family preset sets `minimumReleaseAge` to three days in
[`default.json`](../../default.json) ([ADR-0019](0019-update-proposals-from-renovate-configured-here.md),
[ADR-0020](0020-shared-update-bot-values-as-a-preset.md)).

[`default.json`](../../default.json) sets `automerge` to false for every update,
so the bot never merges on its own.

A package rule matches Docker digest updates and sets `minimumReleaseAge` to
null on every registry
([ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md)). Own
container images on `ghcr.io` match an earlier rule with the same null age.
Updates without a configured `minimumReleaseAge` get no internal status check
at all, so nothing holds them
([`status-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/status-checks.ts#L193-L201),
[`status-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/status-checks.ts#L245-L255)).

What stays pending under the preset: **version** updates of outside images on
registries other than Docker Hub (no `releaseTimestamp` from Renovate), and
**digest** updates of fork images, which the last package rule still keeps at
three days.

Renovate 44.257.9 defaults `minimumReleaseAgeBehaviour` to `timestamp-required`
([`options/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/config/options/index.ts#L2497-L2503),
[`configuration-options.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/configuration-options.md?plain=1#L3075-L3083)).
Under that behaviour, an update without a
`releaseTimestamp` is treated as not yet past the age; Renovate 42 made this
the default
([`minimum-release-age.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/key-concepts/minimum-release-age.md?plain=1#L523-L540)).

### Where Docker timestamps come from

The docker datasource sets `releaseTimestampSupport` to true but documents that
only Docker Hub supplies a timestamp, from `tag_last_pushed` on tag results
([`docker/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/modules/datasource/docker/index.ts#L107-L109),
[`tag_last_pushed`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/modules/datasource/docker/index.ts#L1206-L1212)).
GHCR, Quay, ECR, and other non–Docker Hub registries are listed as unsupported in
Renovate’s minimum-release-age documentation
([`minimum-release-age.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/key-concepts/minimum-release-age.md?plain=1#L779-L781)).

## Candidate paths checked in the source

Renovate 44.257.9 upstream sources at tag 44.257.9. Line numbers below match
that tag.

### Status quo (works)

`internalChecksFilter` defaults to `strict`
([`options/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/config/options/index.ts#L2566-L2572)).
When every candidate release is still pending, lookup sets `pendingChecks`
([`filter-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/process/lookup/filter-checks.ts#L251-L262)).
Branch creation is then skipped
([`branch/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/index.ts#L156-L163)).

Under `timestamp-required`, an undated update is always pending in
`computeInternalChecksStatus`
([`status-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/status-checks.ts#L276-L298)).

A Dependency Dashboard request (`unpend`) overrides that gate
([`branch/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/index.ts#L81-L86),
[`branch/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/index.ts#L156-L163)).

The dashboard stores the request by branch name, without recording a version
([`branch/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/index.ts#L81-L86)).
When every candidate is pending, lookup selects the highest pending release
([`filter-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/process/lookup/filter-checks.ts#L251-L262)).
The requested branch can therefore contain a newer release published after the
person checked the age. Before merging, the reviewer checks the release age of
the version in the pull request outside Renovate and merges only once that
version is at least three days old.

With `internalChecksFilter: strict`, undated outside images therefore get neither
a branch nor a pull request until a person judges release age elsewhere and
requests the update on the dashboard. The reviewer then checks the age of the
version in the pull request before merging.

### Time-based pull request creation (refuted)

`prCreation: "not-pending"` with `prNotPendingHours` would open a pull request a
fixed number of hours after the branch’s latest commit
([`pr/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/pr/index.ts#L267-L290),
[`configuration-options.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/configuration-options.md?plain=1#L4693-L4708)).

For an undated update the stability status is `yellow`. When `prCreation` is
`not-pending` or `status-success`, branch creation is skipped before any commit
exists
([`branch/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/index.ts#L351-L371)),
so the timer never starts.

With `internalChecksFilter: "none"` and `prCreation: "immediate"`, the branch
and pull request open at once with a pending `renovate/stability-days` status
that never turns green for an undated update
([`status-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/status-checks.ts#L84-L130),
[`status-checks.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/branch/status-checks.ts#L276-L298)):
no release-age wait, only a pending status on an open pull request.

### `registryUrls` (refuted for host-qualified names)

`getRegistryRepository` sends a package name whose first segment contains `.` or
`:` (for example `ghcr.io/...`, `quay.io/...`) to that host, regardless of
`registryUrls`
([`docker/common.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/modules/datasource/docker/common.ts#L313-L345)).
`registryUrls` applies only to names without an embedded registry host.

### Dated twin per image (unverified)

`packageRules[].overridePackageName`
([`options/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/config/options/index.ts#L2663-L2672),
[`configuration-options.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/configuration-options.md?plain=1#L4057-L4062))
can point lookup at a Docker Hub copy where `tag_last_pushed` yields a
timestamp
([`docker/index.ts`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/modules/datasource/docker/index.ts#L1206-L1212)).

**Unverified** in source at 44.257.9: that Renovate still writes the original
image name in the update, and that the Docker Hub copy carries the same tags and
digests as the image in the file. A repository may use this per image only after
all three proofs hold: a Renovate dry run shows the release timestamp, the same
dry run shows an unchanged image name in the file, and a comparison of both
registries shows that the twin lists the same tags as the source image, each tag
resolving to the same manifest digest. When any of those proofs is missing, that
image stays on the dashboard path in **Status quo (works)**; the preset does not.

### `registryAliases`

`registryAliases` rewrites registry hosts appearing in dependency files for
lookup (documented option text and manager list in
[`configuration-options.md`](https://github.com/renovatebot/renovate/blob/44.257.9/docs/usage/configuration-options.md?plain=1#L4879-L4909)).
It is a string substitution on registry URLs in files, not a datasource
timestamp hook. **Unverified** that it can attach `releaseTimestamp` to a docker
image update; the copies read for this record do not show that path.

## Decision

Keep the preset at `minimumReleaseAge: "3 days"` with Renovate’s default
`timestamp-required` behaviour. Do not add `timestamp-optional` to the shared
preset; per-package `timestamp-optional` is out of scope for this record.

The wait-keeping path for undated outside images is section **Status quo
(works)** above: no branch and no pull request until a person checks release age
outside Renovate and requests the update on the Dependency Dashboard, and no
merge until the reviewer checks outside Renovate that the version in the pull
request is at least three days old.

Per-image **overridePackageName** toward a Docker Hub twin is allowed only when
all three proofs in **Dated twin per image** hold; when any is missing, the
dashboard path in **Status quo (works)** applies. **Time-based pull request
creation** and host-qualified **`registryUrls`** do not keep a wait.

Revisit when upstream work on registry timestamps (renovatebot/renovate issues
38656, 39064, 38572, 38659; names only) lands in a Renovate release that the
update bot proposes.

## Consequences

- **Positive**: Undated version updates outside Docker Hub open no branch and
  no pull request on their own; the three-day policy is not bypassed while Renovate has no
  timestamp. A dashboard request is an explicit maintainer act after an external
  age check.
- **Negative**: Those updates need a Dependency Dashboard request; under the
  default `internalChecksFilter: strict` they do not appear as branches on
  their own.
- **Neutral**: Own-image, vulnerability, and digest exceptions unchanged
  ([ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md)). Issue
  titles above name upstream gaps; this repository does not depend on their
  resolution for the preset default.

## References

- [ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md): digest
  updates wait for no release age on the docker datasource.
- [`default.json`](../../default.json), [`tests/test_update_bot.py`](../../tests/test_update_bot.py).
- Renovate 44.257.9 sources cited in **Candidate paths checked in the source**.
