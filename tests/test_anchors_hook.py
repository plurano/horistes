# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the pre-commit anchors hook in lefthook.yml."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import anchors
import ciwf
import pytest
import yaml
from conftest import ROOT, Tree, external, measured, rooted
from test_dev_mode import _child

ANCHORS_RUN = "make --no-print-directory check-anchors"
FORBIDDEN_KEYS = ("glob", "files", "skip", "only")


def _find_lefthook() -> str | None:
    """Resolve the lefthook binary the Makefile sets, else the one on PATH."""
    return shutil.which(rooted(external("LEFTHOOK_BIN", "lefthook"))[0])


POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32", reason="planted lefthook commands need a POSIX shell"
)


def _copy_file(src: Path, dst: Path) -> None:
    """Copy a single file into the planted tree, creating parent directories."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _init_planted_repo(tmp_path: Path) -> Tree:
    """Build a fresh git repo holding only lefthook, anchors tooling, answers and pinned files."""
    tree = Tree(tmp_path / "repo")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    _copy_file(ROOT / "lefthook.yml", tree.root / "lefthook.yml")
    _copy_file(ROOT / "Makefile", tree.root / "Makefile")
    _copy_file(ROOT / "requirements" / "tools.txt", tree.root / "requirements" / "tools.txt")
    _copy_file(ROOT / ".bestpractices.json", tree.root / ".bestpractices.json")
    (tree.root / ".gitignore").write_text(".venv\n", encoding="utf-8")
    shutil.copytree(
        ROOT / ".config" / "lefthook",
        tree.root / ".config" / "lefthook",
        ignore=ignore,
    )
    shutil.copytree(ROOT / "tools", tree.root / "tools", ignore=ignore)
    for rel_path in dict.fromkeys(pin.path for pin in anchors.PINS):
        dst = tree.root / rel_path
        if not dst.exists():
            _copy_file(ROOT / rel_path, dst)
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "init")
    (tree.root / ".venv").symlink_to((ROOT / ".venv").resolve())
    tree.write({"staged.txt": "staged\n"})
    tree.git("add", "staged.txt")
    return tree


def _run_planted_pre_commit(tree: Tree, *commands: str) -> subprocess.CompletedProcess[str]:
    """Run `lefthook run pre-commit` restricted to `commands` in the planted repository.

    `lefthook` runs by literal name from a dedicated link directory on PATH, in
    a fresh environment so no caller lefthook or git setting reaches the run.
    A missing lefthook fails the test, never skips it.
    """
    found = _find_lefthook()
    assert found, "lefthook is missing"
    link = tree.root.parent / "bin" / "lefthook"
    if not link.is_symlink():
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(Path(found).resolve())
    selected = commands or ("anchors",)
    cmd = ["lefthook", "run", "pre-commit"]
    for name in selected:
        cmd.extend(["--command", name])
    path_parts = [str(link.parent)]
    if sys.executable:
        path_parts.append(str(Path(sys.executable).parent))
    path_parts.append(os.environ.get("PATH", ""))
    env = measured(
        {
            "PATH": os.pathsep.join(path_parts),
            "HOME": str(tree.root),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return _child(cmd, cwd=tree.root, env=env)


def _committed_text(tree: Tree, rel: str) -> str:
    """Return the blob at HEAD for one tracked path."""
    return tree.git("show", f"HEAD:{rel}")


def _insert_drift(tree: Tree) -> None:
    """Insert one blank line at the top of the first pinned file, above every range it anchors."""
    target = tree.root / anchors.PINS[0].path
    target.write_text("\n" + target.read_text(encoding="utf-8"), encoding="utf-8")


def test_lefthook_pre_commit_anchors_config() -> None:
    """Validate lefthook.yml pre-commit.commands.anchors run string and absence of filters."""
    doc = ciwf.load(ROOT / "lefthook.yml")
    commands = doc.get("pre-commit", {}).get("commands", {})
    assert "anchors" in commands
    anchors_cmd = commands["anchors"]
    assert isinstance(anchors_cmd, dict)
    assert anchors_cmd.get("run") == ANCHORS_RUN
    for key in FORBIDDEN_KEYS:
        assert key not in anchors_cmd


@POSIX_SHELL
def test_planted_pre_commit_anchors_clean(tmp_path: Path) -> None:
    """Clean planted repository passes pre-commit restricted to the anchors command."""
    tree = _init_planted_repo(tmp_path)
    proc = _run_planted_pre_commit(tree)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@POSIX_SHELL
def test_planted_pre_commit_anchors_staged_drift_refused(tmp_path: Path) -> None:
    """Staged drift in a pinned file fails pre-commit with FAIL while the worktree is clean."""
    tree = _init_planted_repo(tmp_path)
    rel = anchors.PINS[0].path
    clean = _committed_text(tree, rel)
    _insert_drift(tree)
    tree.git("add", rel)
    (tree.root / rel).write_text(clean, encoding="utf-8")
    proc = _run_planted_pre_commit(tree)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "FAIL" in output


@POSIX_SHELL
def test_planted_pre_commit_anchors_unstaged_drift_passes(tmp_path: Path) -> None:
    """Drift only in the working tree, not staged, passes the anchors pre-commit job."""
    tree = _init_planted_repo(tmp_path)
    _insert_drift(tree)
    proc = _run_planted_pre_commit(tree)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output


@POSIX_SHELL
def test_planted_pre_commit_without_anchors_allows_drift(tmp_path: Path) -> None:
    """Removing anchors from planted lefthook.yml lets the same drift exit 0.

    lefthook 2.2.2 fails when `--command` matches zero jobs, so `reuse-lint`
    accompanies `anchors`: with `anchors` present the pair fails on drift, and
    with `anchors` removed the identical invocation exits 0.
    """
    tree = _init_planted_repo(tmp_path)
    rel = anchors.PINS[0].path
    clean = _committed_text(tree, rel)
    _insert_drift(tree)
    tree.git("add", rel)
    (tree.root / rel).write_text(clean, encoding="utf-8")
    before = _run_planted_pre_commit(tree, "anchors", "reuse-lint")
    assert before.returncode != 0, before.stdout + before.stderr
    assert "FAIL" in before.stdout + before.stderr

    lefthook_path = tree.root / "lefthook.yml"
    doc = ciwf.loads(lefthook_path.read_text(encoding="utf-8"), lefthook_path)
    assert "anchors" in doc["pre-commit"]["commands"]
    del doc["pre-commit"]["commands"]["anchors"]
    lefthook_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert (
        "anchors"
        not in ciwf.loads(lefthook_path.read_text(encoding="utf-8"), lefthook_path)["pre-commit"][
            "commands"
        ]
    )
    after = _run_planted_pre_commit(tree, "anchors", "reuse-lint")
    assert after.returncode == 0, after.stdout + after.stderr


def test_anchors_check_index_alternate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """check --index reads GIT_INDEX_FILE; the default index staying clean still passes."""
    tree = _init_planted_repo(tmp_path)
    rel = anchors.PINS[0].path
    alt = tree.root / "alt.index"
    shutil.copy2(tree.root / ".git" / "index", alt)
    env_alt = {**os.environ, "GIT_INDEX_FILE": str(alt)}
    _insert_drift(tree)
    _child(
        ["git", "-C", str(tree.root), "add", rel],
        cwd=tree.root,
        env=env_alt,
    )
    (tree.root / rel).write_text(_committed_text(tree, rel), encoding="utf-8")
    monkeypatch.setenv("GIT_INDEX_FILE", str(alt))
    assert anchors.main(["check", "--index", "--root", str(tree.root)]) == 1
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    assert anchors.main(["check", "--index", "--root", str(tree.root)]) == 0


def test_anchors_fix_index_usage() -> None:
    """fix --index is a usage error."""
    with pytest.raises(SystemExit) as caught:
        anchors.main(["fix", "--index"])
    assert caught.value.code == 2


@POSIX_SHELL
def test_planted_pre_commit_anchors_missing_venv(tmp_path: Path) -> None:
    """Without the tools environment, the anchors job names make tools, not import errors."""
    tree = _init_planted_repo(tmp_path)
    (tree.root / ".venv").unlink()
    proc = _run_planted_pre_commit(tree)
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, output
    assert "run make tools" in output
    assert "ModuleNotFoundError" not in output
