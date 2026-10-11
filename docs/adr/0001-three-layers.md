<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0001: One source, a copy per owner, thin callers

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

The repositories of the family each keep their own copy of the same CI jobs.
The copies drift: the same job exists with tag pins and commit pins, with and
without a timeout, on different runner images. The jobs are to be kept once.

Three rules of the forge shape where "once" can be (forge documentation, read
2026-10-11):

- A public repository can call reusable workflows only from a public
  repository. A private repository's workflows are reachable only from private
  repositories of the same owner.
- A called workflow reaches an owner's self-hosted runners only when caller and
  called workflow have the same owner.
- A renamed workflow repository is not redirected for callers.

The consumers belong to several owners, and most of them are public.

## Decision

Three layers:

1. **This repository is the one source.** It holds the reusable workflows
   (`.github/workflows/`), composite steps (`actions/<name>/`), caller
   templates, the recorded interface and the tests.
2. **One rendered copy per owner.** The standards engine renders the released
   workflows into one repository of each owner. A caller always calls the copy
   of its own owner.
3. **Thin callers in each repository**, rendered by the engine from the
   repository's standards file and pinned to a commit of the owner's copy. The
   engine's audit reports a caller or a copy that differs from its rendering.

Until the copies exist, only private repositories of this organisation call
this repository directly.

## Consequences

- **Positive**: one text per job; public and private consumers and self-hosted
  runners all work within the forge's rules; no caller names this repository,
  so it can be renamed.
- **Negative**: a release reaches a consumer in two steps (copy, then caller);
  the copies depend on the engine's rendering, which does not exist yet.

## References

- Fleet answer ledger, ids ci-config-02 and ci-config-04 (CI configuration
  layers), and shared-ci-start, 2026-10-11 (release of this work)
  (paraphrased): callers stay thin and call a per-owner copy; this repository
  sets itself up first and consumers switch one at a time.
