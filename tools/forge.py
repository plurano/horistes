# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Reads the runs of a trial from the forge into the shape verdict.py judges.

Every request is a GET through the forge's command-line client (`gh api`),
so the reader contacts no host but the forge and changes nothing there. The
client is taken from the environment variable GH when it is set.

Shape of the result:

    {"source": "live forge", "repository": "owner/name", "errors": [],
     "subjects": [{"commit": ..., "event": ..., "ref": ..., "occurrence": 1,
                   "checks": [{"name": ..., "workflow": ...,
                               "attempts": [{"attempt": 1, "status": ...,
                                             "conclusion": ..., "failed_steps": [],
                                             "annotations": [], "seconds": 12.0}]}]}]}

A subject is one triggering event on one commit. The forge gives the runs
of one event no shared identifier, so runs are grouped by commit, event and
branch, and where one workflow ran several times for such a group (a draft
marked ready, a reopened pull request) its n-th run in time belongs to the
n-th subject.

A request that fails is recorded under "errors" and ends the reading; the
caller must treat a result with errors as unusable.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from urllib.parse import quote

PER_PAGE = 100
MAX_PAGES = 10
RETRIES = 4
WAIT_SECONDS = 20
DEADLINE_SECONDS = 60
HEADERS = (
    "-H",
    "Accept: application/vnd.github+json",
    "-H",
    "X-GitHub-Api-Version: 2022-11-28",
)


class ForgeError(Exception):
    """A request the forge did not answer with readable data."""


def api(path: str) -> object:
    """The decoded answer of one GET; a rate limit is waited out a few times."""
    command = [*shlex.split(os.environ.get("GH", "gh")), "api", *HEADERS, path]
    for attempt in range(RETRIES):
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=DEADLINE_SECONDS
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"GET {path}: {err}") from err
        if done.returncode == 0:
            try:
                return json.loads(done.stdout)
            except ValueError as err:
                raise ForgeError(f"GET {path}: answer is not JSON ({err})") from err
        if "rate limit" not in done.stderr.lower():
            raise ForgeError(f"GET {path}: {done.stderr.strip()[:300] or done.returncode}")
        if attempt + 1 < RETRIES:
            time.sleep(WAIT_SECONDS * (attempt + 1))
    raise ForgeError(f"GET {path}: rate limit, {RETRIES} attempts used")


def paged(path: str, key: str | None) -> list[dict]:
    """Every entry of a listing; `key` names the list inside the answer, None when it is the answer.

    A listing longer than MAX_PAGES pages is an error, never a silent cut.
    """
    entries: list[dict] = []
    joint = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        answer = api(f"{path}{joint}per_page={PER_PAGE}&page={page}")
        batch = answer.get(key) if key and isinstance(answer, dict) else answer
        if not isinstance(batch, list):
            raise ForgeError(f"GET {path}: no list{' ' + key if key else ''} in the answer")
        entries.extend(entry for entry in batch if isinstance(entry, dict))
        if len(batch) < PER_PAGE:
            return entries
    raise ForgeError(f"GET {path}: more than {MAX_PAGES * PER_PAGE} entries")


def _seconds(job: dict) -> float | None:
    try:
        started = datetime.fromisoformat(str(job.get("started_at")).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(job.get("completed_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


def _attempt(repository: str, number: int, job: dict) -> dict:
    """One attempt of one job: its conclusion and what names the reason of a failure."""
    failed = [
        str(step.get("name"))
        for step in job.get("steps") or []
        if isinstance(step, dict) and step.get("conclusion") == "failure"
    ]
    notes = []
    if job.get("conclusion") in ("failure", "timed_out"):
        for note in paged(f"repos/{repository}/check-runs/{job.get('id')}/annotations", None):
            notes.append(" ".join(str(note[k]) for k in ("title", "message") if note.get(k)))
    return {
        "attempt": number,
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "failed_steps": failed,
        "annotations": notes,
        "seconds": _seconds(job),
    }


def _checks(repository: str, run: dict) -> list[dict]:
    """The jobs of one run as checks, each with every attempt of the run."""
    checks: dict[str, dict] = {}
    for number in range(1, int(run.get("run_attempt") or 1) + 1):
        path = f"repos/{repository}/actions/runs/{run['id']}/attempts/{number}/jobs"
        for job in paged(path, "jobs"):
            name = str(job.get("name"))
            check = checks.setdefault(
                name, {"name": name, "workflow": run.get("path"), "attempts": []}
            )
            check["attempts"].append(_attempt(repository, number, job))
    return list(checks.values())


def _groups(runs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Runs as subjects: (commit, event, ref, occurrence) with the runs that belong to it."""
    grouped: dict[tuple, dict[str, list[dict]]] = {}
    for run in sorted(runs, key=lambda r: (str(r.get("created_at")), r["id"])):
        key = (run.get("head_sha"), run.get("event"), run.get("head_branch"))
        grouped.setdefault(key, {}).setdefault(str(run.get("path")), []).append(run)
    subjects = []
    for (commit, event, ref), per_workflow in grouped.items():
        for index in range(max(len(found) for found in per_workflow.values())):
            subject = {"commit": commit, "event": event, "ref": ref, "occurrence": index + 1}
            members = [found[index] for found in per_workflow.values() if index < len(found)]
            subjects.append((subject, members))
    return subjects


def collect(repository: str, branches: list[str], workflows: set[str]) -> dict:
    """The runs of the named workflow files on the named branches, as subjects with checks."""
    data = {"source": "live forge", "repository": repository, "errors": [], "subjects": []}
    try:
        runs = {}
        for branch in branches:
            listing = f"repos/{repository}/actions/runs?branch={quote(branch, safe='')}"
            for run in paged(listing, "workflow_runs"):
                if run.get("path") in workflows and "id" in run:
                    runs[run["id"]] = run
        for subject, members in _groups(list(runs.values())):
            subject["checks"] = []
            for run in members:
                subject["checks"].extend(_checks(repository, run))
            data["subjects"].append(subject)
    except ForgeError as err:
        data["errors"].append(str(err))
    return data
