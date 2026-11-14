# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep report module."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import codacy_opengrep_baseline as baselines
import codacy_opengrep_common as common
import codacy_opengrep_report as reports
import exceptions
import pytest

from .codacy_opengrep_helpers import (
    TODAY,
    VERSION,
    fixture_files,
    issue,
    report,
    result,
    write,
)


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
    assert (reports.fingerprint(issue(left)) == reports.fingerprint(issue(right))) == same
    assert reports.fingerprint({**issue(), "multilineContent": [left]}) == reports.fingerprint(
        issue(left)
    )
    assert reports.fingerprint({**issue(left), "lineNumber": 999}) == reports.fingerprint(
        issue(left)
    )


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
    with pytest.raises(common.ScanError, match="report issue"):
        reports.fingerprint(data)


@pytest.mark.parametrize("size", [common.MAX_CONTENT_CHARS, common.MAX_CONTENT_CHARS + 1])
def test_fingerprint_size(size: int) -> None:
    if size > common.MAX_CONTENT_CHARS:
        with pytest.raises(common.ScanError, match="at most"):
            reports.fingerprint(issue("x" * size))
    else:
        assert len(reports.fingerprint(issue("x" * size)).split("\0")[2]) == 64


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
    problems = reports.report_problems(
        main, ["a.py"], Counter({reports.fingerprint(issue()): 1}), VERSION
    )
    assert any(match in text for text in problems)


@pytest.mark.parametrize("value", [None, [], "x"])
def test_report_not_object(value: object) -> None:
    assert reports.report_problems(value, [], Counter(), VERSION) == ["report is not a JSON object"]


@pytest.mark.parametrize(
    "fields,match",
    [({"configSource": "local"}, "rules came from"), ({"version": "0.0.0"}, "Opengrep 0.0.0 ran")],
)
def test_with_capability(fields: dict, match: str) -> None:
    main = report()
    main["capability"]["ready"][0].update(fields)
    assert any(match in p for p in reports.capability_problems(main, VERSION))


def test_report_counts_coverage_and_repeat() -> None:
    main = report()
    baseline = Counter({reports.fingerprint(issue()): 1})
    assert not reports.report_problems(main, ["a.py"], baseline, VERSION)
    main["issues"].append(copy.deepcopy(main["issues"][0]))
    assert len(reports.report_problems(main, ["a.py"], baseline, VERSION)) == 1
    stale = baseline + Counter({"demo\0missing.py\0" + "0" * 64: 1})
    assert reports.report_problems(report(), ["a.py"], stale, VERSION)[0].startswith(
        "stale baseline entry:"
    )
    assert not reports.report_problems(report(), ["a.py"], stale, VERSION, enforce_stale=False)
    assert "covered" in reports.report_problems(report(), [], baseline, VERSION)[0]
    assert "unpassed" in reports.report_problems(report(), ["b.py"], baseline, VERSION)[0]


def test_canary_both_directions() -> None:
    issues = [issue(rule=rule, path=path) for rule, path in common.CANARY_WANT]
    planted = report(issues, count=2)
    assert not reports.canary_problems(planted, Counter(), VERSION)
    for rule, path in common.CANARY_WANT:
        missing = copy.deepcopy(planted)
        missing["issues"] = [i for i in issues if (i["patternId"], i["filePath"]) != (rule, path)]
        assert reports.canary_problems(missing, Counter(), VERSION)
    repeated = copy.deepcopy(planted)
    repeated["issues"].append(issues[0])
    assert reports.canary_problems(repeated, Counter(), VERSION)


@pytest.mark.parametrize(
    "names,rc,match",
    [
        ("", 0, "expected"),
        ("../bad.py\0", 0, "names"),
        ("/bad.py\0", 0, "names"),
        ("a.py\0", 1, "ls-files failed"),
        ("a.py\0b.py\0", 0, None),
        (common.FIXTURES + "/x.py\0a.py\0", 0, None),
    ],
)
def test_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: str, rc: int, match: str | None
) -> None:
    write(tmp_path, "a.py", "pass")
    write(tmp_path, "b.py", "pass")
    monkeypatch.setattr(reports, "git", lambda *_args: result(names, rc))
    if match:
        with pytest.raises(common.ScanError, match=match):
            reports.tracked_files(tmp_path)
    else:
        assert reports.tracked_files(tmp_path) == sorted(
            n for n in names.split("\0") if n and not n.startswith(common.FIXTURES)
        )


def test_tracked_file_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports, "MAX_FILES", 1)
    monkeypatch.setattr(reports, "git", lambda *_args: result("a.py\0b.py\0"))
    with pytest.raises(common.ScanError, match="expected 1 to 1"):
        reports.tracked_files(tmp_path)


def test_read_sources_and_save(tmp_path: Path) -> None:
    write(tmp_path, "a.py", "x = 1\n")
    assert reports.read_sources(tmp_path, ["a.py"]) == {"a.py": "x = 1\n"}
    with pytest.raises(common.ScanError, match="cannot read a scan target"):
        reports.read_sources(tmp_path, ["missing.py"])
    reports.save_report(None, "unused", {})
    reports.save_report(tmp_path / "out", "report.json", report())
    assert common.load_json(tmp_path / "out/report.json") == report()


