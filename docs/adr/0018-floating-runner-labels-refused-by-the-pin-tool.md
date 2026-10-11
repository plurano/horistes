<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0018: A floating label of a hosted machine is refused by the pin tool, without an exception list for now

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

A job that runs on `ubuntu-latest` changes its build machine on a day the
forge chooses, without a commit in the repository. The family's order is that
every workflow names the release of the machine by 2026-10-19.

Four repositories of the family wrote a check of their own for that order.
They do not read the same thing:

| Repository | Check | What it reads |
| :--- | :--- | :--- |
| sveltesentio | `scripts/check-runner-labels.mjs` | every line of a workflow that is no whole-line comment, for `ubuntu-latest` as a whole word; a step name that says the label fails as well |
| pelorus | `scripts/check-build-config.py` | the exact machine of each job, and the words `ubuntu-latest` and `windows-latest` |
| 20-watts | `scripts/validate-engineering-policy.mjs` | `runs-on` of each job has to be one named release |
| golusoris | `scripts/ci/portability_test.py` | the three machines of one workflow as exact text: `ubuntu-26.04` beside `macos-latest` and `windows-latest` |

One implementation should replace the part they share (see References).
[ADR-0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md) made
`tools/pins.py` the family's tool for what a workflow is pinned to, and it
already reads every workflow of a checkout with the strict reader.

The forge's documentation names five floating labels of hosted machines:
`ubuntu-latest`, `windows-latest`, `macos-latest`, `macos-latest-large` and
`macos-latest-xlarge`. An organisation can give a machine group any label, so
`ubuntu-latest-xl` or `team-ubuntu-latest` is a label of its own. The
documentation lists the contexts `runs-on` may take: `github`, `needs`,
`strategy`, `matrix`, `vars` and `inputs`. A label can therefore reach a job
from a matrix, from an input of the workflow, or from the job of another
workflow that calls it.

## Decision

- `tools/pins.py` gets the command `runners`. It fails on a workflow that
  names one of the five labels where a workflow can choose a machine. Exit
  code 0 is a pass, 1 a finding, 3 a workflow that cannot be read. `check`
  and `pin` run it as well; `pin` rewrites no label, because which release a
  job should run on is a decision of the repository.
- The places read are:
  - `runs-on` of a job: a text, a list, the form with `group` and `labels`,
    and the text of an expression;
  - every value under `strategy` of a job;
  - every value a job hands to a called workflow under `with`;
  - `default` and `options` of an input of `workflow_call` and
    `workflow_dispatch`.
- The label is matched as a whole word and without regard to case. A letter,
  a digit, `_`, `-` or `.` next to it makes it another label, which passes.
  Whether the forge tells labels apart by case was not verified, so the check
  takes the side that refuses.
- A name, a description, a command, a variable and a comment are not read. A
  workflow may say the label where it chooses no machine.
- The check asks no host, needs no token and does not start zizmor. It is
  part of `make verify` here (`make check-runners`); another checkout is read
  with `PINS_ROOT`.
- There is no exception list yet. The exceptions of the family are the list
  in `.standards.yaml` that the engine's audit reads, and the engine refuses
  an entry whose rule it does not know. The engine will get the rule name
  `runner-label`, bound to one workflow file by its path. Until it has, a
  finding is fixed; the reading of that list is a later record.

This adds to ADR-0016. Its statement that the tool has no exception list
holds for pins and `with` keys, and for now for labels too.

## Consequences

- **Positive**:
  - One command gives one `FAIL` line with file, line, place and label for
    any checkout of the family, whatever its own language.
  - A label that reaches a job through a matrix, an input or a calling job is
    found, which a reading of `runs-on` alone misses.
  - A workflow that only mentions the label in a name or a command passes,
    so nothing has to be reworded.
  - The gate of this repository refuses a floating label on every commit.
- **Negative**:
  - A label that exists only while the workflow runs is not read: a
    repository variable (`vars`), an output of an earlier job (`needs`), a
    text put together from parts (`ubuntu-${{ inputs.release }}`). The pass
    line counts the jobs that take their label from an expression, so a
    reader sees how many depend on a value the check may not have seen.
  - Every value under `strategy` and `with` is read, not only the ones a
    `runs-on` uses. A matrix value that holds the label for another purpose
    fails and has to be renamed.
  - A repository with a reason to keep a floating label cannot pass until
    the engine has the rule name and the later record is in force.
  - The check does not say which release to name, and it does not refuse a
    release that the forge has retired.
  - The checks of pelorus and 20-watts demand one exact machine for a job.
    This check is weaker than that part and does not replace it.
  - The test of golusoris demands `macos-latest` and `windows-latest` in its
    portability workflow. This check refuses both, so that repository has to
    name two releases and change its test before it passes.
  - A workflow with a YAML merge key (`<<`) is refused with exit code 3, as
    for the other commands of the tool.
- **Neutral**: `make lint-workflows` keeps its own reading of `runs-on`.
  actionlint 1.7.12 and zizmor 1.30.1 with the persona `pedantic` report no
  floating label (measured 2026-10-11 on a workflow with
  `runs-on: ubuntu-latest`).

## References

- Fleet answer ledger, 2026-10-11 (paraphrased). sveltesentio-runner-pin-validator:
  four repositories built their own copy for the order of 2026-10-19; one
  implementation in this repository's pin tool should replace them, with
  planted cases. shared-ci-open-list-20261011: no second exception format;
  the check reads the engine's list in `.standards.yaml`.
  shared-ci-runner-label-rule-name: the engine adds the rule name
  `runner-label`, bound to one workflow file; the check ships without
  exception reading until then.
- Forge documentation, read 2026-10-11: the contexts of
  `jobs.<job_id>.runs-on`
  (<https://docs.github.com/en/actions/reference/workflows-and-actions/contexts>)
  and the labels of hosted machines
  (<https://docs.github.com/en/actions/reference/runners/github-hosted-runners>,
  <https://docs.github.com/en/actions/reference/runners/larger-runners>).
- `tests/test_runner_labels.py`: a workflow that says the label in every
  place that chooses no machine passes; 17 planted cases fail with the exact
  line, one for each place and each label; four labels of an organisation's
  own pass. `tests/test_pins.py`: `check` and `pin` fail on a planted label
  and leave the file as it was.
- Each check was seen to refuse on 2026-10-11: 25 defects planted one at a
  time in a copy of the tool, among them each place left unread, each label
  taken out, the word boundary and the case rule removed, and the finding
  left out of `check`; the tests failed on each. The log is not part of the
  tree.
- The engine's exception list as read on 2026-10-11:
  `internal/config/exceptions.go` of the engine knows nine rule names and
  refuses any other when it loads the manifest.
