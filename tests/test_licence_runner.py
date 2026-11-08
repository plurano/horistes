# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Licence runner gate: the early step requires a Linux runner and refuses anything else.

Pins gap audit row C13-02 for .github/workflows/licence.yml and README.md.
"""

from __future__ import annotations

import copy
import re
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

STEP_NAME = "Require a Linux runner"
REQUIRED_IF = "runner.os != 'Linux'"
REQUIRED_EXIT = "exit 1"
REQUIRED_ANNOTATION = "::error"
README_RUNNER_SUBSTRING = "`runner` (Linux label"
LICENCE_WORKFLOW = ".github/workflows/licence.yml"

RUNNER_OS_PATTERN = re.compile(r"^runner\.os != '([A-Za-z]+)'$")
ASSIGNMENT_PATTERN = re.compile(r'^\$[A-Za-z_]\w*\s*=\s*"[^"]*"$')
ANNOTATION_PATTERN = re.compile(r'^Write-Output\s+"::error[^"]*"$')


def _check_job(job: dict) -> list[str]:
    """Check job-level properties for the reuse job."""
    problems: list[str] = []
    if "if" in job:
        problems.append(f"job `reuse` has a condition ({job['if']!r})")
    if "continue-on-error" in job:
        problems.append(f"job `reuse` has continue-on-error ({job['continue-on-error']!r})")
    return problems


def _check_step(step: dict, idx: int) -> list[str]:
    """Check step-level properties for the Linux runner gate."""
    problems: list[str] = []
    if idx != 0:
        problems.append(f"step {STEP_NAME!r} is not the first step of the job")
    if step.get("if") != REQUIRED_IF:
        problems.append(f"step {STEP_NAME!r} `if` is not exactly {REQUIRED_IF!r}")
    if "continue-on-error" in step:
        problems.append(f"step {STEP_NAME!r} has continue-on-error ({step['continue-on-error']!r})")
    if step.get("shell") != "pwsh":
        problems.append(f"step {STEP_NAME!r} shell is not 'pwsh' (got {step.get('shell')!r})")
    return problems


def _check_script(step: dict) -> list[str]:
    """Check run script statements against an allow-list, returning at most one problem."""
    run_script = str(step.get("run", ""))
    raw_lines = [line.strip() for line in run_script.splitlines()]
    effective_lines = [line for line in raw_lines if line and not line.startswith("#")]

    if not effective_lines or effective_lines[-1] != REQUIRED_EXIT:
        return [
            (
                f"step {STEP_NAME!r} run script does not end the step with a nonzero exit"
                f" (the last statement must be {REQUIRED_EXIT!r})"
            )
        ]

    for line in effective_lines[:-1]:
        if (
            "$(" in line
            or "`" in line
            or ";" in line
            or not (ASSIGNMENT_PATTERN.fullmatch(line) or ANNOTATION_PATTERN.fullmatch(line))
        ):
            return [f"statement outside the allow-list: {line}"]

    if not any(ANNOTATION_PATTERN.fullmatch(line) for line in effective_lines[:-1]):
        return [f"step {STEP_NAME!r} run script writes no {REQUIRED_ANNOTATION!r} annotation"]

    return []


def linux_gate_problems(doc: dict) -> list[str]:
    """Return one text per defect in the reuse job Linux runner gate."""
    job = doc.get("jobs", {}).get("reuse")
    if not isinstance(job, dict):
        return ["no job `reuse`"]
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["job `reuse` has no steps"]

    indices = [
        i
        for i, step in enumerate(steps)
        if isinstance(step, dict) and step.get("name") == STEP_NAME
    ]
    if not indices:
        return [f"no step named {STEP_NAME!r}"]

    idx = indices[0]
    step = steps[idx]

    problems: list[str] = []
    problems.extend(_check_job(job))
    problems.extend(_check_step(step, idx))
    problems.extend(_check_script(step))
    return problems


def _mutate_step_removed(doc: dict) -> None:
    """Planted defect: the gate step is removed from the job."""
    steps = doc["jobs"]["reuse"]["steps"]
    doc["jobs"]["reuse"]["steps"] = [s for s in steps if s.get("name") != STEP_NAME]


def _mutate_step_moved(doc: dict) -> None:
    """Planted defect: the gate step runs after the draft check instead of first."""
    steps = doc["jobs"]["reuse"]["steps"]
    idx = next(i for i, s in enumerate(steps) if s.get("name") == STEP_NAME)
    step = steps.pop(idx)
    draft_idx = next(
        i for i, s in enumerate(steps) if s.get("name") == "Stop on a draft pull request"
    )
    steps.insert(draft_idx + 1, step)


def _mutate_condition(doc: dict) -> None:
    """Planted defect: the step condition is inverted to `==`."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["if"] = "runner.os == 'Linux'"


