# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Interface lock: the lock must describe the tree, and a break needs a higher interface_major."""

from __future__ import annotations

import json

import pytest
from conftest import ACTION, REUSABLE

WORKFLOW = ".github/workflows/licence.yml"
ACTION_FILE = "actions/draft-stop/action.yml"


@pytest.fixture
def released(tree):
    """A tree whose lock is committed, as a release would leave it."""
    tree.write({WORKFLOW: REUSABLE, ACTION_FILE: ACTION})
    assert tree.tool("interface.py", "snapshot").returncode == 0
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "release")
    return tree


def change(tree, path, old, new):
    text = (tree.root / path).read_text(encoding="utf-8")
    assert old in text, f"fixture no longer holds {old!r}"
    tree.write({path: text.replace(old, new)})
    assert tree.tool("interface.py", "snapshot").returncode == 0


def test_lock_records_what_a_caller_depends_on(released):
    lock = json.loads((released.root / "interface.lock.json").read_text(encoding="utf-8"))
    workflow = lock["workflows"][WORKFLOW]
    assert workflow["inputs"]["publish"] == {"type": "boolean", "required": False, "default": False}
    assert workflow["secrets"] == {"token": {"required": False}}
    assert workflow["outputs"] == ["verdict"]
    assert workflow["jobs"] == {"check": "Licence check"}
    assert workflow["write_scopes"] == []
    assert lock["actions"][ACTION_FILE]["outputs"] == ["stopped"]
    assert lock["interface_major"] == 0


def test_unchanged_tree_passes(released):
    done = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == 0, done.stderr
    assert "1 reusable workflows, 1 actions" in done.stdout


def test_stale_lock_fails(released):
    text = (released.root / WORKFLOW).read_text(encoding="utf-8")
    released.write({WORKFLOW: text.replace("      path:\n", "      directory:\n")})
    done = released.tool("interface.py", "check")
    assert done.returncode == 1
    assert "does not describe the tree" in done.stderr


BREAKS = [
    (WORKFLOW, "      path:\n", "      directory:\n", "input path removed"),
    (WORKFLOW, "        type: boolean\n", "        type: string\n", "input publish changed type"),
    (
        WORKFLOW,
        "        type: string\n        required: false\n",
        "        type: string\n        required: true\n",
        "input path became required",
    ),
    (
        WORKFLOW,
        "        required: false\n    outputs:",
        "        required: true\n    outputs:",
        "secret token became required",
    ),
    (
        WORKFLOW,
        "    name: Licence check\n    runs-on",
        "    name: REUSE\n    runs-on",
        "check name of job check changed",
    ),
    (WORKFLOW, "  contents: read\njobs:", "  contents: write\njobs:", "new write scope contents"),
    (WORKFLOW, "permissions:\n  contents: read\njobs:", "jobs:", "new write scope <unset>"),
    (
        WORKFLOW,
        "      verdict:\n        description: pass or fail",
        "      result:\n        description: pass or fail",
        "output verdict removed",
    ),
    (ACTION_FILE, "  message:\n", "  text:\n", "input message removed"),
    (ACTION_FILE, "  stopped:\n", "  halted:\n", "output stopped removed"),
]


@pytest.mark.parametrize(("path", "old", "new", "message"), BREAKS, ids=[b[3] for b in BREAKS])
def test_break_without_major_fails_and_passes_with_it(released, path, old, new, message):
    change(released, path, old, new)
    refused = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert refused.returncode == 1, refused.stdout
    assert message in refused.stderr
    lock_path = released.root / "interface.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["interface_major"] = 1
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    declared = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert declared.returncode == 0, declared.stderr


def test_removed_workflow_is_a_break(released):
    (released.root / WORKFLOW).unlink()
    assert released.tool("interface.py", "snapshot").returncode == 0
    done = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == 1
    assert f"{WORKFLOW}: removed" in done.stderr


def test_additions_are_compatible(released):
    change(
        released,
        WORKFLOW,
        "    secrets:\n",
        "      extra:\n        type: string\n        required: false\n    secrets:\n",
    )
    change(released, WORKFLOW, "        default: .\n", "        default: src\n")
    done = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == 0, done.stderr


def test_unknown_base_ref_is_an_error_not_a_pass(released):
    done = released.tool("interface.py", "check", "--base-ref", "no-such-ref")
    assert done.returncode == 3
    assert "is not a commit here" in done.stderr


def test_base_without_lock_counts_every_entry_as_new(tree):
    tree.write({"README.md": "start\n"})
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "start")
    tree.write({WORKFLOW: REUSABLE})
    assert tree.tool("interface.py", "snapshot").returncode == 0
    done = tree.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == 0, done.stderr
    assert "holds no interface.lock.json" in done.stdout


def test_missing_lock_is_an_error(tree):
    tree.write({WORKFLOW: REUSABLE})
    assert tree.tool("interface.py", "check").returncode == 3
