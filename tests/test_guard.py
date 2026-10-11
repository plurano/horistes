# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Visibility guard: a workflow with an input for public repositories only refuses for itself.

The rule is read by hosts.py on fixtures and on the workflows of this
repository. The command of the real guard is run as the forge runs it, once
for every visibility an event can name.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from conftest import HOSTS, REUSABLE, ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

WORKFLOW = ".github/workflows/licence.yml"
SCORECARD = ".github/workflows/scorecard.yml"
RUN = '          test -d "$TARGET"\n'
LOG = "          curl -fsSL https://rekor.sigstore.dev/api/v1/log\n"
MARKED = (
    HOSTS
    + f'\n[files."{WORKFLOW}"."rekor.sigstore.dev"]\n'
    + 'reason = "public transparency log"\ninput = "publish"\npublic_only = true\n'
)
CONDITION = "    if: inputs.publish\n"
READS = "          VISIBILITY: ${{ github.event.repository.visibility }}\n"
STEP = "      - name: Refuse a repository that is not public\n"
COMMAND = '        run: |\n          test "$VISIBILITY" = public\n'
GUARD = f"""\
  visibility:
    name: Public repository
{CONDITION}    runs-on: ubuntu-24.04
    timeout-minutes: 5
    steps:
{STEP}        env:
{READS}{COMMAND}"""
JOB = "  check:\n    name: Licence check\n"
NEEDS = "    needs: visibility\n"
UNGUARDED = REUSABLE.replace(RUN, RUN + LOG)
GUARDED = UNGUARDED.replace("jobs:\n" + JOB, "jobs:\n" + GUARD + JOB + NEEDS)
# What the host scan reads of this repository: the workflows, the declaration, the Makefile.
REAL = (
    ".github/workflows/ci.yml",
    ".github/workflows/licence-check.yml",
    WORKFLOW,
    SCORECARD,
    "hosts.toml",
    "Makefile",
)


def scan(tree, workflow=GUARDED):
    return tree.write({WORKFLOW: workflow, "hosts.toml": MARKED}).tool("hosts.py", "check")


def test_fixture_is_built_as_meant():
    assert LOG in UNGUARDED
    assert GUARD + JOB + NEEDS in GUARDED


def test_guarded_workflow_passes(tree):
    done = scan(tree)
    assert done.returncode == 0, done.stderr


def test_workflow_without_a_guard_fails(tree):
    done = scan(tree, UNGUARDED)
    assert done.returncode == 1, done.stdout
    assert "no job `visibility` refuses another repository" in done.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (CONDITION, "    if: ${{ inputs.publish }}\n"),
        (NEEDS, "    needs: [visibility]\n"),
        (NEEDS, NEEDS + "    if: inputs.publish && github.event_name == 'push'\n"),
    ],
    ids=["braces", "needs-list", "condition-without-status"],
)
def test_other_spelling_of_the_guard_passes(tree, old, new):
    assert GUARDED.count(old) == 1
    done = scan(tree, GUARDED.replace(old, new))
    assert done.returncode == 0, done.stderr


PLANTED = [
    pytest.param(CONDITION, "", "does not run exactly when `inputs.publish`", id="no-condition"),
    pytest.param(
        CONDITION,
        "    if: inputs.path\n",
        "does not run exactly when `inputs.publish`",
        id="other-input",
    ),
    pytest.param(
        CONDITION,
        "    if: inputs.publish && github.event_name == 'push'\n",
        "does not run exactly when `inputs.publish`",
        id="narrowed-condition",
    ),
    pytest.param(
        READS,
        "          VISIBILITY: ${{ github.repository }}\n",
        "has no step that runs a command on `${{ github.event.repository.visibility }}`",
        id="reads-something-else",
    ),
    pytest.param(
        COMMAND,
        "        uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n",
        "has no step that runs a command on",
        id="no-command",
    ),
    pytest.param(
        CONDITION,
        CONDITION + "    continue-on-error: true\n",
        "sets `continue-on-error`, so its failure would not count",
        id="lenient-job",
    ),
    pytest.param(
        STEP,
        STEP + "        continue-on-error: true\n",
        "sets `continue-on-error`, so its failure would not count",
        id="lenient-step",
    ),
    pytest.param(
        CONDITION,
        CONDITION + "    needs: check\n",
        "needs another job, so it can be skipped",
        id="guard-needs-a-job",
    ),
]


