# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for check-tools-stamp target and pre-push hook configuration."""

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

HASH_COMMAND = (
    "$$($(PYTHON) -c 'import hashlib, pathlib, sys; "
    "print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "
    "requirements/tools.txt)"
)
HASH_FAILED = "cannot hash requirements/tools.txt"
# The planted lefthook commands are `touch`, `true` and `exit`, which need a
# POSIX shell; the Makefile gate that runs this suite needs one too.
POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32", reason="planted lefthook commands need a POSIX shell"
)


def _real_python3() -> str | None:
    """Resolve the running interpreter; PATH shims may need HOME dropped by bare-PATH tests."""
    return os.path.realpath(sys.executable) if sys.executable else None


def _plant_home_shim(shim_dir: Path, py_path: str) -> Path:
    """Plant a python3 shim that needs HOME before executing the real interpreter."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "${HOME+x}" != x ]; then exit 1; fi\n'
        f'exec {shlex.quote(py_path)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _plant_path_python3(path_dir: Path) -> Path:
    """Plant an executable python3 shell script that exits successfully."""
    path_dir.mkdir(parents=True, exist_ok=True)
    path_python = path_dir / "python3"
    path_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path_python.chmod(0o755)
    return path_python


def _run_make(
    tmp_dir: Path,
    target: str = "check-tools-stamp",
    *extra_args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `make -s -C <tmp_dir> -f <ROOT>/Makefile <target> [*extra_args]`.

    The target defaults to `check-tools-stamp`; `make` is looked up on the
    `PATH` of `env` when one is given.
    """
    return subprocess.run(
        [
            "make",
            "-s",
            "-C",
            str(tmp_dir),
            "-f",
            str(ROOT / "Makefile"),
            target,
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=env,
    )


def _setup_stub_venv(tmp_path: Path) -> Path:
    """Create executable .venv/bin/python stub script that exits 0."""
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


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
    proc = _run_make(tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_check_tools_stamp_negative_stale(tmp_path: Path) -> None:
    """Negative planted stale stamp: different content hash fails and names 'make tools'."""
    _, stamp = _setup_tree(tmp_path, "pytest==8.0.0\n")
    stale_hash = hashlib.sha256(b"pytest==7.0.0\n").hexdigest()
    stamp.write_text(f"{stale_hash}\n", encoding="utf-8")
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr
    assert ".venv/.stamp" in proc.stderr
    assert "requirements/tools.txt" in proc.stderr


def test_check_tools_stamp_negative_missing_stamp(tmp_path: Path) -> None:
    """Negative: .venv exists without .stamp -> rc != 0 and stderr names 'make tools'."""
    _setup_tree(tmp_path)
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_negative_missing_venv(tmp_path: Path) -> None:
    """Negative: missing .venv directory -> rc != 0 and stderr names 'make tools'."""
    req_dir = tmp_path / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "tools.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_negative_missing_requirements(tmp_path: Path) -> None:
    """Negative: no requirements/tools.txt and no stamp fails; empty equals empty is no match."""
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "requirements/tools.txt" in proc.stderr


def test_check_tools_stamp_boundary_without_trailing_newline(tmp_path: Path) -> None:
    """Boundary: stamp without trailing newline matches and exits 0."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(h, encoding="utf-8")
    proc = _run_make(tmp_path)
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_check_tools_stamp_boundary_extra_line(tmp_path: Path) -> None:
    """Boundary: stamp with hash and extra line fails."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h}\nextra-line\n", encoding="utf-8")
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_boundary_uppercase_hex(tmp_path: Path) -> None:
    """Boundary: stamp with upper-case hex of right hash fails."""
    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h.upper()}\n", encoding="utf-8")
    proc = _run_make(tmp_path)
    assert proc.returncode != 0
    assert "make tools" in proc.stderr


def test_check_tools_stamp_boundary_empty_requirements(tmp_path: Path) -> None:
    """Boundary: empty requirements/tools.txt with its sha256 stamp exits 0."""
    tools_txt, stamp = _setup_tree(tmp_path, tools_content="")
    h = hashlib.sha256(b"").hexdigest()
    stamp.write_text(f"{h}\n", encoding="utf-8")
    proc = _run_make(tmp_path)
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

    proc = _run_make(tmp_path)
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


