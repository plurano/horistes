# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Replay the stall and repository concurrency against rendered workflow topology."""

from __future__ import annotations

import ciwf
import pytest
from conftest import ROOT
from expressions import evaluate
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
    for run in trains[1:]:
        train = next(job for job in run.jobs if job.name == "train")
        assert train.start is not None and train.start <= run.scheduled + cycle, "train stalled"
    release_run = next(run for run in timeline.runs if run.kind == RELEASE)
    assert next(job for job in release_run.jobs if job.name == "plan").state == "done"
    first_legs = [job for job in release_run.jobs if job.repository]
    assert any(job.state == "queued" for job in first_legs)
    assert all(job.start is None for job in first_legs)
    return timeline


def test_queued_release_legs_do_not_hold_the_train():
    check_train_progress(layout(ROOT))


def old_layout():
    specs = layout(ROOT)
    specs[TRAIN]["workflow"]["jobs"]["release"] = {
        "needs": "train",
        "if": "${{ !cancelled() && inputs.switch == 'on' && "
        "needs.train.outputs.releases != '' && needs.train.outputs.releases != '[]' }}",
        "runs-on": "${{ inputs.release_runner || inputs.runner }}",
        "environment": "merge-train",
        "timeout-minutes": 15,
        "permissions": {},
        "strategy": {
            "fail-fast": False,
            "max-parallel": "${{ inputs.release_max_parallel }}",
            "matrix": {"repository": "${{ fromJSON(needs.train.outputs.releases) }}"},
        },
        "steps": specs[RELEASE]["workflow"]["jobs"]["release"]["steps"],
    }
    definitions = specs[TRAIN]["workflow"]["on"]["workflow_call"]["inputs"]
    definitions.update(release_runner={"default": "arc-cauda"}, release_max_parallel={"default": 3})
    specs.pop(RELEASE)
    return specs


def test_planted_old_layout_stalls_the_next_train():
    specs = old_layout()
    first, cycle = cadence(specs[TRAIN])
    timeline = Timeline(specs, release_free=False).play(
        [(first, TRAIN, [A]), (first + cycle, TRAIN, [A])]
    )
    assert timeline.legs()[0].state == "queued"
    assert timeline.runs[1].jobs[0].start is None, "old layout did not stall"


def test_planted_false_old_guard_does_not_stall():
    specs = old_layout()
    specs[TRAIN]["workflow"]["jobs"]["release"]["if"] = "${{ false }}"
    timeline = Timeline(specs, release_free=False).play([(0, TRAIN, [A]), (20, TRAIN, [A])])
    assert timeline.runs[1].jobs[0].start == 20
    assert all(job.state == "skipped" and job.start is None for job in timeline.legs())
    assert all(job.end is not None for job in timeline.legs())
    assert ("skipped", 1, "release", 8) in timeline.events
    assert all(run.state == "done" for run in timeline.runs)


def test_unreadable_job_guard_raises():
    specs = old_layout()
    specs[TRAIN]["workflow"]["jobs"]["release"]["if"] = "${{ unsupported() }}"
    with pytest.raises(ValueError, match="not read here"):
        Timeline(specs).play([(0, TRAIN, [A])])


def test_repository_group_separators_do_not_collide():
    repositories = [{"owner": "fleet", "name": "a-b"}, {"owner": "fleet-a", "name": "b"}]
    legs = Timeline(layout(ROOT)).play([(0, RELEASE, repositories)]).legs()
    assert len({job.group for job in legs}) == 2
    assert max(job.start for job in legs) < min(job.end for job in legs)


@pytest.mark.parametrize("count", [256, 257])
def test_matrix_job_bound(count):
    repositories = [{"owner": "fleet", "name": f"r{i}"} for i in range(count)]
    if count == 257:
        with pytest.raises(AssertionError):
            Timeline(layout(ROOT)).play([(0, RELEASE, repositories)])
        return
    timeline = Timeline(layout(ROOT), leg_minutes=1).play([(0, RELEASE, repositories)])
    assert len(timeline.legs()) == 256
    assert all(job.state == "done" and job.end is not None for job in timeline.legs())
    assert timeline.runs[0].state == "done"


