# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Replay the stall and repository concurrency against rendered workflow topology."""

from __future__ import annotations

import copy

import ciwf
import pytest
from conftest import ROOT
from train_timeline import Timeline, cadence, layout

A = {"owner": "fleet", "name": "A"}
B = {"owner": "fleet", "name": "B"}
C = {"owner": "fleet", "name": "C"}
TRAIN = "merge-train"
RELEASE = "merge-train-release"


def check_train_progress(specs):
    first, cycle = cadence(specs[TRAIN])
    arrivals = [(first + cycle * i, TRAIN, [A, B, C]) for i in range(6)]
    timeline = Timeline(specs, release_free=False).play(arrivals)
    trains = [run for run in timeline.runs if run.kind == TRAIN]
    for run in trains[1:3]:
        train = next(job for job in run.jobs if job.name == "train")
        assert train.start is not None and train.start <= run.scheduled + cycle, "train stalled"
    first_legs = [job for job in timeline.legs() if job.run.repositories == [A, B, C]]
    assert first_legs and all(job.start is None for job in first_legs)
    return timeline


def test_queued_release_legs_do_not_hold_the_train():
    check_train_progress(layout(ROOT))


def test_planted_old_layout_stalls_the_next_train():
    specs = layout(ROOT)
    old_release = copy.deepcopy(specs[RELEASE]["workflow"]["jobs"]["release"])
    old_release.update(needs="train", **{"runs-on": "${{ inputs.release_runner }}"})
    old_release.pop("concurrency")
    old_release["strategy"]["matrix"]["repository"] = (
        "${{ fromJSON(needs.train.outputs.releases) }}"
    )
    specs[TRAIN]["workflow"]["jobs"]["release"] = old_release
    definitions = specs[TRAIN]["workflow"]["on"]["workflow_call"]["inputs"]
    definitions.update(release_runner={"default": "arc-cauda"}, release_max_parallel={"default": 3})
    specs.pop(RELEASE)
    with pytest.raises(AssertionError, match="train stalled"):
        check_train_progress(specs)


def race(specs, second):
    timeline = Timeline(specs).play([(0, RELEASE, [A]), (10, RELEASE, [second])])
    legs = timeline.legs()
    assert len(legs) == 2
    assert all(job.state == "done" for job in legs), "release leg cancelled"
    assert all(job.start is not None and job.end is not None for job in legs)
    overlap = max(job.start for job in legs) < min(job.end for job in legs)
    return overlap


def check_repository_concurrency(specs):
    assert not race(specs, A), "same repository overlaps"
    assert race(specs, B), "different repositories serialized"


def test_release_repository_groups_serialize_only_the_same_repository():
    check_repository_concurrency(layout(ROOT))


def test_group_names_are_case_insensitive():
    assert not race(layout(ROOT), {"owner": "FLEET", "name": "a"})


def test_newer_pending_leg_replaces_older_pending_leg():
    timeline = Timeline(layout(ROOT), leg_minutes=30).play(
        [(0, RELEASE, [A]), (10, RELEASE, [A]), (20, RELEASE, [A])]
    )
    first, older, newer = timeline.legs()
    assert [first.state, older.state, newer.state] == ["done", "cancelled", "done"]
    assert older.start is None
    assert newer.start >= first.end


def mutate(specs, name):
    job = specs[RELEASE]["workflow"]["jobs"]["release"]
    if name == "group-without-matrix":
        job["concurrency"]["group"] = "release-${{ inputs.app_client_id }}"
    elif name == "group-with-run-id":
        job["concurrency"]["group"] += "-${{ github.run_id }}"
    elif name == "cancel-running":
        job["concurrency"]["cancel-in-progress"] = True
    elif name == "no-job-concurrency":
        job.pop("concurrency")
    elif name == "workflow-concurrency":
        specs[RELEASE]["caller"]["concurrency"] = {
            "group": "merge-train",
            "cancel-in-progress": False,
        }
    else:
        raise ValueError(name)


@pytest.mark.parametrize(
    "name",
    [
        "group-without-matrix",
        "group-with-run-id",
        "cancel-running",
        "no-job-concurrency",
        "workflow-concurrency",
    ],
)
def test_planted_concurrency_mutants_are_refused(name):
    specs = layout(ROOT)
    mutate(specs, name)
    with pytest.raises(AssertionError):
        check_repository_concurrency(specs)


def check_handoff(train, release):
    assert "release" not in train["jobs"]
    job = train["jobs"]["train"]
    assert job["permissions"] == {"actions": "write"} and "outputs" not in job
    upload = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert (
        upload["if"]
        == "${{ !cancelled() && inputs.switch == 'on' && steps.sweep.outputs.releases != '' }}"
    )
    assert upload["with"] == {
        "name": "merge-train-releases",
        "path": "${{ runner.temp }}/merge-train-releases.json",
        "retention-days": 1,
        "if-no-files-found": "error",
    }
    check_dispatch_wiring(job, upload)
    sweep = next(s for s in job["steps"] if s.get("id") == "sweep")
    assert sweep["env"]["TRAIN_RELEASE_FILE"] == upload["with"]["path"]
    plan = release["jobs"]["plan"]
    assert plan["permissions"] == {"actions": "read"}
    assert plan["timeout-minutes"] == 5
    download = next(
        s for s in plan["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")
    )
    assert download["with"] == {
        "pattern": "merge-train-releases",
        "merge-multiple": True,
        "path": "${{ runner.temp }}/merge-train-plan",
        "run-id": "${{ inputs.train_run_id }}",
        "repository": "${{ github.repository }}",
        "github-token": "${{ github.token }}",
    }
    check_plan_wiring(plan, release["jobs"]["release"])


