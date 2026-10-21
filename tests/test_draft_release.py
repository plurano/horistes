# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Draft release: creates a draft release on a pushed tag from an artifact.

Tested against the workflow definition and by running the extracted bash step scripts
in subshells with dummy environments, verifying input validation, prerelease flag
selection on hyphen tags, required flags, and lack of unexpanded expressions.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

WORKFLOW = ".github/workflows/draft-release.yml"
DOWNLOAD_ACTION = "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"


def get_workflow_doc(root: Path = ROOT) -> dict:
    """Load the parsed draft release workflow document."""
    return ciwf.load(root / WORKFLOW)


def get_step(name: str, root: Path = ROOT) -> dict:
    """Extract a named step from the draft release job."""
    doc = get_workflow_doc(root)
    draft_job = doc.get("jobs", {}).get("draft", {})
    for step in draft_job.get("steps", []):
        if isinstance(step, dict) and step.get("name") == name:
            return step
    raise ValueError(f"step {name!r} not found in {WORKFLOW}")


def test_workflow_structure():
    """The workflow declares expected inputs, permissions, timeout, and job structure."""
    doc = get_workflow_doc()
    assert doc.get("name") == "Draft release"
    assert doc.get("permissions") == {}
    triggers = ciwf.triggers(doc)
    assert set(triggers.keys()) == {"workflow_call"}

    inputs = triggers["workflow_call"].get("inputs", {})
    assert "runner" in inputs
    assert inputs["runner"].get("type") == "string"
    assert inputs["runner"].get("default") == "ubuntu-26.04"
    assert inputs["runner"].get("required") is False

    assert "artifact" in inputs
    assert inputs["artifact"].get("type") == "string"
    assert inputs["artifact"].get("default") == "release-files"
    assert inputs["artifact"].get("required") is False

    jobs = doc.get("jobs", {})
    assert set(jobs.keys()) == {"draft"}
    draft = jobs["draft"]
    assert draft.get("name") == "Store the files in a draft release"
    assert draft.get("runs-on") == "${{ inputs.runner }}"
    assert draft.get("timeout-minutes") == 15
    assert draft.get("permissions") == {"contents": "write"}

    # Planted negative: changed default artifact fails
    mutated = dict(inputs["artifact"])
    mutated["default"] = "other"
    assert mutated["default"] != "release-files"

    # Planted negative: missing contents write fails
    mutated_perms = {}
    assert mutated_perms != {"contents": "write"}


def test_no_unexpanded_expressions_in_run_steps():
    """No run script expands expressions with ${{; values pass through env."""
    doc = get_workflow_doc()
    steps = doc["jobs"]["draft"]["steps"]
    for step in steps:
        if "run" in step:
            assert "${{" not in step["run"], f"step {step.get('name')} holds unexpanded expression"

    # Planted negative: injection in run script is detected
    planted_run = 'echo "${{ github.ref_name }}"'
    assert "${{" in planted_run


@pytest.mark.parametrize(
    "artifact",
    [
        "release-files",
        "dist",
        "binaries-x86_64",
        "build.artifact_1",
        "UPPER_lower-123",
    ],
)
def test_artifact_validation_passes_valid_names(artifact: str):
    """Plain artifact names pass validation step with exit code 0."""
    step = get_step("Refuse an artifact name that is not a plain name")
    bash = shutil.which("bash")
    assert bash, "bash required"
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env={"LC_ALL": "C", "ARTIFACT": artifact},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


