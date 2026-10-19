<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0045: The merge train opens no release pull request where the release train is off

- **Status**: Accepted
- **Date**: 2026-10-19
- **Authors**: Lusoris

## Context

A repository with `release-please-config.json` and
`.release-please-manifest.json` on its default branch gets a second job after
the sweep: release-please with a token of the merge-train app limited to that
repository, contents, pull requests, and workflows write (decision records
0031, 0040).

`cordanaLLM/Aegis-OS` keeps its release train off: its own `release-please.yml`
runs only when the repository variable `RELEASE_TRAIN_ENABLED == 'true'`, and its
value is `false`. The merge train ignored that setting because the sweep only
checked for the presence of the two configuration files. On 2026-10-19T14:45Z
the app opened release pull request #388 there (head
`release-please--branches--main--components--aegis-os`), following earlier #120
on the same head.

## Decision

- **Caller-configured opt-out.** A new `workflow_call` input `release_off`
  lists repositories (`owner/name` separated by spaces) where the train opens
  and pushes no release pull request.
- **No repository variable reads.** The caller list was chosen over reading
  `RELEASE_TRAIN_ENABLED` through the forge API: the merge-train app has no
  Variables permission (`variables: read`), and an unset variable would stop
  release pull requests across the other 13 repositories the train serves
  (k8s run 39272649933 release matrix: 14 repositories).
- **Case-insensitive matching.** Forge names are case-insensitive, so entries
  are stored lowercased and matched against each target repository's lowercased
  full name.
- **Reporting.** A listed repository with both release files is omitted from the
  `releases` output, and the sweep summary names it in a single line
  (`Release train off: ...`).

## Consequences

- The caller explicitly lists each opted-out repository in `release_off`.
- Unlisted repositories continue to receive release runs without changes.
- No new token rights or forge API calls are required.
- Invalid entries that do not match `owner/name` refuse the run before any forge
  call is made.
