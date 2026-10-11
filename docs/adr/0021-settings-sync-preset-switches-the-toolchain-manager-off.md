<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0021: A second preset switches the toolchain manager off in a repository under settings sync

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

Some repositories of the family hold a file equal to a source in another
repository and compare it in their gate; the family calls this settings sync.
The first such file is `rust-toolchain.toml`: the repository keeps a copy and
its gate fails when the checksum differs from the source.

Renovate (the update bot, [ADR-0019](0019-update-proposals-from-renovate-configured-here.md))
reads that file with its manager `rust-toolchain` and proposes the next
toolchain. In a repository under settings sync that pull request can never
turn green: it changes the copy, and the gate compares the copy with the
source.

The family preset `default.json`
([ADR-0020](0020-shared-update-bot-values-as-a-preset.md)) reaches every
repository that extends it. A repository that owns its toolchain file wants
the proposal, so the switch does not belong there.

| Fact | Source |
| :--- | :--- |
| `github>owner/repository:name` reads `name.json` of that repository | preset documentation |
| the manager `rust-toolchain` reads `rust-toolchain` and `rust-toolchain.toml` in any folder | its `index.js` in the image |
| a block named after a manager with `enabled` off stops that manager | run of the bot, below |
| the validator refuses a misspelt manager as a key, and passes one inside `matchManagers` | validator runs, below |

## Decision

- `settings-sync.json` in the root of this repository is a second preset. A
  repository under settings sync names it beside the family preset:
  `github>plurano/shared-ci:settings-sync`.
- It holds one block per manager whose file is held by settings sync, and the
  block holds `enabled` off and nothing else. Today that is `rust-toolchain`.
- The switch is the manager's own block, not a package rule with
  `matchManagers`: a misspelt manager is then an unknown key, which the
  validator and the tests refuse. Inside `matchManagers` the same slip passes
  the validator and the rule matches nothing.
- The preset extends nothing. The family preset and this one are named side by
  side, so a repository can take either without the other.
- This repository does not extend it: it holds no toolchain file, and it names
  its managers.
- A further file kind under settings sync that a manager of the bot reads gets
  its manager added to the allow-list in `tests/test_update_bot.py` first, then
  to the preset.

## Consequences

- **Positive**:
  - The bot opens no pull request that the settings-sync check must refuse.
  - The family preset stays free of manager switches; a planted switch there
    fails the tests.
  - A copy of the block in a repository that cannot extend the preset yet is
    one key, and it is the same text as the preset holds.
- **Negative**:
  - Nothing here checks that a repository which extends the preset really
    holds its toolchain file by settings sync. A repository that extends it
    and owns the file gets no toolchain proposal, and no message says so.
  - While this repository is private, a public repository cannot extend the
    preset and carries the block as a dated copy, as for the family preset.
  - The switch and the source of the synced file live in different
    repositories. Adding a synced file there does not add its manager here.
- **Neutral**: the fleet ledger calls this a bridge. The target is the source
  of the synced settings beside the preset, so that both change in one place.
  A repository that names its managers in `enabledManagers` and leaves
  `rust-toolchain` out needs neither.

## References

- Fleet answer ledger, 2026-10-12 (paraphrased). amussis-settings-source: as a
  bridge, the family's preset switches the `rust-toolchain` manager off for
  repositories under settings sync, so that the bot's toolchain pull request
  does not turn the checksum gate red; the settings source beside the preset
  is the target.
- Renovate image `renovate/renovate:44.143.0`, read 2026-10-12:
  `dist/modules/manager/rust-toolchain/index.js` holds the file pattern
  `/(^|/)rust-toolchain(\.toml)?$/`.
- Run 2026-10-12 with that image, without network, `renovate --platform=local
  --dry-run=extract` in a folder with one `rust-toolchain.toml`: without the
  block the bot reports one file and one dependency for `rust-toolchain`; with
  the block it logs "rust-toolchain is disabled" and reports none; with
  `enabledManagers` naming another manager it reports none.
- Checked 2026-10-12 with the same image, without network:
  `renovate-config-validator --strict --no-global` passes on
  `settings-sync.json` and fails on
  `tests/fixtures/update-bot/planted-settings-sync.json` (unknown option
  `rust-toolchainn`). A package rule with `matchManagers` naming
  `rust-toolchainn` passes the validator.
- `tests/test_update_bot.py`: ten planted cases in the suite; six changes
  planted in the files (switch on, manager misspelt, `extends` added, the
  switch in the family preset, the check emptied, the block not compared) each
  fail at least one test.
