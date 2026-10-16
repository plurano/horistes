<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0034: The merge train queues a pull request where the default branch has a merge queue

- **Status**: Accepted
- **Date**: 2026-10-16
- **Authors**: Lusoris

## Context

A repository ruleset can require a merge queue on the default branch. The
forge then refuses the merge mutation for every pull request, CLEAN or not,
with "Repository rule violations found ... Changes must be made through the
merge queue". One fleet repository has such a ruleset, so every sweep that
found a CLEAN pull request there ended with a refusal and failed the run.

## Decision

- **Queue, do not merge.** The repository read asks for the default branch's
  `mergeQueue`. Where one exists, the pull request the train would land is
  added to the queue with the `enqueuePullRequest` mutation, bound to the head
  commit it read. A missing queue entry in the answer is a refusal. The
  repository section reports `queued #N`.
- **A queued pull request holds the train.** Each pull request reads
  `isInMergeQueue`. One in the queue is left alone ("in the merge queue"),
  and while any is queued the train writes nothing else in that repository,
  as it does for an armed pull request in flight. The queue lands it with its
  own checks.
- **Unchanged:** every other verdict, the BEHIND path (arm, then update by
  merge), and repositories without a queue, which still land through the
  merge mutation.

## Consequences

- **Positive:** a repository with a merge queue no longer fails the sweep; its
  clean pull requests land through the queue the ruleset demands.
- **Negative:** the app's rights stay as they are. Whether `contents: write`
  and `pull_requests: write` are enough for `enqueuePullRequest` shows on the
  first sweep that queues a pull request; a refusal there is reported as any
  other.
- **Neutral:** skipping such repositories was the other option. It would have
  left their clean pull requests to a person.

## References

- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- GitHub GraphQL API documentation: `enqueuePullRequest`, `Repository.mergeQueue`,
  `PullRequest.isInMergeQueue`.
