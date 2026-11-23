# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Shared helpers: build a small tree in a temporary directory and run a tool on it.

The fixtures are written here as text, for the purpose; none is copied from
another repository. A planted defect is a string replacement on a good
fixture, so each test shows the one line that makes the tool refuse.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ciwf

ROOT = Path(__file__).resolve().parent.parent

# The consumer path each template's header names.
CALLER_TEMPLATES: dict[str, str] = {
    "templates/callers/hook-copy.yml.tmpl": ".github/workflows/hook-copy.yml",
    "templates/callers/licence.yml.tmpl": ".github/workflows/licence-check.yml",
    "templates/callers/merge-train-release.yml.tmpl": ".github/workflows/merge-train-release.yml",
    "templates/callers/merge-train.yml.tmpl": ".github/workflows/merge-train.yml",
    "templates/callers/release-please.yml.tmpl": ".github/workflows/release-please.yml",
    "templates/callers/required-checks.yml.tmpl": ".github/workflows/required-checks-caller.yml",
    "templates/callers/scorecard.yml.tmpl": ".github/workflows/scorecard-check.yml",
    "templates/callers/secret-scan.yml.tmpl": ".github/workflows/secret-scan.yml",
    "templates/callers/sign-off.yml.tmpl": ".github/workflows/sign-off-check.yml",
    "templates/callers/workflow-check.yml.tmpl": ".github/workflows/workflow-check.yml",
}
CALLER_IDS = [
    Path(template).name.removesuffix(".yml.tmpl") for template in sorted(CALLER_TEMPLATES)
]

# The stub gh programs check only that GH_TOKEN is set; a random value per run
# keeps a token-shaped literal out of the tests.
STUB_GH_TOKEN = secrets.token_hex(8)

