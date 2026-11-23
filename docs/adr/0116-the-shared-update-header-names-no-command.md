<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0116: The shared update header names no command

- **Status**: Accepted
- **Date**: 2026-11-23
- **Authors**: Lusoris

## Context

The family preset [`default.json`](../../default.json) sets `prHeader`, which
Renovate prepends to every dependency-update pull request. Before this decision
the checklist named repository-specific gates (`make verify-all`,
`standardsctl compile-context --verify`) and HISS items. Every repository that
extends the preset without those commands would still show that checklist.

Renovate 44.257.9 compiles `prHeader` with
[`template.compile(config.prHeader, config)`](https://github.com/renovatebot/renovate/blob/44.257.9/lib/workers/repository/update/pr/body/header.ts#L5-L9).
The template sees only fields on the merged configuration, not files in a
consumer repository, so the preset cannot learn which commands a repository
adopted.

The consumer documentation already states that a repository with a different
template sets its own `prHeader` in its `renovate.json`
([`README.md`](../../README.md) updates table). Horistes carries the template
from [`.github/pull_request_template.md`](../../.github/pull_request_template.md)
in its own `renovate.json` and checks it with `pr_header_problems` in
[`tests/test_update_bot.py`](../../tests/test_update_bot.py). The preset is
checked separately with `neutral_header_problems`, which refuses any checklist
item that contains a backtick (a command name).

## Decision

The preset `prHeader` keeps the Description and Pre-Merge Verification
Checklist sections with neutral checklist items only: no backtick code spans, no
HISS wording, no tool names. Horistes (and any repository that adopted the
family gate commands) sets its own `prHeader` in `renovate.json` with the
repository checklist.

`tests/test_update_bot.py` treats `prHeader` as a `REPLACED` key: a consumer
may hold a different value from the preset, but copying the preset value byte
for byte is still refused by `said_twice`.

## Consequences

- **Positive**: New consumers see a checklist that does not name missing
  commands. Repositories that adopted verify commands can name them locally.
- **Negative**: A repository that extends only the preset shows neutral
  checklist text until it adds its own `prHeader`.
- **Neutral**: `minimumReleaseAge`, vulnerability handling, and digest
  exceptions are unchanged. Template-matching checks still run on the
  repository file, not on the preset.

## References

- [ADR-0020](0020-shared-update-bot-values-as-a-preset.md): shared values live
  in `default.json`.
- [`default.json`](../../default.json), [`renovate.json`](../../renovate.json),
  [`tests/test_update_bot.py`](../../tests/test_update_bot.py).