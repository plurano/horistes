# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for tools/runners.py: verify runner-minute measurements against forge runs."""

from __future__ import annotations

import json
import sys

import pytest
from conftest import ROOT, Tree

sys.path.insert(0, str(ROOT / "tools"))
import runners

REPO = "fixture/repo"
WORKFLOW_FILE = ".github/workflows/ci.yml"
WORKFLOW_YAML = """\
name: CI
on:
  push:
    branches: [main]
jobs:
  test:
    name: Test
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    steps:
      - run: echo test
"""
GENEROUS_YAML = """\
name: Fast
on: push
jobs:
  quick:
    name: Quick
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - run: echo fast
"""
NO_TIMEOUT_YAML = """\
name: Untimed
on: push
jobs:
  untimed:
    name: Untimed
    runs-on: ubuntu-24.04
    steps:
      - run: echo untimed
"""
ZERO_TIMEOUT_YAML = """\
name: Zero
on: push
jobs:
  zero:
    name: Zero
    runs-on: ubuntu-24.04
    timeout-minutes: 0
    steps:
      - run: echo zero
"""


def _forge_run(
    run_id: int,
    started_at: str,
    updated_at: str,
    status: str = "completed",
    conclusion: str = "success",
) -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "status": status,
        "conclusion": conclusion,
        "run_started_at": started_at,
        "updated_at": updated_at,
    }


def make_answers(workflow_name: str, runs: list[dict]) -> dict[str, object]:
    path = f"repos/{REPO}/actions/workflows/{workflow_name}/runs?per_page=100&page=1"
    return {path: {"total_count": len(runs), "workflow_runs": runs}}


def run_tool(
    tree: Tree,
    *args: str,
    answers: dict[str, object] | None = None,
    workflow_yaml: str = WORKFLOW_YAML,
    workflow_file: str = WORKFLOW_FILE,
):
    tree.write({workflow_file: workflow_yaml})
    env = {**tree.forge(answers or {}), "GITHUB_REPOSITORY": REPO}
    return tree.tool("runners.py", *args, env=env)


# --- Unit Tests: Reading Workflow YAML ---


def test_read_timeout_minutes_from_repository_workflows():
    """Verify timeout-minutes values read from real repository workflows."""
    workflows = runners.load_workflow_timeouts(ROOT)

    ci = workflows[[".github", "workflows", "ci.yml"][-1]]
    assert ci["timeout_minutes"] == 25
    assert ci["job_timeouts"]["verify"] == 25
    assert ci["job_timeouts"]["floor"] == 10
    assert ci["job_timeouts"]["fuzz"] == 10

    licence = workflows[[".github", "workflows", "licence.yml"][-1]]
    assert licence["timeout_minutes"] == 10
    assert licence["job_timeouts"]["reuse"] == 10

    pins = workflows[[".github", "workflows", "pins.yml"][-1]]
    assert pins["timeout_minutes"] == 10
    assert pins["job_timeouts"]["pins"] == 10

    scorecard = workflows[[".github", "workflows", "scorecard.yml"][-1]]
    assert scorecard["timeout_minutes"] == 15
    assert scorecard["job_timeouts"]["visibility"] == 5
    assert scorecard["job_timeouts"]["switched_off"] == 5
    assert scorecard["job_timeouts"]["analysis"] == 15


def test_read_job_timeouts_from_synthetic_workflow():
    doc = {
        "jobs": {
            "build": {"timeout-minutes": 20},
            "lint": {"timeout-minutes": 5},
            "deploy": {"steps": []},
        }
    }
    timeouts = runners.read_job_timeouts(doc)
    assert timeouts == {"build": 20, "lint": 5}
    assert runners.workflow_timeout(doc) == 20


# --- Unit Tests: Duration and Percentiles ---


def test_extract_duration():
    run = _forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:03:30Z")
    assert runners.extract_duration(run) == 210.0

    incomplete = _forge_run(2, "2026-11-08T01:00:00Z", "2026-11-08T01:03:30Z", status="in_progress")
    assert runners.extract_duration(incomplete) is None

    direct_seconds = {"seconds": 45.5}
    assert runners.extract_duration(direct_seconds) == 45.5


def test_calculate_percentile_95():
    assert runners.calculate_percentile_95([]) == 0.0
    assert runners.calculate_percentile_95([100.0]) == 100.0
    data = [float(i) for i in range(1, 101)]
    p95 = runners.calculate_percentile_95(data)
    assert p95 == 95.05

    skewed = [10.0, 20.0, 30.0, 40.0, 500.0]
    p95_skewed = runners.calculate_percentile_95(skewed)
    assert 407.0 <= p95_skewed <= 408.5


# --- Unit Tests: Workflow Evaluation ---


def test_evaluate_workflow_zero_timeout_warns():
    """Planted defect: timeout 0 is read as a 0 s budget and fails."""
    ev = runners.evaluate_workflow(
        {"name": "X", "path": ".github/workflows/x.yml", "timeout_minutes": 0},
        [540.0],
    )
    assert ev.severity == "warn"
    assert ev.finding == "no timeout-minutes declared"


# --- Integration Tests with Mocked Forge Responses ---


def test_run_within_bounds_passes(tree: Tree):
    """A run well within bounds (3m on a 10m timeout) passes (exit 0)."""
    # 180s is 30% of 600s (below 80%), and 600s <= 5 * 180s (not generous)
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:03:00Z")]
    answers = make_answers("ci.yml", runs)
    done = run_tool(tree, "check", answers=answers)

    assert done.returncode == 0, done.stderr
    assert "PASS: ci.yml: timeout 10m, measured p95 180.0s, max 180.0s" in done.stdout
    assert done.stderr == ""


