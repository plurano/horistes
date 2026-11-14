# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep install module."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import codacy_opengrep_common as common
import codacy_opengrep_install as installer
import pytest
from codacy_opengrep_helpers import (
    ORIGINAL_RUN,
    VERSION,
    FakeResponse,
    document,
    result,
    write,
)
from codacy_opengrep_helpers import installed as installed
from codacy_opengrep_helpers import no_external_calls as no_external_calls


@pytest.mark.parametrize(
    "fault",
    [
        "missing-stamp",
        "stale-stamp",
        "missing-cache",
        "bad-cache",
        "cli-version",
        "cli-missing",
        "adapter-version",
        "adapter-missing",
    ],
)
def test_check_install_refuses(installed: tuple, fault: str) -> None:
    root, versions = installed
    package = root / common.PACKAGE
    stamp, _ = installer.npm_stamp(root)
    binary = installer.cache_path(versions)
    if fault == "missing-stamp":
        stamp.unlink()
    elif fault == "stale-stamp":
        stamp.write_text("stale\n", encoding="utf-8")
    elif fault == "missing-cache":
        binary.unlink()
    elif fault == "bad-cache":
        binary.write_bytes(b"changed")
    elif fault == "cli-version":
        document(
            package, "node_modules/" + common.CLI_PACKAGE + "/package.json", {"version": "0.28.0"}
        )
    elif fault == "cli-missing":
        (package / "node_modules/.bin/codacy-analysis").unlink()
    elif fault == "adapter-version":
        write(package, common.ADAPTER, 'preferredVersion: "1.29.0",')
    else:
        (package / common.ADAPTER).unlink()
    with pytest.raises(common.ScanError, match="run make opengrep-tools"):
        installer.check_install(root)


def test_check_install_current_and_cache_default(
    installed: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, versions = installed
    installer.check_install(root)
    assert installer.stamp_current(root)
    assert installer.cached_binary(versions).read_bytes() == b"binary"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setattr(installer.Path, "home", lambda: Path("/synthetic-home"))
    assert str(installer.cache_path(versions)).startswith(
        "/synthetic-home/.cache/horistes/opengrep/"
    )


@pytest.mark.parametrize(
    "data", [None, {"dependencies": {}}, {"dependencies": {common.CLI_PACKAGE: "^0.29.0"}}]
)
def test_cli_pin_shape(installed: tuple, data: object) -> None:
    root, _ = installed
    document(root, common.PACKAGE + "/package.json", data)
    with pytest.raises(common.ScanError, match="exact"):
        installer.cli_path(root)


@pytest.mark.parametrize("data", [None, {}, {"version": "1.0.0"}])
def test_cli_installed_version_shape(installed: tuple, data: object) -> None:
    root, _ = installed
    document(root, common.PACKAGE + "/node_modules/" + common.CLI_PACKAGE + "/package.json", data)
    with pytest.raises(common.ScanError, match="installed, the pin"):
        installer.cli_path(root)


@pytest.mark.parametrize(
    "text",
    [
        "",
        'preferredVersion: "1.30.0", preferredVersion: "1.30.0",',
        "preferredVersion: variable,",
        'preferredVersion: "1.29.0",',
    ],
)
def test_preferred_version_literal(installed: tuple, text: str) -> None:
    root, versions = installed
    write(root / common.PACKAGE, common.ADAPTER, text)
    with pytest.raises(common.ScanError, match="preferredVersion"):
        installer.preferred_version(root, versions)


@pytest.mark.parametrize(
    "text",
    [
        "# comment\nOTHER=x\n",
        "OPENGREP_VERSION=oops\nOPENGREP_SHA256=" + "a" * 64 + "\nOPENGREP_URL=x",
        "OPENGREP_VERSION=1.30.0\nOPENGREP_SHA256=bad\nOPENGREP_URL=x",
        "OPENGREP_VERSION=1.30.0\nOPENGREP_SHA256="
        + "a" * 64
        + "\nOPENGREP_URL=https://example.invalid/x",
    ],
)
def test_tool_version_refusals(tmp_path: Path, text: str) -> None:
    write(tmp_path, common.TOOL_VERSIONS, text)
    with pytest.raises(common.ScanError, match="Opengrep|OPENGREP"):
        installer.tool_versions(tmp_path)


@pytest.mark.parametrize("chunks", [0, 3])
def test_download_cases_quick(tmp_path: Path, chunks: int) -> None:
    with FakeResponse(chunks) as response:
        digest, size = installer.fetch_within(
            "https://example.invalid/binary",
            tmp_path / "out",
            5,
            lambda *_args, **_kwargs: response,
        )
    assert (digest, size) == (hashlib.sha256(b"x" * chunks).hexdigest(), chunks)


def test_download_deadline(tmp_path: Path) -> None:
    with (
        FakeResponse(300, 0.01) as response,
        pytest.raises(common.ScanError, match="did not finish within"),
    ):
        installer.fetch_within(
            "https://example.invalid/slow",
            tmp_path / "out",
            0.02,
            lambda *_args, **_kwargs: response,
        )


def test_download_error(tmp_path: Path) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic refusal")

    with pytest.raises(common.ScanError, match="download failed"):
        installer.fetch_within("https://example.invalid/error", tmp_path / "out", 5, refused)


@pytest.mark.parametrize("cap,chunks,match", [(2, 3, "passed"), (3, 3, None), (0, 0, None)])
def test_download_byte_cap(
    monkeypatch: pytest.MonkeyPatch, cap: int, chunks: int, match: str | None
) -> None:
    monkeypatch.setattr(installer, "MAX_DOWNLOAD_BYTES", cap)
    with FakeResponse(chunks) as response:
        if match:
            with pytest.raises(common.ScanError, match=match):
                installer.copy_capped(response, io.BytesIO(), hashlib.sha256())
        else:
            assert installer.copy_capped(response, io.BytesIO(), hashlib.sha256()) == chunks


def test_download_read_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "MAX_READS", 2)
    with FakeResponse(3) as response, pytest.raises(common.ScanError, match="within 2 reads"):
        installer.copy_capped(response, io.BytesIO(), hashlib.sha256())


