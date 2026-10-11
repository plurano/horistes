# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Service rules: a workflow that runs the Scorecard action keeps to what the service accepts.

    scorecard.py check [--root DIR]

Before it stores a published result, the Scorecard service reads the workflow
file that ran the action and refuses the result when the file breaks one of
its rules. For a reusable workflow that file is the called one, so an edit
here would stop the publication of every caller, and it would show only
there, at run time. This check reads the same rules from the text:

- the workflow sets no `env` and no `defaults` and grants no write scope to
  all jobs;
- one job runs the action, and no other job may write `id-token`;
- that job has no container, no services, no `env` and no `defaults`, and
  runs on one label of a hosted Ubuntu image, written out, 22.04 or newer;
- every step of that job uses an action of the service's list (ALLOWED
  below); a step that runs a command is refused.

Source: ossf/scorecard-infra, api/app/server/verify_workflow.go at commit
ffe0a11789f8 (2026-09-28), read 2026-10-11. The service also asks the forge
whether a commit pin belongs to the action's repository; that is not read
here. The service picks the job that runs the action in no fixed order, so
a second such job is refused here; it also takes the action as an image
(`docker://`), which is refused here as a step outside the list. A workflow
that does not run the action is not judged.

Exit codes: 0 pass, 1 a rule is broken, 3 input error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

ACTION = "ossf/scorecard-action"
# The names under which the service recognises the job: the action, and its image.
RUNS = (ACTION, "gcr.io/openssf/scorecard-action", "ghcr.io/ossf/scorecard-action")
ALLOWED = (
    "actions/checkout",
    "actions/create-github-app-token",
    ACTION,
    "actions/upload-artifact",
    "github/codeql-action/upload-sarif",
    "step-security/harden-runner",
)
LABEL = re.compile(r"ubuntu-(latest|\d{2}\.\d{2})(-arm)?")
# Version labels are two digits, a dot and two digits, so the order of the
# texts is the order of the versions.
OLDEST = "22.04"
JOB_KEYS = ("container", "env", "defaults")
IMAGE = "docker://"


def _steps(job: object) -> list:
    steps = job.get("steps") if isinstance(job, dict) else None
    return steps if isinstance(steps, list) else []


def _used(step: object) -> str | None:
    """The action or image a step uses, without its reference; None for a step that uses none."""
    uses = step.get("uses") if isinstance(step, dict) else None
    if not isinstance(uses, str):
        return None
    if uses.startswith(IMAGE):
        return uses[len(IMAGE) :].split("@", 1)[0].split(":", 1)[0]
    return uses.split("@", 1)[0]


def _running(doc: dict) -> list[str]:
    """The jobs with a step that runs the action, in the order of the file."""
    return [
        str(name)
        for name, job in (doc.get("jobs") or {}).items()
        if any(_used(step) in RUNS for step in _steps(job))
    ]


def _label_problem(runs_on: object) -> str:
    """Why the service would refuse what the job runs on."""
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    if len(labels) != 1 or not isinstance(labels[0], str):
        return "does not run on exactly one label"
    found = LABEL.fullmatch(labels[0])
    if found is None or (found[1] != "latest" and found[1] < OLDEST):
        return f"runs on `{labels[0]}`: no hosted Ubuntu label of {OLDEST} or newer, written out"
    return ""


def _workflow_problems(doc: dict, running: list[str]) -> list[str]:
    """Rules on the workflow as a whole and on its other jobs."""
    analysis = running[0]
    problems = [f"the workflow sets `{key}`" for key in ("env", "defaults") if key in doc]
    if len(running) > 1:
        names = ", ".join(f"`{name}`" for name in running)
        problems.append(f"jobs {names} all run the action; the service judges one, not a fixed one")
    if "permissions" in doc:
        for scope in ciwf.write_scopes(doc["permissions"]):
            problems.append(f"the workflow lets every job write: {scope}")
    for name, job in (doc.get("jobs") or {}).items():
        scopes = job.get("permissions") if isinstance(job, dict) else None
        if name != analysis and isinstance(scopes, dict) and scopes.get("id-token") == "write":
            problems.append(f"job `{name}` may write `id-token`; only job `{analysis}` may")
    return problems


def _job_problems(name: str, job: dict) -> list[str]:
    """Rules on the job that runs the action."""
    problems = [f"job `{name}` sets `{key}`" for key in JOB_KEYS if key in job]
    if job.get("services"):
        problems.append(f"job `{name}` sets `services`")
    label = _label_problem(job.get("runs-on"))
    if label:
        problems.append(f"job `{name}` {label}")
    for number, step in enumerate(_steps(job), 1):
        used = _used(step)
        if used is None:
            problems.append(f"job `{name}`, step {number} runs a command; it may only use actions")
        elif used not in ALLOWED:
            problems.append(f"job `{name}`, step {number} uses {used}, which is not on the list")
    return problems


def check(root: Path) -> int:
    try:
        documents = ciwf.files(root)
    except ciwf.ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    judged, problems = 0, []
    for path, kind, doc in documents:
        running = _running(doc) if kind == "workflow" else []
        if not running:
            continue
        judged += 1
        found = _workflow_problems(doc, running)
        found += _job_problems(running[0], doc["jobs"][running[0]])
        problems += [f"{path}: {problem}" for problem in found]
    for line in problems:
        ciwf.complain(f"FAIL: {line}")
    if problems:
        return 1
    ciwf.say(
        f"PASS: {judged} workflow(s) run the Scorecard action, each within the service's rules"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    return check(args.root)


if __name__ == "__main__":
    sys.exit(main())
