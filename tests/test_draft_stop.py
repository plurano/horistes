# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Run every copy of the draft stop step in every job: halt on drafts, pass otherwise.

The run scripts and conditions are extracted from workflows with tools/ciwf.py
and executed with bash in a temporary directory, verifying step summaries and
error annotations.
"""

from __future__ import annotations

import json
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
# Maximum number of workflow and composite action files inspected per root.
MAX_WORKFLOW_FILES = 64
# Maximum number of jobs inspected per file.
MAX_JOBS = 32
# Maximum number of steps inspected per job.
MAX_STEPS = 64
COPIES = [
    (".github/workflows/ci.yml", "floor"),
    (".github/workflows/ci.yml", "fuzz"),
    (".github/workflows/ci.yml", "verify"),
    (".github/workflows/codeql.yml", "analyze"),
    (".github/workflows/hook-copy.yml", "check"),
    (".github/workflows/image-age.yml", "image-age"),
    (".github/workflows/licence.yml", "reuse"),
    (".github/workflows/pins.yml", "pins"),
    (".github/workflows/secret-scan.yml", "scan"),
    (".github/workflows/workflow-check.yml", "check"),
]


def _draft_steps(job: object, where: str) -> list[dict]:
    """Every draft stop step in a job, preserving duplicate copies."""
    if not isinstance(job, dict):
        return []
    steps = job.get("steps")
    if not isinstance(steps, list):
        return []
    if len(steps) > MAX_STEPS:
        raise ValueError(f"{where} has {len(steps)} steps, at most {MAX_STEPS} supported")
    return [
        step
        for step in steps[:MAX_STEPS]
        if isinstance(step, dict) and step.get("name") == DRAFT_STEP
    ]


def draft_copies(root: Path = ROOT) -> list[tuple[str, str]]:
    """Every path and job declaring a draft stop step, including duplicates."""
    docs = ciwf.files(root)
    if len(docs) > MAX_WORKFLOW_FILES:
        raise ValueError(
            f"{root} has {len(docs)} workflow/action files, at most {MAX_WORKFLOW_FILES} supported"
        )
    copies = []
    for path, _, doc in docs[:MAX_WORKFLOW_FILES]:
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            continue
        if len(jobs) > MAX_JOBS:
            raise ValueError(f"{path} has {len(jobs)} jobs, at most {MAX_JOBS} supported")
        for job_id, job in list(jobs.items())[:MAX_JOBS]:
            copies.extend((path, job_id) for _ in _draft_steps(job, f"{path} job {job_id}"))
    return sorted(copies)


def extract_step(rel_path: str, job_id: str, root: Path = ROOT) -> dict:
    """The named job's draft stop step; name the path and job if absent."""
    try:
        job = ciwf.load(root / rel_path)["jobs"][job_id]
    except KeyError as err:
        raise ValueError(f"no job {job_id} in {rel_path}") from err
    steps = _draft_steps(job, f"{rel_path} job {job_id}")
    if not steps:
        raise ValueError(f"no draft stop step in {rel_path} job {job_id}")
    return steps[0]


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


def test_every_draft_copy_is_covered(tmp_path: Path) -> None:
    """The declared copies catch renamed workflows and extra jobs in a covered file."""
    assert draft_copies() == COPIES

    planted = tmp_path / ".github/workflows"
    planted.mkdir(parents=True)
    workflows = list(dict.fromkeys(path for path, _ in COPIES))
    for rel_path in workflows[:-1]:
        shutil.copy(ROOT / rel_path, planted)
    shutil.copy(ROOT / workflows[-1], planted / "new.yml")
    renamed = ".github/workflows/new.yml"
    expected = sorted((renamed if path == workflows[-1] else path, job) for path, job in COPIES)
    assert draft_copies(tmp_path) == expected

    extra_root = tmp_path / "extra-job"
    rel_path = ".github/workflows/ci.yml"
    copied = extra_root / rel_path
    copied.parent.mkdir(parents=True)
    shutil.copy(ROOT / rel_path, copied)
    doc = ciwf.load(copied)
    doc["jobs"]["extra-floor"] = doc["jobs"]["floor"]
    copied.write_text(json.dumps(doc), encoding="utf-8")
    in_file = [copy for copy in COPIES if copy[0] == rel_path]
    assert draft_copies(extra_root) == sorted([*in_file, (rel_path, "extra-floor")])


