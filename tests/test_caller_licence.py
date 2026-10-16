# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The rendered caller templates carry their licence header and pass reuse lint."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, external

sys.path.insert(0, str(ROOT / "tools"))
import render

CALLERS = [
    ("templates/callers/licence.yml.tmpl", ".github/workflows/licence-check.yml"),
    ("templates/callers/scorecard.yml.tmpl", ".github/workflows/scorecard-check.yml"),
    ("templates/callers/merge-train.yml.tmpl", ".github/workflows/merge-train.yml"),
]


def lint(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the licence tool in `root`; a tool path relative to the repository stays valid there."""
    tool, *options = external("REUSE", "reuse")
    if "/" in tool:
        tool = str((ROOT / tool).resolve())
    command = [tool, *options, *args, "lint", "--lines"]
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, env=env)


@pytest.mark.parametrize(
    ("template", "name"),
    CALLERS,
    ids=["licence", "scorecard", "merge-train"],
)
def test_rendered_caller_passes_reuse_lint(tmp_path: Path, template: str, name: str) -> None:
    source = (ROOT / template).read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, template)
    workflow = tmp_path / name
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(rendered, encoding="utf-8")

    identifier = render.SAMPLE["Licence"]["Identifier"]
    licenses = tmp_path / "LICENSES"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSES" / f"{identifier}.txt", licenses / f"{identifier}.txt")

    done = lint(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr

    lines = rendered.splitlines(keepends=True)
    unlicensed = "".join(lines[2:])
    workflow.write_text(unlicensed, encoding="utf-8")
    planted = lint(tmp_path)
    assert planted.returncode == 1, planted.stdout + planted.stderr
    assert name in planted.stdout
