# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Development mode: the suite fails a test that leaves a resource open.

The test recipe starts the suite under ``python -X dev`` and pyproject.toml
turns ResourceWarning into an error. Each planted run starts a child pytest
on a small planted suite, one test that drops an HTTPError without closing
it and one that closes it, under the filters the run names. The recipe tests
run `make test` with a probe in place of python, which records what each
step inherits.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from conftest import ROOT
from test_coverage_floor import _recipe_lines

DEADLINE = 120
# Only these variables reach a planted run, so a caller's PYTEST_ADDOPTS,
# PYTHONWARNINGS, PYTHONDEVMODE or coverage settings cannot change it.
KEEP = ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "LANG", "LC_ALL")
FILTERS = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"][
    "ini_options"
]["filterwarnings"]
PLANTED = """\
import gc
import io
import urllib.error


def _error():
    return urllib.error.HTTPError("https://x.test", 404, "Not Found", {}, io.BytesIO(b""))


def test_leaks_an_http_error():
    try:
        raise _error()
    except urllib.error.HTTPError:
        pass
    gc.collect()


def test_closes_the_http_error():
    try:
        raise _error()
    except urllib.error.HTTPError as err:
        err.close()
    gc.collect()
"""


def _child(command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run one child process with fixed arguments and no shell, within DEADLINE."""
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )


def _planted_run(tmp_path: Path, filters: list[str]) -> subprocess.CompletedProcess[str]:
    """Run the planted suite in development mode under the given warning filters."""
    (tmp_path / "test_planted.py").write_text(PLANTED, encoding="utf-8")
    lines = ["[pytest]", "addopts = -q -p no:cacheprovider"]
    if filters:
        lines += ["filterwarnings =", *(f"    {entry}" for entry in filters)]
    ini = tmp_path / "pytest.ini"
    ini.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = {name: os.environ[name] for name in KEEP if name in os.environ}
    command = [sys.executable, "-X", "dev", "-B", "-m", "pytest", "-c", str(ini)]
    return _child([*command, "--rootdir", str(tmp_path), "test_planted.py"], cwd=tmp_path, env=env)


def test_repository_filters_fail_the_planted_leak(tmp_path: Path) -> None:
    """Under the filters of pyproject.toml the leak fails and the closed error passes."""
    done = _planted_run(tmp_path, FILTERS)
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert re.search(r"^FAILED test_planted\.py::test_leaks_an_http_error\b", out, re.M), out
    assert "ResourceWarning" in out, out
    assert re.search(r"^1 failed, 1 passed\b", out, re.M), out


def test_without_filters_the_planted_leak_passes(tmp_path: Path) -> None:
    """Negative control: development mode alone reports the leak and fails nothing."""
    done = _planted_run(tmp_path, [])
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert re.search(r"^2 passed\b", out, re.M), out


@pytest.mark.parametrize("entry", FILTERS)
def test_one_filter_alone_lets_the_planted_leak_pass(tmp_path: Path, entry: str) -> None:
    """Each filter alone fails nothing: the leak fails only under both."""
    done = _planted_run(tmp_path, [entry])
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert re.search(r"^2 passed\b", out, re.M), out


def test_test_recipe_keeps_assertions() -> None:
    """No step of the test recipe starts Python with -O or -OO, and PYTHONOPTIMIZE is cleared."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    steps = _recipe_lines(makefile, "test")
    assert any(" -X dev " in step for step in steps), steps
    assert not [step for step in steps if re.search(r"\s-O{1,2}(\s|$)", step)], steps
    run_step = next(step for step in steps if "coverage run" in step)
    assert re.search(r"\bPYTHONOPTIMIZE=\s", run_step), run_step
    assert re.search(r"\bPYTEST_ADDOPTS=\s", run_step), run_step
    assert not re.search(r"PYTHONOPTIMIZE=\S", makefile)


# The probe stands in for $(BIN)/python: each call logs its optimization
# level, PYTEST_ADDOPTS and arguments as one JSON line, then exits 0.
PROBE = """\
import json
import os
import sys
from pathlib import Path

line = json.dumps(
    {
        "optimize": sys.flags.optimize,
        "addopts": os.environ.get("PYTEST_ADDOPTS"),
        "argv": sys.argv[1:],
    }
)
with (Path(__file__).resolve().parent / "probe.jsonl").open("a", encoding="utf-8") as log:
    log.write(line + "\\n")
"""
CLEARED = "PYTHONOPTIMIZE= PYTEST_ADDOPTS= "


def _setup_probe_tree(tmp_path: Path) -> None:
    """Plant requirements, a matching stamp and a probe python: `make test` installs nothing."""
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "tools.txt").write_text("tools\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    stamp = hashlib.sha256(b"tools\n").hexdigest()
    (tmp_path / ".venv" / ".stamp").write_text(stamp + "\n", encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    (tmp_path / "bin").mkdir()
    python = tmp_path / "bin" / "python"
    python.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path}/probe.py" "$@"\n', encoding="utf-8"
    )
    python.chmod(0o755)


def _probe_coverage_run(tmp_path: Path, cleared: bool, inherited: dict[str, str]) -> dict:
    """Run `make test` with the probe as python and return what the coverage run step saw.

    With cleared False the recipe runs from a copy of the Makefile without the
    two empty assignments, so the probe shows what the step would inherit.
    """
    _setup_probe_tree(tmp_path)
    makefile = ROOT / "Makefile"
    if not cleared:
        text = makefile.read_text(encoding="utf-8")
        assert text.count(CLEARED) == 1
        makefile = tmp_path / "Makefile.uncleared"
        makefile.write_text(text.replace(CLEARED, ""), encoding="utf-8")
    env = {name: os.environ[name] for name in KEEP if name in os.environ} | inherited
    command = [
        "make",
        "-s",
        "-C",
        str(tmp_path),
        "-f",
        str(makefile),
        "test",
        f"BIN={tmp_path}/bin",
    ]
    done = _child(command, cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    log = (tmp_path / "probe.jsonl").read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in log]
    assert len(calls) == 4, calls
    steps = [call["argv"][call["argv"].index("coverage") + 1] for call in calls]
    assert steps == ["run", "combine", "report", "report"], calls
    assert calls[0]["argv"][:2] == ["-X", "dev"], calls
    return calls[0]


@pytest.mark.parametrize(
    ("cleared", "inherited", "optimize", "addopts"),
    [
        (True, {"PYTHONOPTIMIZE": "2", "PYTEST_ADDOPTS": "-Wignore"}, 0, ""),
        (False, {"PYTHONOPTIMIZE": "2", "PYTEST_ADDOPTS": "-Wignore"}, 2, "-Wignore"),
        (False, {"PYTHONOPTIMIZE": "", "PYTEST_ADDOPTS": ""}, 0, ""),
    ],
    ids=["positive-cleared", "negative-uncleared", "boundary-empty-is-cleared"],
)
def test_test_recipe_clears_the_inherited_settings(
    tmp_path: Path, cleared: bool, inherited: dict[str, str], optimize: int, addopts: str
) -> None:
    """The coverage run step gets no inherited PYTHONOPTIMIZE or PYTEST_ADDOPTS.

    The negative case shows the probe sees what a step inherits; the boundary
    case shows an empty value, which is what the recipe sets, leaves the
    asserts on and adds no option.
    """
    run = _probe_coverage_run(tmp_path, cleared, inherited)
    assert (run["optimize"], run["addopts"]) == (optimize, addopts), run
