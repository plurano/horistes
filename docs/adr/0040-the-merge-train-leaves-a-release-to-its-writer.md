<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0040: The merge train leaves a release to its writer

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

The train's release job ran release-please once, for the release pull
request and the release of a merged one (decision record 0031). Two
repositories showed where that goes wrong:

- **A repository that writes its own release.** jellysin/plugin-lastfm has
  its release written by its own prepare-release workflow, and the train
  created a second draft for v0.2.0 next to it. Setting
  `skip-github-release` in `release-please-config.json` does not help:
  release-please-action v5.0.0 reads that switch only from its own input
  (`src/index.ts`), never from the config file. With releases skipped,
  nothing moves the label `autorelease: pending` to `autorelease: tagged`,
  and release-please opens no new release pull request while a merged one
  is still pending.
- **A release that already exists.** In jellysin/release-helper-sandbox the
  repository's own publish workflow made the immutable release v0.1.25
  before the train ran (lusoris/k8s run 39075206588). release-please found
  the duplicate tag, moved the labels itself, then failed with
  `already_exists`, and the job went red although the release was done.

## Decision

- **Releases and release pull requests in two steps.** One release-please
  step creates releases only, the other opens or updates the release pull
  request only.
- **The config file's `skip-github-release` is honoured.** A step reads
  `release-please-config.json` through the API. When every package skips
  the release, the release step does not run; a mix of skipping and
  non-skipping packages is an error, never a guess.
- **The label moves once the tag names the merge commit.** With releases
  skipped, the train moves each merged release pull request from
  `autorelease: pending` to `autorelease: tagged` once its version tag
  points at the pull request's merge commit, as jellysin/release-helper's
  reusable release workflow does. It adds the new label before removing the
  old one, so a failure between the two writes leaves the pull request
  pending. A tag not yet there, or on another commit, leaves the label for a
  later sweep.
- **An existing release on the release commit is done.** The release step
  continues on error, and the next step judges it: the run counts as done
  only when release-please had a merged release pull request to release,
  that pull request no longer carries `autorelease: pending`, its tag names
  its merge commit, and the forge holds a release for the tag. Any other
  failure stays red.

## Consequences

- Repositories with their own release writer set `skip-github-release` in
  their config and get no second release from the train.
- Their next release pull request opens once the tag is on the merge
  commit, at the latest one sweep after the tag is pushed.
- A failed release step is judged by the train's own reads, not by
  release-please's message; a failure while a merged release pull request
  was pending and whose release does not exist on its merge commit stays
  red.
- The job runs release-please twice and reads the config, the 20 newest
  closed pull requests and the tags of pending ones.