def test_draft_copies_preserves_duplicate_steps(tmp_path: Path) -> None:
    """Two draft stop steps in one job yield two entries and fail single-copy coverage."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {"a": {"steps": [step, step]}}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    assert draft_copies(tmp_path) == [(rel_path, "a"), (rel_path, "a")]
    assert draft_copies(tmp_path) != [(rel_path, "a")]


@pytest.mark.parametrize("jobs", [None, [], "jobs"], ids=["null", "list", "text"])
def test_draft_copies_skips_jobs_that_are_not_a_mapping(tmp_path: Path, jobs: object) -> None:
    """A non-mapping jobs value contributes no draft stop copies."""
    planted = tmp_path / ".github/workflows/ci.yml"
    planted.parent.mkdir(parents=True)
    planted.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    assert draft_copies(tmp_path) == []


@pytest.mark.parametrize("steps", [None, {"a": 1}, "steps"], ids=["null", "mapping", "text"])
def test_draft_copies_skips_steps_that_are_not_a_list(tmp_path: Path, steps: object) -> None:
    """A non-list steps value contributes no draft stop copies."""
    planted = tmp_path / ".github/workflows/ci.yml"
    planted.parent.mkdir(parents=True)
    planted.write_text(json.dumps({"jobs": {"verify": {"steps": steps}}}), encoding="utf-8")
    assert draft_copies(tmp_path) == []


def test_draft_copies_accepts_maximum_steps(tmp_path: Path) -> None:
    """Exactly MAX_STEPS draft stops in one job preserve every copy."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {"verify": {"steps": [step] * MAX_STEPS}}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    assert draft_copies(tmp_path) == [(rel_path, "verify")] * MAX_STEPS


def test_draft_copies_rejects_too_many_steps(tmp_path: Path) -> None:
    """An over-limit step list raises ValueError naming its path, job, count and bound."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {"verify": {"steps": [step] * (MAX_STEPS + 1)}}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        draft_copies(tmp_path)
    assert rel_path in str(caught.value)
    assert "job verify" in str(caught.value)
    assert f"has {MAX_STEPS + 1} steps, at most {MAX_STEPS}" in str(caught.value)


def test_draft_copies_accepts_maximum_jobs(tmp_path: Path) -> None:
    """Exactly MAX_JOBS jobs in one file preserve every draft stop copy."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {f"job{i}": {"steps": [step]} for i in range(MAX_JOBS)}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    assert len(draft_copies(tmp_path)) == MAX_JOBS


