# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline scanner checks; subprocesses, npm and downloads use local stand-ins."""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import subprocess
import sys
import threading
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import codacy_opengrep_scan as scan
import pytest


@pytest.mark.parametrize("copies", [0, 2])
def test_canary_refuses_changed_source(tmp_path: Path, copies: int) -> None:
    target = tmp_path / scan.CANARY_FILE
    target.parent.mkdir(parents=True)
    target.write_text(scan.CANARY_OLD * copies, encoding="utf-8")
    with pytest.raises(scan.ScanError, match="re-derive the canary"):
        scan.canary_source(tmp_path)


def test_checked_in_baseline_is_valid() -> None:
    entries = scan.load_baseline(scan.ROOT, date(2026, 11, 14))
    assert entries
    assert not scan.compare_baselines(entries, entries)
    assert not scan.compare_baselines(entries, {})
    assert scan.compare_baselines({}, entries)


TODAY = date(2026, 11, 14)
VERSION = scan.tool_versions(scan.ROOT)["OPENGREP_VERSION"]
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


def issue(text: str = "run(cmd)", rule: str = scan.AUDIT_RULE, path: str = "a.py") -> dict:
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
        "patternId": scan.AUDIT_RULE,
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

    monkeypatch.setattr(scan.urllib.request, "urlopen", refused)
    monkeypatch.setattr(scan.subprocess, "run", refused)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    package = root / scan.PACKAGE
    document(root, scan.PACKAGE + "/package.json", {"dependencies": {scan.CLI_PACKAGE: "0.29.0"}})
    document(root, scan.PACKAGE + "/package-lock.json", {"lockfileVersion": 3})
    document(package, "node_modules/" + scan.CLI_PACKAGE + "/package.json", {"version": "0.29.0"})
    write(package, "node_modules/.bin/codacy-analysis", "fake CLI")
    write(package, scan.ADAPTER, f'preferredVersion: "{VERSION}",')
    digest = hashlib.sha256(b"binary").hexdigest()
    versions = {
        "OPENGREP_VERSION": VERSION,
        "OPENGREP_SHA256": digest,
        "OPENGREP_URL": scan.DOWNLOAD_PREFIX + VERSION + "/opengrep_manylinux_x86",
    }
    write(root, scan.TOOL_VERSIONS, "\n".join(f"{k}={v}" for k, v in versions.items()))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    binary = scan.cache_path(versions)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    stamp, expected = scan.npm_stamp(root)
    stamp.write_text(expected + "\n", encoding="utf-8")
    return root, versions


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"version": 1, "findings": []},
        {"version": 2, "findings": [], "extra": 1},
        {"version": 2, "findings": None},
    ],
)
def test_baseline_shape_rejected(data: object) -> None:
    with pytest.raises(scan.ScanError, match="baseline must"):
        scan.baseline_findings(data)


@pytest.mark.parametrize("count", [0, scan.MAX_BASELINE_ENTRIES, scan.MAX_BASELINE_ENTRIES + 1])
def test_baseline_entry_cap(count: int) -> None:
    data = {"version": 2, "findings": [{}] * count}
    if count > scan.MAX_BASELINE_ENTRIES:
        with pytest.raises(scan.ScanError, match="at most"):
            scan.baseline_findings(data)
    else:
        assert len(scan.baseline_findings(data)) == count


@pytest.mark.parametrize(
    "changes",
    [
        {"line": "57"},
        {"expires": "2027-03-15"},
        {"category": "A", "expires": "2026-11-14"},
        {"expires": "2027-02-30"},
        {"entered": "bad"},
        {"patternId": "unknown"},
        {"filePath": "../outside.py"},
        {"filePath": "/outside.py"},
        {"category": "E"},
        {"reason": "short"},
        {"reason": "a" * 301},
        {"reason": "a" * 20 + "\n"},
        {"count": "0"},
        {"count": "100"},
        {"issue": "0"},
        {"contentSha256": "A" * 64},
    ],
)
def test_entry_cases_reject(changes: dict) -> None:
    assert scan.entry_problems(entry(**changes), {scan.AUDIT_RULE})


@pytest.mark.parametrize("data", [None, {}, {"reason": 2}])
def test_entry_shape(data: object) -> None:
    assert scan.entry_problems(data, None)


@pytest.mark.parametrize("category,days", [("A", 1), ("B", 3), ("C", 10), ("D", 120)])
def test_entry_category_boundaries(category: str, days: int) -> None:
    from datetime import timedelta

    sample = entry(
        category=category, expires=str(TODAY + timedelta(days=days)), count="99", reason="a" * 20
    )
    assert not scan.entry_problems(sample, {scan.AUDIT_RULE})
    assert not scan.entry_problems(dict(sample, reason="a" * 300), None)
    assert not scan.entry_expired(sample, date.fromisoformat(sample["expires"]))
    assert scan.entry_expired(sample, date.fromisoformat(sample["expires"]) + timedelta(days=1))
    assert not scan.entry_problems(dict(sample, patternId="old.rule"), None)


@pytest.mark.parametrize("text", ["2026-11-14", "2026-1-14", "2026-02-30", "", "2026-11-14\n"])
def test_parse_date(text: str) -> None:
    if text == "2026-11-14":
        assert scan.parse_today(text) == TODAY
    else:
        assert scan.parse_date(text) is None
        with pytest.raises(argparse.ArgumentTypeError):
            scan.parse_today(text)


