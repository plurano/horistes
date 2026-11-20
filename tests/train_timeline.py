# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Bounded minute-clock model of the real callers and reusable workflow jobs.

Groups are case-insensitive, with one owner and one replaceable pending entrant.
A workflow owns its group through queued jobs; a job owns its group while waiting
for its runner and executing. Completion triggers workflow_run callers. This models
scheduling, not forge execution or release-please itself (ADR-0098).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import ciwf
import render
import yaml
from expressions import evaluate, interpolate

HORIZON = 160
MAX_RUNS = 32
MAX_JOBS = 256


def layout(root: Path) -> dict:
    """Render both callers and load both reusables from disk, without a forge."""
    result = {}
    for name in ("merge-train", "merge-train-release"):
        template = root / f"templates/callers/{name}.yml.tmpl"
        caller = yaml.load(
            render.render(template.read_text(), render.SAMPLE, name), ciwf.StrictLoader
        )
        result[name] = {
            "caller": caller,
            "workflow": ciwf.load(root / f".github/workflows/{name}.yml"),
        }
    return result


def call_inputs(spec: dict, runner: str) -> dict:
    """Defaults plus the actual caller's with mapping, with fixture repository variables."""
    caller_jobs = spec["caller"]["jobs"]
    assert len(caller_jobs) == 1
    call = next(iter(caller_jobs.values()))
    definitions = spec["workflow"]["on"]["workflow_call"]["inputs"]
    values = {key: value.get("default") for key, value in definitions.items()}
    variables = {"MERGE_TRAIN_APP_CLIENT_ID": "fixture-app", "MERGE_TRAIN": "on"}
    for key, value in call["with"].items():
        values[key] = evaluate(value, {}, vars=variables) if "${{" in str(value) else value
    values["runner"] = runner
    return values


def cadence(spec: dict) -> tuple[int, int]:
    """The caller's literal hourly minute list determines the schedule and cycle."""
    cron = spec["caller"]["on"]["schedule"][0]["cron"]
    minutes, *fields = cron.split()
    assert fields == ["*", "*", "*", "*"]
    values = [int(value) for value in minutes.split(",")]
    assert len(values) >= 2
    intervals = [b - a for a, b in zip(values, values[1:], strict=False)]
    assert len({*intervals, 60 + values[0] - values[-1]}) == 1
    return values[0], intervals[0]


@dataclass
class Job:
    """An expanded YAML job, including a single matrix repository."""

    run: Run
    name: str
    config: dict
    repository: dict = field(default_factory=dict)
    state: str = "new"
    start: int | None = None
    end: int | None = None
    group: str = ""
    runner: str = ""


@dataclass
class Run:
    """One workflow run; its group outlives every queued or running job."""

    number: int
    kind: str
    scheduled: int
    spec: dict
    inputs: dict
    repositories: list[dict]
    jobs: list[Job] = field(default_factory=list)
    state: str = "new"
    group: str = ""


def group_name(config: dict, run: Run, repository: dict) -> str:
    """Evaluate supported expression operands, then apply case-insensitive group semantics."""
    group = config.get("concurrency", {}).get("group", "")
    return interpolate(
        group, run.inputs, matrix={"repository": repository}, github={"run_id": run.number}
    ).casefold()


def expand_jobs(run: Run) -> list[Job]:
    """Jobs, needs, runner labels and matrix shape come from the reusable YAML."""
    jobs = []
    for name, config in run.spec["workflow"]["jobs"].items():
        matrix = config.get("strategy", {}).get("matrix", {})
        repositories = [{}]
        if matrix:
            assert matrix["repository"] in (
                "${{ fromJSON(needs.plan.outputs.releases) }}",
                "${{ fromJSON(needs.train.outputs.releases) }}",
            )
            repositories = run.repositories
        assert len(repositories) <= MAX_JOBS
        for repository in repositories[:MAX_JOBS]:
            job = Job(run, name, config, repository)
            job.runner = str(evaluate(config["runs-on"], run.inputs))
            job.group = group_name(config, run, repository)
            jobs.append(job)
    return jobs


