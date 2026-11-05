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


def findings(answers: dict[str, str], root: Path) -> list[str]:
    """Return every answer the tree at root contradicts."""
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
    return found


def _tree(tmp_path: Path, source: str) -> Path:
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