@pytest.mark.parametrize("bad", [False, True])
def test_install_fake_npm_and_opener(
    installed: tuple, monkeypatch: pytest.MonkeyPatch, bad: bool, capsys: pytest.CaptureFixture
) -> None:
    root, versions = installed
    stamp, _ = installer.npm_stamp(root)
    stamp.unlink()
    binary = installer.cache_path(versions)
    binary.write_bytes(b"old")
    commands = []

    def npm(argv: list, **kwargs: object) -> SimpleNamespace:
        commands.append((argv, kwargs["cwd"], kwargs["timeout"]))
        return result()

    calls = []

    def opener(url: str, timeout: float) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(b"wrong" if bad else b"binary")

    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/fake/npm")
    monkeypatch.setattr(common.subprocess, "run", npm)
    monkeypatch.setattr(installer.urllib.request, "urlopen", opener)
    if bad:
        with pytest.raises(common.ScanError, match="sha256 mismatch"):
            installer.install(root)
        assert binary.read_bytes() == b"old"
    else:
        installer.install(root)
        assert "installed pinned tools" in capsys.readouterr().out
        installer.check_install(root)
        assert binary.stat().st_mode & 0o777 == 0o755
    assert commands == [
        (
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            root / common.PACKAGE,
            common.NPM_TIMEOUT_SECONDS,
        )
    ]
    assert calls == [(versions["OPENGREP_URL"], common.DOWNLOAD_TIMEOUT_SECONDS)]
    assert not list(binary.parent.glob("horistes-opengrep-install-*"))


def test_noop_install(installed: tuple, capsys: pytest.CaptureFixture) -> None:
    root, _ = installed
    installer.install(root)
    assert capsys.readouterr().out == "codacy-opengrep: installation current; no-op\n"


@pytest.mark.parametrize("rc", [None, 1])
def test_install_npm_refusal(
    installed: tuple, monkeypatch: pytest.MonkeyPatch, rc: int | None
) -> None:
    root, _ = installed
    stamp, _ = installer.npm_stamp(root)
    stamp.unlink()
    monkeypatch.setattr(
        installer.shutil, "which", lambda _name: None if rc is None else "/fake/npm"
    )
    monkeypatch.setattr(installer, "npm_run", lambda *_args: result(rc=1))
    with pytest.raises(common.ScanError, match="npm"):
        installer.install_npm(root)
    assert not stamp.exists()


@pytest.mark.parametrize("fault", [None, "copy", "version", "runtime"])
def test_codacy_home(installed: tuple, monkeypatch: pytest.MonkeyPatch, fault: str | None) -> None:
    _, versions = installed
    monkeypatch.setattr(
        installer,
        "engine_run",
        lambda *_args: result(
            "bad" if fault == "version" else VERSION + "\n", rc=1 if fault == "runtime" else 0
        ),
    )
    if fault == "copy":
        monkeypatch.setattr(
            installer.shutil, "copyfile", lambda _src, dest: dest.write_bytes(b"tampered")
        )
    if fault:
        with (
            pytest.raises(common.ScanError, match="sha256|cannot run"),
            installer.codacy_home(versions),
        ):
            pytest.fail("unverified binary escaped")
    else:
        with installer.codacy_home(versions) as home:
            assert (home / "tools/Semgrep/opengrep-release").read_bytes() == b"binary"
            assert "--taint-intrafile" in (home / "tools/Semgrep/opengrep").read_text()
        assert not home.exists()