@pytest.mark.parametrize(
    "samples,day,match",
    [
        ([entry(), entry()], TODAY, "duplicate key"),
        ([entry()], date(2027, 3, 15), "expired"),
        ([{}], TODAY, "keys must"),
        ([None], TODAY, "keys must"),
    ],
)
def test_parse_baseline_refuses(samples: list, day: date, match: str) -> None:
    with pytest.raises(scan.ScanError, match=match):
        scan.parse_baseline({"version": 2, "findings": samples}, None, day)


def test_baseline_counts_and_load(tmp_path: Path) -> None:
    sample = entry(count="2")
    document(
        tmp_path,
        scan.CONFIG,
        {
            "tools": [
                {"useLocalConfigurationFile": False, "patterns": [{"patternId": scan.AUDIT_RULE}]}
            ]
        },
    )
    document(tmp_path, scan.BASELINE, {"version": 2, "findings": [sample]})
    entries = scan.load_baseline(tmp_path, TODAY)
    assert scan.baseline_counts(entries) == Counter({scan.entry_key(sample): 2})
    assert scan.parse_baseline({"version": 2, "findings": [sample]}, None, None) == entries
    with pytest.raises(scan.ScanError, match="baseline.json"):
        scan.load_baseline(tmp_path, date(2027, 3, 15))


@pytest.mark.parametrize(
    "fields,refused",
    [
        ({}, False),
        ({"expires": "2099-01-01"}, True),
        ({"category": "C"}, True),
        ({"entered": "2026-11-15"}, True),
        ({"issue": "187"}, True),
        ({"reason": "another reason entirely"}, True),
        ({"count": "3"}, True),
        ({"count": "1"}, False),
    ],
)
def test_shrink_cases(fields: dict, refused: bool) -> None:
    old = entry(count="2")
    key = scan.entry_key(old)
    assert bool(scan.compare_baselines({key: old}, {key: dict(old, **fields)})) == refused


@pytest.mark.parametrize(
    "left,right,same",
    [
        ("    run(cmd,   shell=True)", "run(cmd, shell=True)", True),
        ('run("a  b")', 'run("a b")', False),
        ('x = """a\n  b"""', 'x = """a\n b"""', False),
        ('run("a\\"  b")', 'run("a\\" b")', False),
        ("x = '''a\n  b'''", "x = '''a\n b'''", False),
        ("", "  ", True),
    ],
)
def test_fingerprint_cases(left: str, right: str, same: bool) -> None:
    assert (scan.fingerprint(issue(left)) == scan.fingerprint(issue(right))) == same
    assert scan.fingerprint({**issue(), "multilineContent": [left]}) == scan.fingerprint(
        issue(left)
    )
    assert scan.fingerprint({**issue(left), "lineNumber": 999}) == scan.fingerprint(issue(left))


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"patternId": "p", "filePath": 1, "lineContent": "x"},
        {"patternId": "p", "filePath": "a.py", "multilineContent": [1]},
        {"patternId": 2, "filePath": "a.py", "lineContent": "x"},
    ],
)
def test_fingerprint_malformed(data: object) -> None:
    with pytest.raises(scan.ScanError, match="report issue"):
        scan.fingerprint(data)


@pytest.mark.parametrize("size", [scan.MAX_CONTENT_CHARS, scan.MAX_CONTENT_CHARS + 1])
def test_fingerprint_size(size: int) -> None:
    if size > scan.MAX_CONTENT_CHARS:
        with pytest.raises(scan.ScanError, match="at most"):
            scan.fingerprint(issue("x" * size))
    else:
        assert len(scan.fingerprint(issue("x" * size)).split("\0")[2]) == 64


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("errors", [{"message": "tool failed"}], "tool error:"),
        ("skippedFiles", [{"filePath": "large.py"}], "skippedFiles"),
        ("skippedFiles", None, "skippedFiles"),
        ("toolResults", [], "successful Semgrep"),
        ("toolResults", [{"toolId": "Semgrep", "status": "error"}], "successful Semgrep"),
        ("issues", None, "lacks errors"),
        ("errors", None, "lacks errors"),
        ("toolResults", None, "lacks errors"),
        ("issues", [None], "not an object"),
        ("capability", None, "ready Semgrep"),
        ("capability", {"ready": None}, "ready Semgrep"),
        ("capability", {"ready": [{"toolId": "other"}]}, "ready Semgrep"),
    ],
)
def test_report_cases_refuse(field: str, value: object, match: str) -> None:
    main = report()
    main[field] = value
    problems = scan.report_problems(
        main, ["a.py"], Counter({scan.fingerprint(issue()): 1}), VERSION
    )
    assert any(match in text for text in problems)


@pytest.mark.parametrize("value", [None, [], "x"])
def test_report_not_object(value: object) -> None:
    assert scan.report_problems(value, [], Counter(), VERSION) == ["report is not a JSON object"]


@pytest.mark.parametrize(
    "fields,match",
    [({"configSource": "local"}, "rules came from"), ({"version": "0.0.0"}, "Opengrep 0.0.0 ran")],
)
def test_with_capability(fields: dict, match: str) -> None:
    main = report()
    main["capability"]["ready"][0].update(fields)
    assert any(match in p for p in scan.capability_problems(main, VERSION))


