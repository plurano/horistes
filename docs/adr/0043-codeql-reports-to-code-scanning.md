<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0043: CodeQL analyses the workflows and the Python tools and reports to code scanning

- **Status**: Accepted
- **Date**: 2026-10-18
- **Authors**: Lusoris

## Context

The forge's code scanning held no analysis for this repository: the code
scanning API answered 404 for its analyses on 2026-10-18. The scorecard
workflow uploads to code scanning only where a caller runs it with
`outside_hosts` ([ADR-0013](0013-scorecard-behind-a-switch.md)), and this
repository does not call it. The product is workflow files, and the checks
under `tools/` are Python; zizmor and actionlint read the workflows in the gate,
but nothing analysed the data flow of either.

## Decision

- `.github/workflows/codeql.yml` runs CodeQL for the languages `actions` and
  `python` with build mode `none`, and uploads the findings to code scanning.
- It runs on a pull request that changes a Python file, a workflow or an
  action, on a push to `main`, weekly and by hand. It is no part of
  `make verify-all` and no required check: code scanning shows the findings on
  the pull request and keeps the alerts. The path filter keeps it out of the
  required checks the engine derives for the branch ruleset, which takes every
  job that reports on every pull request; the ruleset is unchanged.
- `github/codeql-action/init` and `github/codeql-action/analyze` are declared
  in `hosts.toml` with no host beyond the forge: the bundle comes from the
  forge's release assets, and neither language is built, so no package index
  is asked ([ADR-0003](0003-declared-hosts.md)). Both are pinned to the same
  commit as `github/codeql-action/upload-sarif`, so the update bot moves the
  three together.

## Consequences

- **Positive**: code scanning holds an analysis of every change and of `main`;
  the security overview of the repository is no longer empty.
- **Negative**: one more job runs on most pull requests, and an outage of the
  forge's code scanning fails it without a local defect. A change outside the
  filtered paths is analysed on `main` after the merge, not before it.
- **Neutral**: an alert does not fail the gate; it is read on the pull request
  and in code scanning.

## References

- [ADR-0003](0003-declared-hosts.md)
- [ADR-0013](0013-scorecard-behind-a-switch.md)
