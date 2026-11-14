# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep common module."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import codacy_opengrep_common as common
import codacy_opengrep_install as installer
import pytest
from codacy_opengrep_helpers import (
    document,
    result,
    write,
)
from codacy_opengrep_helpers import installed as installed
from codacy_opengrep_helpers import no_external_calls as no_external_calls


@pytest.mark.parametrize(
    "name,contents",
    [("missing.json", None), ("bad.json", "{"), ("bad-utf8.json", b"\xff"), ("good.json", "[]")],
)
def test_load_json(tmp_path: Path, name: str, contents: object) -> None:
    target = tmp_path / name
    if isinstance(contents, bytes):
        target.write_bytes(contents)
    elif contents is not None:
        target.write_text(contents, encoding="utf-8")
    if name == "good.json":
        assert common.load_json(target) == []
    else:
        with pytest.raises(common.ScanError, match="cannot read"):
            common.load_json(target)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"tools": []},
        {"tools": [None]},
        {"tools": [{}, {}]},
        {"tools": [{"useLocalConfigurationFile": True}]},
    ],
)
def test_config_tool_refusals(tmp_path: Path, data: object) -> None:
    document(tmp_path, common.CONFIG, data)
    with pytest.raises(common.ScanError):
        common.config_tool(tmp_path)


@pytest.mark.parametrize(
    "patterns",
    [
        None,
        [],
        [{"patternId": 1}],
        [{"patternId": "z"}, {"patternId": "a"}],
        [{"patternId": "a"}, {"patternId": "a"}],
    ],
)
def test_config_ids_refusals(tmp_path: Path, patterns: object) -> None:
    document(
        tmp_path,
        common.CONFIG,
        {"tools": [{"useLocalConfigurationFile": False, "patterns": patterns}]},
    )
    with pytest.raises(common.ScanError, match="pattern"):
        common.config_ids(tmp_path)


def test_drift_listing(tmp_path: Path) -> None:
    listed = write(tmp_path, "ids.txt", "a\nz\n")
    config = {
        "tools": [
            {
                "useLocalConfigurationFile": False,
                "patterns": [{"patternId": "a"}, {"patternId": "z"}],
            }
        ],
        "metadata": {"listingSha256": installer.file_sha256(listed), "listingCount": 2},
    }
    document(tmp_path, common.CONFIG, config)
    common.check_drift(tmp_path, listed)
    assert common.id_drift(["new"], ["old"]) == (["new"], ["old"])
    listed.write_text("a\n", encoding="utf-8")
    with pytest.raises(common.ScanError, match="metadata"):
        common.check_drift(tmp_path, listed)
    config["metadata"] = {"listingSha256": installer.file_sha256(listed), "listingCount": 1}
    document(tmp_path, common.CONFIG, config)
    with pytest.raises(common.ScanError, match="listing drift"):
        common.check_drift(tmp_path, listed)


@pytest.mark.parametrize("text", ["z\na\n", "a\na\nz\n"])
def test_drift_order_and_duplicates(tmp_path: Path, text: str) -> None:
    listing = write(tmp_path, "ids.txt", text)
    document(
        tmp_path,
        common.CONFIG,
        {
            "tools": [
                {
                    "useLocalConfigurationFile": False,
                    "patterns": [{"patternId": "a"}, {"patternId": "z"}],
                }
            ],
            "metadata": {
                "listingSha256": installer.file_sha256(listing),
                "listingCount": len(text.splitlines()),
            },
        },
    )
    with pytest.raises(common.ScanError, match="listing drift"):
        common.check_drift(tmp_path, listing)


def test_environment_barriers() -> None:
    assert common.git_env(
        {"GIT_DIR": "/other", "GIT_INDEX_FILE": "/other/index", "PATH": "/bin"}
    ) == {"PATH": "/bin"}
    filtered = common.cli_env(
        {"CODACY_UNUSED": "synthetic", "CODACY_GLOBAL_DIR": "/other", "PATH": "/bin"}, Path("/h")
    )
    assert filtered == {
        "PATH": "/bin",
        "CODACY_GLOBAL_DIR": "/h",
        "SEMGREP_LOG_FILE": "/h/opengrep.log",
    }


@pytest.mark.parametrize(
    "failure", [None, OSError("missing"), subprocess.TimeoutExpired("test", 1)]
)
def test_run_capture_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    seen = []

    def runner(argv: list, **kwargs: object) -> SimpleNamespace:
        seen.append((argv, kwargs))
        if failure:
            raise failure
        return result("done")

    monkeypatch.setattr(common.subprocess, "run", runner)
    if failure:
        with pytest.raises(common.ScanError, match="did not complete"):
            common.git(tmp_path, "status", stdin="x")
    else:
        assert common.git(tmp_path, "status", stdin="x").stdout == "done"
        argv, kwargs = seen[0]
        assert argv == ["git", "status"]
        assert kwargs == {
            "cwd": tmp_path,
            "env": common.git_env(dict(common.os.environ)),
            "input": "x",
            "capture_output": True,
            "text": True,
            "timeout": common.GIT_TIMEOUT_SECONDS,
            "check": False,
        }


