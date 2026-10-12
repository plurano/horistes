# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Interface lock: the lock must describe the tree, and a break needs a higher version.

A break needs a higher interface_major; while that is 0, a higher interface_minor.
"""

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


def set_version(tree, major, minor):
    """Write the version fields of the lock in the tree."""
    path = tree.root / "interface.lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    lock["interface_major"], lock["interface_minor"] = major, minor
    path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    assert (lock["interface_major"], lock["interface_minor"]) == (0, 0)


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
@pytest.mark.parametrize("raised", [(1, 0), (0, 1)], ids=["major", "minor-at-0"])
def test_undeclared_break_fails_and_a_raised_version_declares_it(
    released, path, old, new, message, raised
):
    change(released, path, old, new)
    refused = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert refused.returncode == 1, refused.stdout
    assert message in refused.stderr
    assert "with the interface still at 0.0; raise interface_minor" in refused.stderr
    set_version(released, *raised)
    declared = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert declared.returncode == 0, declared.stderr


def finished(tree):
    """A released tree whose committed lock stands at interface 1.0."""
    set_version(tree, 1, 0)
    tree.git("commit", "-q", "-am", "1.0")
    return tree


@pytest.mark.parametrize(
    ("raised", "code"), [((1, 1), 1), ((1, 9), 1), ((2, 0), 0)], ids=["minor", "minors", "major"]
)
def test_from_major_1_only_a_higher_major_declares_a_break(released, raised, code):
    finished(released)
    change(released, WORKFLOW, "      path:\n", "      directory:\n")
    set_version(released, *raised)
    done = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == code, done.stdout + done.stderr
    if code:
        assert "raise interface_major" in done.stderr


def test_lower_major_with_a_higher_minor_does_not_declare_a_break(released):
    finished(released)
    change(released, WORKFLOW, "      path:\n", "      directory:\n")
    set_version(released, 0, 5)
    done = released.tool("interface.py", "check", "--base-ref", "HEAD")
    assert done.returncode == 1, done.stdout


def test_minor_that_is_no_integer_is_an_error(released):
    set_version(released, 0, "1")
    done = released.tool("interface.py", "check")
    assert done.returncode == 3
    assert "interface_minor is not an integer" in done.stderr


def test_snapshot_keeps_the_version(released):
    set_version(released, 0, 3)
    assert released.tool("interface.py", "snapshot").returncode == 0
    lock = json.loads((released.root / "interface.lock.json").read_text(encoding="utf-8"))
    assert (lock["interface_major"], lock["interface_minor"]) == (0, 3)


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


def test_null_base_of_a_new_branch_counts_every_entry_as_new(released):
    done = released.tool("interface.py", "check", "--base-ref", "0" * 40)
    assert done.returncode == 0, done.stderr
    assert "is the null commit" in done.stdout


def test_null_base_with_a_stale_lock_still_fails(released):
    text = (released.root / WORKFLOW).read_text(encoding="utf-8")
    released.write({WORKFLOW: text.replace("      path:\n", "      directory:\n")})
    done = released.tool("interface.py", "check", "--base-ref", "0" * 40)
    assert done.returncode == 1
    assert "does not describe the tree" in done.stderr


def test_base_one_digit_short_of_null_is_an_error(released):
    done = released.tool("interface.py", "check", "--base-ref", "0" * 39)
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
