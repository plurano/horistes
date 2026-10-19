# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Draft pull request stop: the step halts runs on draft PRs and passes otherwise.

The run scripts and conditions are extracted from workflows with tools/ciwf.py
and executed with bash in a temporary directory, verifying step summaries and
error annotations.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

DRAFT_STEP = "Stop on a draft pull request"
WORKFLOWS = [
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".github/workflows/image-age.yml",
    ".github/workflows/licence.yml",
    ".github/workflows/pins.yml",
]


def workflows_with_draft_step(root: Path = ROOT) -> list[str]:
    """Every workflow file declaring the draft stop step."""
    return sorted(
        {
            path
            for path, _, doc in ciwf.files(root)
            for job in doc.get("jobs", {}).values()
            if isinstance(job, dict)
            for step in job.get("steps", [])
            if isinstance(step, dict) and step.get("name") == DRAFT_STEP
        }
    )


def extract_step(rel_path: str) -> dict:
    """The draft stop step dictionary from the workflow."""
    for job in ciwf.load(ROOT / rel_path).get("jobs", {}).values():
        for step in (job.get("steps") or []) if isinstance(job, dict) else []:
            if isinstance(step, dict) and step.get("name") == DRAFT_STEP:
                return step
    raise ValueError(f"no draft stop step in {rel_path}")


def _condition_holds(condition: str, draft: object) -> bool:
    """Whether the workflow condition evaluates to true for the given draft state."""
    text = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    if text == "github.event.pull_request.draft == true":
        return draft is True or draft == "true"
    if text == "github.event.pull_request.draft != true":
        return not (draft is True or draft == "true")
    return text in ("true", "always()")


def run_step(step: dict, draft: object, tmp_path: Path) -> tuple[subprocess.CompletedProcess, str]:
    """Run the step script in bash the way GitHub Actions evaluates and executes it."""
    bash = shutil.which("bash")
    assert bash, "bash is needed to run the step"
    assert "${{" not in step.get("run", ""), "run script must not contain unexpanded expressions"
    runs = _condition_holds(step.get("if", ""), draft) if "if" in step else True
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {**os.environ, **step.get("env", {}), "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"] if runs else "exit 0"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    return done, summary.read_text(encoding="utf-8")


def test_every_workflow_with_the_step_is_covered(tmp_path: Path) -> None:
    """The declared workflow list covers every workflow with the draft stop step."""
    assert workflows_with_draft_step() == WORKFLOWS

    planted = tmp_path / ".github/workflows"
    planted.mkdir(parents=True)
    for rel_path in WORKFLOWS[:-1]:
        shutil.copy(ROOT / rel_path, planted)
    shutil.copy(ROOT / WORKFLOWS[-1], planted / "new.yml")
    assert workflows_with_draft_step(tmp_path) != WORKFLOWS


@pytest.mark.parametrize("draft", [True, "true"])
@pytest.mark.parametrize("rel_path", WORKFLOWS)
def test_draft_stop_halts_on_a_draft_pull_request(
    rel_path: str, draft: object, tmp_path: Path
) -> None:
    """A draft pull request triggers the stop step, exiting non-zero with an error annotation."""
    step = extract_step(rel_path)
    done, summary = run_step(step, draft, tmp_path)
    assert done.returncode == 1, done.stderr
    assert "::error title=Gate not run on a draft::" in done.stdout
    assert summary.strip() == step["env"]["MESSAGE"].strip()

    mutated = {**step, "run": step["run"].replace("exit 1", "exit 0")}
    planted_done, _ = run_step(mutated, draft, tmp_path)
    assert planted_done.returncode == 0


@pytest.mark.parametrize("draft", [False, "false"])
@pytest.mark.parametrize("rel_path", WORKFLOWS)
def test_draft_stop_passes_on_a_ready_pull_request(
    rel_path: str, draft: object, tmp_path: Path
) -> None:
    """A ready pull request skips the stop step, exiting zero without annotations."""
    step = extract_step(rel_path)
    done, summary = run_step(step, draft, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "::error" not in done.stdout and "::notice" not in done.stdout
    assert "::error" not in done.stderr and "::notice" not in done.stderr
    assert summary == ""

    mutated = {**step, "if": "github.event.pull_request.draft != true"}
    planted_done, planted_summary = run_step(mutated, draft, tmp_path)
    assert planted_done.returncode == 1
    assert "::error" in planted_done.stdout
    assert planted_summary.strip() == step["env"]["MESSAGE"].strip()


@pytest.mark.parametrize("draft", ["", None])
@pytest.mark.parametrize("rel_path", WORKFLOWS)
def test_draft_stop_passes_when_draft_state_is_empty_or_missing(
    rel_path: str, draft: object, tmp_path: Path
) -> None:
    """An empty or missing draft state skips the stop step, exiting zero without annotations."""
    step = extract_step(rel_path)
    done, summary = run_step(step, draft, tmp_path)
    assert done.returncode == 0, done.stderr
    assert "::error" not in done.stdout and "::notice" not in done.stdout
    assert "::error" not in done.stderr and "::notice" not in done.stderr
    assert summary == ""

    mutated = {**step, "if": "true"}
    planted_done, planted_summary = run_step(mutated, draft, tmp_path)
    assert planted_done.returncode == 1
    assert "::error" in planted_done.stdout
    assert planted_summary.strip() == step["env"]["MESSAGE"].strip()
