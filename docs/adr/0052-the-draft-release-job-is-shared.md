<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0052: The draft release job is shared

- **Status**: Accepted
- **Date**: 2026-10-21
- **Authors**: Lusoris

## Context

The job that creates a draft release on a pushed tag was duplicated across
calling repositories: `plurano/amussis` (`.github/workflows/release.yml`, job
`draft`) and `plurano/pertica` (`.github/workflows/release.yml`, job `draft`).
Both jobs fetched release files using `actions/download-artifact`, constructed
flags for `gh release create` (`--draft`, `--verify-tag`, `--title "$TAG"`,
`--generate-notes`, and `--prerelease` for a tag with a hyphen), and published
the files under `dist/*`.

Duplicating this job in each repository leaves input validation, action
pinning, runner selection, and release flags without central verification.

## Decision

- Provide a reusable workflow `.github/workflows/draft-release.yml` triggered
  on `workflow_call`.
- Expose inputs `runner` (string, default `ubuntu-26.04`) and `artifact`
  (string, default `release-files`).
- Refuse an artifact name that is not a plain name before downloading the
  artifact.
- Download the artifact with `actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333`
  (# v8.0.2) to `dist`.
- Declare `permissions: {}` at workflow scope; the job declares
  `contents: write`.
- Pass tag, repository, and token through environment variables only (`TAG`,
  `GH_REPO`, `GH_TOKEN`), never expanding expressions inside the run script.
- Declare `actions/download-artifact` in `hosts.toml` under `[actions]` with no
  host of its own: the artifact storage it reads is in the forge list.
- As a tag-push workflow, the job is exempt from the pull request draft stop
  step ([ADR-0013](0013-scorecard-behind-a-switch.md)).

## Consequences

- Callers replace the duplicated job with a call to `.github/workflows/draft-release.yml`.
- `interface.lock.json` records the new workflow without breaking existing
  interfaces (append-only ABI).
- `tests/test_draft_release.py` verifies workflow structure, input validation,
  the lack of unexpanded expressions, and bash execution with stable and
  prerelease tags.