def _init_repo_tree(tmp_path: Path) -> Tree:
    tree = Tree(tmp_path / "repo")
    tree.git("init", "-b", "main")
    tree.write({"file.txt": "committed\n"})
    tree.git("add", "file.txt")
    tree.git("commit", "-m", "init")
    return tree


def _planted_pre_push_config(
    tools_stamp_run: str, *, piped: bool | None = None
) -> dict[str, object]:
    doc = ciwf.load(ROOT / "lefthook.yml")
    pre_push = doc.get("pre-push", {})
    commands = pre_push.get("commands", {})
    planted_commands: dict[str, dict[str, object]] = {}
    for name in sorted(commands):
        entry: dict[str, object] = dict(commands[name])
        if name == "tools-stamp":
            entry["run"] = tools_stamp_run
        else:
            entry["run"] = f"touch {name}.ran"
        planted_commands[name] = entry
    cfg = dict(pre_push)
    cfg["commands"] = planted_commands
    if piped is not None:
        cfg["piped"] = piped
    return {"pre-push": cfg}


def _run_planted_lefthook(
    tree: Tree, config: dict[str, object]
) -> subprocess.CompletedProcess[str]:
    """Run `lefthook run pre-push` in the planted repository.

    `lefthook` runs by a literal name, found first on a PATH that starts with
    the directory of the binary the Makefile sets. The environment is built
    fresh, so no lefthook setting of the caller reaches the run. A missing
    lefthook fails the test, never skips it.
    """
    tree.write({"lefthook.yml": yaml.safe_dump(config)})
    found = shutil.which(rooted(external("LEFTHOOK_BIN", "lefthook"))[0])
    assert found, "lefthook is missing"
    env = {
        "PATH": os.pathsep.join([str(Path(found).parent), os.environ.get("PATH", "")]),
        "HOME": str(tree.root),
    }
    return subprocess.run(
        ["lefthook", "run", "pre-push"],
        cwd=tree.root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@POSIX_SHELL
def test_lefthook_pre_push_stop_on_failure_negative(tmp_path: Path) -> None:
    """Planted: tools-stamp exit 1 stops pre-push execution; no other command runs."""
    tree = _init_repo_tree(tmp_path)
    cfg = _planted_pre_push_config("exit 1")
    proc = _run_planted_lefthook(tree, cfg)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    ran_files = list(tree.root.glob("*.ran"))
    assert ran_files == [], f"expected no *.ran files, got {ran_files}"


@POSIX_SHELL
def test_lefthook_pre_push_stop_on_failure_positive(tmp_path: Path) -> None:
    """When tools-stamp succeeds with run: true, all subsequent commands run and exit 0."""
    tree = _init_repo_tree(tmp_path)
    cfg = _planted_pre_push_config("true")
    proc = _run_planted_lefthook(tree, cfg)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ran_names = {p.name for p in tree.root.glob("*.ran")}
    doc = ciwf.load(ROOT / "lefthook.yml")
    expected_others = {
        f"{name}.ran"
        for name in doc.get("pre-push", {}).get("commands", {})
        if name != "tools-stamp"
    }
    assert ran_names == expected_others


@POSIX_SHELL
def test_lefthook_pre_push_mutation_proof_without_piped(tmp_path: Path) -> None:
    """Planted: without piped: true, a failing tools-stamp does not prevent subsequent commands."""
    tree = _init_repo_tree(tmp_path)
    cfg = _planted_pre_push_config("exit 1", piped=False)
    proc = _run_planted_lefthook(tree, cfg)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    ran_files = list(tree.root.glob("*.ran"))
    assert len(ran_files) >= 1, "expected at least one *.ran file when piped is false"


@pytest.mark.skipif(sys.platform == "win32", reason="planted python3 shim needs a POSIX shell")
def test_check_tools_stamp_without_sha256sum_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plant without sha256sum on PATH: passes with valid stamp, fails when stale."""
    if not sys.executable:
        pytest.skip("running interpreter path unknown")
    shim_dir = tmp_path / "shim"
    shim = _plant_home_shim(shim_dir, os.path.realpath(sys.executable))
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    assert shutil.which("python3") == str(shim)
    py_path = _real_python3()
    cat_path = shutil.which("cat")
    if py_path is None:
        pytest.skip("running interpreter path unknown")
    if cat_path is None:
        pytest.skip("cat not found on PATH")
    make_bin = shutil.which("make")
    if make_bin is None:
        pytest.skip("make not found on PATH")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python3").symlink_to(py_path)
    (bin_dir / "cat").symlink_to(cat_path)
    (bin_dir / "make").symlink_to(make_bin)
    restricted_env = {"PATH": str(bin_dir)}

    tools_txt, stamp = _setup_tree(tmp_path)
    h = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    stamp.write_text(f"{h}\n", encoding="utf-8")

    proc = _run_make(tmp_path, env=restricted_env)
    assert proc.returncode == 0
    assert proc.stderr == ""

    stale_hash = hashlib.sha256(b"stale-tools-content\n").hexdigest()
    stamp.write_text(f"{stale_hash}\n", encoding="utf-8")

    proc_stale = _run_make(tmp_path, env=restricted_env)
    assert proc_stale.returncode != 0
    assert "make tools" in proc_stale.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="planted python3 shim needs a POSIX shell")
def test_real_python3_ignores_a_path_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plant a HOME-dependent PATH shim; the real interpreter works with only PATH set."""
    py_path = _real_python3()
    if py_path is None:
        pytest.skip("running interpreter path unknown")

    shim_dir = tmp_path / "shim"
    shim = _plant_home_shim(shim_dir, py_path)
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))
    assert shutil.which("python3") == str(shim)
    real_python = _real_python3()
    assert real_python is not None
    assert real_python != str(shim)
    assert real_python == os.path.realpath(sys.executable)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    linked_python = bin_dir / "python3"
    linked_python.symlink_to(real_python)
    proc = subprocess.run(
        ["./python3", "-c", "pass"],
        cwd=bin_dir,
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    shim_proc = subprocess.run(
        ["./python3", "-c", "pass"],
        cwd=shim_dir,
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert shim_proc.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="planted python3 needs a POSIX shell")
def test_real_python3_none_without_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return None without an executable path even when PATH contains python3."""
    pathbin = tmp_path / "pathbin"
    path_python = _plant_path_python3(pathbin)
    monkeypatch.setenv("PATH", str(pathbin))
    assert shutil.which("python3") == str(path_python)
    monkeypatch.setattr(sys, "executable", "")
    assert _real_python3() is None


@pytest.mark.skipif(sys.platform == "win32", reason="planted python3 needs a POSIX shell")
def test_real_python3_follows_sys_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve a linked sys.executable instead of the planted PATH interpreter."""
    if not sys.executable:
        pytest.skip("running interpreter path unknown")
    original_python = os.path.realpath(sys.executable)

    pathbin = tmp_path / "pathbin"
    path_python = _plant_path_python3(pathbin)
    monkeypatch.setenv("PATH", str(pathbin))
    assert shutil.which("python3") == str(path_python)

    exe_dir = tmp_path / "exe"
    exe_dir.mkdir()
    linked_python = exe_dir / "python3"
    try:
        linked_python.symlink_to(original_python)
    except OSError:
        if sys.platform == "win32":
            pytest.skip("interpreter symlink creation unsupported")
        raise
    monkeypatch.setattr(sys, "executable", str(linked_python))
    real_python = _real_python3()
    assert real_python == original_python
    assert real_python != str(path_python)


def test_makefile_shares_hashlib_command() -> None:
    """Assert Makefile text holds the hashlib hash command exactly once."""
    makefile_text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert makefile_text.count(HASH_COMMAND) == 1
    assert "sha256sum" not in makefile_text
    assert "shasum" not in makefile_text
    assert "check-tools-stamp" in makefile_text


def test_tools_negative_hash_command_fails(tmp_path: Path) -> None:
    """Negative: stub venv python, failing PYTHON -> rc != 0, stamp unchanged."""
    _setup_tree(tmp_path)
    _setup_stub_venv(tmp_path)
    old_stamp = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
    stamp = tmp_path / ".venv" / ".stamp"
    stamp.write_bytes(old_stamp)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    nopython = bin_dir / "nopython"
    nopython.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nopython.chmod(0o755)

    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(nopython))}")
    assert proc.returncode != 0
    assert stamp.read_bytes() == old_stamp
    assert HASH_FAILED in proc.stderr


