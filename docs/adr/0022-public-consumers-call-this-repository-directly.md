<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0022: Public consumers call this repository directly; the copy per owner is not built

- **Status**: Accepted
- **Date**: 2026-10-12
- **Authors**: Lusoris

## Context

[ADR-0001](0001-three-layers.md) set three layers: this repository as the one
source, one rendered copy of the released workflows in a repository of each
owner, and thin callers that call the copy of their own owner. The copy
answered two rules of the forge: a public repository calls reusable workflows
only from a public repository, and this repository is private.

The copies were never built. They depend on a rendering in the standards
engine that does not exist, and each release would reach a consumer in two
steps. The operator decided the other way round: this repository becomes
public, so the rule no longer stands between it and a public consumer.

The rules of the forge, read again 2026-10-12:

| Rule | Source |
| :--- | :--- |
| a public caller reaches reusable workflows in public repositories only; a private caller reaches private and public ones | "Reusing workflow configurations", access table |
| a private repository's workflows are reachable only where its access setting allows it | same section |
| a called workflow reaches the caller's self-hosted runners only when it has the same owner as the caller | same page, "Self-hosted runners" |
| the machine of a hosted job is decided and billed in the caller's context | same page, "GitHub-hosted runners" |
| a call to a workflow or an action is not redirected after its repository was renamed or moved to another owner | same section, note; "Renaming a repository", note |

## Decision

- The second layer of ADR-0001 is not built. There are two layers: this
  repository as the one source, and thin callers that call it directly, pinned
  to a full commit of this repository as
  [ADR-0002](0002-versioning-and-pinning.md) asks.
- This repository becomes public. The order is fixed:
  1. its real name, chosen with the operator;
  2. a proof over the whole history that it holds no private content;
  3. the first release;
  4. the change of visibility, which is the operator's act.
- Until step 4 only private repositories of the same owner call it. A public
  repository keeps its own job until then.
- The name is final with the first release. Callers name this repository, and
  the forge does not redirect a call after a rename. A later name would be a
  new repository beside an archived old one, never a rename.
- A consumer of another owner runs the called jobs on hosted machines. Its own
  self-hosted runners are out of reach for a workflow called from here. A job
  that needs an owner's self-hosted runners stays in a repository of that
  owner.

This replaces layer 2 of ADR-0001, the clause of its layer 3 under which a
caller is pinned to the owner's copy, and its sentence "Until the copies
exist, only private repositories of this organisation call this repository
directly". Where ADR-0002 names copies beside callers, callers alone are
meant. Layers 1 and 3 of ADR-0001 stand otherwise.

## Consequences

- **Positive**:
  - A release reaches a consumer in one step: the pin in its caller.
  - Nothing here waits for a rendering of copies in the engine.
  - A public consumer reads the workflow it runs in a public place.
- **Negative**:
  - ADR-0001 counted it as a gain that no caller names this repository, so
    that it can be renamed. That is gone: the name must be right before the
    first release.
  - Consumers of another owner cannot run the shared jobs on their
    self-hosted runners. The input `runner` of the licence workflow takes any
    label, and a label of such a runner would leave the job waiting. This is
    read from the documentation and is not confirmed by a run.
  - History, issues and pull requests become public with the repository. The
    proof of step 2 covers the history of the files; the texts on the forge
    need their own reading before step 4.
  - Until step 4 a public repository of the family has no shared job at all.
- **Neutral**: the caller templates and the engine's audit of a rendered
  caller ([ADR-0009](0009-caller-templates-in-the-engines-syntax.md)) are
  unchanged; only the repository a caller names differs from ADR-0001.

## References

- Fleet answer ledger, 2026-10-11 (paraphrased).
  shared-ci-public-consumer-path: this repository gets its real name and a
  first release and then becomes public, so that public projects call it
  directly; the order is name, a proof of the whole history for private
  content, the first release, then the change of visibility as the operator's
  act; the copy path of ADR-0001 is not built and a superseding record says
  so.
- Forge documentation, read 2026-10-12:
  `docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations`
  (sections "Access to reusable workflows" and "How reusable workflows use
  runners"),
  `docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository`
  (note on calls to a renamed repository).
- `.github/workflows/licence.yml`: the input `runner` sets `runs-on` of the
  called job.
