# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Development mode: the suite fails a test that leaves a resource open.

The test recipe starts the suite under ``python -X dev`` and pyproject.toml
turns ResourceWarning into an error. Each planted run starts a child pytest
on a small planted suite, one test that drops an HTTPError without closing
it and one that closes it, under the filters the run names.
"""

from __future__ import annotations

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
    return subprocess.run(
        [*command, "--rootdir", str(tmp_path), "test_planted.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )


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
    """No step of the test recipe starts Python with -O or -OO, and no PYTHONOPTIMIZE is set."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    steps = _recipe_lines(makefile, "test")
    assert any(" -X dev " in step for step in steps), steps
    assert not [step for step in steps if re.search(r"\s-O{1,2}(\s|$)", step)], steps
    assert "PYTHONOPTIMIZE" not in makefile
