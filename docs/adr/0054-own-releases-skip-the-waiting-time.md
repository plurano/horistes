<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0054: Own releases skip the waiting time

- **Status**: Accepted
- **Date**: 2026-10-26
- **Authors**: Lusoris

## Context

The preset `default.json` makes every update proposed by the bot wait three days
after release (`minimumReleaseAge`, decision records 0019, 0020, and 0050).
Holding updates for three days shields downstream repositories from broken or
malicious upstream releases.

However, that waiting time holds own releases of the family's organisations as
well: a release of one repository reaches the others only after three days. An own
release that fixes one repository for another waits as long as an outside
release, although the family can read and revert its own code directly.

At the same time, nine repositories in the family organisations are forks of
outside projects:

- `lusoris/llama-swap`
- `lusoris/controller-tools`
- `lusoris/hindsight`
- `lusoris/Vulkan-ValidationLayers`
- `lusoris/helm-charts`
- `lusoris/paperclip`
- `lusoris/model_server`
- `lusoris/paperclip-aperture`
- `VMAFx/netflix-vmaf-contributions`

Because these track external upstream code, they carry third-party supply chain
risks and must continue to observe the three-day waiting time.

## Decision

The shared preset `default.json` of Horistes (Greek *horistes*, the boundary
commissioner who fixes limits; pronounced ho-RIS-tees) appends five package rules after the existing
hosted-machine runner rule:

1. Four rules, one per datasource group, matching packages originating from any
   of the nine fleet organisations (`lusoris`, `cordanaLLM`, `golusoris`,
   `VMAFx`, `tekmora`, `plurano`, `vidiko`, `jellysin`, `goph-arr`):
   - GitHub releases, tags, digests, and attachments (`github-tags`,
     `github-releases`, `github-digest`, `github-release-attachments`) with
     prefix `""`
   - Go modules (`go`) with prefix `github.com/`
   - Container images (`docker`) with prefix `ghcr.io/`
   - npm packages (`npm`) with prefix `@`

   Each rule anchors with `^` and trailing `\/` so names such as `lusoris-x/` or
   `notplurano/` do not match. Each rule sets `"minimumReleaseAge": null`.

2. One fork rule placed last in `packageRules`:
   - Matches all package name forms of the nine outside forks across all
     datasources:
     `/^(github\.com\/|ghcr\.io\/)?(lusoris\/(llama-swap|controller-tools|hindsight|vulkan-validationlayers|helm-charts|paperclip|model_server|paperclip-aperture)|vmafx\/netflix-vmaf-contributions)(\/|$)/i`
   - Sets `"minimumReleaseAge": "3 days"`.

Because Renovate evaluates package rules in order and later matching rules
override earlier keys, placing the fork rule last ensures that forks of outside
projects keep the three-day waiting period even though an earlier own-organisation
rule matched.

## Consequences

- Releases of own family repositories are proposed without waiting for the
  three-day release age.
- Forks of outside projects continue to wait for three days.
- Adding a new organisation or a new fork of an outside project requires an
  update to `default.json` and the corresponding constants in
  `tests/test_update_bot.py`.
- The vulnerability alert bypass (`vulnerabilityAlerts.minimumReleaseAge: null`)
  remains unchanged.
- A consumer repository that sets its own `minimumReleaseAge` in a package rule
  of its `renovate.json` overrides these preset rules for the packages that rule
  matches, since its rules come after the preset's.
- A consumer's top-level `minimumReleaseAge` does not override them: package
  rules apply after the top-level value, so own releases still skip the waiting
  time there.