def test_report_counts_coverage_and_repeat() -> None:
    main = report()
    baseline = Counter({scan.fingerprint(issue()): 1})
    assert not scan.report_problems(main, ["a.py"], baseline, VERSION)
    main["issues"].append(copy.deepcopy(main["issues"][0]))
    assert len(scan.report_problems(main, ["a.py"], baseline, VERSION)) == 1
    stale = baseline + Counter({"demo\0missing.py\0" + "0" * 64: 1})
    assert scan.report_problems(report(), ["a.py"], stale, VERSION)[0].startswith(
        "stale baseline entry:"
    )
    assert not scan.report_problems(report(), ["a.py"], stale, VERSION, enforce_stale=False)
    assert "covered" in scan.report_problems(report(), [], baseline, VERSION)[0]
    assert "unpassed" in scan.report_problems(report(), ["b.py"], baseline, VERSION)[0]


def test_canary_both_directions() -> None:
    issues = [issue(rule=rule, path=path) for rule, path in scan.CANARY_WANT]
    planted = report(issues, count=2)
    assert not scan.canary_problems(planted, Counter(), VERSION)
    for rule, path in scan.CANARY_WANT:
        missing = copy.deepcopy(planted)
        missing["issues"] = [i for i in issues if (i["patternId"], i["filePath"]) != (rule, path)]
        assert scan.canary_problems(missing, Counter(), VERSION)
    repeated = copy.deepcopy(planted)
    repeated["issues"].append(issues[0])
    assert scan.canary_problems(repeated, Counter(), VERSION)


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
        assert scan.load_json(target) == []
    else:
        with pytest.raises(scan.ScanError, match="cannot read"):
            scan.load_json(target)


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
    document(tmp_path, scan.CONFIG, data)
    with pytest.raises(scan.ScanError):
        scan.config_tool(tmp_path)


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
        scan.CONFIG,
        {"tools": [{"useLocalConfigurationFile": False, "patterns": patterns}]},
    )
    with pytest.raises(scan.ScanError, match="pattern"):
        scan.config_ids(tmp_path)


def test_drift_listing(tmp_path: Path) -> None:
    listed = write(tmp_path, "ids.txt", "a\nz\n")
    config = {
        "tools": [
            {
                "useLocalConfigurationFile": False,
                "patterns": [{"patternId": "a"}, {"patternId": "z"}],
            }
        ],
        "metadata": {"listingSha256": scan.file_sha256(listed), "listingCount": 2},
    }
    document(tmp_path, scan.CONFIG, config)
    scan.check_drift(tmp_path, listed)
    assert scan.id_drift(["new"], ["old"]) == (["new"], ["old"])
    listed.write_text("a\n", encoding="utf-8")
    with pytest.raises(scan.ScanError, match="metadata"):
        scan.check_drift(tmp_path, listed)
    config["metadata"] = {"listingSha256": scan.file_sha256(listed), "listingCount": 1}
    document(tmp_path, scan.CONFIG, config)
    with pytest.raises(scan.ScanError, match="listing drift"):
        scan.check_drift(tmp_path, listed)


@pytest.mark.parametrize("text", ["z\na\n", "a\na\nz\n"])
def test_drift_order_and_duplicates(tmp_path: Path, text: str) -> None:
    listing = write(tmp_path, "ids.txt", text)
    document(
        tmp_path,
        scan.CONFIG,
        {
            "tools": [
                {
                    "useLocalConfigurationFile": False,
                    "patterns": [{"patternId": "a"}, {"patternId": "z"}],
                }
            ],
            "metadata": {
                "listingSha256": scan.file_sha256(listing),
                "listingCount": len(text.splitlines()),
            },
        },
    )
    with pytest.raises(scan.ScanError, match="listing drift"):
        scan.check_drift(tmp_path, listing)


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
    package = root / scan.PACKAGE
    stamp, _ = scan.npm_stamp(root)
    binary = scan.cache_path(versions)
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
            package, "node_modules/" + scan.CLI_PACKAGE + "/package.json", {"version": "0.28.0"}
        )
    elif fault == "cli-missing":
        (package / "node_modules/.bin/codacy-analysis").unlink()
    elif fault == "adapter-version":
        write(package, scan.ADAPTER, 'preferredVersion: "1.29.0",')
    else:
        (package / scan.ADAPTER).unlink()
    with pytest.raises(scan.ScanError, match="run make opengrep-tools"):
        scan.check_install(root)