def check_dispatch_wiring(job, upload):
    dispatch = next(s for s in job["steps"] if s.get("name") == "Start the release run")
    assert upload["id"] == "upload"
    assert job["steps"].index(dispatch) == job["steps"].index(upload) + 1
    assert dispatch["if"] == (
        "${{ !cancelled() && inputs.switch == 'on' && steps.sweep.outputs.releases != '' "
        "&& steps.upload.outcome == 'success' }}"
    )
    assert dispatch["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "REPOSITORY": "${{ github.repository }}",
        "TRAIN_RUN_ID": "${{ github.run_id }}",
        "RELEASE_WORKFLOW": "${{ inputs.release_workflow }}",
    }
    assert dispatch["run"] == (
        'python3 "$GITHUB_WORKSPACE/merge-train-source/tools/release_labels.py" dispatch'
    )
    checkout = next(s for s in job["steps"] if "sparse-checkout" in s.get("with", {}))
    assert {"tools/release_labels.py", "tools/release_workflows.py"} <= set(
        checkout["with"]["sparse-checkout"].splitlines()
    )


def check_plan_wiring(plan, leg):
    step = next(s for s in plan["steps"] if s.get("id") == "plan")
    assert step["run"].endswith('release_labels.py" plan')
    assert step["env"] == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "TRAIN_RUN_ID": "${{ inputs.train_run_id }}",
        "TRAIN_WORKFLOW": "${{ inputs.train_workflow }}",
        "REPOSITORY": "${{ github.repository }}",
        "MAX_PARALLEL": "${{ inputs.release_max_parallel }}",
        "FILE": "${{ runner.temp }}/merge-train-plan/merge-train-releases.json",
    }
    assert plan["outputs"] == {
        "releases": "${{ steps.plan.outputs.releases }}",
        "hold_labels": "${{ steps.plan.outputs.hold_labels }}",
    }
    assert leg["needs"] == "plan"
    assert leg["if"] == (
        "${{ !cancelled() && needs.plan.outputs.releases != '' "
        "&& needs.plan.outputs.releases != '[]' }}"
    )


def test_artifact_handoff_contract():
    check_handoff(
        ciwf.load(ROOT / ".github/workflows/merge-train.yml"),
        ciwf.load(ROOT / ".github/workflows/merge-train-release.yml"),
    )


@pytest.mark.parametrize(
    "where",
    [
        "upload-switch",
        "upload-missing",
        "download-run",
        "plan-run",
        "plan-output",
        "leg-list",
        "dispatch-guard",
        "train-permission",
    ],
)
def test_planted_handoff_mutants_are_refused(where):
    train = ciwf.load(ROOT / ".github/workflows/merge-train.yml")
    release = ciwf.load(ROOT / ".github/workflows/merge-train-release.yml")
    upload = train["jobs"]["train"]["steps"][-2]
    plan = release["jobs"]["plan"]
    if where == "upload-switch":
        upload["if"] = "${{ !cancelled() }}"
    elif where == "upload-missing":
        upload["with"]["if-no-files-found"] = "ignore"
    elif where == "download-run":
        plan["steps"][1]["with"]["run-id"] = "${{ github.run_id }}"
    elif where == "plan-run":
        plan["steps"][2]["env"]["TRAIN_RUN_ID"] = "${{ github.run_id }}"
    elif where == "dispatch-guard":
        train["jobs"]["train"]["steps"][-1]["if"] = upload["if"]
    elif where == "train-permission":
        train["jobs"]["train"]["permissions"] = {}
    elif where == "plan-output":
        plan["outputs"].pop("hold_labels")
    else:
        release["jobs"]["release"]["if"] = "${{ !cancelled() }}"
    with pytest.raises(AssertionError):
        check_handoff(train, release)


def check_release_caller(spec):
    caller = spec["caller"]
    assert caller["name"] == "Merge train releases"
    assert caller["on"] == {
        "workflow_dispatch": {
            "inputs": {
                "train_run_id": {
                    "description": "Train run whose release list is read.",
                    "type": "string",
                    "required": True,
                }
            }
        }
    }
    assert caller["jobs"]["release"]["with"].get("train_run_id") == "${{ inputs.train_run_id }}"
    assert caller["permissions"] == {} and "concurrency" not in caller
    assert caller["jobs"]["release"]["permissions"] == {"actions": "read"}
    assert "concurrency" not in spec["workflow"]
    inputs = spec["workflow"]["on"]["workflow_call"]["inputs"]
    assert set(inputs) == {
        "runner",
        "app_client_id",
        "release_max_parallel",
        "train_run_id",
        "train_workflow",
    }
    assert inputs["train_run_id"]["type"] == "string" and inputs["train_run_id"]["required"]
    assert inputs["train_workflow"]["default"] == "merge-train.yml"


def test_release_caller_has_no_workflow_group():
    check_release_caller(layout(ROOT)[RELEASE])


@pytest.mark.parametrize("key", ["concurrency", "permissions", "on", "train-run-id"])
def test_planted_release_caller_mutants_are_refused(key):
    spec = layout(ROOT)[RELEASE]
    if key == "train-run-id":
        spec["caller"]["jobs"]["release"]["with"].pop("train_run_id")
    else:
        spec["caller"][key] = {}
    if key == "on":
        spec["caller"][key] = {
            "workflow_run": {"workflows": ["Merge train"], "types": ["completed"]}
        }
    if key == "permissions":
        spec["caller"][key] = {"contents": "write"}
    with pytest.raises(AssertionError):
        check_release_caller(spec)
