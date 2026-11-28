# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Decision-record diff behavior and planted regressions."""

from __future__ import annotations

import io
import shlex
import sys
from subprocess import Popen
from types import SimpleNamespace

import pytest
import records
import records_diff
from records_cases import (
    NEW,
    NEW_RECORD,
    NO_POINTER,
    OLD,
    OLD_RECORD,
    POINTED,
    UNPOINTED,
    base_repo,
    change,
    failures,
)


@pytest.mark.parametrize("setting", ["diff.mnemonicPrefix", "diff.noprefix"])
def test_planted_user_diff_prefix_cannot_hide_a_record(tree, setting):
    repo = base_repo(tree)
    repo.git("config", setting, "true")
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, UNPOINTED)})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]
    diff = records_diff.git(repo.root, *records_diff.DIFF, "main", "--", f":(literal){NEW}")
    assert f"+++ b/{NEW}\n" in diff


@pytest.mark.parametrize("pointed", [False, True], ids=["unpointed", "pointed"])
def test_planted_quoted_unicode_record_is_judged(tree, pointed):
    repo = base_repo(tree)
    name = "docs/adr/0002-café-decision.md"
    text = NEW_RECORD if pointed else NEW_RECORD.replace(POINTED, UNPOINTED)
    done = change(repo, {name: text})
    assert done.returncode == (0 if pointed else 1), done.stdout + done.stderr
    if pointed:
        assert "1 decision record(s) changed" in done.stdout
    else:
        assert failures(done) == [f"FAIL: {name}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("pattern", ["[ab]", "*"], ids=["brackets", "asterisk"])
def test_planted_record_pathspec_is_literal(tree, pattern):
    repo = base_repo(tree)
    name = f"docs/adr/0002-{pattern}-decision.md"
    sibling = "docs/adr/0002-a-decision.md"
    try:
        repo.write({name: OLD_RECORD, sibling: NEW_RECORD})
    except OSError as err:
        if pattern == "*" and err.errno == 22:
            pytest.skip(f"filesystem refuses an asterisk in a record name: {err}")
        raise
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "old records")
    # Only the sibling changes a claim; the literal record changes its status alone.
    done = change(
        repo,
        {
            name: OLD_RECORD.replace("Accepted", "Proposed"),
            sibling: NEW_RECORD.replace(POINTED, UNPOINTED),
        },
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {sibling}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize(
    ("base", "message"),
    [
        pytest.param("0" * 40, "null commit", id="null-sha1"),
        pytest.param("0" * 64, "null commit", id="null-sha256"),
        pytest.param("no-such-ref", "no merge base of no-such-ref", id="unknown-ref"),
    ],
)
def test_a_base_without_a_merge_base_fails_closed(tree, base, message):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD}, base=base)
    assert done.returncode == 3
    assert message in done.stderr


def test_an_unrelated_history_fails_closed(tree):
    """A shallow clone or a grafted history leaves no merge base; the check never guesses."""
    repo = base_repo(tree)
    repo.git("checkout", "-q", "--orphan", "lone")
    repo.git("commit", "-q", "--allow-empty", "-m", "lone")
    repo.git("checkout", "-q", "main")
    done = change(repo, {NEW: NEW_RECORD}, base="lone")
    assert done.returncode == 3
    assert "no merge base of lone and HEAD" in done.stderr


def test_planted_git_output_with_invalid_utf8_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)
    popen = Popen

    def undecodable(command, **kwargs):
        if command[:3] == ["git", "-C", str(repo.root)]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid filename")
        return popen(command, **kwargs)

    monkeypatch.setattr(records_diff.subprocess, "Popen", undecodable)
    with pytest.raises(records.ciwf.ReadError, match="git diff.*utf-8"):
        records_diff.git(repo.root, "diff", "-z", "--name-only", "main")


def test_planted_untracked_new_record_is_judged(tree):
    repo = base_repo(tree)
    repo.write({NEW: NEW_RECORD.replace(POINTED, UNPOINTED)})
    done = repo.tool("records.py", "check", "--base-ref", "main")
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("new_count", ["0", "1", ""])
def test_boundary_diff_hunk_counts(new_count):
    suffix = "," + new_count if new_count else ""
    body = "-old\n-other\n" + ("" if new_count == "0" else "+new\n")
    diff = f"+++ b/{OLD}\n@@ -12,2 +12{suffix} @@\n{body}"
    expected = set() if new_count == "0" else {12}
    assert records_diff.added_lines(diff) == expected


def test_boundary_diff_dev_null_clears_the_record():
    added = f"+++ b/{OLD}\n@@ -1 +1 @@\n-old\n+new\n"
    diff = f"--- a/{OLD}\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-one\n-two\n-three\n"
    assert records_diff.added_lines(added) == {1}
    assert records_diff.added_lines(diff) == set()


