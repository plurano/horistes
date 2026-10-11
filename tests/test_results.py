# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Result reader: a SARIF file becomes checks and findings, and two results are compared.

The good file has the shape of a scorecard result: every check is a rule, a
check below full score also a result with one place and a message whose
first line carries the score.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

import pytest
from conftest import ROOT, archive, sarif_log

sys.path.insert(0, str(ROOT / "tools"))
import results

RULES = ("PinnedDependenciesID", "TokenPermissionsID", "VulnerabilitiesID")
PINNED = (
    "PinnedDependenciesID",
    ".github/workflows/planted.yml",
    7,
    "score is 7: dependency not pinned by hash\nWarn: third-party action\nWarn: container image",
)
TOKEN = ("TokenPermissionsID", ".github/workflows/ci.yml", 1, "score is 9: one workflow writes")
GOOD = sarif_log((PINNED, TOKEN), RULES)


def rest(*lines: str) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:12]


def test_checks_are_the_rules_and_findings_name_rule_place_score_and_details():
    assert results.from_sarif(GOOD) == {
        "tools": ["Scorecard v5.5.0"],
        "checks": list(RULES),
        "findings": [
            "PinnedDependenciesID | .github/workflows/planted.yml:7 | "
            "score is 7: dependency not pinned by hash | "
            + rest("Warn: third-party action", "Warn: container image"),
            "TokenPermissionsID | .github/workflows/ci.yml:1 | "
            f"score is 9: one workflow writes | {rest()}",
        ],
    }


def test_rule_of_a_result_is_a_check_even_when_the_tool_does_not_list_it():
    assert results.from_sarif(sarif_log((TOKEN,), ()))["checks"] == ["TokenPermissionsID"]


def test_order_of_the_detail_lines_does_not_count_and_their_text_does():
    swapped = (*PINNED[:3], "\n".join(PINNED[3].split("\n")[i] for i in (0, 2, 1)))
    assert swapped != PINNED
    assert results.from_sarif(sarif_log((swapped, TOKEN), RULES)) == results.from_sarif(GOOD)
    other = (*PINNED[:3], PINNED[3].replace("container image", "package manager"))
    assert results.from_sarif(sarif_log((other, TOKEN), RULES)) != results.from_sarif(GOOD)


@pytest.mark.parametrize(
    "changed",
    [
        (*PINNED[:3], PINNED[3].replace("score is 7", "score is 8")),
        (*PINNED[:2], 8, PINNED[3]),
        (PINNED[0], ".github/workflows/other.yml", *PINNED[2:]),
        ("DangerousWorkflowID", *PINNED[1:]),
    ],
    ids=["score", "line", "file", "rule"],
)
def test_score_place_and_rule_of_a_finding_count(changed):
    assert changed != PINNED
    found = results.from_sarif(sarif_log((changed, TOKEN), RULES))["findings"]
    assert found != results.from_sarif(GOOD)["findings"]


def test_what_names_the_run_is_left_out_and_several_runs_are_one_result():
    doc = json.loads(GOOD)
    doc["runs"][0]["automationDetails"]["id"] = "supply-chain/local/ffff-12 Oct 26 09:00 +0000"
    second = json.loads(sarif_log((), ("BranchProtectionID",)))["runs"][0]
    doc["runs"].append(second)
    read = results.from_sarif(json.dumps(doc))
    assert read["findings"] == results.from_sarif(GOOD)["findings"]
    assert read["checks"] == ["BranchProtectionID", *RULES]


UNREADABLE = [
    ("<html>", "result file is not JSON"),
    (GOOD.replace('"2.1.0"', '"2.0.0"'), "no SARIF 2.1.0 log"),
    ('{"version": "2.1.0"}', "no SARIF 2.1.0 log"),
    (GOOD.replace('"ruleId": "TokenPermissionsID", ', ""), "a result names no rule"),
    (GOOD.replace('"tool"', '"instrument"'), "a run names no tool"),
    (GOOD.replace('"rules": [', '"rules": "none", "listed": ['), "rules or results of a run"),
]


@pytest.mark.parametrize(("text", "message"), UNREADABLE, ids=[case[1] for case in UNREADABLE])
def test_file_that_is_no_result_is_refused(text, message):
    assert text != GOOD
    with pytest.raises(results.ResultError, match=message):
        results.from_sarif(text)


def test_more_results_than_the_limit_are_refused_not_cut(monkeypatch):
    monkeypatch.setattr(results, "MAX_RESULTS", 2)
    assert results.from_sarif(GOOD)["checks"] == list(RULES)
    with pytest.raises(results.ResultError, match="3 results in one run, at most 2 are read"):
        results.from_sarif(sarif_log((PINNED, TOKEN, TOKEN), RULES))


def test_archive_with_one_result_file_is_read_and_its_digest_recorded():
    data = archive({"results.sarif": GOOD})
    digest = hashlib.sha256(data).hexdigest()
    read = results.from_archive(data, f"sha256:{digest}")
    assert read == {"sha256": digest, **results.from_sarif(GOOD)}
    assert results.from_archive(data, "") == read


