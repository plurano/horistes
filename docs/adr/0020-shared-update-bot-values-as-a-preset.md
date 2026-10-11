<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0020: The update bot's shared values are a preset file here, and this repository extends it

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

[ADR-0019](0019-update-proposals-from-renovate-configured-here.md) put the
configuration of Renovate (the update bot of Mend; the name is the English
verb) into `renovate.json` of this repository "until the shared preset
replaces it". The bot is installed on the whole organisation, and every
repository received the same onboarding pull request with `config:recommended`
alone. Each would need the same four corrections that ADR-0019 made here: the
sign-off line, three days of waiting, no merge by the bot, no proposal for
the machine of a job. Written into each repository, they are as many copies
that drift.

Renovate reads shared values from a preset: a JSON file in a repository that
another configuration names in `extends`.

| Fact | Source |
| :--- | :--- |
| `github>owner/repository` without a file name reads `default.json` of that repository | preset documentation |
| `renovate.json` as the name of a preset file is deprecated | preset documentation |
| `#1.2.3` behind the reference names a tag; without it the default branch is read | preset documentation |
| the manager `renovate-config` raises a reference that names a tag, and leaves one without a tag alone | readme of that manager |
| a file passed to the validator is read as the configuration of a self-hosted bot unless `--no-global` is given | validation documentation |

## Decision

- `default.json` in the root of this repository is the preset. It holds only
  what is the same in every repository: `config:recommended`, `:gitSignOff`,
  `minimumReleaseAge` of three days, `automerge` off, and the rule that the
  `github-actions` manager proposes no other release of a hosted machine
  ([ADR-0018](0018-floating-runner-labels-refused-by-the-pin-tool.md)).
- Which managers run and which files they read stays in the repository. The
  preset names no manager list, no file pattern and no package: a key outside
  a short allow-list fails the tests.
- `renovate.json` of this repository extends the preset by the reference a
  consumer uses (`github>plurano/shared-ci`) and repeats none of its values.
  It keeps the three managers, the file pattern of the compiled tool pins,
  the custom manager for the licence image and the rule that leaves that
  image to it. This repository is the first consumer.
- A consumer that lints with REUSE names its `renovate.json` in `REUSE.toml`;
  the README holds the table to copy.
- From the first release on, a consumer names the release in the reference
  and lets the manager `renovate-config` raise it, as
  [ADR-0002](0002-versioning-and-pinning.md) asks of every pin. Until then a
  reference follows the default branch.

This replaces the first point of the decision of ADR-0019 (`renovate.json`
alone is the configuration). Its other points stand.

## Consequences

- **Positive**:
  - One place holds the waiting time and the sign-off rule for every
    repository that extends the preset.
  - `tests/test_update_bot.py` fails when the preset gains a key that belongs
    to one repository, when `renovate.json` repeats a value of the preset, and
    when `renovate.json` stops extending it.
  - A consumer's configuration shrinks to the reference and its own managers.
- **Negative**:
  - While this repository is private, a public repository cannot extend the
    preset. That statement comes from the fleet ledger and is not confirmed by
    a run of the bot.
  - Before the first release every consumer follows the default branch: a
    merged change of the preset reaches all of them on the bot's next run,
    without a proposal in their repository.
  - The interface lock does not record the preset. Removing a value from it
    changes every consumer and nothing here refuses that.
  - The validator is not part of the gate. It ships in an image of about
    2 GB; the check is run by hand before a change of either file, with the
    command in the README.
  - The tests name the allowed keys of the preset. A new shared value needs
    the key added there first.
  - Whether the bot resolves the reference from this repository's own file is
    seen only after the merge, in the bot's dashboard issue.
- **Neutral**: the reference names the repository by its working name. A
  rename changes the reference in every consumer; the forge redirects the old
  name for a time.

## References

- Fleet answer ledger, 2026-10-11 (paraphrased).
  python-library-renovate-preset-source: the configuration of this repository
  becomes the family's preset, published as a preset file and extended by
  reference. amussis-renovate-pr8-path: the REUSE annotation for
  `renovate.json` is a matter of every repository and ships with the preset.
  preset-private-public-fact: a preset in a private repository serves only
  private repositories of the same organisation, and it is a file of its own.
- Renovate at release 44.151.0, read 2026-10-12: `docs/usage/config-presets.md`
  (file names, tag form, deprecated name), `docs/usage/config-validation.md`
  (`--strict`, `--no-global`), `lib/modules/manager/renovate-config/readme.md`
  (which references the manager raises).
- Checked 2026-10-12 with the image `renovate/renovate:44.143.0`, without
  network: `renovate-config-validator --strict --no-global` passes on
  `default.json` and on `renovate.json` and fails on
  `tests/fixtures/update-bot/planted-preset.json` with two errors (an unknown
  option, a text where a truth value belongs). The validator does not fetch
  the preset a file extends.
- `tests/test_update_bot.py`: five changes planted in the two files (a
  manager list in the preset, a repeated value, another `extends`, another
  waiting time, a preset without the sign-off) each fail at least one test.
