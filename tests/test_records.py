# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""tools/records.py: what a change adds to a decision record restates no code and points somewhere.

Each test commits a base, commits a change on a topic branch the way a pull request
would, and runs the check against the base, as CI does with BASE_REF.
"""

from __future__ import annotations

import importlib.util
import io
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import records
from conftest import ROOT, Tree

NO_POINTER = "names no tracked file, command, test function or link"
RESTATED = "restated code, cite the file instead:"

APP = '''\
"""Retry a forge read."""

# Run make verify BASE_REF=origin/main before a release.
# See tools/app.py read_with_retries(read) for the loop.

DEADLINE_SECONDS = timeout_for("forge")
RETRY_LIMIT = 3


def read_with_retries(read):
    for attempt in range(RETRY_LIMIT):
        if read(attempt):
            return True
    return False
'''

APP_TESTS = """\
def test_app_retries():
    assert True
"""

OLD = "docs/adr/0001-the-app-retries.md"
NEW = "docs/adr/0002-the-app-waits-for-the-forge.md"

# An accepted record from before the check: its Decision and Consequences name nothing.
OLD_RECORD = """\
# ADR-0001: The app retries

- **Status**: Accepted
- **Date**: 2026-11-01

## Context

A forge read fails now and then.

## Decision

The app retries a failed read three times.

## Consequences

A read takes longer when the forge is slow.
"""

NEW_RECORD = """\
# ADR-0002: The app waits for the forge

- **Status**: Accepted
- **Date**: 2026-11-16

## Context

The forge answers slowly while it recalculates.

## Decision

The app waits for the forge before it retries (`tools/app.py` `read_with_retries()`).

## Consequences

