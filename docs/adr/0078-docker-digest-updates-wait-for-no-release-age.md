<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0078: Docker digest updates wait for no release age

- **Status**: Accepted
- **Date**: 2026-11-12
- **Authors**: Lusoris

## Context

The shared Renovate preset `default.json` sets a top-level
`minimumReleaseAge` of three days.

The preceding decisions addressed different parts of update freshness:

- [ADR-0019](0019-update-proposals-from-renovate-configured-here.md) configured
  Renovate to propose pinned updates, including image releases and digests, with
  a three-day waiting time; it recorded that image age comes from the tag's last
  push time.
- [ADR-0020](0020-shared-update-bot-values-as-a-preset.md) moved shared values
  into `default.json` so consumers do not maintain drifting copies.
- [ADR-0035](0035-a-weekly-job-bounds-the-licence-tool-image-age.md) added a
  weekly check that refuses an outdated licence image, a moved tag or an
  unreadable registry.
- [ADR-0046](0046-the-merge-train-counts-the-minimum-age-from-the-pull-request.md)
  stopped rebases from restarting the merge train's age by counting from pull
  request creation; it left Renovate's release-age check separate.
- [ADR-0050](0050-a-vulnerability-fix-skips-the-waiting-time.md) removed the
  release-age wait for known vulnerability fixes and enabled OSV alerts.
- [ADR-0054](0054-own-releases-skip-the-waiting-time.md) removed the wait for
  own releases while keeping outside forks at three days through a final rule.

Renovate measures the age of a Docker Hub digest update from the tag's
`tag_last_pushed` time. A re-pushed tag restarts the delay. ADR-0019 expected
this only to lengthen the wait; a tag that is re-pushed more often than every
three days holds its digest update for good. `renovate/stability-days` then
never succeeds; with `internalChecksFilter: strict`, Renovate leaves the update
branch behind its base and the merge train waits.

On 2026-11-12 ten Docker digest pull requests were open across the family's
repositories, the oldest from 2026-10-24. The one checked, VMAFx/pelorus pull
request 478 (`ubuntu:26.04` digest), had been open since 2026-11-01 with
`renovate/stability-days` pending; the tag was re-pushed on 2026-11-12.

## Decision

Propose one rule after the own npm rule and before the final forks rule:

```json
{
  "description": "Digest updates of container images wait for no release age, since a re-pushed tag restarts the age.",
  "matchDatasources": ["docker"],
  "matchUpdateTypes": ["digest"],
  "minimumReleaseAge": null,
  "prBodyNotes": [
    "This container image digest update waits for no release age because a re-pushed tag restarts the age."
  ]
}
```

Only Docker datasource updates of type `digest` receive this exception. The
three-day default still covers major, minor and patch updates of outside images.
Later matching package rules override earlier values, so the final forks rule
keeps fork images at three days even for digest updates. Own images retain their
existing exception. The top-level description and README name the exception.
The decision follows fleet answer pelorus-dep-digest-release-age (see
References).

### Alternatives

- A rule only in the affected consumer: copies shared policy into one repository
  and leaves the other nine open digest pull requests on the three-day age.
- Teach the merge-stragglers process to recognise this held class: classifies the
  symptom while stale base images still never update.
- Add a hold label: hides the finding without removing the recurring delay.

## Consequences

- **Positive**: re-pushed image digests can land without the three-day release
  cooling period. `prBodyNotes` explains the exception on the pull request.
- **Negative**: outside image digest updates lose that release-age cooling
  period; a digest can contain newly pushed upstream changes.
- **Neutral**: CI still gates every pull request; bot automerge remains off.
  The merge train's own age policy and the security-labelled path stay unchanged.
  Forks keep three days, and outside image version updates keep three days.
  `tests/test_update_bot.py` checks digest scope, rule order, PR notes and planted
  defects without contacting Renovate or a registry.

## References

- Fleet answer ledger, id pelorus-dep-digest-release-age, 2026-11-12
  (paraphrased): the digest exception goes into the shared preset, and the
  held pelorus pull request stays open without a hold label.
- [Renovate minimum release age](https://docs.renovatebot.com/key-concepts/minimum-release-age/),
  read 2026-11-12: Docker Hub digest updates age from the tag's last push.
- [Renovate configuration schema](https://docs.renovatebot.com/renovate-schema.json),
  read 2026-11-12 (SHA-256 prefix `ef5acb9b3b48`): `matchUpdateTypes` accepts
  `digest`; `prBodyNotes` is an array of strings; there is no `prBodyNote`.
- [Preset](../../default.json), [tests](../../tests/test_update_bot.py) and
  [consumer documentation](../../README.md#updates).