def test_report_without_optional_skipped_field() -> None:
    clean = report()
    del clean["skippedFiles"]
    assert not reports.report_problems(
        clean, ["a.py"], Counter({reports.fingerprint(issue()): 1}), VERSION
    )


def test_recorded_reports_both_directions() -> None:
    fixtures = common.ROOT / common.FIXTURES
    baseline = baselines.baseline_counts(baselines.load_baseline(common.ROOT, TODAY))
    main = common.load_json(fixtures / "main-report.json")
    planted = common.load_json(fixtures / "planted-report.json")
    canary = common.load_json(fixtures / "canary-report.json")
    assert not reports.report_problems(main, fixture_files(main), baseline, VERSION)
    problems = reports.report_problems(planted, fixture_files(planted), baseline, VERSION)
    assert len(problems) == 1
    assert problems[0].startswith(f"new finding: {common.AUDIT_RULE} | {common.CANARY_FILE} | ")
    assert not reports.canary_problems(canary, baseline, VERSION)
    for rule, path in common.CANARY_WANT:
        missing = copy.deepcopy(canary)
        missing["issues"] = [
            i for i in canary["issues"] if (i["patternId"], i["filePath"]) != (rule, path)
        ]
        assert reports.canary_problems(missing, baseline, VERSION)
    repeated = copy.deepcopy(main)
    repeated["issues"].append(main["issues"][0])
    assert reports.report_problems(repeated, fixture_files(main), baseline, VERSION)[0].startswith(
        "new finding:"
    )
    assert reports.report_problems(
        main, fixture_files(main), baseline + Counter({"demo\0missing.py\0" + "0" * 64: 1}), VERSION
    )[0].startswith("stale baseline entry:")


@pytest.mark.parametrize("name", ["main-report.json", "planted-report.json"])
def test_recorded_reports_quote_suppressions_escaped(name: str) -> None:
    """A quoted ignore comment is JSON-escaped, so check-exceptions does not count it twice."""
    raw = (common.ROOT / common.FIXTURES / name).read_text(encoding="utf-8")
    counts: dict[str, int] = {}
    assert not exceptions.opengrep_problems(raw, name, TODAY, set(), counts)
    assert not counts
    quoted = [
        (issue["filePath"], line)
        for issue in json.loads(raw)["issues"]
        for line in issue.get("multilineContent", [])
        if exceptions.OPENGREP_AFTER_CODE.search(line)
    ]
    assert [path for path, _line in quoted] == ["tools/forge.py"]


def test_scratch_copy_includes_new_python_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def listed(_root: Path, *args: str) -> SimpleNamespace:
        seen.append(args)
        return result("existing.py\0new.py\0")

    write(tmp_path, "existing.py", "pass")
    write(tmp_path, "new.py", "pass")
    monkeypatch.setattr(reports, "git", listed)
    assert reports.tracked_files(tmp_path) == ["existing.py", "new.py"]
    assert seen == [("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py")]


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_unterminated_literal_trailing_backslash(quote: str) -> None:
    text = quote + "a  b\\"
    assert reports.normalised_source(text) == text


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_long_unterminated_literal(quote: str) -> None:
    text = quote + "\\a" * 10000
    assert reports.normalised_source(text) == text


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "boundary", "invalid-utf8"])
def test_source_file_barriers(tmp_path: Path, kind: str) -> None:
    import os

    target = tmp_path / "target.py"
    if kind == "symlink":
        target.symlink_to(write(tmp_path, "real.py", "pass"))
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(
            b"\xff"
            if kind == "invalid-utf8"
            else b"x" * (common.MAX_SOURCE_BYTES + int(kind == "oversize"))
        )
    if kind == "boundary":
        assert reports.read_sources(tmp_path, [target.name]) == {
            target.name: "x" * common.MAX_SOURCE_BYTES
        }
    else:
        with pytest.raises(common.ScanError, match="target.py"):
            reports.read_sources(tmp_path, [target.name])


@pytest.mark.parametrize(
    "field", ["issues", "errors", "skippedFiles", "patternSummary", "toolResults"]
)
@pytest.mark.parametrize("size", [common.MAX_REPORT_ITEMS, common.MAX_REPORT_ITEMS + 1])
def test_report_list_bound(field: str, size: int) -> None:
    sample = report()
    sample[field] = [None] * size
    if field == "toolResults":
        sample[field][0] = report()[field][0]
    if size > common.MAX_REPORT_ITEMS:
        with pytest.raises(common.ScanError, match=f"{field} exceeds"):
            reports.report_lists(sample)
    elif field == "skippedFiles":
        with pytest.raises(common.ScanError, match="must be an empty list"):
            reports.report_lists(sample)
    else:
        reports.report_lists(sample)


def test_deleted_index_target_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path, "exists.py", "pass")
    monkeypatch.setattr(reports, "git", lambda *_args: result("deleted.py\0exists.py\0"))
    assert reports.tracked_files(tmp_path) == ["exists.py"]


def test_save_report_escapes_quoted_ignore(tmp_path: Path) -> None:
    sample = report([issue("# nose" + "mgrep: rule")])
    reports.save_report(tmp_path, "report.json", sample)
    raw = (tmp_path / "report.json").read_text()
    assert "nose" + "mgrep" not in raw
    assert common.load_json(tmp_path / "report.json") == sample
