# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Runner minutes: measures workflow durations against declared timeout-minutes.

    runners.py check [--root DIR] [--repo OWNER/REPO] [--json] [--strict]
    runners.py list  [--root DIR]

Reads every `timeout-minutes` value from workflow files under .github/workflows/,
fetches run durations from the forge API, and verifies:
- A run approaching timeout (max or p95 duration > configured fraction, e.g. 80%) fails.
- A wastefully generous timeout (> 5x p95 duration) warns.

Exit codes: 0 pass, 1 finding, 3 could not measure (no token, no runs).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf
import forge

DEFAULT_FRACTION = 0.8
DEFAULT_GENEROUS_FACTOR = 5.0
MAX_WORKFLOWS = 100
MAX_RUNS = 1000
DEADLINE_SECONDS = 60
WORKFLOW_FOLDER = ".github/workflows"


class RunnersError(Exception):
    """The runner minutes check could not be completed."""


class Evaluation(NamedTuple):
    workflow: str
    name: str
    path: str
    timeout_minutes: int
    measured_p95_seconds: float
    measured_max_seconds: float
    run_count: int
    finding: str | None
    severity: str | None


def read_job_timeouts(source: Path | dict) -> dict[str, int]:
    """Read timeout-minutes from all jobs in a workflow document or file."""
    doc = ciwf.load(source) if isinstance(source, Path) else source
    timeouts: dict[str, int] = {}
    jobs = doc.get("jobs") or {}
    if isinstance(jobs, dict):
        for job_id, job_spec in list(jobs.items())[:MAX_WORKFLOWS]:
            if isinstance(job_spec, dict) and "timeout-minutes" in job_spec:
                val = job_spec.get("timeout-minutes")
                if isinstance(val, (int, float)):
                    timeouts[str(job_id)] = int(val)
    return timeouts


def workflow_timeout(source: Path | dict) -> int:
    """Maximum declared timeout-minutes across jobs in a workflow."""
    timeouts = read_job_timeouts(source)
    return max(timeouts.values(), default=0)


def load_workflow_timeouts(root: Path) -> dict[str, dict]:
    """Read timeout-minutes and metadata for every workflow under .github/workflows/."""
    workflows: dict[str, dict] = {}
    wf_dir = root / WORKFLOW_FOLDER
    if not wf_dir.is_dir():
        return workflows
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(wf_dir.glob(pattern))[:MAX_WORKFLOWS]:
            try:
                doc = ciwf.load(path)
            except ciwf.ReadError as err:
                raise RunnersError(str(err)) from err
            rel_name = path.name
            job_timeouts = read_job_timeouts(doc)
            timeout = max(job_timeouts.values(), default=0)
            workflows[rel_name] = {
                "workflow": rel_name,
                "path": str(path.relative_to(root)),
                "name": str(doc.get("name") or rel_name),
                "timeout_minutes": timeout,
                "timeout-minutes": timeout,
                "job_timeouts": job_timeouts,
            }
    return workflows


def detect_repository(root: Path, explicit: str | None = None) -> str:
    """Determine repository owner/name from argument, env, or git remote."""
    if explicit:
        return explicit
    if os.environ.get("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEADLINE_SECONDS,
        )
        if done.returncode == 0 and done.stdout.strip():
            url = done.stdout.strip()
            match = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+?)(?:\.git)?$", url)
            if match:
                return match.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RunnersError("no repository: pass --repo or set GITHUB_REPOSITORY")


def fetch_workflow_runs(repository: str, workflow_id: str) -> list[dict]:
    """Fetch workflow runs from the forge API."""
    path = f"repos/{repository}/actions/workflows/{quote(workflow_id, safe='')}/runs"
    try:
        return forge.paged(path, "workflow_runs")
    except forge.ForgeError as err:
        raise RunnersError(f"could not read workflow runs for {workflow_id}: {err}") from err


def _parse_time(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_duration(run: dict) -> float | None:
    """Extract duration in seconds from a workflow run."""
    if "seconds" in run and isinstance(run["seconds"], (int, float)):
        return float(run["seconds"])
    if "duration" in run and isinstance(run["duration"], (int, float)):
        return float(run["duration"])
    status = run.get("status")
    if status and status != "completed":
        return None
    started = _parse_time(run.get("run_started_at")) or _parse_time(run.get("created_at"))
    ended = _parse_time(run.get("updated_at"))
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds())


def calculate_percentile_95(durations: list[float]) -> float:
    """Compute the 95th percentile of run durations."""
    if not durations:
        return 0.0
    sorted_durations = sorted(durations)
    n = len(sorted_durations)
    if n == 1:
        return sorted_durations[0]
    rank = 0.95 * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    weight = rank - lower
    return sorted_durations[lower] + weight * (sorted_durations[upper] - sorted_durations[lower])


def evaluate_workflow(
    workflow_info: dict,
    durations: list[float],
    fraction: float = DEFAULT_FRACTION,
    generous_factor: float = DEFAULT_GENEROUS_FACTOR,
) -> Evaluation:
    """Evaluate workflow run durations against declared timeout."""
    name = workflow_info["name"]
    wf_path = workflow_info["path"]
    timeout_m = workflow_info["timeout_minutes"]
    timeout_s = timeout_m * 60.0
    p95_s = calculate_percentile_95(durations)
    max_s = max(durations) if durations else 0.0
    count = len(durations)

    finding = None
    severity = None
    if timeout_m <= 0:
        finding = "no timeout-minutes declared"
        severity = "warn"
    elif max_s > fraction * timeout_s or p95_s > fraction * timeout_s:
        finding = (
            f"measured duration exceeds {int(fraction * 100)}% of timeout "
            f"({timeout_m}m): max {max_s:.1f}s, p95 {p95_s:.1f}s"
        )
        severity = "fail"
    elif count == 0:
        finding = "no completed runs measured"
        severity = "warn"
    elif p95_s > 0 and timeout_s > generous_factor * p95_s:
        finding = (
            f"timeout ({timeout_m}m) is more than {int(generous_factor)}x "
            f"measured p95 ({p95_s:.1f}s): wastefully generous"
        )
        severity = "warn"

    return Evaluation(
        workflow=workflow_info.get("workflow", wf_path.split("/")[-1]),
        name=name,
        path=wf_path,
        timeout_minutes=timeout_m,
        measured_p95_seconds=p95_s,
        measured_max_seconds=max_s,
        run_count=count,
        finding=finding,
        severity=severity,
    )


