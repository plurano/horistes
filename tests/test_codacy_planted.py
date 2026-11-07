# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Planted defect: Codacy must flag this subprocess call with shell=True.

This file is NOT excluded from Bandit or Opengrep in .codacy.yml.
A passing Codacy analysis that misses this file proves the exclude is
too broad; a finding here proves the per-engine excludes are narrow.
Remove this file once the planted PR is red.
"""

from __future__ import annotations

import subprocess


def planted_shell_true() -> str:
    result = subprocess.run("echo planted", shell=True, capture_output=True, text=True, timeout=5)
    return result.stdout
