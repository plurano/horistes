<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0097: The train requires an allowed status creator

- **Status**: Accepted
- **Date**: 2026-11-20
- **Authors**: Lusoris

## Context

[ADR-0076](0076-train-holds-on-a-required-status.md) lets a caller require a
successful commit status on the pull request head. Accounts with push access
can publish a status for that same context. When a caller relies on a watcher
to judge a head, a success from any other account must keep the pull request
held. [GitHub commit status API](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status)

## Decision

Callers can set `required_status_creators` to a comma-separated list of
GitHub account logins. The train trims spaces around entries and compares
logins case-insensitively. Each login is at most 39 characters, including any
`[bot]` suffix. The base uses ASCII letters, digits or single hyphens and
starts and ends with a letter or digit. The suffix is part of the identity.
An invalid entry stops the sweep with a setting error before its first forge
call.

The existing head status query reads `StatusContext.creator.login` with the
state. If the allow-list is set, the existing status hold treats a creator
outside the list or missing creator data as an absent status. The hold reason
identifies the creator when available. [GitHub `StatusContext` schema](https://docs.github.com/en/graphql/reference/commits#statuscontext)

## Consequences

For `lane-judged`, set `required_status` to `lane-judged` and name the watcher
login in `required_status_creators`. The default empty allow-list accepts any
creator. The existing `statuses: read` permission covers the status query.
The train still applies the arming and merge queue rules from ADR-0076.

`tests/test_merge_train.py` covers a listed creator, an unlisted creator,
missing creator data, the empty default, and invalid input before a forge call.
