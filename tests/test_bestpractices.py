# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Checks on .bestpractices.json: answers must match the tree they describe."""

from __future__ import annotations

import json
import re
from pathlib import Path

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
LINK = re.compile(r"blob/main/([^\s#)]+)")
ANCHOR = re.compile(r"blob/main/([^\s#)]+)#L(\d+)(?:-L(\d+))?")

EVIDENCE: dict[str, tuple[str, str]] = {
    "tools/merge_train.py#L934-L963": (
        "def app_jwt(",
        'return f"{signing_input}.{b64url(done.stdout)}"',
    ),
    "tools/merge_train.py#L667": (
        "if hashlib.sha256(content).hexdigest() != sha:",
        "if hashlib.sha256(content).hexdigest() != sha:",
    ),
    "tools/merge_train.py#L708": (
        "blob_sha = hashlib.sha1(",
        "blob_sha = hashlib.sha1(",
    ),
    "tools/merge_train.py#L176-L214": (
        "def key_bits(",
        "return int(match.group(1)) if match else None",
    ),
    "tools/merge_train.py#L176-L236": (
        "def key_bits(",
        'f"the app key has {bits} bits; at least {MIN_KEY_BITS} are required"',
    ),
    "tests/test_merge_train.py#L1793-L1978": (
        "@pytest.mark.parametrize(",
        'assert forge.writes == [("merge", "PR_1", f"{1:040x}", "SQUASH")]',
    ),
    "tools/results.py#L119-L121": (
        "found = hashlib.sha256(data).hexdigest()",
        'raise ResultError(f"archive is sha256:{found[:12]}, the forge lists {digest[:19]}")',
    ),
    "tools/hook_copy.py#L16-L19": (
        "def _sha256(path: Path) -> str:",
        'return hashlib.file_digest(source, "sha256").hexdigest()',
    ),
    "tools/stranger.py#L39": (
        "digest, size = hashlib.sha256(), 0",
        "digest, size = hashlib.sha256(), 0",
    ),
}


def _is_outside(start: int, end: int, total: int) -> bool:
    """Return True if start or end line is 0, inverted, or past end of file."""
    if start < 1 or end < 1:
        return True
    return end < start or start > total or end > total


def _anchor_finding(
    key: str,
    path: str,
    start_s: str,
    end_s: str | None,
    root: Path,
    pins: dict[str, tuple[str, str]],
) -> str | None:
    """Return a finding string if an anchor in key's answer is invalid, else None."""
    if not (root / path).exists():
        return None
    anchor = f"{path}#L{start_s}-L{end_s}" if end_s else f"{path}#L{start_s}"
    if anchor not in pins:
        return f"{key}: anchor {anchor} is not pinned"
    start = int(start_s)
    end = int(end_s) if end_s else start
    lines = (root / path).read_text(encoding="utf-8").splitlines()
    if _is_outside(start, end, len(lines)):
        return f"{key}: anchor {anchor} is outside {path}"
    first_pin, last_pin = pins[anchor]
    if first_pin not in lines[start - 1]:
        return f"{key}: anchor {anchor} line {start} does not hold {first_pin}"
    if last_pin not in lines[end - 1]:
        return f"{key}: anchor {anchor} line {end} does not hold {last_pin}"
    return None


def _anchor_findings(
    answers: dict[str, str],
    root: Path,
    pins: dict[str, tuple[str, str]],
) -> list[str]:
    """Return findings for invalid anchor references across all answers."""
    found: list[str] = []
    for key, text in answers.items():
        for match in ANCHOR.finditer(text):
            err = _anchor_finding(key, match.group(1), match.group(2), match.group(3), root, pins)
            if err is not None:
                found.append(err)
    return found


def findings(
    answers: dict[str, str],
    root: Path,
    evidence: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    """Return every answer the tree at root contradicts."""
    pins = EVIDENCE if evidence is None else evidence
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
            for path in LINK.findall(text)
            if not (root / path.rstrip(".,")).exists()
        ]
    found += _anchor_findings(answers, root, pins)
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
    assert findings({"crypto_call_status": "N/A"}, root) == [
        "crypto_call: N/A while tools/ calls cryptography"
    ]


def test_na_refused_where_not_offered(tmp_path: Path) -> None:
    """Planted: N/A on a criterion whose definition has no N/A answer."""
    root = _tree(tmp_path, "")
    assert findings({"report_responses_status": "N/A"}, root) == [
        "report_responses: N/A is not offered"
    ]


