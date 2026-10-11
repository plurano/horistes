# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Shared helpers: build a small tree in a temporary directory and run a tool on it.

The fixtures are written here as text, for the purpose; none is copied from
another repository. A planted defect is a string replacement on a good
fixture, so each test shows the one line that makes the tool refuse.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"

REUSABLE = f"""\
---
name: Licence check
on:
  workflow_call:
    inputs:
      path:
        description: Directory to check.
        type: string
        required: false
        default: .
      publish:
        description: Publish the result to the public log.
        type: boolean
        required: false
        default: false
    secrets:
      token:
        description: Token for a private dependency.
        required: false
    outputs:
      verdict:
        description: pass or fail
        value: ${{{{ jobs.check.outputs.verdict }}}}
permissions:
  contents: read
jobs:
  check:
    name: Licence check
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    outputs:
      verdict: ${{{{ steps.run.outputs.verdict }}}}
    steps:
      - name: Check out
        uses: {CHECKOUT}
        with:
          persist-credentials: false
      - name: Run
        id: run
        env:
          TARGET: ${{{{ inputs.path }}}}
        run: |
          test -d "$TARGET"
          echo "verdict=pass" >> "$GITHUB_OUTPUT"
"""

ACTION = """\
---
name: Draft stop
description: Fails on a draft pull request.
inputs:
  message:
    description: Text of the error.
    required: false
    default: The gate does not run on a draft.
outputs:
  stopped:
    description: true when the step stopped the job
    value: ${{ steps.stop.outputs.stopped }}
runs:
  using: composite
  steps:
    - name: Stop
      id: stop
      shell: bash
      env:
        MESSAGE: ${{ inputs.message }}
      run: |
        echo "stopped=false" >> "$GITHUB_OUTPUT"
        echo "$MESSAGE"
"""

HOSTS = """\
forge = ["github.com", "api.github.com", "ghcr.io"]

[actions]
"actions/checkout" = []
"""


class Tree:
    """A directory of fixture files and the tools run inside it."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, files: dict[str, str]) -> Tree:
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return self

    def tool(self, name: str, *args: str) -> subprocess.CompletedProcess:
        command = [
            sys.executable,
            "-B",
            str(ROOT / "tools" / name),
            *args,
            "--root",
            str(self.root),
        ]
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def git(self, *args: str) -> str:
        identity = [
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
        ]
        done = subprocess.run(
            ["git", "-C", str(self.root), *identity, *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def external(variable: str, default: str) -> list[str]:
    """The command of an outside linter, from the environment the Makefile sets.

    A missing tool is an error, never a skip.
    """
    return shlex.split(os.environ.get(variable, default))
