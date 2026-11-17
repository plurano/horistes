# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Decision-record reads behavior and planted regressions."""

from __future__ import annotations

import importlib.util
import io
import subprocess
from pathlib import Path

import pytest
import records
from records_cases import NEW, NEW_RECORD, NO_POINTER, OLD, base_repo


def test_planted_oversized_source_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)
    monkeypatch.setattr(records, "MAX_SOURCE_BYTES", 5)
    with pytest.raises(records.ciwf.ReadError, match="source byte limit"):
        records._source_text(repo.root / "tools/app.py")


@pytest.mark.parametrize("kind", ["source", "record"])
def test_planted_device_symlinks_are_not_read(tree, kind):
    repo = base_repo(tree)
    if not Path("/dev/zero").exists() or importlib.util.find_spec("resource") is None:
        pytest.skip("requires /dev/zero and the resource module for a bounded child")
    repo.git("checkout", "-q", "-b", "topic")
    repo.write({NEW: NEW_RECORD})
    name = "tools/zero" if kind == "source" else NEW
    path = repo.root / name
    if path.exists():
        path.unlink()
    path.symlink_to("/dev/zero")
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "device symlink")
    done = repo.tool(
        "records.py", "check", "--base-ref", "main", memory_limit=256 * 1024 * 1024, timeout=30
    )
    assert done.returncode == (0 if kind == "source" else 3), done.stdout + done.stderr
    assert ("PASS: 1 decision record(s) changed" if kind == "source" else "ERROR:") in (
        done.stdout + done.stderr
    )
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("reader", ["_source_text", "record_text"])
def test_planted_reader_caps_each_read(tmp_path, monkeypatch, reader):
    path = tmp_path / "regular"
    path.write_bytes(b"text")
    requested = []

    class ReadSpy(io.BytesIO):
        def read(self, size=-1):
            requested.append(size)
            assert 0 <= size <= records.MAX_SOURCE_BYTES + 1
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: ReadSpy(b"text"))
    assert getattr(records, reader)(path) == "text"
    assert requested == [records.MAX_SOURCE_BYTES + 1]


@pytest.mark.parametrize("reader", ["_source_text", "record_text"])
def test_planted_reader_byte_limit(tmp_path, reader):
    path = tmp_path / "regular"
    path.write_bytes(b"x" * (records.MAX_SOURCE_BYTES + 1))
    with pytest.raises(records.ciwf.ReadError, match=f"byte limit {records.MAX_SOURCE_BYTES}"):
        getattr(records, reader)(path)


@pytest.mark.parametrize("reader", ["_source_text", "record_text"])
def test_reader_byte_limit_boundary(tmp_path, reader):
    path = tmp_path / "regular"
    path.write_bytes(b"x" * records.MAX_SOURCE_BYTES)
    assert len(getattr(records, reader)(path)) == records.MAX_SOURCE_BYTES


@pytest.mark.parametrize("kind", ["missing", "directory", "binary"])
def test_reader_unsupported_source_is_empty_and_record_errors(tmp_path, kind):
    path = tmp_path / "input"
    if kind == "directory":
        path.mkdir()
    elif kind == "binary":
        path.write_bytes(b"\xff")
    assert records._source_text(path) == ""
    with pytest.raises(records.ciwf.ReadError):
        records.record_text(path)


def test_limited_tool_enforces_memory_and_preserves_arguments(tree, monkeypatch):
    import conftest

    if not Path("/dev/zero").exists() or importlib.util.find_spec("resource") is None:
        pytest.skip("requires /dev/zero and the resource module for a bounded child")
    tree.write(
        {
            "tools/limited_fixture.py": (
                "import resource, sys\n"
                "def main(argv):\n"
                "    assert resource.getrlimit(resource.RLIMIT_AS) == (67108864, 67108864)\n"
                "    print(repr(argv))\n"
                "    return 7\n"
                'if __name__ == "__main__":\n'
                "    sys.exit(main(sys.argv[1:]))\n"
            )
        }
    )
    monkeypatch.setattr(conftest, "ROOT", tree.root)
    done = tree.tool("limited_fixture.py", "check", "two words", memory_limit=67108864, timeout=30)
    assert done.returncode == 7, done.stdout + done.stderr
    assert done.stdout == repr(["check", "two words", "--root", str(tree.root)]) + "\n"


def test_limited_tool_obeys_timeout(tree, monkeypatch):
    import conftest

    if not Path("/dev/zero").exists() or importlib.util.find_spec("resource") is None:
        pytest.skip("requires /dev/zero and the resource module for a bounded child")
    tree.write(
        {
            "tools/slow_fixture.py": (
                "import time\ndef main(argv):\n    time.sleep(5)\n"
                'if __name__ == "__main__":\n    main([])\n'
            )
        }
    )
    monkeypatch.setattr(conftest, "ROOT", tree.root)
    with pytest.raises(subprocess.TimeoutExpired):
        tree.tool("slow_fixture.py", memory_limit=67108864, timeout=0.1)


def test_planted_dev_null_body_cannot_hide_an_added_claim(tree):
    repo = base_repo(tree)
    before = "# Record\n\n## Decision\n\nSee `tools/app.py`.\n"
    repo.write({OLD: before})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted record")
    repo.git("checkout", "-q", "-b", "topic")
    repo.write({OLD: before + "\nThe app retries.\n++ /dev/null\n"})
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 1)
    assert problems == [f"{OLD}:7: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("deleted", [False, True], ids=["hunk-body", "deletion-header"])
def test_planted_dev_null_is_a_deletion_only_in_the_diff_header(tmp_path, deleted):
    path = tmp_path / OLD
    path.parent.mkdir(parents=True)
    path.write_text("present\n", encoding="utf-8")
    diff = (
        f"--- a/{OLD}\n+++ "
        + ("/dev/null" if deleted else f"b/{OLD}")
        + "\n@@ -10,0 +11,2 @@\n+The app retries.\n+++ /dev/null\n"
    )
    changes = records._record_changes(tmp_path, OLD, diff)
    assert changes == (None if deleted else records.Changes(added={11, 12}))


def test_deleted_record_is_not_counted_or_judged(tree):
    repo = base_repo(tree)
    repo.git("checkout", "-q", "-b", "topic")
    (repo.root / OLD).unlink()
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "remove record")
    assert records.check_records(repo.root, "main") == ([], 0, 0)
