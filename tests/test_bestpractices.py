# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on .bestpractices.json: answers must match the tree they describe."""

from __future__ import annotations

import json
import re
from pathlib import Path

import anchors
import pytest
from conftest import ROOT

# Passing-level criteria whose upstream definition offers no N/A answer
# (coreinfrastructure/best-practices-badge criteria/criteria.yml, checked 2026-11-05).
NO_NA = frozenset(
    {
        "contribution",
        "contribution_requirements",
        "delivery_mitm",
        "delivery_unsigned",
        "description_good",
        "discussion",
        "dynamic_analysis",
        "dynamic_analysis_enable_assertions",
        "english",
        "enhancement_responses",
        "floss_license",
        "floss_license_osi",
        "interact",
        "know_common_errors",
        "know_secure_design",
        "license_location",
        "maintained",
        "no_leaked_credentials",
        "repo_distributed",
        "repo_interim",
        "repo_public",
        "repo_track",
        "report_archive",
        "report_process",
        "report_responses",
        "report_tracker",
        "sites_https",
        "test",
        "test_continuous_integration",
        "test_invocation",
        "test_most",
        "test_policy",
        "tests_are_added",
        "tests_documented_added",
        "version_semver",
        "version_tags",
        "version_unique",
        "vulnerabilities_critical_fixed",
        "vulnerabilities_fixed_60_days",
        "vulnerability_report_process",
    }
)

# Criteria that cannot be N/A once the code calls a cryptographic primitive.
CRYPTO_USE = (
    "crypto_published",
    "crypto_call",
    "crypto_floss",
    "crypto_working",
    "crypto_weaknesses",
)
CRYPTO_CALL = re.compile(r"\bhashlib\.|\bopenssl\b|\bhmac\.|\bcryptography\b")
LINK = re.compile(r"blob/main/([^\s#)]+)(#L\d)?")


def findings(
    answers: dict[str, str],
    root: Path,
    pins: tuple[anchors.Pin, ...] | None = None,
) -> list[str]:
    """Return every answer the tree at root contradicts."""
    pins = anchors.PINS if pins is None else pins
    found = [
        f"{name}: N/A is not offered"
        for name in sorted(NO_NA)
        if answers.get(f"{name}_status") == "N/A"
    ]
    sources = sorted(root.glob("tools/*.py"))
    if any(CRYPTO_CALL.search(path.read_text(encoding="utf-8")) for path in sources):
        found += [
            f"{name}: N/A while tools/ calls cryptography"
            for name in CRYPTO_USE
            if answers.get(f"{name}_status") == "N/A"
        ]
    for key, text in answers.items():
        found += [
            f"{key}: links missing {path}"
            for path, anchored in LINK.findall(text)
            if not anchored and not (root / path.rstrip(".,")).exists()
        ]
    found += anchors.findings(answers, root, pins)
    return found


