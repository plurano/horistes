# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Plant root-file leaks in disposable repositories, never in the source checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import leaked_root_names

COLON_NAMES = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot hold a colon"
)


def _git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


@pytest.fixture
def git_root(tmp_path: Path) -> Path:
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True,
        check=True,
        timeout=30,
        env=_git_env(),
    )
    return tmp_path


def test_planted_untracked_root_file_is_named(git_root: Path):
    before = set(os.listdir(git_root))
    (git_root / "planted.txt").write_text("leaked output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == ["planted.txt"]


@COLON_NAMES
@pytest.mark.parametrize("name", [":(literal)planted.txt", ":x"])
def test_planted_pathspec_magic_root_name_is_named(git_root: Path, name: str):
    before = set(os.listdir(git_root))
    (git_root / name).write_text("leaked output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == [name]


@COLON_NAMES
@pytest.mark.parametrize("name", [":(literal)planted.txt", ":x"])
def test_new_ignored_pathspec_magic_root_name_is_not_named(git_root: Path, name: str):
    (git_root / ".gitignore").write_text(f"{name}\n", encoding="utf-8")
    before = set(os.listdir(git_root))
    (git_root / name).write_text("ignored output", encoding="utf-8")
    ignored = subprocess.run(
        ["git", "-C", str(git_root), "check-ignore", "-v", "--", f"./{name}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=_git_env(),
    )
    assert ignored.returncode == 0
    assert ignored.stdout == f".gitignore:1:{name}\t./{name}\n"
    assert leaked_root_names(git_root, before) == []


def test_planted_glob_character_root_name_is_named(git_root: Path):
    before = set(os.listdir(git_root))
    (git_root / "a[b]").write_text("leaked output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == ["a[b]"]


def test_new_ignored_root_name_is_not_named(git_root: Path):
    (git_root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    before = set(os.listdir(git_root))
    (git_root / "ignored.txt").write_text("ignored output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == []


def test_planted_untracked_root_directory_is_named(git_root: Path):
    before = set(os.listdir(git_root))
    directory = git_root / "leak"
    directory.mkdir()
    (directory / "out.txt").write_text("leaked output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == ["leak"]


def test_new_self_ignoring_cache_directory_is_not_named(git_root: Path):
    before = set(os.listdir(git_root))
    directory = git_root / ".ruff_cache"
    directory.mkdir()
    (directory / ".gitignore").write_text("*\n", encoding="utf-8")
    (directory / "cache").write_text("cached output", encoding="utf-8")
    assert leaked_root_names(git_root, before) == []


def test_new_empty_root_directory_is_not_named(git_root: Path):
    before = set(os.listdir(git_root))
    (git_root / "empty").mkdir()
    assert leaked_root_names(git_root, before) == []


def test_no_new_root_name_is_not_a_leak(git_root: Path):
    assert leaked_root_names(git_root, set(os.listdir(git_root))) == []


def test_changed_preexisting_root_name_is_not_a_leak(git_root: Path):
    path = git_root / "existing.txt"
    path.write_text("before", encoding="utf-8")
    before = set(os.listdir(git_root))
    path.write_text("after", encoding="utf-8")
    assert leaked_root_names(git_root, before) == []


def test_root_outside_git_fails_closed(tmp_path: Path):
    before = set(os.listdir(tmp_path))
    (tmp_path / "planted.txt").write_text("leaked output", encoding="utf-8")
    assert leaked_root_names(tmp_path, before) == ["planted.txt"]


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 30)]
)
def test_unavailable_ignore_check_fails_closed(git_root: Path, monkeypatch, failure):
    (git_root / ".gitignore").write_text("planted.txt\n", encoding="utf-8")
    before = set(os.listdir(git_root))
    (git_root / "planted.txt").write_text("leaked output", encoding="utf-8")

    def unavailable(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", unavailable)
        assert leaked_root_names(git_root, before) == ["planted.txt"]
