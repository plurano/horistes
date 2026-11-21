# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Keep workflow run scripts below 4000 conservative fields.

This over-counts the engine rule by keeping comments and joined lines, so a
passing script stays under 4096 with room. The engine's own count remains the
judge (ADR-0053, ADR-0082, ADR-0094).
"""

import ciwf
import pytest
from conftest import ROOT


def run_script_fields(run: str) -> int:
    """Words, newlines, and an extra field for each word ending in a semicolon."""
    words = run.split()
    return len(words) + run.count("\n") + sum(word.endswith(";") for word in words)


def check_run_script(run: str) -> None:
    fields = run_script_fields(run)
    assert fields <= 4000, f"run script has {fields} fields; limit 4000"


def workflow_runs(path):
    """Finite workflow jobs and step lists bound enumeration."""
    jobs = ciwf.load(path).get("jobs", {})
    for job, config in jobs.items():
        steps = config.get("steps", [])
        for index in range(len(steps)):
            if "run" in steps[index]:
                yield f"{path.name}:{job}:step-{index + 1}", steps[index]["run"]


@pytest.mark.parametrize(
    "run",
    [
        pytest.param(run, id=name)
        for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
        for name, run in workflow_runs(path)
    ],
)
def test_workflow_run_script_fields(run):
    check_run_script(run)


@pytest.mark.parametrize(("fields", "accepted"), [(1, True), (4000, True), (4001, False)])
def test_run_script_field_boundary(fields, accepted):
    run = "word " * fields
    assert run_script_fields(run) == fields
    if accepted:
        check_run_script(run)
    else:
        with pytest.raises(AssertionError, match="4001 fields; limit 4000"):
            check_run_script(run)


def test_run_script_field_count_keeps_comments_and_joined_lines():
    assert run_script_fields("echo; \\\nword # comment\n") == 8


def test_padded_train_rules_step_is_refused(tmp_path):
    source = ROOT / ".github/workflows/merge-train.yml"
    path = tmp_path / source.name
    text = source.read_text()
    marker = "cat > \"$RUNNER_TEMP/train_rules.py\" <<'PY'"
    assert text.count(marker) == 1
    original = dict(workflow_runs(source))["merge-train.yml:train:step-1"]
    padding = "padding " * (4001 - run_script_fields(original))
    path.write_text(text.replace(marker, "# " + padding + "\n          " + marker))
    runs = dict(workflow_runs(path))
    with pytest.raises(AssertionError, match=r"run script has \d+ fields; limit 4000"):
        check_run_script(runs["merge-train.yml:train:step-1"])
