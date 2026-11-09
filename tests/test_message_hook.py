# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The commit-msg hook judges one message by the gate's rules (ADR-0072, ADR-0074).

Each case plants a repository whose `lefthook.yml` holds this repository's
commit-msg job and whose Makefile, pyproject.toml, requirements, tests and tools
are links to this checkout. It has no .venv: the hook runs on host python3 alone
(ADR-0074). The hook then runs as the squash helper runs it
(`lefthook run commit-msg <file>`) and as git runs it on a commit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import ciwf
import pytest
import yaml
from conftest import ROOT, Tree, external, rooted

DEADLINE = 120  # seconds for one hook run: make, then the judge
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
# A name that runs `touch planted` when a shell line pastes it inside single quotes.
HOSTILE = "a'$(touch planted)'b c.txt"
UNQUOTED = "MESSAGE_FILE={1} make --no-print-directory check-message"
QUOTED = "MESSAGE_FILE='{1}' make --no-print-directory check-message"
OTHER = "Other Person <other@example.org>"
OTHER_CLEAN = CLEAN.replace(SIGN_OFF, f"Signed-off-by: {OTHER}")
UNSIGNED = "Merge branch 'side'\n"
LINKED = ("Makefile", "pyproject.toml", "requirements", "tests", "tools")
# Links, a shell stub and lefthook's shell lines need a POSIX system.
POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32", reason="the planted repository links files and runs a shell stub"
)


def planted(tmp_path: Path, run: str = RUN, env: dict[str, str] | None = None) -> Tree:
    """A repository whose commit-msg job runs `run` against this checkout's rules.

    With `env` given, the job sets those variables for its command: they reach
    make and the judge as an inherited setting would, and lefthook itself, a
    Python wrapper in a virtual environment, never reads them.
    """
    tree = Tree(tmp_path / "repo")
    tree.git("init", "-b", "main")
    for name in LINKED:
        (tree.root / name).symlink_to(ROOT / name)
    command: dict[str, object] = {"run": run}
    if env:
        command["env"] = env
    job = {"commands": {"message-judge": command}}
    return tree.write({"lefthook.yml": yaml.safe_dump({"commit-msg": job})})


def environment(tree: Tree) -> dict[str, str]:
    """PATH with a lefthook link first, HOME in the tree, and the author git hands the hook.

    The link sits in a directory of its own next to the tree, so the directory
    lefthook came from (a virtual environment's bin, say) stays off PATH and the
    hook runs the host python3 (ADR-0074).
    """
    found = shutil.which(rooted(external("LEFTHOOK_BIN", "lefthook"))[0])
    assert found, "lefthook is missing"
    link = tree.root.parent / "bin" / "lefthook"
    if not link.is_symlink():
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(Path(found).absolute())
    path = os.pathsep.join([str(link.parent), os.environ.get("PATH", "")])
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


def test_job_reads_the_message_file_from_a_quoted_here_document():
    """The hook job sets MESSAGE_FILE from a quoted here-document before running make."""
    assert RUN == (
        "MESSAGE_FILE=$(cat <<'GIT_ARGUMENT'\n{1}\nGIT_ARGUMENT\n"
        ") make --no-print-directory check-message\n"
    )


@POSIX_SHELL
def test_message_file_name_with_shell_syntax_is_one_literal_path(tmp_path):
    """A message file name with shell syntax is read as a literal path without executing code."""
    tree = planted(tmp_path)
    done = hook(tree, CLEAN, name=HOSTILE)
    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert not (tree.root / "planted").exists()


@POSIX_SHELL
def test_planted_single_quoted_file_runs_shell_syntax_in_the_name(tmp_path):
    """A planted single-quoted hook command runs shell syntax in the file name and is refused."""
    tree = planted(tmp_path, QUOTED)
    done = hook(tree, CLEAN, name=HOSTILE)
    assert done.returncode != 0, done.stdout + done.stderr
    assert (tree.root / "planted").exists()


