<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0087: The regenerate commit message fits commitlint

- **Status**: Accepted
- **Date**: 2026-11-16
- **Authors**: Lusoris

## Context

The commit message body clause of
[ADR-0032](0032-the-merge-train-commits-generated-files.md) produces an
unwrapped paragraph and a sign-off using the bot login. With workflow run
43781873903 and the app identity in
`test_planted_regenerate_message_fits_commitlint` in
[tests/test_merge_train.py](../../tests/test_merge_train.py), the paragraph
is 131 columns and the sign-off is 101 columns. Both exceed the 100-column
`body-max-line-length` and `footer-max-line-length` limits of commitlint's
config-conventional, reproduced as assertions in that test.

## Decision

The pure `regenerate_message` builder in
[tools/merge_train.py](../../tools/merge_train.py) wraps the body paragraph
at 72 columns without splitting long words or hyphenated tokens. Its
sign-off uses the app slug as the name and keeps the full `[bot]` login in
the email. The shared `bot_slug` helper also supplies the existing slug
derivation in `close_issues`.

`commit_generated_files` calls the builder. The headline, commit author
and email, and `write_hold` matching remain unchanged; the rules heredoc
in [.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml)
does not change. This decision supersedes only ADR-0032's commit message
body clause.

The planted cases of `test_planted_regenerate_message_fits_commitlint`
in [tests/test_merge_train.py](../../tests/test_merge_train.py)
(`run-id`, `run-url`, `run-url-short`, `run-url-long`) capture the commit
mutation and check both line limits, the exact sign-off, and preservation
of the run reference and artifact name. Each case also checks that the run
reference and `regenerated-<sha>` stay whole on one line: `run-url-short`
with a letter-leading head fails if hyphenated words may break, and
`run-url-long` (a 96-column token) fails if long words may break.
`test_dependency_pull_with_valid_artifact_commits_generated_files` checks
the complete expected body through the stand-in forge. The existing
squash sign-off and write-hold tests in the same file provide controls.

## Consequences

- **Positive**: the numeric run ID and ordinary run URL cases fit the
  commitlint limits, as the two planted cases verify.
- **Neutral**: `squash_message` in the workflow copies sign-offs verbatim,
  so subsequent squash bodies carry the slug-form sign-off. The existing
  squash sign-off tests in `tests/test_merge_train.py` check preservation.
- **Negative**: a run URL longer than 100 columns would still exceed the
  limit. The builder deliberately preserves each token; wrapping cannot
  shorten such a URL without splitting it.