def test_draft_copies_rejects_too_many_jobs(tmp_path: Path) -> None:
    """An over-limit jobs mapping raises ValueError naming its path, count and bound."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {f"job{i}": {"steps": [step]} for i in range(MAX_JOBS + 1)}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        draft_copies(tmp_path)
    assert rel_path in str(caught.value)
    assert f"has {MAX_JOBS + 1} jobs, at most {MAX_JOBS}" in str(caught.value)


def test_draft_copies_accepts_maximum_files(tmp_path: Path) -> None:
    """Exactly MAX_WORKFLOW_FILES files preserve every draft stop copy."""
    step = extract_step(".github/workflows/ci.yml", "verify")
    planted = tmp_path / ".github/workflows"
    planted.mkdir(parents=True)
    doc = {"jobs": {"verify": {"steps": [step]}}}
    for i in range(MAX_WORKFLOW_FILES):
        (planted / f"w{i}.yml").write_text(json.dumps(doc), encoding="utf-8")
    assert len(draft_copies(tmp_path)) == MAX_WORKFLOW_FILES


def test_draft_copies_rejects_too_many_files(tmp_path: Path) -> None:
    """An over-limit file list raises ValueError naming its root, count and bound."""
    step = extract_step(".github/workflows/ci.yml", "verify")
    planted = tmp_path / ".github/workflows"
    planted.mkdir(parents=True)
    doc = {"jobs": {"verify": {"steps": [step]}}}
    for i in range(MAX_WORKFLOW_FILES + 1):
        (planted / f"w{i}.yml").write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        draft_copies(tmp_path)
    assert str(tmp_path) in str(caught.value)
    assert (
        f"has {MAX_WORKFLOW_FILES + 1} workflow/action files, at most {MAX_WORKFLOW_FILES}"
        in str(caught.value)
    )


def test_planted_second_job_copy_is_run(tmp_path: Path) -> None:
    """Job-specific extraction exposes a second job mutated to pass on a draft."""
    rel_path = ".github/workflows/ci.yml"
    step = extract_step(rel_path, "verify")
    mutated = {**step, "run": step["run"].replace("exit 1", "exit 0")}
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    doc = {"jobs": {"a": {"steps": [step]}, "b": {"steps": [mutated]}}}
    planted.write_text(json.dumps(doc), encoding="utf-8")
    assert draft_copies(tmp_path) == [(rel_path, "a"), (rel_path, "b")]
    first = extract_step(rel_path, "a", root=tmp_path)
    second = extract_step(rel_path, "b", root=tmp_path)
    assert second == mutated
    assert second["run"] != first["run"]
    second_done, _ = run_step(second, True, tmp_path)
    first_done, _ = run_step(first, True, tmp_path)
    assert second_done.returncode == 0
    assert first_done.returncode == 1


def test_extract_step_names_missing_job() -> None:
    """An absent job raises ValueError naming both the workflow and the job."""
    rel_path = ".github/workflows/ci.yml"
    job_id = "no-such-job"
    with pytest.raises(ValueError) as caught:
        extract_step(rel_path, job_id)
    assert rel_path in str(caught.value)
    assert job_id in str(caught.value)


def test_extract_step_names_missing_step(tmp_path: Path) -> None:
    """A job without a draft stop step raises ValueError naming its path and job."""
    rel_path = ".github/workflows/ci.yml"
    job_id = "no-stop"
    planted = tmp_path / rel_path
    planted.parent.mkdir(parents=True)
    planted.write_text(json.dumps({"jobs": {job_id: {"steps": []}}}), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        extract_step(rel_path, job_id, root=tmp_path)
    assert rel_path in str(caught.value)
    assert job_id in str(caught.value)


@pytest.mark.parametrize("draft", [True, "true"])
@pytest.mark.parametrize(
    ("rel_path", "job_id"),
    COPIES,
    ids=[f"{Path(path).name}:{job_id}" for path, job_id in COPIES],
)
def test_draft_stop_halts_on_a_draft_pull_request(
    rel_path: str, job_id: str, draft: object, tmp_path: Path
) -> None:
    """A draft pull request triggers the stop step, exiting non-zero with an error annotation."""
    step = extract_step(rel_path, job_id)
    done, summary = run_step(step, draft, tmp_path)
    assert done.returncode == 1, done.stderr
    assert "::error title=Gate not run on a draft::" in done.stdout
    assert step["env"]["TITLE"] == "Gate not run on a draft"
    assert (
        f"::error title={step['env']['TITLE']}::{step['env']['MESSAGE']}"
        in done.stdout.splitlines()
    )
    assert summary.strip() == step["env"]["MESSAGE"].strip()

    mutated = {**step, "run": step["run"].replace("exit 1", "exit 0")}
    planted_done, _ = run_step(mutated, draft, tmp_path)
    assert planted_done.returncode == 0


@pytest.mark.parametrize("draft", [False, "false"])
@pytest.mark.parametrize(
    ("rel_path", "job_id"),
    COPIES,
    ids=[f"{Path(path).name}:{job_id}" for path, job_id in COPIES],
)
def test_draft_stop_passes_on_a_ready_pull_request(
    rel_path: str, job_id: str, draft: object, tmp_path: Path
) -> None:
    """A ready pull request skips the stop step, exiting zero without annotations."""
    step = extract_step(rel_path, job_id)
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
@pytest.mark.parametrize(
    ("rel_path", "job_id"),
    COPIES,
    ids=[f"{Path(path).name}:{job_id}" for path, job_id in COPIES],
)
def test_draft_stop_passes_when_draft_state_is_empty_or_missing(
    rel_path: str, job_id: str, draft: object, tmp_path: Path
) -> None:
    """An empty or missing draft state skips the stop step, exiting zero without annotations."""
    step = extract_step(rel_path, job_id)
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