def test_deleted_record_has_no_added_lines(tree):
    repo = base_repo(tree)
    (repo.root / OLD).unlink()
    done = change(repo, {})
    assert done.returncode == 0, done.stderr
    assert "0 decision record(s) changed" in done.stdout


def test_renamed_record_is_judged_as_an_addition(tree):
    repo = base_repo(tree)
    (repo.root / OLD).rename(repo.root / NEW)
    done = change(repo, {})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [
        f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}",
        f"FAIL: {NEW}:16: claim under 'Consequences' {NO_POINTER}",
    ]


def test_planted_record_beyond_the_line_limit_fails_closed(tree, monkeypatch):
    base_repo(tree)
    monkeypatch.setattr(records, "MAX_LINES", 5)
    with pytest.raises(records.ciwf.ReadError, match="line limit"):
        records.blocks(NEW_RECORD)


def test_planted_diff_beyond_the_line_limit_fails_closed(monkeypatch):
    monkeypatch.setattr(records_diff, "MAX_LINES", 1)
    diff = "\n" * 11 + f"+++ b/{NEW}\n@@ -0,0 +1 @@\n"
    with pytest.raises(records.ciwf.ReadError, match="diff line limit"):
        records_diff.added_lines(diff)


def test_planted_changed_names_beyond_the_file_limit_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)
    monkeypatch.setattr(records_diff, "MAX_FILES", 1)
    monkeypatch.setattr(records_diff, "git", lambda *args: f"{OLD}\0{NEW}\0")
    with pytest.raises(records.ciwf.ReadError, match="changed file limit"):
        records_diff.changed_records(repo.root, "main")


def test_planted_combined_diffs_beyond_the_line_limit_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)

    git = records_diff.git

    def diff_output(root, *args):
        if args[0] == "check-attr":
            return git(root, *args)
        if "--name-only" in args:
            return f"{OLD}\0{NEW}\0"
        return "@@ -0,0 +1 @@\n" + "\n" * 5

    monkeypatch.setattr(records_diff, "MAX_LINES", 1)
    monkeypatch.setattr(records_diff, "git", diff_output)
    with pytest.raises(records.ciwf.ReadError, match="diff line limit"):
        records_diff.changed_records(repo.root, "main")


def test_planted_tracked_tree_beyond_the_file_limit_fails_closed(tree, monkeypatch):
    repo = base_repo(tree)
    monkeypatch.setattr(records, "MAX_FILES", 1)
    with pytest.raises(records.ciwf.ReadError, match="file limit"):
        records.read_tree(repo.root)


def test_untracked_record_with_invalid_utf8_fails_closed(tree):
    repo = base_repo(tree)
    (repo.root / NEW).write_bytes(b"\xff")
    done = repo.tool("records.py", "check", "--base-ref", "main")
    assert done.returncode == 3, done.stderr
    assert "ERROR:" in done.stderr
    assert "Traceback" not in done.stderr


def test_a_diverged_base_is_compared_through_its_merge_base(tree):
    repo = base_repo(tree)
    repo.write({OLD: OLD_RECORD.replace("three times", "five times")})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "base moves")
    repo.git("checkout", "-q", "-b", "topic", "HEAD~1")
    done = change(repo, {NEW: NEW_RECORD})
    assert done.returncode == 0, done.stderr
    assert "1 decision record(s) changed against main, 5 changed block(s) judged" in done.stdout


def test_planted_textconv_cannot_shift_record_lines(tree):
    repo = base_repo(tree)
    script = (
        "import pathlib,sys; sys.stdout.write(''.join("
        "pathlib.Path(sys.argv[1]).read_text(encoding='utf-8').splitlines(True)[10:]))"
    )
    repo.git("config", "diff.strip.textconv", shlex.join([sys.executable, "-c", script]))
    repo.write({".gitattributes": "docs/adr/*.md diff=strip\n"})
    done = change(repo, {OLD: OLD_RECORD.replace("three times", "four times")})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {OLD}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("edit_claim", [False, True], ids=["untouched", "edited"])
def test_planted_interhunk_context_judges_only_edits(tree, edit_claim):
    repo = base_repo(tree)
    # Keep the separator unchanged so this isolates inter-hunk context.
    repo.write({OLD: OLD_RECORD + "\n"})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted separator")
    repo.git("config", "diff.interHunkContext", "100")
    text = OLD_RECORD.replace("Accepted", "Proposed") + f"\n{POINTED}\n"
    if edit_claim:
        text = text.replace("three times", "four times")
    done = change(repo, {OLD: text})
    assert done.returncode == (1 if edit_claim else 0), done.stdout + done.stderr
    if edit_claim:
        assert failures(done) == [f"FAIL: {OLD}:12: claim under 'Decision' {NO_POINTER}"]
    else:
        assert "2 changed block(s) judged" in done.stdout


