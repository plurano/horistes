# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the standalone commit message judge tool (ADR-0074).

Verifies that tools/commit_message.py is standard-library-only, runs under
isolated Python (python3 -I -S -B), and implements the CLI contract with 3D
positive, negative, and boundary coverage.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

import commit_message
import pytest
from commit_message import FLOOR, MAX_MESSAGE, MESSAGE_BYTES, below_floor
from conftest import ROOT

SIGN_OFF = "Signed-off-by: A Person <person@example.org>"
CLEAN = f"feat(x): add a thing\n\nThe body says why.\n\n{SIGN_OFF}\n"
CO_AUTHOR = "Co-authored-by: renovate[bot] <29139614+renovate[bot]@users.noreply.github.com>"
PLANTED = f"{CLEAN}{CO_AUTHOR}\n"


def stdlib_violations(source: str) -> list[str]:
    """Return findings if source imports anything outside standard library or __future__."""
    tree = ast.parse(source)
    findings: list[str] = []
    stdlib = sys.stdlib_module_names | {"__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in stdlib:
                    findings.append(f"non-stdlib import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0:
                findings.append(f"relative import level {node.level}")
            elif node.module:
                top = node.module.split(".")[0]
                if top not in stdlib:
                    findings.append(f"non-stdlib import: {node.module}")
    return findings


def test_commit_message_tool_is_stdlib_only():
    source = (ROOT / "tools" / "commit_message.py").read_text(encoding="utf-8")
    assert stdlib_violations(source) == []


def test_stdlib_check_detects_non_stdlib_import():
    source = (ROOT / "tools" / "commit_message.py").read_text(encoding="utf-8")
    planted = "import yaml\n" + source
    findings = stdlib_violations(planted)
    assert any("yaml" in f for f in findings)


def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "A Person"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "person@example.org"], cwd=repo, check=True)
    return repo


