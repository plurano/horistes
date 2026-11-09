# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The commit-msg hook judges one message by the gate's rules (ADR-0072).

Each case plants a repository whose `lefthook.yml` holds this repository's
commit-msg job and whose Makefile, pyproject.toml, requirements, tests and tools
are links to this checkout. Its `.venv/bin/python` runs the interpreter of this
test run, and its stamp matches `requirements/tools.txt`. The hook then runs as
the squash helper runs it (`lefthook run commit-msg <file>`) and as git runs it
on a commit.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import ciwf
import pytest
import yaml
from conftest import ROOT, Tree, external, rooted

DEADLINE = 120  # seconds for one hook run: make, then a pytest start
JOB = ciwf.load(ROOT / "lefthook.yml")["commit-msg"]
RUN = JOB["commands"]["message-judge"]["run"]
PERSON = {
    "GIT_AUTHOR_NAME": "A Person",
    "GIT_AUTHOR_EMAIL": "person@example.org",
    "GIT_COMMITTER_NAME": "A Person",
    "GIT_COMMITTER_EMAIL": "person@example.org",
    "GIT_CONFIG_NOSYSTEM": "1",
}
SIGN_OFF = "Signed-off-by: A Person <person@example.org>"
CLEAN = f"fix(x): mend a thing\n\nThe body says why.\n\n{SIGN_OFF}\n"
CO_AUTHOR = "Co-authored-by: renovate[bot] <29139614+renovate[bot]@users.noreply.github.com>"
PLANTED = f"{CLEAN}{CO_AUTHOR}\n"
SPACED = "message file.txt"
LINKED = ("Makefile", "pyproject.toml", "requirements", "tests", "tools")
# Links, a shell stub and lefthook's shell lines need a POSIX system.
POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32", reason="the planted repository links files and runs a shell stub"
)


def planted(tmp_path: Path, run: str = RUN, pyproject: str | None = None) -> Tree:
    """A repository whose commit-msg job runs `run` against this checkout's rules.

    With `pyproject` given, the tree holds that text instead of a link.
    """
    tree = Tree(tmp_path / "repo")
    tree.git("init", "-b", "main")
    for name in LINKED:
        if name == "pyproject.toml" and pyproject is not None:
            tree.write({name: pyproject})
            continue
        (tree.root / name).symlink_to(ROOT / name)
    python = tree.root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    digest = hashlib.sha256((ROOT / "requirements" / "tools.txt").read_bytes()).hexdigest()
    job = {"commands": {"message-judge": {"run": run}}}
    return tree.write(
        {".venv/.stamp": f"{digest}\n", "lefthook.yml": yaml.safe_dump({"commit-msg": job})}
    )


def environment(tree: Tree) -> dict[str, str]:
    """PATH with lefthook first, HOME in the tree, and the author git hands the hook."""
    found = shutil.which(rooted(external("LEFTHOOK_BIN", "lefthook"))[0])
    assert found, "lefthook is missing"
    path = os.pathsep.join([str(Path(found).parent), os.environ.get("PATH", "")])
    return {"PATH": path, "HOME": str(tree.root), "LC_ALL": "C", **PERSON}


