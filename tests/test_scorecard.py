# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Service rules: each rule of the Scorecard service is refused once, and what it allows passes."""

from __future__ import annotations

import pytest
from conftest import CHECKOUT, REUSABLE, ROOT

WORKFLOW = ".github/workflows/scorecard.yml"
PIN = "0123456789abcdef0123456789abcdef01234567"
LABEL = "    runs-on: ubuntu-24.04\n"
TOP = "permissions: {}\n"
JOB = "    timeout-minutes: 15\n"
LAST = f"""\
      - name: Run the analysis
        uses: ossf/scorecard-action@{PIN} # v2.4.4
        with:
          results_file: results.sarif
"""
GOOD = f"""\
---
name: Scorecard
on:
  push:
    branches: [main]
{TOP}jobs:
  analysis:
    name: Analysis
{LABEL}{JOB}    permissions:
      contents: read
      id-token: write
    steps:
      - name: Check out
        uses: {CHECKOUT}
        with:
          persist-credentials: false
{LAST}"""
OTHER = """\
  other:
    name: Other
    runs-on: ${{ matrix.os }}
    env:
      A: b
    permissions:
      contents: read
      id-token: read
    steps:
      - name: Say
        run: echo done
"""


def judge(tree, workflow=GOOD):
    return tree.write({WORKFLOW: workflow}).tool("scorecard.py", "check")


def step(uses):
    return f"      - name: Extra\n        uses: {uses}\n"


def test_workflow_within_the_rules_passes(tree):
    done = judge(tree)
    assert done.returncode == 0, done.stderr
    assert "PASS: 1 workflow(s) run the Scorecard action" in done.stdout


PLANTED = [
    pytest.param(TOP, "env:\n  A: b\n" + TOP, "the workflow sets `env`", id="workflow-env"),
    pytest.param(
        TOP,
        "defaults:\n  run:\n    shell: bash\n" + TOP,
        "the workflow sets `defaults`",
        id="workflow-defaults",
    ),
    pytest.param(
        TOP,
        "permissions:\n  contents: write\n",
        "the workflow lets every job write: contents",
        id="workflow-write-scope",
    ),
    pytest.param(
        TOP,
        "permissions:\n  id-token: write\n",
        "the workflow lets every job write: id-token",
        id="workflow-token-scope",
    ),
    pytest.param(
        TOP,
        "permissions: write-all\n",
        "the workflow lets every job write: <write-all>",
        id="workflow-write-all",
    ),
    pytest.param(
        JOB,
        JOB + "    container: ubuntu:24.04\n",
        "job `analysis` sets `container`",
        id="container",
    ),
    pytest.param(
        JOB,
        JOB + "    services:\n      cache:\n        image: redis:8\n",
        "job `analysis` sets `services`",
        id="services",
    ),
    pytest.param(JOB, JOB + "    env:\n      A: b\n", "job `analysis` sets `env`", id="job-env"),
    pytest.param(
        JOB,
        JOB + "    defaults:\n      run:\n        shell: bash\n",
        "job `analysis` sets `defaults`",
        id="job-defaults",
    ),
    pytest.param(
        LAST,
        LAST + "      - name: Say\n        run: echo done\n",
        "job `analysis`, step 3 runs a command; it may only use actions",
        id="run-step",
    ),
    pytest.param(
        LAST,
        step(f"actions/setup-go@{PIN} # v6.0.0") + LAST,
        "job `analysis`, step 2 uses actions/setup-go, which is not on the list",
        id="action-off-the-list",
    ),
    pytest.param(
        LAST,
        step(f"github/codeql-action/analyze@{PIN} # v4.38.3") + LAST,
        "job `analysis`, step 2 uses github/codeql-action/analyze, which is not on the list",
        id="other-action-of-a-listed-repository",
    ),
    pytest.param(
        f"uses: ossf/scorecard-action@{PIN} # v2.4.4",
        "uses: docker://ghcr.io/ossf/scorecard-action:v2.4.4",
        "job `analysis`, step 2 uses ghcr.io/ossf/scorecard-action, which is not on the list",
        id="action-as-an-image",
    ),
]


@pytest.mark.parametrize(("old", "new", "message"), PLANTED)
def test_broken_rule_fails(tree, old, new, message):
    assert GOOD.count(old) == 1
    done = judge(tree, GOOD.replace(old, new))
    assert done.returncode == 1, done.stdout
    assert done.stderr == f"FAIL: {WORKFLOW}: {message}\n"