A slow forge delays a release; `test_app_retries` holds the order.
"""

UNPOINTED = "The app waits for the forge before it retries."
POINTED = "The app waits for the forge before it retries (`tools/app.py` `read_with_retries()`)."


@pytest.fixture
def repo(tree: Tree) -> Tree:
    """A base commit on main holding the app, its test and one accepted record."""
    tree.write(
        {
            "tools/app.py": APP,
            "tests/test_app.py": APP_TESTS,
            "docs/adr/README.md": "# Decision records\n",
            OLD: OLD_RECORD,
        }
    )
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "base")
    return tree


def change(repo: Tree, files: dict[str, str], base: str = "main") -> subprocess.CompletedProcess:
    """Commit files on the topic branch, then check the records against base."""
    repo.git("checkout", "-q", "-B", "topic")
    repo.write(files)
    repo.git("add", "-A")
    repo.git("commit", "-q", "--allow-empty", "-m", "change")
    return repo.tool("records.py", "check", "--base-ref", base)


def failures(done: subprocess.CompletedProcess) -> list[str]:
    return [line for line in done.stderr.splitlines() if line.startswith("FAIL: ")]


def test_planted_added_record_without_a_pointer_fails(repo):
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, UNPOINTED)})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_fixed_added_record_passes(repo):
    done = change(repo, {NEW: NEW_RECORD})
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "PASS: 1 decision record(s) changed against main, 5 changed block(s) judged\n"
    )


@pytest.mark.parametrize("setting", ["diff.mnemonicPrefix", "diff.noprefix"])
def test_planted_user_diff_prefix_cannot_hide_a_record(repo, setting):
    repo.git("config", setting, "true")
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, UNPOINTED)})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]
    diff = records.git(repo.root, *records.DIFF, "main", "--", f":(literal){NEW}")
    assert f"+++ b/{NEW}\n" in diff


@pytest.mark.parametrize("pointed", [False, True], ids=["unpointed", "pointed"])
def test_planted_quoted_unicode_record_is_judged(repo, pointed):
    name = "docs/adr/0002-café-decision.md"
    text = NEW_RECORD if pointed else NEW_RECORD.replace(POINTED, UNPOINTED)
    done = change(repo, {name: text})
    assert done.returncode == (0 if pointed else 1), done.stdout + done.stderr
    if pointed:
        assert "1 decision record(s) changed" in done.stdout
    else:
        assert failures(done) == [f"FAIL: {name}:12: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("pattern", ["[ab]", "*"], ids=["brackets", "asterisk"])
def test_planted_record_pathspec_is_literal(repo, pattern):
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


def test_planted_restated_span_fails_and_a_named_symbol_passes(repo):
    restated = POINTED.replace("`read_with_retries()`", '`DEADLINE_SECONDS = timeout_for("forge")`')
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, restated)})
    assert done.returncode == 1
    assert failures(done) == [
        f'FAIL: {NEW}:12: {RESTATED} `DEADLINE_SECONDS = timeout_for("forge")`'
    ]
    named = POINTED.replace("`read_with_retries()`", "`DEADLINE_SECONDS`")
    assert change(repo, {NEW: NEW_RECORD.replace(POINTED, named)}).returncode == 0


def test_planted_restated_fenced_block_fails(repo):
    fenced = f"{POINTED}\n\n```python\nfor attempt in range(RETRY_LIMIT):\n```"
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, fenced)})
    assert done.returncode == 1
    assert failures(done) == [f"FAIL: {NEW}:14: {RESTATED} `for attempt in range(RETRY_LIMIT):`"]


@pytest.mark.parametrize(
    "span",
    [
        pytest.param("`RETRY_LIMIT = 3`", id="shorter-than-the-floor"),
        pytest.param('`DEADLINE_SECONDS = timeout_for("hub")`', id="not-in-tracked-source"),
        pytest.param("`make verify BASE_REF=origin/main`", id="a-command"),
        pytest.param("`tools/app.py read_with_retries(read)`", id="a-path-first"),
    ],
)
def test_boundary_spans_that_restate_nothing_pass(repo, span):
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, f"{POINTED} {span}")})
    assert done.returncode == 0, done.stderr


def test_source_held_only_in_markdown_is_not_restated(repo):
    note = 'The tuning note keeps `DEADLINE_SECONDS = timeout_for("hub")` as an example.\n'
    restated = POINTED.replace("`read_with_retries()`", '`DEADLINE_SECONDS = timeout_for("hub")`')
    files = {"docs/tuning.md": note, "NOTES.md": note, NEW: NEW_RECORD.replace(POINTED, restated)}
    assert change(repo, files).returncode == 0


@pytest.mark.parametrize(
    "pointer",
    [
        pytest.param("`tools/app.py`", id="tracked-file"),
        pytest.param("`tools/app.py:7`", id="tracked-file-line"),
        pytest.param("`tools/app.py::read_with_retries`", id="tracked-file-symbol"),
        pytest.param("`tools/`", id="tracked-directory"),
        pytest.param("`make verify`", id="make"),
        pytest.param("`praetorctl audit`", id="praetorctl"),
        pytest.param("`test_app_retries`", id="test-function"),
        pytest.param("[the app](../../tools/app.py)", id="relative-link"),
        pytest.param("[the app](../../tools/app.py#L7)", id="relative-link-anchor"),
        pytest.param("[the record](0001-the-app-retries.md)", id="sibling-record"),
        pytest.param("[forge docs](https://example.invalid/forge)", id="web-page"),
    ],
)
def test_pointer_kinds_pass(repo, pointer):
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, f"{UNPOINTED} See {pointer}.")})
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "pointer",
    [
        pytest.param("`tools/missing.py`", id="untracked-file"),
        pytest.param("`test_app_waits`", id="undefined-test"),
        pytest.param("`read_with_retries()`", id="bare-symbol"),
        pytest.param("[the app](../../tools/missing.py)", id="relative-link-to-nothing"),
        pytest.param("[decision](#decision)", id="same-page-anchor"),
        pytest.param("[mail](mailto:forge@example.invalid)", id="mail-address"),
        pytest.param("tools/app.py", id="path-outside-a-code-span"),
    ],
)
def test_pointers_to_nothing_fail(repo, pointer):
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, f"{UNPOINTED} See {pointer}.")})
    assert done.returncode == 1
    assert len(failures(done)) == 1


def test_planted_number_outside_claim_sections_needs_a_pointer(repo):
    context = "The forge answers slowly while it recalculates."
    measured = "The forge answered 41 of 42 reads within the deadline."
    done = change(repo, {NEW: NEW_RECORD.replace(context, measured)})
    assert done.returncode == 1
    assert failures(done) == [f"FAIL: {NEW}:8: claim under 'Context' {NO_POINTER}"]
    sourced = f"{measured} [sample](https://example.invalid/reads)"
    assert change(repo, {NEW: NEW_RECORD.replace(context, sourced)}).returncode == 0


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Measured on 2026-11-16T09:00Z after ADR-0001.", id="date-and-record"),
        pytest.param("1. The forge answers slowly.", id="list-number"),
        pytest.param("The `RETRY_LIMIT` of `tools/app.py` sets it.", id="number-free-span"),
    ],
)
def test_uncounted_numbers_are_no_claim(repo, text):
    context = "The forge answers slowly while it recalculates."
    assert change(repo, {NEW: NEW_RECORD.replace(context, text)}).returncode == 0


def test_numbers_under_references_are_no_claim(repo):
    references = f"{NEW_RECORD}\n## References\n\n- Forge outage of 2026-10-30, 41 reads lost.\n"
    assert change(repo, {NEW: references}).returncode == 0


def test_untouched_blocks_of_an_accepted_record_stay_unjudged(repo):
    added = f"{OLD_RECORD}\nThe retries are counted in `tools/app.py` `RETRY_LIMIT`.\n"
    done = change(repo, {OLD: added})
    assert done.returncode == 0, done.stderr
    assert "1 changed block(s) judged" in done.stdout


def test_a_status_change_of_an_accepted_record_passes(repo):
    superseded = OLD_RECORD.replace("Accepted", "Superseded by [ADR-0002](0002-x.md)")
    assert change(repo, {OLD: superseded}).returncode == 0


def test_a_changed_line_binds_the_whole_old_block(repo):
    edited = OLD_RECORD.replace("three times", "four times")
    done = change(repo, {OLD: edited})
    assert done.returncode == 1
    assert failures(done) == [f"FAIL: {OLD}:12: claim under 'Decision' {NO_POINTER}"]


def test_files_outside_the_record_pattern_are_not_judged(repo):
    done = change(repo, {"docs/adr/README.md": "# Decision records\n\nIt lists 88 records.\n"})
    assert done.returncode == 0
    assert done.stdout.startswith("PASS: 0 decision record(s) changed")


def test_a_comment_block_is_no_claim(repo):
    comment = f"{NEW_RECORD}\n<!-- Reviewed 3 times. -->\n"
    assert change(repo, {NEW: comment}).returncode == 0


@pytest.mark.parametrize(
    ("base", "message"),
    [
        pytest.param("0" * 40, "null commit", id="null-sha1"),
        pytest.param("0" * 64, "null commit", id="null-sha256"),
        pytest.param("no-such-ref", "no merge base of no-such-ref", id="unknown-ref"),
    ],
)
def test_a_base_without_a_merge_base_fails_closed(repo, base, message):
    done = change(repo, {NEW: NEW_RECORD}, base=base)
    assert done.returncode == 3
    assert message in done.stderr


def test_an_unrelated_history_fails_closed(repo):
    """A shallow clone or a grafted history leaves no merge base; the check never guesses."""
    repo.git("checkout", "-q", "--orphan", "lone")
    repo.git("commit", "-q", "--allow-empty", "-m", "lone")
    repo.git("checkout", "-q", "main")
    done = change(repo, {NEW: NEW_RECORD}, base="lone")
    assert done.returncode == 3
    assert "no merge base of lone and HEAD" in done.stderr


ADR_0066 = "docs/adr/0066-a-train-write-holds-the-landing-order.md"
RECORD_FIXTURES = ROOT / "tests/fixtures/records"


def test_adr_0066_as_landed_replays_red(tree):
    """The record that restated the train's code, added again on top of the source it quoted."""
    source = (RECORD_FIXTURES / "merge_train.py.txt").read_text(encoding="utf-8")
    tree.write({"tools/merge_train.py": source})
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "base")
    record = (RECORD_FIXTURES / "0066-as-landed.md.txt").read_text(encoding="utf-8")
    done = change(tree, {ADR_0066: record})
    assert done.returncode == 1
    found = failures(done)
    assert f"FAIL: {ADR_0066}:25: claim under 'Measurement'" in "\n".join(found)
    assert f"FAIL: {ADR_0066}:46: claim under 'Decision'" in "\n".join(found)
    assert f'FAIL: {ADR_0066}:30: {RESTATED} `pull["mergeStateStatus"] == "BEHIND"`' in found


