# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Decision-record claims behavior and planted regressions."""

from __future__ import annotations

import pytest
import records
from conftest import Tree
from records_cases import (
    ADR_0066,
    NEW,
    NEW_RECORD,
    NO_POINTER,
    OLD,
    OLD_RECORD,
    POINTED,
    RECORD_FIXTURES,
    RESTATED,
    UNPOINTED,
    base_repo,
    change,
    failures,
)


def test_planted_added_record_without_a_pointer_fails(tree):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, UNPOINTED)})
    assert done.returncode == 1, done.stdout + done.stderr
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_fixed_added_record_passes(tree):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD})
    assert done.returncode == 0, done.stderr
    assert done.stdout == (
        "PASS: 1 decision record(s) changed against main, 5 changed block(s) judged\n"
    )


def test_planted_restated_span_fails_and_a_named_symbol_passes(tree):
    repo = base_repo(tree)
    restated = POINTED.replace("`read_with_retries()`", '`DEADLINE_SECONDS = timeout_for("forge")`')
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, restated)})
    assert done.returncode == 1
    assert failures(done) == [
        f'FAIL: {NEW}:12: {RESTATED} `DEADLINE_SECONDS = timeout_for("forge")`'
    ]
    named = POINTED.replace("`read_with_retries()`", "`DEADLINE_SECONDS`")
    assert change(repo, {NEW: NEW_RECORD.replace(POINTED, named)}).returncode == 0


def test_planted_restated_fenced_block_fails(tree):
    repo = base_repo(tree)
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
def test_boundary_spans_that_restate_nothing_pass(tree, span):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, f"{POINTED} {span}")})
    assert done.returncode == 0, done.stderr


def test_source_held_only_in_markdown_is_not_restated(tree):
    repo = base_repo(tree)
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
def test_pointer_kinds_pass(tree, pointer):
    repo = base_repo(tree)
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
def test_pointers_to_nothing_fail(tree, pointer):
    repo = base_repo(tree)
    done = change(repo, {NEW: NEW_RECORD.replace(POINTED, f"{UNPOINTED} See {pointer}.")})
    assert done.returncode == 1
    assert len(failures(done)) == 1


def test_planted_number_outside_claim_sections_needs_a_pointer(tree):
    repo = base_repo(tree)
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
def test_uncounted_numbers_are_no_claim(tree, text):
    repo = base_repo(tree)
    context = "The forge answers slowly while it recalculates."
    assert change(repo, {NEW: NEW_RECORD.replace(context, text)}).returncode == 0


def test_numbers_under_references_are_no_claim(tree):
    repo = base_repo(tree)
    references = f"{NEW_RECORD}\n## References\n\n- Forge outage of 2026-10-30, 41 reads lost.\n"
    assert change(repo, {NEW: references}).returncode == 0


def test_untouched_blocks_of_an_accepted_record_stay_unjudged(tree):
    repo = base_repo(tree)
    # The separator predates the edit; an added blank now binds its neighbours.
    repo.write({OLD: OLD_RECORD + "\n"})
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "accepted separator")
    added = f"{OLD_RECORD}\nThe retries are counted in `tools/app.py` `RETRY_LIMIT`.\n"
    done = change(repo, {OLD: added})
    assert done.returncode == 0, done.stderr
    assert "1 changed block(s) judged" in done.stdout


def test_a_status_change_of_an_accepted_record_passes(tree):
    repo = base_repo(tree)
    superseded = OLD_RECORD.replace("Accepted", "Superseded by [ADR-0002](0002-x.md)")
    assert change(repo, {OLD: superseded}).returncode == 0


def test_a_changed_line_binds_the_whole_old_block(tree):
    repo = base_repo(tree)
    edited = OLD_RECORD.replace("three times", "four times")
    done = change(repo, {OLD: edited})
    assert done.returncode == 1
    assert failures(done) == [f"FAIL: {OLD}:12: claim under 'Decision' {NO_POINTER}"]


def test_files_outside_the_record_pattern_are_not_judged(tree):
    repo = base_repo(tree)
    done = change(repo, {"docs/adr/README.md": "# Decision records\n\nIt lists 88 records.\n"})
    assert done.returncode == 0
    assert done.stdout.startswith("PASS: 0 decision record(s) changed")


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


def test_planted_hidden_pointer_does_not_support_visible_claim(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " <!-- `tools/app.py` -->")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_crlf_record_with_a_fence_info_string_is_judged(tree):
    repo = base_repo(tree)
    text = NEW_RECORD + "\n```python title=retry\nfor attempt in range(RETRY_LIMIT):\n```\n"
    repo.write({NEW: text.replace("\n", "\r\n")})
    done = change(repo, {})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:18: {RESTATED} `for attempt in range(RETRY_LIMIT):`"]


def test_doubled_backtick_span_restating_source_fails(tree):
    repo = base_repo(tree)
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


def test_changed_line_binds_restated_span_on_unchanged_line(tree):
    repo = base_repo(tree)
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


def test_planted_numeric_claim_before_a_section_needs_a_pointer(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace("## Context", "The check retries 42 times.\n\n## Context")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert any(NO_POINTER in line for line in failures(done))


def test_planted_link_destination_without_a_label_is_no_pointer(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " ](https://example.invalid/reads)")
    done = change(repo, {NEW: text})
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]


def test_a_defined_test_function_call_is_a_pointer(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " See `test_app_retries()`.")
    done = change(repo, {NEW: text})
    assert done.returncode == 0, done.stderr


def test_existing_untracked_file_is_no_pointer(tree):
    repo = base_repo(tree)
    text = NEW_RECORD.replace(POINTED, UNPOINTED + " See `tools/local.py`.")
    repo.write({NEW: text, "tools/local.py": "# Local draft, not tracked.\n"})
    done = repo.tool("records.py", "check", "--base-ref", "main")
    assert done.returncode == 1, done.stdout
    assert failures(done) == [f"FAIL: {NEW}:12: claim under 'Decision' {NO_POINTER}"]
