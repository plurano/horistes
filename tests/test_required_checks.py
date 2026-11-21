# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the reusable required-checks judge and forge `--include` extensions (ADR-0108)."""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import CHECKOUT, ROOT, Tree

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import forge
import links
import render
import required_checks
from required_checks import (
    API_BACKOFF_SECONDS,
    FLOOR,
    MAX_CONTEXTS,
    InputError,
    below_floor,
    main,
    parse_config,
    run_judge,
    self_test,
)

REPO = "plurano/horistes"
SHA = "bfe95b5170e7e5e1e9681ea4d766c14d55e28851"
SHA256 = "b" * 64
CHECKS_URL = f"repos/{REPO}/commits/{SHA}/check-runs?per_page=100&page=1"
RUNS_URL = f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100&page=1"
STATUSES_URL = f"repos/{REPO}/commits/{SHA}/statuses?per_page=100&page=1"
WORKFLOW_PATH = ROOT / ".github/workflows/required-checks.yml"
TEMPLATE_PATH = ROOT / "templates/callers/required-checks.yml.tmpl"
ADR_PATH = ROOT / "docs/adr/0108-a-reusable-workflow-judges-the-required-checks.md"


class Clock:
    """Controllable simulated clock recording every sleep duration."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def check_item(
    run_id: int,
    name: str,
    conclusion: str | None = "success",
    *,
    status: str | None = None,
    app: str | None = "github-actions",
    suite_id: int | None = 1,
    started_at: str = "2026-11-21T12:00:00Z",
) -> dict:
    """Build one check-run API dictionary."""
    default_status = "in_progress" if conclusion is None else "completed"
    actual_status = default_status if status is None else status
    item: dict[str, object] = {
        "id": run_id,
        "name": name,
        "status": actual_status,
        "conclusion": conclusion,
        "started_at": started_at,
    }
    if app is not None:
        item["app"] = {"slug": app}
    if suite_id is not None:
        item["check_suite"] = {"id": suite_id}
    return item


def status_item(
    status_id: int,
    context: str,
    state: str = "success",
    *,
    updated_at: str = "2026-11-21T12:00:00Z",
) -> dict:
    """Build one commit-status API dictionary."""
    return {
        "id": status_id,
        "context": context,
        "state": state,
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def wf_run(
    run_id: int,
    event: str = "push",
    *,
    suite_id: int = 1,
    head_branch: str = "main",
) -> dict:
    """Build one workflow-run API dictionary."""
    return {
        "id": run_id,
        "event": event,
        "head_branch": head_branch,
        "check_suite_id": suite_id,
    }


def base_answers(
    checks: list[dict] | dict = (),
    runs: list[dict] | dict = (),
    statuses: list[dict] | dict = (),
) -> dict[str, object]:
    """Build stand-in forge endpoint answers for one commit."""
    return {
        CHECKS_URL: checks if isinstance(checks, dict) else {"check_runs": list(checks)},
        RUNS_URL: runs if isinstance(runs, dict) else {"workflow_runs": list(runs)},
        STATUSES_URL: statuses if isinstance(statuses, dict) else list(statuses),
    }


def invoke_with_forge(
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, object],
    *,
    required: list[str] | str = ("Build",),
    event: str = "push",
    ref: str = "refs/heads/main",
    pull_request_only: list[str] | str = (),
    not_owed: list[str] | str = (),
    apps: dict[str, str] | str = (),
    wait_minutes: int | str = 60,
    rate_limit_wait_minutes: int | str = 30,
) -> tuple[int, str, str, Clock]:
    """Run `run_judge` through `forge.request` backed by `conftest.FAKE_CLIENT`."""
    for name, value in tree.forge(answers).items():
        monkeypatch.setenv(name, value)
    cfg = parse_config(
        repository=REPO,
        event_name=event,
        ref=ref,
        sha=SHA,
        required_json=required if isinstance(required, str) else json.dumps(list(required)),
        pull_request_only_json=(
            pull_request_only
            if isinstance(pull_request_only, str)
            else json.dumps(list(pull_request_only))
        ),
        not_owed_json=not_owed if isinstance(not_owed, str) else json.dumps(list(not_owed)),
        apps_json=apps if isinstance(apps, str) else json.dumps(dict(apps)),
        wait_minutes=wait_minutes,
        rate_limit_wait_minutes=rate_limit_wait_minutes,
    )
    clock = Clock()
    out, err = io.StringIO(), io.StringIO()
    rc = run_judge(cfg, now=clock.now, sleep=clock.sleep, out=out, err=err)
    return rc, out.getvalue(), err.getvalue(), clock


def _imported_module(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level != 0:
            return ["<relative>"]
        return [node.module.split(".")[0]] if node.module else []
    return []


def import_violations(source: str, allowed_siblings: frozenset[str] = frozenset()) -> list[str]:
    """Return non-stdlib imports not in `allowed_siblings`."""
    parsed = ast.parse(source)
    allowed = sys.stdlib_module_names | {"__future__"} | set(allowed_siblings)
    nodes = list(ast.walk(parsed))
    return [
        f"non-stdlib import: {mod}"
        for node in nodes[:10000]
        for mod in _imported_module(node)
        if mod not in allowed
    ]


# ---------------------------------------------------------------------------
# Stdlib, floor, workflow contract, ADR links, and sparse-checkout isolation
# ---------------------------------------------------------------------------


def test_required_checks_and_forge_are_stdlib_only() -> None:
    rc_src = (ROOT / "tools/required_checks.py").read_text(encoding="utf-8")
    forge_src = (ROOT / "tools/forge.py").read_text(encoding="utf-8")
    results_src = (ROOT / "tools/results.py").read_text(encoding="utf-8")
    assert import_violations(rc_src, frozenset({"forge"})) == []
    assert import_violations(forge_src, frozenset({"results"})) == []
    assert import_violations(results_src) == []
    assert import_violations("import yaml\n" + rc_src, frozenset({"forge"})) == [
        "non-stdlib import: yaml"
    ]


def test_python_floor_3d(monkeypatch: pytest.MonkeyPatch) -> None:
    assert below_floor((3, 11, 9)) is not None
    assert "3.12 or newer" in (below_floor((3, 11)) or "")
    assert below_floor((*FLOOR, 0)) is None
    assert below_floor((3, 13, 2)) is None

    monkeypatch.setattr(required_checks, "FLOOR", (sys.version_info[0] + 1, 0))
    out, err = io.StringIO(), io.StringIO()
    assert main(["self-test"], out=out, err=err) == 2
    assert "::error::the required-checks judge needs Python" in err.getvalue()


def test_workflow_inputs_and_permissions_contract() -> None:
    doc = ciwf.load(WORKFLOW_PATH)
    assert doc["permissions"] == {}
    inputs = doc["on"]["workflow_call"]["inputs"]
    assert set(inputs) == {
        "runner",
        "required",
        "pull-request-only",
        "not-owed",
        "apps",
        "wait-minutes",
        "rate-limit-wait-minutes",
    }
    assert inputs["runner"]["default"] == "ubuntu-26.04"
    assert inputs["runner"]["required"] is False
    assert inputs["required"]["required"] is True
    assert inputs["pull-request-only"]["default"] == "[]"
    assert inputs["not-owed"]["default"] == "[]"
    assert inputs["apps"]["default"] == "{}"
    assert inputs["wait-minutes"] == {
        "description": inputs["wait-minutes"]["description"],
        "type": "number",
        "required": False,
        "default": 60,
    }
    assert inputs["rate-limit-wait-minutes"] == {
        "description": inputs["rate-limit-wait-minutes"]["description"],
        "type": "number",
        "required": False,
        "default": 30,
    }


def test_workflow_job_timeout_steps_and_caller_template_contract() -> None:
    doc = ciwf.load(WORKFLOW_PATH)
    job = doc["jobs"]["required-checks"]
    assert job["name"] == "Required checks"
    assert job["runs-on"] == "${{ inputs.runner }}"
    assert job["permissions"] == {
        "contents": "read",
        "checks": "read",
        "statuses": "read",
        "actions": "read",
    }
    pattern = r"fromJSON\('(\[[^\']*?\])'\)\[inputs\.wait-minutes\]\s*\|\|\s*(\d+)"
    match = re.search(pattern, str(job["timeout-minutes"]))
    assert match is not None
    table = json.loads(match.group(1))
    assert int(match.group(2)) == 10
    assert table == [0, *(minute + 10 for minute in range(1, 241))]

    steps = job["steps"]
    assert steps[0]["if"] == "github.event.pull_request.draft == true"
    assert steps[1]["uses"] == CHECKOUT.split()[0]
    assert steps[1]["with"]["repository"] == "${{ job.workflow_repository }}"
    assert steps[1]["with"]["ref"] == "${{ job.workflow_sha }}"
    assert steps[1]["with"]["persist-credentials"] is False
    assert "self-test" in steps[2]["run"]
    assert "judge" in steps[3]["run"]

    rendered = render.render(
        TEMPLATE_PATH.read_text(encoding="utf-8"), render.SAMPLE, "required-checks.yml.tmpl"
    )
    assert render.caller_problems(rendered, ROOT) == []
    problems, link_count = links.check_links(ROOT, [ADR_PATH])
    assert problems == [] and link_count >= 8


def test_sparse_checkout_runs_isolated_self_test_and_judge(tmp_path: Path) -> None:
    doc = ciwf.load(WORKFLOW_PATH)
    sparse = doc["jobs"]["required-checks"]["steps"][1]["with"]["sparse-checkout"].splitlines()
    sparse_files = [line.strip() for line in sparse if line.strip()]
    assert sparse_files == ["tools/forge.py", "tools/required_checks.py", "tools/results.py"]

    isolated = tmp_path / "required-checks-source"
    for rel in sparse_files[:10]:
        dest = isolated / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text((ROOT / rel).read_text(encoding="utf-8"), encoding="utf-8")

    script = str(isolated / "tools/required_checks.py")
    cmd_self = [sys.executable, "-I", "-B", script, "self-test"]
    done_self = subprocess.run(cmd_self, capture_output=True, text=True, check=False, timeout=30)
    assert done_self.returncode == 0, done_self.stdout + done_self.stderr
    assert all(line.startswith("ok ") for line in done_self.stdout.splitlines())

    fixture_tree = Tree(tmp_path / "forge-standin")
    env = fixture_tree.forge(base_answers(checks=[check_item(1, "Gate", "success")]))
    run_env = {
        **os.environ,
        **env,
        "REPOSITORY": REPO,
        "EVENT_NAME": "push",
        "REF": "refs/heads/main",
        "COMMIT_SHA": SHA,
        "REQUIRED_JSON": '["Gate"]',
    }
    cmd_judge = [sys.executable, "-I", "-B", script, "judge"]
    done_judge = subprocess.run(
        cmd_judge, capture_output=True, text=True, check=False, timeout=30, env=run_env
    )
    assert done_judge.returncode == 0, done_judge.stdout + done_judge.stderr
    assert "OK (success): Gate" in done_judge.stdout

    (isolated / "tools/results.py").unlink()
    broken = subprocess.run(cmd_self, capture_output=True, text=True, check=False, timeout=30)
    assert broken.returncode != 0
    assert "No module named 'results'" in broken.stderr


def test_self_test_passes_and_catches_planted_judge_defects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.StringIO()
    assert self_test(out) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 11
    assert all(line.startswith("ok ") for line in lines)

    # Planted 1: accepting skipped on owed checks breaks self_test.
    monkeypatch.setattr(required_checks, "_owed_run_problem", lambda run: "")
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 2: accepting any app slug breaks self_test.
    monkeypatch.setattr(
        required_checks, "_check_run_admitted", lambda run, suites, apps: (run.get("name"), None)
    )
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 3: ignoring foreign-event suites breaks self_test.
    monkeypatch.setattr(required_checks, "foreign_suites", lambda runs, ev, ref: set())
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 4: accepting failed commit status breaks self_test.
    monkeypatch.setattr(required_checks, "_owed_status_problem", lambda status: "")
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 5: ignoring rate-limit budget bound breaks self_test.
    monkeypatch.setattr(
        required_checks.ApiSession, "_within_rate_limit_bounds", lambda self, d, w: True
    )
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 6: exempting pull-request-only on pull_request_target breaks self_test.
    monkeypatch.setattr(
        required_checks,
        "exempt_reason",
        lambda name, cfg: (
            "exempt" if name in cfg.pull_request_only and cfg.event_name != "pull_request" else ""
        ),
    )
    assert self_test(io.StringIO()) == 1


# ---------------------------------------------------------------------------
# Input validation 3D tests (positive, negative, boundary; zero API calls)
# ---------------------------------------------------------------------------


def test_parse_config_positive_and_boundaries() -> None:
    cfg = parse_config(
        repository=REPO,
        event_name="pull_request",
        ref="refs/pull/12/merge",
        pr_head_sha=SHA256,
        commit_sha="0" * 40,
        required_json='["Build", "CodeQL", "Status"]',
        pull_request_only_json='["CodeQL", "CodeQL"]',
        not_owed_json='["Status"]',
        apps_json='{"CodeQL": "github-advanced-security"}',
        wait_minutes=1,
        rate_limit_wait_minutes=0,
    )
    assert cfg.sha == SHA256
    assert cfg.required == ("Build", "CodeQL", "Status")
    assert cfg.pull_request_only == frozenset({"CodeQL"})
    assert cfg.not_owed == frozenset({"Status"})
    assert cfg.apps == {"CodeQL": "github-advanced-security"}
    assert cfg.wait_minutes == 1
    assert cfg.rate_limit_wait_minutes == 0

    max_names = [f"check-{i}" for i in range(MAX_CONTEXTS)]
    cfg_max = parse_config(
        repository=REPO,
        event_name="push",
        commit_sha=SHA,
        required_json=json.dumps(max_names),
        pull_request_only_json=json.dumps(max_names),
        not_owed_json=json.dumps(max_names),
        apps_json=json.dumps(dict.fromkeys(max_names, "github-actions")),
        wait_minutes="240",
        rate_limit_wait_minutes="90",
    )
    assert len(cfg_max.required) == MAX_CONTEXTS
    assert cfg_max.wait_minutes == 240
    assert cfg_max.rate_limit_wait_minutes == 90


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"repository": ""}, "repository:"),
        ({"repository": "bad-repo"}, "repository:"),
        ({"repository": "owner/.."}, "repository:"),
        ({"event_name": "   "}, "event:"),
        ({"sha": "", "commit_sha": "not-a-sha"}, "sha:"),
        ({"event_name": "pull_request", "sha": "", "pr_head_sha": ""}, "sha:"),
        ({"required_json": "{bad"}, "required: invalid JSON"),
        ({"required_json": "[NaN]"}, "required: invalid JSON"),
        ({"required_json": "{}"}, "required: must be a JSON list"),
        ({"required_json": "[]"}, "required: must not be empty"),
        ({"required_json": '[""]'}, "required: context names"),
        ({"required_json": '["a\\nb"]'}, "required: context names"),
        ({"required_json": "[123]"}, "required: context names"),
        ({"required_json": '["Build", "Build"]'}, "required: duplicate context name"),
        (
            {"required_json": json.dumps([f"c-{i}" for i in range(MAX_CONTEXTS + 1)])},
            "required: 501 entries exceeds limit",
        ),
        ({"pull_request_only_json": "{}"}, "pull-request-only: must be a JSON list"),
        ({"pull_request_only_json": "[1]"}, "pull-request-only: every entry must be a string"),
        (
            {"pull_request_only_json": '["Unknown"]'},
            "pull-request-only: unknown context 'Unknown'",
        ),
        (
            {"pull_request_only_json": json.dumps(["Build"] * (MAX_CONTEXTS + 1))},
            "pull-request-only: 501 entries exceeds limit",
        ),
        ({"not_owed_json": "true"}, "not-owed: must be a JSON list"),
        ({"not_owed_json": "[null]"}, "not-owed: every entry must be a string"),
        ({"not_owed_json": '["Absent"]'}, "not-owed: unknown context 'Absent'"),
        ({"apps_json": "[]"}, "apps: must be a JSON object"),
        ({"apps_json": '{"Unknown": "github-actions"}'}, "apps: unknown context 'Unknown'"),
        ({"apps_json": '{"Build": ""}'}, "apps: invalid app slug"),
        ({"apps_json": '{"Build": "bad slug"}'}, "apps: invalid app slug"),
        ({"apps_json": '{"Build": 7}'}, "apps: invalid app slug"),
        ({"wait_minutes": 0}, "wait-minutes: 0 is out of range 1..240"),
        ({"wait_minutes": -1}, "wait-minutes: -1 is out of range 1..240"),
        ({"wait_minutes": 241}, "wait-minutes: 241 is out of range 1..240"),
        ({"wait_minutes": "1.5"}, "wait-minutes: must be a whole number"),
        ({"wait_minutes": "abc"}, "wait-minutes: must be a whole number"),
        ({"wait_minutes": True}, "wait-minutes: must be a whole number"),
        (
            {"rate_limit_wait_minutes": -1},
            "rate-limit-wait-minutes: -1 is out of range 0..90",
        ),
        (
            {"rate_limit_wait_minutes": 91},
            "rate-limit-wait-minutes: 91 is out of range 0..90",
        ),
        (
            {"rate_limit_wait_minutes": "2.5"},
            "rate-limit-wait-minutes: must be a whole number",
        ),
        (
            {"rate_limit_wait_minutes": False},
            "rate-limit-wait-minutes: must be a whole number",
        ),
    ],
)
def test_malformed_inputs_fail_before_polling(
    tree: Tree, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], match: str
) -> None:
    for name, value in tree.forge({}).items():
        monkeypatch.setenv(name, value)
    kwargs: dict[str, Any] = {
        "repository": REPO,
        "event_name": "push",
        "sha": SHA,
        "required_json": '["Build"]',
    }
    kwargs.update(overrides)
    with pytest.raises(InputError, match=match):
        parse_config(**kwargs)
    assert tree.forge_calls() == []


def test_cli_malformed_input_exits_2_without_polling(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in tree.forge({}).items():
        monkeypatch.setenv(name, value)
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        [
            "judge",
            "--repository",
            REPO,
            "--event",
            "push",
            "--sha",
            SHA,
            "--required",
            "[]",
        ],
        env={},
        out=out,
        err=err,
    )
    assert rc == 2
    assert "Invalid required-checks input" in err.getvalue()
    assert "required: must not be empty" in err.getvalue()
    assert tree.forge_calls() == []


# ---------------------------------------------------------------------------
# Outcome semantics: owed checks, app slugs, foreign runs, statuses, exemptions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conclusion",
    [
        "skipped",
        "neutral",
        "cancelled",
        "timed_out",
        "action_required",
        "failure",
        "stale",
        "startup_failure",
    ],
)
def test_non_success_check_conclusions_all_fail(
    tree: Tree, monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", conclusion)]),
    )
    assert rc == 1
    assert clock.sleeps == []
    assert f"Build: {conclusion}" in err
    assert "All owed required checks reported success" not in out


def test_absent_and_still_running_owed_checks_fail_at_deadline(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, _, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Active", None)]),
        required=["Active", "Missing"],
        wait_minutes=2,
    )
    assert rc == 1
    assert sum(clock.sleeps) == 120
    assert "Active: still in_progress" in err
    assert f"Missing: never reported on SHA {SHA}" in err


def test_newest_check_run_and_app_slug_pinning(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    checks = [
        check_item(1, "Build", "failure", started_at="2026-11-21T12:00:00Z"),
        check_item(2, "Build", "success", started_at="2026-11-21T12:01:00Z"),
        check_item(3, "Build", "failure", app="other-app", started_at="2026-11-21T12:05:00Z"),
        check_item(4, "SameSecond", "failure", started_at="2026-11-21T12:00:00Z"),
        check_item(5, "SameSecond", "success", started_at="2026-11-21T12:00:00Z"),
        check_item(6, "CodeQL", "success", app="github-actions"),
        check_item(7, "CodeQL", "success", app="github-advanced-security"),
        check_item(8, "MissingApp", "success", app=None),
    ]
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks),
        required=["Build", "SameSecond", "CodeQL"],
        apps={"CodeQL": "github-advanced-security"},
    )
    assert rc == 0, err
    assert "Build (other-app, expected github-actions)" in out
    assert "CodeQL (github-actions, expected github-advanced-security)" in out

    rc_bad, _, err_bad, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(8, "CodeQL", "success", app=None)],
            statuses=[status_item(1, "CodeQL", "success")],
        ),
        required=["CodeQL"],
        apps={"CodeQL": "github-advanced-security"},
        wait_minutes=1,
    )
    assert rc_bad == 1
    assert "CodeQL: never reported" in err_bad


def test_foreign_app_logging_caps_at_twenty() -> None:
    names = [f"check-{i}" for i in range(22)]
    cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json=json.dumps(names),
    )
    checks = [check_item(i, name, "success", app="foreign") for i, name in enumerate(names, 1)]
    checks += [check_item(100 + i, name, "success") for i, name in enumerate(names, 1)]
    by_name, ignored = required_checks.newest_check_runs(
        checks, set(), cfg.apps, frozenset(cfg.required)
    )
    snap = required_checks.Snapshot(by_name, {}, ignored)
    out = io.StringIO()
    assert required_checks.judge_snapshot(snap, cfg, out) == []
    assert "Ignored 2 additional foreign-producer check names" in out.getvalue()


@pytest.mark.parametrize(
    ("event", "ref", "runs", "admitted_suites"),
    [
        (
            "push",
            "refs/heads/main",
            [
                wf_run(1, "push", suite_id=10, head_branch="main"),
                wf_run(2, "schedule", suite_id=20, head_branch="main"),
                wf_run(3, "pull_request", suite_id=30, head_branch="feat"),
                wf_run(4, "push", suite_id=40, head_branch="other-branch"),
            ],
            {10},
        ),
        (
            "pull_request",
            "refs/pull/5/merge",
            [
                wf_run(1, "pull_request", suite_id=10, head_branch="feat"),
                wf_run(2, "pull_request_target", suite_id=11, head_branch="main"),
                wf_run(3, "schedule", suite_id=20, head_branch="main"),
                wf_run(4, "push", suite_id=30, head_branch="feat"),
            ],
            {10, 11},
        ),
        (
            "merge_group",
            "refs/gh-readonly-queue/main/pr-5",
            [
                wf_run(1, "merge_group", suite_id=10, head_branch="gh-readonly-queue/main/pr-5"),
                wf_run(2, "pull_request", suite_id=20, head_branch="feat"),
                wf_run(3, "schedule", suite_id=30, head_branch="main"),
            ],
            {10},
        ),
        (
            "workflow_dispatch",
            "refs/heads/main",
            [
                wf_run(1, "push", suite_id=10, head_branch="main"),
                wf_run(2, "workflow_dispatch", suite_id=11, head_branch="main"),
                wf_run(3, "schedule", suite_id=20, head_branch="main"),
                wf_run(4, "push", suite_id=30, head_branch="other"),
            ],
            {10, 11},
        ),
    ],
)
def test_foreign_workflow_runs_excluded_across_events(
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    ref: str,
    runs: list[dict],
    admitted_suites: set[int],
) -> None:
    all_suites = {r["check_suite_id"] for r in runs}
    foreign = all_suites - admitted_suites
    checks = [
        check_item(s, "Gate", "success", suite_id=s, started_at="2026-11-21T12:00:00Z")
        for s in admitted_suites
    ] + [
        check_item(100 + s, "Gate", "failure", suite_id=s, started_at="2026-11-21T12:10:00Z")
        for s in foreign
    ]
    rc, _, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks, runs=runs),
        required=["Gate"],
        event=event,
        ref=ref,
    )
    assert rc == 0, err


def test_commit_statuses_3d(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = [
        status_item(1, "ext/ci", "pending", updated_at="2026-11-21T12:00:00Z"),
        status_item(2, "ext/ci", "failure", updated_at="2026-11-21T12:01:00Z"),
        status_item(3, "ext/ci", "success", updated_at="2026-11-21T12:02:00Z"),
    ]
    rc, out, err, _ = invoke_with_forge(
        tree, monkeypatch, base_answers(statuses=statuses), required=["ext/ci"]
    )
    assert rc == 0, err
    assert "OK (success): ext/ci" in out

    for bad_state in ("failure", "error", "pending"):
        rc_bad, _, err_bad, _ = invoke_with_forge(
            tree,
            monkeypatch,
            base_answers(statuses=[status_item(10, "ext/ci", bad_state)]),
            required=["ext/ci"],
            wait_minutes=1,
        )
        assert rc_bad == 1
        assert f"ext/ci: status {bad_state}" in err_bad

    rc_mixed, _, err_mixed, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Dual", "success")],
            statuses=[status_item(2, "Dual", "failure")],
        ),
        required=["Dual"],
    )
    assert rc_mixed == 1
    assert "Dual: status failure" in err_mixed


def test_pull_request_only_and_not_owed_pass_cases(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[
                check_item(1, "Build", "success"),
                check_item(2, "PR-Only", "skipped"),
                check_item(3, "Not-Owed-Skip", "skipped"),
            ]
        ),
        required=["Build", "PR-Only", "PR-Absent", "Not-Owed-Skip", "Not-Owed-Absent"],
        pull_request_only=["PR-Only", "PR-Absent"],
        not_owed=["Not-Owed-Skip", "Not-Owed-Absent"],
        event="push",
    )
    assert rc == 0, err
    assert clock.sleeps == []
    assert "OK (pull-request-only on push): PR-Only" in out
    assert "OK (not owed): Not-Owed-Absent" in out


def test_pull_request_only_on_pr_and_failed_exemptions_fail(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc_pr, _, err_pr, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "PR-Only", "skipped")]),
        required=["PR-Only"],
        pull_request_only=["PR-Only"],
        event="pull_request",
    )
    assert rc_pr == 1
    assert "PR-Only: skipped" in err_pr

    rc_fail, _, err_fail, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Build", "success"), check_item(2, "Optional", "failure")]
        ),
        required=["Build", "Optional"],
        not_owed=["Optional"],
    )
    assert rc_fail == 1
    assert "Optional: failure (ran although not owed)" in err_fail

    rc_sfail, _, err_sfail, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Build", "success")],
            statuses=[status_item(2, "Optional", "error")],
        ),
        required=["Build", "Optional"],
        not_owed=["Optional"],
    )
    assert rc_sfail == 1
    assert "Optional: status error (reported although not owed)" in err_sfail


def test_pull_request_target_sha_resolution_3d() -> None:
    cfg_sha1 = parse_config(
        repository=REPO,
        event_name="pull_request_target",
        ref="refs/heads/main",
        commit_sha="0" * 40,
        pr_head_sha=SHA,
        required_json='["Build"]',
    )
    assert cfg_sha1.sha == SHA

    cfg_sha256 = parse_config(
        repository=REPO,
        event_name="pull_request_target",
        commit_sha="0" * 40,
        pr_head_sha=f"  {SHA256}\n",
        required_json='["Build"]',
    )
    assert cfg_sha256.sha == SHA256

    cfg_explicit = parse_config(
        repository=REPO,
        event_name="pull_request_target",
        sha="f" * 40,
        commit_sha="0" * 40,
        pr_head_sha=SHA,
        required_json='["Build"]',
    )
    assert cfg_explicit.sha == "f" * 40

    with pytest.raises(InputError, match="sha: invalid commit SHA ''") as err_pr:
        parse_config(
            repository=REPO,
            event_name="pull_request",
            commit_sha=SHA,
            pr_head_sha="",
            required_json='["Build"]',
        )
    for empty_head in ("", "   "):
        with pytest.raises(InputError, match="sha: invalid commit SHA ''") as err_prt:
            parse_config(
                repository=REPO,
                event_name="pull_request_target",
                commit_sha=SHA,
                pr_head_sha=empty_head,
                required_json='["Build"]',
            )
        assert str(err_prt.value) == str(err_pr.value)


def test_pull_request_target_cli_and_pr_only_3d(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = base_answers(
        checks=[check_item(1, "Build", "success"), check_item(2, "PR-Only", "success")]
    )
    for name, value in tree.forge(answers).items():
        monkeypatch.setenv(name, value)
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["judge"],
        env={
            "REPOSITORY": REPO,
            "EVENT_NAME": "pull_request_target",
            "REF": "refs/heads/main",
            "COMMIT_SHA": "0" * 40,
            "PR_HEAD_SHA": SHA,
            "REQUIRED_JSON": '["Build", "PR-Only"]',
            "PULL_REQUEST_ONLY_JSON": '["PR-Only"]',
        },
        out=out,
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "OK (success): PR-Only" in out.getvalue()

    for pr_event in ("pull_request", "pull_request_target"):
        rc_abs, _, err_abs, _ = invoke_with_forge(
            tree,
            monkeypatch,
            base_answers(checks=[check_item(1, "Build", "success")]),
            required=["Build", "PR-Only"],
            pull_request_only=["PR-Only"],
            event=pr_event,
            wait_minutes=1,
        )
        assert rc_abs == 1
        assert f"PR-Only: never reported on SHA {SHA}" in err_abs

    rc_mg, out_mg, err_mg, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "success")]),
        required=["Build", "PR-Only"],
        pull_request_only=["PR-Only"],
        event="merge_group",
    )
    assert rc_mg == 0, err_mg
    assert "OK (pull-request-only on merge_group): PR-Only" in out_mg


# ---------------------------------------------------------------------------
# Backoff, rate limits, re-listing, adaptive polling, and forge `--include`
# ---------------------------------------------------------------------------


def test_transient_5xx_waits_up_to_15_minutes_then_fails(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    err_502 = {"http": 502, "raw_body": "<html>Bad Gateway</html>", "stderr": "gh: HTTP 502"}
    answers_recover = base_answers(
        checks={"sequence": [*([err_502] * 6), {"check_runs": [check_item(1, "Build", "success")]}]}
    )
    rc, _, err, clock = invoke_with_forge(tree, monkeypatch, answers_recover)
    assert rc == 0, err
    assert clock.sleeps == list(API_BACKOFF_SECONDS)
    assert sum(clock.sleeps) == 900

    answers_outage = base_answers(checks={"sequence": [err_502] * 7})
    rc_fail, out_fail, err_fail, clock_fail = invoke_with_forge(tree, monkeypatch, answers_outage)
    assert rc_fail == 1
    assert clock_fail.sleeps == list(API_BACKOFF_SECONDS)
    assert "Transient GitHub API failure:" in err_fail
    assert "Required checks did not pass" not in err_fail
    assert out_fail.count("::warning::") == 6

    rc_short, _, err_short, clock_short = invoke_with_forge(
        tree, monkeypatch, answers_outage, wait_minutes=1
    )
    assert rc_short == 1
    assert clock_short.sleeps == [30]
    assert "Transient GitHub API failure:" in err_short


def test_rate_limit_wait_modes_and_exponential_cap(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_epoch = 1_700_000_000
    seq_headers = [
        {"http": 429, "headers": {"Retry-After": "45"}, "body": {"message": "slow down"}},
        {
            "http": 403,
            "headers": {
                "X-Ratelimit-Remaining": "0",
                "X-Ratelimit-Reset": str(start_epoch + 45 + 20),
            },
            "body": {"message": "API rate limit exceeded"},
        },
        {"check_runs": [check_item(1, "Build", "success")]},
    ]
    rc, _, err, clock = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"sequence": seq_headers})
    )
    assert rc == 0, err
    assert clock.sleeps == [45, 25]

    secondary = {"http": 403, "headers": {}, "body": {"message": "You hit a secondary rate limit"}}
    seq_exp = [*([secondary] * 6), {"check_runs": [check_item(1, "Build", "success")]}]
    rc_exp, _, err_exp, clock_exp = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": seq_exp}),
        wait_minutes=90,
        rate_limit_wait_minutes=90,
    )
    assert rc_exp == 0, err_exp
    assert clock_exp.sleeps == [60, 120, 240, 480, 960, 960]


def test_rate_limit_budget_and_deadline_bounds(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    exact = [
        {"http": 429, "headers": {"Retry-After": "120"}, "body": {"message": "rate limit"}},
        {"check_runs": [check_item(1, "Build", "success")]},
    ]
    rc_exact, _, err_exact, clock_exact = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": exact}),
        wait_minutes=2,
        rate_limit_wait_minutes=2,
    )
    assert rc_exact == 0, err_exact
    assert clock_exact.sleeps == [120]

    over = [
        {"http": 429, "headers": {"Retry-After": "121"}, "body": {"message": "rate limit"}},
        {"check_runs": [check_item(1, "Build", "success")]},
    ]
    rc_over, _, err_over, clock_over = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": over}),
        wait_minutes=10,
        rate_limit_wait_minutes=2,
    )
    assert rc_over == 1 and clock_over.sleeps == []
    assert "GitHub API rate limit:" in err_over
    assert "Required checks did not pass" not in err_over

    rc_dl, _, err_dl, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": exact}),
        wait_minutes=1,
        rate_limit_wait_minutes=10,
    )
    assert rc_dl == 1 and "GitHub API rate limit:" in err_dl

    zero_rl = {"http": 429, "headers": {"Retry-After": "0"}, "body": {"message": "rate limit"}}
    rc_zero, _, err_zero, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": [zero_rl]}),
        rate_limit_wait_minutes=0,
    )
    assert rc_zero == 1 and "GitHub API rate limit:" in err_zero


def test_rate_limit_call_cap_and_permission_403(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rl_10 = {"http": 429, "headers": {"Retry-After": "10"}, "body": {"message": "rate limit"}}
    rc_six, _, err_six, clock_six = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks={
                "sequence": [*([rl_10] * 6), {"check_runs": [check_item(1, "Build", "success")]}]
            }
        ),
    )
    assert rc_six == 0, err_six
    assert clock_six.sleeps == [10] * 6

    rc_seven, _, err_seven, clock_seven = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"sequence": [rl_10] * 7})
    )
    assert rc_seven == 1 and clock_seven.sleeps == [10] * 6
    assert "GitHub API rate limit:" in err_seven

    perm_403 = {
        "http": 403,
        "headers": {"X-Ratelimit-Remaining": "4999"},
        "body": {"message": "Resource not accessible by integration"},
    }
    rc_perm, _, err_perm, clock_perm = invoke_with_forge(
        tree, monkeypatch, base_answers(checks=perm_403)
    )
    assert rc_perm == 1 and clock_perm.sleeps == []
    assert "Resource not accessible by integration" in err_perm


def test_relists_check_runs_after_rate_limit_wait_during_poll(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When statuses wait on a rate limit, check-runs are re-listed before judging."""
    answers = {
        CHECKS_URL: {
            "sequence": [
                {"check_runs": []},
                {"check_runs": [check_item(1, "Build", "success")]},
            ]
        },
        RUNS_URL: {"workflow_runs": []},
        STATUSES_URL: {
            "sequence": [
                {"http": 429, "headers": {"Retry-After": "60"}, "body": {"message": "rate limit"}},
                [],
            ]
        },
    }
    rc, _, err, clock = invoke_with_forge(
        tree, monkeypatch, answers, required=["Build"], wait_minutes=1, rate_limit_wait_minutes=5
    )
    assert rc == 0, err
    assert clock.sleeps == [60]
    assert tree.forge_calls().count(CHECKS_URL) == 2