def test_planted_hunk_body_excludes_context_and_deleted_lines():
    diff = (
        "--- a/record\n+++ b/record\n@@ -10,3 +10,5 @@\n"
        "-removed\n+added\n unchanged\n+another\n\\ No newline at end of file\n"
        " unchanged too\n+last\n@@ -20 +21,0 @@\n-deleted\n"
    )
    assert records_diff.added_lines(diff) == {10, 12, 14}


@pytest.mark.parametrize("binary_attribute", [False, True], ids=["normal", "minus-diff"])
@pytest.mark.parametrize("invalid", [False, True], ids=["claim", "invalid-utf8"])
def test_planted_binary_attribute_cannot_hide_record(tree, binary_attribute, invalid):
    repo = base_repo(tree)
    if binary_attribute:
        repo.write({".gitattributes": "docs/adr/*.md -diff\n"})
    path = repo.root / NEW
    path.write_bytes(b"\xff" if invalid else NEW_RECORD.replace(POINTED, UNPOINTED).encode())
    done = change(repo, {})
    assert done.returncode == (3 if invalid else 1), done.stdout + done.stderr
    if invalid:
        assert "ERROR:" in done.stderr
        assert "Traceback" not in done.stderr
    else:
        assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize(
    "summary", ["Binary files a/record and b/record differ", "GIT binary patch"]
)
def test_planted_binary_summary_fails_closed(tree, monkeypatch, summary):
    repo = base_repo(tree)

    git = records_diff.git

    def binary_diff(root, *args):
        if args[0] == "check-attr":
            return git(root, *args)
        return f"{NEW}\0" if "--name-only" in args else summary + "\n"

    monkeypatch.setattr(records_diff, "git", binary_diff)
    with pytest.raises(records.ciwf.ReadError, match="binary record diff"):
        records_diff.changed_records(repo.root, "main")


@pytest.mark.parametrize("pointer_first", [False, True], ids=["last-line", "first-line"])
def test_planted_deletion_only_pointer_removal_judges_surviving_claim(tree, pointer_first):
    repo = base_repo(tree)
    claim, pointer = "The app waits.\n", "See `tools/app.py`.\n"
    before = "## Decision\n\n" + (pointer + claim if pointer_first else claim + pointer)
    repo.write({OLD: before})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted record")
    done = change(repo, {OLD: before.replace(pointer, "")})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {OLD}:3: claim under 'Decision' {NO_POINTER}"]
    assert records.check_records(repo.root, "main")[1:] == (1, 1)


@pytest.mark.parametrize("removed", ["Discarded claim.\n\n", "\nDiscarded claim.\n"])
def test_deleted_paragraph_and_blank_bind_no_untouched_neighbour(tree, removed):
    repo = base_repo(tree)
    before = "## Decision\n\nFirst untouched.\n\nDiscarded claim.\n\nLast untouched.\n"
    repo.write({OLD: before})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted record")
    done = change(repo, {OLD: before.replace(removed, "")})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "0 changed block(s) judged" in done.stdout


def test_deletion_walk_tracks_runs_and_new_side_coordinates():
    diff = (
        "--- a/record\n+++ b/record\n@@ -1,2 +0,0 @@\n-first\n-\n"
        "@@ -5,4 +3,4 @@\n-old\n+new\n context\n-\n-last\n"
        "\\ No newline at end of file\n+replacement\n+end\n"
    )
    changes = records_diff.diff_changes(diff)
    assert changes.added == {3, 5, 6}
    assert [(d.point, d.first, d.last) for d in changes.deletions] == [
        (0, True, False),
        (2, True, True),
        (4, False, True),
    ]


def test_deleted_record_is_never_read(tree, monkeypatch):
    repo = base_repo(tree)
    (repo.root / OLD).unlink()

    def unexpected_read(path):
        raise AssertionError(f"deleted record was read: {path}")

    monkeypatch.setattr(records_diff, "record_text", unexpected_read)
    assert records.check_records(repo.root, "main") == ([], 0, 0)