def run_tool(
    repo: Path,
    arguments: list[str],
    flags: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    tool = str(ROOT / "tools" / "commit_message.py")
    call_flags = ["-I", "-S", "-B"] if flags is None else flags
    call_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "A Person",
        "GIT_AUTHOR_EMAIL": "person@example.org",
        "GIT_COMMITTER_NAME": "A Person",
        "GIT_COMMITTER_EMAIL": "person@example.org",
        **(env or {}),
    }
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs the judge tool
    return subprocess.run(
        [sys.executable, *call_flags, tool, *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=call_env,
    )


def test_floor_refuses_an_older_python():
    assert below_floor((3, 11, 9, "final", 0)) == (
        "the commit message judge needs Python 3.12 or newer (ADR-0074); this python3 is 3.11"
    )


def test_floor_takes_the_floor_and_newer():
    assert below_floor((*FLOOR, 0, "final", 0)) is None
    assert below_floor((3, 13, 1, "final", 0)) is None
    assert below_floor((4, 0)) is None


def test_floor_refuses_before_judging(tmp_path, monkeypatch, capsys):
    repo = git_repo(tmp_path)
    msg_file = repo / "clean.txt"
    msg_file.write_text(CLEAN, encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(commit_message, "FLOOR", (sys.version_info[0] + 1, 0))
    assert commit_message.main(["check", str(msg_file)]) == 2
    assert "or newer (ADR-0074); this python3 is" in capsys.readouterr().err


def test_cli_clean_message_passes(tmp_path):
    repo = git_repo(tmp_path)
    msg_file = repo / "clean.txt"
    msg_file.write_text(CLEAN, encoding="utf-8")
    done = run_tool(repo, ["check", str(msg_file)])
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stderr == ""


def test_cli_planted_bot_co_author_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    msg_file = repo / "bot.txt"
    msg_file.write_text(PLANTED, encoding="utf-8")
    done = run_tool(repo, ["check", str(msg_file)])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "trailer Co-authored-by" in done.stderr


def test_cli_unsigned_message_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    msg_file = repo / "unsigned.txt"
    msg_file.write_text("feat(x): add a thing\n\nNo sign-off here.\n", encoding="utf-8")
    done = run_tool(repo, ["check", str(msg_file)])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no trailer Signed-off-by" in done.stderr


def test_cli_missing_file_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    missing = repo / "absent.txt"
    done = run_tool(repo, ["check", str(missing)])
    assert done.returncode == 2, done.stdout + done.stderr
    assert str(missing) in done.stderr


def test_cli_directory_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    dir_path = repo / "sub_dir"
    dir_path.mkdir()
    done = run_tool(repo, ["check", str(dir_path)])
    assert done.returncode == 2, done.stdout + done.stderr
    assert str(dir_path) in done.stderr
    assert "not a regular file" in done.stderr


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this system")
def test_cli_fifo_is_refused_without_hanging(tmp_path):
    repo = git_repo(tmp_path)
    fifo_path = repo / "msg.fifo"
    os.mkfifo(fifo_path)
    started = time.monotonic()
    done = run_tool(repo, ["check", str(fifo_path)])
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"FIFO check took {elapsed}s"
    assert done.returncode == 2, done.stdout + done.stderr
    assert str(fifo_path) in done.stderr
    assert "not a regular file" in done.stderr


def test_cli_oversize_file_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    large_file = repo / "large.txt"
    large_file.write_bytes(b"x" * (MESSAGE_BYTES + 1))
    done = run_tool(repo, ["check", str(large_file)])
    assert done.returncode == 2, done.stdout + done.stderr
    assert str(large_file) in done.stderr
    assert f"over {MESSAGE_BYTES} bytes" in done.stderr


def test_cli_without_git_names_the_failure(tmp_path):
    """Negative: with no git on PATH the judge refuses with rc 2 and names the message file."""
    repo = git_repo(tmp_path)
    msg_file = repo / "clean.txt"
    msg_file.write_text(CLEAN, encoding="utf-8")
    empty = tmp_path / "no-git"
    empty.mkdir()
    done = run_tool(repo, ["check", str(msg_file)], env={"PATH": str(empty)})
    assert done.returncode == 2, done.stdout + done.stderr
    assert f"{msg_file}: the judge could not run" in done.stderr
    assert "Traceback" not in done.stderr


def test_cli_no_arguments_is_refused(tmp_path):
    repo = git_repo(tmp_path)
    done = run_tool(repo, [])
    assert done.returncode == 2, done.stdout + done.stderr


def test_cli_refuses_outside_git_repository(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    msg_file = empty / "clean.txt"
    msg_file.write_text(CLEAN, encoding="utf-8")
    done = run_tool(empty, ["check", str(msg_file)])
    assert done.returncode == 2, done.stdout + done.stderr
    assert "not a git repository" in done.stderr
    assert str(empty) in done.stderr


def test_cli_refuses_planted_under_optimize_flag(tmp_path):
    repo = git_repo(tmp_path)
    msg_file = repo / "planted.txt"
    msg_file.write_text(PLANTED, encoding="utf-8")
    done = run_tool(repo, ["check", str(msg_file)], flags=["-I", "-O", "-B"])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "trailer Co-authored-by" in done.stderr


def test_cli_refuses_oversize_under_optimize_flag(tmp_path):
    repo = git_repo(tmp_path)
    large_file = repo / "large.txt"
    large_file.write_bytes(b"x" * (MESSAGE_BYTES + 1))
    done = run_tool(repo, ["check", str(large_file)], flags=["-I", "-O", "-B"])
    assert done.returncode == 2, done.stdout + done.stderr
    assert str(large_file) in done.stderr
    assert f"over {MESSAGE_BYTES} bytes" in done.stderr


def test_cli_character_boundary(tmp_path):
    repo = git_repo(tmp_path)
    head, tail = "feat(x): add a thing\n\n", f"\n\n{SIGN_OFF}\n"
    exact_msg = head + "x" * (MAX_MESSAGE - len(head) - len(tail)) + tail
    exact_file = repo / "exact.txt"
    exact_file.write_text(exact_msg, encoding="utf-8")
    done_exact = run_tool(repo, ["check", str(exact_file)])
    assert done_exact.returncode == 0, done_exact.stdout + done_exact.stderr

    over_msg = head + "x" * (MAX_MESSAGE + 1 - len(head) - len(tail)) + tail
    over_file = repo / "over.txt"
    over_file.write_text(over_msg, encoding="utf-8")
    done_over = run_tool(repo, ["check", str(over_file)])
    assert done_over.returncode == 1, done_over.stdout + done_over.stderr
    assert f"over {MAX_MESSAGE}" in done_over.stderr


def two_repositories(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    """Repository `ours` (author ours@example.org), `other` (other@example.org), cwd in other.

    The message is signed off by the author of `ours`; the author comes from each
    repository's own config, as git reads it for the commit it is about to make.
    """
    (tmp_path / "ours").mkdir()
    (tmp_path / "other").mkdir()
    ours = git_repo(tmp_path / "ours")
    other = git_repo(tmp_path / "other")
    for repo, email in ((ours, "ours@example.org"), (other, "other@example.org")):
        subprocess.run(["git", "config", "user.email", email], cwd=repo, check=True, timeout=20)
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_DIR", "GIT_WORK_TREE", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    message = tmp_path / "message.txt"
    message.write_text(
        "fix(x): mend a thing\n\nThe body says why.\n\n"
        "Signed-off-by: A Person <ours@example.org>\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(other)
    return ours, other, message


def test_judge_follows_the_repository_git_dir_names(tmp_path, monkeypatch, capsys):
    """GIT_DIR and GIT_WORK_TREE, which git exports to its hooks, pick the repository judged."""
    ours, _, message = two_repositories(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_DIR", str(ours / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ours))
    assert commit_message.main(["check", str(message)]) == 0, capsys.readouterr().err


def test_judge_without_git_dir_judges_the_working_directory(tmp_path, monkeypatch, capsys):
    """Without GIT_DIR the judge reads the repository it runs in, whose author differs."""
    _, _, message = two_repositories(tmp_path, monkeypatch)
    assert commit_message.main(["check", str(message)]) == 1
    assert "is not the author other@example.org" in capsys.readouterr().err