def test_planted_adr_0066_replay_ignores_live_tree(tree, monkeypatch, tmp_path_factory):
    """A later source refactor cannot change the historical replay's inputs."""
    refactored = Tree(tmp_path_factory.mktemp("refactored-live-tree"))
    refactored.write(
        {
            "tools/merge_train.py": "# The train source no longer holds the quoted span.\n",
            ".github/workflows/merge-train.yml": "# The train workflow was refactored.\n",
            ADR_0066: (RECORD_FIXTURES / "0066-as-landed.md.txt").read_text(encoding="utf-8"),
        }
    )
    monkeypatch.setitem(globals(), "ROOT", refactored.root)
    test_adr_0066_as_landed_replays_red(tree)


def test_planted_git_output_with_invalid_utf8_fails_closed(repo, monkeypatch):
    run = subprocess.run

    def undecodable(command, **kwargs):
        if command[:3] == ["git", "-C", str(repo.root)]:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid filename")
        return run(command, **kwargs)

    monkeypatch.setattr(records.subprocess, "run", undecodable)
    with pytest.raises(records.ciwf.ReadError, match="git diff.*utf-8"):
        records.git(repo.root, "diff", "-z", "--name-only", "main")


@pytest.mark.parametrize(
    ("text", "spans"),
    [
        pytest.param("`a`", ["a"], id="plain"),
        pytest.param("`` a`b ``", ["a`b"], id="double-backticks-strip-one-space"),
        pytest.param("`  `", ["  "], id="blank-span-keeps-its-spaces"),
        pytest.param("`one\ntwo`", ["one two"], id="newline-is-a-space"),
        pytest.param("``a`", [], id="unmatched-run"),
    ],
)
def test_boundary_code_spans(text, spans):
    assert records.code_spans(text) == spans


