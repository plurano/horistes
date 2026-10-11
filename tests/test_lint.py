# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""The two outside workflow linters still refuse what the gate relies on them to refuse.

They run here as the Makefile runs them. A missing linter fails the test:
a skipped linter would read as a green gate.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import ACTION, CHECKOUT, REUSABLE, external

WORKFLOW = ".github/workflows/licence.yml"
UNPINNED = REUSABLE.replace(CHECKOUT, "actions/checkout@v7")
INJECTION = REUSABLE.replace('test -d "$TARGET"', 'echo "${{ github.event.pull_request.title }}"')
CREDENTIALS = REUSABLE.replace("        with:\n          persist-credentials: false\n", "")
NO_JOB = REUSABLE.replace("    runs-on: ubuntu-24.04\n", "")


def actionlint(tree):
    return subprocess.run(
        [*external("ACTIONLINT", "actionlint"), str(tree.root / WORKFLOW)],
        capture_output=True,
        text=True,
        check=False,
    )


def zizmor(tree):
    command = [
        *external("ZIZMOR", "zizmor"),
        "--offline",
        "--strict-collection",
        "--persona",
        "pedantic",
        str(tree.root),
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def test_planted_fixtures_differ_from_the_good_one():
    assert len({REUSABLE, UNPINNED, INJECTION, CREDENTIALS, NO_JOB}) == 5


def test_good_workflow_and_action_pass_both_linters(tree):
    tree.write({WORKFLOW: REUSABLE, "actions/draft-stop/action.yml": ACTION})
    assert actionlint(tree).returncode == 0, actionlint(tree).stdout
    done = zizmor(tree)
    assert done.returncode == 0, done.stdout
    assert "No findings to report" in done.stdout


@pytest.mark.parametrize(
    ("workflow", "finding"),
    [(UNPINNED, "unpinned-uses"), (INJECTION, "template-injection"), (CREDENTIALS, "artipacked")],
    ids=["tag-pinned-action", "script-injection", "persisted-credentials"],
)
def test_zizmor_refuses(tree, workflow, finding):
    done = zizmor(tree.write({WORKFLOW: workflow}))
    assert done.returncode != 0
    assert f"[{finding}]" in done.stdout


@pytest.mark.parametrize(
    ("workflow", "finding"),
    [(INJECTION, "potentially untrusted"), (NO_JOB, '"runs-on" section is missing')],
    ids=["script-injection", "job-without-runner"],
)
def test_actionlint_refuses(tree, workflow, finding):
    done = actionlint(tree.write({WORKFLOW: workflow}))
    assert done.returncode != 0
    assert finding in done.stdout