def _mutate_exit(doc: dict) -> None:
    """Planted defect: the refusal ends with `exit 0`."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["run"] = step["run"].replace("exit 1", "exit 0")


def _mutate_annotation(doc: dict) -> None:
    """Planted defect: the `::error` annotation line is removed."""
    step = doc["jobs"]["reuse"]["steps"][0]
    lines = [line for line in step["run"].splitlines() if REQUIRED_ANNOTATION not in line]
    step["run"] = "\n".join(lines)


def _mutate_shell_bash(doc: dict) -> None:
    """Planted defect: the gate step runs in bash instead of pwsh."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["shell"] = "bash"


def _mutate_job_condition(doc: dict) -> None:
    """Planted defect: the job carries `if: false`."""
    doc["jobs"]["reuse"]["if"] = False


def _mutate_job_continue_on_error(doc: dict) -> None:
    """Planted defect: the job carries `continue-on-error: true`."""
    doc["jobs"]["reuse"]["continue-on-error"] = True


def _mutate_step_continue_on_error(doc: dict) -> None:
    """Planted defect: the gate step carries `continue-on-error: true`."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["continue-on-error"] = True


def _mutate_early_exit_zero(doc: dict) -> None:
    """Planted defect: an `exit 0` runs before the refusal."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["run"] = "exit 0\n" + step["run"]


