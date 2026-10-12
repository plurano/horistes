# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Calls of the scorecard workflow: a run that analyses nothing is red.

The jobs of the real workflow are played the way the forge plays a call: a
condition and a name are read with the inputs, a job is skipped when a job it
needs did not succeed, and every bash step is run. A step that uses an action
is taken as passing. The planted case is a caller with the default inputs
(decision record 0026).
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
from expressions import evaluate

SCORECARD = ROOT / ".github/workflows/scorecard.yml"
VISIBILITY = "${{ github.event.repository.visibility }}"
INPUT = re.compile(r"^\$\{\{\s*inputs\.(\w+)\s*\}\}$")
DEFAULTS = {"outside_hosts": False, "publish": False}


def _environment(env: dict, inputs: dict, visibility: str) -> dict[str, str]:
    """A step's environment as the forge renders it for one call."""
    out = {}
    for key, value in env.items():
        text = str(value)
        found = INPUT.match(text)
        if text == VISIBILITY:
            text = visibility
        elif found:
            text = str(inputs[found.group(1)]).lower()
        assert "${{" not in text, f"{key}: expression not rendered here"
        out[str(key)] = text
    return out


def _run_job(job: dict, inputs: dict, visibility: str, tmp_path) -> tuple[str, str]:
    """Run the bash steps of one job; its result and what it printed."""
    bash = shutil.which("bash")
    assert bash, "the refusals are bash steps; bash is needed to run them"
    summary = tmp_path / "summary.md"
    for step in job["steps"]:
        if "run" not in step:
            continue
        summary.write_text("", encoding="utf-8")
        env = _environment(step.get("env") or {}, inputs, visibility)
        done = subprocess.run(
            [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
            capture_output=True,
            text=True,
            check=False,
            env={**env, "GITHUB_STEP_SUMMARY": str(summary)},
            timeout=20,
        )
        if done.returncode:
            return "failure", done.stdout
    return "success", ""


def play(doc: dict, inputs: dict, visibility: str, tmp_path) -> dict[str, tuple[str, str]]:
    """The check of every job of one call: name -> (result, printed text)."""
    results: dict[str, str] = {}
    checks: dict[str, tuple[str, str]] = {}
    for job_id, job in doc["jobs"].items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        assert all(name in results for name in needs), f"{job_id} needs a later job"
        check = ("skipped", "")
        if all(results[name] == "success" for name in needs) and (
            "if" not in job or evaluate(str(job["if"]), inputs)
        ):
            check = _run_job(job, inputs, visibility, tmp_path)
        results[job_id] = check[0]
        name = str(job.get("name", job_id))
        name = str(evaluate(name, inputs)) if "${{" in name else name
        assert name not in checks, f"two jobs report the check {name!r}"
        checks[name] = check
    return checks


@pytest.mark.parametrize("visibility", ["public", "private"])
def test_planted_caller_with_the_default_inputs_turns_red(tmp_path, visibility):
    checks = play(ciwf.load(SCORECARD), DEFAULTS, visibility, tmp_path)
    result, printed = checks["Analysis"]
    assert result == "failure"
    assert printed.startswith("::error title=Analysis switched off::The analysis did not run")
    assert checks["Analysis (not started)"] == ("skipped", "")


CALLS = list(itertools.product([False, True], [False, True], ["public", "private", ""]))


@pytest.mark.parametrize(("outside_hosts", "publish", "visibility"), CALLS)
def test_run_is_green_only_when_the_analysis_ran(tmp_path, outside_hosts, publish, visibility):
    inputs = {"outside_hosts": outside_hosts, "publish": publish}
    checks = play(ciwf.load(SCORECARD), inputs, visibility, tmp_path)
    ran = checks.get("Analysis", ("", ""))[0] == "success"
    red = any(result == "failure" for result, _ in checks.values())
    assert ran != red, checks
    assert ran == (outside_hosts and visibility == "public"), checks


def test_without_the_refusal_job_a_default_call_reads_green(tmp_path):
    """The defect the refusal job fixes: with it removed, the player shows a green run."""
    doc = ciwf.load(SCORECARD)
    del doc["jobs"]["switched_off"]
    checks = play(doc, DEFAULTS, "public", tmp_path)
    assert all(result != "failure" for result, _ in checks.values()), checks
    assert checks["Analysis (not started)"][0] == "skipped"
