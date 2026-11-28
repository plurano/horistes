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
`test_adr_0066_as_landed_replays_red` in `tests/test_records_claims.py` adds
[ADR-0066](0066-a-train-write-holds-the-landing-order.md) again and reproduces
both findings without editing the accepted record.
The replay reads frozen inputs under `tests/fixtures/records/`, independent
of later train refactors, as `test_planted_adr_0066_replay_ignores_live_tree`
in `tests/test_records_claims.py` verifies.

## Decision

`tools/records.py` judges blocks affected by additions or contiguous deletions against the
merge base of HEAD and `BASE_REF`, which defaults to `origin/main`. A block is
a paragraph, a top-level list item with its continuation lines, or a fenced
block. After a blank line, continuation paragraphs must reach the list item's
content column, measured with four-column tab stops; `test_planted_item_continuation_requires_content_indent`
and `test_planted_list_item_holds_nested_paragraph` in `tests/test_records_blocks.py`
cover the boundary. Nested fences use that column and end when their container
ends, as `test_planted_nested_fence_judges_source_line` and
`test_unclosed_nested_fence_ends_with_its_item` verify. A fence may start on
the list marker's line; `test_planted_marker_line_fence_judges_source_line` in
`tests/test_records_blocks.py` checks copied source in that position.
Top-level markers may have up to three leading columns; markers at or past
the containing item's content column stay nested.
`test_planted_indented_top_items_cannot_share_a_pointer` and
`test_planted_nested_item_keeps_its_parents_pointer` in `tests/test_records_blocks.py`
verify that siblings need separate pointers while nested items share their parent's block.
A changed line binds its
whole block; untouched blocks stay unjudged, as
`test_untouched_blocks_of_an_accepted_record_stay_unjudged` verifies.
When whitespace-only lines are added, `tools/records.py` also selects the closest
surviving text on either side. Splitting a paragraph therefore subjects both
resulting blocks to the pointer rule. `test_planted_added_blank_binds_neighbouring_blocks`
in `tests/test_records_markdown.py` covers a detached pointer and two pointed blocks.
Local new records are judged before staging too, as
`test_planted_untracked_new_record_is_judged` verifies in `tests/test_records_diff.py`.

`tools/records_diff.py` reads changed names through NUL-delimited Git output and
tracks added lines and deletion boundaries in each literal path's diff with pinned prefixes.
A deletion binds adjacent surviving text only at a nonblank end of the removed
run; deleting a paragraph with its blank line leaves its neighbours unjudged.
`test_planted_deletion_only_pointer_removal_judges_surviving_claim` and
`test_deleted_paragraph_and_blank_bind_no_untouched_neighbour` in
`tests/test_records_diff.py` verify these boundaries. Textconv is disabled;
records with a `filter` or `working-tree-encoding` attribute other than
`unspecified` or `unset` are refused before diffing, so conversion cannot shift
the raw record's coordinates. `test_planted_textconv_cannot_shift_record_lines`
in `tests/test_records_diff.py` and `test_planted_record_conversion_attribute_fails_closed`
in `tests/test_records_git.py` verify these boundaries.
Line boundaries follow Git's LF coordinates, with CRLF treated like LF and
other separators retained within a line; `test_planted_lf_line_numbers_keep_edited_claim_visible`
in `tests/test_records_diff.py` and `test_crlf_record_matches_lf_blocks_and_preserves_comment_positions`
in `tests/test_records_blocks.py` cover these inputs.
Inter-hunk context is disabled too, and context lines never count as added,
as `test_planted_interhunk_context_judges_only_edits` and
`test_planted_hunk_body_excludes_context_and_deleted_lines` verify in
`tests/test_records_diff.py`.
User prefix settings and quoted Unicode paths cannot hide a changed record from `tools/records.py`;
`test_planted_user_diff_prefix_cannot_hide_a_record`,
`test_planted_quoted_unicode_record_is_judged` and
`test_planted_record_pathspec_is_literal` in `tests/test_records_diff.py` cover
these inputs without interpreting path headers.
Only a patch header before its first hunk can mark that patch as deleted;
`test_planted_dev_null_body_cannot_hide_an_added_claim` and
`test_planted_dev_null_is_a_deletion_only_in_the_diff_header` in
`tests/test_records_reads.py` keep an added header-like body line visible.
Type changes split into deletion and creation patches; only surviving patches
bind the record. `test_planted_record_type_change_keeps_surviving_side` in
`tests/test_records_git.py` covers both directions and refuses surviving symlinks.

