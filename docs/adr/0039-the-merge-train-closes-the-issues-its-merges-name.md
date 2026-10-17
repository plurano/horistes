<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0039: The merge train closes the issues its merges name

- **Status**: Accepted
- **Date**: 2026-10-17
- **Authors**: Lusoris

## Context

Pull requests the merge train merged left the issues they name in `Closes #N`
open (#32). In cordanaLLM/nucleus, #170 (`Closes #113`) and #174 (`Closes
#114`) were merged by the app, and both issues were closed by hand later. Both
were auto-merges: the train armed them and the forge merged them for the app,
so `mergedBy` reads `plurano-merge-train`, a bot. The forge still lists the
issue under `closingIssuesReferences` of the merged pull request. Pull requests
a person merged in the same repository closed their issues on merge.

Most train merges take the auto-merge path, so a close step only after the
train's own merge call would miss most of them.

## Decision

- **Every sweep closes what recent train merges left open.** After its one
  write, the train reads the 10 most recently updated merged pull requests of
  the repository. For each one the app merged in the last 7 days it closes the
  open issues in `closingIssuesReferences`, the forge's own reading of the
  closing keywords and of issues linked by hand, so the train does not keep a
  second parser of those keywords.
- **Same repository only.** The token is limited to the repository; issues in
  other repositories stay with a person.
- **A reopened issue stays open.** An issue with a reopen event after the merge
  was reopened on purpose; the train leaves it alone.
- **The comment says why.** Before closing, the train comments "Closed by #P,
  which the merge train merged as <commit>", then closes the issue as
  completed.
- **Issues write, in a token of its own.** The app's rights gain issues write
  (`apps/merge-train.json`). The train mints a token with issues write alone,
  for that one repository, only when an issue is to be closed, and revokes it
  afterwards. Without the right the mint is refused and the repository is
  reported refused, naming the issues that stay open; nothing is skipped
  silently.

## Consequences

- Linked issues close within one sweep of the merge, whoever pressed merge
  for the app.
- The app asks every installation for one more right. Until an installation
  accepts it, repositories there report a refusal whenever an issue is to be
  closed, and work otherwise as before.
- A sweep costs one more GraphQL read per repository.
- An issue a merge names after the 7-day window, or past the 10 newest merged
  pull requests, is not closed by the train.
- The train's GraphQL documents move into the rules module the step before
  writes, which keeps the train script under the engine's cap of 4096 fields
  per run script (decision record 0037).

## References

- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- GitHub GraphQL API documentation: `PullRequest.closingIssuesReferences`,
  `PullRequest.mergedBy`, `ReopenedEvent`.
- GitHub REST API documentation: "Update an issue" (`state_reason`), "Create
  an issue comment".
