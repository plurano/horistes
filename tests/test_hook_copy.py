# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the commit-msg hook, hook_copy drift tool, and workflows."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import hook_copy
import render


def test_stocked_hook_passes_its_own_tests() -> None:
    """The stocked commit-msg hook passes its test suite."""
    hook = ROOT / "hooks/commit-msg.sh"
    test_sh = ROOT / "hooks/commit-msg_test.sh"
    done = subprocess.run(
        ["sh", str(test_sh), str(hook)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert "cases passed" in done.stdout


def test_exact_copy_passes(tmp_path: Path) -> None:
    """Exact copy of hooks passes drift check with exit code 0."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(ROOT / "hooks", copy_dir)
    status = hook_copy.main(["check", "--source", str(ROOT / "hooks"), "--copy", str(copy_dir)])
    assert status == 0


def test_planted_drift_in_commit_msg_fails_with_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drift in commit-msg.sh fails with exit 1 and prints both digests."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(ROOT / "hooks", copy_dir)
    target = copy_dir / "commit-msg.sh"
    target.write_bytes(target.read_bytes() + b"\n")
    status = hook_copy.main(["check", "--source", str(ROOT / "hooks"), "--copy", str(copy_dir)])
    assert status == 1
    captured = capsys.readouterr()
    assert "source sha256:" in captured.err
    assert "copy   sha256:" in captured.err


def test_planted_drift_in_commit_msg_test_fails_with_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drift in commit-msg_test.sh fails with exit 1 and prints both digests."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(ROOT / "hooks", copy_dir)
    target = copy_dir / "commit-msg_test.sh"
    target.write_bytes(target.read_bytes() + b"\n")
    status = hook_copy.main(["check", "--source", str(ROOT / "hooks"), "--copy", str(copy_dir)])
    assert status == 1
    captured = capsys.readouterr()
    assert "source sha256:" in captured.err
    assert "copy   sha256:" in captured.err


def test_missing_copy_fails(tmp_path: Path) -> None:
    """A missing copy file fails with exit 1."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(ROOT / "hooks", copy_dir)
    (copy_dir / "commit-msg.sh").unlink()
    status = hook_copy.main(["check", "--source", str(ROOT / "hooks"), "--copy", str(copy_dir)])
    assert status == 1


def test_symlinked_copy_fails(tmp_path: Path) -> None:
    """A symlinked copy file fails with exit 1."""
    copy_dir = tmp_path / "hooks"
    copy_dir.mkdir(parents=True)
    shutil.copy(ROOT / "hooks/commit-msg_test.sh", copy_dir / "commit-msg_test.sh")
    (copy_dir / "commit-msg.sh").symlink_to(ROOT / "hooks/commit-msg.sh")
    status = hook_copy.main(["check", "--source", str(ROOT / "hooks"), "--copy", str(copy_dir)])
    assert status == 1


def test_stocked_files_equal_snatcharr_source_except_spdx() -> None:
    """Stocked hook files match the SnatchArr source files except for SPDX line."""
    src_dir = ROOT / ".workingdir/src"
    if not (src_dir / "commit-msg.sh").is_file() or not (src_dir / "commit-msg_test.sh").is_file():
        pytest.skip("source copies are local only")

    for name in ("commit-msg.sh", "commit-msg_test.sh"):
        raw_source = (src_dir / name).read_text(encoding="utf-8")
        normalized_source = raw_source.replace(
            "# SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>",
            "# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>",
        )
        stocked = (ROOT / "hooks" / name).read_text(encoding="utf-8")
        assert stocked == normalized_source


def test_hook_copy_workflow_structure() -> None:
    """Structure and security invariants for hook-copy.yml."""
    doc = ciwf.load(ROOT / ".github/workflows/hook-copy.yml")
    assert doc.get("permissions") == {}
    job = doc["jobs"]["check"]
    assert job["name"] == "Hook copy"
    assert job["timeout-minutes"] == 5
    assert job["permissions"] == {"contents": "read"}
    steps = job["steps"]

    checkout_step = next(s for s in steps if s.get("name") == "Check out the hook source")
    with_block = checkout_step["with"]
    assert with_block["repository"] == "${{ job.workflow_repository }}"
    assert with_block["ref"] == "${{ job.workflow_sha }}"
    assert with_block["persist-credentials"] is False
    sparse = [
        line.strip() for line in with_block["sparse-checkout"].strip().splitlines() if line.strip()
    ]
    assert set(sparse) == {"hooks/", "tools/hook_copy.py"}
    assert with_block["sparse-checkout-cone-mode"] is False

    check_idx = next(i for i, s in enumerate(steps) if "hook_copy.py" in s.get("run", ""))
    test_idx = next(i for i, s in enumerate(steps) if "commit-msg_test.sh" in s.get("run", ""))
    assert check_idx < test_idx

    for step in steps:
        assert "${{" not in step.get("run", ""), f"unexpanded expression in {step.get('name')}"


def test_release_please_workflow_structure() -> None:
    """Structure and security invariants for release-please.yml."""
    doc = ciwf.load(ROOT / ".github/workflows/release-please.yml")
    assert doc.get("permissions") == {}
    job = doc["jobs"]["release-please"]
    assert job["name"] == "Release please"
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {}
    assert "release-type" not in str(job)

    steps = job["steps"]
    token_step = steps[0]
    rp_step = steps[1]
    assert (
        token_step["uses"]
        == "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"
    )
    assert (
        rp_step["uses"]
        == "googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7"
    )
    raw = (ROOT / ".github/workflows/release-please.yml").read_text(encoding="utf-8")
    assert "# v3.3.0" in raw
    assert "# v5.0.0" in raw


@pytest.mark.parametrize(
    "template_path",
    [
        ROOT / "templates/callers/release-please.yml.tmpl",
        ROOT / "templates/callers/hook-copy.yml.tmpl",
    ],
    ids=["release-please", "hook-copy"],
)
def test_caller_templates_render(template_path: Path) -> None:
    """Caller templates render cleanly and hook-copy reports required result job."""
    source = template_path.read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, str(template_path))
    problems = render.caller_problems(rendered, ROOT)
    assert problems == []

    doc = yaml.load(rendered, Loader=ciwf.StrictLoader)
    if template_path.name == "hook-copy.yml.tmpl":
        assert doc["jobs"]["result"]["name"] == "Hook copy"