def test_check_install_current_and_cache_default(
    installed: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, versions = installed
    scan.check_install(root)
    assert scan.stamp_current(root)
    assert scan.cached_binary(versions).read_bytes() == b"binary"
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setattr(scan.Path, "home", lambda: Path("/synthetic-home"))
    assert str(scan.cache_path(versions)).startswith("/synthetic-home/.cache/horistes/opengrep/")


@pytest.mark.parametrize(
    "data", [None, {"dependencies": {}}, {"dependencies": {scan.CLI_PACKAGE: "^0.29.0"}}]
)
def test_cli_pin_shape(installed: tuple, data: object) -> None:
    root, _ = installed
    document(root, scan.PACKAGE + "/package.json", data)
    with pytest.raises(scan.ScanError, match="exact"):
        scan.cli_path(root)


@pytest.mark.parametrize("data", [None, {}, {"version": "1.0.0"}])
def test_cli_installed_version_shape(installed: tuple, data: object) -> None:
    root, _ = installed
    document(root, scan.PACKAGE + "/node_modules/" + scan.CLI_PACKAGE + "/package.json", data)
    with pytest.raises(scan.ScanError, match="installed, the pin"):
        scan.cli_path(root)


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
    write(root / scan.PACKAGE, scan.ADAPTER, text)
    with pytest.raises(scan.ScanError, match="preferredVersion"):
        scan.preferred_version(root, versions)


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
    write(tmp_path, scan.TOOL_VERSIONS, text)
    with pytest.raises(scan.ScanError, match="Opengrep|OPENGREP"):
        scan.tool_versions(tmp_path)


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


@pytest.mark.parametrize("chunks", [0, 3])
def test_download_cases_quick(tmp_path: Path, chunks: int) -> None:
    with FakeResponse(chunks) as response:
        digest, size = scan.fetch_within(
            "https://example.invalid/binary",
            tmp_path / "out",
            5,
            lambda *_args, **_kwargs: response,
        )
    assert (digest, size) == (hashlib.sha256(b"x" * chunks).hexdigest(), chunks)


def test_download_deadline(tmp_path: Path) -> None:
    with (
        FakeResponse(300, 0.01) as response,
        pytest.raises(scan.ScanError, match="did not finish within"),
    ):
        scan.fetch_within(
            "https://example.invalid/slow",
            tmp_path / "out",
            0.02,
            lambda *_args, **_kwargs: response,
        )


def test_download_error(tmp_path: Path) -> None:
    def refused(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic refusal")

    with pytest.raises(scan.ScanError, match="download failed"):
        scan.fetch_within("https://example.invalid/error", tmp_path / "out", 5, refused)


@pytest.mark.parametrize("cap,chunks,match", [(2, 3, "passed"), (3, 3, None), (0, 0, None)])
def test_download_byte_cap(
    monkeypatch: pytest.MonkeyPatch, cap: int, chunks: int, match: str | None
) -> None:
    monkeypatch.setattr(scan, "MAX_DOWNLOAD_BYTES", cap)
    with FakeResponse(chunks) as response:
        if match:
            with pytest.raises(scan.ScanError, match=match):
                scan.copy_capped(response, io.BytesIO(), hashlib.sha256())
        else:
            assert scan.copy_capped(response, io.BytesIO(), hashlib.sha256()) == chunks


def test_download_read_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "MAX_READS", 2)
    with FakeResponse(3) as response, pytest.raises(scan.ScanError, match="within 2 reads"):
        scan.copy_capped(response, io.BytesIO(), hashlib.sha256())


@pytest.mark.parametrize("bad", [False, True])
def test_install_fake_npm_and_opener(
    installed: tuple, monkeypatch: pytest.MonkeyPatch, bad: bool, capsys: pytest.CaptureFixture
) -> None:
    root, versions = installed
    stamp, _ = scan.npm_stamp(root)
    stamp.unlink()
    binary = scan.cache_path(versions)
    binary.write_bytes(b"old")
    commands = []

    def npm(argv: list, cwd: Path, timeout: float) -> SimpleNamespace:
        commands.append((argv, cwd, timeout))
        return result()

    calls = []

    def opener(url: str, timeout: float) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(b"wrong" if bad else b"binary")

    monkeypatch.setattr(scan.shutil, "which", lambda _name: "/fake/npm")
    monkeypatch.setattr(scan, "run_capture", npm)
    monkeypatch.setattr(scan.urllib.request, "urlopen", opener)
    if bad:
        with pytest.raises(scan.ScanError, match="sha256 mismatch"):
            scan.install(root)
        assert binary.read_bytes() == b"old"
    else:
        scan.install(root)
        assert "installed pinned tools" in capsys.readouterr().out
        scan.check_install(root)
        assert binary.stat().st_mode & 0o777 == 0o755
    assert commands == [
        (
            ["/fake/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            root / scan.PACKAGE,
            scan.NPM_TIMEOUT_SECONDS,
        )
    ]
    assert calls == [(versions["OPENGREP_URL"], scan.DOWNLOAD_TIMEOUT_SECONDS)]
    assert not list(binary.parent.glob("horistes-opengrep-install-*"))


def test_noop_install(installed: tuple, capsys: pytest.CaptureFixture) -> None:
    root, _ = installed
    scan.install(root)
    assert capsys.readouterr().out == "codacy-opengrep: installation current; no-op\n"


@pytest.mark.parametrize("rc", [None, 1])
def test_install_npm_refusal(
    installed: tuple, monkeypatch: pytest.MonkeyPatch, rc: int | None
) -> None:
    root, _ = installed
    stamp, _ = scan.npm_stamp(root)
    stamp.unlink()
    monkeypatch.setattr(scan.shutil, "which", lambda _name: None if rc is None else "/fake/npm")
    monkeypatch.setattr(scan, "run_capture", lambda *_args: result(rc=1))
    with pytest.raises(scan.ScanError, match="npm"):
        scan.install_npm(root)
    assert not stamp.exists()


@pytest.mark.parametrize("fault", [None, "copy", "version", "runtime"])
def test_codacy_home(installed: tuple, monkeypatch: pytest.MonkeyPatch, fault: str | None) -> None:
    _, versions = installed
    monkeypatch.setattr(
        scan,
        "run_capture",
        lambda *_args: result(
            "bad" if fault == "version" else VERSION + "\n", rc=1 if fault == "runtime" else 0
        ),
    )
    if fault == "copy":
        monkeypatch.setattr(
            scan.shutil, "copyfile", lambda _src, dest: dest.write_bytes(b"tampered")
        )
    if fault:
        with pytest.raises(scan.ScanError, match="sha256|cannot run"), scan.codacy_home(versions):
            pytest.fail("unverified binary escaped")
    else:
        with scan.codacy_home(versions) as home:
            assert (home / "tools/Semgrep/opengrep-release").read_bytes() == b"binary"
            assert "--taint-intrafile" in (home / "tools/Semgrep/opengrep").read_text()
        assert not home.exists()


@pytest.mark.parametrize(
    "ref,allowed",
    [
        ("main", True),
        ("origin/main", True),
        ("HEAD~1", True),
        ("3506378db2f78c26f54bd8dd5d8f73a6a59bd411", True),
        ("--output=x", False),
        ("main..HEAD", False),
        ("bad\nref", False),
        ("a" * 200, True),
        ("a" * 201, False),
    ],
)
def test_guard_refs(ref: str, allowed: bool) -> None:
    assert (scan.ref_problem(ref) is None) == allowed


def test_environment_barriers() -> None:
    assert scan.git_env(
        {"GIT_DIR": "/other", "GIT_INDEX_FILE": "/other/index", "PATH": "/bin"}
    ) == {"PATH": "/bin"}
    filtered = scan.cli_env(
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

    monkeypatch.setattr(scan.subprocess, "run", runner)
    if failure:
        with pytest.raises(scan.ScanError, match="did not complete"):
            scan.run_capture(["/stub/tool"], tmp_path, 1)
    else:
        assert (
            scan.run_capture(["/stub/tool"], tmp_path, 1, {"PATH": "/bin"}, stdin="x").stdout
            == "done"
        )
        assert seen == [
            (
                ["/stub/tool"],
                {
                    "cwd": tmp_path,
                    "env": {"PATH": "/bin"},
                    "input": "x",
                    "capture_output": True,
                    "text": True,
                    "timeout": 1,
                    "check": False,
                },
            )
        ]


@pytest.mark.parametrize("available", [False, True])
def test_git_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    monkeypatch.setattr(scan.shutil, "which", lambda _name: "/bin/git" if available else None)
    seen = []
    monkeypatch.setattr(
        scan, "run_capture", lambda *args, **kwargs: seen.append((args, kwargs)) or result()
    )
    if available:
        scan.git(tmp_path, "cat-file", "--batch", stdin="HEAD\n")
        args, kwargs = seen[0]
        assert args[:3] == (["/bin/git", "cat-file", "--batch"], tmp_path, scan.GIT_TIMEOUT_SECONDS)
        assert not any(key.startswith("GIT_") for key in args[3])
        assert kwargs == {"stdin": "HEAD\n"}
    else:
        with pytest.raises(scan.ScanError, match="git not found"):
            scan.git(tmp_path, "ls-files")


@pytest.mark.parametrize(
    "header,rc,allowed",
    [
        ("a" * 40 + " commit 1\n", 0, True),
        ("a" * 64 + " commit 1\n", 0, True),
        ("bad commit 1\n", 0, False),
        ("HEAD missing\n", 0, False),
        ("a" * 40 + " blob 1\n", 0, False),
        ("a" * 40 + " commit 1\n", 1, False),
    ],
)
def test_resolve_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str, rc: int, allowed: bool
) -> None:
    monkeypatch.setattr(scan, "git", lambda *_args, **_kwargs: result(header, rc))
    if allowed:
        assert scan.resolve_commit(tmp_path, "main") == header.split()[0]
    else:
        with pytest.raises(scan.ScanError, match="not a commit"):
            scan.resolve_commit(tmp_path, "main")
    with pytest.raises(scan.ScanError, match="not a branch"):
        scan.resolve_commit(tmp_path, "--bad")


@pytest.mark.parametrize(
    "body,rc,want",
    [
        ("missing", 0, "first baseline"),
        ("bad", 0, "not a file"),
        ("invalid-json", 0, "at main"),
        ("invalid-baseline", 0, "at main"),
        ("unchanged", 0, "shrink"),
        ("grown", 0, "baseline grew"),
        ("missing", 1, "failed"),
    ],
)
def test_shrink_git_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    body: str,
    rc: int,
    want: str,
) -> None:
    current = {scan.entry_key(entry()): entry()}
    monkeypatch.setattr(scan, "load_baseline", lambda *_args: current)
    monkeypatch.setattr(scan, "resolve_commit", lambda *_args: "a" * 40)
    bodies = {
        "missing": "key missing\n",
        "bad": "a tree 0\n",
        "invalid-json": "a blob 1\n{\n",
        "invalid-baseline": "a blob 2\n{}\n",
        "unchanged": "a blob 1\n" + json.dumps({"version": 2, "findings": [entry()]}) + "\n",
        "grown": "a blob 1\n" + json.dumps({"version": 2, "findings": []}) + "\n",
    }
    monkeypatch.setattr(
        scan, "git", lambda *_args, **_kwargs: result(bodies[body], rc, "synthetic")
    )
    if body in {"missing", "unchanged", "grown"} and rc == 0:
        problems = scan.shrink_problems(tmp_path, "main", TODAY)
        assert want in ("\n".join(problems) if problems else capsys.readouterr().out)
    else:
        with pytest.raises(scan.ScanError, match=want):
            scan.shrink_problems(tmp_path, "main", TODAY)


@pytest.mark.parametrize(
    "names,rc,match",
    [
        ("", 0, "expected"),
        ("../bad.py\0", 0, "names"),
        ("/bad.py\0", 0, "names"),
        ("a.py\0", 1, "ls-files failed"),
        ("a.py\0b.py\0", 0, None),
        (scan.FIXTURES + "/x.py\0a.py\0", 0, None),
    ],
)
def test_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: str, rc: int, match: str | None
) -> None:
    monkeypatch.setattr(scan, "git", lambda *_args: result(names, rc))
    if match:
        with pytest.raises(scan.ScanError, match=match):
            scan.tracked_files(tmp_path)
    else:
        assert scan.tracked_files(tmp_path) == sorted(
            n for n in names.split("\0") if n and not n.startswith(scan.FIXTURES)
        )