@POSIX_SHELL
def test_planted_bot_co_author_is_refused(tmp_path):
    """A message with a planted bot Co-authored-by trailer is refused by the hook."""
    done = hook(planted(tmp_path), PLANTED)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "trailer Co-authored-by" in output


def sitecustomize_env(tmp_path: Path) -> dict[str, str]:
    """A directory on PYTHONPATH holding a sitecustomize.py that calls os._exit(0)."""
    site = tmp_path / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "sitecustomize.py").write_text("import os; os._exit(0)\n", encoding="utf-8")
    return {"PYTHONPATH": str(site)}


def python_stub_env(tmp_path: Path) -> dict[str, str]:
    """A stub executable named by PYTHON that exits 0."""
    stub = tmp_path / "python_stub"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return {"PYTHON": str(stub)}


@POSIX_SHELL
@pytest.mark.parametrize(
    "env_builder",
    [
        pytest.param(lambda _: {"PYTHONOPTIMIZE": "2"}, id="optimize"),
        pytest.param(sitecustomize_env, id="pythonpath-sitecustomize"),
        pytest.param(python_stub_env, id="python-stub"),
    ],
)
def test_inherited_settings_cannot_pass_a_planted_message(tmp_path, env_builder):
    """The -I barrier and literal python3 refuse inherited settings on a planted message."""
    tree = planted(tmp_path, env=env_builder(tmp_path))
    done = hook(tree, PLANTED)
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "trailer Co-authored-by" in output


@POSIX_SHELL
def test_clean_message_passes(tmp_path):
    """A clean commit message signed off by its author passes the commit-msg hook."""
    done = hook(planted(tmp_path), CLEAN)
    output = done.stdout + done.stderr
    assert done.returncode == 0, output


@POSIX_SHELL
def test_sign_off_of_another_author_is_refused(tmp_path):
    """A commit message signed off by a different author is refused by the hook."""
    done = hook(planted(tmp_path), CLEAN, env={"GIT_AUTHOR_EMAIL": "other@example.net"})
    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "is not the author other@example.net" in output


@POSIX_SHELL
def test_planted_unquoted_file_splits_at_the_space(tmp_path):
    """A planted unquoted hook command splits on spaced file names and is refused."""
    done = hook(planted(tmp_path, UNQUOTED), CLEAN)
    assert done.returncode != 0, done.stdout + done.stderr


@POSIX_SHELL
def test_missing_message_file_is_refused(tmp_path):
    """Running the commit-msg hook with a missing message file is refused."""
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
    """make check-message is refused when MESSAGE_FILE is unset or empty."""
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
    """make check-message accepts MESSAGE_FILE passed as a Make variable."""
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
def test_check_message_runs_in_fresh_tree_without_venv(tmp_path):
    """A fresh tree without .venv passes CLEAN and refuses PLANTED."""
    tree = planted(tmp_path)
    clean_done = hook(tree, CLEAN)
    assert clean_done.returncode == 0, clean_done.stdout + clean_done.stderr
    planted_done = hook(tree, PLANTED)
    output = planted_done.stdout + planted_done.stderr
    assert planted_done.returncode != 0, output
    assert "trailer Co-authored-by" in output


def install(tree: Tree) -> dict[str, str]:
    """Install the planted repository's hooks; return the environment to run git in."""
    env = environment(tree)
    subprocess.run(
        ["lefthook", "install"],
        cwd=tree.root,
        env=env,
        check=True,
        timeout=DEADLINE,
        capture_output=True,
    )
    return env


def commit(tree: Tree, message: str, *options: str) -> subprocess.CompletedProcess[str]:
    """`git commit -F` of one changed file in the planted repository, hooks installed.

    `options` go to git commit, as `--author` or `--amend` do.
    """
    tree.write({"message.txt": message, "file.txt": message})
    env = install(tree)
    subprocess.run(
        ["git", "add", "file.txt"],
        cwd=tree.root,
        env=env,
        check=True,
        timeout=DEADLINE,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "commit", "-q", *options, "-F", "message.txt"],
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
    """git commit runs the hook judge, landing a clean message and refusing a planted one."""
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