# The commit address of the merge-train app's bot user (numeric user id, then the
# app slug), read from GET /users/plurano-merge-train[bot]. Renovate compares an
# entry for equality before it reads it as a glob, so the brackets stay literal.
TRAIN_BOT = "342223998+plurano-merge-train[bot]@users.noreply.github.com"

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
# a file of canned answers, records every call, and serves nothing else. An
# answer {"hex": ...} is served as those bytes, the way an archive comes; `--include`
# wraps answers with HTTP status and headers and supports stateful `sequence` lists.
FAKE_CLIENT = """\
import json
import os
import sys

HEAD = ["api", "-H", "Accept: application/vnd.github+json"]
HEAD += ["-H", "X-GitHub-Api-Version: 2022-11-28"]
INCLUDE_HEAD = ["api", "--include", *HEAD[1:]]
args = sys.argv[1:]
with open(os.environ["FAKE_FORGE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
include = args[0:2] == ["api", "--include"] if len(args) >= 2 else False
endpoint = None
post_fields = None
if args and args[0] == "api" and "-X" in args:
    idx = args.index("-X")
    if idx + 1 < len(args) and args[idx + 1] not in ("GET", "POST"):
        sys.stderr.write("fake forge: only GET/POST requests through `api` are served\\n")
        sys.exit(2)
    if idx + 2 < len(args):
        endpoint = args[idx + 2]
    fields: dict[str, str] = {}
    pos = idx + 3
    while pos + 1 < len(args):
        if args[pos] == "-f" and "=" in args[pos + 1]:
            key, val = args[pos + 1].split("=", 1)
            fields[key] = val
            pos += 2
        else:
            pos += 1
    if idx + 1 < len(args) and args[idx + 1] == "POST":
        post_fields = fields
elif len(args) >= 6 and args[:5] == HEAD:
    endpoint = args[5]
    include = False
elif len(args) >= 7 and args[:6] == INCLUDE_HEAD:
    include, endpoint = True, args[6]
else:
    sys.stderr.write("fake forge: only GET/POST requests through `api` are served\\n")
    sys.exit(2)
if post_fields is not None:
    body = json.dumps({"id": 1, "state": post_fields.get("state", "pending")})
    sys.stdout.write(f"HTTP/2.0 201 Created\\r\\nContent-Type: application/json\\r\\n\\r\\n{body}")
    sys.exit(0)
forge_path = os.environ["FAKE_FORGE"]
with open(forge_path, encoding="utf-8") as source:
    answer = json.load(source).get(endpoint)
if isinstance(answer, dict) and list(answer) == ["sequence"]:
    state_path = forge_path + ".state"
    counts = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as state_in:
            counts = json.load(state_in)
    idx = counts.get(endpoint, 0)
    counts[endpoint] = idx + 1
    with open(state_path, "w", encoding="utf-8") as state_out:
        json.dump(counts, state_out)
    seq = answer["sequence"]
    answer = seq[idx] if idx < len(seq) else seq[-1]
if not include:
    if answer is None:
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    if isinstance(answer, dict) and list(answer) == ["fail"]:
        sys.stderr.write(answer["fail"] + "\\n")
        sys.exit(1)
    if isinstance(answer, dict) and list(answer) == ["hex"]:
        sys.stdout.buffer.write(bytes.fromhex(answer["hex"]))
        sys.exit(0)
    sys.stdout.write(answer["raw"] if isinstance(answer, dict) and list(answer) == ["raw"]
                     else json.dumps(answer))
    sys.exit(0)
if answer is None:
    sys.stdout.write('HTTP/2.0 404 Not Found\\r\\n\\r\\n{"message":"Not Found"}')
    sys.stderr.write("gh: Not Found (HTTP 404)\\n")
    sys.exit(1)
if isinstance(answer, dict) and "raw_include" in answer:
    sys.stdout.write(answer["raw_include"])
    if answer.get("stderr"):
        sys.stderr.write(answer["stderr"] + "\\n")
    sys.exit(int(answer.get("exit", 0)))
if isinstance(answer, dict) and "http" in answer:
    status = int(answer["http"])
    hdrs = "".join(f"{k}: {v}\\r\\n" for k, v in (answer.get("headers") or {}).items())
    body = answer["raw_body"] if "raw_body" in answer else json.dumps(answer.get("body", {}))
    sys.stdout.write(f"HTTP/2.0 {status} Status\\r\\n{hdrs}\\r\\n{body}")
    if answer.get("stderr"):
        sys.stderr.write(answer["stderr"] + "\\n")
    elif status >= 400:
        sys.stderr.write(f"gh: HTTP {status}\\n")
    sys.exit(0 if 200 <= status < 300 else 1)
if isinstance(answer, dict) and list(answer) == ["fail"]:
    sys.stderr.write(answer["fail"] + "\\n")
    sys.exit(1)
body = answer["raw"] if isinstance(answer, dict) and list(answer) == ["raw"] else json.dumps(answer)
sys.stdout.write(f"HTTP/2.0 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n{body}")
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


def sarif_log(findings: tuple = (), rules: tuple = (), version: str = "v5.5.0") -> str:
    """A result file as a scorecard run writes it: its rules, and one result per finding.

    A finding is (rule, file, line, message).
    """
    results = [
        {
            "ruleId": rule,
            "ruleIndex": 0,
            "message": {"text": text},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"uri": uri},
                        "region": {"startLine": line},
                    }
                }
            ],
        }
        for rule, uri, line, text in findings
    ]
    driver = {"name": "Scorecard", "semanticVersion": version, "rules": [{"id": r} for r in rules]}
    run = {
        "automationDetails": {"id": "supply-chain/local/0000-11 Oct 26 01:28 +0000"},
        "tool": {"driver": driver},
        "results": results,
    }
    return json.dumps({"version": "2.1.0", "runs": [run]})


def archive(members: dict[str, str]) -> bytes:
    """A zip archive with these members, as the forge serves an artifact."""
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as made:
        for name, text in members.items():
            made.writestr(name, text)
    return packed.getvalue()


def forge_artifact(number: int, name: str, data: bytes, **fields) -> dict:
    """One artifact as the forge lists it; `data` is the archive its download gives."""
    return {
        "id": number,
        "name": name,
        "expired": False,
        "size_in_bytes": len(data),
        "created_at": "2026-10-11T01:28:55Z",
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "data": data.hex(),
        **fields,
    }


def _artifact_answers(repository: str, run: dict, names: tuple[str, ...]) -> dict[str, object]:
    """The listing of a run's artifacts per asked name, and the download of each."""
    answers: dict[str, object] = {}
    held = run.get("artifacts") or []
    for name in {*names, *(artifact["name"] for artifact in held)}:
        listed = [
            {k: v for k, v in artifact.items() if k != "data"}
            for artifact in held
            if artifact["name"] == name
        ]
        asked = f"name={quote(name, safe='')}&{PAGE}"
        path = f"repos/{repository}/actions/runs/{run['id']}/artifacts?{asked}"
        answers[path] = {"total_count": len(listed), "artifacts": listed}
    for artifact in held:
        answers[f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"] = {
            "hex": artifact["data"]
        }
    return answers