def test_tracked_file_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "MAX_FILES", 1)
    monkeypatch.setattr(scan, "git", lambda *_args: result("a.py\0b.py\0"))
    with pytest.raises(scan.ScanError, match="expected 1 to 1"):
        scan.tracked_files(tmp_path)


@pytest.mark.parametrize("text", [None, scan.CANARY_OLD, scan.CANARY_OLD + "\n" + scan.CANARY_NEW])
def test_canary_source_read(tmp_path: Path, text: str | None) -> None:
    if text is None:
        with pytest.raises(scan.ScanError, match="cannot read"):
            scan.canary_source(tmp_path)
        assert "cannot read" in scan.canary_check(tmp_path, tmp_path, Counter(), VERSION, None)[0]
    else:
        write(tmp_path, scan.CANARY_FILE, text)
        assert scan.canary_source(tmp_path) == text.replace(scan.CANARY_OLD, scan.CANARY_NEW)


def test_read_sources_and_save(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "x = 1\n")
    assert scan.read_sources(tmp_path, ["a.py"]) == {"a.py": "x = 1\n"}
    with pytest.raises(scan.ScanError, match="cannot read a scan target"):
        scan.read_sources(tmp_path, ["missing.py"])
    scan.save_report(None, "unused", {})
    scan.save_report(tmp_path / "out", "report.json", report())
    assert scan.load_json(tmp_path / "out/report.json") == report()


