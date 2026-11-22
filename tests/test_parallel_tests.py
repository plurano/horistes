# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Parallel test workers: Makefile cap, xdist pins, and worker coverage save hook."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest
from conftest import ROOT, pytest_sessionfinish
from test_coverage_floor import _recipe_lines
from test_dev_mode import KEEP, _child, _setup_probe_tree

WORKERS_MSG = re.compile(r"TEST_WORKERS must be a whole number from 1 to 16, got:")


def _makefile_text() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def _run_probe_test(tmp_path: Path, extra_make_args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``make test`` in a probe tree and return the completed process."""
    _setup_probe_tree(tmp_path)
    (tmp_path / "Makefile").write_text(_makefile_text(), encoding="utf-8")
    env = {name: os.environ[name] for name in KEEP if name in os.environ}
    command = [
        "make",
        "-s",
        "-C",
        str(tmp_path),
        "test",
        f"BIN={tmp_path}/bin",
        *extra_make_args,
    ]
    return _child(command, cwd=tmp_path, env=env)


def _coverage_run_argv(tmp_path: Path, workers: str) -> list[str]:
    done = _run_probe_test(tmp_path, [f"TEST_WORKERS={workers}"])
    assert done.returncode == 0, done.stdout + done.stderr
    log = (tmp_path / "probe.jsonl").read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in log]
    return calls[0]["argv"]


def test_tools_in_lists_pytest_xdist() -> None:
    """requirements/tools.in names pytest-xdist."""
    text = (ROOT / "requirements" / "tools.in").read_text(encoding="utf-8")
    assert "pytest-xdist" in text.split()


def test_tools_txt_pins_pytest_xdist_with_hashes() -> None:
    """requirements/tools.txt pins pytest-xdist with sha256 hashes."""
    lines = (ROOT / "requirements" / "tools.txt").read_text(encoding="utf-8").splitlines()
    idx = next(i for i, line in enumerate(lines) if line.startswith("pytest-xdist=="))
    assert lines[idx].startswith("pytest-xdist==")
    hashes = []
    for line in lines[idx + 1 :]:
        stripped = line.strip()
        if stripped.startswith("--hash=sha256:"):
            hashes.append(stripped)
        elif stripped and not stripped.startswith("#") and not stripped.startswith("\\"):
            break
    assert len(hashes) >= 2, hashes


def test_makefile_default_test_workers_is_within_bounds() -> None:
    """Makefile TEST_WORKERS default is a whole number from 1 to 16."""
    match = re.search(r"^TEST_WORKERS \?= (\d+)\s*$", _makefile_text(), re.M)
    assert match is not None
    value = int(match.group(1))
    assert 1 <= value <= 16


def test_test_recipe_coverage_run_uses_xdist_cap() -> None:
    """The coverage run step passes -n auto and --maxprocesses=$(TEST_WORKERS)."""
    steps = _recipe_lines(_makefile_text(), "test")
    run_step = next(step for step in steps if "coverage run" in step)
    assert "-n auto" in run_step
    assert "--maxprocesses=$(TEST_WORKERS)" in run_step


@pytest.mark.parametrize(
    "workers",
    [
        "0",
        "17",
        "abc",
        "",
        "8 -p no:xdist",
        "999999999999999999999999",
        "08",
        "-1",
    ],
    ids=[
        "zero",
        "over-cap",
        "non-numeric",
        "empty",
        "injection",
        "overflow",
        "leading-zero",
        "negative",
    ],
)
def test_test_recipe_refuses_invalid_test_workers(tmp_path: Path, workers: str) -> None:
    """Invalid TEST_WORKERS exits 2 before the probe python runs."""
    done = _run_probe_test(tmp_path, [f"TEST_WORKERS={workers}"])
    assert done.returncode == 2, done.stdout + done.stderr
    assert WORKERS_MSG.search(done.stderr), done.stderr
    assert not (tmp_path / "probe.jsonl").exists()


@pytest.mark.parametrize(
    ("workers", "expected"),
    [
        ("1", "--maxprocesses=1"),
        ("9", "--maxprocesses=9"),
        ("10", "--maxprocesses=10"),
        ("16", "--maxprocesses=16"),
    ],
    ids=["minimum", "single-digit-max", "two-digit-min", "maximum"],
)
def test_test_recipe_passes_worker_cap_to_pytest(
    tmp_path: Path, workers: str, expected: str
) -> None:
    """TEST_WORKERS at the bounds reaches pytest with the matching --maxprocesses."""
    argv = _coverage_run_argv(tmp_path, workers)
    assert expected in argv, argv


class _Session:
    def __init__(self, *, worker: bool) -> None:
        self.config = types.SimpleNamespace()
        if worker:
            self.config.workerinput = {}


def test_worker_sessionfinish_saves_coverage_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a worker, the hook calls Coverage.current().save() exactly once."""
    calls: list[None] = []

    class _Current:
        def save(self) -> None:
            calls.append(None)

    class _Coverage:
        @staticmethod
        def current() -> _Current:
            return _Current()

    monkeypatch.setitem(sys.modules, "coverage", types.SimpleNamespace(Coverage=_Coverage))
    pytest_sessionfinish(_Session(worker=True))
    assert len(calls) == 1


def test_controller_sessionfinish_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without workerinput the hook does not touch coverage."""
    calls: list[None] = []

    class _Coverage:
        @staticmethod
        def current() -> object:
            calls.append(None)
            return None

    monkeypatch.setitem(sys.modules, "coverage", types.SimpleNamespace(Coverage=_Coverage))
    pytest_sessionfinish(_Session(worker=False))
    assert calls == []


def test_sessionfinish_without_coverage_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing coverage in sys.modules is a quiet no-op."""
    monkeypatch.delitem(sys.modules, "coverage", raising=False)
    pytest_sessionfinish(_Session(worker=True))


def test_sessionfinish_when_coverage_current_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coverage.current() returning None does not raise."""

    class _Coverage:
        @staticmethod
        def current() -> None:
            return None

    monkeypatch.setitem(sys.modules, "coverage", types.SimpleNamespace(Coverage=_Coverage))
    pytest_sessionfinish(_Session(worker=True))


def test_sessionfinish_propagates_save_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed save() must fail the worker run."""

    class _Broken:
        def save(self) -> None:
            raise OSError("disk full")

    class _Coverage:
        @staticmethod
        def current() -> _Broken:
            return _Broken()

    monkeypatch.setitem(sys.modules, "coverage", types.SimpleNamespace(Coverage=_Coverage))
    with pytest.raises(OSError, match="disk full"):
        pytest_sessionfinish(_Session(worker=True))


def test_sessionfinish_hook_is_trylast() -> None:
    """The hook stays registered with trylast so it runs after session teardown."""
    assert getattr(pytest_sessionfinish, "pytest_impl", {}).get("trylast") is True
