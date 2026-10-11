<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0019: Updates are proposed by Renovate from a configuration in this repository

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

Renovate (the update bot of Mend; the name is the English verb) is installed
on the organisation since 2026-10-11 and opened its onboarding pull request
here. Until then nothing moved a pin:
[ADR-0002](0002-versioning-and-pinning.md) pins every outside action to a
commit and every tool to a hash,
[ADR-0008](0008-licence-tool-image-by-digest.md) names the licence tool's
image by digest and records that nothing updates it, and
[ADR-0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md)
leaves newer releases to Renovate.

The configuration the onboarding pull request proposes (`config:recommended`
alone) does not fit this repository in five places:

| Place | What happens with the proposed configuration |
| :--- | :--- |
| `requirements/tools.txt` | not read: the `pip-compile` manager has no file pattern by default, and it takes the compile command from the header of the file, which the `Makefile` switched off |
| the image of `uses: docker://…@sha256:…` | found, but without a release: a digest alone names no version to compare |
| `.devcontainer/` | proposed for update, although the Praetor engine writes those files and compares them byte for byte |
| waiting time | none; ADR-0002 asks for three days |
| commits of the bot | no `Signed-off-by` line, which the gate asks of every commit of a pull request |

The family plans one shared preset for all repositories. It does not exist
yet.

## Decision

- `renovate.json` in the root of this repository is the configuration, until
  the shared preset replaces it.
- Three managers run and no other (`enabledManagers`): `github-actions`,
  `pip-compile` and one custom regular expression. A manager that is not
  named reads nothing, so the files the engine writes stay untouched.
- Every proposal waits three days after the release (`minimumReleaseAge`).
  Nothing merges by itself (`automerge` is off); a proposal is a pull request
  that passes the gate and is merged by a person.
- Commits of the bot carry the sign-off line (preset `:gitSignOff`).
- `make requirements` writes the header again, with options Renovate accepts:
  each value joined with `=`, the output named with `--output-file=`, the
  waiting time as a duration (`--exclude-newer=P3D`) instead of a date
  computed by the shell. Renovate reruns that command with the one package it
  raises, so the hashes and the waiting time of a recompile hold for its
  proposals too. The annotations (`# via …`) come back with the header.
- The image line of the licence workflow gets its release as a comment on the
  line above (`# release 6.2.0`), and the custom manager reads the release
  and the digest together. The release cannot stand behind the digest on the
  same line: the line would pass the 120 characters the YAML lint allows.
- The `github-actions` manager proposes neither that image (it belongs to the
  custom manager) nor another release of a hosted machine:
  [ADR-0018](0018-floating-runner-labels-refused-by-the-pin-tool.md) leaves
  the machine of a job to the repository.

This adds to ADR-0002 and ADR-0016 and ends the negative consequence ADR-0008
records for the image digest.

## Consequences

- **Positive**:
  - A newer release of an action, a tool or the licence image arrives as a
    pull request with the release notes, three days after it appeared.
  - `tests/test_update_bot.py` fails when a file stops matching what the bot
    reads: an option in the header it refuses, a compile command in the
    `Makefile` that differs from the header, an image line without its
    release comment, or an image in another workflow that no manager reads.
  - The header shows how the file was made; the annotations show which tool
    pulls in which package.
- **Negative**:
  - actionlint is installed at one version with a checksum in the `Makefile`
    and the gate workflow. No manager reads those two places; that update
    stays manual.
  - The allow-list of header options in the test is a copy of the bot's list
    at release 44.143.0. A later release of the bot may accept more or
    fewer; the test then has to follow.
  - The waiting time of an image comes from the time its tag was last
    pushed. Upstream notes that a digest can therefore look newer than it
    is, which only lengthens the wait.
  - The `pip-compile` manager also proposes a periodic refresh of all tool
    pins (`lockFileMaintenance`, on by default for this manager). Renovate
    does not apply its own waiting time to a refresh; there the three days
    hold only through `--exclude-newer=P3D` in the header, which is why the
    test demands that option.
  - Only the workflows under `.github/workflows` are read. A caller template
    under `templates/` names this repository, not an outside action, so
    nothing is missed today.
- **Neutral**: the dependency dashboard issue of `config:recommended` appears
  in this repository. The onboarding pull request of the bot is left to the
  bot.

## References

- Fleet answer ledger, 2026-10-11 (paraphrased). shared-ci-own-update-bot: an
  interim configuration of its own with action digests, the compiled tool
  pins, a regular expression for the image digest, three days of waiting and
  no automatic merge. plurano-update-bot-installed: the bot is installed on
  the organisation for all repositories; each repository handles its own
  onboarding pull request and does not close it.
- Renovate 44.143.0, read 2026-10-11: `lib/modules/manager/pip-compile/common.ts`
  (header expression, accepted options, the refusal of `--no-header` and of a
  value without `=`), `lib/modules/manager/github-actions/extract.ts`
  (dependency types `docker` and `github-runner`),
  `lib/modules/datasource/docker/index.ts` (release time of an image),
  `lib/modules/manager/pip-compile/index.ts` (the refresh is on by default),
  `docs/usage/key-concepts/minimum-release-age.md`.
- Checked 2026-10-11 with the image `renovate/renovate:44.143.0`, without
  network: `renovate-config-validator --strict` passes on `renovate.json` and
  fails on a copy with a misspelt option and a custom manager without a
  datasource; `renovate --platform=local --dry-run=extract` on an export of
  the tracked tree reads 4 workflow files (15 dependencies), the 21 pins of
  `requirements/tools.txt` and the one image with release `6.2.0`, and no
  file under `.devcontainer/`. The log is not part of the tree.
- uv 0.12.18: `--exclude-newer=P3D` is accepted and written into the header
  as given; `--quiet` is left out of the header. The recompile changed no pin
  and no hash (21 pins compared).
- `tests/test_update_bot.py`: run against the tree before this change, seven
  of its tests fail (header, `Makefile` command, image line).
