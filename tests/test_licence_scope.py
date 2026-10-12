# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""A licence run narrowed to a folder is named for it and says so.

The required check of a consumer is "<calling job> / REUSE lint". A run whose
`root` is a folder checks that folder only, so it must report another name,
and its summary names what it checked (decision record 0027). The planted case
is a narrowed run, which must not report the required name.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
from expressions import evaluate

LICENCE = ROOT / ".github/workflows/licence.yml"
REQUIRED = "REUSE lint"
GUARD = "Refuse a root that is no plain relative path"
SCOPE = "Name the checked scope"
NARROWED = ["sub/dir", "tests/fixtures/unlicensed", "a", "./"]


def reported(doc: dict, root: str) -> str:
    """The check name the forge reports for the licence job of one call."""
    name = str(doc["jobs"]["reuse"].get("name", "reuse"))
    return str(evaluate(name, {"root": root})) if "${{" in name else name


def default_root(doc: dict) -> str:
    return str(ciwf.triggers(doc)["workflow_call"]["inputs"]["root"]["default"])


@pytest.mark.parametrize("root", NARROWED)
def test_planted_narrowed_run_does_not_report_the_required_check(root):
    name = reported(ciwf.load(LICENCE), root)
    assert name != REQUIRED
    assert name == f"{REQUIRED} ({root})"


def test_run_of_the_whole_repository_reports_the_required_check():
    doc = ciwf.load(LICENCE)
    assert default_root(doc) == "."
    assert reported(doc, default_root(doc)) == REQUIRED


def test_with_a_fixed_name_a_narrowed_run_reports_the_required_check():
    """The defect the scoped name fixes: with one fixed name, a narrowed run passes as the whole."""
    doc = ciwf.load(LICENCE)
    doc["jobs"]["reuse"]["name"] = REQUIRED
    assert reported(doc, NARROWED[0]) == REQUIRED


def scope_step() -> dict:
    steps = ciwf.load(LICENCE)["jobs"]["reuse"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index(GUARD) < names.index(SCOPE), "the scope is named after the root is checked"
    return steps[names.index(SCOPE)]


def summary(root: str, tmp_path) -> str:
    """Run the scope step for one root; what it writes to the run's summary."""
    step = scope_step()
    bash = shutil.which("bash")
    assert bash, "the scope step is a bash step; bash is needed to run it"
    out = tmp_path / "summary.md"
    env = {k: str(v) for k, v in step["env"].items() if "${{" not in str(v)}
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        capture_output=True,
        text=True,
        check=False,
        env={**env, "ROOT": root, "GITHUB_STEP_SUMMARY": str(out)},
        timeout=20,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return out.read_text(encoding="utf-8")


def test_scope_step_is_given_the_root_and_runs_unless_on_a_draft():
    step = scope_step()
    assert step["env"]["ROOT"] == "${{ inputs.root }}"
    assert step["if"] == "github.event.pull_request.draft != true"


def test_summary_of_the_whole_repository(tmp_path):
    text = summary(".", tmp_path)
    assert "whole repository" in text
    assert "not checked" not in text


@pytest.mark.parametrize("root", NARROWED)
def test_summary_of_a_narrowed_run_names_the_folder_and_what_it_left_out(tmp_path, root):
    text = summary(root, tmp_path)
    assert f"`{root}`" in text
    assert "outside it were not checked" in text
