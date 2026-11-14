# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep scan module."""

from __future__ import annotations

import json
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import codacy_opengrep_baseline as baselines
import codacy_opengrep_common as common
import codacy_opengrep_install as installer
import codacy_opengrep_report as reports
import codacy_opengrep_scan as scan
import pytest

from .codacy_opengrep_helpers import (
    TODAY,
    VERSION,
    document,
    entry,
    issue,
    report,
    result,
    write,
)


@pytest.mark.parametrize("copies", [0, 2])
def test_canary_refuses_changed_source(tmp_path: Path, copies: int) -> None:
    target = tmp_path / common.CANARY_FILE
    target.parent.mkdir(parents=True)
    target.write_text(common.CANARY_OLD * copies, encoding="utf-8")
    with pytest.raises(common.ScanError, match="re-derive the canary"):
        scan.canary_source(tmp_path)


@pytest.mark.parametrize(
    "text", [None, common.CANARY_OLD, common.CANARY_OLD + "\n" + common.CANARY_NEW]
)
def test_canary_source_read(tmp_path: Path, text: str | None) -> None:
    if text is None:
        with pytest.raises(common.ScanError, match="cannot read"):
            scan.canary_source(tmp_path)
        assert "cannot read" in scan.canary_check(tmp_path, tmp_path, Counter(), VERSION, None)[0]
    else:
        write(tmp_path, common.CANARY_FILE, text)
        assert scan.canary_source(tmp_path) == text.replace(common.CANARY_OLD, common.CANARY_NEW)


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
        with pytest.raises(common.ScanError, match=f"git {fail} failed"):
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

    def runner(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append(argv)
        cwd = kwargs["cwd"]
        assert cwd == tmp_path and kwargs["timeout"] == common.CLI_TIMEOUT_SECONDS
        assert kwargs["env"]["CODACY_GLOBAL_DIR"] == str(tmp_path)
        if contents is not None:
            data = (
                {**contents, "metadata": {"repositoryRoot": "local", "retained": True}}
                if contents == {}
                else contents
            )
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(data), encoding="utf-8")
        write(cwd, ".codacy/generated/Semgrep/opengrep-rules.yaml", "rules: []")
        return result(rc=rc)

    monkeypatch.setattr(common.subprocess, "run", runner)
    if rc > 1 or contents is None or isinstance(contents, list):
        with pytest.raises(common.ScanError, match="CLI"):
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
        raise common.ScanError("synthetic failure")

    monkeypatch.setattr(scan, "cli_run", failed)
    with pytest.raises(common.ScanError, match="synthetic failure"):
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
    monkeypatch.setattr(scan, "install_lock", lambda *_args, **_kwargs: nullcontext())
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
        "--base-ref": "scan",
        "--drift": "check_drift",
    }
    assert calls[0][0] == (expected[mode[0]] if mode else "scan")


@pytest.mark.parametrize("failure", [common.ScanError("synthetic"), OSError("synthetic"), None])
def test_main_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, failure: Exception | None
) -> None:
    monkeypatch.setattr(scan, "install_lock", lambda *_args, **_kwargs: nullcontext())
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
    assert (
        scan.main(["--drift", str(common.ROOT / common.FIXTURES / "enabled-python-ids.txt")]) == 0
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--install", "--check-install"],
        ["--today", "bad"],
        ["--self-test"],
        ["--base-ref", "main", "--install"],
        ["--base-ref", "main", "--check-install"],
        ["--base-ref", "main", "--drift", "listing"],
    ],
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
        ["scanner", "--drift", str(common.ROOT / common.FIXTURES / "enabled-python-ids.txt")],
    )
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(common.ROOT / "tools/codacy_opengrep_scan.py"), run_name="__main__")
    assert caught.value.code == 0