def forge_answers(repository: str, runs: list[dict], names: tuple[str, ...] = ()) -> dict:
    """The canned answers for a listing of runs on the branch trial/licence.

    A run may carry `artifacts`; `names` are artifact names every run is asked for.
    """
    hidden = ("attempts", "artifacts")
    listing = [{k: v for k, v in run.items() if k not in hidden} for run in runs]
    answers: dict[str, object] = {
        f"repos/{repository}/actions/runs?branch=trial%2Flicence&{PAGE}": {
            "total_count": len(listing),
            "workflow_runs": listing,
        }
    }
    for run in runs:
        answers.update(_artifact_answers(repository, run, names))
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
        (self.root / "forge.json.state").unlink(missing_ok=True)
        self.write({"fake_gh.py": FAKE_CLIENT, "forge.json": json.dumps(answers)})
        return {
            "GH": shlex.join([sys.executable, "-B", str(self.root / "fake_gh.py")]),
            "FAKE_FORGE": str(self.root / "forge.json"),
            "FAKE_FORGE_LOG": str(self.root / "forge.log"),
        }

    def forge_calls(self) -> list[str]:
        """The endpoint of each GET or POST request the stand-in client received."""
        log = self.root / "forge.log"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        assert all(call[0] == "api" for call in calls), calls
        return [call[call.index("-X") + 2] if "-X" in call else call[-1] for call in calls]

    def git(self, *args: str) -> str:
        """Run git with fixture identity, isolated environment and a timeout."""
        self.root.mkdir(parents=True, exist_ok=True)
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
            timeout=30,
            env=ciwf.git_env(),
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


def rooted(cmd: list[str]) -> list[str]:
    """The command with a relative tool path read from the checkout, as the Makefile passes it.

    The planted tree is the working directory, so `.venv/bin/x` would name a
    file inside it. The Makefile writes `/`; a native separator counts too, so
    the rule holds where `os.sep` is a backslash.
    """
    head = cmd[0]
    if ("/" in head or os.sep in head) and not os.path.isabs(head):
        return [str(ROOT / head), *cmd[1:]]
    return cmd


def measured(env: dict[str, str]) -> dict[str, str]:
    """Pass COVERAGE_PROCESS_CONFIG to a child process if set in the parent environment."""
    if "COVERAGE_PROCESS_CONFIG" in os.environ:
        return {**env, "COVERAGE_PROCESS_CONFIG": os.environ["COVERAGE_PROCESS_CONFIG"]}
    return dict(env)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Save coverage in each pytest-xdist worker once session teardown has finished.

    Under pytest-xdist one worker's exit-time coverage save was lost; saving here
    keeps the parallel measured total equal to the serial run.
    """
    if not hasattr(session.config, "workerinput"):
        return
    module = sys.modules.get("coverage")
    if module is None:
        return
    cov = module.Coverage.current()
    if cov is None:
        return
    cov.save()
