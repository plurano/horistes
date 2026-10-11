# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The planted licence fixtures: the tool refuses one and passes the other, as the workflow runs it.

The unlicensed fixture is a file that must stay without licence information.
It is committed like any other file: REUSE.toml names that one file, so the
check at the repository root passes over it. The licence workflow, given the
fixture folder as `root`, must fail on it; given the licensed fixture, pass.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import ROOT, external

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

UNLICENSED = "tests/fixtures/unlicensed"
LICENSED = "tests/fixtures/licensed"
PLANTED = f"{UNLICENSED}/planted.py"
GUARD = "Refuse a root that is no plain relative path"
TOOL = "reuse lint"
IMAGE = re.compile(r"docker://docker\.io/fsfe/reuse@sha256:[0-9a-f]{64}")
# Git reads these as one setting of the command scope, where `safe.directory` counts.
TRUSTED_CHECKOUT = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "/github/workspace",
}


def lint(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the licence tool in `root`; a tool path relative to the repository stays valid there."""
    tool, *options = external("REUSE", "reuse")
    if "/" in tool:
        tool = str((ROOT / tool).resolve())
    command = [tool, *options, *args, "lint", "--lines"]
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, env=env)


def refused(done: subprocess.CompletedProcess, root: Path) -> dict[str, list[str]]:
    """The files the tool refused, relative to `root`, each with what it lacks."""
    found: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        name, _, lack = line.rpartition(": ")
        path = (root / name).resolve().relative_to(root.resolve())
        found.setdefault(path.as_posix(), []).append(lack)
    return found


def steps() -> list[dict]:
    doc = ciwf.load(ROOT / ".github/workflows/licence.yml")
    return doc["jobs"]["reuse"]["steps"]


def copy_of_the_tracked_tree(target: Path) -> Path:
    """The files git tracks, copied without the repository: what a checkout holds."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True
    )
    for name in filter(None, listed.stdout.split("\0")):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, path, follow_symlinks=False)
    return target


LACKS = ["no license identifier", "no copyright notice"]


def test_unlicensed_fixture_is_refused_as_root():
    done = lint(ROOT, "--root", UNLICENSED)
    assert done.returncode == 1, done.stdout + done.stderr
    assert refused(done, ROOT) == {PLANTED: LACKS}


def test_licensed_fixture_passes_as_root():
    done = lint(ROOT, "--root", LICENSED)
    assert (done.returncode, done.stdout) == (0, ""), done.stdout + done.stderr


def test_repository_passes_only_through_the_annotation_of_the_planted_file(tmp_path):
    tree = copy_of_the_tracked_tree(tmp_path)
    done = lint(tree)
    assert (done.returncode, done.stdout) == (0, ""), done.stdout + done.stderr
    declared = tree / "REUSE.toml"
    text = declared.read_text(encoding="utf-8")
    entry = f'path = ["{PLANTED}"]'
    assert text.count(entry) == 1
    declared.write_text(text.replace(entry, 'path = ["tests/fixtures/absent.py"]'))
    done = lint(tree)
    assert done.returncode == 1, done.stdout + done.stderr
    assert refused(done, tree) == {PLANTED: LACKS}


def test_annotation_names_the_planted_file_and_nothing_else_below_tests():
    data = tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))
    paths = [path for entry in data["annotations"] for path in entry["path"]]
    assert [path for path in paths if path.startswith("tests")] == [PLANTED]
    overriding = [e["path"] for e in data["annotations"] if e["precedence"] == "override"]
    assert overriding == [[PLANTED]]


def test_workflow_passes_the_root_to_the_tool_and_guards_it_first():
    names = [step.get("name") for step in steps()]
    assert names.index(GUARD) < names.index(TOOL)
    tool = steps()[names.index(TOOL)]
    expression = "${{ inputs.root == '.' && 'lint' || format('--root {0} lint', inputs.root) }}"
    assert tool["with"] == {"args": expression}
    guard = steps()[names.index(GUARD)]
    assert guard["env"]["ROOT"] == "${{ inputs.root }}"


def test_workflow_runs_the_tool_from_an_image_named_by_digest():
    tool = next(step for step in steps() if step.get("name") == TOOL)
    assert IMAGE.fullmatch(tool["uses"]), tool["uses"]


def test_workflow_lets_git_trust_the_checkout_the_tool_reads():
    tool = next(step for step in steps() if step.get("name") == TOOL)
    assert tool["env"] == TRUSTED_CHECKOUT


ROOTS = [
    (".", 0),
    (UNLICENSED, 0),
    ("a_b/c-d.e", 0),
    ("", 1),
    ("/etc", 1),
    ("--include-submodules", 1),
    ("-x", 1),
    ("../other", 1),
    ("a/../b", 1),
    ("a b", 1),
    ("a;id", 1),
    ("$(id)", 1),
    ("a\nb", 1),
    ("ä", 1),
]


@pytest.mark.parametrize(("root", "code"), ROOTS)
def test_guard_refuses_a_root_that_is_no_plain_relative_path(root, code):
    guard = next(step for step in steps() if step.get("name") == GUARD)
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": guard["env"]["LC_ALL"], "ROOT": root}
    done = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", guard["run"]],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == code, done.stdout + done.stderr
    assert ("::error title=Root refused::" in done.stdout) == bool(code)