def test_run_approaching_timeout_fails(tree: Tree):
    """Planted defect: a run exceeding 80% of timeout-minutes fails (exit 1)."""
    # 540s is 90% of 600s (> 80% fraction threshold)
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:09:00Z")]
    answers = make_answers("ci.yml", runs)
    done = run_tool(tree, "check", answers=answers)

    assert done.returncode == 1, done.stdout
    assert "FAIL: ci.yml: measured duration exceeds 80%" in done.stderr
    assert "max 540.0s, p95 540.0s" in done.stderr


def test_generous_timeout_warns(tree: Tree):
    """Planted defect: timeout > 5x measured p95 emits warning."""
    # 30s run on a 15m (900s) timeout: 900s > 5 * 30s = 150s
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:00:30Z")]
    answers = make_answers("fast.yml", runs)
    done = run_tool(
        tree,
        "check",
        answers=answers,
        workflow_yaml=GENEROUS_YAML,
        workflow_file=".github/workflows/fast.yml",
    )

    # By default, warning does not fail (exit 0)
    assert done.returncode == 0, done.stderr
    assert "WARN: fast.yml: timeout (15m) is more than 5x" in done.stdout
    assert "wastefully generous" in done.stdout

    # With --strict, warning causes failure (exit 1)
    done_strict = run_tool(
        tree,
        "check",
        "--strict",
        answers=answers,
        workflow_yaml=GENEROUS_YAML,
        workflow_file=".github/workflows/fast.yml",
    )
    assert done_strict.returncode == 1, done_strict.stdout


@pytest.mark.parametrize(
    ("workflow_yaml", "workflow_file"),
    [
        pytest.param(NO_TIMEOUT_YAML, "untimed.yml", id="key-absent"),
        pytest.param(ZERO_TIMEOUT_YAML, "zero.yml", id="key-zero"),
    ],
)
def test_untimed_workflow_warns_not_fails(tree: Tree, workflow_yaml: str, workflow_file: str):
    """Planted defect: a job with no usable timeout-minutes gives a false FAIL.

    Regression for the CodeRabbit item on PR 127: with no timeout the
    budget is 0 s, so any measured run would exceed it. The check must
    warn "no timeout-minutes declared" instead, and --strict turns that
    warning into exit 1.
    """
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:09:00Z")]
    answers = make_answers(workflow_file, runs)
    path = f".github/workflows/{workflow_file}"
    done = run_tool(
        tree,
        "check",
        answers=answers,
        workflow_yaml=workflow_yaml,
        workflow_file=path,
    )

    assert done.returncode == 0, done.stderr
    assert f"WARN: {workflow_file}: no timeout-minutes declared" in done.stdout
    assert "FAIL:" not in done.stdout
    assert "FAIL:" not in done.stderr

    done_strict = run_tool(
        tree,
        "check",
        "--strict",
        answers=answers,
        workflow_yaml=workflow_yaml,
        workflow_file=path,
    )
    assert done_strict.returncode == 1, done_strict.stdout


# --- Exit Code Tests ---


def test_exit_code_zero_on_pass(tree: Tree):
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:02:30Z")]
    answers = make_answers("ci.yml", runs)
    done = run_tool(tree, "check", answers=answers)
    assert done.returncode == 0


def test_exit_code_one_on_finding(tree: Tree):
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:09:30Z")]
    answers = make_answers("ci.yml", runs)
    done = run_tool(tree, "check", answers=answers)
    assert done.returncode == 1


def test_exit_code_three_when_no_runs_found(tree: Tree):
    answers = make_answers("ci.yml", [])
    done = run_tool(tree, "check", answers=answers)
    assert done.returncode == 3
    assert "ERROR: could not measure: no runs found" in done.stderr


def test_exit_code_three_when_forge_fails(tree: Tree):
    # Missing answer in fake forge results in HTTP 404
    done = run_tool(tree, "check", answers={})
    assert done.returncode == 3
    assert "ERROR: could not read workflow runs for ci.yml" in done.stderr


# --- Output Shape and Options Tests ---


def test_json_output(tree: Tree):
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:03:00Z")]
    answers = make_answers("ci.yml", runs)
    done = run_tool(tree, "check", "--json", answers=answers)

    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    item = data[0]
    assert item["workflow"] == "ci.yml"
    assert item["name"] == "CI"
    assert item["timeout_minutes"] == 10
    assert item["timeout-minutes"] == 10
    assert item["measured_p95_seconds"] == 180.0
    assert item["measured_max_seconds"] == 180.0
    assert item["finding"] is None


def test_workflow_filter(tree: Tree):
    runs = [_forge_run(1, "2026-11-08T01:00:00Z", "2026-11-08T01:03:00Z")]
    answers = make_answers("ci.yml", runs)
    tree.write({".github/workflows/other.yml": GENEROUS_YAML})
    done = run_tool(tree, "check", "--workflow", "ci.yml", answers=answers)
    assert done.returncode == 0, done.stderr
    assert "ci.yml" in done.stdout
    assert "other.yml" not in done.stdout


def test_runners_list_command(tree: Tree):
    done = run_tool(tree, "list")
    assert done.returncode == 0
    assert "ci.yml: timeout-minutes: 10" in done.stdout
    assert "Total: 1 workflow(s)" in done.stdout
