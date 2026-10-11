<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0008: The licence tool's image is named by digest

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0005](0005-host-exception-with-an-end.md) kept the licence check on
`fsfe/reuse-action`, pinned by commit. That pin fixes the action's three files
and nothing else: its Dockerfile starts from `fsfe/reuse:6`, a tag that moves
with every release of the tool. On 2026-10-11 the tags `6`, `6.2` and `6.2.0`
all named the same image, so a later release changes what every caller runs
without a change in this repository.

The action adds two things to that image: a script that tells git to trust the
checkout and then calls the tool with the given arguments, and a working
directory. The runner sets the working directory of any container step itself.
The git setting matters: the tool runs as root on a checkout that belongs to
the runner's user, git refuses such a checkout, and the tool then reads every
file in the folder instead of the files git tracks.

ADR-0005 named installing the tool with recorded hashes as the fallback for a
moving tag. A digest keeps the same image from the same registry and changes
less, so it is taken first.

## Decision

- `.github/workflows/licence.yml` runs the tool's image directly, named by its
  digest on `docker.io`, with the release named in the comment above it. The action
  leaves the list of allowed actions in `hosts.toml`.
- The step passes git the setting `safe.directory = /github/workspace` through
  the `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and `GIT_CONFIG_VALUE_0`
  variables, in place of the action's script. The arguments stay as they were.
- This replaces two sentences of ADR-0005: the action no longer stays pinned by
  commit, and the fallback of installing with hashes is not used. The host
  exception of ADR-0005 stays as it is: one host, one file, until 2026-12-31.
- The change of form was run beside the old form before it landed, as a
  consumer switch is ([ADR-0004](0004-same-verdict-before-a-switch.md)): the
  repository root and both planted fixtures
  ([ADR-0006](0006-planted-case-as-a-fixture.md)), plus a probe with a file git
  ignores, which the tool must pass over with the setting and refuses without.

## Consequences

- **Positive**: a release of the tool no longer changes the check by itself; a
  new image arrives as a reviewed change of one line. zizmor refuses an image
  named by tag (`unpinned-images`), so the pin cannot be dropped unnoticed.
- **Negative**: nothing updates the digest yet. Until the repository has an
  update tool that reads `docker://` references, a new release of the licence
  tool is taken by hand, and the pre-commit hook of a caller may run a newer
  release than the check does.
- **Neutral**: the image is still pulled from the public registry without a
  login; the day that ends is the one ADR-0005 set.

## References

- Fleet answer ledger, id shared-ci-1212-entry-place, 2026-10-11 (paraphrased):
  the digest pin of the licence tool's image is a change of its own, with its
  own proof on the fixtures, and is not part of the switch of this repository.
- `tests/test_lint.py` (`tag-pinned-image`), `tests/test_fixtures.py` (the
  image reference and the git setting of the step).
