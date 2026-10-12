<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0023: The repository is named Horistes, and the rename comes before the first release

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

[ADR-0022](0022-public-consumers-call-this-repository-directly.md) fixed the
order towards a public repository: the real name, a proof over the history,
the first release, the change of visibility. Until now the repository carried
the working name `shared-ci`. The name sits in every caller line and in every
preset reference of a consumer, and the forge does not redirect a call after a
rename, so it has to be right before the first release.

The operator chose the name on 2026-10-12 from a screened list in the style of
the family: a Greek or Latin word for an impartial office or an exact
instrument.

Where the working name stood on that day (commit `9193f1d` of the private
archive `plurano/horistes-archive`; the same tree is commit `1bbad79` here):

| Kind | Places | Treated as |
| :--- | :--- | :--- |
| identity the standards engine wrote at adoption: the manifest name, the title of `AGENTS.md` and the four files compiled from it, the debt baseline, the checkpoint policy, the Paperclip harness and its rules, the devcontainer name, the link in the README's governance block | 12 | changed by value |
| own text and code: the README title, its section `Name` and the preset reference it shows, the title of `CONTRIBUTING.md`, `renovate.json`, two tests, the user agent of `tools/stranger.py` | 9 | changed |
| records 0001 to 0022: 23 ids of the fleet answer ledger that begin with `shared-ci-`, and the preset reference in ADR-0020 and ADR-0021 | 25 | kept |

The engine has no command for a rename (its issue 1188). A new adoption run
would change the identity and, at the pinned engine, also add a licence
workflow, a required check and a rule for the update bot. None of these
belongs to a rename.

## Decision

- The repository is named **Horistes**: Greek *horistes*, the boundary
  commissioner who fixes limits; pronounced ho-RIS-tees. On the forge it is
  `plurano/horistes`.
- The name is explained where it is shown: in the README's section `Name`.
- The rename on the forge is the operator's act. It comes before the first
  release, and the change that carries this record merges after it. The
  repository's own `renovate.json` names `github>plurano/horistes`, and that
  reference resolves only once the forge knows the name.
- The identity the engine wrote is changed value by value, followed by
  `praetorctl compile-context`. The engine's audit is the judge: it reads the
  identity from the manifest and passes on the result. No adoption run is made
  for the rename.
- Accepted records keep their text. A ledger id keeps its `shared-ci-` prefix,
  because it is the address of an answer, not a name of this repository. The
  preset reference in ADR-0020 and ADR-0021 reads `github>plurano/horistes`
  from this record on; ADR-0020 foresaw this as the cost of a rename.
- A consumer changes its reference in its own repository after the rename on
  the forge.

This completes step 1 of the order in ADR-0022. Its other steps stand.

## Consequences

- **Positive**:
  - Callers and preset references can be written with the final name before
    the first release pins them.
  - A caller line or a preset reference on the former address fails the
    tests here.
  - The gate passes before and after the rename on the forge: while the
    manifest and the forge disagree, the audit skips its two reads of the
    forge and says so.
- **Negative**:
  - Between the rename on the forge and the change in a consumer, that
    consumer's preset reference rests on the forge's redirect of the old name.
    That it resolves is not confirmed by a run of the bot; a reference that
    does not resolve shows as a configuration issue in the consumer.
  - The audit does not name every stale place. Measured at engine
    `bd40beae2969`: with the old name left in the debt baseline, in the
    checkpoint policy, in the title of `CONTRIBUTING.md` or in the title of
    `AGENTS.md` together with its compiled files, it passes. These places
    were found by a search of the tree, not by the audit.
  - The gate refuses the former address (owner and working name) in every
    tracked file except ADR-0020 and ADR-0021 (`tests/test_name.py`). The
    bare working name in new prose is not refused, because ledger ids carry
    it.
  - Texts on the forge (issues, pull requests, the dashboard of the update
    bot) keep the working name. They are read before the change of
    visibility, as ADR-0022 says.
- **Neutral**: the working name stays readable in records 0001 to 0022 and in
  the history.

## References

- Fleet answer ledger, 2026-10-12 (paraphrased). shared-ci-name: the operator
  chose Horistes as the real name of the shared CI repository; it becomes
  final with the first release, and the rename with every place that carries
  the name follows as a unit of this repository, the name explained where it
  appears. shared-ci-rename-engine-path: the engine has no rename command;
  two repositories of the fleet were renamed by hand with the audit as judge;
  accepted records keep their text and a new record names the rename;
  consumers change their reference in their own repositories after the
  rename on the forge. shared-ci-naming-funnel: the style of the family's
  names.
- Engine `bd40beae2969`, measured 2026-10-12 in a worktree of this change:
  with the manifest name alone changed, `praetorctl audit --offline` fails on
  the README's governance block; with the twelve identity values changed it
  passes; `praetorctl audit` then passes and skips the reads of workflow runs
  and of branch protection, because the manifest and the remote name different
  repositories. Each identity place was set back to the old value alone and
  the audit run again: the Paperclip harness, its rules, the devcontainer and
  the README block fail it; the debt baseline, the checkpoint policy and the
  title of `CONTRIBUTING.md` do not; `AGENTS.md` fails it only while its
  compiled files differ from it.
- `praetorctl adopt --dry-run` at the same engine, read 2026-10-12: the plan
  of a new adoption run.
- Forge documentation, read 2026-10-12, as cited in ADR-0022: a call is not
  redirected after a rename.