def _mutate_annotation_in_comment(doc: dict) -> None:
    """Planted defect: the annotation survives only as a comment."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["run"] = step["run"].replace('Write-Output "::error', '# Write-Output "::error')


def _mutate_annotation_in_branch(doc: dict) -> None:
    """Planted defect: the annotation sits inside a branch that never runs."""
    step = doc["jobs"]["reuse"]["steps"][0]
    lines = []
    for line in step["run"].splitlines():
        if 'Write-Output "::error' in line:
            lines.append('if ($false) { Write-Output "::error x" }')
        else:
            lines.append(line)
    step["run"] = "\n".join(lines)


def _mutate_annotation_after_exit(doc: dict) -> None:
    """Planted defect: the annotation comes after the final exit."""
    step = doc["jobs"]["reuse"]["steps"][0]
    lines = [line for line in step["run"].splitlines() if line.strip()]
    exit_idx = next(i for i, line in enumerate(lines) if line.strip() == "exit 1")
    annotation_idx = next(i for i, line in enumerate(lines) if "Write-Output" in line)
    lines[exit_idx], lines[annotation_idx] = lines[annotation_idx], lines[exit_idx]
    step["run"] = "\n".join(lines)


def _mutate_subexpression(doc: dict) -> None:
    """Planted defect: an assignment value runs a `$(exit 0)` subexpression."""
    step = doc["jobs"]["reuse"]["steps"][0]
    step["run"] = step["run"].replace('$title = "Wrong runner OS"', '$title = "$(exit 0)"')


def evaluate_runner_os_condition(condition: str, runner_os: str) -> bool:
    """Evaluate condition of the single form `runner.os != '<OS>'`."""
    expr = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    match = RUNNER_OS_PATTERN.fullmatch(expr)
    if not match:
        raise ValueError(f"unsupported condition: {condition!r}")
    target_os = match.group(1)
    return runner_os != target_os


def extract_readme_workflow_row(readme_text: str, workflow_rel_path: str) -> str:
    """Find the table row for a workflow file in the README.md workflows table."""
    target = f"| `{workflow_rel_path}` |"
    for line in readme_text.splitlines():
        if line.startswith(target):
            return line
    raise ValueError(f"workflow {workflow_rel_path!r} not found in README table")


def readme_row_runner_problems(row: str) -> list[str]:
    """Flag missing Linux runner specification in README row."""
    if README_RUNNER_SUBSTRING not in row:
        return [f"row does not contain {README_RUNNER_SUBSTRING!r}"]
    return []


def test_licence_workflow_linux_gate_passes() -> None:
    """Positive test: the licence workflow reuse job passes the Linux gate checks."""
    doc = ciwf.load(ROOT / LICENCE_WORKFLOW)
    assert linux_gate_problems(doc) == []


PLANTED_DEFECTS = [
    pytest.param(_mutate_step_removed, "no step named", id="step-removed"),
    pytest.param(_mutate_step_moved, "not the first step", id="step-moved-after-draft-stop"),
    pytest.param(_mutate_condition, "not exactly", id="condition-inverted"),
    pytest.param(_mutate_exit, "nonzero exit", id="exit-zero"),
    pytest.param(_mutate_annotation, "::error", id="annotation-missing"),
    pytest.param(_mutate_shell_bash, "shell is not 'pwsh'", id="shell-bash"),
    pytest.param(_mutate_job_condition, "job `reuse` has a condition", id="job-condition"),
    pytest.param(_mutate_job_continue_on_error, "continue-on-error", id="job-continue-on-error"),
    pytest.param(_mutate_step_continue_on_error, "continue-on-error", id="step-continue-on-error"),
    pytest.param(_mutate_early_exit_zero, "statement outside the allow-list", id="early-exit-zero"),
    pytest.param(_mutate_annotation_in_comment, "::error", id="annotation-in-comment"),
    pytest.param(
        _mutate_annotation_in_branch, "statement outside the allow-list", id="annotation-in-branch"
    ),
    pytest.param(_mutate_annotation_after_exit, "nonzero exit", id="annotation-after-exit"),
    pytest.param(_mutate_subexpression, "statement outside the allow-list", id="subexpression"),
]


@pytest.mark.parametrize(("mutate", "expected_fragment"), PLANTED_DEFECTS)
def test_planted_defects_each_yield_one_problem(mutate, expected_fragment: str) -> None:
    """Negative tests: each planted defect yields exactly one problem naming that defect."""
    doc = copy.deepcopy(ciwf.load(ROOT / LICENCE_WORKFLOW))
    mutate(doc)
    problems = linux_gate_problems(doc)
    assert len(problems) == 1
    assert expected_fragment in problems[0]


@pytest.mark.parametrize(
    ("runner_os", "expected_runs"),
    [
        ("Linux", False),
        ("Windows", True),
        ("macOS", True),
    ],
)
def test_runner_os_boundary_conditions(runner_os: str, expected_runs: bool) -> None:
    """Boundary test: the step runs (refuses) for Windows and macOS only."""
    doc = ciwf.load(ROOT / LICENCE_WORKFLOW)
    step = doc["jobs"]["reuse"]["steps"][0]
    condition = step["if"]
    runs = evaluate_runner_os_condition(condition, runner_os)
    assert runs is expected_runs


@pytest.mark.parametrize(
    "condition",
    [
        "runner.os == 'Linux'",
        "runner.os != 'Linux' || true",
        "runner.os != Linux",
        "runner.os != ''",
    ],
)
def test_evaluator_refuses_other_condition_forms(condition: str) -> None:
    """Negative test: a condition outside `runner.os != '<OS>'` is refused, not guessed."""
    with pytest.raises(ValueError, match="unsupported condition"):
        evaluate_runner_os_condition(condition, "Linux")


def test_readme_documents_linux_runner_label() -> None:
    """README test: workflow table row names runner as Linux label, flagged if missing."""
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    row = extract_readme_workflow_row(readme_text, LICENCE_WORKFLOW)
    assert README_RUNNER_SUBSTRING in row
    assert readme_row_runner_problems(row) == []

    planted_row = row.replace("Linux label", "label")
    problems = readme_row_runner_problems(planted_row)
    assert len(problems) == 1
    assert README_RUNNER_SUBSTRING in problems[0]
