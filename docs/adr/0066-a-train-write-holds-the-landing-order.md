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

## Decision

- Skip behind heads: `is_candidate()` returns `False` when `pull["mergeStateStatus"] == "BEHIND"`; the row keeps its existing behind note.
- The forge query reads `messageHeadline` and `author { email }` alongside `committedDate` on the head commit of each open pull request.
- A train write holds the landing order for 60 minutes (`WRITE_HOLD_MINUTES = 60`): `write_hold()` identifies open pull requests whose latest commit has headline `chore(deps): regenerate generated files` (`REGENERATE_HEADLINE`) authored by the bot email (`f"{conf['bot_id']}+{conf['bot_login']}@users.noreply.github.com"`), whose `committedDate` is newer than `now - 60 minutes`, and which are not red (`is_red(pull)` is False).
- `yield_to_write()` keeps the held pull request and every security update (`is_security`); all other rows with reason `"land"` or `"behind"` are dropped with note `f"held for the generated files of #{n} until {stamp}"`.
- The hold is stateless and ends automatically when:
  1. The written head lands (it is no longer an open pull request).
  2. The written head turns red.
  3. The written head is rebased by Renovate (the latest commit author is no longer the train bot).
  4. 60 minutes elapse from the commit date.
- Security updates bypass the write hold so critical patches never wait behind generated file runs.

## Consequences

A train write is protected from base movements while its checks run. A slow or red-pending CI run holds other landings for up to 60 minutes before timing out and unblocking the train. The value is twice the slowest measured check run on a dependency head in the week before this record (27.9 minutes, p90 26.9 minutes, golusoris). A direct push to the default branch by a person still moves the base.

Tests in `tests/test_merge_train.py` verify that behind heads are skipped, a fresh train write holds other landings with the note, the written head itself lands under the hold, security updates bypass the hold, the hold releases on timeout, red status, rebase by Renovate, or unauthorized author commits, and the GraphQL query fetches the required commit fields.
