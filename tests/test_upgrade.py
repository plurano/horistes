# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Upgrade tests: consumer callers and interface check names across versions.

A caller written for an older interface version is refused when required check
names change and accepted once updated. An appended check name on the same major
version is accepted as a compatible interface addition.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import interface
import render
import verdict


def test_current_rendered_caller_is_accepted_against_declared_interface(tmp_path: Path) -> None:
    """The current rendered caller passes caller validation and matches the declared check name."""
    source = (ROOT / "templates/callers/licence.yml.tmpl").read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, "templates/callers/licence.yml.tmpl")
    assert render.caller_problems(rendered, ROOT) == []

    lock = json.loads((ROOT / "interface.lock.json").read_text(encoding="utf-8"))
    assert ".github/workflows/licence.yml" in lock["workflows"]

    caller = tmp_path / "caller.yml"
    caller.write_text(rendered, encoding="utf-8")
    old = tmp_path / "old.yml"
    old.write_text(
        "---\nname: REUSE\non:\n  pull_request:\npermissions: {}\njobs:\n"
        "  reuse:\n    name: REUSE lint\n    runs-on: ubuntu-24.04\n"
        "    steps:\n      - run: exit 0\n",
        encoding="utf-8",
    )
    pair = verdict.Pair(
        family="licence",
        old="REUSE lint",
        new="Licence / REUSE lint",
        old_workflow=str(old.name),
        new_workflow=str(caller.name),
        called_workflow=".github/workflows/licence.yml",
        draft_stop="",
        runner="hosted",
    )
    problems, told = verdict.side_conditions(pair, tmp_path, ROOT)
    assert problems == []
    assert any(
        "required check moves from 'REUSE lint' to 'Licence / REUSE lint'" in t for t in told
    )

    without_result = rendered.split("  result:\n")[0]
    planted = render.caller_problems(without_result, ROOT)
    assert any(
        "the caller runs on pull requests and no job `result` reports `Licence`" in p
        for p in planted
    )


def test_caller_carrying_an_older_check_name_is_refused_until_updated(tmp_path: Path) -> None:
    """A caller carrying an older check name is refused with its message until updated."""
    source = (ROOT / "templates/callers/licence.yml.tmpl").read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, "templates/callers/licence.yml.tmpl")

    outdated_caller = rendered.replace(
        "  result:\n    name: Licence\n", "  result:\n    name: Licence check\n"
    )
    problems = render.caller_problems(outdated_caller, ROOT)
    assert "job `result` has `name: Licence check`, not `Licence`" in problems
    assert render.caller_problems(rendered, ROOT) == []

    cur_doc = ciwf.load(ROOT / ".github/workflows/scorecard.yml")
    cur_jobs = {k: str(v.get("name", k)) for k, v in cur_doc["jobs"].items()}
    empty_spec = {"inputs": {}, "secrets": {}, "outputs": [], "write_scopes": []}
    old_lock = {
        "interface_major": 0,
        "workflows": {
            ".github/workflows/scorecard.yml": {
                "jobs": {"analysis": "Analysis", "visibility": "Public repository"},
                **empty_spec,
            }
        },
    }
    cur_lock = {
        "interface_major": 0,
        "workflows": {".github/workflows/scorecard.yml": {"jobs": cur_jobs, **empty_spec}},
    }
    breaks = interface.breaks(old_lock, cur_lock)
    assert any("check name of job analysis changed" in b for b in breaks)
    assert not interface.declared(old_lock, cur_lock)

    matching_breaks = interface.breaks(cur_lock, cur_lock)
    assert matching_breaks == []


def test_appended_check_name_on_the_same_major_is_accepted_as_compatible() -> None:
    """A check name appended on the same interface major is no break."""
    empty_spec = {"inputs": {}, "secrets": {}, "outputs": [], "write_scopes": []}
    base_lock = {
        "interface_major": 0,
        "interface_minor": 2,
        "workflows": {
            ".github/workflows/merge-train.yml": {"jobs": {"train": "Merge train"}, **empty_spec}
        },
    }
    appended_lock = {
        "interface_major": 0,
        "interface_minor": 2,
        "workflows": {
            ".github/workflows/merge-train.yml": {
                "jobs": {"train": "Merge train", "release": "Release pull request and release"},
                **empty_spec,
            }
        },
    }
    assert interface.breaks(base_lock, appended_lock) == []

    doc = ciwf.load(ROOT / ".github/workflows/merge-train.yml")
    assert verdict._job(doc, "Merge train", "merge-train.yml")["name"] == "Merge train"
    assert (
        verdict._job(doc, "Release pull request and release", "merge-train.yml")["name"]
        == "Release pull request and release"
    )

    mutated = {
        "interface_major": 0,
        "interface_minor": 2,
        "workflows": {
            ".github/workflows/merge-train.yml": {
                "jobs": {"train": "Train", "release": "Release pull request and release"},
                **empty_spec,
            }
        },
    }
    breaks = interface.breaks(base_lock, mutated)
    assert any("check name of job train changed 'Merge train' -> 'Train'" in b for b in breaks)
    assert not interface.declared(base_lock, mutated)
