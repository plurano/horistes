# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""tools/records.py: what a change adds to a decision record restates no code and points somewhere.

Each test commits a base, commits a change on a topic branch the way a pull request
would, and runs the check against the base, as CI does with BASE_REF.
"""

from __future__ import annotations

import subprocess

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
ADR_0066_SOURCE = ("tools/merge_train.py", ".github/workflows/merge-train.yml")


def test_adr_0066_as_landed_replays_red(tree):
    """The record that restated the train's code, added again on top of the source it quoted."""
    tree.write({name: (ROOT / name).read_text(encoding="utf-8") for name in ADR_0066_SOURCE})
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "base")
    done = change(tree, {ADR_0066: (ROOT / ADR_0066).read_text(encoding="utf-8")})
    assert done.returncode == 1
    found = failures(done)
    assert f"FAIL: {ADR_0066}:25: claim under 'Measurement'" in "\n".join(found)
    assert f"FAIL: {ADR_0066}:46: claim under 'Decision'" in "\n".join(found)
    assert f'FAIL: {ADR_0066}:30: {RESTATED} `pull["mergeStateStatus"] == "BEHIND"`' in found


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
    diff = f"+++ b/{OLD}\n@@ -12,2 +12{suffix} @@\n"
    expected = {} if new_count == "0" else {OLD: {12}}
    assert records.added_lines(diff) == expected


def test_boundary_diff_dev_null_clears_the_record():
    diff = f"+++ b/{OLD}\n@@ -1 +1 @@\n+++ /dev/null\n@@ -1,3 +4,2 @@\n"
    assert records.added_lines(diff) == {OLD: {1}}


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
