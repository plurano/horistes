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
from conftest import ACTION, CHECKOUT, REUSABLE, ROOT, external, rooted

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
PY_ISC_GOOD = """\
ITEMS = [
    (
        "a"
        "b"
    ),
]
"""
PY_ISC_FINDING = """\
ITEMS = [
    "a"
    "b",
]
"""
PY_ISORT_BLOCK = """\
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

try:
    from yaml import YAMLError, safe_load_all
except ImportError:
    safe_load_all = None
    YAMLError = Exception
"""


def actionlint_sha_line() -> str:
    """The sha256 verification line from the actionlint installation in ci.yml."""
    gate = ROOT / ".github/workflows/ci.yml"
    for line in gate.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if "ACTIONLINT_SHA256" in stripped and "sha256sum" in stripped:
            return stripped
    raise AssertionError("actionlint sha256 check line not found in .github/workflows/ci.yml")


def actionlint(tree):
    """Run actionlint against the workflow file in the tree."""
    # nosemgrep: dangerous-subprocess-use-audit runs the Makefile actionlint on a planted workflow
    return subprocess.run(
        [*external("ACTIONLINT", "actionlint"), str(tree.root / WORKFLOW)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def zizmor(tree):
    """Run zizmor offline in pedantic mode against the planted tree root."""
    command = [
        *external("ZIZMOR", "zizmor"),
        "--offline",
        "--strict-collection",
        "--persona",
        "pedantic",
        str(tree.root),
    ]
    # nosemgrep: dangerous-subprocess-use-audit runs the Makefile zizmor offline on a planted tree
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)


def yamllint(tree, *args):
    """Run yamllint in strict mode against the given targets in the tree."""
    cmd = rooted(external("YAMLLINT", "yamllint"))
    target = list(args) if args else ["."]
    # nosemgrep: dangerous-subprocess-use-audit runs the Makefile yamllint on a planted tree
    return subprocess.run(
        [*cmd, "--strict", *target],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def ruff(tree, *args):
    """Run ruff against the planted tree."""
    cmd = rooted(external("RUFF", "ruff"))
    subcommand = list(args) if args else ["check", "."]
    # nosemgrep: dangerous-subprocess-use-audit runs the Makefile ruff on a planted tree
    return subprocess.run(
        [*cmd, *subcommand],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_planted_fixtures_differ_from_the_good_one():
    """Planted defect fixtures differ from the baseline passing fixtures."""
    assert len({REUSABLE, UNPINNED, INJECTION, CREDENTIALS, NO_JOB, BY_DIGEST, BY_TAG}) == 7
    assert len({YAML_GOOD, YAML_BROKEN_SYNTAX, YAML_RULE_VIOLATION}) == 3
    assert len({PY_GOOD, PY_LINT_FINDING}) == 2
    assert len({PY_ISC_GOOD, PY_ISC_FINDING}) == 2


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

    # nosemgrep: dangerous-subprocess-use-audit runs the ci.yml sha256 line on a planted archive
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
        timeout=60,
    )
    assert passed.returncode == 0, passed.stdout + passed.stderr

    # nosemgrep: dangerous-subprocess-use-audit runs the ci.yml sha256 line with a wrong digest
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
        timeout=60,
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


def test_ruff_isort_force_sort_within_sections_both_directions(tree):
    """The try/except import block passes with false and fails with true."""
    base = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    assert "force-sort-within-sections = false" in base

    tree.write({"ruff.toml": base, "sample.py": PY_ISORT_BLOCK})
    passes = ruff(tree, "check", "--select", "I001", ".")
    assert passes.returncode == 0, passes.stdout + passes.stderr

    forced = base.replace(
        "force-sort-within-sections = false",
        "force-sort-within-sections = true",
    )
    tree.write({"ruff.toml": forced})
    fails = ruff(tree, "check", "--select", "I001", ".")
    assert fails.returncode != 0
    assert "I001" in fails.stdout


def test_ruff_isc_select_both_directions(tree):
    """PY_ISC_GOOD passes and PY_ISC_FINDING fails under ISC; passes without ISC."""
    base = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    select_line = ""
    for line in base.splitlines():
        if line.startswith("select ="):
            select_line = line
            break
    assert '"ISC"' in select_line

    tree.write({"ruff.toml": base, "sample.py": PY_ISC_GOOD})
    good = ruff(tree)
    assert good.returncode == 0, good.stdout + good.stderr

    bad = ruff(tree.write({"sample.py": PY_ISC_FINDING}))
    assert bad.returncode != 0
    assert "ISC004" in bad.stdout

    without_isc = base.replace(select_line, select_line.replace(', "ISC"', ""))
    tree.write({"ruff.toml": without_isc})
    passed = ruff(tree)
    assert passed.returncode == 0, passed.stdout + passed.stderr


def test_ruff_config_flag_overrides_nested_and_dot_configs(tree):
    """The --config flag forces ruff.toml over nested pyproject.toml and root .ruff.toml."""
    base = (ROOT / "ruff.toml").read_text(encoding="utf-8")
    tree.write(
        {
            "ruff.toml": base,
            "sub/pyproject.toml": (
                '[tool.ruff]\nline-length = 100\n[tool.ruff.lint]\nselect = ["E"]\n'
            ),
            "sub/sample.py": PY_ISC_FINDING,
        }
    )
    passes_nested = ruff(tree, "check", ".")
    assert passes_nested.returncode == 0, passes_nested.stdout + passes_nested.stderr
    fails_nested = ruff(tree, "check", "--config", "ruff.toml", ".")
    assert fails_nested.returncode != 0
    assert "ISC004" in fails_nested.stdout

    (tree.root / "sub" / "pyproject.toml").unlink()
    tree.write({".ruff.toml": '[lint]\nselect = ["E"]\n'})
    passes_dot = ruff(tree, "check", ".")
    assert passes_dot.returncode == 0, passes_dot.stdout + passes_dot.stderr
    fails_dot = ruff(tree, "check", "--config", "ruff.toml", ".")
    assert fails_dot.returncode != 0
    assert "ISC004" in fails_dot.stdout


def _recipe(makefile: str, target: str) -> list[str]:
    """The recipe lines of target (lines starting with a tab after the line starting <target>:)."""
    recipe: list[str] = []
    found = False
    for line in makefile.splitlines():
        if not found:
            if line.startswith(f"{target}:"):
                found = True
        else:
            if line.startswith("\t"):
                recipe.append(line.removeprefix("\t"))
            else:
                break
    return recipe


def test_makefile_lint_python_recipe_passes_config_flag():
    """Makefile lint-python recipe passes --config ruff.toml to ruff check and format."""
    content = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = _recipe(content, "lint-python")
    assert "$(BIN)/ruff check --config ruff.toml tools tests fuzz" in recipe
    assert "$(BIN)/ruff format --check --config ruff.toml tools tests fuzz" in recipe
    verify_prereqs: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("verify:"):
            curr = line
            idx = i
            verify_prereqs.extend(curr.split(":", 1)[1].removesuffix("\\").split())
            while curr.endswith("\\") and idx + 1 < len(lines):
                idx += 1
                curr = lines[idx]
                verify_prereqs.extend(curr.removesuffix("\\").split())
            break
    assert "lint-python" in verify_prereqs
    for line in content.splitlines():
        if "$(BIN)/ruff check" in line or "$(BIN)/ruff format" in line:
            assert "--config ruff.toml" in line


def test_rooted_reads_a_relative_tool_path_from_the_checkout() -> None:
    """Positive: a Makefile path such as `.venv/bin/ruff` is read from the checkout."""
    assert rooted([".venv/bin/ruff", "check"]) == [str(ROOT / ".venv/bin/ruff"), "check"]


@pytest.mark.parametrize(
    "head", ["ruff", os.path.abspath("bin/ruff")], ids=["bare-name", "absolute"]
)
def test_rooted_keeps_a_bare_name_or_absolute_path(head: str) -> None:
    """Negative: a name found on PATH, or an absolute path, is passed through."""
    assert rooted([head, "check"]) == [head, "check"]


def test_rooted_reads_the_makefile_separator_under_a_backslash_sep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary: where `os.sep` is a backslash, the Makefile's `/` path is still rooted."""
    monkeypatch.setattr(os, "sep", "\\")
    assert rooted([".venv/bin/ruff"]) == [str(ROOT / ".venv/bin/ruff")]
    assert rooted(["ruff"]) == ["ruff"]
