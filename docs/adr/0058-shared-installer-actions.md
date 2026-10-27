<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0058: Shared installer actions

- **Status**: Accepted
- **Date**: 2026-10-27
- **Authors**: Lusoris

## Context

Downstream callers across the fleet (`golusoris/golusoris` and
`golusoris/template-native-gpu`) maintain duplicate copies of
`scripts/ci/lib/verified-download.sh` and `scripts/ci/install-gitleaks.sh`.
Both copies duplicate bounded HTTPS-only asset downloading with SHA-256
verification and platform-specific extraction of the Gitleaks binary.
In this repository, `.github/workflows/secret-scan.yml` previously used an
inline curl and sha256sum step to fetch and unpack the pinned Gitleaks binary.

## Decision

- Two composite actions are established under `actions/`:
  - `actions/verified-download`: composite action wrapping
    `actions/verified-download/verified-download.sh` (`fetch_verified`),
    taking inputs `url`, `sha256`, `output` (required), `max-bytes`
    (default `33554432`), and `timeout-seconds` (default `120`). Exposes
    output `lib` holding the absolute path of `verified-download.sh`.
  - `actions/install-gitleaks`: composite action running
    `actions/install-gitleaks/install-gitleaks.sh`, taking inputs
    `version` (no leading `v`), `sha256`, and `install-dir` (required).
    Sources `../verified-download/verified-download.sh` relative to its own
    directory without duplication, verifies Linux/x86_64 host architecture,
    unpacks `gitleaks` refusing symlink entries, verifies the version
    read-back, installs mode `0755`, and exposes output `path`.
- Inputs in both action steps are passed via environment variables, never
  embedded directly into `run:` scripts, preventing template injection
  ([ADR-0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md)).
- `.github/workflows/secret-scan.yml` replaces its inline download and tar
  unpack with a sparse checkout of `actions/` from
  `job.workflow_repository@job.workflow_sha` and calls
  `./secret-scan-source/actions/install-gitleaks`.

## Consequences

- **Positive**:
  - Download verification and Gitleaks installation logic are centralized in
    one shared home.
  - Consumers drop duplicated shell scripts in favor of `uses: plurano/horistes/actions/<name>@<sha>`.
  - `.github/workflows/secret-scan.yml` shares the same verified installer
    path as consumers.
- **Negative**:
  - None.
- **Neutral**:
  - Zizmor's `self-repository` audit for `./secret-scan-source/actions/install-gitleaks`
    is accepted in `.github/zizmor.yml` until actionlint supports the `$/` syntax.