@pytest.mark.parametrize(("old", "new", "message"), PLANTED)
def test_guard_that_cannot_refuse_fails(tree, old, new, message):
    assert GUARD.count(old) == 1
    done = scan(tree, GUARDED.replace(GUARD, GUARD.replace(old, new)))
    assert done.returncode == 1, done.stdout
    assert f"{WORKFLOW}: job `visibility` {message}" in done.stderr


def test_job_that_does_not_need_the_guard_fails(tree):
    done = scan(tree, GUARDED.replace(NEEDS, ""))
    assert done.returncode == 1, done.stdout
    assert "job `check` does not need job `visibility`" in done.stderr


@pytest.mark.parametrize(
    "condition",
    [
        "always()",
        "${{ !cancelled() }}",
        "failure() || inputs.publish",
        "${{ always () && inputs.publish }}",
    ],
)
def test_job_that_runs_after_a_failed_guard_fails(tree, condition):
    done = scan(tree, GUARDED.replace(NEEDS, NEEDS + f"    if: {condition}\n"))
    assert done.returncode == 1, done.stdout
    assert "job `check` runs although job `visibility` failed" in done.stderr


def real(tree, old="", new=""):
    """The workflows of this repository with one text changed in the scorecard workflow."""
    files = {name: (ROOT / name).read_text(encoding="utf-8") for name in REAL}
    assert files[SCORECARD].count(old) == 1 or not old
    files[SCORECARD] = files[SCORECARD].replace(old, new)
    return tree.write(files).tool("hosts.py", "check")


def test_scorecard_workflow_of_this_repository_is_guarded(tree):
    done = real(tree)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "    if: inputs.outside_hosts || inputs.publish\n",
            "    if: inputs.outside_hosts\n",
            "does not run exactly when `inputs.outside_hosts || inputs.publish`",
        ),
        ("    needs: visibility\n", "", "job `analysis` does not need job `visibility`"),
        ("  visibility:\n", "  public:\n", "no job `visibility` refuses another repository"),
    ],
    ids=["one-input", "no-needs", "renamed"],
)
def test_scorecard_workflow_with_a_weakened_guard_fails(tree, old, new, message):
    done = real(tree, old, new)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def refuse(tmp_path, visibility):
    """Run the command of the real guard the way the forge runs a bash step."""
    guard = ciwf.load(ROOT / SCORECARD)["jobs"]["visibility"]
    (step,) = guard["steps"]
    assert step["shell"] == "bash"
    assert step["env"]["VISIBILITY"] == "${{ github.event.repository.visibility }}"
    assert "${{" not in step["run"]
    bash = shutil.which("bash")
    assert bash, "the guard is a bash step; bash is needed to run it"
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {**step["env"], "VISIBILITY": visibility, "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=20,
    )
    return done, summary.read_text(encoding="utf-8")


def test_guard_passes_in_a_public_repository(tmp_path):
    done, summary = refuse(tmp_path, "public")
    assert done.returncode == 0, done.stderr
    assert (done.stdout, summary) == ("", "")


@pytest.mark.parametrize("visibility", ["private", "internal", "", "Public", "public ", "true"])
def test_guard_fails_with_its_reason_anywhere_else(tmp_path, visibility):
    done, summary = refuse(tmp_path, visibility)
    assert done.returncode == 1, done.stdout
    line = done.stdout.strip()
    assert line.startswith("::error title=Repository is not public::The analysis did not run.")
    assert f"visibility as '{visibility}'." in line
    assert "\n" not in line
    assert summary.strip() == line.split("::", 2)[2]
