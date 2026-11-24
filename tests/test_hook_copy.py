# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the commit-msg hook, hook_copy drift tool, and workflows."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from conftest import MAX_WORKFLOW_STEPS, ROOT, assert_action_pin, uses_lines
from test_draft_release import check_download_artifact_step
from test_release_labels import check_release_checkout

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import hook_copy
import pins
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


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("commit-msg.sh", id="commit_msg"),
        pytest.param("commit-msg_test.sh", id="commit_msg_test"),
    ],
)
def test_planted_drift_fails_with_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], filename: str
) -> None:
    """Drift in a hook file fails with exit 1 and prints both digests."""
    copy_dir = tmp_path / "hooks"
    shutil.copytree(ROOT / "hooks", copy_dir)
    target = copy_dir / filename
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


def check_release_please_workflow(root: Path = ROOT) -> None:
    """Verify structure, security, and action pin invariants for release-please.yml."""
    workflow_path = root / ".github/workflows/release-please.yml"
    doc = ciwf.load(workflow_path)
    assert doc.get("permissions") == {}
    job = doc["jobs"]["release-please"]
    assert job["name"] == "Release please"
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {}
    assert "release-type" not in str(job)

    steps = job["steps"]
    raw_lines = uses_lines(workflow_path, "release-please")
    assert_action_pin(steps[0]["uses"], "actions/create-github-app-token", raw_lines[0])
    assert_action_pin(steps[1]["uses"], "googleapis/release-please-action", raw_lines[1])


def test_release_please_workflow_structure() -> None:
    """Structure and security invariants for release-please.yml."""
    check_release_please_workflow()


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


THIRD_PARTY_PIN = re.compile(
    r"^(\s*(?:-\s+)?uses:\s+(?!\./|\.\./|docker://)[^@\s]+@)"
    r"([^\s#]+)"
    r"(\s+# v)(\d+(?:\.\d+){0,2})$",
    re.MULTILINE,
)
APP_TOKEN_PIN = re.compile(
    r"(actions/create-github-app-token@)([^\s#]+)(\s+# v)(\d+)(?:\.(\d+))?(?:\.\d+)?"
)
SAMPLE_SHA = "a" * 40
SAMPLE_ACTION = "actions/create-github-app-token"


def _bump_pin_match(match: re.Match[str]) -> str:
    """Rewrite one third-party action pin with a distinct 40-hex sha and bumped patch."""
    prefix, sha, comment_prefix, version = match.groups()
    assert pins.COMMIT.fullmatch(sha) is not None
    new_sha = ("0" if sha[0] != "0" else "1") + sha[1:]
    padded = ([int(part) for part in version.split(".")] + [0, 0])[:3]
    padded[2] += 1
    return f"{prefix}{new_sha}{comment_prefix}{padded[0]}.{padded[1]}.{padded[2]}"


def _bump_app_token_match(match: re.Match[str]) -> str:
    """Rewrite one create-github-app-token pin with a distinct sha and bumped minor."""
    prefix, sha, comment_prefix, major, minor = match.groups()
    new_sha = ("0" if sha[0] != "0" else "1") + sha[1:]
    next_minor = (0 if minor is None else int(minor)) + 1
    return f"{prefix}{new_sha}{comment_prefix}{major}.{next_minor}.0"


def _check_merge_train_release(root: Path) -> None:
    check_release_checkout(root / ".github/workflows/merge-train-release.yml")


@pytest.mark.parametrize(
    ("rel_path", "checker"),
    [
        pytest.param(
            ".github/workflows/release-please.yml",
            check_release_please_workflow,
            id="release-please",
        ),
        pytest.param(
            ".github/workflows/draft-release.yml",
            check_download_artifact_step,
            id="draft-release",
        ),
        pytest.param(
            ".github/workflows/merge-train-release.yml",
            _check_merge_train_release,
            id="merge-train-release",
        ),
    ],
)
def test_renovate_pin_bump_replay_keeps_structure_checks_green(
    tmp_path: Path, rel_path: str, checker: Callable[[Path], None]
) -> None:
    """Renovate-style sha and patch-comment bumps keep converted workflow checks green."""
    source = (ROOT / rel_path).read_text(encoding="utf-8")
    original_shas = [m.group(2) for m in THIRD_PARTY_PIN.finditer(source)]
    assert original_shas, f"no third-party pins matched in {rel_path}"
    rewritten, count = THIRD_PARTY_PIN.subn(_bump_pin_match, source)
    assert count == len(original_shas)
    for old_sha in original_shas[:MAX_WORKFLOW_STEPS]:
        assert old_sha not in rewritten

    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rewritten, encoding="utf-8")
    checker(tmp_path)