def test_boundary_blocks_follow_fences_and_headings():
    text = "## Decision\n\n```\n## Context\n```\n####### seven\n- one\n  nested\n- two\n"
    found = records.blocks(text)
    assert [(b.section, b.start, b.end, b.fenced) for b in found] == [
        ("Decision", 3, 5, True),
        ("Decision", 6, 6, False),
        ("Decision", 7, 8, False),
        ("Decision", 9, 9, False),
    ]


@pytest.mark.parametrize(
    "comment",
    [
        """<!--

## Decision

```python
for attempt in range(RETRY_LIMIT):
```

Hidden 42 claims.
-->""",
        '<!-- `DEADLINE_SECONDS = timeout_for("forge")` -->',
    ],
    ids=["hidden-blocks", "hidden-span"],
)
def test_planted_code_and_claims_inside_html_comments_are_ignored(repo, comment):
    done = change(repo, {NEW: NEW_RECORD + "\n" + comment + "\n"})
    assert done.returncode == 0, done.stderr


def test_planted_comment_cannot_hide_a_visible_claim(repo):
    text = NEW_RECORD.replace(POINTED, "<!-- explanation --> " + UNPOINTED)
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_planted_hidden_pointer_does_not_support_visible_claim(repo):
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " <!-- `tools/app.py` -->")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_planted_untracked_new_record_is_judged(repo):
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
    assert records.added_lines(diff) == expected


def test_boundary_diff_dev_null_clears_the_record():
    added = f"+++ b/{OLD}\n@@ -1 +1 @@\n-old\n+new\n"
    diff = f"--- a/{OLD}\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-one\n-two\n-three\n"
    assert records.added_lines(added) == {1}
    assert records.added_lines(diff) == set()


def test_deleted_record_has_no_added_lines(repo):
    (repo.root / OLD).unlink()
    done = change(repo, {})
    assert done.returncode == 0, done.stderr
    assert "0 decision record(s) changed" in done.stdout


def test_renamed_record_is_judged_as_an_addition(repo):
    (repo.root / OLD).rename(repo.root / NEW)
    done = change(repo, {})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [
        f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}",
        f"FAIL: {NEW}:16: claim under 'Consequences' {NO_POINTER}",
    ]