@pytest.mark.parametrize("bad_tree", [False, True])
def test_scan_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, bad_tree: bool
) -> None:
    from contextlib import nullcontext

    sample = issue()
    key = reports.fingerprint(sample)
    baseline = {key: entry(contentSha256=key.split("\0")[2])}
    monkeypatch.setattr(scan, "install_lock", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(scan, "check_drift", lambda *_args: None)
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
        if common.CANARY_FILE in sources:
            assert sources[common.CANARY_FILE] == "planted"
            return report([issue(rule=r, path=p) for r, p in common.CANARY_WANT], count=2)
        return report([sample, sample] if bad_tree else [sample])

    monkeypatch.setattr(scan, "copy_report", copied)
    problems = scan.scan(tmp_path, TODAY, tmp_path / "reports")
    assert len(problems) == int(bad_tree)
    assert events == ["check-install", (common.CANARY_FILE, common.INTRAFILE_FILE), ("a.py",)]
    assert (tmp_path / "reports/canary-report.json").is_file()
    assert (tmp_path / "reports/tree-report.json").is_file()
    output = capsys.readouterr().out
    assert ("0 new, 0 stale" in output) != bad_tree
    if not bad_tree:
        assert "753b7aa / fc53740" in output and "0 new, 0 stale" in output


def test_unrunnable_engine_never_reaches_cli(
    installed: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _versions = installed
    monkeypatch.setattr(installer, "engine_run", lambda *_args: result(rc=1))
    monkeypatch.setattr(scan, "install_lock", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(scan, "check_drift", lambda *_args: None)
    monkeypatch.setattr(scan, "load_baseline", lambda *_args: {})
    monkeypatch.setattr(scan, "tracked_files", lambda _root: ["a.py"])
    with pytest.raises(common.ScanError, match="refusing CLI auto-repair"):
        scan.scan(root, TODAY, None)


def test_scan_checks_mirrored_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    listing = write(tmp_path, common.FIXTURES + "/enabled-python-ids.txt", "kept\nremoved\n")
    config = {
        "tools": [
            {
                "useLocalConfigurationFile": False,
                "patterns": [{"patternId": "kept"}, {"patternId": "removed"}],
            }
        ],
        "metadata": {
            "listingCount": 2,
            "listingSha256": common.hashlib.sha256(listing.read_bytes()).hexdigest(),
        },
    }
    document(tmp_path, common.CONFIG, config)
    monkeypatch.setattr(scan, "install_lock", lambda *_args, **_kwargs: nullcontext())
    events = []

    def stop(_root: Path) -> dict:
        events.append("after-drift")
        raise common.ScanError("reached after drift")

    monkeypatch.setattr(scan, "tool_versions", stop)
    with pytest.raises(common.ScanError, match="reached after drift"):
        scan.scan(tmp_path, TODAY, None)
    assert events == ["after-drift"]
    config["tools"][0]["patterns"].pop()
    document(tmp_path, common.CONFIG, config)
    with pytest.raises(common.ScanError, match="removed"):
        scan.scan(tmp_path, TODAY, None)
    assert events == ["after-drift"]


def test_scan_uses_one_baseline_snapshot(installed: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = installed
    document(
        root,
        common.CONFIG,
        {
            "tools": [
                {"useLocalConfigurationFile": False, "patterns": [{"patternId": common.AUDIT_RULE}]}
            ]
        },
    )
    document(root, common.BASELINE, {"version": 2, "findings": []})
    reads = []
    load = baselines.load_json

    def reader(path: Path) -> object:
        reads.append(path)
        return load(path)

    monkeypatch.setattr(baselines, "load_json", reader)
    monkeypatch.setattr(scan, "check_drift", lambda *_args: None)
    monkeypatch.setattr(scan, "tracked_files", lambda _root: ["a.py"])
    monkeypatch.setattr(scan, "codacy_home", lambda _versions: nullcontext(root))
    monkeypatch.setattr(scan, "canary_check", lambda *_args: [])
    monkeypatch.setattr(scan, "read_sources", lambda *_args: {"a.py": "pass"})
    monkeypatch.setattr(scan, "copy_report", lambda *_args: report([], 1))
    snapshots = []
    counts = scan.baseline_counts

    def counted(snapshot: dict) -> Counter:
        snapshots.append(snapshot)
        return counts(snapshot)

    def shrunk(_root: Path, ref: str, snapshot: dict) -> list:
        assert ref == "origin/main" and snapshot is snapshots[0]
        assert snapshot == {}
        return []

    monkeypatch.setattr(scan, "baseline_counts", counted)
    monkeypatch.setattr(scan, "shrink_problems", shrunk)
    assert not scan.scan(root, TODAY, None, "origin/main")
    assert reads == [root / common.BASELINE]


def test_scan_lock_blocks_exclusive_holder(
    installed: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = installed
    monkeypatch.setattr(installer, "LOCK_TRIES", 1)
    monkeypatch.setattr(installer, "LOCK_SLEEP_SECONDS", 0)
    with (
        installer.install_lock(root, exclusive=True),
        pytest.raises(common.ScanError, match=r"\.install\.lock"),
    ):
        scan.scan(root, TODAY, None)


def test_check_install_holds_shared_lock(installed: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _ = installed
    monkeypatch.setattr(scan, "ROOT", root)
    monkeypatch.setattr(installer, "LOCK_TRIES", 1)
    monkeypatch.setattr(installer, "LOCK_SLEEP_SECONDS", 0)
    monkeypatch.setattr(scan.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scan.platform, "machine", lambda: "x86_64")
    with installer.install_lock(root):
        assert scan.main(["--check-install"]) == 0
    with installer.install_lock(root, exclusive=True):
        assert scan.main(["--check-install"]) == 1
