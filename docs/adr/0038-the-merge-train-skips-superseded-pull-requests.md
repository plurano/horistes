<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0038: The merge train skips superseded pull requests

- **Status**: Accepted
- **Date**: 2026-10-16
- **Authors**: Lusoris

## Context

A fleet session folded four pull requests into one, cordanaLLM/praetor#1408,
whose body says "Supersedes #1404, #1405, #1406, #1407". The folded pull
requests stayed open for their authors to close. On 2026-10-16 the merge train
found #1407 CLEAN and landed it (commit `a02add5e5`), although #1408 already
carried the same change.

Nothing on #1407 itself said it was replaced: no label, no line in its body.
The only signal was the body of the other open pull request.

## Decision

- **A superseded pull request is left alone.** `verdict` returns
  "superseded: a person closes it" when any of these holds:
  - the pull request carries the label `superseded`;
  - its own body says "Superseded by #N";
  - the body of another open pull request in the same repository says
    "Supersedes #N", or lists several numbers after it ("Supersedes #4, #1
    and #6"), and N is this pull request's number.
- **The body is read in the same query.** The pull request query reads `body`
  next to the title; `superseded(pulls)` collects the replaced numbers once
  per repository and `run` passes them to every verdict.
- **Matching is case-insensitive and needs a number.** "Supersedes the old
  approach; see #1" replaces nothing: the number must follow the word.
- **The train closes nothing.** Closing a superseded pull request stays with its
  author or a person; the train only refuses to land it.

## Consequences

- A fold or replacement no longer races the train: listing the replaced
  numbers in the new pull request's body is enough, before anyone closes them.
- A pull request that only mentions "supersedes #N" in passing holds back #N.
  Removing the line from the body, or closing the mentioning pull request,
  releases it on the next run.
- Only open pull requests in the same repository count; a reference from a
  closed or merged one, or from another repository, does not hold anything.

## References

- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- GitHub GraphQL API documentation: `PullRequest.body`.
