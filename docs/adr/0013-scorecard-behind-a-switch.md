<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0013: The scorecard run sits behind a switch that a caller sets by the repository's visibility

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

The second shared workflow runs the OpenSSF Scorecard action. Three things
about that action do not fit the rules the first workflow was written to.

- **Hosts.** The action asks four outside hosts on every run and has no input
  that turns them off. Two of them, the best-practices badge service and the
  dependency insight service, are asked with the name of the repository; for
  a private repository that name leaves the forge. Publishing a result adds
  four more hosts. [ADR-0003](0003-declared-hosts.md) lets a reusable workflow
  reach only the forge with default inputs, and
  [ADR-0005](0005-host-exception-with-an-end.md) allows a host without a
  switch only with a day it ends, which these hosts do not have.
- **Scopes.** The job writes its findings to the forge's code scanning and
  signs a publication with the forge's identity token, so it asks for
  `security-events: write` and `id-token: write`. A called job gets no more
  than the calling job grants, and the forge refuses to start a run whose
  caller grants less. The template check of
  [ADR-0009](0009-caller-templates-in-the-engines-syntax.md) refused every
  write scope.
- **Service rules.** Before the Scorecard service stores a published result it
  reads the workflow file that ran the action, which for a reusable workflow
  is the called file, and refuses the result when that file breaks one of its
  rules: among them one runner label written out, no `run` step and no `env`.
  A broken rule shows in no linter and in no test run here; it shows as a
  failed publication in every caller.

## Decision

- `.github/workflows/scorecard.yml` has one job. The boolean input
  `outside_hosts`, off by default, gates the whole job; the second boolean
  input `publish`, off by default, switches on the publication and its four
  hosts. `hosts.toml` declares all eight for the file with their input, so
  ADR-0003 holds as written.
- A host that is asked with the repository's name, or that stores a result
  under it, carries `public_only = true` beside its `input` in `hosts.toml`.
  `tools/hosts.py check` reads the mark: it is `true` or absent, it needs an
  `input`, and only a reusable workflow takes it.
- A caller passes an input that switches on a marked host either as false or
  as the repository's visibility, written exactly as
  `${{ github.event.repository.visibility == 'public' }}` (the constant
  `PUBLIC` in `tools/render.py`). `tools/render.py check` refuses anything
  else, a plain `true` first of all. The expression is false in a private or
  internal repository and false where the event names no repository, so the
  switch fails towards off, and the job starts to run by itself on the day a
  repository becomes public.
- A caller's job grants exactly the scopes the jobs of the called workflow ask
  for, each at the highest level one of them asks. `tools/render.py check`
  refuses a scope that is granted and not asked for, and a scope that is asked
  for and not granted. This replaces the clause of ADR-0009 under which a
  caller asks for no write scope; the rest of that record stands.
- `tools/scorecard.py check`, part of the gate as `make check-scorecard`,
  holds every workflow that runs the action to the service's rules as its
  source states them. It is stricter in two places: it refuses a second job
  that runs the action, because the service picks the job in no fixed order,
  and it refuses the action written as an image.
- Because the service takes one literal label, the workflow names
  `ubuntu-26.04` itself and has no `runner` input. Because it takes no `run`
  step, the workflow has no step that stops on a draft pull request; the
  template runs the job on a push to the default branch and on a schedule
  only.
- `templates/callers/scorecard.yml.tmpl` passes `outside_hosts` by visibility
  and does not pass `publish`: the service fetches the called file from this
  repository, which it can do only once the repository is public.
- The forge's own storage for artifacts
  (`results-receiver.actions.githubusercontent.com`,
  `*.blob.core.windows.net`) joins the `forge` list of `hosts.toml`; the
  workflow keeps its result file as an artifact.

## Consequences

- **Positive**: a private repository's name reaches no outside host through a
  rendered caller; a caller that grants too little fails this repository's
  gate and not the consumer's first run; an edit that would stop every
  caller's publication fails the gate here.
- **Negative**:
  - The visibility rule binds the template. A caller written by hand in a
    private repository can still pass `true`, and the called workflow then
    runs; it does not check the visibility itself.
  - `id-token: write` is granted on every run, because a scope takes no
    expression; the action asks for the token only when `publish` is true.
  - The action is pinned by commit, and its `action.yaml` names its image on
    the forge's registry by tag, not by digest.
  - Whether the event of a scheduled run carries the repository's visibility
    is not measured yet. If it does not, a scheduled run skips the job and the
    push run still reports; the first public consumer shows which.
  - `*.blob.core.windows.net` is a pattern over every storage account of that
    provider. The host scan is a review aid on the text of the files
    (ADR-0003), so the pattern widens what a reviewer accepts, not what a
    runner can reach.
- **Neutral**: no consumer can call the workflow while this repository is
  private, so the workflow, its template and both checks are shown on
  fixtures and by the linters; the same-verdict run of
  [ADR-0011](0011-uploaded-result-of-a-job-that-always-passes.md) follows with
  the first public consumer.

## References

- Fleet answer ledger, id shared-ci-scorecard-hosts, 2026-10-11 (paraphrased):
  one boolean input, off by default, gates the whole scorecard job, because
  the action reaches four outside hosts on every run and has no switch of its
  own; the publishing hosts sit behind a second input; the caller template
  passes true in public repositories only, because two of the hosts receive
  the repository's path; a class of hosts without a switch and an end is a
  tier and is not introduced.
- ossf/scorecard-infra, `api/app/server/verify_workflow.go` at commit
  `ffe0a11789f8` (2026-09-28), read 2026-10-11: the rules
  `tools/scorecard.py` holds a workflow to.
- ossf/scorecard v5.5.0, read 2026-10-11: `clients/cii_client.go` (badge
  service), `internal/packageclient/depsdev.go` (dependency insight, asked
  with the repository's path), `clients/ossfuzz/client.go` (fuzzing build
  logs), `clients/osv.go` (vulnerability database).
- ossf/scorecard-action v2.4.4 (commit `2d1146689b8c`), read 2026-10-11:
  `action.yaml` (image by tag), `options/options.go` (no publication for a
  private repository), `signing/signing.go` (signature and service upload).
- GitHub Docs, self-hosted runners reference, essential communications
  (docs commit `9477b6a96cb4`, 2026-07-28), read 2026-10-11: the hosts of the
  artifact storage.
- `tests/test_scorecard.py`: one planted defect per service rule, and the
  labels and actions the service takes.
- `tests/test_render.py`: the grant that is too wide and the one that is too
  narrow; the real scorecard template with a plain `true`, which is refused.
- `tests/test_hosts.py`: a `public_only` that is not `true`, one without an
  `input`, and one outside a reusable workflow.
