<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0011: A job that passes whatever it finds is compared by the result it uploads

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0004](0004-same-verdict-before-a-switch.md) compares the old and the new
job by pass and fail, and leaves out every job that does more than give a
read-only verdict. The next shared workflow is the scorecard run. Such a job
passes whatever it finds: what it states is a result file (SARIF) it uploads
as an artifact. Pass against pass says only that both jobs ran, so a new job
that scans less, or scans another tree, would count as equal.

## Decision

- A pair in the trial's map has a class. Class R is the comparison of
  ADR-0004, unchanged. Class U is a job that uploads a result; its pair names
  the artifact of each side (`old_artifact`, `new_artifact`).
- On a row of class U where both jobs pass, `tools/verdict.py` compares the
  two results. A result is the set of checks the tool ran (the rules of the
  SARIF log) and the set of its findings. A finding is its rule, its places,
  the first line of its message (for scorecard the score) and a digest of the
  other lines. What only names the run, its time and commit, is left out.
  `tools/results.py` reads the file.
- The row is equal only when both results hold the same checks and the same
  findings. A result on one side only differs. No result on either side, two
  results without a check, and two failed jobs are no evidence. An expired
  result makes the row invalid. A result that is unreadable or not recorded is
  an error and ends the check with exit code 3.
- The planted cases of class U are commits: one on which a finding must show
  in both results (`expect = "FINDING"`) and one on which it must show in
  neither (`expect = "CLEAN"`), each with a marker that is searched in the
  findings. The map needs both, beside the ten equal rows and the ordinary
  commit of ADR-0004. `PASS` and `FAIL` are refused in class U, `FINDING` and
  `CLEAN` in class R. A fixture stays class R, and a family holds pairs of one
  class.
- `tools/forge.py` reads the result of a job's last attempt, and only when
  that attempt passed: the newest artifact of the named name that was created
  after the attempt started. It refuses an archive whose digest is not the one
  the forge lists, an archive or result file above 20 MiB, more than 10,000
  results in one run, and an archive without exactly one `*.sarif` file. Its
  requests stay GET requests.
- The recorded file of a trial holds each result (checks, findings, the
  archive's digest), so a recorded trial is judged again without the forge.
- This adds to ADR-0004. Side conditions and exit codes are unchanged.

## Consequences

- **Positive**: the switch of a job that always passes is a statement about
  what both jobs found on named commits; a new job that scans less shows as
  checks or findings on one side only; the planted finding shows that the
  comparison sees a real difference in the tree.
- **Negative**: the version of the scanning tool enters the result, so two
  jobs that pin different versions can differ without a defect in the caller.
  The difference line names both versions, and the trial then pins the same
  one. A trial has to be judged, or recorded, before its artifacts expire.
  What a job sends elsewhere, to code scanning or to the scorecard service,
  is not compared.
- **Neutral**: only SARIF in an artifact is read. A job that uploads no
  artifact, such as a code scanning analysis that sends its result straight
  to the forge, needs another source and is not judged yet.

## References

- Fleet answer ledger, id shared-ci-second-workflow, 2026-10-11
  (paraphrased): the scorecard family is the candidate for the second shared
  workflow, built only after its one unknown is settled, and proven on
  fixtures first.
- Fleet answer ledger, id shared-ci-scorecard-publishing, 2026-10-11
  (paraphrased): scorecard results can be published from a called workflow,
  so the scorecard family is the second shared workflow.
- `tests/test_verdict_result.py`: the passing trial and the fourteen recorded
  cases that must not pass; `tests/test_results.py` and `tests/test_forge.py`:
  the reader of a result and of an artifact.
