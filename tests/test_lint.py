# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The outside linters still refuse what the gate relies on them to refuse.

They run here as the Makefile runs them. A missing linter fails the test:
a skipped linter would read as a green gate.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest
from conftest import ACTION, CHECKOUT, REUSABLE, ROOT, external

WORKFLOW = ".github/workflows/licence.yml"
UNPINNED = REUSABLE.replace(CHECKOUT, "actions/checkout@v7")
INJECTION = REUSABLE.replace('test -d "$TARGET"', 'echo "${{ github.event.pull_request.title }}"')
CREDENTIALS = REUSABLE.replace("        with:\n          persist-credentials: false\n", "")
NO_JOB = REUSABLE.replace("    runs-on: ubuntu-24.04\n", "")
IMAGE = "docker://docker.io/library/alpine"
BY_DIGEST = f"{REUSABLE}      - name: Tool\n        uses: {IMAGE}@sha256:{'0123456789abcdef' * 4}\n"
BY_TAG = f"{REUSABLE}      - name: Tool\n        uses: {IMAGE}:3\n"
YAML_GOOD = "---\nkey: value\n"
YAML_BROKEN_SYNTAX = "---\nkey: [unclosed\n"
YAML_RULE_VIOLATION = "---\nkey: " + '"' + "a" * 130 + '"\n'
PY_GOOD = "VALUE: int = 1\n"
PY_LINT_FINDING = "import sys\n"


def actionlint_sha_line() -> str:
    """The sha256 verification line from the actionlint installation in ci.yml."""
    gate = ROOT / ".github/workflows/ci.yml"
    for line in gate.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "ACTIONLINT_SHA256" in stripped and "sha256sum" in stripped:
            return stripped
    raise AssertionError("actionlint sha256 check line not found in .github/workflows/ci.yml")


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


def rooted(cmd):
    """The command with a relative tool path read from the checkout, as the Makefile passes it.

    The planted tree is the working directory, so `.venv/bin/x` would name a
    file inside it.
    """
    head = cmd[0]
    if os.sep in head and not os.path.isabs(head):
        return [str(ROOT / head), *cmd[1:]]
    return cmd


def yamllint(tree, *args):
    cmd = rooted(external("YAMLLINT", "yamllint"))
    target = list(args) if args else ["."]
    return subprocess.run(
        [*cmd, "--strict", *target],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
    )


def ruff(tree, *args):
    cmd = rooted(external("RUFF", "ruff"))
    subcommand = list(args) if args else ["check", "."]
    return subprocess.run(
        [*cmd, *subcommand],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_planted_fixtures_differ_from_the_good_one():
    assert len({REUSABLE, UNPINNED, INJECTION, CREDENTIALS, NO_JOB, BY_DIGEST, BY_TAG}) == 7
    assert len({YAML_GOOD, YAML_BROKEN_SYNTAX, YAML_RULE_VIOLATION}) == 3
    assert len({PY_GOOD, PY_LINT_FINDING}) == 2


def test_good_workflow_and_action_pass_both_linters(tree):
    tree.write({WORKFLOW: REUSABLE, "actions/draft-stop/action.yml": ACTION})
    assert actionlint(tree).returncode == 0, actionlint(tree).stdout
    done = zizmor(tree)
    assert done.returncode == 0, done.stdout
    assert "No findings to report" in done.stdout


def test_image_named_by_digest_passes_both_linters(tree):
    tree.write({WORKFLOW: BY_DIGEST})
    assert actionlint(tree).returncode == 0, actionlint(tree).stdout
    done = zizmor(tree)
    assert done.returncode == 0, done.stdout
    assert "No findings to report" in done.stdout


@pytest.mark.parametrize(
    ("workflow", "finding"),
    [
        (UNPINNED, "unpinned-uses"),
        (INJECTION, "template-injection"),
        (CREDENTIALS, "artipacked"),
        (BY_TAG, "unpinned-images"),
    ],
    ids=["tag-pinned-action", "script-injection", "persisted-credentials", "tag-pinned-image"],
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


def test_actionlint_sha256_verification_refuses_planted_wrong_digest(tmp_path):
    """The sha256 verification line in ci.yml fails on a bad digest and passes on a match."""
    line = actionlint_sha_line()
    archive = tmp_path / "actionlint.tar.gz"
    archive.write_bytes(b"planted actionlint archive payload\n")
    right_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    wrong_digest = "0" * 64
    shell = shutil.which("bash") or "sh"

    passed = subprocess.run(
        [shell, "-c", line],
        env={
            "PATH": os.environ["PATH"],
            "ACTIONLINT_SHA256": right_digest,
            "archive": str(archive),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr

    failed = subprocess.run(
        [shell, "-c", line],
        env={
            "PATH": os.environ["PATH"],
            "ACTIONLINT_SHA256": wrong_digest,
            "archive": str(archive),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0


@pytest.mark.parametrize(
    ("content", "finding"),
    [
        (YAML_BROKEN_SYNTAX, "syntax error"),
        (YAML_RULE_VIOLATION, "line-length"),
    ],
    ids=["broken-yaml-syntax", "config-rule-violation"],
)
def test_yamllint_refuses_planted_defects(tree, content, finding):
    """yamllint fails on broken syntax and rule violations, and passes clean YAML."""
    tree.write({".yamllint.yaml": (ROOT / ".yamllint.yaml").read_text(encoding="utf-8")})
    good = yamllint(tree.write({"doc.yml": YAML_GOOD}))
    assert good.returncode == 0, good.stdout
    bad = yamllint(tree.write({"doc.yml": content}))
    assert bad.returncode != 0
    assert finding in bad.stdout


def test_ruff_refuses_a_planted_lint_finding(tree):
    """ruff check fails on a planted lint defect and passes clean Python source."""
    tree.write({"ruff.toml": (ROOT / "ruff.toml").read_text(encoding="utf-8")})
    good = ruff(tree.write({"sample.py": PY_GOOD}))
    assert good.returncode == 0, good.stdout + good.stderr
    bad = ruff(tree.write({"sample.py": PY_LINT_FINDING}))
    assert bad.returncode != 0
    assert "F401" in bad.stdout
