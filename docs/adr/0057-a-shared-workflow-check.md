<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0057: A shared workflow check

- **Status**: Accepted
- **Date**: 2026-10-27
- **Authors**: Lusoris

## Context

Callers across the fleet (`golusoris/goenvoy`, `golusoris/template-native-gpu`)
maintain their own copies of `tools/workflow-check.sh` running `actionlint`
and a `zizmor` pin and comment check. This duplicates linting logic, tool
installations and version pinning across individual repositories.

In this repository, actionlint is pinned in `.github/workflows/ci.yml`, and
action pins, release comments and undeclared `with:` keys are checked by
`tools/pins.py check --root DIR` using `zizmor` behind three guards
([ADR-0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md),
[ADR-0017](0017-pin-check-as-its-own-job-outside-the-gate.md),
[ADR-0047](0047-the-pin-check-is-required-on-every-pull-request.md)).
ADR-0017 deferred the reusable form of the pin check to a later record once one
week of green runs with measured forge request counts was established. That
prerequisite is met: `pins.yml` has run green 100 of 100 runs.

## Decision

- A shared reusable workflow `.github/workflows/workflow-check.yml` lints
  workflows with actionlint and verifies action pins with `tools/pins.py`.
- Callers invoke the shared workflow by commit hash with a release version
  comment ([ADR-0002](0002-versioning-and-pinning.md)).
- The workflow stops immediately on draft pull requests, checks out the caller
  without credentials, and performs a sparse checkout of this repository's check
  tools (`tools/pins.py`, `tools/ciwf.py`, `tools/forge.py`, `tools/results.py`,
  and `requirements/tools.txt`) at `job.workflow_sha`
  ([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)).
- `zizmor` and PyYAML, the two packages `tools/pins.py` needs, are installed on
  the Python 3.12 that `actions/setup-python` provides, into a temporary virtual
  environment with `pip --require-hashes`, from their hash-pinned entries in
  `requirements/tools.txt`. The step fails when either entry or its hashes are
  missing; `tools/pins.py` runs on that environment's interpreter, never on the
  runner's own Python.
- `actionlint` is installed at the release version and SHA-256 pinned in
  `.github/workflows/ci.yml`.
- `actionlint` runs against the caller's workflows.
- `tools/pins.py check --root .` runs with `ZIZMOR` pointed to the installed
  binary and `GH_TOKEN` set to `github.token`.
- A caller template `templates/callers/workflow-check.yml.tmpl` provides thin
  caller wiring with a result job reporting the required check name
  `Workflow check` ([ADR-0030](0030-a-result-job-reports-the-calling-jobs-name.md)).
- This decision supersedes the "later record" condition of ADR-0017.

## Consequences

- **Positive**:
  - Consumer repositories drop their copied `tools/workflow-check.sh` scripts
    and wire to the shared workflow.
  - Actionlint versioning and pin verification logic are maintained in one place.
  - The check runs under the caller's token and verifies pins against live forge
    metadata.
- **Negative**:
  - Installing `zizmor` and PyYAML via pip contacts `pypi.org` and `files.pythonhosted.org`,
    requiring dated host exceptions under ADR-0005 until an offline bundle or
    forge release asset distribution is used.
- **Neutral**:
  - `actionlint` 1.7.12 does not recognize `job.workflow_*`; the finding is
    accepted in `.github/actionlint.yaml` with a dated expiry bounded under
    [ADR-0010](0010-one-longest-time-for-every-exception.md).