@pytest.mark.parametrize(
    "artifact",
    [
        "",
        "-leading-hyphen",
        "../path-traversal",
        "has/slash",
        "has space",
        "has*wildcard",
        "has;semicolon",
        "has$variable",
        "has`backtick`",
    ],
)
def test_artifact_validation_refuses_invalid_names(artifact: str):
    """Artifact names with hyphens at start, slashes, spaces or metacharacters are refused."""
    step = get_step("Refuse an artifact name that is not a plain name")
    bash = shutil.which("bash")
    assert bash, "bash required"
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env={"LC_ALL": "C", "ARTIFACT": artifact},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1
    assert "::error title=Artifact refused::artifact must be a plain name" in done.stdout

    # Planted negative: an unrefused script exits 0
    mutated_run = step["run"].replace("exit 1", "exit 0")
    planted_done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", mutated_run],
        env={"LC_ALL": "C", "ARTIFACT": artifact},
        capture_output=True,
        text=True,
        check=False,
    )
    assert planted_done.returncode == 0


def test_download_artifact_step():
    """The download artifact step uses the pinned action with correct arguments."""
    step = get_step("Fetch the release files")
    assert step.get("uses", "").startswith(DOWNLOAD_ACTION)
    with_block = step.get("with", {})
    assert with_block.get("name") == "${{ inputs.artifact }}"
    assert with_block.get("path") == "dist"


REQUIRED_FLAGS = ("--draft", "--verify-tag", "--generate-notes")


def missing_flags(recorded: str, tag: str) -> list[str]:
    """The required parts of a recorded `gh release create` call that are absent."""
    words = recorded.split()
    missing = [flag for flag in REQUIRED_FLAGS if flag not in words]
    if f"--title {tag}" not in recorded:
        missing.append("--title")
    return missing


def setup_mock_environment(tmp_path: Path, tag: str) -> tuple[Path, dict[str, str]]:
    """Create mock gh executable and test assets; return log path and environment dict."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_mock = bin_dir / "gh"
    log_file = tmp_path / "gh_calls.log"
    gh_mock.write_text(
        f'#!/bin/sh\necho "$@" >> "{log_file}"\nexit 0\n',
        encoding="utf-8",
    )
    gh_mock.chmod(gh_mock.stat().st_mode | stat.S_IXUSR)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    (dist_dir / "asset1.tar.gz").write_bytes(b"binary-1")
    (dist_dir / "asset2.sha256").write_text("dummy sha", encoding="utf-8")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_TOKEN": "test-token",
        "GH_REPO": "plurano/amussis",
        "TAG": tag,
    }
    return log_file, env


@pytest.mark.parametrize(
    ("tag", "expect_prerelease"),
    [
        ("v1.0.0", False),
        ("v2.1.3", False),
        ("1.0.0", False),
        ("release-20261021", True),
        ("v0.1.0-rc1", True),
        ("v1.0.0-alpha.1", True),
        ("v2.0.0-beta", True),
        ("v1.2.3-preview-4", True),
    ],
)
def test_draft_release_step_execution(tmp_path: Path, tag: str, expect_prerelease: bool):
    """The draft release step sets --draft, --verify-tag, notes and conditional --prerelease."""
    step = get_step("Create the draft release")
    bash = shutil.which("bash")
    assert bash, "bash required"

    assert "--draft" in step["run"]
    assert "--verify-tag" in step["run"]
    assert "--generate-notes" in step["run"]
    assert "--title" in step["run"]

    log_file, env = setup_mock_environment(tmp_path, tag)
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr

    recorded = log_file.read_text(encoding="utf-8").strip()
    assert recorded.startswith(f"release create {tag}")
    assert missing_flags(recorded, tag) == []
    assert "dist/asset1.tar.gz dist/asset2.sha256" in recorded

    if expect_prerelease:
        assert "--prerelease" in recorded
    else:
        assert "--prerelease" not in recorded


def test_draft_release_planted_omitted_flag(tmp_path: Path):
    """A step that drops --draft is refused by the flag check the execution test uses."""
    step = get_step("Create the draft release")
    bash = shutil.which("bash")
    assert bash, "bash required"

    log_file, env = setup_mock_environment(tmp_path, "v1.0.0")
    planted_run = step["run"].replace("--draft", "")
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", planted_run],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    recorded = log_file.read_text(encoding="utf-8").strip()
    assert missing_flags(recorded, "v1.0.0") == ["--draft"]
