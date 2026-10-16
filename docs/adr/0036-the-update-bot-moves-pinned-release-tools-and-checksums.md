<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0036: The update bot moves pinned release tools and their checksums

- **Status**: Accepted
- **Date**: 2026-10-16
- **Authors**: Lusoris

## Context

[ADR-0019](0019-update-proposals-from-renovate-configured-here.md) recorded as a
negative consequence that actionlint was installed at a pinned version with a
SHA-256 checksum in `.github/workflows/ci.yml` and `Makefile`, but no manager
read those two files. The pin updates remained manual. Issue plurano/horistes#25
tracked automating this workflow. gitleaks is pinned the same way in the same
workflow, so this record covers it too.

In `.github/workflows/ci.yml`, `ACTIONLINT_VERSION` and `ACTIONLINT_SHA256`
pin the actionlint release archive (`actionlint_<version>_linux_amd64.tar.gz`),
and `GITLEAKS_VERSION` and `GITLEAKS_SHA256` pin the gitleaks binary archive
(`gitleaks_<version>_linux_x64.tar.gz`). In `Makefile`, `ACTIONLINT_VERSION`
enforces that local runs match the exact version CI expects.

The install steps in `.github/workflows/ci.yml` verify downloaded archives with
`sha256sum --check --strict`. A version incremented without updating the
matching checksum fails immediately during CI.

## Decision

- Custom regex managers in `renovate.json` manage `rhysd/actionlint` and
  `gitleaks/gitleaks` using the `github-release-attachments` datasource.
- The checksum pins in `.github/workflows/ci.yml` hold the release tag itself
  (`v1.7.12`, `v8.30.1`). To map a checksum, the datasource fetches
  `releases/tags/<currentValue>` and `releases/tags/<newValue>`
  (`lib/modules/datasource/github-release-attachments/index.ts`, `getDigest`).
  A version stripped of its `v` by `extractVersionTemplate` names no release
  there: a local dry run of that shape ended in a 404 and an update with no
  digest. The install steps strip the `v` for the archive name with
  `${ACTIONLINT_VERSION#v}` and `${GITLEAKS_VERSION#v}`.
- The `Makefile` pin has no checksum and keeps the bare version, which
  `make lint` compares with the installed actionlint, so its manager strips
  the `v` with `extractVersionTemplate: "^v(?<version>.+)$"`.
- In `.github/workflows/ci.yml`, regex managers match the version environment
  variable, the release asset comment, and the SHA-256 digest variable together.
  Because Renovate's default regex replacement only updates the first occurrence
  of `currentValue` within the matched string (`lib/workers/repository/update/branch/auto-replace.ts:301-304`),
  leaving the comment stale, `autoReplaceStringTemplate` is defined
  (`auto-replace.ts:286-291`, `486-491`). It renders the tag in the
  environment variable, the bare version in the comment through the template
  helper `replace` (`{{{replace '^v' '' newValue}}}`), and the new digest.
- In `Makefile`, a dedicated regex manager updates `ACTIONLINT_VERSION`
  (`currentValue` only) under the same dependency name `rhysd/actionlint`.
- Updates across files share a single branch and pull request: Renovate derives
  `branchTopic` from `depNameSanitized` (`lib/config/options/index.ts:2672`,
  `lib/workers/repository/updates/flatten.ts:38-41`) and groups upgrades with
  identical branch names together (`lib/workers/repository/updates/branchify.ts:34-40`).
  Consequently, `ci.yml` and `Makefile` updates for actionlint land in one branch.
- Two barriers defend against a stale checksum:
  1. The `github-release-attachments` datasource reads the release's published
     checksum file asset (`actionlint_<version>_checksums.txt`,
     `gitleaks_<version>_checksums.txt`) and maps the digest to the new release
     asset (`lib/modules/datasource/github-release-attachments/index.ts:33-52`, `143-176`).
  2. The workflow installation step runs `sha256sum --check --strict` against
     the downloaded archive. An incorrect digest fails the install step, before
     the tool runs.
- `customManagers[0]` remains the licence image manager so `tools/image_age.py`
  can read it by index without modification.
- `tests/test_update_bot.py` validates that all patterns match real files, that
  every checksum pin reads the release tag and no checksum manager strips it,
  that actionlint versions in `ci.yml` and `Makefile` name the same release, that digests
  are 64 hexadecimal characters, that `autoReplaceStringTemplate` renders
  valid re-matching blocks without stale versions, and that planted changes to
  comments, Makefile variables, or SHA placement fail detection.

## Verification

A local Renovate 44.164.6 dry run (`--platform=local --dry-run=lookup`) on
this tree proposes nothing. With planted older pins (actionlint `v1.7.11`,
gitleaks `v8.30.0`, each with its real checksum) it proposes actionlint
`v1.7.12` with digest `8aca8db9…` in `ci.yml` and `1.7.12` in `Makefile`, both
in branch `renovate/rhysd-actionlint-1.x`, and gitleaks `v8.30.1` with digest
`551f6fc8…` in branch `renovate/gitleaks-gitleaks-8.x`. The install step with
tag `v1.7.11` and the `v1.7.12` checksum exits 1 at `sha256sum --check`.

## Consequences

- **Positive**:
  - `actionlint` and `gitleaks` versions and checksums are updated
    automatically by Renovate.
  - Actionlint pins in `.github/workflows/ci.yml` and `Makefile` are updated
    together in a single pull request.
  - The install step's strict checksum check remains an independent second
    barrier against supply-chain tampering.
- **Negative**:
  - If upstream changes release asset naming, archive packaging, or checksum
    file formatting, the regex manager or datasource mapping will not match
    until updated.
- **Neutral**:
  - `tools/image_age.py` remains unchanged because the licence manager stays
    at index 0.

## References

- Fleet answer ledger, id amussis-next-20261016-0840, 2026-10-16 (paraphrased):
  the update bot moves the pinned actionlint release and its checksum, and a
  planted stale pin is shown to fail.
- plurano/horistes#25: Let the update bot move the pinned actionlint release
  and its checksum.
- Renovate source code:
  - `lib/modules/datasource/github-release-attachments/index.ts:33-52, 143-176`
    (finding checksum assets and mapping digests across releases).
  - `lib/workers/repository/update/branch/auto-replace.ts:286-291, 301-304, 486-491`
    (`autoReplaceStringTemplate` vs single replacement).
  - `lib/config/options/index.ts:2652, 2672` (`branchName` and `branchTopic` defaults).
  - `lib/workers/repository/updates/flatten.ts:38-41` (`depNameSanitized`).
  - `lib/workers/repository/updates/branchify.ts:34-40` (grouping updates by branch).
- [ADR-0019](0019-update-proposals-from-renovate-configured-here.md): Updates
  are proposed by Renovate from a configuration in this repository.
- [ADR-0028](0028-every-commit-is-scanned-for-secrets.md): Every commit is
  scanned for secrets in the gate, with the tool's own rules only.
