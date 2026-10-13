# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The secret scan of the gate, run on planted repositories (decision record 0028).

The command of the step "Scan every commit for secrets" in the gate workflow
is run as written, with gitleaks from the environment the Makefile sets. The
planted token is built while the test runs, so the scan of this repository
never finds it in this file.
"""

from __future__ import annotations

import hashlib
import shutil
import string
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, Tree, external

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

GATE = ROOT / ".github/workflows/ci.yml"
STEP = "Scan every commit for secrets"
ALPHABET = string.ascii_letters + string.digits


def planted_token() -> str:
    """A token of the forge's personal-token shape, the same on every run."""
    digest = hashlib.sha256(b"planted").digest()
    return "ghp" + "_" + "".join(ALPHABET[byte % len(ALPHABET)] for byte in (digest * 2)[:36])


def scan_command(doc: dict | None = None) -> str:
    doc = doc or ciwf.load(GATE)
    steps = [step for step in doc["jobs"]["verify"]["steps"] if step.get("name") == STEP]
    assert len(steps) == 1, f"one step named {STEP!r}"
    return steps[0]["run"]


def scan(tree: Tree, command: str | None = None) -> subprocess.CompletedProcess:
    """Run the step in the planted repository, with gitleaks as the only tool on the path."""
    found = shutil.which(external("GITLEAKS", "gitleaks")[0])
    assert found, "gitleaks is needed: set GITLEAKS or put it on the path"
    bin_dir = tree.root.parent / f"{tree.root.name}-bin"
    bin_dir.mkdir(exist_ok=True)
    for name, target in (("gitleaks", found), ("git", shutil.which("git"))):
        link = bin_dir / name
        if not link.exists():
            link.symlink_to(target)
    bash = shutil.which("bash")
    assert bash, "the step is a bash script"
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", command or scan_command()],
        cwd=tree.root,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(bin_dir), "HOME": str(bin_dir)},
        timeout=120,
    )


def committed(tree: Tree, files: dict[str, str], message: str = "planted") -> Tree:
    if not (tree.root / ".git").exists():
        tree.git("init", "--quiet")
    tree.write(files)
    tree.git("add", "--all")
    tree.git("commit", "--quiet", "--allow-empty", "--message", message)
    return tree


def test_clean_history_passes(tree):
    done = scan(committed(tree, {"app.py": "token = None\n"}))
    assert done.returncode == 0, done.stdout + done.stderr


def test_planted_token_fails(tree):
    done = scan(committed(tree, {"app.py": f'token = "{planted_token()}"\n'}))
    assert done.returncode == 1, done.stdout + done.stderr
    assert planted_token() not in done.stdout + done.stderr, "the finding is redacted"


def test_token_removed_by_a_later_commit_still_fails(tree):
    committed(tree, {"app.py": f'token = "{planted_token()}"\n'})
    done = scan(committed(tree, {"app.py": "token = None\n"}, "remove the token"))
    assert done.returncode == 1, done.stdout + done.stderr


def test_allow_comment_does_not_hide_a_token(tree):
    marker = "gitleaks" + ":allow"
    done = scan(committed(tree, {"app.py": f'token = "{planted_token()}"  # {marker}\n'}))
    assert done.returncode == 1, done.stdout + done.stderr


@pytest.mark.parametrize("name", [".gitleaks.toml", ".gitleaksignore"])
def test_rule_file_in_the_tree_is_refused(tree, name):
    done = scan(committed(tree, {"app.py": "token = None\n", name: 'title = "planted"\n'}))
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout.startswith(f"::error title=Secret scan refused::{name} "), done.stdout


def test_without_the_refusal_a_rule_file_hides_a_token(tree):
    """The defect the refusal fixes: a rule file with no rules turns a planted token green."""
    command = scan_command()
    loop = command[command.index("for file in") : command.index("done\n") + len("done\n")]
    files = {"app.py": f'token = "{planted_token()}"\n', ".gitleaks.toml": 'title = "no rules"\n'}
    done = scan(committed(tree, files), command.replace(loop, ""))
    assert done.returncode == 0, done.stdout + done.stderr


def test_gitleaks_is_the_release_the_gate_pins():
    pinned = ciwf.load(GATE)["env"]["GITLEAKS_VERSION"]
    done = subprocess.run(
        [*external("GITLEAKS", "gitleaks"), "version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert done.stdout.strip().removeprefix("v") == pinned


def test_planted_token_is_not_written_in_this_file():
    assert planted_token() not in Path(__file__).read_text(encoding="utf-8")