def _tree(tmp_path: Path, source: str) -> Path:
    """Create a minimal tools/a.py tree for testing."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/a.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_committed_answers_match_the_tree() -> None:
    """The committed answers raise no finding against the repository."""
    answers = json.loads((ROOT / ".bestpractices.json").read_text(encoding="utf-8"))
    assert findings(answers, ROOT) == []


def test_crypto_na_refused_when_code_hashes(tmp_path: Path) -> None:
    """Planted: crypto answered N/A while a tool calls hashlib."""
    root = _tree(tmp_path, "import hashlib\nhashlib.sha256(b'')\n")
    assert findings({"crypto_call_status": "N/A"}, root, pins=()) == [
        "crypto_call: N/A while tools/ calls cryptography"
    ]


def test_na_refused_where_not_offered(tmp_path: Path) -> None:
    """Planted: N/A on a criterion whose definition has no N/A answer."""
    root = _tree(tmp_path, "")
    assert findings({"report_responses_status": "N/A"}, root, pins=()) == [
        "report_responses: N/A is not offered"
    ]


def test_crypto_na_allowed_without_crypto(tmp_path: Path) -> None:
    """Boundary: with no cryptographic call, crypto N/A stands."""
    root = _tree(tmp_path, "print('no crypto here')\n")
    assert (
        findings({"crypto_call_status": "N/A", "crypto_random_status": "N/A"}, root, pins=()) == []
    )


def test_missing_link_refused(tmp_path: Path) -> None:
    """Planted: an answer links a file that does not exist."""
    root = _tree(tmp_path, "")
    text = "Evidence: https://github.com/plurano/horistes/blob/main/GONE.md"
    assert findings({"x_justification": text}, root, pins=()) == [
        "x_justification: links missing GONE.md"
    ]


@pytest.mark.parametrize("case", ["GONE.md#L", "GONE.md#Lintro"])
def test_planted_missing_file_with_digitless_line_fragment(tmp_path: Path, case: str) -> None:
    """Planted: a digitless line fragment cannot hide a missing linked file."""
    root = _tree(tmp_path, "")
    text = f"Evidence: https://github.com/plurano/horistes/blob/main/{case}"
    assert findings({"x_justification": text}, root, pins=()) == [
        "x_justification: links missing GONE.md"
    ]


def test_missing_linked_file_with_anchor_gets_one_finding(tmp_path: Path) -> None:
    """A missing file with an anchor gets one finding, from the anchor check alone."""
    root = _tree(tmp_path, "")
    text = "Evidence: https://github.com/plurano/horistes/blob/main/GONE.md#L10"
    assert findings({"x_justification": text}, root, pins=()) == [
        "x_justification: anchor GONE.md#L10 links missing GONE.md"
    ]


def test_planted_stale_first_line(tmp_path: Path) -> None:
    """Planted: the first block no longer appears in the linked file."""
    root = _tree(tmp_path, "drifted\nsecond\n")
    pins = (anchors.Pin("tools/a.py", ("first",), ("second",), ("x_justification",)),)
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1-L2"
    assert findings({"x_justification": text}, root, pins) == [
        "pin tools/a.py first text 'first' matches 0 places",
        (
            "x_justification: anchor tools/a.py#L1-L2 matches no pin for this answer; "
            "run tools/anchors.py fix"
        ),
    ]


def test_planted_stale_last_line_of_range(tmp_path: Path) -> None:
    """Planted: the last block no longer appears after the pinned start."""
    root = _tree(tmp_path, "first\ndrifted\n")
    pins = (anchors.Pin("tools/a.py", ("first",), ("second",), ("x_justification",)),)
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1-L2"
    assert findings({"x_justification": text}, root, pins) == [
        "pin tools/a.py last text 'second' matches no place at or after line 1",
        (
            "x_justification: anchor tools/a.py#L1-L2 matches no pin for this answer; "
            "run tools/anchors.py fix"
        ),
    ]


@pytest.mark.parametrize("anchor", ["L1", "L5", "L0", "L2-L1"])
def test_planted_anchor_matching_no_pin(tmp_path: Path, anchor: str) -> None:
    """Planted: unknown, past-EOF, zero and inverted anchors all lack a pin."""
    root = _tree(tmp_path, "first\nsecond\n")
    text = f"Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#{anchor}"
    assert findings({"x_justification": text}, root, ()) == [
        (
            f"x_justification: anchor tools/a.py#{anchor} matches no pin for this answer; "
            "run tools/anchors.py fix"
        )
    ]


def test_boundary_anchor_on_last_line_passes(tmp_path: Path) -> None:
    """Boundary: an anchor targeting the exact last line of a file passes."""
    root = _tree(tmp_path, "first\nlast\n")
    pins = (anchors.Pin("tools/a.py", ("last",), ("last",), ("x_justification",)),)
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L2"
    assert findings({"x_justification": text}, root, pins) == []


def test_boundary_single_line_anchor_checks_both_texts(tmp_path: Path) -> None:
    """Boundary: both distinct pinned substrings can occupy the same line."""
    root = _tree(tmp_path, "prefix suffix\n")
    pins = (anchors.Pin("tools/a.py", ("prefix",), ("suffix",), ("x_justification",)),)
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1"
    assert findings({"x_justification": text}, root, pins) == []

    stale_second = (anchors.Pin("tools/a.py", ("prefix",), ("absent",), ("x_justification",)),)
    assert findings({"x_justification": text}, root, stale_second) == [
        "pin tools/a.py last text 'absent' matches no place at or after line 1",
        (
            "x_justification: anchor tools/a.py#L1 matches no pin for this answer; "
            "run tools/anchors.py fix"
        ),
    ]


def test_planted_ambiguous_first_text(tmp_path: Path) -> None:
    """Planted: two matching first blocks cannot identify a unique range."""
    root = _tree(tmp_path, "first\nfirst\nlast\n")
    pins = (anchors.Pin("tools/a.py", ("first",), ("last",), ("x_justification",)),)
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1-L3"
    assert findings({"x_justification": text}, root, pins) == [
        "pin tools/a.py first text 'first' matches 2 places",
        (
            "x_justification: anchor tools/a.py#L1-L3 matches no pin for this answer; "
            "run tools/anchors.py fix"
        ),
    ]


def test_planted_unused_pin(tmp_path: Path) -> None:
    """Planted: a valid pin without its declared answer's link is refused."""
    root = _tree(tmp_path, "first\n")
    pins = (anchors.Pin("tools/a.py", ("first",), ("first",), ("x_justification",)),)
    assert findings({}, root, pins) == ["pin tools/a.py#L1 is not linked by x_justification"]