@pytest.mark.parametrize(
    "version",
    [
        pytest.param("3", id="major-only"),
        pytest.param("3.3", id="major-minor"),
        pytest.param("3.3.0", id="semver"),
    ],
)
def test_single_action_minor_bump_replay_keeps_checks_green(tmp_path: Path, version: str) -> None:
    """A minor bump of create-github-app-token from a 1-to-3-part comment keeps checks green."""
    expected_comment = "# v3.1.0" if version == "3" else "# v3.4.0"
    workflows_dir = tmp_path / ".github/workflows"
    workflows_dir.mkdir(parents=True)
    for name in ("release-please.yml", "merge-train-release.yml"):
        raw = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        seeded, seeded_count = APP_TOKEN_PIN.subn(rf"\g<1>\g<2>\g<3>{version}", raw)
        assert seeded_count == 1
        rewritten, count = APP_TOKEN_PIN.subn(_bump_app_token_match, seeded)
        assert count == 1
        assert (
            re.search(
                rf"{re.escape(SAMPLE_ACTION)}@[0-9a-f]{{40}}\s+{re.escape(expected_comment)}$",
                rewritten,
                re.MULTILINE,
            )
            is not None
        )
        (workflows_dir / name).write_text(rewritten, encoding="utf-8")

    check_release_please_workflow(tmp_path)
    check_release_checkout(workflows_dir / "merge-train-release.yml")


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(None, id="uses-only"),
        pytest.param(f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # v3", id="major-only"),
        pytest.param(f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # v3.4", id="major-minor"),
        pytest.param(f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # v3.4.0", id="semver"),
    ],
)
def test_assert_action_pin_accepts_valid_pins(line: str | None) -> None:
    """Valid 40-hex commits and 1-to-3-part version comments pass assert_action_pin."""
    assert_action_pin(f"{SAMPLE_ACTION}@{SAMPLE_SHA}", SAMPLE_ACTION, line)


@pytest.mark.parametrize(
    ("uses", "line"),
    [
        pytest.param(f"{SAMPLE_ACTION}@v3", None, id="tag-pin"),
        pytest.param(f"{SAMPLE_ACTION}@64ab729", None, id="short-sha"),
        pytest.param(f"{SAMPLE_ACTION}@{'a' * 39}", None, id="sha-39-hex"),
        pytest.param(f"{SAMPLE_ACTION}@{'a' * 41}", None, id="sha-41-hex"),
        pytest.param(f"{SAMPLE_ACTION}@{'A' * 40}", None, id="uppercase-hex"),
        pytest.param(f"{SAMPLE_ACTION}@{'a' * 39}F", None, id="mixed-case-hex"),
        pytest.param(SAMPLE_ACTION, None, id="missing-at"),
        pytest.param(f"other/action@{SAMPLE_SHA}", None, id="wrong-action"),
        pytest.param(
            f"{SAMPLE_ACTION}@{SAMPLE_SHA}",
            f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA}",
            id="missing-comment",
        ),
        pytest.param(
            f"{SAMPLE_ACTION}@{SAMPLE_SHA}",
            f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # main",
            id="branch-comment",
        ),
        pytest.param(
            f"{SAMPLE_ACTION}@{SAMPLE_SHA}",
            f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # v1.2.3.4",
            id="four-part-version",
        ),
        pytest.param(
            f"{SAMPLE_ACTION}@{SAMPLE_SHA}",
            f"      - uses: {SAMPLE_ACTION}@{SAMPLE_SHA} # v3.4.0 extra",
            id="trailing-words",
        ),
    ],
)
def test_assert_action_pin_refuses_invalid_pins(uses: str, line: str | None) -> None:
    """Invalid refs, wrong action names, and missing/malformed version comments fail."""
    with pytest.raises(AssertionError) as caught:
        assert_action_pin(uses, SAMPLE_ACTION, line)
    message = str(caught.value)
    assert SAMPLE_ACTION in message
    assert (line if line is not None else uses) in message
