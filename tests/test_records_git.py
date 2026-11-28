# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Bounded Git output, surviving record patches and conversion attributes."""

from __future__ import annotations

import re
import sys

import pytest
import records
import records_diff
from records_cases import NEW, NEW_RECORD, NO_POINTER, OLD, OLD_RECORD, base_repo


@pytest.mark.parametrize("extra", [0, 64], ids=["at-budget", "over-budget"])
def test_planted_git_output_byte_budget_reaps_child(tmp_path, monkeypatch, extra):
    processes = []
    popen = records_diff.subprocess.Popen
    size = 128 + extra

    def writer(command, **kwargs):
        if command[:3] == ["git", "-C", str(tmp_path)]:
            script = (
                f"import sys,time; sys.stdout.buffer.write(b'x' * {size}); "
                f"sys.stdout.buffer.flush(); time.sleep({5 if extra else 0})"
            )
            process = popen([sys.executable, "-B", "-c", script], **kwargs)
            processes.append(process)
            return process
        return popen(command, **kwargs)

    monkeypatch.setattr(records_diff, "MAX_GIT_BYTES", 128, raising=False)
    monkeypatch.setattr(records_diff, "GIT_CHUNK_BYTES", 64, raising=False)
    monkeypatch.setattr(records_diff.subprocess, "Popen", writer)
    if extra:
        with pytest.raises(records.ciwf.ReadError, match="git diff.*byte budget 128"):
            records_diff.git(tmp_path, "diff")
    else:
        assert records_diff.git(tmp_path, "diff") == "x" * size
    assert processes and all(process.returncode is not None for process in processes)
    if extra:
        assert all(process.returncode != 0 for process in processes)


def test_planted_git_deadline_kills_and_reaps_child(tmp_path, monkeypatch):
    processes = []
    popen = records_diff.subprocess.Popen

    def slow(command, **kwargs):
        if command[:3] == ["git", "-C", str(tmp_path)]:
            process = popen([sys.executable, "-B", "-c", "import time; time.sleep(5)"], **kwargs)
            processes.append(process)
            return process
        return popen(command, **kwargs)

    monkeypatch.setattr(records_diff.subprocess, "Popen", slow)
    monkeypatch.setattr(records_diff, "DEADLINE_SECONDS", 0.1)
    monkeypatch.setattr(records_diff, "MAX_GIT_BYTES", 128, raising=False)
    with pytest.raises(records.ciwf.ReadError, match="git diff.*deadline.*byte budget 128"):
        records_diff.git(tmp_path, "diff")
    assert processes and all(process.returncode is not None for process in processes)


@pytest.mark.parametrize(
    "regular_before", [False, True], ids=["symlink-to-file", "file-to-symlink"]
)
def test_planted_record_type_change_keeps_surviving_side(tree, regular_before):
    repo = base_repo(tree)
    path = repo.root / OLD
    if not regular_before:
        path.unlink()
        path.symlink_to("../../tools/app.py")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "accepted symlink")
    path.unlink()
    if regular_before:
        path.symlink_to("../../tools/app.py")
        with pytest.raises(
            records.ciwf.ReadError, match=rf"^{re.escape(OLD)}: record is not a regular file$"
        ):
            records.check_records(repo.root, "main")
        done = repo.tool("records.py", "check", "--base-ref", "main", timeout=30)
        assert done.returncode == 3, done.stdout + done.stderr
        assert "record is not a regular file" in done.stderr
    else:
        path.write_text("## Decision\n\nThe app retries.\n", encoding="utf-8")
        assert records.check_records(repo.root, "main") == (
            [f"{OLD}:3: claim under 'Decision' {NO_POINTER}"],
            1,
            1,
        )
        assert records_diff.changed_records(repo.root, "main")[OLD].deletions == []


def test_planted_record_reader_refuses_symlink_to_tracked_markdown(tree):
    repo = base_repo(tree)
    regular = repo.root / OLD
    symlink = repo.root / NEW
    symlink.symlink_to(regular.name)
    assert records_diff.record_text(regular) == OLD_RECORD
    assert records._source_text(symlink) == ""
    with pytest.raises(
        records.ciwf.ReadError, match=rf"^{re.escape(str(symlink))}: record is not a regular file$"
    ):
        records_diff.record_text(symlink)


@pytest.mark.parametrize(
    ("attribute", "name"),
    [
        ("filter=strip", "filter"),
        ("filter", "filter"),
        ("working-tree-encoding=UTF-8", "working-tree-encoding"),
    ],
    ids=["clean-filter", "set-filter", "working-tree-encoding"],
)
def test_planted_record_conversion_attribute_fails_closed(tree, attribute, name):
    repo = base_repo(tree)
    repo.git("config", "filter.strip.clean", "sed 1,2d")
    repo.write(
        {
            ".gitattributes": f"docs/adr/*.md {attribute}\n",
            OLD: OLD_RECORD.replace("three times", "four times"),
        }
    )
    with pytest.raises(records.ciwf.ReadError, match=f"{OLD}.*{name}"):
        records.check_records(repo.root, "main")
    done = repo.tool("records.py", "check", "--base-ref", "main", timeout=30)
    assert done.returncode == 3, done.stdout + done.stderr
    assert OLD in done.stderr and name in done.stderr


@pytest.mark.parametrize("attributes", ["* text=auto", "* -filter -working-tree-encoding"])
def test_record_line_preserving_attributes_pass(tree, attributes):
    repo = base_repo(tree)
    repo.write({".gitattributes": attributes + "\n", NEW: NEW_RECORD})
    assert records.check_records(repo.root, "main")[0] == []


def test_record_attributes_checked_once_before_diff(tree, monkeypatch):
    repo = base_repo(tree)
    repo.write({NEW: NEW_RECORD, OLD: OLD_RECORD.replace("Accepted", "Proposed")})
    calls = []
    git = records_diff.git

    def traced(root, *args):
        calls.append(args)
        return git(root, *args)

    monkeypatch.setattr(records_diff, "git", traced)
    records_diff.changed_records(repo.root, "main")
    attributes = [call for call in calls if call[0] == "check-attr"]
    assert attributes == [("check-attr", "-z", "filter", "working-tree-encoding", "--", OLD, NEW)]
    assert calls.index(attributes[0]) < next(i for i, call in enumerate(calls) if "--text" in call)
