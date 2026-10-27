# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Secret-scan gate: exceptions expire, scan detects planted keys, callers render correctly."""

from __future__ import annotations

import contextlib
import datetime
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from conftest import ROOT
from test_secret_scan import gitleaks

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import render
import secret_scan
from secret_scan import ScanError, parse_exceptions, scan

COMMIT = "0123456789abcdef0123456789abcdef01234567"
FINGERPRINT = f"{COMMIT}:src/Example.cs:generic-api-key:12"
TODAY = datetime.date(2026, 10, 11)
WORKFLOW = ROOT / ".github/workflows/secret-scan.yml"
TEMPLATE = ROOT / "templates/callers/secret-scan.yml.tmpl"


def listing(expires: str, fingerprint: str = FINGERPRINT) -> str:
    return f"# an explanatory comment\n# expires {expires}: public upstream value\n{fingerprint}\n"


# ---------------------------------------------------------------------------
# Unit tests ported from upstream source
# ---------------------------------------------------------------------------


def test_reads_fingerprint_with_expiry() -> None:
    (entry,) = parse_exceptions(listing("2027-01-06"))
    assert entry.fingerprint == FINGERPRINT
    assert entry.active(TODAY)


def test_expiry_date_itself_is_no_longer_active() -> None:
    (entry,) = parse_exceptions(listing(TODAY.isoformat()))
    assert not entry.active(TODAY)
    assert entry.active(TODAY - datetime.timedelta(days=1))


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("no expiry", f"{FINGERPRINT}\n"),
        ("no reason", f"# expires 2027-01-06:\n{FINGERPRINT}\n"),
        ("impossible date", listing("2027-02-30")),
        ("path-only fingerprint", listing("2027-01-06", "src/Example.cs:generic-api-key:12")),
        (
            "short commit",
            listing("2027-01-06", f"{COMMIT[:7]}:src/Example.cs:generic-api-key:12"),
        ),
        ("wildcard path", listing("2027-01-06", f"{COMMIT}:src/*:generic-api-key:12")),
        (
            "one expiry for two entries",
            listing("2027-01-06") + f"{COMMIT}:src/Other.cs:generic-api-key:3\n",
        ),
        ("duplicate", listing("2027-01-06") + listing("2027-01-06")),
        ("dangling expiry", listing("2027-01-06") + "# expires 2027-01-06: nothing follows\n"),
    ],
)
def test_rejects_entries_that_could_widen_into_a_tier(label: str, text: str) -> None:
    with pytest.raises(ScanError):
        parse_exceptions(text)


def _decide(expires: str, found: list[str]) -> tuple[list[str], list[str], list[str]]:
    entries = parse_exceptions(listing(expires))
    with (
        patch.object(secret_scan, "load_exceptions", return_value=entries),
        patch.object(secret_scan, "findings", return_value=found),
    ):
        return scan("gitleaks", ".", TODAY)


def test_unexpired_exception_allows_only_its_own_finding() -> None:
    other = f"{COMMIT}:src/Other.cs:generic-api-key:3"
    assert _decide("2027-01-06", [FINGERPRINT, other]) == ([FINGERPRINT], [other], [])


def test_expired_exception_blocks_like_a_missing_one() -> None:
    assert _decide("2026-10-11", [FINGERPRINT]) == ([], [FINGERPRINT], [])


def test_reports_an_exception_that_matches_nothing() -> None:
    assert _decide("2027-01-06", []) == ([], [], [FINGERPRINT])