@POSIX_SHELL
@pytest.mark.parametrize(
    ("message", "lands"),
    [(OTHER_CLEAN, True), (CLEAN, False)],
    ids=["author-signs", "committer-signs"],
)
def test_git_commit_judges_the_author_option(tmp_path, message, lands):
    """git commit --author requires a sign-off matching the specified author."""
    done = commit(planted(tmp_path), message, f"--author={OTHER}")
    output = done.stdout + done.stderr
    assert (done.returncode == 0) is lands, output
    assert lands or "is not the author other@example.org" in output


@POSIX_SHELL
@pytest.mark.parametrize(
    ("message", "lands"),
    [(OTHER_CLEAN, True), (CLEAN, False)],
    ids=["author-signs", "committer-signs"],
)
def test_git_commit_amend_judges_the_author_it_keeps(tmp_path, message, lands):
    """git commit --amend verifies the sign-off against the preserved original author."""
    tree = planted(tmp_path)
    first = commit(tree, OTHER_CLEAN, f"--author={OTHER}")
    assert first.returncode == 0, first.stdout + first.stderr
    done = commit(tree, message, "--amend")
    output = done.stdout + done.stderr
    assert (done.returncode == 0) is lands, output
    assert lands or "is not the author other@example.org" in output


def git_in(tree: Tree, env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    """One git call in the planted repository with the hooks' environment."""
    return subprocess.run(
        ["git", *arguments],
        cwd=tree.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
    )


def ran(done: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    """The finished call, after checking it exited 0."""
    assert done.returncode == 0, (done.args, done.stdout + done.stderr)
    return done


def two_branches(tmp_path: Path) -> tuple[Tree, dict[str, str]]:
    """A planted repository on main; main and side each add one file after the first commit."""
    tree = planted(tmp_path)
    ran(commit(tree, CLEAN))
    env = install(tree)
    ran(git_in(tree, env, "checkout", "-q", "-b", "side"))
    tree.write({"side.txt": "side\n"})
    ran(git_in(tree, env, "add", "side.txt"))
    ran(git_in(tree, env, "commit", "-q", "-F", "message.txt"))
    ran(git_in(tree, env, "checkout", "-q", "main"))
    tree.write({"main.txt": "main\n"})
    ran(git_in(tree, env, "add", "main.txt"))
    ran(git_in(tree, env, "commit", "-q", "-F", "message.txt"))
    return tree, env


def parent_count(tree: Tree, env: dict[str, str]) -> int:
    """How many parents HEAD has."""
    line = ran(git_in(tree, env, "rev-list", "--parents", "-n", "1", "HEAD")).stdout
    return len(line.split()) - 1


@POSIX_SHELL
def test_git_merge_of_a_clean_merge_without_sign_off_passes(tmp_path):
    """A clean git merge without a sign-off passes the judge and creates a two-parent commit."""
    tree, env = two_branches(tmp_path)
    done = git_in(tree, env, "merge", "--no-ff", "-q", "-m", UNSIGNED, "side")
    assert done.returncode == 0, done.stdout + done.stderr
    assert parent_count(tree, env) == 2


@POSIX_SHELL
@pytest.mark.parametrize(
    ("own", "lands"), [(False, True), (True, False)], ids=["clean", "own-content"]
)
def test_git_commit_of_a_merge_without_sign_off_judges_its_tree(tmp_path, own, lands):
    """A merge without sign-off passes if clean, but is refused when own content is staged."""
    tree, env = two_branches(tmp_path)
    ran(git_in(tree, env, "merge", "--no-ff", "--no-commit", "-q", "side"))
    tree.write({"message.txt": UNSIGNED, "extra.txt": "own content\n"})
    if own:
        ran(git_in(tree, env, "add", "extra.txt"))
    done = git_in(tree, env, "commit", "-q", "-F", "message.txt")
    output = done.stdout + done.stderr
    assert (done.returncode == 0) is lands, output
    assert parent_count(tree, env) == (2 if lands else 1)
    assert lands or "no trailer Signed-off-by" in output
