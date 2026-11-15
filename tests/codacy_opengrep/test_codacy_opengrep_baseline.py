# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Offline checks for the Codacy Opengrep baseline module."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path

import codacy_opengrep_baseline as baseline
import codacy_opengrep_common as common
import codacy_opengrep_scan as scan
import pytest

from .codacy_opengrep_helpers import (
    TODAY,
    document,
    entry,
    result,
)


def test_checked_in_baseline_is_valid() -> None:
    entries = baseline.load_baseline(common.ROOT, date(2026, 11, 14))
    assert isinstance(entries, dict)
    assert not baseline.compare_baselines(entries, entries)
    assert not baseline.compare_baselines(entries, {})
    assert baseline.compare_baselines({}, entries) == [
        f"baseline grew: {common.pretty(key)}" for key in sorted(entries)
    ]


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
    with pytest.raises(common.ScanError, match="baseline must"):
        baseline.baseline_findings(data)


@pytest.mark.parametrize("count", [0, common.MAX_BASELINE_ENTRIES, common.MAX_BASELINE_ENTRIES + 1])
def test_baseline_entry_cap(count: int) -> None:
    data = {"version": 2, "findings": [{}] * count}
    if count > common.MAX_BASELINE_ENTRIES:
        with pytest.raises(common.ScanError, match="at most"):
            baseline.baseline_findings(data)
    else:
        assert len(baseline.baseline_findings(data)) == count


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
    assert baseline.entry_problems(entry(**changes), {common.AUDIT_RULE})


@pytest.mark.parametrize("data", [None, {}, {"reason": 2}])
def test_entry_shape(data: object) -> None:
    assert baseline.entry_problems(data, None)


@pytest.mark.parametrize("category,days", [("A", 1), ("B", 3), ("C", 10), ("D", 120)])
def test_entry_category_boundaries(category: str, days: int) -> None:
    from datetime import timedelta

    sample = entry(
        category=category, expires=str(TODAY + timedelta(days=days)), count="99", reason="a" * 20
    )
    assert not baseline.entry_problems(sample, {common.AUDIT_RULE})
    assert not baseline.entry_problems(dict(sample, reason="a" * 300), None)
    assert not baseline.entry_expired(sample, date.fromisoformat(sample["expires"]))
    assert baseline.entry_expired(sample, date.fromisoformat(sample["expires"]) + timedelta(days=1))
    assert not baseline.entry_problems(dict(sample, patternId="old.rule"), None)


@pytest.mark.parametrize("text", ["2026-11-14", "2026-1-14", "2026-02-30", "", "2026-11-14\n"])
def test_parse_date(text: str) -> None:
    if text == "2026-11-14":
        assert scan.parse_today(text) == TODAY
    else:
        assert baseline.parse_date(text) is None
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
    with pytest.raises(common.ScanError, match=match):
        baseline.parse_baseline({"version": 2, "findings": samples}, None, day)


def test_baseline_counts_and_load(tmp_path: Path) -> None:
    sample = entry(count="2")
    document(
        tmp_path,
        common.CONFIG,
        {
            "tools": [
                {"useLocalConfigurationFile": False, "patterns": [{"patternId": common.AUDIT_RULE}]}
            ]
        },
    )
    document(tmp_path, common.BASELINE, {"version": 2, "findings": [sample]})
    entries = baseline.load_baseline(tmp_path, TODAY)
    assert baseline.baseline_counts(entries) == Counter({baseline.entry_key(sample): 2})
    assert baseline.parse_baseline({"version": 2, "findings": [sample]}, None, None) == entries
    with pytest.raises(common.ScanError, match="baseline.json"):
        baseline.load_baseline(tmp_path, date(2027, 3, 15))


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
    key = baseline.entry_key(old)
    assert bool(baseline.compare_baselines({key: old}, {key: dict(old, **fields)})) == refused


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
    assert (baseline.ref_problem(ref) is None) == allowed


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
    monkeypatch.setattr(baseline, "git", lambda *_args, **_kwargs: result(header, rc))
    if allowed:
        assert baseline.resolve_commit(tmp_path, "main") == header.split()[0]
    else:
        with pytest.raises(common.ScanError, match="not a commit"):
            baseline.resolve_commit(tmp_path, "main")
    with pytest.raises(common.ScanError, match="not a branch"):
        baseline.resolve_commit(tmp_path, "--bad")


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
    current = {baseline.entry_key(entry()): entry()}
    monkeypatch.setattr(baseline, "load_baseline", lambda *_args: current)
    monkeypatch.setattr(baseline, "resolve_commit", lambda *_args: "a" * 40)
    bodies = {
        "missing": "key missing\n",
        "bad": "a tree 0\n",
        "invalid-json": "a blob 1\n{\n",
        "invalid-baseline": "a blob 2\n{}\n",
        "unchanged": "a blob 1\n" + json.dumps({"version": 2, "findings": [entry()]}) + "\n",
        "grown": "a blob 1\n" + json.dumps({"version": 2, "findings": []}) + "\n",
    }
    monkeypatch.setattr(
        baseline, "git", lambda *_args, **_kwargs: result(bodies[body], rc, "synthetic")
    )
    if body in {"missing", "unchanged", "grown"} and rc == 0:
        problems = baseline.shrink_problems(tmp_path, "main", current)
        assert want in ("\n".join(problems) if problems else capsys.readouterr().out)
    else:
        with pytest.raises(common.ScanError, match=want):
            baseline.shrink_problems(tmp_path, "main", current)