def _run_mocked_self_test(found: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with (
        patch.object(
            secret_scan,
            "fixture",
            side_effect=lambda root, name, side_branch=False: Path(root) / name,
        ),
        patch.object(secret_scan, "findings", return_value=found),
        contextlib.redirect_stderr(stderr),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        status = secret_scan.main(["--gitleaks", "gitleaks", "--self-test"])
    return status, stderr.getvalue()


def test_scanner_that_finds_no_planted_key_fails_with_a_clear_message() -> None:
    status, stderr = _run_mocked_self_test([])
    assert status == 2
    assert "self-test fixture 'valid': expected 1 planted finding, got 0" in stderr


def test_scanner_that_reports_extra_findings_fails_with_a_clear_message() -> None:
    findings = [FINGERPRINT, f"{COMMIT}:src/Other.cs:generic-api-key:3"]
    status, stderr = _run_mocked_self_test(findings)
    assert status == 2
    assert "expected 1 planted finding, got 2" in stderr


# ---------------------------------------------------------------------------
# Real Gitleaks tests on planted repositories
# ---------------------------------------------------------------------------


def test_real_gitleaks_clean_passes(tmp_path: Path) -> None:
    repo = secret_scan.fixture(tmp_path, "clean")
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 0


def test_real_gitleaks_planted_head_blocks(tmp_path: Path) -> None:
    repo = secret_scan.fixture(tmp_path, "head")
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 1


def test_real_gitleaks_planted_side_branch_blocks(tmp_path: Path) -> None:
    repo = secret_scan.fixture(tmp_path, "side", side_branch=True)
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 1


def test_real_gitleaks_planted_key_removed_later_blocks(tmp_path: Path) -> None:
    repo = secret_scan.fixture(tmp_path, "removed")
    (repo / "settings.py").unlink()
    secret_scan.git(repo, "add", "-A")
    secret_scan.git(repo, "commit", "--quiet", "--message", "remove key")
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 1


def test_real_gitleaks_unexpired_exception_allows(tmp_path: Path) -> None:
    today = datetime.datetime.now(datetime.UTC).date()
    repo = secret_scan.excepted(
        tmp_path, gitleaks(), "unexpired", today + datetime.timedelta(days=7)
    )
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 0


def test_real_gitleaks_exception_expiring_today_blocks(tmp_path: Path) -> None:
    today = datetime.datetime.now(datetime.UTC).date()
    repo = secret_scan.excepted(tmp_path, gitleaks(), "today", today)
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 1


def test_real_gitleaks_expired_exception_blocks(tmp_path: Path) -> None:
    today = datetime.datetime.now(datetime.UTC).date()
    repo = secret_scan.excepted(tmp_path, gitleaks(), "expired", today - datetime.timedelta(days=1))
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 1


@pytest.mark.parametrize(
    "malformation",
    ["no_expiry", "wildcard", "duplicate"],
)
def test_real_gitleaks_malformed_exception_list_exits_two(
    tmp_path: Path, malformation: str
) -> None:
    repo = secret_scan.fixture(tmp_path, f"malformed-{malformation}")
    found = secret_scan.findings(gitleaks(), repo)
    assert len(found) == 1
    fp = found[0]
    future = (datetime.datetime.now(datetime.UTC).date() + datetime.timedelta(days=7)).isoformat()
    if malformation == "no_expiry":
        content = f"{fp}\n"
    elif malformation == "wildcard":
        content = f"# expires {future}: reason\n*:*:*:*\n"
    else:
        content = f"# expires {future}: reason\n{fp}\n# expires {future}: reason\n{fp}\n"
    (repo / secret_scan.EXCEPTIONS).write_text(content, encoding="utf-8")
    assert secret_scan.main(["--gitleaks", gitleaks(), "--repository", str(repo)]) == 2


def test_real_gitleaks_self_test_exits_zero() -> None:
    assert secret_scan.main(["--gitleaks", gitleaks(), "--self-test"]) == 0


# ---------------------------------------------------------------------------
# Workflow structure tests
# ---------------------------------------------------------------------------


def test_workflow_permissions_and_shape() -> None:
    doc = ciwf.load(WORKFLOW)
    assert doc.get("permissions") == {}
    assert doc["jobs"]["scan"]["permissions"] == {"contents": "read"}
    assert doc["jobs"]["scan"]["timeout-minutes"] == 10
    assert doc["jobs"]["scan"]["name"] == "Secret scan"


def test_workflow_gitleaks_pins_match_ci() -> None:
    doc = ciwf.load(WORKFLOW)
    ci_doc = ciwf.load(ROOT / ".github/workflows/ci.yml")
    assert doc["env"]["GITLEAKS_VERSION"] == ci_doc["env"]["GITLEAKS_VERSION"]
    assert doc["env"]["GITLEAKS_SHA256"] == ci_doc["env"]["GITLEAKS_SHA256"]


def test_workflow_steps_guarded_after_draft_stop() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["scan"]["steps"]
    assert steps[0]["name"] == "Stop on a draft pull request"
    assert steps[0]["if"] == "github.event.pull_request.draft == true"
    for step in steps[1:]:
        assert step.get("if") == "github.event.pull_request.draft != true"


def test_workflow_script_checkout_parameters() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["scan"]["steps"]
    checkout = [s for s in steps if s.get("name") == "Check out the scan script"]
    assert len(checkout) == 1
    with_block = checkout[0]["with"]
    assert with_block["repository"] == "${{ job.workflow_repository }}"
    assert with_block["ref"] == "${{ job.workflow_sha }}"
    assert with_block["path"] == "secret-scan-source"
    assert with_block["persist-credentials"] is False
    assert with_block["sparse-checkout"].splitlines() == ["actions/", "tools/secret_scan.py"]
    assert with_block["sparse-checkout-cone-mode"] is False


def test_workflow_self_test_strictly_before_scan() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["scan"]["steps"]
    self_test_idx = [i for i, s in enumerate(steps) if "--self-test" in s.get("run", "")]
    scan_idx = [i for i, s in enumerate(steps) if "--repository" in s.get("run", "")]
    assert len(self_test_idx) == 1
    assert len(scan_idx) == 1
    assert self_test_idx[0] < scan_idx[0]


# ---------------------------------------------------------------------------
# Caller template tests
# ---------------------------------------------------------------------------


def test_caller_template_renders_and_reports_required_name() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, str(TEMPLATE))
    problems = render.caller_problems(rendered, ROOT)
    assert problems == []

    doc = yaml.load(rendered, Loader=ciwf.StrictLoader)
    assert doc["name"] == "Secret scan"
    assert "result" in doc["jobs"]
    assert doc["jobs"]["result"]["name"] == "Secret scan"
    assert doc["jobs"]["result"]["needs"] == "secret-scan"
    assert doc["jobs"]["secret-scan"]["name"] == "Secret scan"
