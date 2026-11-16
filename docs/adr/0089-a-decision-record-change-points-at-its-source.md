<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0089: A decision record change points at its source

- **Status**: Accepted
- **Date**: 2026-11-16
- **Authors**: Lusoris

## Context

Run `make check-records` before submitting a decision record. The local check
addresses the recurring review findings in
[issue 197](https://github.com/plurano/horistes/issues/197): records restate
source code or make claims without evidence pointers. The replay
`test_adr_0066_as_landed_replays_red` in `tests/test_records.py` adds
[ADR-0066](0066-a-train-write-holds-the-landing-order.md) again and reproduces
both findings without editing the accepted record.

## Decision

`tools/records.py` judges blocks containing added or changed lines against the
merge base of HEAD and `BASE_REF`, which defaults to `origin/main`. A block is
a paragraph, a top-level list item with its continuation lines, or a fenced
block. A changed line binds its whole block; untouched blocks stay unjudged,
as `test_untouched_blocks_of_an_accepted_record_stay_unjudged` verifies.
Local new records are judged before staging too, as
`test_planted_untracked_new_record_is_judged` verifies in `tests/test_records.py`.

A changed block must not copy tracked source through a code span or fenced
line. `tools/records.py` compares substantive snippets with tracked source
outside Markdown and `docs/`; bare paths, identifiers and commands name the
source instead. The span and fence cases in `tests/test_records.py` hold this
rule; HTML comments contribute neither claims nor pointers.

Claim paragraphs under Decision or Consequences require a pointer. Numeric
claims outside References require one too; dates, ADR identifiers, list
numbers, code spans and link destinations do not count as numeric prose.
`tools/records.py` accepts a code span naming a tracked file or directory,
a `make` or `praetorctl` command, a defined test function, or an inline link
to a tracked file or an HTTP page. `test_pointer_kinds_pass` and
`test_pointers_to_nothing_fail` in `tests/test_records.py` cover these boundaries.

Without a merge base, `tools/records.py` exits with an input error rather than
guessing which records changed. Null commits, unknown refs, unrelated or
insufficient history cannot establish that scope; unreadable records and
exceeded input limits also fail closed. The base refusal cases and
`test_planted_record_beyond_the_line_limit_fails_closed` in
`tests/test_records.py` preserve the scope guarantee.

## Consequences

`Makefile` adds `make check-records` to `make verify` so authors can run the
same focused check locally. Authors of new records provide a source pointer
for each claim block and cite implementation files rather than copying their
code; `test_planted_added_record_without_a_pointer_fails`,
`test_planted_restated_span_fails_and_a_named_symbol_passes` and
`test_planted_restated_fenced_block_fails` in `tests/test_records.py` show
rejected changes and passing controls.

The changed-line boundary avoids reopening accepted records solely because
the check was introduced. Editing an old block makes that whole block subject
to the rules, as `test_changed_line_binds_restated_span_on_unchanged_line` in
`tests/test_records.py` verifies. Renaming a record adds it under its new path;
deleting a record supplies no added lines, covered by
`test_renamed_record_is_judged_as_an_addition` and
`test_deleted_record_has_no_added_lines` in `tests/test_records.py`.
