<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0086: An exception for a finding without a file names no path

- **Status**: Accepted
- **Date**: 2026-11-15
- **Authors**: Lusoris

## Context

[ADR-0007](0007-one-own-exception-list.md) requires one file per entry in
[exceptions.yaml](../../exceptions.yaml) and refuses a missing field.
The Scorecard check `CIIBestPracticesID` reports on the repository, with no
file location. The fleet security-alert flow matches an entry with a path
only to alerts on that file, and an entry without a path only to alerts
without a file location (fleet order cited below). The entry for issue 116
named `.bestpractices.json`, so it never matched that alert.
[ADR-0069](0069-the-first-release-is-0-1-0-from-release-please.md) records why
the four release criteria stay `Unmet` until the first release.

## Decision

`path` is optional in [tools/exceptions.py](../../tools/exceptions.py).
An entry without it names a rule whose finding has no file. Such an entry
never covers an inline suppression in a file. A present path still has to
be non-empty text naming a tracked file. Every other field, the category
interval, the expiry check and the duplicate check stay unchanged.
Two entries naming the same rule alone are duplicates; a rule alone and
the same rule with a path are distinct entries.

This narrows ADR-0007's decision that one entry names one file. ADR-0007
stays unchanged as the record of that earlier decision. Only the path of
the `CIIBestPracticesID` entry is removed; its other fields, including its
2026-11-23 expiry, stay unchanged in `exceptions.yaml`.

[tests/test_exceptions.py](../../tests/test_exceptions.py) covers acceptance
without a subject file, refusal of empty and non-text paths, duplicate
rules without paths, coexistence with a path entry, and the boundary that
only a path entry covers an inline suppression. Run
`.venv/bin/python -B -m pytest tests/test_exceptions.py` and
`make check-exceptions` to check the reader and the list.

## Consequences

- **Positive**: the repository-level alert's exception can match the alert
  and is counted, dated and reviewed like the other entries in
  `exceptions.yaml`.
- **Negative**: an entry without a file stands for the whole repository,
  so only a finding without a file location may use it. Reviewers must
  check that location before accepting such an entry.
- **Neutral**: inline suppressions still need a path entry, as
  `test_own_rule_only_entry_does_not_cover_an_inline_suppression` verifies.

## References

- [ADR-0007](0007-one-own-exception-list.md): the original exception format.
- [ADR-0073](0073-suppressions-name-a-mel-category.md): category intervals.
- Fleet answer ledger, id horistes-cii-alert-exception-source, 2026-11-15
  (paraphrased): the exception for the best-practices alert lives in this
  repository's exceptions.yaml.
- Fleet order horistes-cii-entry-rule-only-20261115, 2026-11-15 (paraphrased):
  the fleet's reader has landed and the alert carries no file location, so
  the entry drops its path and keeps its other fields.
