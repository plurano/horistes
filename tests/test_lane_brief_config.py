# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Lane briefs require the anchors self-check before implementation or fixes launch."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".config/lane-brief.yaml"
ANCHORS_CHECK = "tools/anchors.py check"
TIMEOUT = 30
STAGES = {"impl", "fix", "review", "audit", "research"}
KEYS = {"required", "required_by_stage", "credential_stub_tests"}


def validate_shape(config: object) -> None:
    """Assert the reader's bounded shape and both stages' anchors requirement."""
    assert isinstance(config, dict), "config must be a mapping"
    assert config.keys() <= KEYS, "unknown top-level key"
    stages = config.get("required_by_stage")
    assert isinstance(stages, dict), "required_by_stage must be a mapping"
    assert len(stages) <= 8, "at most 8 stages"
    assert stages.keys() <= STAGES, "unknown stage"
    lists = [config.get("required", []), config.get("credential_stub_tests", []), *stages.values()]
    for entries in lists:  # At most five stages plus two top-level lists.
        assert isinstance(entries, list), "requirements must be lists"
        assert len(entries) <= 32, "at most 32 entries"
        for entry in entries:  # At most 32, checked above.
            assert isinstance(entry, str) and 1 <= len(entry) <= 200, "strings of 1..200 chars"
    for stage in ("impl", "fix"):
        assert ANCHORS_CHECK in stages.get(stage, []), f"{stage} must require {ANCHORS_CHECK}"


def test_config_shape():
    validate_shape(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({"required_by_stage": {"impl": [ANCHORS_CHECK]}}, "fix must require"),
        ({"unknown": []}, "unknown top-level key"),
        (
            {"required_by_stage": {"impl": [ANCHORS_CHECK], "fix": [ANCHORS_CHECK], "other": []}},
            "unknown stage",
        ),
        ({"required": ["x" * 201]}, "1..200 chars"),
    ],
    ids=["missing-fix", "unknown-key", "unknown-stage", "entry-too-long"],
)
def test_invalid_shape(replacement, message):
    config = copy.deepcopy(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))
    config.update(replacement)
    with pytest.raises(AssertionError, match=message):
        validate_shape(config)


def test_shape_boundary():
    config = copy.deepcopy(yaml.safe_load(CONFIG.read_text(encoding="utf-8")))
    config["required_by_stage"]["impl"] = [ANCHORS_CHECK] + ["x" * 200] * 31
    validate_shape(config)


def test_anchors_check_subcommand():
    anchors = ROOT / "tools/anchors.py"
    assert anchors.is_file(), "required anchors tool must exist"
    done = subprocess.run(
        [sys.executable, "-B", str(anchors), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "check" in done.stdout, done.stdout


@pytest.fixture
def reader():
    explicit = os.environ.get("LANE_BRIEF_STAGE_PY")
    directory = os.environ.get("LANE_TOOLS_DIR")
    if explicit is not None:
        path = Path(explicit)
    elif directory is not None:
        path = Path(directory) / "brief_stage.py"
    else:
        pytest.skip(
            "LANE_BRIEF_STAGE_PY and LANE_TOOLS_DIR unset; alternate coverage: "
            "shape cases + host replay in the PR evidence"
        )
    assert path.is_file(), "configured lane launcher's reader must be an existing file"
    return path.resolve()


@pytest.mark.parametrize(
    ("task", "with_check", "expected"),
    [
        ("feature_implementation", False, 1),
        ("ci_debugging", False, 1),
        ("feature_implementation", True, 0),
        ("ci_debugging", True, 0),
        ("code_review", False, 0),
    ],
    ids=["impl-missing", "fix-missing", "impl-present", "fix-present", "review"],
)
def test_reader_replay(tmp_path, reader, task, with_check, expected):
    worktree = tmp_path / "worktree"
    config = worktree / ".config/lane-brief.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    router = tmp_path / "router.yaml"
    router.write_text(
        "tiers:\n  test:\n    target_tasks: [feature_implementation, ci_debugging, code_review]\n",
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text(
        f"task: {task}\n" + (f"self-check: {ANCHORS_CHECK}\n" if with_check else ""),
        encoding="utf-8",
    )
    done = subprocess.run(
        [sys.executable, "-B", str(reader), "required", str(brief), str(worktree)],
        env={**os.environ, "LANE_ROUTER_CONFIG": str(router)},
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT,
    )
    assert done.returncode == expected, done.stdout + done.stderr
    if expected == 1:
        assert ANCHORS_CHECK in done.stdout.splitlines(), done.stdout