@pytest.mark.parametrize("available", [False, True])
def test_git_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    monkeypatch.setattr(common.shutil, "which", lambda _name: "/bin/git" if available else None)
    seen = []
    monkeypatch.setattr(
        common.subprocess, "run", lambda *args, **kwargs: seen.append((args, kwargs)) or result()
    )
    if available:
        common.git(tmp_path, "cat-file", "--batch", stdin="HEAD\n")
        args, kwargs = seen[0]
        assert args == (["git", "cat-file", "--batch"],)
        assert kwargs["cwd"] == tmp_path and kwargs["timeout"] == common.GIT_TIMEOUT_SECONDS
        assert not any(key.startswith("GIT_") for key in kwargs["env"])
        assert kwargs["input"] == "HEAD\n"
    else:
        with pytest.raises(common.ScanError, match="git not found"):
            common.git(tmp_path, "ls-files")


def test_config_rejects_mixed_pattern_shapes(tmp_path: Path) -> None:
    document(
        tmp_path,
        common.CONFIG,
        {"tools": [{"useLocalConfigurationFile": False, "patterns": [{"patternId": "a"}, None]}]},
    )
    with pytest.raises(common.ScanError, match="invalid pattern list"):
        common.config_ids(tmp_path)


def test_drift_rejects_malformed_metadata(tmp_path: Path) -> None:
    listing = write(tmp_path, "ids.txt", "a\n")
    document(
        tmp_path,
        common.CONFIG,
        {
            "tools": [{"useLocalConfigurationFile": False, "patterns": [{"patternId": "a"}]}],
            "metadata": None,
        },
    )
    with pytest.raises(common.ScanError, match="metadata"):
        common.check_drift(tmp_path, listing)


@pytest.mark.parametrize("tool", ["npm", "engine", "cli"])
@pytest.mark.parametrize(
    "failure", [None, OSError("missing"), subprocess.TimeoutExpired("stub", 1)]
)
def test_narrow_process_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, failure: Exception | None
) -> None:
    seen = []

    def runner(argv: list, **kwargs: object) -> SimpleNamespace:
        seen.append((argv, kwargs))
        if failure:
            raise failure
        return result("ok")

    monkeypatch.setattr(common.subprocess, "run", runner)
    calls = {
        "npm": lambda: common.npm_run(tmp_path),
        "engine": lambda: common.engine_run(tmp_path / "wrapper", tmp_path),
        "cli": lambda: common.cli_run(
            tmp_path, tmp_path / "cli", tmp_path, tmp_path, tmp_path / "report"
        ),
    }
    if failure:
        with pytest.raises(common.ScanError, match="did not complete"):
            calls[tool]()
        return
    assert calls[tool]().stdout == "ok"
    argv, kwargs = seen[0]
    expected = expected_process_argv(tmp_path)
    assert argv == expected[tool]
    options = {"cwd": tmp_path, "capture_output": True, "text": True, "check": False}
    if tool == "npm":
        options.update(cwd=tmp_path / common.PACKAGE, timeout=common.NPM_TIMEOUT_SECONDS)
    else:
        options.update(
            env=common.cli_env(common.os.environ, tmp_path),
            timeout=common.GIT_TIMEOUT_SECONDS if tool == "engine" else common.CLI_TIMEOUT_SECONDS,
        )
    assert kwargs == options


def test_cli_drops_git_selection() -> None:
    supplied = {
        "GIT_DIR": "other",
        "GIT_WORK_TREE": "other",
        "GIT_INDEX_FILE": "other",
        "CODACY_TEST": "other",
        "PATH": "/bin",
    }
    assert common.cli_env(supplied, Path("/home/fixture")) == {
        "PATH": "/bin",
        "CODACY_GLOBAL_DIR": "/home/fixture",
        "SEMGREP_LOG_FILE": "/home/fixture/opengrep.log",
    }


def expected_process_argv(tmp_path: Path) -> dict[str, list[str]]:
    """Exact allowed commands for each narrow subprocess boundary."""
    return {
        "npm": ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        "engine": ["sh", str(tmp_path / "wrapper"), "--version"],
        "cli": [
            "node",
            str(tmp_path / "cli"),
            "analyze",
            ".",
            "--tool",
            "Semgrep",
            "--config-file",
            str(tmp_path / common.CONFIG),
            "--output-format",
            "json",
            "--output",
            str(tmp_path / "report"),
            "--max-file-size",
            "1MB",
            "--fail-if-missing",
            "--parallel-tools",
            "1",
            "--tool-timeout",
            str(common.TOOL_TIMEOUT_MS),
            "--no-log",
            "--no-update-notifier",
        ],
    }