def test_crlf_record_with_a_fence_info_string_is_judged(repo):
    text = NEW_RECORD + "\n```python title=retry\nfor attempt in range(RETRY_LIMIT):\n```\n"
    repo.write({NEW: text.replace("\n", "\r\n")})
    done = change(repo, {})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:18: {RESTATED} `for attempt in range(RETRY_LIMIT):`"]


def test_doubled_backtick_span_restating_source_fails(repo):
    span = '`` DEADLINE_SECONDS = timeout_for("forge") ``'
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, POINTED + " " + span)})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [
        f'FAIL: {NEW}:12: {RESTATED} `DEADLINE_SECONDS = timeout_for("forge")`'
    ]


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("[42 reads](https://example.invalid/reads)", True),
        ("[reads](https://example.invalid/42)", False),
    ],
    ids=["label-counts", "url-does-not"],
)
def test_boundary_numbers_in_link_labels_and_destinations(text, number):
    assert records.states_number(records.Block("Context", 1, [text])) is number


def test_changed_line_binds_restated_span_on_unchanged_line(repo):
    text = OLD_RECORD.replace(
        "The app retries a failed read three times.",
        'The app retries.\n`DEADLINE_SECONDS = timeout_for("forge")`',
    )
    repo.write({OLD: text})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "old restatement")
    edited = text.replace("The app retries.", "The app waits before retrying.")
    done = change(repo, {OLD: edited})
    assert done.returncode == 1, done.stdout
    assert any(RESTATED in line for line in failures(done))


@pytest.mark.parametrize(
    "text",
    [
        "`<!-- literal -->`",
        "`` <!-- literal ` marker --> ``",
        "```python\n<!-- literal -->\n```",
    ],
    ids=["span", "doubled-span", "fence"],
)
def test_comment_markers_inside_code_stay_literal(text):
    assert records.visible_text(text) == text


def test_planted_numeric_claim_before_a_section_needs_a_pointer(repo):
    text = NEW_RECORD.replace("## Context", "The check retries 42 times.\n\n## Context")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert any(NO_POINTER in line for line in failures(done))


def test_planted_record_beyond_the_line_limit_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(records, "MAX_LINES", 5)
    with pytest.raises(records.ciwf.ReadError, match="line limit"):
        records.blocks(NEW_RECORD)


def test_planted_diff_beyond_the_line_limit_fails_closed(monkeypatch):
    monkeypatch.setattr(records, "MAX_LINES", 1)
    diff = "\n" * 11 + f"+++ b/{NEW}\n@@ -0,0 +1 @@\n"
    with pytest.raises(records.ciwf.ReadError, match="diff line limit"):
        records.added_lines(diff)


def test_planted_changed_names_beyond_the_file_limit_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(records, "MAX_FILES", 1)
    monkeypatch.setattr(records, "git", lambda *args: f"{OLD}\0{NEW}\0")
    with pytest.raises(records.ciwf.ReadError, match="changed file limit"):
        records.changed_records(repo.root, "main")


def test_planted_combined_diffs_beyond_the_line_limit_fails_closed(repo, monkeypatch):
    def diff_output(root, *args):
        if "--name-only" in args:
            return f"{OLD}\0{NEW}\0"
        return "@@ -0,0 +1 @@\n" + "\n" * 5

    monkeypatch.setattr(records, "MAX_LINES", 1)
    monkeypatch.setattr(records, "git", diff_output)
    with pytest.raises(records.ciwf.ReadError, match="diff line limit"):
        records.changed_records(repo.root, "main")


def test_planted_tracked_tree_beyond_the_file_limit_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(records, "MAX_FILES", 1)
    with pytest.raises(records.ciwf.ReadError, match="file limit"):
        records.read_tree(repo.root)


def test_untracked_record_with_invalid_utf8_fails_closed(repo):
    (repo.root / NEW).write_bytes(b"\xff")
    done = repo.tool("records.py", "check", "--base-ref", "main")
    assert done.returncode == 3, done.stderr
    assert "ERROR:" in done.stderr
    assert "Traceback" not in done.stderr