def test_yaml_text_reader_has_one_safe_load_site():
    needle = "yaml" + ".load("
    assert needle not in (ROOT / "tests/train_timeline.py").read_text()
    assert (ROOT / "tools/ciwf.py").read_text().count(needle) == 1
    assert ciwf.loads("on: true", "fixture") == {"on": True}
    for text in ("[]", "a: 1\na: 2", "!!python/object:bad {}"):
        with pytest.raises(ciwf.ReadError, match="fixture"):
            ciwf.loads(text, "fixture")


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
        == "${{ !cancelled() && inputs.switch == 'on' && steps.sweep.outputs.releases != '' "
        "&& steps.sweep.outputs.releases != '[]' }}"
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
    downloads = [
        s for s in plan["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact@")
    ]
    assert len(downloads) == 2
    identified, download = downloads
    assert identified["if"] == "${{ inputs.artifact_id != '' }}"
    assert download["if"] == "${{ inputs.artifact_id == '' }}"
    assert identified["with"] == {
        **{key: value for key, value in download["with"].items() if key != "pattern"},
        "artifact-ids": "${{ inputs.artifact_id }}",
    }
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
        "&& steps.sweep.outputs.releases != '[]' && steps.upload.outcome == 'success' }}"
    )
    references = (
        ("GITHUB_TOKEN", "github.token"),
        ("REPOSITORY", "github.repository"),
        ("TRAIN_RUN_ID", "github.run_id"),
        ("RELEASE_WORKFLOW", "inputs.release_workflow"),
        ("ARTIFACT_ID", "steps.upload.outputs.artifact-id"),
    )
    assert dispatch["env"] == {name: "${{ " + ref + " }}" for name, ref in references}
    assert dispatch["run"] == (
        'python3 "$GITHUB_WORKSPACE/merge-train-source/tools/release_plan.py" dispatch'
    )
    checkout = next(s for s in job["steps"] if "sparse-checkout" in s.get("with", {}))
    assert {
        "tools/release_labels.py",
        "tools/release_workflows.py",
        "tools/release_plan.py",
    } <= set(checkout["with"]["sparse-checkout"].splitlines())


@pytest.mark.parametrize("step", ["upload", "dispatch"])
@pytest.mark.parametrize(
    "releases,expected",
    [
        pytest.param('[{"owner":"fleet","name":"A"}]', True, id="nonempty"),
        pytest.param("[]", False, id="empty-list"),
        pytest.param("", False, id="empty-string"),
    ],
)
def test_handoff_guards_require_releases(step, releases, expected):
    train = ciwf.load(ROOT / ".github/workflows/merge-train.yml")
    upload, dispatch = train["jobs"]["train"]["steps"][-2:]
    guard = upload["if"] if step == "upload" else dispatch["if"]
    steps = {"sweep": {"outputs": {"releases": releases}}, "upload": {"outcome": "success"}}
    assert evaluate(guard, {"switch": "on"}, steps=steps) is expected


def check_plan_wiring(plan, leg):
    step = next(s for s in plan["steps"] if s.get("id") == "plan")
    assert step["run"].endswith('release_plan.py" plan')
    references = (
        ("GITHUB_TOKEN", "github.token"),
        ("TRAIN_RUN_ID", "inputs.train_run_id"),
        ("ARTIFACT_ID", "inputs.artifact_id"),
        ("TRAIN_WORKFLOW", "inputs.train_workflow"),
        ("REPOSITORY", "github.repository"),
        ("MAX_PARALLEL", "inputs.release_max_parallel"),
    )
    expected = {name: "${{ " + ref + " }}" for name, ref in references}
    expected["FILE"] = "${{ runner.temp }}/merge-train-plan/merge-train-releases.json"
    assert step["env"] == expected
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
        plan["steps"][2]["with"]["run-id"] = "${{ github.run_id }}"
    elif where == "plan-run":
        plan["steps"][3]["env"]["TRAIN_RUN_ID"] = "${{ github.run_id }}"
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
                },
                "artifact_id": {
                    "description": "Set by the train; empty for a request by hand.",
                    "type": "string",
                    "required": False,
                    "default": "",
                },
            }
        }
    }
    assert caller["jobs"]["release"]["with"].get("train_run_id") == "${{ inputs.train_run_id }}"
    assert caller["jobs"]["release"]["with"]["artifact_id"] == "${{ inputs.artifact_id }}"
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
        "artifact_id",
    }
    assert inputs["train_run_id"]["type"] == "string" and inputs["train_run_id"]["required"]
    assert inputs["train_workflow"]["default"] == "merge-train.yml"
    artifact = inputs["artifact_id"]
    assert (artifact["type"], artifact["required"], artifact["default"]) == ("string", False, "")


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


def test_handoff_module_keeps_label_module_bounded():
    labels = (ROOT / "tools/release_labels.py").read_text()
    assert sum(bool(line.strip()) for line in labels.splitlines()) <= 450
    plan = (ROOT / "tools/release_plan.py").read_text()
    assert "ADR-0098" in plan
    for command in ("plan", "dispatch"):
        assert f"def cmd_{command}(" in plan
        assert f"def cmd_{command}(" not in labels


@pytest.mark.parametrize("nonmatrix", [8, 9])
def test_total_job_bound_includes_nonmatrix_jobs(nonmatrix):
    specs = layout(ROOT)
    for index in range(nonmatrix - 1):
        specs[RELEASE]["workflow"]["jobs"][f"extra{index}"] = {"runs-on": "${{ inputs.runner }}"}
    repositories = [{"owner": "fleet", "name": f"r{i}"} for i in range(256)]
    if nonmatrix == 9:
        with pytest.raises(AssertionError):
            Timeline(specs).play([(0, RELEASE, repositories)])
        return
    timeline = Timeline(specs, leg_minutes=1).play([(0, RELEASE, repositories)])
    assert len(timeline.runs[0].jobs) == 264
    assert timeline.runs[0].state == "done"
