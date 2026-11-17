<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0095: Pull requests check the full sign-off ident

- **Status**: Accepted
- **Date**: 2026-11-17
- **Authors**: Lusoris

## Context

The commit-msg hook checks a message when it is written, and the repository gate
checks commit ranges by author email
([ADR-0074](0074-the-commit-message-judge-runs-on-the-host-python.md)). A consumer
without the hook has no forge-side sign-off check. The shared workflow supplies
that check before a pull request lands.

The gate deliberately compares email only: the forge writes the forge profile
name as the author of a squash commit, while its sign-off carries the git
identity name. Comparing both on the resulting main history would refuse those
commits. The existing hook and gate behavior must therefore stay email-only.

## Decision

[sign-off.yml](../../.github/workflows/sign-off.yml) provides the reusable
`Sign-off` job. On a pull request it judges every commit in the event's
`base.sha..head.sha` range, using full history from the caller's merge ref. On
other events it reports a notice that sign-offs are judged on the pull request.
The repository's [caller](../../.github/workflows/sign-off-check.yml) runs on pull
requests and merge groups; its `result` job reports the required `Sign-off` check,
including a failure when a draft skips the call.

[commit_message.py](../../tools/commit_message.py) adds a `range` command that
requires at least one sign-off whose name and email match the commit author's
name and email, after stripping surrounding whitespace and applying `casefold`.
Malformed sign-offs and oversized messages fail. The existing bot exception
remains: a forge bot author can carry a person's sign-off. A clean two-parent
merge without a sign-off remains exempt. The command applies no tool-credit
rules; the hook and gate retain those rules and their email-only comparison.
Both range judges share one bounded revision reader, limited to 500 commits.

The identity rule credits the [DCO GitHub App's `lib/dco.js`](https://github.com/dcoapp/app/blob/master/lib/dco.js),
licensed under ISC: its model compares name and email after lowercasing, accepts
author or committer, and skips merges and bot authors. This implementation
requires the **author** specifically, uses Unicode case folding, and preserves
the narrower existing bot and clean-merge exemptions. No upstream code is copied.

Before judging the caller, the workflow runs `self-test` in an isolated temporary
repository. The fixed cases prove acceptance and refusal through real git commit
objects. [test_commit_message_tool.py](../../tests/test_commit_message_tool.py)
covers identity matching, unchanged trailer syntax, explicit heads, the range
bound, clean merges and CLI exit codes. The Python 3.12 floor stays unchanged.

## Consequences

- Consumers gain a forge-side check without installing the commit-msg hook.
- Contributor commits need the author's full ident; matching the committer alone
  does not pass. At least one matching sign-off suffices, but malformed additional
  sign-offs still fail.
- Main squash history remains compatible with the hook and gate. Merge-group
  runs report success after the self-test rather than rejudging squash identities.
- The reusable workflow reads repository contents only, checks out its own judge
  at its workflow revision, and passes event revisions through environment fields.

## Revisit trigger

Reconsider keeping the hook and gate email-only when the forge squash author name
equals the sign-off name, or the squash writer signs off with the forge name.