@pytest.mark.parametrize("unrelated", [False, True])
def test_remove_generated_config(tmp_path: Path, unrelated: bool) -> None:
    generated = ".codacy/generated/Semgrep/opengrep-rules.yaml"
    write(tmp_path, generated, "rules: []\n")
    if unrelated:
        write(tmp_path, ".codacy/generated/Semgrep/keep", "keep")
    scan.remove_generated_config(tmp_path)
    assert not (tmp_path / generated).exists()
    assert (tmp_path / ".codacy").exists() == unrelated
    scan.remove_generated_config(tmp_path)


@pytest.mark.parametrize("fail", [None, "init", "add"])
def test_copy_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: str | None) -> None:
    seen = []

    def scratch_git(cwd: Path, *args: str) -> SimpleNamespace:
        assert cwd != tmp_path
        assert (cwd / "nested/a.py").read_text() == "x = 1\n"
        seen.append(args)
        return result(rc=int(args[0] == fail))

    monkeypatch.setattr(scan, "git", scratch_git)
    monkeypatch.setattr(scan, "live_report", lambda *_args: report())
    if fail:
        with pytest.raises(scan.ScanError, match=f"git {fail} failed"):
            scan.copy_report(tmp_path, tmp_path, {"nested/a.py": "x = 1\n"})
    else:
        assert scan.copy_report(tmp_path, tmp_path, {"nested/a.py": "x = 1\n"}) == report()
        assert seen == [("init", "-q"), ("add", "--all", "--force")]


@pytest.mark.parametrize(
    "rc,contents", [(0, {}), (1, {}), (2, {}), (0, None), (0, []), (0, {"metadata": None})]
)
def test_live_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int, contents: object
) -> None:
    monkeypatch.setattr(scan, "cli_path", lambda _root: tmp_path / "fake-cli")
    seen = []

    def runner(argv: list[str], cwd: Path, timeout: int, env: dict) -> SimpleNamespace:
        seen.append(argv)
        assert cwd == tmp_path and timeout == scan.CLI_TIMEOUT_SECONDS
        assert env["CODACY_GLOBAL_DIR"] == str(tmp_path)
        if contents is not None:
            data = (
                {**contents, "metadata": {"repositoryRoot": "local", "retained": True}}
                if contents == {}
                else contents
            )
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(data), encoding="utf-8")
        write(cwd, ".codacy/generated/Semgrep/opengrep-rules.yaml", "rules: []")
        return result(rc=rc)

    monkeypatch.setattr(scan, "run_capture", runner)
    if rc > 1 or contents is None or isinstance(contents, list):
        with pytest.raises(scan.ScanError, match="CLI"):
            scan.live_report(tmp_path, tmp_path, tmp_path)
    else:
        data = scan.live_report(tmp_path, tmp_path, tmp_path)
        assert data.get("metadata") == ({"retained": True} if contents == {} else None)
    assert not (tmp_path / ".codacy").exists()
    assert seen[0][seen[0].index("--max-file-size") + 1] == "1MB"
    assert "--fail-if-missing" in seen[0] and "--no-update-notifier" in seen[0]