def test_adaptive_polling_doubles_to_240_and_resets_to_30_on_change(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued_a = check_item(1, "A", None, status="queued")
    running_a = check_item(1, "A", None, status="in_progress")
    done_a = check_item(1, "A", "success")
    queued_b = check_item(2, "B", None, status="queued")
    done_b = check_item(2, "B", "success")

    seq = [
        {"check_runs": [queued_a, queued_b]},
        {"check_runs": [queued_a, queued_b]},
        {"check_runs": [queued_a, queued_b]},
        {"check_runs": [queued_a, queued_b]},
        {"check_runs": [queued_a, queued_b]},
        {"check_runs": [running_a, queued_b]},
        {"check_runs": [running_a, queued_b]},
        {"check_runs": [done_a, done_b]},
    ]
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks={"sequence": seq}),
        required=["A", "B", "C", "D", "E"],
        not_owed=["C", "D", "E"],
        wait_minutes=60,
    )
    assert rc == 0, err
    assert clock.sleeps == [30.0, 60.0, 120.0, 240.0, 240.0, 30.0, 60.0]
    assert "All owed required checks reported success." in out


def test_forge_request_parsing_and_cli_judge_end_to_end(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    redirect_payload = (
        "HTTP/1.1 302 Found\nLocation: https://api.github.com/next\n\n"
        'HTTP/2.0 200 OK\nX-Custom: Val\n\n{"ok": true}'
    )
    answers = {
        "repos/x/redirect": {"raw_include": redirect_payload, "exit": 0},
        "repos/x/no-http": {"raw_include": '{"ok": true}', "exit": 0},
        "repos/x/transport-503": {"fail": "gh: Service Unavailable (HTTP 503)"},
        CHECKS_URL: {"check_runs": [check_item(1, "Gate", "success")]},
        RUNS_URL: {"workflow_runs": []},
        STATUSES_URL: [],
    }
    for name, value in tree.forge(answers).items():
        monkeypatch.setenv(name, value)

    status, headers, body = forge.request("repos/x/redirect")
    assert status == 200 and headers == {"x-custom": "Val"}
    assert json.loads(body) == {"ok": True}

    with pytest.raises(forge.ForgeError, match="missing HTTP status"):
        forge.request("repos/x/no-http")

    with pytest.raises(forge.ResponseError) as caught:
        forge.request("repos/x/transport-503")
    assert caught.value.status == 503

    assert forge.rate_limit_delay(429, {"retry-after": "1.5"}, "", 0, 100.0) == 1.5
    assert forge.rate_limit_delay(429, {"retry-after": "bad"}, "rate limit", 1, 100.0) == 120
    assert forge.rate_limit_delay(404, {"retry-after": "10"}, "rate limit", 0, 100.0) is None

    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["judge"],
        env={
            "REPOSITORY": REPO,
            "EVENT_NAME": "pull_request",
            "REF": "refs/pull/1/merge",
            "PR_HEAD_SHA": SHA,
            "REQUIRED_JSON": '["Gate"]',
        },
        out=out,
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "OK (success): Gate" in out.getvalue()
    assert main(["self-test"], out=io.StringIO(), err=io.StringIO()) == 0


def test_boundary_edge_cases_for_parsers_and_snapshot(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert forge._parse_included(b"HTTP/2.0 200 OK\r\nNo-Blank-Line") is None
    assert forge._parse_included(b"HTTP/2.0 BAD\r\n\r\n{}") is None
    redirects_11 = b"HTTP/1.1 302 Found\r\n\r\n" * 11 + b'HTTP/2.0 200 OK\r\n\r\n{"ok":true}'
    assert forge._parse_included(redirects_11) is None
    status_no_colon = b'HTTP/2.0 200 OK\r\nBareHeaderLine\r\nX-A: 1\r\n\r\n{"ok":true}'
    assert forge._parse_included(status_no_colon) == (200, {"x-a": "1"}, b'{"ok":true}')
    assert forge._error_message(b"", "gh: HTTP 500") == "gh: HTTP 500"

    monkeypatch.setenv("GH", str(tree.root / "missing-gh-binary"))
    with pytest.raises(forge.ForgeError, match="GET repos/x/y"):
        forge.request("repos/x/y")

    over_apps = {f"c-{i}": "github-actions" for i in range(MAX_CONTEXTS + 1)}
    with pytest.raises(InputError, match="apps: 501 entries exceeds limit"):
        parse_config(
            repository=REPO,
            event_name="push",
            sha=SHA,
            required_json='["Build"]',
            apps_json=json.dumps(over_apps),
        )

    assert required_checks._iso_epoch("not-a-timestamp") == 0.0
    checks = [
        {"id": 9, "name": ""},
        check_item(5, "Build", "success", started_at="2026-11-21T12:10:00Z"),
        check_item(4, "Build", "failure", started_at="2026-11-21T12:00:00Z"),
    ]
    statuses = [
        {"id": 9, "context": ""},
        status_item(5, "Optional", "pending", updated_at="2026-11-21T12:10:00Z"),
        status_item(4, "Optional", "failure", updated_at="2026-11-21T12:00:00Z"),
    ]
    rc, _, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks, statuses=statuses),
        required=["Build", "Optional"],
        not_owed=["Optional"],
    )
    assert rc == 0, err

    # Non-JSON 200 body and non-list paged answer fail cleanly as API errors.
    rc_html, _, err_html, _ = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"raw": "<html>"})
    )
    assert rc_html == 1 and "GitHub API error" in err_html

    rc_shape, _, err_shape, _ = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"unexpected": 1})
    )
    assert rc_shape == 1 and "no list check_runs" in err_shape