def test_crypto_na_allowed_without_crypto(tmp_path: Path) -> None:
    """Boundary: with no cryptographic call, crypto N/A stands."""
    root = _tree(tmp_path, "print('no crypto here')\n")
    assert findings({"crypto_call_status": "N/A", "crypto_random_status": "N/A"}, root) == []


def test_missing_link_refused(tmp_path: Path) -> None:
    """Planted: an answer links a file that does not exist."""
    root = _tree(tmp_path, "")
    text = "Evidence: https://github.com/plurano/horistes/blob/main/GONE.md"
    assert findings({"x_justification": text}, root) == ["x_justification: links missing GONE.md"]


def test_missing_linked_file_with_anchor_gets_no_anchor_finding(tmp_path: Path) -> None:
    """A missing file with an anchor reports only links missing, no anchor finding."""
    root = _tree(tmp_path, "")
    text = "Evidence: https://github.com/plurano/horistes/blob/main/GONE.md#L10"
    assert findings({"x_justification": text}, root) == ["x_justification: links missing GONE.md"]


def test_planted_stale_first_line(tmp_path: Path) -> None:
    """Planted: the first line of an anchor range does not hold its pinned text."""
    root = _tree(tmp_path, "drifted\nsecond\n")
    pins = {"tools/a.py#L1-L2": ("first", "second")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1-L2"
    assert findings({"x_justification": text}, root, pins) == [
        "x_justification: anchor tools/a.py#L1-L2 line 1 does not hold first"
    ]


def test_planted_stale_last_line_of_range(tmp_path: Path) -> None:
    """Planted: the last line of an anchor range does not hold its pinned text."""
    root = _tree(tmp_path, "first\ndrifted\n")
    pins = {"tools/a.py#L1-L2": ("first", "second")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1-L2"
    assert findings({"x_justification": text}, root, pins) == [
        "x_justification: anchor tools/a.py#L1-L2 line 2 does not hold second"
    ]


def test_planted_unpinned_anchor(tmp_path: Path) -> None:
    """Planted: an anchor is not present in the allow-list pin table."""
    root = _tree(tmp_path, "first\n")
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1"
    assert findings({"x_justification": text}, root, {}) == [
        "x_justification: anchor tools/a.py#L1 is not pinned"
    ]


def test_planted_anchor_past_end_of_file(tmp_path: Path) -> None:
    """Planted: an anchor points past the last line of the file."""
    root = _tree(tmp_path, "first\n")
    pins = {"tools/a.py#L5": ("first", "first")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L5"
    assert findings({"x_justification": text}, root, pins) == [
        "x_justification: anchor tools/a.py#L5 is outside tools/a.py"
    ]


def test_planted_line_zero(tmp_path: Path) -> None:
    """Planted: an anchor specifies line 0."""
    root = _tree(tmp_path, "first\n")
    pins = {"tools/a.py#L0": ("first", "first")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L0"
    assert findings({"x_justification": text}, root, pins) == [
        "x_justification: anchor tools/a.py#L0 is outside tools/a.py"
    ]


def test_planted_inverted_range(tmp_path: Path) -> None:
    """Planted: an anchor range whose end line comes before its start line."""
    root = _tree(tmp_path, "first\nsecond\n")
    pins = {"tools/a.py#L2-L1": ("second", "first")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L2-L1"
    assert findings({"x_justification": text}, root, pins) == [
        "x_justification: anchor tools/a.py#L2-L1 is outside tools/a.py"
    ]


def test_boundary_anchor_on_last_line_passes(tmp_path: Path) -> None:
    """Boundary: an anchor targeting the exact last line of a file passes."""
    root = _tree(tmp_path, "first\nlast\n")
    pins = {"tools/a.py#L2": ("last", "last")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L2"
    assert findings({"x_justification": text}, root, pins) == []


def test_boundary_single_line_anchor_checks_both_texts(tmp_path: Path) -> None:
    """Boundary: a single-line anchor verifies both pinned texts against that line."""
    root = _tree(tmp_path, "prefix suffix\n")
    matching_pins = {"tools/a.py#L1": ("prefix", "suffix")}
    text = "Evidence: https://github.com/plurano/horistes/blob/main/tools/a.py#L1"
    assert findings({"x_justification": text}, root, matching_pins) == []

    stale_second = {"tools/a.py#L1": ("prefix", "absent")}
    assert findings({"x_justification": text}, root, stale_second) == [
        "x_justification: anchor tools/a.py#L1 line 1 does not hold absent"
    ]
