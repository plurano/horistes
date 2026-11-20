<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0100: Release-off follows a moved repository

- **Status**: Accepted
- **Date**: 2026-11-20
- **Authors**: Lusoris
- **Amends**: [ADR-0045](0045-the-merge-train-opens-no-release-pull-request-where-the-release-train-is-off.md)

## Context

The `release_off` setting names repositories as `owner/name`. Comparing only
the current full name silently loses an opt-out after a transfer or rename.
The repository then receives release runs again despite the caller's setting.
`test_release_off_follows_a_moved_repository` in
[tests/test_merge_train.py](../../tests/test_merge_train.py) reproduces both moves.

## Decision

[tools/merge_train.py](../../tools/merge_train.py) resolves unmatched entries once
per installation, while its existing metadata-read list token is still live.
Casefolded current names match without a lookup. Other entries use
`GET /repos/{owner}/{name}`; the returned integer repository ID identifies an
opted-out target. The forge already knows repository moves, so discovery replaces
a hand-kept owner map. No additional token permissions are needed.

Automatic redirect following stays disabled. At most one redirect is followed,
and only to the same API origin and either `{api}/repositories/<digits>` or
`{api}/repos/<owner>/<name>`, with a full URL match and no query or fragment.
A 404 at either hop matches nothing. A visible public repository whose ID is
absent from this installation also matches nothing.

Every other failure closes release fan-out for that installation for this run:
all its targets count as release-off. This includes refused requests after the
existing GET retries, malformed metadata, unsafe locations and a second
redirect. A warning in the summary names the setting entry, installation and
sanitized reason. Response bodies and tokens are never included. Other
installations continue independently, and the off list uses current full names.

`MAX_RELEASE_OFF = 50` bounds the setting and resolution loop.
`release_off_setting` in [tools/merge_train.py](../../tools/merge_train.py)
refuses more than 50 whitespace-separated entries, repeated entries included,
with the existing setting error before the rules module parses them. The bound
sits there rather than in the rules module inline in
[.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml),
whose run script is close to the run-script field limit of the pinned Praetor
engine's workflow scan.

## Consequences

- A current-name match costs no metadata request in that installation.
- Each unmatched entry costs one GET, or two with a redirect, before retries.
  There are at most 100 logical GETs per installation; the existing
  `READ_ATTEMPTS` retry bound applies to each GET.
- An entry for another installation is resolved independently and may return
  an unrelated public ID or 404. Neither outcome disables releases there.
- Discovery errors can postpone releases, preserving the caller's opt-out
  while leaving the merge sweep available.

## Alternatives

- **Owner map:** rejected because it duplicates forge discovery, needs manual
  maintenance and cannot cover arbitrary transfers and renames.
- **Repository IDs in the setting:** stable across moves, but would change the
  caller interface and require callers to discover and maintain numeric IDs.
  Resolving the existing names preserves that interface.

## Verification

The cases in [tests/test_merge_train.py](../../tests/test_merge_train.py) cover:

- `test_release_off_follows_a_moved_repository`: former owners, former names,
  and both allowed redirect paths.
- `test_release_off_exact_match_needs_no_repository_lookup` and
  `test_release_off_missing_repository_matches_nothing`: request cost and 404s.
- `test_release_off_resolves_repository_metadata_without_a_redirect` and
  `test_release_off_bounds_repository_lookups_per_installation`: direct metadata
  resolution, body confidentiality and the lookup bound.
- `test_release_off_lookup_failure_closes_only_its_installation`: refusal,
  exhausted retries, isolation and sanitized warnings.
- `test_release_off_refuses_unsafe_repository_redirect`,
  `test_release_off_refuses_a_second_repository_redirect` and
  `test_release_off_refuses_invalid_repository_metadata`: fail-closed guards.
- `test_release_off_repository_id_does_not_match_another_owner` and
  `test_release_off_public_repository_outside_installations_matches_nothing`:
  ID matching without name collisions.
- `test_release_off_setting_accepts_50_entries`,
  `test_release_off_setting_refuses_51_entries`,
  `test_release_off_setting_counts_repeated_entries` and
  `test_release_off_over_the_bound_stops_before_any_forge_call`: the input bound.
