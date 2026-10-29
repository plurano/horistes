<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0063: CodeQL runs on every pull request

- **Status**: Accepted
- **Date**: 2026-10-29
- **Authors**: Lusoris

## Context

The OpenSSF Scorecard SAST check counts the merged commits whose pull request had a SAST
run. The `pull_request` trigger of `.github/workflows/codeql.yml` carried a path filter
(`**/*.py`, `.github/workflows/**`, `actions/**`), so a pull request that changed only
documentation or other files merged without a CodeQL run: Scorecard found 28 of the
last 30 commits checked.

## Decision

The `pull_request` trigger of `.github/workflows/codeql.yml` has no path filter, so
CodeQL runs on every pull request. The push to `main`, the weekly schedule and the
manual run stay. This supersedes only the trigger clause of
[ADR-0043](0043-codeql-reports-to-code-scanning.md); the rest of that record stands.

## Consequences

Every merged commit has a SAST run. The engine derives the required checks of the branch
ruleset from the jobs that report on every pull request
([ADR-0043](0043-codeql-reports-to-code-scanning.md), second Decision bullet), so the
rendered ruleset `.github/rulesets/main.json` now requires the `CodeQL` context; it takes
effect on GitHub at the next remote ruleset sync. An outage of the forge's code scanning
then blocks merges without a defect in the repository. One more job runs
on every pull request, documentation changes included. Alerts still do not fail the
gate: code scanning shows them on the pull request.

Tests: `test_codeql_runs_on_every_pull_request` reads the workflow and refuses a path
filter on the `pull_request` trigger; `test_predicate_refuses_planted_path_filters`
plants `paths` and `paths-ignore` and the same check refuses both.

## References

- [ADR-0043](0043-codeql-reports-to-code-scanning.md)