@pytest.mark.parametrize(
    ("before", "after", "finding"),
    [
        ("The app waits (`tools/app.py`).\n", "The app retries.\n", "claim under 'Decision'"),
        (
            "The app waits (`tools/app.py`).\n",
            '`DEADLINE_SECONDS = timeout_for("forge")`\n',
            "restated code",
        ),
        ("The app waits.\nSee `tools/app.py`.\n", "The app waits.\n", "claim under 'Decision'"),
    ],
    ids=["edit-claim", "copy-source", "delete-pointer"],
)
def test_planted_lf_line_numbers_keep_edited_claim_visible(tree, before, after, finding):
    repo = base_repo(tree)
    prefix = "# Title\u2028subtitle\n\n## Decision\n\n"
    repo.write({OLD: prefix + before})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted record with a Unicode separator")
    repo.write({OLD: prefix + after})
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 1)
    assert any(f"{OLD}:5: {finding}" in problem for problem in problems)
    assert all(problem.startswith(f"{OLD}:5:") for problem in problems)


@pytest.mark.parametrize("tracked", [False, True], ids=["untracked", "tracked"])
def test_planted_lone_cr_new_record_keeps_claim_visible(tree, tracked):
    repo = base_repo(tree)
    repo.git("checkout", "-q", "-b", "topic")
    repo.write({NEW: "# T" + "\r x" * 4 + "\n\n## Decision\n\nThe app retries.\n"})
    if tracked:
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "new record with lone CR")
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 1)
    assert problems == [f"{NEW}:5: claim under 'Decision' {NO_POINTER}"]


def test_planted_lone_cr_above_edited_claim_keeps_git_line_number(tree):
    repo = base_repo(tree)
    repo.write({OLD: "## Decision\nThe app waits.\n"})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted record")
    repo.git("checkout", "-q", "-b", "topic")
    repo.write({OLD: "## Decision\n# T" + "\r x" * 4 + "\nThe app retries.\n"})
    problems, changed, judged = records.check_records(repo.root, "main")
    assert (changed, judged) == (1, 1)
    assert problems == [f"{OLD}:3: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_tracked_crlf_record_matches_lf_findings(tree, newline):
    repo = base_repo(tree)
    repo.git("checkout", "-q", "-b", "topic")
    repo.write({NEW: "# T\n\n## Decision\n\nThe app retries.\n".replace("\n", newline)})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "new record")
    assert records.check_records(repo.root, "main") == (
        [f"{NEW}:5: claim under 'Decision' {NO_POINTER}"],
        1,
        1,
    )


@pytest.mark.parametrize("returncode", [0, 1], ids=["stdout-strict", "stderr-replacement"])
def test_planted_git_byte_output_decode_errors(tree, monkeypatch, returncode):
    repo = base_repo(tree)
    popen = Popen

    def byte_output(command, **kwargs):
        if command[:3] == ["git", "-C", str(repo.root)]:
            kwargs["stderr"].write(b"\xff")
            return SimpleNamespace(
                returncode=returncode,
                stdout=io.BytesIO(b"\xff"),
                wait=lambda **kwargs: returncode,
                poll=lambda: returncode,
            )
        return popen(command, **kwargs)

    monkeypatch.setattr(records_diff.subprocess, "Popen", byte_output)
    message = "git diff.*utf-8" if returncode == 0 else "git diff: \ufffd"
    with pytest.raises(records.ciwf.ReadError, match=message):
        records_diff.git(repo.root, "diff")


def test_diff_context_unicode_separator_is_one_git_line():
    changes = records_diff.diff_changes("@@ -1,2 +1,2 @@\n # A\u2028+B\n-old\n+new\n")
    assert changes.added == {2}
    assert [(d.point, d.first, d.last) for d in changes.deletions] == [(1, True, True)]


@pytest.mark.parametrize("extra", [0, 1], ids=["limit", "over-limit"])
def test_record_line_limit_counts_lf_only(tmp_path, extra):
    path = tmp_path / "record.md"
    text = "text\u2028continued\n" * (records.MAX_LINES + extra)
    path.write_bytes(text.encode())
    if extra:
        with pytest.raises(records.ciwf.ReadError, match="record line limit"):
            records_diff.record_text(path)
    else:
        assert records_diff.record_text(path) == text


@pytest.mark.parametrize("keepends", [False, True])
@pytest.mark.parametrize("text", ["", "a", "\n", "a\n", "a\n\n", "a\r\nb\r", "a\r\r\nb"])
def test_git_lines_preserve_lone_cr_and_terminal_lf_count(text, keepends):
    expected = {
        "": [],
        "a": ["a"],
        "\n": [""],
        "a\n": ["a"],
        "a\n\n": ["a", ""],
        "a\r\nb\r": ["a", "b\r"],
        "a\r\r\nb": ["a\r", "b"],
    }
    lines = records_diff.git_lines(text, keepends=keepends)
    assert len(lines) == len(expected[text])
    assert "".join(lines) == text if keepends else lines == expected[text]
