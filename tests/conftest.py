# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Shared helpers: build a small tree in a temporary directory and run a tool on it.

The fixtures are written here as text, for the purpose; none is copied from
another repository. A planted defect is a string replacement on a good
fixture, so each test shows the one line that makes the tool refuse.
"""

from __future__ import annotations

import json
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

# A stand-in for the forge's command-line client. It answers GET requests from
# a file of canned answers, records every call, and serves nothing else.
FAKE_CLIENT = """\
import json
import os
import sys

HEAD = ["api", "-H", "Accept: application/vnd.github+json"]
HEAD += ["-H", "X-GitHub-Api-Version: 2022-11-28"]
args = sys.argv[1:]
with open(os.environ["FAKE_FORGE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if args[:5] != HEAD or len(args) != 6:
    sys.stderr.write("fake forge: only GET requests through `api` are served\\n")
    sys.exit(2)
with open(os.environ["FAKE_FORGE"], encoding="utf-8") as source:
    answer = json.load(source).get(args[5])
if answer is None:
    sys.stderr.write("gh: Not Found (HTTP 404)\\n")
    sys.exit(1)
if isinstance(answer, dict) and list(answer) == ["fail"]:
    sys.stderr.write(answer["fail"] + "\\n")
    sys.exit(1)
sys.stdout.write(answer["raw"] if isinstance(answer, dict) and list(answer) == ["raw"]
                 else json.dumps(answer))
"""
PAGE = "per_page=100&page=1"


def forge_job(number: int, name: str, conclusion: str | None, failed: str = "", note: str = ""):
    """One job as the forge lists it; `failed` names the step that failed, `note` its annotation."""
    steps = [{"name": "Set up job", "status": "completed", "conclusion": "success", "number": 1}]
    if failed:
        steps.append({"name": failed, "status": "completed", "conclusion": "failure", "number": 2})
    return {
        "id": number,
        "name": name,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "started_at": "2026-10-11T01:28:40Z",
        "completed_at": "2026-10-11T01:29:01Z" if conclusion else None,
        "steps": steps,
        "note": note,
    }


def forge_run(number: int, path: str, commit: str, created: str, *attempts: list, **fields):
    """One workflow run with the jobs of each of its attempts."""
    return {
        "id": number,
        "path": path,
        "head_sha": commit,
        "event": "pull_request",
        "head_branch": "trial/licence",
        "created_at": created,
        "run_attempt": len(attempts),
        "attempts": list(attempts),
        **fields,
    }


def forge_answers(repository: str, runs: list[dict]) -> dict[str, object]:
    """The canned answers for a listing of runs on the branch trial/licence."""
    listing = [{k: v for k, v in run.items() if k != "attempts"} for run in runs]
    answers: dict[str, object] = {
        f"repos/{repository}/actions/runs?branch=trial%2Flicence&{PAGE}": {
            "total_count": len(listing),
            "workflow_runs": listing,
        }
    }
    for run in runs:
        for number, jobs in enumerate(run["attempts"], 1):
            path = f"repos/{repository}/actions/runs/{run['id']}/attempts/{number}/jobs?{PAGE}"
            answers[path] = {"total_count": len(jobs), "jobs": jobs}
            for job in jobs:
                notes = [{"annotation_level": "failure", "title": "", "message": job["note"]}]
                answers[f"repos/{repository}/check-runs/{job['id']}/annotations?{PAGE}"] = notes
    return answers


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

    def tool(
        self, name: str, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        command = [
            sys.executable,
            "-B",
            str(ROOT / "tools" / name),
            *args,
            "--root",
            str(self.root),
        ]
        return subprocess.run(
            command, capture_output=True, text=True, check=False, env={**os.environ, **(env or {})}
        )

    def forge(self, answers: dict[str, object]) -> dict[str, str]:
        """Environment that puts the stand-in client with these answers in the forge's place."""
        self.write({"fake_gh.py": FAKE_CLIENT, "forge.json": json.dumps(answers)})
        return {
            "GH": shlex.join([sys.executable, "-B", str(self.root / "fake_gh.py")]),
            "FAKE_FORGE": str(self.root / "forge.json"),
            "FAKE_FORGE_LOG": str(self.root / "forge.log"),
        }

    def forge_calls(self) -> list[str]:
        """The path of every request the stand-in client received; anything but a GET fails."""
        log = self.root / "forge.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        assert all(len(call) == 6 and call[0] == "api" for call in calls), calls
        return [call[5] for call in calls]

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