def test_live_report_cleanup_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan, "cli_path", lambda _root: tmp_path / "fake-cli")

    def failed(*_args: object) -> None:
        write(tmp_path, ".codacy/generated/Semgrep/opengrep-rules.yaml", "rules: []")
        raise scan.ScanError("synthetic failure")

    monkeypatch.setattr(scan, "run_capture", failed)
    with pytest.raises(scan.ScanError, match="synthetic failure"):
        scan.live_report(tmp_path, tmp_path, tmp_path)
    assert not (tmp_path / ".codacy").exists()


@pytest.mark.parametrize("mode", [[], ["--install"], ["--check-install"], ["--base-ref", "main"]])
@pytest.mark.parametrize(
    "system,machine", [("Darwin", "arm64"), ("Linux", "aarch64"), ("Windows", "AMD64")]
)
def test_platform_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    mode: list[str],
    system: str,
    machine: str,
) -> None:
    monkeypatch.setattr(scan.platform, "system", lambda: system)
    monkeypatch.setattr(scan.platform, "machine", lambda: machine)
    assert scan.main(mode) == 0
    assert capsys.readouterr().out == (
        f"codacy-opengrep: skipped: the Opengrep pin is a Linux x64 binary ({system} {machine})\n"
    )


@pytest.mark.parametrize(
    "mode", [[], ["--install"], ["--check-install"], ["--base-ref", "main"], ["--drift", "listing"]]
)
def test_main_dispatch(monkeypatch: pytest.MonkeyPatch, mode: list[str]) -> None:
    monkeypatch.setattr(scan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scan.platform, "machine", lambda: "x86_64")
    calls = []
    for name in ("scan", "install", "check_install", "shrink_problems", "check_drift"):

        def called(*args: object, name: str = name) -> list:
            calls.append((name, args))
            return []

        monkeypatch.setattr(scan, name, called)
    assert scan.main([*mode, "--today", "2026-11-14", "--report-out", "reports"]) == 0
    assert len(calls) == 1
    expected = {
        "--install": "install",
        "--check-install": "check_install",
        "--base-ref": "shrink_problems",
        "--drift": "check_drift",
    }
    assert calls[0][0] == (expected[mode[0]] if mode else "scan")


@pytest.mark.parametrize("failure", [scan.ScanError("synthetic"), OSError("synthetic"), None])
def test_main_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, failure: Exception | None
) -> None:
    monkeypatch.setattr(scan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scan.platform, "machine", lambda: "x86_64")

    def failed(*_args: object) -> list:
        if failure:
            raise failure
        return ["synthetic"]

    monkeypatch.setattr(scan, "scan", failed)
    assert scan.main([]) == 1
    assert capsys.readouterr().err == "codacy-opengrep: synthetic\n"


def test_drift_runs_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scan.platform, "system", lambda: "Darwin")
    assert scan.main(["--drift", str(scan.ROOT / scan.FIXTURES / "enabled-python-ids.txt")]) == 0


@pytest.mark.parametrize(
    "args", [["--install", "--check-install"], ["--today", "bad"], ["--self-test"]]
)
def test_argument_refusals(args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        scan.parse_args(args)
    assert caught.value.code == 2


def test_script_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(
        sys,
        "argv",
        ["scanner", "--drift", str(scan.ROOT / scan.FIXTURES / "enabled-python-ids.txt")],
    )
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(scan.ROOT / "tools/codacy_opengrep_scan.py"), run_name="__main__")
    assert caught.value.code == 0


@pytest.mark.parametrize("bad_tree", [False, True])
def test_scan_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, bad_tree: bool
) -> None:
    from contextlib import nullcontext

    sample = issue()
    key = scan.fingerprint(sample)
    baseline = {key: entry(contentSha256=key.split("\0")[2])}
    monkeypatch.setattr(scan, "tool_versions", lambda _root: {"OPENGREP_VERSION": VERSION})
    monkeypatch.setattr(scan, "load_baseline", lambda *_args: baseline)
    monkeypatch.setattr(scan, "tracked_files", lambda _root: ["a.py"])
    events = []
    monkeypatch.setattr(scan, "check_install", lambda _root: events.append("check-install"))
    monkeypatch.setattr(scan, "codacy_home", lambda _versions: nullcontext(tmp_path))
    monkeypatch.setattr(scan, "canary_source", lambda _root: "planted")
    monkeypatch.setattr(scan, "read_sources", lambda _root, names: dict.fromkeys(names, "source"))

    def copied(_root: Path, _home: Path, sources: dict) -> dict:
        events.append(tuple(sources))
        if scan.CANARY_FILE in sources:
            assert sources[scan.CANARY_FILE] == "planted"
            return report([issue(rule=r, path=p) for r, p in scan.CANARY_WANT], count=2)
        return report([sample, sample] if bad_tree else [sample])

    monkeypatch.setattr(scan, "copy_report", copied)
    problems = scan.scan(tmp_path, TODAY, tmp_path / "reports")
    assert len(problems) == int(bad_tree)
    assert events == ["check-install", (scan.CANARY_FILE, scan.INTRAFILE_FILE), ("a.py",)]
    assert (tmp_path / "reports/canary-report.json").is_file()
    assert (tmp_path / "reports/tree-report.json").is_file()
    output = capsys.readouterr().out
    assert bool(output) != bad_tree
    if not bad_tree:
        assert "753b7aa / fc53740" in output and "0 new, 0 stale" in output


