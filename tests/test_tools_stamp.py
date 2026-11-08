# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for check-tools-stamp target and pre-push hook configuration."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import ciwf
import pytest
from conftest import ROOT

HASH_COMMAND = (
    "$$($(PYTHON) -c 'import hashlib, pathlib, sys; "
    "print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "
    "requirements/tools.txt)"
)


def _run_make_check(
    tmp_dir: Path,
    *,
    env: dict[str, str] | None = None,
    make_bin: str = "make",
) -> subprocess.CompletedProcess[str]:
    """Run `make -s -C <tmp_dir> -f <ROOT>/Makefile check-tools-stamp`."""
    return subprocess.run(
        [make_bin, "-s", "-C", str(tmp_dir), "-f", str(ROOT / "Makefile"), "check-tools-stamp"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _setup_tree(tmp_path: Path, tools_content: str = "pytest==8.0.0\n") -> tuple[Path, Path]:
    """Populate requirements/tools.txt and create .venv directory."""
    req_dir = tmp_path / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    tools_txt = req_dir / "tools.txt"
    tools_txt.write_text(tools_content, encoding="utf-8")
    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir(parents=True, exist_ok=True)
    stamp = venv_dir / ".stamp"
    return tools_txt, stamp


def test_check_tools_stamp_positive(tmp_path: Path) -> None:
    """Positive: stamp = sha256 hex + newline exits 0 with empty stderr."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h}\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_check_tools_stamp_negative_stale(tmp_path: Path) -> None:
    """Negative planted stale stamp: different content hash fails and names 'make tools'."""
    _, stamp = _setup_tree(tmp_path, "pytest==8.0.0\n")
    stale_hash = hashlib.sha256(b"pytest==7.0.0\n").hexdigest()
    stamp.write_text(f"{stale_hash}\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr
    assert ".venv/.stamp" in proc.stderr
    assert "requirements/tools.txt" in proc.stderr


def test_check_tools_stamp_negative_missing_stamp(tmp_path: Path) -> None:
    """Negative: .venv exists without .stamp -> rc != 0 and stderr names 'make tools'."""
    _setup_tree(tmp_path)
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_negative_missing_venv(tmp_path: Path) -> None:
    """Negative: missing .venv directory -> rc != 0 and stderr names 'make tools'."""
    req_dir = tmp_path / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "tools.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_negative_missing_requirements(tmp_path: Path) -> None:
    """Negative: no requirements/tools.txt and no stamp fails; empty equals empty is no match."""
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "requirements/tools.txt" in proc.stderr


def test_check_tools_stamp_boundary_without_trailing_newline(tmp_path: Path) -> None:
    """Boundary: stamp without trailing newline matches and exits 0."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(h, encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_check_tools_stamp_boundary_extra_line(tmp_path: Path) -> None:
    """Boundary: stamp with hash and extra line fails."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h}\nextra-line\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_boundary_uppercase_hex(tmp_path: Path) -> None:
    """Boundary: stamp with upper-case hex of right hash fails."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h.upper()}\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_boundary_empty_requirements(tmp_path: Path) -> None:
    """Boundary: empty requirements/tools.txt with its sha256 stamp exits 0."""
    tools_txt, stamp = _setup_tree(tmp_path, tools_content="")
    h = hashlib.sha256(b"").hexdigest()
    stamp.write_text(f"{h}\n", encoding="utf-8")
    proc = _run_make_check(tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_check_tools_stamp_writes_nothing_on_failure(tmp_path: Path) -> None:
    """Check writes nothing: tree listing and stamp bytes unchanged after failing run."""
    _, stamp = _setup_tree(tmp_path)
    stale_bytes = b"stale-stamp-bytes\n"
    stamp.write_bytes(stale_bytes)

    paths = sorted(tmp_path.rglob("*"))
    before_listing = [
        (p.relative_to(tmp_path).as_posix(), p.read_bytes() if p.is_file() else b"") for p in paths
    ]

    proc = _run_make_check(tmp_path)
    assert proc.returncode != 0

    after_paths = sorted(tmp_path.rglob("*"))
    after_listing = [
        (p.relative_to(tmp_path).as_posix(), p.read_bytes() if p.is_file() else b"")
        for p in after_paths
    ]

    assert after_listing == before_listing
    assert stamp.read_bytes() == stale_bytes


def test_lefthook_pre_push_tools_stamp() -> None:
    """Validate lefthook.yml pre-push configuration for tools-stamp."""
    doc = ciwf.load(ROOT / "lefthook.yml")
    pre_push = doc.get("pre-push", {})
    assert pre_push.get("piped") is True
    assert pre_push.get("parallel") is not True

    commands = pre_push.get("commands", {})
    assert "tools-stamp" in commands
    tools_cmd = commands["tools-stamp"]
    assert "check-tools-stamp" in tools_cmd.get("run", "")

    stamp_prio = tools_cmd.get("priority")
    assert isinstance(stamp_prio, int)
    assert stamp_prio >= 1

    for name, other_cmd in commands.items():
        if name == "tools-stamp":
            continue
        other_prio = other_cmd.get("priority")
        effective_prio = float("inf") if other_prio in (None, 0) else other_prio
        assert stamp_prio < effective_prio


def test_check_tools_stamp_without_sha256sum_on_path(tmp_path: Path) -> None:
    """Plant without sha256sum on PATH: passes with valid stamp, fails when stale."""
    py_path = shutil.which("python3")
    cat_path = shutil.which("cat")
    if py_path is None or cat_path is None:
        pytest.skip("python3 or cat not found on PATH")
    make_bin = shutil.which("make")
    if make_bin is None:
        pytest.skip("make not found on PATH")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python3").symlink_to(py_path)
    (bin_dir / "cat").symlink_to(cat_path)
    restricted_env = {"PATH": str(bin_dir)}

    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h}\n", encoding="utf-8")

    proc = _run_make_check(tmp_path, env=restricted_env, make_bin=make_bin)
    assert proc.returncode == 0
    assert proc.stderr == ""

    stale_hash = hashlib.sha256(b"stale-tools-content\n").hexdigest()
    stamp.write_text(f"{stale_hash}\n", encoding="utf-8")

    proc_stale = _run_make_check(tmp_path, env=restricted_env, make_bin=make_bin)
    assert proc_stale.returncode != 0
    assert "make tools" in proc_stale.stderr


def test_makefile_shares_hashlib_command() -> None:
    """Assert Makefile text holds the hashlib hash command exactly once."""
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile_text.count(HASH_COMMAND) == 1
    assert "sha256sum" not in makefile_text
    assert "shasum" not in makefile_text
    assert "check-tools-stamp" in makefile_text
