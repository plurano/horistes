# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Fixtures of the Codacy Opengrep scanner tests.

They apply to the test modules in this directory only: the autouse guard
replaces subprocess.run and urlopen, which the rest of the suite needs.
pytest finds them here, so no test module imports a fixture by name.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import codacy_opengrep_common as common
import codacy_opengrep_install as installer
import pytest

from .codacy_opengrep_helpers import VERSION, document, write


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
