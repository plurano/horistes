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
import os
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


def gitleaks() -> str:
    """The gitleaks executable itself, with a version-manager shim resolved.

    A mise shim is a link to mise, which reads its trust state from HOME; the
    scan replaces HOME, so the shim fails there. The shim is resolved with
    mise which, and the result must run in an environment like the scan's.
    A missing or failing one is an error, never a skip.
    """
    found = shutil.which(external("GITLEAKS", "gitleaks")[0])
    assert found, "gitleaks is needed: set GITLEAKS or put it on the path"
    real = Path(found).resolve()
    if real.name == "mise":
        done = subprocess.run(
            [str(real), "which", "gitleaks"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert done.returncode == 0, f"{found} is a mise shim; mise which gitleaks: {done.stderr}"
        real = Path(done.stdout.strip()).resolve()
    home = Path(os.environ.get("TMPDIR") or "/tmp")
    probe = subprocess.run(
        [str(real), "version"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "HOME": str(home)},
        timeout=20,
    )
    assert probe.returncode == 0, (
        f"{found} resolves to {real}, which fails without the caller's HOME and PATH: "
        f"{probe.stdout}{probe.stderr}set GITLEAKS to the gitleaks executable itself"
    )
    return str(real)


def scan(tree: Tree, command: str | None = None) -> subprocess.CompletedProcess:
    """Run the step in the planted repository, with gitleaks as the only tool on the path."""
    found = gitleaks()
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
        [gitleaks(), "version"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert done.stdout.strip().removeprefix("v") == pinned.removeprefix("v")


def test_planted_token_is_not_written_in_this_file():
    assert planted_token() not in Path(__file__).read_text(encoding="utf-8")


def shims(tmp_path: Path, mise_which: str, monkeypatch) -> Path:
    """A path with only a mise-style shim: gitleaks links to a mise that refuses the shim call."""
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    real = tmp_path / "installs" / "gitleaks"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\necho 8.30.1\n", encoding="utf-8")
    real.chmod(0o755)
    mise = shim_dir / "mise"
    mise.write_text(
        "#!/bin/sh\n"
        'if [ "${0##*/}" = mise ] && [ "$1" = which ]; then\n'
        f"{mise_which}\n"
        "fi\n"
        "echo 'mise ERROR Config files are not trusted.' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    mise.chmod(0o755)
    (shim_dir / "gitleaks").symlink_to(mise)
    monkeypatch.setenv("PATH", str(shim_dir))
    monkeypatch.delenv("GITLEAKS", raising=False)
    return real


def test_a_shim_only_path_resolves_to_the_executable(tmp_path, monkeypatch):
    real = shims(tmp_path, 'echo "${0%/*}/../installs/gitleaks"; exit 0', monkeypatch)
    assert gitleaks() == str(real.resolve())


def test_a_shim_mise_cannot_resolve_fails_closed(tmp_path, monkeypatch):
    shims(tmp_path, "echo 'mise ERROR gitleaks is not installed' >&2; exit 1", monkeypatch)
    with pytest.raises(AssertionError, match="is a mise shim; mise which gitleaks: mise ERROR"):
        gitleaks()


def test_an_executable_that_needs_home_fails_closed(tmp_path, monkeypatch):
    real = tmp_path / "gitleaks"
    real.write_text(
        f'#!/bin/sh\n[ "$HOME" = "{tmp_path}" ] || exit 1\necho 8.30.1\n', encoding="utf-8"
    )
    real.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GITLEAKS", str(real))
    assert subprocess.run([str(real)], check=False, timeout=20).returncode == 0
    with pytest.raises(AssertionError, match="fails without the caller's HOME and PATH"):
        gitleaks()


def test_no_gitleaks_on_the_path_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("GITLEAKS", raising=False)
    with pytest.raises(AssertionError, match="gitleaks is needed"):
        gitleaks()
