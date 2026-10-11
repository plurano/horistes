<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0017: The pin check runs as its own job, on workflow changes, weekly and by hand, outside the gate

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md) made
`tools/pins.py` and left it without a runner: `make check-pins` ran when a
person or a session started it. The check cannot join `make verify`, because
the gate asks no host and this check asks the forge and the repository of
every action.

Two things go wrong while nobody runs it:

- A change to a workflow brings a wrong release comment or a misspelled
  `with:` key, and the gate passes.
- A release name is moved to another commit after the pin was written. No
  file of this repository changes, so no pull request shows it.

The family wants the same check in every repository. What one run costs a
caller's token was not known, and a repository's token has a budget of its
own.

## Decision

- `.github/workflows/pins.yml` runs `make check-pins` on the workflows of this
  repository. It starts on a pull request that changes a file under
  `.github/workflows/`, `tools/pins.py`, `tools/ciwf.py`, `tools/forge.py`,
  `requirements/tools.txt` or the `Makefile`; once a week; and by hand.
- The job runs with the token of the run and read access to the contents. It
  takes no secret.
- The job is no part of `make verify` and no required check. A red run is a
  finding to fix; a host that does not answer does not hold a merge that
  changes no workflow.
- Every run writes the number of forge requests the check took into its
  summary. The number is the difference of the counter in the header of one
  ordinary request before the check and one after it, less that second
  request.
- A reusable workflow that lets other repositories call the check is a later
  record. It waits for one week of green runs here with the request count
  known.

This adds to ADR-0016. Its statement that nothing runs the check on its own
ends with this record; everything else in it stands.

## Consequences

- **Positive**:
  - A wrong pin, comment or `with:` key in a changed workflow is a red job on
    the pull request that brings it.
  - A moved release name is seen within a week.
  - The cost of a run is on record with each run. On 2026-10-11 the check
    took 6 requests for the 9 outside uses of this repository: one for each
    of the 5 distinct actions, and a second one for the action whose metadata
    is in `action.yaml`. zizmor's own reading took none.
- **Negative**:
  - The job can be red while the gate is green, and a merge is not held by
    it. Somebody has to read the weekly run; nothing opens an issue.
  - The engine's audit reads the latest run of every workflow on `main`. A
    weekly run that failed is a warning in the next local gate; the audit
    reports it and does not fail on it.
  - A pull request that changes none of the listed paths does not start the
    job.
  - The count is taken from a counter the runs of this repository share.
    Another run in the same minute adds to it: the run on the last commit of
    the pull request read 8, with the gate and the licence check started in
    the same second.
  - The forge's counter endpoint cannot be used for the count: for the token
    of a run it answered 0 used before and after the check, while the headers
    of ordinary requests counted (measured in two runs on 2026-10-11).
  - The other repositories of the family still run the check by hand.
- **Neutral**: the two package hosts of `make tools` are declared for the new
  file in `hosts.toml`, as for the gate.

## References

- Fleet answer ledger, 2026-10-11 (paraphrased). shared-ci-check-pins-in-ci:
  the pin check first runs as its own job in this repository, on pull
  requests that touch workflows and weekly, with the repository's token and
  outside the gate; a reusable workflow follows after one green week with the
  forge request count measured.
- The job was seen to refuse on 2026-10-11. A commit of the pull request that
  brought this record commented the checkout of the gate as `v7.0.0` on the
  commit of `v7.0.1`; run 38147757680 failed with
  `FAIL: .github/workflows/ci.yml:53: ... is pointed to by tag v7.0.1`. The
  run on the commit before the plant, 38147394234, passed on 5 files, 9
  outside uses and 19 `with` keys.
- Request count: run 38147877630 read the counter as 21 before the check and
  28 after it, the second reading included. The same check run on a
  workstation through a client that logs its calls made 6 requests: one
  `action.yml` for each of the 5 distinct actions and `action.yaml` for
  `ossf/scorecard-action`. Run 38148186670 read 32 and 41.
- `tools/hosts.py check` refused the workflow without one of its two
  declarations: `host files.pythonhosted.org (Makefile, command
  `python -m pip install`) is not declared`.
