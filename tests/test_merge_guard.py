# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Merge guard: a non-CLEAN merge state is refused."""

from __future__ import annotations

import importlib
import subprocess
import sys
from unittest.mock import patch

from conftest import ROOT

GUARD = ROOT / "tools" / "merge_guard.py"


def _fake_gh(state: str):
    """Return a mock for subprocess.run that reports a merge state."""

    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{state}\n", stderr="")

    return _run


def _guard():
    sys.path.insert(0, str(GUARD.parent))
    return importlib.import_module("merge_guard")


def test_clean_passes():
    mod = _guard()
    with patch("merge_guard.subprocess.run", _fake_gh("CLEAN")):
        assert mod.main(["check", "1"]) == 0


def test_unstable_refused():
    mod = _guard()
    with patch("merge_guard.subprocess.run", _fake_gh("UNSTABLE")):
        assert mod.main(["check", "1"]) == 1


def test_unstable_accepted_when_granted():
    mod = _guard()
    with patch("merge_guard.subprocess.run", _fake_gh("UNSTABLE")):
        assert mod.main(["check", "1", "--accept", "UNSTABLE"]) == 0


def test_behind_refused():
    mod = _guard()
    with patch("merge_guard.subprocess.run", _fake_gh("BEHIND")):
        assert mod.main(["check", "1"]) == 1