ARCHIVES = [
    (archive({"results.sarif": GOOD}), "sha256:" + "0" * 64, "the forge lists sha256:000000000000"),
    (archive({"results.sarif": GOOD}), "md5:0f", "the forge lists md5:0f"),
    (archive({"a.sarif": GOOD, "b.sarif": GOOD}), "", "2 files [*].sarif in the archive"),
    (archive({"results.json": GOOD}), "", "0 files [*].sarif in the archive"),
    (b"no archive", "", "archive cannot be read"),
    (archive({"results.sarif": "<html>"}), "", "result file is not JSON"),
]


@pytest.mark.parametrize(
    ("data", "digest", "message"), ARCHIVES, ids=[case[2][:30] for case in ARCHIVES]
)
def test_archive_that_holds_no_single_result_is_refused(data, digest, message):
    with pytest.raises(results.ResultError, match=message):
        results.from_archive(data, digest)


def test_result_file_over_the_limit_is_not_read_whatever_the_archive_says(monkeypatch):
    data = archive({"results.sarif": GOOD})
    monkeypatch.setattr(results, "MAX_BYTES", len(GOOD))
    assert results.from_archive(data, "")["checks"] == list(RULES)
    monkeypatch.setattr(results, "MAX_BYTES", len(GOOD) - 1)
    with pytest.raises(results.ResultError, match="results.sarif is larger than"):
        results.from_archive(data, "")


READ = {"state": "read", "artifact": "a", **results.from_sarif(GOOD)}
NONE = {"state": "none", "artifact": "a"}
EXPIRED = {"state": "expired", "artifact": "a"}


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (READ, ""),
        (NONE, ""),
        (EXPIRED, ""),
        (None, "no result is recorded"),
        ({"state": "fine"}, "no result is recorded"),
        ({"state": "unreadable", "detail": "archive cannot be read"}, "archive cannot be read"),
        ({"state": "unreadable"}, "no reason recorded"),
        ({"state": "read", "checks": ["a"]}, "no list `findings`"),
        ({"state": "read", "checks": "a", "findings": []}, "no list `checks`"),
        ({"state": "read", "checks": [], "findings": [1]}, "no list `findings`"),
    ],
)
def test_record_that_cannot_be_judged_says_why(record, reason):
    told = results.problem(record)
    assert (told == "") if not reason else (reason in told)


def without(record: dict, key: str, index: int = 0) -> dict:
    return {**record, key: [e for n, e in enumerate(record[key]) if n != index]}


def test_same_checks_and_findings_are_equal_and_the_marker_is_looked_for():
    assert results.compare(READ, dict(READ), None) == ("equal", "-", "")
    assert results.compare(READ, dict(READ), re.compile(r"planted\.yml:7 ")) == ("equal", "yes", "")
    assert results.compare(READ, dict(READ), re.compile("DangerousWorkflowID")) == (
        "equal",
        "no",
        "",
    )


def test_a_finding_or_a_check_on_one_side_only_differs_and_is_named():
    state, marker, note = results.compare(READ, without(READ, "findings"), None)
    assert (state, marker) == ("differ", "-")
    assert note == f"findings only in the old result: {READ['findings'][0]}"
    state, _, note = results.compare(without(READ, "checks", 2), READ, None)
    assert state == "differ"
    assert note == "checks only in the new result: VulnerabilitiesID"


def test_long_difference_names_three_and_counts_the_rest():
    many = {**READ, "checks": [*READ["checks"], *(f"Extra{n}ID" for n in range(5))]}
    _, _, note = results.compare(READ, many, None)
    assert note == "checks only in the new result: Extra0ID; Extra1ID; Extra2ID and 2 more"


def test_difference_names_the_tools_when_they_are_not_the_same():
    newer = {**without(READ, "findings"), "tools": ["Scorecard v5.6.0"]}
    _, _, note = results.compare(READ, newer, None)
    assert note.endswith("(tools: old Scorecard v5.5.0, new Scorecard v5.6.0)")


EMPTY = {"state": "read", "artifact": "a", "checks": [], "findings": []}


@pytest.mark.parametrize(
    ("old", "new", "state", "note"),
    [
        (NONE, NONE, "no-evidence", "neither job uploaded a result"),
        (READ, NONE, "differ", "the new job uploaded no result"),
        (NONE, READ, "differ", "the old job uploaded no result"),
        (READ, EXPIRED, "invalid", "a result has expired on the forge"),
        (EXPIRED, NONE, "invalid", "a result has expired on the forge"),
        (EMPTY, EMPTY, "no-evidence", "both results hold no check"),
        (EMPTY, READ, "differ", "checks only in the new result"),
    ],
)
def test_result_missing_on_a_side_is_never_equal(old, new, state, note):
    found = results.compare(old, new, re.compile("."))
    assert found[:2] == (state, "-")
    assert found[2].startswith(note)
