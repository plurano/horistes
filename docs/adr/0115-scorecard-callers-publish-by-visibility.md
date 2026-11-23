<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0115: Scorecard callers publish results for public repositories

- **Status**: Accepted
- **Date**: 2026-11-23
- **Authors**: Lusoris

## Context

The Scorecard service verifies the called workflow at the revision identified
by the signing certificate. Horistes is now public, so the service can read
that file. The reason for omitting `publish` from the caller has expired.
[ADR-0013](0013-scorecard-behind-a-switch.md) records the original decision.

## Decision

The repository's [own caller](../../.github/workflows/scorecard-check.yml)
and the [consumer template](../../templates/callers/scorecard.yml.tmpl) pass
`publish` with the same expression as `outside_hosts` on every trigger:
`${{ github.event.repository.visibility == 'public' }}`. This supersedes
only the caller clause of ADR-0013.

## Consequences

A public caller requests a signed result under its own repository name in
addition to the artifact and code-scanning report. Consumers with an older
rendered caller pick up publication when they render the updated template.
Both inputs evaluate to false for private, internal or missing visibility.
The [caller tests](../../tests/test_scorecard_calls.py) cover the public
inputs, the private-repository refusal and rejection of a literal
`publish: true`.