def test_tools_negative_python_nonexistent(tmp_path: Path) -> None:
    """Negative: stub venv python, missing PYTHON -> rc != 0, stamp unchanged."""
    _setup_tree(tmp_path)
    _setup_stub_venv(tmp_path)
    old_stamp = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
    stamp = tmp_path / ".venv" / ".stamp"
    stamp.write_bytes(old_stamp)

    nonexistent = tmp_path / "bin" / "does-not-exist"
    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(nonexistent))}")
    assert proc.returncode != 0
    assert stamp.read_bytes() == old_stamp
    assert HASH_FAILED in proc.stderr


def test_tools_boundary_missing_requirements(tmp_path: Path) -> None:
    """Boundary: missing tools.txt, stub venv -> rc != 0, stamp unchanged, hash failure named."""
    py_path = _real_python3()
    if py_path is None:
        pytest.skip("running interpreter path unknown")

    _setup_stub_venv(tmp_path)
    stamp = tmp_path / ".venv" / ".stamp"
    old_stamp = b"old-stamp-bytes\n"
    stamp.write_bytes(old_stamp)

    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(py_path))}")
    assert proc.returncode != 0
    assert stamp.read_bytes() == old_stamp
    assert HASH_FAILED in proc.stderr


def test_tools_positive_stale_stamp(tmp_path: Path) -> None:
    """Positive: real python3, stub venv python, stale stamp -> rc 0, stamp updated."""
    py_path = _real_python3()
    if py_path is None:
        pytest.skip("running interpreter path unknown")

    tools_txt, stamp = _setup_tree(tmp_path)
    _setup_stub_venv(tmp_path)
    stale_stamp = b"stale-stamp-bytes\n"
    stamp.write_bytes(stale_stamp)

    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(py_path))}")
    assert proc.returncode == 0
    expected_hash = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    assert stamp.read_text(encoding="utf-8") == f"{expected_hash}\n"