LABELS = [
    pytest.param("ubuntu-20.04", "runs on `ubuntu-20.04`: no hosted Ubuntu label", id="too-old"),
    pytest.param("ubuntu-2404", "runs on `ubuntu-2404`: no hosted Ubuntu label", id="no-dot"),
    pytest.param("Ubuntu-24.04", "runs on `Ubuntu-24.04`: no hosted", id="other-case"),
    pytest.param("ubuntu-24.04-large", "runs on `ubuntu-24.04-large`: no hosted", id="larger"),
    pytest.param("macos-26", "runs on `macos-26`: no hosted Ubuntu label", id="other-system"),
    pytest.param("self-hosted", "runs on `self-hosted`: no hosted Ubuntu label", id="own-runner"),
    pytest.param(
        "${{ inputs.runner }}",
        "runs on `${{ inputs.runner }}`: no hosted Ubuntu label",
        id="expression",
    ),
    pytest.param("[ubuntu-24.04, x64]", "does not run on exactly one label", id="two-labels"),
    pytest.param("[]", "does not run on exactly one label", id="no-label"),
    pytest.param(
        "\n      group: large\n      labels: ubuntu-24.04",
        "does not run on exactly one label",
        id="runner-group",
    ),
]


@pytest.mark.parametrize(("label", "message"), LABELS)
def test_label_the_service_does_not_take_fails(tree, label, message):
    done = judge(tree, GOOD.replace(LABEL, f"    runs-on: {label}\n"))
    assert done.returncode == 1, done.stdout
    assert f"FAIL: {WORKFLOW}: job `analysis` {message}" in done.stderr


def test_job_without_a_label_fails(tree):
    done = judge(tree, GOOD.replace(LABEL, ""))
    assert done.returncode == 1, done.stdout
    assert "job `analysis` does not run on exactly one label" in done.stderr


@pytest.mark.parametrize(
    "label",
    ["ubuntu-22.04", "ubuntu-26.04", "ubuntu-latest", "ubuntu-24.04-arm", "[ubuntu-24.04]"],
)
def test_label_the_service_takes_passes(tree, label):
    done = judge(tree, GOOD.replace(LABEL, f"    runs-on: {label}\n"))
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "uses",
    [
        f"actions/create-github-app-token@{PIN} # v3.1.1",
        f"actions/upload-artifact@{PIN} # v7.0.2",
        f"github/codeql-action/upload-sarif@{PIN} # v4.38.3",
        f"step-security/harden-runner@{PIN} # v2.16.0",
    ],
)
def test_action_of_the_list_passes(tree, uses):
    done = judge(tree, GOOD.replace(LAST, LAST + step(uses)))
    assert done.returncode == 0, done.stderr


def test_rules_of_the_job_do_not_bind_another_job(tree):
    done = judge(tree, GOOD + OTHER)
    assert done.returncode == 0, done.stderr


def test_another_job_that_may_write_the_token_fails(tree):
    assert "      id-token: read\n" in OTHER
    done = judge(tree, GOOD + OTHER.replace("      id-token: read\n", "      id-token: write\n"))
    assert done.returncode == 1, done.stdout
    assert done.stderr == (
        f"FAIL: {WORKFLOW}: job `other` may write `id-token`; only job `analysis` may\n"
    )


def test_second_job_that_runs_the_action_fails(tree):
    second = "  again:\n    name: Again\n" + LABEL + "    steps:\n" + LAST
    done = judge(tree, GOOD + second)
    assert done.returncode == 1, done.stdout
    assert "jobs `analysis`, `again` all run the action" in done.stderr


def test_job_that_runs_the_image_alone_is_judged(tree):
    image = "uses: docker://gcr.io/openssf/scorecard-action:v2.4.4"
    done = judge(tree, GOOD.replace(f"uses: ossf/scorecard-action@{PIN} # v2.4.4", image))
    assert done.returncode == 1, done.stdout
    assert "step 2 uses gcr.io/openssf/scorecard-action, which is not on the list" in done.stderr


def test_every_broken_rule_is_reported_in_one_run(tree):
    workflow = GOOD.replace(TOP, "env:\n  A: b\n" + TOP).replace(LABEL, "    runs-on: macos-26\n")
    done = judge(tree, workflow + "      - name: Say\n        run: echo done\n")
    assert done.returncode == 1, done.stdout
    assert done.stderr.count("FAIL: ") == 3


def test_workflow_that_does_not_run_the_action_is_not_judged(tree):
    assert "run: |" in REUSABLE
    done = judge(tree, REUSABLE.replace("permissions:\n  contents: read\n", "env:\n  A: b\n"))
    assert done.returncode == 0, done.stderr
    assert "PASS: 0 workflow(s)" in done.stdout


def test_unreadable_workflow_is_an_error(tree):
    done = judge(tree, "---\non: [\n")
    assert done.returncode == 3
    assert done.stderr.startswith("ERROR: ")


def test_workflow_of_this_repository_is_judged_and_passes(tree):
    text = (ROOT / WORKFLOW).read_text(encoding="utf-8")
    done = judge(tree, text)
    assert done.returncode == 0, done.stderr
    assert "PASS: 1 workflow(s)" in done.stdout
    done = judge(tree, text.replace("runs-on: ubuntu-26.04", "runs-on: ${{ inputs.runner }}"))
    assert done.returncode == 1, done.stdout