A changed block must not copy tracked source through a code span or fenced
line. `tools/records.py` compares substantive snippets with tracked source
outside Markdown and `docs/`; bare paths, identifiers and commands name the
source instead. The span and fence cases in `tests/test_records_claims.py` hold this
rule; HTML comments contribute neither claims nor pointers. The nested fence
cases in `tests/test_records_blocks.py` preserve literal comment markers inside
code, using the same fence finder as the block parser.
Backtick fence info strings cannot contain backticks; refused openers stay
plain lines whose code spans are judged. Tilde info strings may contain backticks.
`test_planted_backticks_in_fence_info_leave_a_judged_span` and
`test_valid_fence_info_preserves_body_and_literal_comments` in
`tests/test_records_blocks.py` cover refused and accepted openers, including a later fence.

`tools/records_diff.py` reads only regular files, with a byte bound shared by
sources and records before decoding. Missing or non-text sources contribute
no text; non-regular records are input errors. The bounded-child symlink cases
in `test_planted_device_symlinks_are_not_read`, the read-size spy in
`test_planted_reader_caps_each_read` and the size boundaries in
`test_planted_reader_byte_limit` and `test_reader_byte_limit_boundary` verify
these guarantees in `tests/test_records_reads.py`.
The Git runner in `tools/records_diff.py` bounds stdout while reading under a
deadline, kills and reaps a child on either limit, and bounds stderr diagnostics.
`test_planted_git_output_byte_budget_reaps_child` and
`test_planted_git_deadline_kills_and_reaps_child` in `tests/test_records_git.py`
check refusal and cleanup, including output exactly at the byte budget.

`tools/records.py` forces textual diffs for records, refuses binary summaries
and treats invalid UTF-8 as an input error. Attributes cannot silently hide a
changed record; `test_planted_binary_attribute_cannot_hide_record` and
`test_planted_binary_summary_fails_closed` verify this in `tests/test_records_diff.py`.

Claim paragraphs under Decision or Consequences require a pointer. Numeric
claims outside References require one too; dates, ADR identifiers, list
numbers, code spans and link destinations do not count as numeric prose.
`tools/records.py` accepts a code span naming a tracked file or directory,
a `make` or `praetorctl` command, a defined test function, or an inline link
to a tracked file or an HTTP page. `test_pointer_kinds_pass` and
`test_pointers_to_nothing_fail` in `tests/test_records_claims.py` cover these boundaries.
Test pointers come from parsed function definitions rather than strings or
comments; invalid syntax and an exceeded AST node bound are input errors.
`test_planted_test_pointer_requires_ast_definition`,
`test_planted_unparseable_test_source_fails_closed` and
`test_planted_test_ast_node_limit_fails_closed` in `tests/test_records_markdown.py`
cover these boundaries without importing or executing the tracked tests.

Without a merge base, `tools/records.py` exits with an input error rather than
guessing which records changed. Null commits, unknown refs, unrelated or
insufficient history cannot establish that scope; unreadable records and
exceeded input limits also fail closed. The base refusal cases and
`test_planted_record_beyond_the_line_limit_fails_closed` in
`tests/test_records_diff.py` preserve the scope guarantee.

## Consequences

`Makefile` adds `make check-records` to `make verify` so authors can run the
same focused check locally. Authors of new records provide a source pointer
for each claim block and cite implementation files rather than copying their
code; `test_planted_added_record_without_a_pointer_fails`,
`test_planted_restated_span_fails_and_a_named_symbol_passes` and
`test_planted_restated_fenced_block_fails` in `tests/test_records_claims.py` show
rejected changes and passing controls.

The changed-block boundary in `tools/records.py` avoids reopening accepted records
solely because the check was introduced. Editing an old block makes that whole block subject
to the rules, as `test_changed_line_binds_restated_span_on_unchanged_line` in
`tests/test_records_claims.py` verifies. Renaming a record adds it under its new path;
deleting a record skips it without reading the absent file, covered by
`test_renamed_record_is_judged_as_an_addition` and
`test_deleted_record_has_no_added_lines` and `test_deleted_record_is_never_read`
in `tests/test_records_diff.py`.