def test_planted_oversized_source_fails_closed(repo, monkeypatch):
    monkeypatch.setattr(records, "MAX_SOURCE_BYTES", 5)
    with pytest.raises(records.ciwf.ReadError, match="source byte limit"):
        records._source_text(repo.root / "tools/app.py")


def test_planted_link_destination_without_a_label_is_no_pointer(repo):
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " ](https://example.invalid/reads)")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_a_defined_test_function_call_is_a_pointer(repo):
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " See `test_app_retries()`.")
    done = change(repo, {NEW: text})
    assert done.returncode == 0, done.stderr


def test_existing_untracked_file_is_no_pointer(repo):
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " See `tools/local.py`.")
    repo.write({NEW: text, "tools/local.py": "# Local draft, not tracked.\n"})
    done = repo.tool("records.py", "check", "--base-ref", "main")
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_a_diverged_base_is_compared_through_its_merge_base(repo):
    repo.write({OLD: OLD_RECORD.replace("three times", "five times")})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "base moves")
    repo.git("checkout", "-q", "-b", "topic", "HEAD~1")
    done = change(repo, {NEW: NEW_RECORD})
    assert done.returncode == 0, done.stderr
    assert "1 decision record(s) changed against main, 5 changed block(s) judged" in done.stdout


def test_planted_textconv_cannot_shift_record_lines(repo):
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
def test_planted_interhunk_context_judges_only_edits(repo, edit_claim):
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
    assert records.added_lines(diff) == {10, 12, 14}


def limited_record_check(repo):
    """Run potentially endless reads only in a time- and address-space-limited child."""
    if not Path("/dev/zero").exists() or importlib.util.find_spec("resource") is None:
        pytest.skip("requires /dev/zero and the resource module for a bounded child")
    script = (
        "import resource,sys; "
        "resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024)); "
        "sys.path.insert(0, sys.argv[1]); import records; "
        "sys.exit(records.main(['check', '--root', sys.argv[2], '--base-ref', 'main']))"
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT / "tools"), str(repo.root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("kind", ["source", "record"])
def test_planted_device_symlinks_are_not_read(repo, kind):
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
    done = limited_record_check(repo)
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


@pytest.mark.parametrize(
    "nested", ["  See `tools/app.py`.", "  - See `tools/app.py`.", "\tSee `tools/app.py`."]
)
def test_planted_list_item_holds_nested_paragraph(repo, nested):
    text = NEW_RECORD.replace(POINTED, "- The app waits.\n\n" + nested)
    done = change(repo, {NEW: text})
    assert done.returncode == 0, done.stdout + done.stderr
    item = next(block for block in records.blocks(text) if block.start == 12)
    assert item.lines == ["- The app waits.", "", nested]
    assert item.end == 14


def test_list_item_blank_before_unindented_claim_starts_new_block(repo):
    text = NEW_RECORD.replace(POINTED, "- See `tools/app.py`.\n\nThe app waits.")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:14: claim under 'Decision' {NO_POINTER}"]


@pytest.mark.parametrize("next_line", ["plain", "- next", "## Context", "  ## Context", "  ```"])
def test_list_item_does_not_hold_trailing_blanks(next_line):
    found = records.blocks("## Decision\n\n- item\n\n\n" + next_line + "\n")
    assert found[0].lines == ["- item"]
    assert found[0].end == 3
    assert found[1].start == 6 if len(found) > 1 else next_line.lstrip().startswith("##")


def test_paragraph_blank_still_starts_new_block():
    found = records.blocks("## Decision\n\nplain\n\n  nested\n\n")
    assert [(block.start, block.end) for block in found] == [(3, 3), (5, 5)]


@pytest.mark.parametrize("binary_attribute", [False, True], ids=["normal", "minus-diff"])
@pytest.mark.parametrize("invalid", [False, True], ids=["claim", "invalid-utf8"])
def test_planted_binary_attribute_cannot_hide_record(repo, binary_attribute, invalid):
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
def test_planted_binary_summary_fails_closed(repo, monkeypatch, summary):
    def binary_diff(root, *args):
        return f"{NEW}\0" if "--name-only" in args else summary + "\n"

    monkeypatch.setattr(records, "git", binary_diff)
    with pytest.raises(records.ciwf.ReadError, match="binary record diff"):
        records.changed_records(repo.root, "main")
