<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0075: Caller pins name one ref

- **Status**: Accepted
- **Date**: 2026-11-09
- **Authors**: Lusoris

## Context

A caller pins a shared workflow on a commit, for example
`uses: plurano/horistes/.github/workflows/merge-train.yml@<commit> # main`.
The update bot has to move that pin when `main` moves, or the caller runs old
code without anyone noticing.

Renovate 44.181.7, run with `--dry-run=lookup` and a forge token against a
caller that extends this repository's `default.json`, measured three forms of
the same line:

- `# main`: the builtin `github-actions` manager moves the pin to the head of
  `main` (datasource `github-digest`, update type `digest`). Two such pins in
  one caller share one branch, so the caller gets one pull request.
- `# main 2026-11-02 (#96)`, a date and a pull request number after the ref:
  Renovate skips the pin with `skipReason: unversioned-reference`.
- no comment: skipped the same way.

A caller in the fleet's operations repository carried the dated form and
stayed a week behind `main` with no bot pull request. `pins.py check` passed
it: the only comment rule it ran was zizmor's `ref-version-mismatch`. Real
zizmor on `uses: plurano/horistes/.github/workflows/merge-train.yml@<commit> # main`,
with the commit behind main's head, reports `ref-version-mismatch` with the
annotation `branch points to commit <12 hex of the branch head>`. The forge
compare API `repos/<owner>/<name>/compare/<commit>...<ref>` gives status
`ahead` or `identical` for a commit on the branch's history, and `behind` or
`diverged` for a commit off it. Zizmor upstream issue 2378 reports this exact
case: a pin on a commit behind a branch head is flagged by ref-version-mismatch.

`default.json` already lets these updates through: its rule for own GitHub
releases sets `minimumReleaseAge` to `null` for `github-digest` on packages of
the own organisations, `plurano` among them, and no rule disables
`github-actions` digest updates.

A custom regex manager for these pins was also measured. It extracts the same
lines the builtin manager already extracts, so each caller gets two branches
for one move.

## Decision

`tools/pins.py check`, and `pin`, which runs it, refuses an outside use pinned
on a 40-hex commit unless the line comment after the value is one bare ref:
`[A-Za-z0-9][A-Za-z0-9._/-]*`, so `# main`, `# v1.2.3` and `# release/v2` pass,
and `# main 2026-11-02 (#96)`, `# v1.2.3 main`, `# tag=v1.2.3`, `#` alone and a
missing comment fail. The comment is read from where the composed YAML value
ends, so a `#` inside a quoted value is not taken for one. The check is local:
it asks no host and needs no token.

The builtin `github-actions` manager stays the one mechanism that moves these
pins. No custom manager is added, for the double extraction measured above.

`tests/test_update_bot.py` holds the preset conditions the bot needs: no release
age for `github-digest` of `plurano/horistes`, and no `enabled: false` rule that
matches its digest updates
(`test_preset_permits_horistes_digest_updates`,
`test_planted_presets_refuse_disabled_or_aged_horistes_digest`).

A comment on a branch is judged against that branch's history. When zizmor
reports `ref-version-mismatch` with the annotation
`branch points to commit <hex>`, `tools/pins.py check` asks the forge compare
API `repos/<owner>/<name>/compare/<commit>...<ref>`, with the forge client's
60-second deadline (`tools/forge.py`, `DEADLINE_SECONDS`). Only status `ahead`
or `identical` drops the finding. Any other status (`behind`, `diverged`, an
unknown one), a missing status or a failed call keeps the finding and appends
the reason, so the check fails closed (`tests/test_pins.py`,
`test_branch_comment_off_history_or_failing_compare_fails`). A tag mismatch
asks no compare.

Dropping that zizmor finding is a declared exception in `exceptions.yaml`
(file `tools/pins.py`, MEL category D, 120 days), tracked by zizmor upstream
issue 2378. It is revisited when this repository ships release tags or zizmor
judges branch history itself. This decision supersedes the clause of
ADR-0016 that a commit no release names is not judged.

## Consequences

- A caller with a dated or missing comment on a commit pin fails its workflow
  check until the comment is `# main` or a release tag; the FAIL line names the
  file, the line and the accepted form (`tests/test_pins.py`,
  `test_outside_commit_use_with_red_comment_fails`).
- Each move of `main` gives each caller one bot pull request, however many
  pins on `main` the caller holds.
- The caller templates render `# vX.Y.Z` (`templates/callers/*.tmpl`), which
  stays valid; this repository's own pins all carry `# vX.Y.Z`.
- A commit that a release names still answers to zizmor's
  `ref-version-mismatch`, which wants that release's tag as the comment
  (ADR-0016); a commit behind a branch head passes under that branch's comment
  while forge compare puts it on the branch's history.
