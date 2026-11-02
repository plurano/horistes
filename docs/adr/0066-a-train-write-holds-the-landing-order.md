<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0066: A train write holds the landing order

- **Status**: Accepted
- **Date**: 2026-11-01
- **Authors**: Lusoris

## Context

The merge train commits regenerated files onto Renovate dependency branches ([ADR-0032](0032-the-merge-train-commits-generated-files.md)). In 7 days Renovate force-pushed 129 of those commits away across 20 pull requests along two paths:

1. `is_candidate()` lacked a merge-state check, causing the train to regenerate a head that was already `BEHIND`; Renovate rebased it immediately afterward and the train's write was lost.
2. After a train write, subsequent sweeps landed other clean pull requests. The default branch moved forward, the written head fell behind, and Renovate rebased it. On repositories requiring linear history, the train never updates a behind head ([ADR-0044](0044-the-merge-train-updates-only-what-it-armed.md)), making Renovate's rebase the only updater and losing the write.

Coordination must remain stateless and forge-driven without extra app permissions or local storage.

## Measurement

The numbers were measured across the fleet organisations over a one-week sample window:

- The 129 lost commits / 20 pull requests: for every pull request updated 2026-10-25T21:26Z..2026-11-01T21:26Z in the fleet organisations (search by org), its full timeline read from the pull request; a commit authored by `342223998+plurano-merge-train[bot]` counted lost when a later `HeadRefForcePushedEvent` by `renovate[bot]` dropped it from the head.
- Check durations: completed check suites on dependency heads in the same week, per repository median/p90/max (slowest dependency head check run measured that week: 27.9 minutes, p90 26.9 minutes, golusoris).

## Decision

- Skip behind heads: `is_candidate()` returns `False` when `pull["mergeStateStatus"] == "BEHIND"`; the row keeps its existing behind note.
- The forge query reads `messageHeadline`, `author { email user { login } }`, and `signature { wasSignedByGitHub }` alongside `committedDate` on the head commit of each open pull request.
- Forge-verified train identity: `write_hold()` requires headline `chore(deps): regenerate generated files` (`REGENERATE_HEADLINE`), author email matching `f"{conf['bot_id']}+{conf['bot_login']}@users.noreply.github.com"`, author user login `author.user.login == conf["bot_login"]`, and `signature.wasSignedByGitHub is True` (commits from `createCommitOnBranch` are GitHub-signed). A missing signature or user = no hold.
- Skip rules:
  1. Hold only a pull request that can still land: `write_hold()` iterates `(pull, reason)` and considers a row only when `reason == "land"` or `reason.startswith("merge state ")`. This drops behind, red, draft, labelled, too-young, from-another-repository, superseded, and release rows.
  2. Future dates: ignore a commit whose `committedDate` is more than 5 minutes after `now` (`CLOCK_SKEW_MINUTES = 5`).
  3. Armed rows cannot merge past the hold: `regenerate()` does not write while another non-security pull request on the default branch has `autoMergeRequest` set and a reason other than `"behind"` (the forge's auto-merge could merge it during the hold). The candidate is annotated with note `regenerate waits: #N is armed`.
  4. Newly behind between read and write: immediately before `commit_generated_files`, re-read that pull request's `mergeStateStatus` via a GraphQL query by number and skip the write when it is `BEHIND` (note `behind since the read: not regenerated`), `UNKNOWN` or missing (note `merge state UNKNOWN: not regenerated` or `merge state missing: not regenerated`); the forge reports `UNKNOWN` while it recalculates after the base moves.
- A train write holds the landing order for 60 minutes (`WRITE_HOLD_MINUTES = 60`): `write_hold()` identifies valid written heads whose `committedDate` is newer than `now - 60 minutes`, and releases a write only when it is red and not `UNSTABLE` (`is_red()` counts non-required checks; an `UNSTABLE` head can still land, so it stays held).
- `yield_to_write()` keeps only the held pull request and every security update (`is_security`) in `served`; all other rows are dropped. Notes change only for rows whose reason is `"land"` or `"behind"` (`f"held for the generated files of #{n} until {stamp}"`).
- The hold is stateless and ends automatically when:
  1. The written head lands (it is no longer an open pull request).
  2. The written head turns red and is not UNSTABLE.
  3. The written head is rebased by Renovate (the latest commit author is no longer the train bot).
  4. 60 minutes elapse from the commit date.
- Security updates bypass the write hold so critical patches never wait behind generated file runs.
- Concurrency: overlapping runs are serialised by the caller's concurrency group.

## Consequences

A train write is protected from base movements while its checks run. A slow or red-pending CI run holds other landings for up to 60 minutes before timing out and unblocking the train. The value is twice the slowest measured check run on a dependency head in the week before this record (27.9 minutes, p90 26.9 minutes, golusoris). A direct push to the default branch by a person still moves the base.

Tests in `tests/test_merge_train.py` verify that behind heads are skipped, a fresh train write holds other landings with the note, the written head itself lands under the hold, security updates bypass the hold, the hold releases on timeout, red status, rebase by Renovate, unauthorized author or signature commits, skew bounds, or newly behind reads, armed rows cannot merge past the hold, and the GraphQL query fetches the required commit fields.
