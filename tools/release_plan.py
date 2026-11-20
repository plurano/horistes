# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Train release hand-off and artifact validation (ADR-0098)."""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_labels import MAX_BYTES, CallError, call, read_default_branch
from release_workflows import MAX_RELEASES, check_hold_labels

MAX_RELEASE_PARALLEL = 3
PLAN_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
PLAN_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


def check_release_parallel(raw):
    """Release jobs at once, a whole number from 1 to 3 (HISS-06)."""
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= MAX_RELEASE_PARALLEL:
        raise ValueError(
            f"release_max_parallel is {raw!r}, not a whole number from 1 to {MAX_RELEASE_PARALLEL}"
        )
    return int(raw)


def check_plan_repository(item):
    """Allow only repository identifiers safe for a matrix and a token scope."""
    if not isinstance(item, dict) or set(item) != {"owner", "name"}:
        raise CallError("release repository must contain exactly owner and name")
    owner, name = item["owner"], item["name"]
    if not isinstance(owner, str) or not PLAN_OWNER.fullmatch(owner):
        raise CallError("release repository owner is invalid (1..39 letters, digits or hyphens)")
    if not isinstance(name, str) or not PLAN_NAME.fullmatch(name) or name in (".", ".."):
        raise CallError("release repository name is invalid (1..100 letters, digits, dot, _ or -)")
    return (owner.casefold(), name.casefold())


def check_plan_hold(hold):
    """A bounded single-line output value, preserved exactly."""
    if not isinstance(hold, str):
        raise CallError("release hold_labels must be a string")
    return check_hold_labels(hold, CallError)


def check_release_plan(data):
    """Validate the versioned artifact with exact keys and scalar bounds."""
    if not isinstance(data, dict) or set(data) != {"format", "hold_labels", "releases"}:
        raise CallError("release list must contain exactly format, hold_labels and releases")
    if (
        isinstance(data["format"], bool)
        or not isinstance(data["format"], int)
        or data["format"] != 1
    ):
        raise CallError("release list format must be 1")
    hold = check_plan_hold(data["hold_labels"])
    releases = data["releases"]
    if not isinstance(releases, list) or len(releases) > MAX_RELEASES:
        raise CallError(f"release list must be a list of at most {MAX_RELEASES} repositories")
    seen = set()
    for item in releases[:MAX_RELEASES]:
        key = check_plan_repository(item)
        if key in seen:
            raise CallError("release list contains a duplicate repository (case-insensitive)")
        seen.add(key)
    return releases, hold


def write_plan_output(env, releases, hold):
    """One compact output line per value; validation disallows line injection."""
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(
            f"releases={json.dumps(releases, separators=(',', ':'))}\nhold_labels={hold}\n"
        )
    return 0


def read_release_plan(path):
    """Read a bounded local artifact; decode and I/O errors retain their context."""
    try:
        with open(path, encoding="utf-8") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CallError(f"release list exceeds {MAX_BYTES} characters")
        return check_release_plan(json.loads(raw))
    except (OSError, UnicodeError, ValueError) as err:
        raise CallError(f"read release list: {err}") from err


def check_train_inputs(env, workflow_key):
    """Refuse ambiguous run ids and workflow paths before any request."""
    run_id = env.get("TRAIN_RUN_ID", "")
    workflow = env.get(workflow_key, "")
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise CallError("TRAIN_RUN_ID must be ASCII digits with no leading zero")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml", workflow):
        raise CallError(f"{workflow_key} must be one .yml or .yaml file name, without a path")
    return run_id, workflow


def check_artifact_id(env, required=False):
    """An automatic dispatch identifies its upload; manual requests may omit it."""
    artifact_id = env.get("ARTIFACT_ID", "")
    if (required or artifact_id) and not re.fullmatch(r"[1-9][0-9]*", artifact_id):
        raise CallError("ARTIFACT_ID must be ASCII digits with no leading zero")
    return artifact_id


def cmd_dispatch(env):
    """Start one release run; retrying a dispatch could start a duplicate."""
    run_id, workflow = check_train_inputs(env, "RELEASE_WORKFLOW")
    artifact_id = check_artifact_id(env, required=True)
    api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo, token = env["REPOSITORY"], env.get("GITHUB_TOKEN", "")
    branch = read_default_branch(api_url, repo, token)
    url = f"{api_url}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = {"ref": branch, "inputs": {"train_run_id": run_id, "artifact_id": artifact_id}}
    try:
        status, _ = call(url, token, method="POST", body=body)
    except CallError as err:
        if err.status == 404:
            raise CallError(
                f"add .github/workflows/{workflow} on the default branch from "
                "templates/callers/merge-train-release.yml.tmpl",
                err.status,
            ) from err
        raise
    if status not in {200, 204}:
        raise CallError(f"dispatch release workflow: unexpected HTTP {status}", status)
    return 0


def check_train_run(run, workflow, default_branch, repo):
    """Validate the referenced run before its artifact is read or any app token minted."""
    if run.get("path") != f".github/workflows/{workflow}":
        raise CallError("train run path must match the train workflow file")
    if run.get("event") not in {"schedule", "workflow_dispatch"}:
        raise CallError("train run event must be schedule or workflow_dispatch")
    if run.get("head_branch") != default_branch:
        raise CallError("train run head branch must be the default branch")
    if str((run.get("repository") or {}).get("full_name") or "").casefold() != repo.casefold():
        raise CallError("train run repository must be the caller repository")
    if run.get("status") not in {"in_progress", "completed"}:
        raise CallError("train run status must be in_progress or completed")


def cmd_plan(env):
    """Accept only default-branch train runs before consuming their artifact."""
    run_id, workflow = check_train_inputs(env, "TRAIN_WORKFLOW")
    artifact_id = check_artifact_id(env)
    api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo, token = env["REPOSITORY"], env.get("GITHUB_TOKEN", "")
    _, run = call(f"{api_url}/repos/{repo}/actions/runs/{run_id}", token)
    branch = read_default_branch(api_url, repo, token)
    check_train_run(run, workflow, branch, repo)
    conclusion = run.get("conclusion")
    if run["status"] == "completed" and conclusion not in {"success", "failure"}:
        sys.stdout.write(f"::notice::train run conclusion {conclusion!r}; no release legs\n")
        return write_plan_output(env, [], "")
    try:
        check_release_parallel(env["MAX_PARALLEL"])
    except ValueError as err:
        sys.stdout.write(f"::error title=Release plan setting::{err}\n")
        return 3
    if not Path(env["FILE"]).exists():
        if artifact_id:
            raise CallError(f"artifact {artifact_id} supplied no release list file")
        sys.stdout.write(
            "::notice::train run uploaded no release list "
            "(switch off, or the sweep ended before its summary)\n"
        )
        return write_plan_output(env, [], "")
    releases, hold = read_release_plan(env["FILE"])
    if not releases:
        sys.stdout.write("::notice::no repository named in the release list\n")
    return write_plan_output(env, releases, hold)


def main(argv, env):
    """Dispatch a release run or validate its plan."""
    if len(argv) < 2:
        sys.stderr.write("usage: release_plan.py <plan|dispatch>\n")
        return 2
    commands = {"plan": cmd_plan, "dispatch": cmd_dispatch}
    if argv[1] not in commands:
        sys.stderr.write(f"unknown command: {argv[1]}\n")
        return 2
    try:
        return commands[argv[1]](env)
    except (CallError, KeyError, OSError, json.JSONDecodeError) as err:
        sys.stdout.write(f"::error::{argv[1]}: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
