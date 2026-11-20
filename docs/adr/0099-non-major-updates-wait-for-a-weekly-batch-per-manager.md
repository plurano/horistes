<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0099: Non-major updates wait for a weekly batch per manager

- **Status**: Accepted
- **Date**: 2026-11-20
- **Authors**: Lusoris

## Context

The shared [preset](../../default.json) supplies dependency update defaults to
46 repositories (consumer inventory, 2026-11-20). Proposing each update in its
own pull request repeats review and CI work across those repositories. A weekly
batch per package manager reduces that load while keeping different toolchains
in separate proposals.

The three-day release age protects consumers of outside releases. Own releases
skip that age, while forks of outside projects keep it
([ADR-0054](0054-own-releases-skip-the-waiting-time.md)). Vulnerability fixes
already bypass the age and carry the security label
([ADR-0050](0050-a-vulnerability-fix-skips-the-waiting-time.md),
[ADR-0060](0060-the-merge-train-serves-security-updates-first.md)); batching
must preserve their immediate, separate proposals.

## Decision

Insert this package rule immediately after the rule that disables hosted-runner
updates in `default.json`, before the own-release rules:

```json
{
  "description": "Non-major updates wait for one weekly batch per manager; a major update stays its own proposal.",
  "matchUpdateTypes": ["minor", "patch", "digest", "pin", "pinDigest"],
  "groupName": "{{manager}} non-major",
  "schedule": ["* 0-3 * * 1"]
}
```

The template uses Renovate's allowed `manager` field, so each manager has one
group. The cron window is Monday 00:00-03:59 UTC. Majors do not match this rule
and stay separate; `groupSingleUpdates` remains unset.

Each of the four own-release rules follows the batch rule and sets
`groupName: null` and `schedule: []` as well as its existing
`minimumReleaseAge: null`. Later matching rules override earlier values, so own
releases stay separate and eligible at any time. The last fork rule changes
only the release age back to three days: forks retain the own-release rules'
separate, unscheduled proposals. The container digest rule is unchanged.

The top-level `minimumReleaseAge: "3 days"` and the complete
`vulnerabilityAlerts` block remain unchanged. Renovate's vulnerability defaults
clear grouping, scheduling and release age and create the proposal immediately;
the preset continues to inherit those defaults. These behaviours follow the
configuration options and template fields measured on 2026-11-20 in Renovate's
`docs/usage/configuration-options.md`, `lib/config/options/index.ts`, and
`lib/util/template/index.ts` (template source SHA-256 prefix `ba190c732b57`).

[The update-bot tests](../../tests/test_update_bot.py) check the batch rule with
`batch_problems`, the fixed manager probe table, and planted defects. Existing
tests continue to pin the vulnerability block and release-age exceptions.

## Consequences

- An outside non-major update subject to the three-day release age can wait up to a week
  plus three days before its proposal. Existing release-age exceptions still
  apply, including container image digests
  ([ADR-0078](0078-docker-digest-updates-wait-for-no-release-age.md)).
- A Renovate run must occur during Monday 00:00-03:59 UTC to create the weekly
  batch. A missed window defers creation to the next eligible run.
- A repository can override the batch with a later rule in its own
  `renovate.json`. Repository rules follow preset rules, as recorded by the
  `APPENDED` comment in [tests/test_update_bot.py](../../tests/test_update_bot.py)
  (base commit `3bca8d2`, lines 112-113).
- Extenders with their own per-manager groups keep those groups because their
  rules follow the preset's rules. A repository that also needs a different
  schedule overrides `schedule` in its own matching rule.
- Major updates, vulnerability fixes, and own releases remain separate
  proposals. Own releases and vulnerability fixes have no weekly waiting
  window; forks still wait for the three-day release age.
