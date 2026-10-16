<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0037: The merge train writes the squash message

- **Status**: Accepted
- **Date**: 2026-10-16
- **Authors**: Lusoris

## Context

When the merge train updates a BEHIND pull request from the default branch, the
update-branch call adds a merge commit authored by `plurano-merge-train[bot]`.
When the pull request later lands by squash merge (either directly or after
auto-merge was armed), the forge builds the default squash commit message from
every commit on the branch and appends a `Co-authored-by:` trailer for every
additional commit author, including `plurano-merge-train[bot]`.

In `lusoris/k8s`, squash commit `1099b981e` landed through auto-merge after an
update-branch merge commit from the app and carried a `Co-authored-by:` trailer
for `plurano-merge-train[bot]`. Fleet commit-message gates refuse
`Co-authored-by:` trailers on the default branch, so every squash the train
leaves to the forge's default message after an update fails those gates.

## Decision

- **Write the squash message in the train.** Both `MergePullRequestInput` and
  `EnablePullRequestAutoMergeInput` accept `commitHeadline` and `commitBody`.
  When `merge_method` is `SQUASH`, the train reads the pull request's commits
  before calling `mergePullRequest` (on the direct landing path) or
  `enablePullRequestAutoMerge` (on the BEHIND path) and passes `commitHeadline`
  and `commitBody` explicitly.
- **Non-merge commits only, trailers cleaned.** `squash_message(title, number,
  messages)` takes the full commit messages of the pull request's non-merge
  commits (`parents.totalCount == 1`) in oldest-first order:
  - The headline is `<title> (#<number>)`.
  - With one non-merge commit, the body is the text after the first line with
    leading and trailing blank lines stripped. With several non-merge commits,
    each commit is formatted as `* <subject>`, followed after a blank line by
    its body when present, separated by blank lines.
  - Every trailer line matching `^(Co-authored-by|Signed-off-by):`
    (case-insensitive) is removed from the commit bodies.
  - Every distinct `Signed-off-by:` line found across the non-merge commits is
    appended at the end after a blank line, in first-seen order.
  - A pull request with no `Signed-off-by:` line on its non-merge commits, with
    more than 100 commits (`commits.totalCount > 100`, where not every sign-off
    can be read in one page), or whose assembled body still holds a line that
    starts with `Co-authored-by:` is refused with `RefusedError` before any
    write. The last check is a second barrier behind the trailer removal; prose
    that names the trailer inside a line is kept.
- **Unchanged paths.** When `merge_method` is `MERGE`, no commits query is made
  and no headline or body is passed, because merge commits carry no co-author
  trailer. The merge-queue path (`enqueuePullRequest`) takes no commit headline
  or body in the GraphQL schema and remains unchanged; in a repository with a
  merge queue, the queue's squash message follows the repository's own merge
  message settings.

- **The pure rules move to a module.** The train script was close to the
  engine's cap of 4096 fields per run script (decision record 0034), and the
  new code took it over the cap. A step before the train writes
  `train_rules.py` into `$RUNNER_TEMP` through a quoted here-document, and the
  train script imports it. The module holds the rules that read no forge:
  `update_made`, `is_red`, `too_young`, `verdict`, `in_flight`,
  `clean_commit` and `squash_message`. They raise `ValueError` where the train
  turns the error into a refusal. The tests write the module from the step's
  text, lint it, and run mutants of it as they run mutants of the script.

## Consequences

- **Positive:** squash merges landed or armed by the merge train carry the
  sign-off lines of the pull request's own commits (a person's or the update
  bot's) and never a `Co-authored-by:` trailer; unsigned pull requests are
  refused before merging or arming.
- **Negative:** landing or arming with `SQUASH` makes one additional GraphQL
  read for the pull request's commits; pull requests with more than 100 commits
  are refused by the train and left for a person. A refusal ends that
  repository's part of the sweep red each run until the pull request is
  signed off or labelled with a hold label. Auto-merge keeps the message
  written when it was armed; a commit pushed after arming lands without its
  own sign-off in the message until the train arms it again.
- **Neutral:** the train script now stays about 200 fields under the cap.
  `enqueuePullRequest` has no message fields, so repositories
  using a merge queue rely on repository settings for squash commit messages.

## References

- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- Decision record 0034 (`docs/adr/0034-the-merge-train-queues-where-the-branch-has-a-merge-queue.md`).
- GitHub GraphQL API documentation: `MergePullRequestInput`,
  `EnablePullRequestAutoMergeInput` (`commitHeadline`, `commitBody`),
  `EnqueuePullRequestInput`, `PullRequest.commits`.