def evaluation_to_dict(ev: Evaluation, job_timeouts: dict[str, int] | None = None) -> dict:
    """Format an Evaluation as a JSON-serializable dictionary."""
    return {
        "workflow": ev.workflow,
        "name": ev.name,
        "path": ev.path,
        "timeout_minutes": ev.timeout_minutes,
        "timeout-minutes": ev.timeout_minutes,
        "job_timeouts": job_timeouts or {},
        "measured_p95_seconds": round(ev.measured_p95_seconds, 2),
        "measured_max_seconds": round(ev.measured_max_seconds, 2),
        "run_count": ev.run_count,
        "finding": ev.finding,
        "severity": ev.severity,
    }


def listing(root: Path) -> int:
    """List every workflow and its declared timeout-minutes."""
    workflows = load_workflow_timeouts(root)
    if not workflows:
        ciwf.complain(f"ERROR: {root}: no workflow files found")
        return 3
    for name, info in sorted(workflows.items())[:MAX_WORKFLOWS]:
        jobs_str = ", ".join(f"{j}: {t}m" for j, t in sorted(info["job_timeouts"].items()))
        ciwf.say(f"{name}: timeout-minutes: {info['timeout_minutes']} ({jobs_str or 'no jobs'})")
    ciwf.say(f"Total: {len(workflows)} workflow(s)")
    return 0


def _print_evaluations(evaluations: list[Evaluation]) -> None:
    for ev in evaluations[:MAX_WORKFLOWS]:
        if ev.severity == "fail":
            ciwf.complain(f"FAIL: {ev.workflow}: {ev.finding}")
        elif ev.severity == "warn":
            ciwf.say(f"WARN: {ev.workflow}: {ev.finding}")
        else:
            ciwf.say(
                f"PASS: {ev.workflow}: timeout {ev.timeout_minutes}m, "
                f"measured p95 {ev.measured_p95_seconds:.1f}s, "
                f"max {ev.measured_max_seconds:.1f}s"
            )


def check(
    root: Path,
    repo: str | None = None,
    fraction: float = DEFAULT_FRACTION,
    generous_factor: float = DEFAULT_GENEROUS_FACTOR,
    workflow_filter: str | None = None,
    as_json: bool = False,
    strict: bool = False,
    fetch_fn: Callable[[str, str], list[dict]] | None = None,
) -> int:
    """Measure workflow runs and compare against timeout-minutes."""
    all_workflows = load_workflow_timeouts(root)
    if not all_workflows:
        ciwf.complain(f"ERROR: {root}: no workflow files found")
        return 3
    if workflow_filter:
        base_filter = workflow_filter.split("/")[-1]
        keep = (workflow_filter, base_filter)
        all_workflows = {k: v for k, v in all_workflows.items() if k in keep}
        if not all_workflows:
            ciwf.complain(f"ERROR: workflow '{workflow_filter}' not found under {root}")
            return 3

    repository = detect_repository(root, repo)
    evaluations: list[Evaluation] = []
    total_runs = 0
    fetch = fetch_fn or fetch_workflow_runs

    job_timeout_map: dict[str, dict[str, int]] = {}
    for wf_key, wf_info in sorted(all_workflows.items())[:MAX_WORKFLOWS]:
        runs_data = fetch(repository, wf_key)
        durations = []
        for r in runs_data[:MAX_RUNS]:
            d = extract_duration(r)
            if d is not None:
                durations.append(d)
        total_runs += len(durations)
        ev = evaluate_workflow(wf_info, durations, fraction, generous_factor)
        evaluations.append(ev)
        job_timeout_map[ev.workflow] = wf_info["job_timeouts"]

    if total_runs == 0:
        ciwf.complain("ERROR: could not measure: no runs found")
        return 3

    if as_json:
        dicts = [evaluation_to_dict(ev, job_timeout_map.get(ev.workflow, {})) for ev in evaluations]
        ciwf.say(json.dumps(dicts, indent=2))
    else:
        _print_evaluations(evaluations)

    has_failures = any(ev.severity == "fail" for ev in evaluations)
    has_warnings = any(ev.severity == "warn" for ev in evaluations)
    if has_failures or (strict and has_warnings):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", nargs="?", default="check", choices=("check", "list"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--repo", default=None, help="owner/repository on forge")
    parser.add_argument("--workflow", default=None, help="specific workflow file to check")
    parser.add_argument(
        "--fraction",
        type=float,
        default=DEFAULT_FRACTION,
        help="fail threshold ratio",
    )
    parser.add_argument("--generous-factor", type=float, default=DEFAULT_GENEROUS_FACTOR)
    parser.add_argument("--json", action="store_true", help="output JSON")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args()

    try:
        if args.command == "list":
            return listing(args.root)
        return check(
            root=args.root,
            repo=args.repo,
            fraction=args.fraction,
            generous_factor=args.generous_factor,
            workflow_filter=args.workflow,
            as_json=args.json,
            strict=args.strict,
        )
    except RunnersError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