def test_tools_positive_matching_stamp(tmp_path: Path) -> None:
    """Positive: matching stamp -> rc 0, stamp unchanged."""
    py_path = _real_python3()
    if py_path is None:
        pytest.skip("running interpreter path unknown")

    tools_txt, stamp = _setup_tree(tmp_path)
    _setup_stub_venv(tmp_path)
    expected_hash = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    matching_stamp = f"{expected_hash}\n"
    stamp.write_text(matching_stamp, encoding="utf-8")

    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(py_path))}")
    assert proc.returncode == 0
    assert stamp.read_text(encoding="utf-8") == matching_stamp


@pytest.mark.skipif(sys.platform == "win32", reason="quoted interpreter needs a POSIX shell")
def test_tools_python_path_with_space(tmp_path: Path) -> None:
    """Quoted spaced interpreter updates the stamp; an unquoted path leaves it stale."""
    py_path = _real_python3()
    if py_path is None:
        pytest.skip("running interpreter path unknown")

    spaced_dir = tmp_path / "dir with space"
    spaced_dir.mkdir()
    linked_python = spaced_dir / "python3"
    linked_python.symlink_to(py_path)
    tools_txt, stamp = _setup_tree(tmp_path)
    _setup_stub_venv(tmp_path)
    stale_stamp = b"stale-stamp-bytes\n"
    stamp.write_bytes(stale_stamp)

    proc = _run_make(tmp_path, "tools", f"PYTHON={shlex.quote(str(linked_python))}")
    assert proc.returncode == 0
    expected_hash = hashlib.sha256(tools_txt.read_bytes()).hexdigest()
    assert stamp.read_bytes() == f"{expected_hash}\n".encode()

    stamp.write_bytes(stale_stamp)
    unquoted_proc = _run_make(tmp_path, "tools", f"PYTHON={linked_python}")
    assert unquoted_proc.returncode != 0
    assert stamp.read_bytes() == stale_stamp
