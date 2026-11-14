# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep shared fixtures module."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import codacy_opengrep_common as common
import codacy_opengrep_install as installer
import codacy_opengrep_report as reports
import pytest

TODAY = date(2026, 11, 14)


VERSION = installer.tool_versions(common.ROOT)["OPENGREP_VERSION"]


ORIGINAL_RUN = subprocess.run


def write(root: Path, name: str, text: str) -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def document(root: Path, name: str, data: object) -> Path:
    return write(root, name, json.dumps(data))


def result(stdout: str = "", rc: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)


def issue(text: str = "run(cmd)", rule: str = common.AUDIT_RULE, path: str = "a.py") -> dict:
    return {"patternId": rule, "filePath": path, "lineContent": text, "lineNumber": 1}


def report(issues: list | None = None, count: int = 1) -> dict:
    return {
        "capability": {
            "ready": [{"toolId": "Semgrep", "configSource": "codacy", "version": VERSION}]
        },
        "errors": [],
        "issues": issues if issues is not None else [issue()],
        "toolResults": [{"toolId": "Semgrep", "status": "success", "filesAnalyzed": count}],
        "metadata": {},
        "patternSummary": {},
        "skippedFiles": [],
    }


def entry(**changes: str) -> dict[str, str]:
    return {
        "patternId": common.AUDIT_RULE,
        "filePath": "a.py",
        "contentSha256": "0" * 64,
        "count": "1",
        "category": "D",
        "entered": "2026-11-14",
        "expires": "2027-03-14",
        "issue": "186",
        "reason": "Synthetic baseline for offline scanner validation.",
        **changes,
    }


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("unexpected external call")

    monkeypatch.setattr(installer.urllib.request, "urlopen", refused)
    monkeypatch.setattr(common.subprocess, "run", refused)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    package = root / common.PACKAGE
    document(
        root, common.PACKAGE + "/package.json", {"dependencies": {common.CLI_PACKAGE: "0.29.0"}}
    )
    document(
        root,
        common.PACKAGE + "/package-lock.json",
        {
            "lockfileVersion": 3,
            "packages": {
                "": {},
                "node_modules/@codacy/analysis-cli": {"version": "0.29.0"},
                "node_modules/dependency": {"version": "1.0.0"},
            },
        },
    )
    document(package, "node_modules/" + common.CLI_PACKAGE + "/package.json", {"version": "0.29.0"})
    document(package, "node_modules/dependency/package.json", {"version": "1.0.0"})
    write(package, "node_modules/.bin/codacy-analysis", "fake CLI")
    write(package, common.ADAPTER, f'preferredVersion: "{VERSION}",')
    digest = hashlib.sha256(b"binary").hexdigest()
    versions = {
        "OPENGREP_VERSION": VERSION,
        "OPENGREP_SHA256": digest,
        "OPENGREP_URL": common.DOWNLOAD_PREFIX + VERSION + "/opengrep_manylinux_x86",
    }
    write(root, common.TOOL_VERSIONS, "\n".join(f"{k}={v}" for k, v in versions.items()))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    binary = installer.cache_path(versions)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    stamp, expected = installer.npm_stamp(root)
    stamp.write_text(expected + "\n", encoding="utf-8")
    return root, versions


class FakeResponse:
    """Bounded in-memory download; optional delay exercises the absolute deadline."""

    def __init__(self, chunks: int, delay: float = 0) -> None:
        self.left, self.delay, self.stop = chunks, delay, threading.Event()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()

    def read(self, _size: int) -> bytes:
        if self.left <= 0 or self.stop.wait(self.delay):
            return b""
        self.left -= 1
        return b"x"


def fixture_files(data: dict) -> list[str]:
    """Reconstruct the bounded coverage set of a recorded CLI report."""
    _errors, issues, semgrep = reports.report_lists(data)
    names = sorted({i["filePath"] for i in issues})
    count = semgrep["filesAnalyzed"]
    assert len(names) <= count <= common.MAX_FILES
    return names + [f"clean-fixture-{i}.py" for i in range(count - len(names))]