def test_wrapper_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shlex

    directory = tmp_path / "dir with space and 'quote"
    directory.mkdir()
    stub = write(directory, "opengrep-release", '#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    wrapper = scan.write_wrapper(directory, stub)
    assert wrapper.stat().st_mode & 0o777 == 0o755
    text = wrapper.read_text()
    commands = [
        shlex.split(line.strip()) for line in text.splitlines() if line.strip().startswith("exec ")
    ]
    assert commands == [
        ["exec", str(stub), "scan", "--taint-intrafile", "$@"],
        ["exec", str(stub), "$@"],
    ]
    monkeypatch.setattr(scan.subprocess, "run", ORIGINAL_RUN)
    scanned = scan.run_capture([str(wrapper), "scan", "--json", "a b"], directory, 5)
    version = scan.run_capture([str(wrapper), "--version"], directory, 5)
    assert scanned.stdout.splitlines() == ["scan", "--taint-intrafile", "--json", "a b"]
    assert version.stdout.splitlines() == ["--version"]


def test_report_without_optional_skipped_field() -> None:
    clean = report()
    del clean["skippedFiles"]
    assert not scan.report_problems(
        clean, ["a.py"], Counter({scan.fingerprint(issue()): 1}), VERSION
    )


def fixture_files(data: dict) -> list[str]:
    """Reconstruct the bounded coverage set of a recorded CLI report."""
    _errors, issues, semgrep = scan.report_lists(data)
    names = sorted({i["filePath"] for i in issues})
    count = semgrep["filesAnalyzed"]
    assert len(names) <= count <= scan.MAX_FILES
    return names + [f"clean-fixture-{i}.py" for i in range(count - len(names))]


def test_recorded_reports_both_directions() -> None:
    fixtures = scan.ROOT / scan.FIXTURES
    baseline = scan.baseline_counts(scan.load_baseline(scan.ROOT, TODAY))
    main = scan.load_json(fixtures / "main-report.json")
    planted = scan.load_json(fixtures / "planted-report.json")
    canary = scan.load_json(fixtures / "canary-report.json")
    assert not scan.report_problems(main, fixture_files(main), baseline, VERSION)
    problems = scan.report_problems(planted, fixture_files(planted), baseline, VERSION)
    assert len(problems) == 1
    assert problems[0].startswith(f"new finding: {scan.AUDIT_RULE} | {scan.CANARY_FILE} | ")
    assert not scan.canary_problems(canary, baseline, VERSION)
    for rule, path in scan.CANARY_WANT:
        missing = copy.deepcopy(canary)
        missing["issues"] = [
            i for i in canary["issues"] if (i["patternId"], i["filePath"]) != (rule, path)
        ]
        assert scan.canary_problems(missing, baseline, VERSION)
    repeated = copy.deepcopy(main)
    repeated["issues"].append(main["issues"][0])
    assert scan.report_problems(repeated, fixture_files(main), baseline, VERSION)[0].startswith(
        "new finding:"
    )
    assert scan.report_problems(
        main, fixture_files(main), baseline + Counter({"demo\0missing.py\0" + "0" * 64: 1}), VERSION
    )[0].startswith("stale baseline entry:")


def test_unrunnable_engine_never_reaches_cli(
    installed: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _versions = installed
    monkeypatch.setattr(scan, "run_capture", lambda *_args: result(rc=1))
    monkeypatch.setattr(scan, "load_baseline", lambda *_args: {})
    monkeypatch.setattr(scan, "tracked_files", lambda _root: ["a.py"])
    with pytest.raises(scan.ScanError, match="refusing CLI auto-repair"):
        scan.scan(root, TODAY, None)


def test_scratch_copy_includes_new_python_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def listed(_root: Path, *args: str) -> SimpleNamespace:
        seen.append(args)
        return result("existing.py\0new.py\0")

    monkeypatch.setattr(scan, "git", listed)
    assert scan.tracked_files(tmp_path) == ["existing.py", "new.py"]
    assert seen == [("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py")]


@pytest.mark.parametrize("dependencies", [None, [], "invalid"])
def test_cli_rejects_malformed_dependency_table(installed: tuple, dependencies: object) -> None:
    root, _ = installed
    document(root, scan.PACKAGE + "/package.json", {"dependencies": dependencies})
    with pytest.raises(scan.ScanError, match="exact"):
        scan.cli_path(root)


def test_config_rejects_mixed_pattern_shapes(tmp_path: Path) -> None:
    document(
        tmp_path,
        scan.CONFIG,
        {"tools": [{"useLocalConfigurationFile": False, "patterns": [{"patternId": "a"}, None]}]},
    )
    with pytest.raises(scan.ScanError, match="invalid pattern list"):
        scan.config_ids(tmp_path)


def test_non_utf8_stamp_is_stale(installed: tuple) -> None:
    root, _ = installed
    stamp, _expected = scan.npm_stamp(root)
    stamp.write_bytes(b"\xff")
    assert not scan.stamp_current(root)


def test_drift_rejects_malformed_metadata(tmp_path: Path) -> None:
    listing = write(tmp_path, "ids.txt", "a\n")
    document(
        tmp_path,
        scan.CONFIG,
        {
            "tools": [{"useLocalConfigurationFile": False, "patterns": [{"patternId": "a"}]}],
            "metadata": None,
        },
    )
    with pytest.raises(scan.ScanError, match="metadata"):
        scan.check_drift(tmp_path, listing)