def test_wrapper_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shlex

    directory = tmp_path / "dir with space and 'quote"
    directory.mkdir()
    stub = write(directory, "opengrep-release", '#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    wrapper = installer.write_wrapper(directory, stub)
    assert wrapper.stat().st_mode & 0o777 == 0o755
    text = wrapper.read_text()
    commands = [
        shlex.split(line.strip()) for line in text.splitlines() if line.strip().startswith("exec ")
    ]
    assert commands == [
        ["exec", str(stub), "scan", "--taint-intrafile", "$@"],
        ["exec", str(stub), "$@"],
    ]
    monkeypatch.setattr(common.subprocess, "run", ORIGINAL_RUN)
    scanned = ORIGINAL_RUN(
        ["sh", str(wrapper), "scan", "--json", "a b"],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    version = common.engine_run(wrapper, directory)
    assert scanned.stdout.splitlines() == ["scan", "--taint-intrafile", "--json", "a b"]
    assert version.stdout.splitlines() == ["--version"]


@pytest.mark.parametrize("dependencies", [None, [], "invalid"])
def test_cli_rejects_malformed_dependency_table(installed: tuple, dependencies: object) -> None:
    root, _ = installed
    document(root, common.PACKAGE + "/package.json", {"dependencies": dependencies})
    with pytest.raises(common.ScanError, match="exact"):
        installer.cli_path(root)


def test_non_utf8_stamp_is_stale(installed: tuple) -> None:
    root, _ = installed
    stamp, _expected = installer.npm_stamp(root)
    stamp.write_bytes(b"\xff")
    assert not installer.stamp_current(root)


@pytest.mark.parametrize("fault", ["missing", "version", "manifest-shape"])
def test_damaged_stamped_tree(installed: tuple, fault: str) -> None:
    root, _ = installed
    target = root / common.PACKAGE / "node_modules/dependency/package.json"
    if fault == "missing":
        target.unlink()
    else:
        target.write_text("[]" if fault == "manifest-shape" else '{"version": "wrong"}')
    with pytest.raises(common.ScanError, match="run make opengrep-tools"):
        installer.check_install(root)


def test_install_repairs_stamped_tree(installed: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = installed
    target = root / common.PACKAGE / "node_modules/dependency/package.json"
    target.unlink()
    calls = []

    def npm(_root: Path) -> SimpleNamespace:
        calls.append(_root)
        target.write_text('{"version": "1.0.0"}')
        return result()

    monkeypatch.setattr(installer, "npm_run", npm)
    installer.install(root)
    assert calls == [root]
    installer.check_install(root)


@pytest.mark.parametrize("size", [0, common.MAX_PACKAGES, common.MAX_PACKAGES + 1])
def test_package_bound(installed: tuple, size: int) -> None:
    root, _ = installed
    packages = {f"node_modules/unversioned-{i}": {} for i in range(size)}
    document(root, common.PACKAGE + "/package-lock.json", {"packages": packages})
    if size == common.MAX_PACKAGES:
        installer.installed_tree(root)
    else:
        with pytest.raises(common.ScanError, match="packages must hold"):
            installer.installed_tree(root)


@pytest.mark.parametrize(
    "lock",
    [
        None,
        {},
        {"packages": None},
        {"packages": {"node_modules/x": None}},
        {"packages": {"../escape": {"version": "1"}}},
        {"packages": {"outside": {"version": "1"}}},
        {"packages": {"": {"version": "wrong"}}},
    ],
)
def test_invalid_lock_tree(installed: tuple, lock: object) -> None:
    root, _ = installed
    document(root, common.PACKAGE + "/package-lock.json", lock)
    with pytest.raises(common.ScanError):
        installer.installed_tree(root)


@pytest.mark.parametrize("held", [False, True])
def test_install_lock_deadline(
    installed: tuple, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    root, _ = installed
    monkeypatch.setattr(installer, "LOCK_TRIES", 1)
    monkeypatch.setattr(installer, "LOCK_SLEEP_SECONDS", 0)
    with installer.install_lock(root, exclusive=held):
        if held:
            with pytest.raises(common.ScanError, match=r"\.install\.lock"):
                installer.install(root)
        else:
            with installer.install_lock(root):
                pass
    installer.install(root)


def test_lock_releases_after_error(installed: tuple) -> None:
    root, _ = installed
    with pytest.raises(ValueError, match="synthetic"), installer.install_lock(root, exclusive=True):
        raise ValueError("synthetic")
    with installer.install_lock(root, exclusive=True):
        pass


def test_root_package_version_matches_lock(installed: tuple) -> None:
    root, _ = installed
    path = root / common.PACKAGE / "package.json"
    package = common.load_json(path)
    package["version"] = "0.0.0"
    document(root, common.PACKAGE + "/package.json", package)
    lock = common.load_json(root / common.PACKAGE / "package-lock.json")
    lock["packages"][""]["version"] = "0.0.0"
    document(root, common.PACKAGE + "/package-lock.json", lock)
    installer.installed_tree(root)
