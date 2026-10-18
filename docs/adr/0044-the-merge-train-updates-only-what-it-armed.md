<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0044: The merge train updates only what it armed, and never a linear-history branch

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

The train's `advance` step armed auto-merge on the oldest BEHIND pull request
and merged the default branch into it (decision record 0029). Where the
repository allowed no auto-merge, it skipped the arming but still ran the
update, so it merged the default branch into pull requests it had not armed
and would never land.

On 2026-10-18 the app pushed 8df411874c04, a merge of `main`, onto pull request
1452 of cordanaLLM/praetor. That repository allows no auto-merge
(`allow_auto_merge: false`), and the ruleset on its `main` demands linear history
(`REQUIRED_LINEAR_HISTORY`, read from `defaultBranchRef.rules`). A merge commit
there breaks the rule, and the app's merge commit (message "Merge branch 'main'
into fix/exception-expiry-every-rule") carries no sign-off: the checks "DCO 1.1 &
REUSE Compliance Gate" and "Standards & Invariant Verification Gate" failed on
it, and the pull request stays red until a person rewrites its branch.

## Decision

- **Only an armed pull request is updated.** The train updates a BEHIND pull
  request that already has auto-merge armed, or one it arms in the same run.
  Where the repository allows no auto-merge, an unarmed BEHIND pull request is
  reported `behind: auto-merge off, not armed, not updated` and left as it is.
- **A linear-history branch is never updated.** The query reads the rule types
  of the default branch (`defaultBranchRef { rules(first: 50) { nodes { type } } }`).
  When one is `REQUIRED_LINEAR_HISTORY`, the train makes no update in that
  repository and reports each BEHIND pull request as
  `behind: linear history, a person rebases`. A rebase by the forge would
  re-commit the head unsigned, so the train does not rebase either.
- **Unread rules count as linear.** When the rules come back null, the train
  cannot tell whether a merge commit is allowed and updates nothing.
- Only rulesets are read. Classic branch protection is not read, because the
  fleet's repositories declare their rules as rulesets.

## Consequences

- Pull requests in a linear-history repository wait at BEHIND for their author;
  the run summary names each one.
- Landing a CLEAN pull request is unchanged: the merge goes through the forge's
  own merge method, which a linear-history ruleset accepts with squash.
- Tests plant both directions: an armed pull request on a signed branch
  without linear history is still updated; an unarmed one without auto-merge,
  and any pull request on a linear-history or unread branch, gets no write.