class Timeline:
    """A finite discrete-event scheduler; each minute advances the declared jobs."""

    def __init__(self, specs: dict, release_free=True, leg_minutes=15):
        self.specs = specs
        self.release_free = release_free
        self.leg_minutes = leg_minutes
        self.runs: list[Run] = []
        self.owners: dict[str, Run | Job] = {}
        self.pending: dict[str, Run | Job] = {}
        self.events: list[tuple] = []

    def add_run(self, kind: str, minute: int, repositories: list[dict]) -> Run:
        assert len(self.runs) < MAX_RUNS
        spec = self.specs[kind]
        runner = "arc-cauda-train" if kind == "merge-train" else "arc-cauda"
        run = Run(len(self.runs) + 1, kind, minute, spec, call_inputs(spec, runner), repositories)
        run.group = group_name(spec["caller"], run, {})
        run.jobs = expand_jobs(run)
        self.runs.append(run)
        return run

    def cancel(self, entrant: Run | Job, minute: int) -> None:
        entrants = entrant.jobs if isinstance(entrant, Run) else [entrant]
        for job in entrants[:MAX_JOBS]:
            job.state, job.end = "cancelled", minute
            self.events.append(("cancelled", job.run.number, job.name, minute))
            self.release_group(job)
        entrant.state = "cancelled"
        self.release_group(entrant)

    def acquire(self, entrant: Run | Job, config: dict, minute: int) -> bool:
        group = entrant.group
        if not group or self.owners.get(group) is entrant:
            return True
        owner = self.owners.get(group)
        if owner is not None and config.get("concurrency", {}).get("cancel-in-progress", False):
            self.cancel(owner, minute)
            owner = None
        if owner is None:
            self.owners[group] = entrant
            self.pending.pop(group, None)
            return True
        prior = self.pending.get(group)
        if prior is not None and prior is not entrant:
            self.cancel(prior, minute)
        self.pending[group] = entrant
        return False

    def release_group(self, entrant: Run | Job) -> None:
        if self.owners.get(entrant.group) is entrant:
            self.owners.pop(entrant.group)
            pending = self.pending.pop(entrant.group, None)
            if pending is not None:
                self.owners[entrant.group] = pending
        elif self.pending.get(entrant.group) is entrant:
            self.pending.pop(entrant.group)

    def finish(self, minute: int) -> None:
        for run in self.runs[:MAX_RUNS]:
            for job in run.jobs[:MAX_JOBS]:
                duration = self.leg_minutes if job.repository else 8 if job.name == "train" else 0
                if job.state == "running" and minute >= job.start + duration:
                    job.state, job.end = "done", minute
                    self.release_group(job)
            if run.state == "active" and all(j.state in {"done", "cancelled"} for j in run.jobs):
                run.state = "done"
                self.release_group(run)
                self.trigger_release(run, minute)

    def trigger_release(self, run: Run, minute: int) -> None:
        if run.kind != "merge-train" or "merge-train-release" not in self.specs:
            return
        trigger = self.specs["merge-train-release"]["caller"]["on"].get("workflow_run", {})
        if run.spec["caller"]["name"] in trigger.get(
            "workflows", []
        ) and "completed" in trigger.get("types", []):
            self.add_run("merge-train-release", minute, run.repositories)

    def ready(self, job: Job) -> bool:
        needs = job.config.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        dependencies = [j for j in job.run.jobs if j.name in needs]
        if not all(j.state == "done" for j in dependencies):
            return False
        strategy = job.config.get("strategy", {})
        maximum = strategy.get("max-parallel", MAX_JOBS)
        maximum = evaluate(maximum, job.run.inputs) if isinstance(maximum, str) else maximum
        siblings = [
            j for j in job.run.jobs if j.name == job.name and j.state in {"queued", "running"}
        ]
        return job.state == "queued" or len(siblings) < maximum

    def start_jobs(self, run: Run, minute: int) -> None:
        for job in run.jobs[:MAX_JOBS]:
            if job.state not in {"new", "queued"} or not self.ready(job):
                continue
            if not self.acquire(job, job.config, minute):
                continue
            job.state = "queued"
            # Real labels decide runner availability, even in the planted old layout.
            if job.runner == "arc-cauda" and not self.release_free:
                continue
            job.state, job.start = "running", minute
            self.events.append(("start", run.number, job.name, minute))

    def play(self, arrivals: list[tuple[int, str, list[dict]]]) -> Timeline:
        assert len(arrivals) <= MAX_RUNS
        for minute in range(HORIZON):
            self.finish(minute)
            for scheduled, kind, repositories in arrivals[:MAX_RUNS]:
                if scheduled == minute:
                    self.add_run(kind, minute, repositories)
            for run in self.runs[:MAX_RUNS]:
                if run.state == "new" and self.acquire(run, run.spec["caller"], minute):
                    run.state = "active"
                if run.state == "active":
                    self.start_jobs(run, minute)
        return self

    def legs(self) -> list[Job]:
        return [job for run in self.runs for job in run.jobs if job.repository]