def hook(
    tree: Tree, message: str, name: str = SPACED, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Write the message to `name` in the tree and run `lefthook run commit-msg name`."""
    tree.write({name: message})
    return subprocess.run(
        ["lefthook", "run", "commit-msg", name],
        cwd=tree.root,
        env=environment(tree) | (env or {}),
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )


def test_job_names_the_message_file_in_quotes():
    assert RUN == "MESSAGE_FILE='{1}' make --no-print-directory check-message"


@POSIX_SHELL
def test_planted_bot_co_author_is_refused(tmp_path):
    done = hook(planted(tmp_path), PLANTED)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "trailer Co-authored-by" in output


def plain_asserts() -> str:
    """This repository's pyproject.toml with pytest's assertion rewriting turned off."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    line = 'addopts = "-q -p no:cacheprovider"\n'
    assert text.count(line) == 1
    return text.replace(line, 'addopts = "-q -p no:cacheprovider --assert=plain"\n')


@POSIX_SHELL
@pytest.mark.parametrize(
    ("inherited", "rewrite"),
    [({"PYTEST_ADDOPTS": "--collect-only"}, True), ({"PYTHONOPTIMIZE": "2"}, False)],
    ids=["addopts-collects-only", "optimize-without-rewriting"],
)
def test_inherited_settings_cannot_pass_a_planted_message(tmp_path, inherited, rewrite):
    """check-message clears each setting that would let pytest pass the planted message.

    pytest rewrites the asserts of test modules, and those run under -O too; the
    PYTHONOPTIMIZE case therefore turns rewriting off, so the cleared value is
    the barrier that holds.
    """
    tree = planted(tmp_path, pyproject=None if rewrite else plain_asserts())
    done = hook(tree, PLANTED, env=inherited)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "trailer Co-authored-by" in output


@POSIX_SHELL
def test_clean_message_passes(tmp_path):
    done = hook(planted(tmp_path), CLEAN)
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert "1 passed" in output, output


@POSIX_SHELL
def test_sign_off_of_another_author_is_refused(tmp_path):
    done = hook(planted(tmp_path), CLEAN, env={"GIT_AUTHOR_EMAIL": "other@example.net"})
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "is not the author other@example.net" in output


@POSIX_SHELL
def test_planted_unquoted_file_splits_at_the_space(tmp_path):
    done = hook(planted(tmp_path, RUN.replace("'{1}'", "{1}")), CLEAN)
    assert done.returncode != 0, done.stdout + done.stderr


@POSIX_SHELL
def test_missing_message_file_is_refused(tmp_path):
    tree = planted(tmp_path)
    done = subprocess.run(
        ["lefthook", "run", "commit-msg", "absent.txt"],
        cwd=tree.root,
        env=environment(tree),
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "absent.txt" in output


@POSIX_SHELL
@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_check_message_without_message_file_is_refused(tmp_path, value):
    tree = planted(tmp_path)
    env = environment(tree) | ({} if value is None else {"MESSAGE_FILE": value})
    done = subprocess.run(
        ["make", "--no-print-directory", "check-message"],
        cwd=tree.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "check-message needs MESSAGE_FILE" in done.stderr


@POSIX_SHELL
def test_check_message_reads_a_make_variable(tmp_path):
    tree = planted(tmp_path)
    tree.write({"message.txt": PLANTED})
    done = subprocess.run(
        ["make", "--no-print-directory", "check-message", "MESSAGE_FILE=message.txt"],
        cwd=tree.root,
        env=environment(tree),
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "trailer Co-authored-by" in done.stdout + done.stderr


@POSIX_SHELL
def test_check_message_refuses_a_stale_stamp(tmp_path):
    tree = planted(tmp_path).write({".venv/.stamp": "0" * 64 + "\n"})
    done = hook(tree, CLEAN)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "run make tools" in output


def commit(tree: Tree, message: str) -> subprocess.CompletedProcess[str]:
    """`git commit -F` of one new file in the planted repository, hooks installed."""
    env = environment(tree)
    tree.write({"message.txt": message, "file.txt": message})
    for arguments in (["lefthook", "install"], ["git", "add", "file.txt"]):
        subprocess.run(
            arguments, cwd=tree.root, env=env, check=True, timeout=DEADLINE, capture_output=True
        )
    return subprocess.run(
        ["git", "commit", "-q", "-F", "message.txt"],
        cwd=tree.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )


@POSIX_SHELL
@pytest.mark.parametrize(
    ("message", "lands"), [(CLEAN, True), (PLANTED, False)], ids=["clean", "planted"]
)
def test_git_commit_runs_the_judge(tmp_path, message, lands):
    tree = planted(tmp_path)
    done = commit(tree, message)
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=tree.root,
        env=environment(tree),
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )
    assert (done.returncode == 0) is lands, done.stdout + done.stderr
    assert (head.returncode == 0) is lands
