<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0047: The pin check runs on every pull request and is a required check

- **Status**: Accepted
- **Date**: 2026-10-19
- **Authors**: Lusoris

## Context

[ADR-0017](0017-pin-check-as-its-own-job-outside-the-gate.md) started
`.github/workflows/pins.yml` only on pull requests that change a workflow,
the pin tool or its pins, and kept the job out of the required checks. A red
run held no merge.

The second gap audit of 2026-10-19 found the job had never run on `main`
from a push or the schedule, and that the rule set of `main` required only
`Verification gate` and `Licence`. A wrong pin could land while its run was
red.

The engine renders the rule set: it requires the check of every job that
reports on every pull request, read from the workflows with an unfiltered
`pull_request` trigger (`internal/forge/workflow_checks.go`,
`RequiredStatusContexts`). A path filter keeps a job out of the rule set, and
a hand edit of `.github/rulesets/main.json` fails `praetorctl audit`.

## Decision

- `.github/workflows/pins.yml` starts on every pull request, without a path
  filter. The weekly run and the start by hand stay.
- `praetorctl sync` renders `.github/rulesets/main.json` with `Action pins`
  as a third required check.
- After the change is on `main` and a run of the job on `main` is green, the
  rule set is applied through the forge API and read back, as
  [ADR-0025](0025-the-rule-set-goes-live-before-the-engine-fix.md) states.

This replaces the trigger list of the first item and the third item of the
ADR-0017 decision. The rest of ADR-0017 stands.

## Consequences

- **Positive**:
  - A pull request with a wrong pin, release comment or `with:` key cannot
    merge; the merge no longer depends on somebody reading a red run.
  - The file and the live rule set stay equal: the engine renders the new
    check from the workflow itself.
- **Negative**:
  - Every pull request spends the forge requests of one pin check (6 for the
    9 outside uses on 2026-10-11, per ADR-0017) from the repository's token.
  - A forge or action host that does not answer holds every merge, also one
    that changes no workflow, until the job is run again.
- **Neutral**: the job keeps its name, so the required context is the name
  the weekly runs already report.

## References

- Fleet answer ledger, 2026-10-19, horistes-gap2-actionpins-required
  (paraphrased): make the pin check a required check by removing its path
  filter, then apply the rule set and read it back once the job is green on
  `main`.
- Second gap audit, row A2-06: the rule set required two checks, and the
  forge listed no push or scheduled run of `pins.yml` on `main`.
- `praetorctl audit` refused `main.json` with `Action pins` added by hand:
  `ruleset differs from declared branch protection policy`. With the path
  filter removed, `praetorctl sync` rendered the same file from the workflow.
