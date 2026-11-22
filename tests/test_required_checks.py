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
    BOUND_MINUTES_DEFAULT,
    FLOOR,
    MAX_CONTEXTS,
    MAX_TARGETS_DEFAULT,
    OPEN_PULLS_MAX,
    SESSION_DEADLINE_SECONDS,
    ApiFailureError,
    ApiSession,
    InputError,
    below_floor,
    decide,
    main,
    parse_config,
    run_judge,
    run_sweep,
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
    created_at: str = "2026-11-21T10:00:00Z",
    path: str = ".github/workflows/ci.yml",
) -> dict:
    """Build one workflow-run API dictionary."""
    return {
        "id": run_id,
        "event": event,
        "head_branch": head_branch,
        "check_suite_id": suite_id,
        "created_at": created_at,
        "path": path,
    }


COMMIT_URL = f"repos/{REPO}/commits/{SHA}"


def base_answers(
    checks: list[dict] | dict = (),
    runs: list[dict] | dict = (),
    statuses: list[dict] | dict = (),
) -> dict[str, object]:
    """Build stand-in forge endpoint answers for one commit."""
    run_list = runs if isinstance(runs, dict) else list(runs)
    if not run_list and not isinstance(runs, dict):
        run_list = []
    return {
        CHECKS_URL: checks if isinstance(checks, dict) else {"check_runs": list(checks)},
        RUNS_URL: runs if isinstance(runs, dict) else {"workflow_runs": run_list},
        STATUSES_URL: statuses if isinstance(statuses, dict) else list(statuses),
        COMMIT_URL: {
            "commit": {"committer": {"date": "2026-11-21T10:00:00Z"}},
            "sha": SHA,
        },
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
    bound_minutes: int | str = BOUND_MINUTES_DEFAULT,
    only_if_pending: bool | str = False,
    runs: list[dict] | None = None,
) -> tuple[int, str, str, Clock]:
    """Run `run_judge` through `forge.request` backed by `conftest.FAKE_CLIENT`."""
    answers = dict(answers)
    if runs is not None:
        answers[RUNS_URL] = {"workflow_runs": runs}
    else:
        block = answers.get(RUNS_URL)
        listed = block.get("workflow_runs") if isinstance(block, dict) else None
        if not listed:
            branch = "feat" if event in ("pull_request", "pull_request_target") else "main"
            answers[RUNS_URL] = {"workflow_runs": [wf_run(1, event=event, head_branch=branch)]}
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
        bound_minutes=bound_minutes,
        only_if_pending=only_if_pending,
    )
    clock = Clock(start=required_checks._iso_epoch("2026-11-21T12:00:00Z"))
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
    assert {"runner", "required", "pull-request-only", "not-owed", "apps"} <= set(inputs)
    assert inputs["runner"]["default"] == "ubuntu-26.04"
    assert inputs["runner"]["required"] is False
    assert inputs["required"]["required"] is True
    assert inputs["pull-request-only"]["default"] == "[]"
    assert inputs["not-owed"]["default"] == "[]"
    assert inputs["apps"]["default"] == "{}"


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
    assert len(lines) >= 16
    assert all(line.startswith("ok ") for line in lines)

    # Planted 1: accepting skipped on owed checks breaks self_test.
    monkeypatch.setattr(required_checks, "_owed_run_problem", lambda run: "")
    assert self_test(io.StringIO()) == 1
    monkeypatch.undo()

    # Planted 2: accepting any app slug breaks self_test.
    monkeypatch.setattr(
        required_checks,
        "_check_run_admitted",
        lambda run, suites, apps, self_context: (run.get("name"), None),
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
    monkeypatch.setattr(required_checks, "MAX_RATE_LIMIT_WAITS_PER_CALL", 0)
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
        bound_minutes=1,
    )
    assert cfg.sha == SHA256
    assert cfg.required == ("Build", "CodeQL", "Status")
    assert cfg.pull_request_only == frozenset({"CodeQL"})
    assert cfg.not_owed == frozenset({"Status"})
    assert cfg.apps == {"CodeQL": "github-advanced-security"}
    assert cfg.bound_minutes == 1

    max_names = [f"check-{i}" for i in range(MAX_CONTEXTS)]
    cfg_max = parse_config(
        repository=REPO,
        event_name="push",
        commit_sha=SHA,
        required_json=json.dumps(max_names),
        pull_request_only_json=json.dumps(max_names),
        not_owed_json=json.dumps(max_names),
        apps_json=json.dumps(dict.fromkeys(max_names, "github-actions")),
        bound_minutes="330",
    )
    assert len(cfg_max.required) == MAX_CONTEXTS
    assert cfg_max.bound_minutes == 330

    for bound in (1, 330):
        cfg_edge = parse_config(
            repository=REPO,
            event_name="push",
            sha=SHA,
            required_json='["Build"]',
            bound_minutes=bound,
        )
        assert cfg_edge.bound_minutes == bound


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
        ({"bound_minutes": 0}, "bound-minutes: 0 is out of range 1..330"),
        ({"bound_minutes": -1}, "bound-minutes: -1 is out of range 1..330"),
        ({"bound_minutes": 331}, "bound-minutes: 331 is out of range 1..330"),
        ({"bound_minutes": "1.5"}, "bound-minutes: must be a whole number"),
        ({"bound_minutes": "abc"}, "bound-minutes: must be a whole number"),
        ({"bound_minutes": True}, "bound-minutes: must be a whole number"),
        ({"max_targets": 0}, "max-targets: 0 is out of range 1..100"),
        ({"max_targets": 101}, "max-targets: 101 is out of range 1..100"),
        ({"only_if_pending": "yes"}, "only-if-pending: must be true or false"),
        ({"self_workflow": "../x.yml"}, "self-workflow:"),
        (
            {"required_json": '["required-checks"]'},
            "required: context 'required-checks' must not appear in required",
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
    assert rc == 0
    assert clock.sleeps == []
    assert f"Build: {conclusion}" in out
    assert "All owed required checks reported success" not in out


def test_absent_and_still_running_owed_checks_fail_after_bound(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, _, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Active", None)]),
        required=["Active", "Missing"],
        bound_minutes=1,
    )
    assert rc == 0
    assert clock.sleeps == []
    assert "deadline" in out


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

    rc_bad, out_bad, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(8, "CodeQL", "success", app=None)]),
        required=["CodeQL"],
        apps={"CodeQL": "github-advanced-security"},
        bound_minutes=1,
    )
    assert rc_bad == 0
    assert "CodeQL" in out_bad or "deadline" in out_bad


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
        checks, set(), cfg.apps, frozenset(cfg.required), cfg.context
    )
    snap = required_checks.Snapshot(by_name, {}, ignored, 0.0)
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

    for bad_state in ("failure", "error"):
        rc_bad, out_bad, _, _ = invoke_with_forge(
            tree,
            monkeypatch,
            base_answers(statuses=[status_item(10, "ext/ci", bad_state)]),
            required=["ext/ci"],
            bound_minutes=1,
        )
        assert rc_bad == 0
        assert f"ext/ci: status {bad_state}" in out_bad

    rc_mixed, out_mixed, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Dual", "success")],
            statuses=[status_item(2, "Dual", "failure")],
        ),
        required=["Dual"],
    )
    assert rc_mixed == 0
    assert "Dual: status failure" in out_mixed


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
    rc_pr, out_pr, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "PR-Only", "skipped")]),
        required=["PR-Only"],
        pull_request_only=["PR-Only"],
        event="pull_request",
    )
    assert rc_pr == 0
    assert "PR-Only: skipped" in out_pr

    rc_fail, out_fail, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Build", "success"), check_item(2, "Optional", "failure")]
        ),
        required=["Build", "Optional"],
        not_owed=["Optional"],
    )
    assert rc_fail == 0
    assert "Optional: failure (ran although not owed)" in out_fail

    rc_sfail, out_sfail, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Build", "success")],
            statuses=[status_item(2, "Optional", "error")],
        ),
        required=["Build", "Optional"],
        not_owed=["Optional"],
    )
    assert rc_sfail == 0
    assert "Optional: status error (reported although not owed)" in out_sfail


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
    forge_env = tree.forge(answers)
    for name, value in forge_env.items():
        monkeypatch.setenv(name, value)
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["judge"],
        env={
            **forge_env,
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
        rc_abs, out_abs, _, _ = invoke_with_forge(
            tree,
            monkeypatch,
            base_answers(checks=[check_item(1, "Build", "success")]),
            required=["Build", "PR-Only"],
            pull_request_only=["PR-Only"],
            event=pr_event,
            bound_minutes=1,
        )
        assert rc_abs == 0
        assert f"PR-Only: never reported on SHA {SHA}" in out_abs or "deadline" in out_abs

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


def test_transient_5xx_waits_at_most_three_backoffs_then_fails(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    err_502 = {"http": 502, "raw_body": "<html>Bad Gateway</html>", "stderr": "gh: HTTP 502"}
    answers_recover = base_answers(
        checks={"sequence": [*([err_502] * 3), {"check_runs": [check_item(1, "Build", "success")]}]}
    )
    rc, _, err, clock = invoke_with_forge(tree, monkeypatch, answers_recover)
    assert rc == 0, err
    assert clock.sleeps == list(API_BACKOFF_SECONDS)

    answers_outage = base_answers(checks={"sequence": [err_502] * 4})
    rc_fail, out_fail, err_fail, clock_fail = invoke_with_forge(tree, monkeypatch, answers_outage)
    assert rc_fail == 1
    assert clock_fail.sleeps == list(API_BACKOFF_SECONDS)
    assert "Transient GitHub API failure:" in err_fail
    assert "Required checks did not pass" not in err_fail
    assert out_fail.count("::warning::") == 3


def test_rate_limit_wait_modes_within_fixed_budget(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    start_epoch = 1_762_012_800
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
    assert clock.sleeps[:1] == [45]
    assert sum(clock.sleeps) <= 300


def test_rate_limit_budget_and_deadline_bounds(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    exact = [
        {"http": 429, "headers": {"Retry-After": "120"}, "body": {"message": "rate limit"}},
        {"check_runs": [check_item(1, "Build", "success")]},
    ]
    rc_exact, _, err_exact, clock_exact = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"sequence": exact})
    )
    assert rc_exact == 0, err_exact
    assert clock_exact.sleeps == [120]

    over = [
        {"http": 429, "headers": {"Retry-After": "301"}, "body": {"message": "rate limit"}},
        {"check_runs": [check_item(1, "Build", "success")]},
    ]
    rc_over, _, err_over, clock_over = invoke_with_forge(
        tree, monkeypatch, base_answers(checks={"sequence": over})
    )
    assert rc_over == 1 and clock_over.sleeps == []
    assert "GitHub API rate limit:" in err_over
    assert "Required checks did not pass" not in err_over


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


def test_one_shot_queued_checks_post_pending_without_sleep(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = [check_item(i, name, None, status="queued") for i, name in enumerate(["A", "B"], 1)]
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=queued),
        required=["A", "B"],
        bound_minutes=240,
    )
    assert rc == 0, err
    assert clock.sleeps == []
    assert "waiting on" in out or "Posted pending" in out
    assert any("statuses" in call for call in tree.forge_calls())


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
        RUNS_URL: {"workflow_runs": [wf_run(1, event="pull_request", head_branch="feat")]},
        STATUSES_URL: [],
        COMMIT_URL: {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}, "sha": SHA},
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
    with pytest.raises(ValueError, match="DELETE"):
        forge.request("repos/x/y", method="DELETE")

    assert forge.rate_limit_delay(429, {"retry-after": "1.5"}, "", 0, 100.0) == 1.5
    assert forge.rate_limit_delay(429, {"retry-after": "bad"}, "rate limit", 1, 100.0) == 120
    assert forge.rate_limit_delay(404, {"retry-after": "10"}, "rate limit", 0, 100.0) is None

    forge_env = tree.forge(answers)
    for name, value in forge_env.items():
        monkeypatch.setenv(name, value)
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["judge"],
        env={
            **forge_env,
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


def test_decide_direct_outcomes() -> None:
    anchor = required_checks._iso_epoch("2026-11-21T10:00:00Z")
    cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build","Other"]',
        bound_minutes=60,
    )

    def snap(checks: list[dict]) -> required_checks.Snapshot:
        by = {str(c["name"]): c for c in checks}
        return required_checks.Snapshot(by, {}, (), anchor)

    ok_state, _ = decide(
        snap([check_item(1, "Build", "success"), check_item(2, "Other", "success")]),
        cfg,
        anchor + 1,
        anchor,
    )
    assert ok_state == "success"
    fail_state, _ = decide(
        snap([check_item(1, "Build", "failure"), check_item(2, "Other", None)]),
        cfg,
        anchor + 1,
        anchor,
    )
    assert fail_state == "failure"
    pending_state, _ = decide(snap([check_item(1, "Other", None)]), cfg, anchor + 3600, anchor)
    assert pending_state == "pending"
    late_state, late_desc = decide(snap([check_item(1, "Other", None)]), cfg, anchor + 3601, anchor)
    assert late_state == "failure" and "deadline" in late_desc

    pr_cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build","PR Only"]',
        pull_request_only_json='["PR Only"]',
        bound_minutes=60,
    )
    pr_ok, _ = decide(snap([check_item(1, "Build", "success")]), pr_cfg, anchor + 1, anchor)
    assert pr_ok == "success"


def test_self_exclusion_and_context_input(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    self_suite = 99
    checks = [
        check_item(1, "required-checks", "failure", suite_id=self_suite),
        check_item(2, "Gate", "success", suite_id=10),
    ]
    runs = [wf_run(1, suite_id=self_suite, path=required_checks.DEFAULT_SELF_WORKFLOW)]
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks, runs=runs),
        required=["Gate"],
    )
    assert rc == 0, err
    assert "All owed required checks reported success." in out
    with pytest.raises(InputError, match="required:"):
        parse_config(
            repository=REPO,
            event_name="push",
            sha=SHA,
            required_json='["Build","required-checks"]',
        )


def test_status_posting_records_post_and_skips_churn(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = required_checks._iso_epoch("2026-11-21T10:00:00Z")
    cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build"]',
        bound_minutes=240,
    )
    snap = required_checks.Snapshot(
        {"Build": check_item(1, "Build", None)},
        {},
        (),
        anchor,
    )
    _, desc = decide(snap, cfg, required_checks._iso_epoch("2026-11-21T12:00:00Z"), anchor)
    pending_snapshot = base_answers(checks=[check_item(1, "Build", None)])
    rc1, _, _, _ = invoke_with_forge(
        tree, monkeypatch, pending_snapshot, required=["Build"], bound_minutes=240
    )
    assert rc1 == 0
    log = (tree.root / "forge.log").read_text(encoding="utf-8")
    assert "-X" in log and "POST" in log and "state=pending" in log
    pending_status = {
        "id": 1,
        "context": "required-checks",
        "state": "pending",
        "description": desc,
        "updated_at": "2026-11-21T12:01:00Z",
    }
    rc2, out2, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        {**pending_snapshot, STATUSES_URL: [pending_status]},
        required=["Build"],
        bound_minutes=240,
    )
    assert rc2 == 0
    assert "No status post" in out2


def test_only_if_pending_skips_check_runs_when_settled(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = [status_item(1, "required-checks", "success")]
    answers = base_answers(statuses=statuses)
    answers[CHECKS_URL] = {"fail": "check-runs should not be read"}
    rc, out, _, _ = invoke_with_forge(
        tree, monkeypatch, answers, required=["Build"], only_if_pending=True
    )
    assert rc == 0
    assert f"{SHA}: required-checks already settled" in out
    assert CHECKS_URL not in tree.forge_calls()


def test_target_url_https_only_and_http_omitted(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_https = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build"]',
        target_url="https://example.com/run/1",
    )
    assert cfg_https.target_url.startswith("https://")
    with pytest.raises(InputError, match="target-url"):
        parse_config(
            repository=REPO,
            event_name="push",
            sha=SHA,
            required_json='["Build"]',
            target_url="http://example.com",
        )
    rc, _, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "success")]),
        required=["Build"],
    )
    assert rc == 0
    log = (tree.root / "forge.log").read_text(encoding="utf-8")
    assert "target_url=http" not in log


def _statuses_get_count(calls: list[str]) -> int:
    return sum(1 for path in calls if "/commits/" in path and "/statuses" in path)


def test_one_statuses_read_per_evaluated_sha(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    rc, _, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "success")]),
        required=["Build"],
    )
    assert rc == 0, err
    assert _statuses_get_count(tree.forge_calls()) == 1


def test_parse_config_open_pulls_without_sha() -> None:
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
    )
    assert cfg.targets == "open-pulls"
    assert cfg.sha == ""


def test_sha_mode_still_requires_valid_sha() -> None:
    with pytest.raises(InputError, match="sha: invalid commit SHA ''"):
        parse_config(
            repository=REPO,
            event_name="push",
            commit_sha="",
            required_json='["Build"]',
            targets="sha",
        )


def test_failure_verdict_exits_zero_in_sha_mode(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "failure")]),
        required=["Build"],
    )
    assert rc == 0
    assert f"{SHA}:" in out
    assert "Build: failure" in out
    assert "Required checks did not pass" not in err
    assert _statuses_get_count(tree.forge_calls()) == 1
    assert "POST" in (tree.root / "forge.log").read_text(encoding="utf-8")


def test_decide_foreign_app_only_at_bound() -> None:
    anchor = required_checks._iso_epoch("2026-11-21T10:00:00Z")
    cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Scan"]',
        apps_json='{"Scan": "code-scanning"}',
        bound_minutes=60,
    )
    empty = required_checks.Snapshot({}, {}, (), anchor)
    pending_state, _ = decide(empty, cfg, anchor + 60 * 60, anchor)
    assert pending_state == "pending"
    fail_state, fail_desc = decide(empty, cfg, anchor + 60 * 60 + 1, anchor)
    assert fail_state == "failure" and "deadline" in fail_desc


def _sweep_transport_factory(
    heads: list[str],
    settled: set[str],
    *,
    record: list[tuple[str, str]],
) -> Any:
    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        record.append((method, path))
        if "/pulls?" in path:
            body = [{"head": {"sha": s}} for s in heads]
        elif path.endswith("/statuses") or "/statuses?" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            if sha in settled:
                body = [
                    status_item(1, "required-checks", "success", updated_at="2026-11-22T10:00:00Z")
                ]
            else:
                body = []
        elif "/check-runs" in path:
            body = {"check_runs": [check_item(1, "Build", "failure")]}
        elif "/actions/runs" in path:
            body = {"workflow_runs": [wf_run(1)]}
        elif path == f"repos/{REPO}":
            body = {"default_branch": "main"}
        elif path.endswith("/commits/main"):
            body = {"sha": "d" * 40}
        elif "/commits/" in path:
            body = {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}}
        else:
            body = {}
        if method == "POST":
            return 201, {}, json.dumps({"id": 1, "state": "failure"}).encode()
        return 200, {}, json.dumps(body).encode()

    return transport


def test_sweep_judges_all_unsettled_when_first_thirty_settled() -> None:
    heads = [f"{i:040x}" for i in range(1, 41)]
    settled = set(heads[:30])
    record: list[tuple[str, str]] = []
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=30,
    )
    rc = run_sweep(
        cfg,
        transport=_sweep_transport_factory(heads, settled, record=record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert rc == 0
    checked = [
        p.split("/commits/")[1].split("/")[0]
        for m, p in record
        if m == "GET" and "/check-runs" in p
    ]
    unsettled = [s for s in heads if s not in settled]
    assert all(s in checked for s in unsettled)
    assert _statuses_get_count([p for _, p in record]) == len(heads) + 1


def test_sweep_evaluates_never_judged_first_up_to_max_targets() -> None:
    heads = [f"{i:040x}" for i in range(1, 46)]
    record: list[tuple[str, str]] = []
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=30,
    )
    run_sweep(
        cfg,
        transport=_sweep_transport_factory(heads, set(), record=record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    full_evals = [p.split("/commits/")[1].split("/")[0] for m, p in record if "/check-runs" in p]
    assert len(full_evals) == MAX_TARGETS_DEFAULT + 1
    assert full_evals[:30] == heads[:30]


def test_sweep_caps_status_reads_and_evaluations_at_120_heads() -> None:
    heads = [f"{i:040x}" for i in range(1, 121)]
    record: list[tuple[str, str]] = []
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=MAX_TARGETS_DEFAULT,
    )
    run_sweep(
        cfg,
        transport=_sweep_transport_factory(heads, set(), record=record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    paths = [p for _, p in record]
    assert _statuses_get_count(paths) <= OPEN_PULLS_MAX + 1
    check_reads = sum(1 for p in paths if "/check-runs" in p)
    assert check_reads <= MAX_TARGETS_DEFAULT + 1


def test_api_session_call_deadline_matches_remaining_budget() -> None:
    start = 1_000_000.0
    captured: list[float] = []

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        deadline = kwargs.get("deadline")
        if isinstance(deadline, (int, float)):
            captured.append(float(deadline))
        return 200, {}, b'{"check_runs": []}'

    session = ApiSession(deadline=start + 20.0, now=lambda: start, transport=transport)
    session.request_json("check-runs", f"repos/{REPO}/commits/{SHA}/check-runs")
    assert captured == [20.0]


def test_api_session_zero_remaining_skips_transport() -> None:
    start = 2_000_000.0
    seen: list[int] = []

    def transport(**_: object) -> tuple[int, dict[str, str], bytes]:
        seen.append(1)
        return 200, {}, b"{}"

    session = ApiSession(deadline=start, now=lambda: start, transport=transport)
    with pytest.raises(ApiFailureError, match=r"Session deadline.*statuses"):
        session.request_json("statuses", f"repos/{REPO}/commits/{SHA}/statuses")
    assert seen == []


def test_sweep_worst_case_call_bounds_at_defaults() -> None:
    from required_checks import (  # noqa: PLC0415
        CHECK_RUN_MAX_PAGES,
        MAX_CALL_ATTEMPTS,
        MAX_TARGETS_DEFAULT,
        OPEN_PULLS_MAX,
        STATUS_MAX_PAGES,
        WORKFLOW_RUN_MAX_PAGES,
        format_sweep_worst_case_calls,
        sweep_worst_case_calls_one_page,
        sweep_worst_case_calls_page_bound,
    )

    assert sweep_worst_case_calls_one_page() == 227
    bound = sweep_worst_case_calls_page_bound()
    expected = (
        3
        + (OPEN_PULLS_MAX + 1) * STATUS_MAX_PAGES
        + (MAX_TARGETS_DEFAULT + 1) * (CHECK_RUN_MAX_PAGES + WORKFLOW_RUN_MAX_PAGES + 2)
    )
    assert bound == expected
    text = format_sweep_worst_case_calls()
    assert "227" in text.splitlines()[0]
    assert f"MAX_CALL_ATTEMPTS({MAX_CALL_ATTEMPTS})" in text
    assert str(bound * MAX_CALL_ATTEMPTS) in text.splitlines()[1]


def _mixed_sweep_transport(
    pull_order: list[str],
    status_by_sha: dict[str, dict],
    *,
    record: list[tuple[str, str]],
) -> Any:
    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        record.append((method, path))
        if "/pulls?" in path:
            body = [{"head": {"sha": s}} for s in pull_order]
        elif method == "POST" and "/statuses/" in path:
            return 201, {}, json.dumps({"id": 1}).encode()
        elif "/statuses" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            body = [status_by_sha[sha]] if sha in status_by_sha else []
        elif "/check-runs" in path:
            body = {"check_runs": [check_item(1, "Build", "success")]}
        elif "/actions/runs" in path:
            body = {"workflow_runs": [wf_run(1)]}
        elif path == f"repos/{REPO}":
            body = {"default_branch": "main"}
        elif path.endswith("/commits/main"):
            body = {"sha": "d" * 40}
        elif "/commits/" in path:
            body = {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}}
        else:
            body = {}
        return 200, {}, json.dumps(body).encode()

    return transport


def test_sweep_settled_heads_skip_eval_slots_and_log_once() -> None:
    ctx = "required-checks"
    settled_old = [
        status_item(1, ctx, "success", updated_at="2026-11-20T10:00:00Z"),
    ]
    settled_fail = [
        status_item(2, ctx, "failure", updated_at="2026-11-20T09:00:00Z"),
    ]
    pending_new = [
        status_item(3, ctx, "pending", updated_at="2026-11-22T12:00:00Z"),
    ]
    pending_mid = [
        status_item(4, ctx, "pending", updated_at="2026-11-22T11:00:00Z"),
    ]
    heads = [f"{i:040x}" for i in range(1, 7)]
    status_by_sha = {
        heads[0]: settled_old[0],
        heads[1]: settled_fail[0],
        heads[2]: pending_new[0],
        heads[3]: pending_mid[0],
    }
    record: list[tuple[str, str]] = []
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=4,
    )
    out = io.StringIO()
    rc = run_sweep(
        cfg,
        transport=_mixed_sweep_transport(heads, status_by_sha, record=record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0
    stdout = out.getvalue()
    assert stdout.count(f"{heads[0]}: {ctx} already settled") == 1
    assert stdout.count(f"{heads[1]}: {ctx} already settled") == 1
    settled_shas = {heads[0], heads[1]}
    check_shas = {
        p.split("/commits/")[1].split("/")[0]
        for m, p in record
        if m == "GET" and "/check-runs" in p
    }
    assert settled_shas.isdisjoint(check_shas)
    pending_shas = {heads[2], heads[3]}
    assert pending_shas.issubset(check_shas)
    assert {heads[4], heads[5]}.issubset(check_shas)


def test_sweep_pending_priority_orders_never_judged_before_oldest_judged() -> None:
    ctx = "required-checks"
    judged_newer = status_item(1, ctx, "pending", updated_at="2026-11-22T12:00:00Z")
    judged_older = status_item(2, ctx, "pending", updated_at="2026-11-22T11:00:00Z")
    judged_tie_a = status_item(3, ctx, "pending", updated_at="2026-11-22T10:00:00Z")
    judged_tie_b = status_item(4, ctx, "pending", updated_at="2026-11-22T10:00:00Z")
    heads = [f"{(i + 10):040x}" for i in range(1, 5)]
    pull_order = [heads[0], heads[1], heads[2], heads[3]]
    status_by_sha = {heads[0]: judged_newer, heads[1]: judged_older}
    record: list[tuple[str, str]] = []
    cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=3,
    )
    run_sweep(
        cfg,
        transport=_mixed_sweep_transport(pull_order, status_by_sha, record=record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    pull_checks = [
        p.split("/commits/")[1].split("/")[0]
        for m, p in record
        if m == "GET" and "/check-runs" in p and p.split("/commits/")[1].split("/")[0] in heads
    ]
    assert pull_checks == [heads[2], heads[3], heads[1]]
    assert heads[0] not in pull_checks

    tie_record: list[tuple[str, str]] = []
    tie_heads = [f"{(i + 20):040x}" for i in range(1, 3)]
    tie_status = {tie_heads[0]: judged_tie_a, tie_heads[1]: judged_tie_b}
    tie_cfg = parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=2,
    )
    run_sweep(
        tie_cfg,
        transport=_mixed_sweep_transport(tie_heads, tie_status, record=tie_record),
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    tie_checks = [
        p.split("/commits/")[1].split("/")[0]
        for m, p in tie_record
        if m == "GET" and "/check-runs" in p and p.split("/commits/")[1].split("/")[0] in tie_heads
    ]
    assert tie_checks == tie_heads


def test_session_deadline_stops_sweep_and_passes_bounded_deadline() -> None:
    heads = [f"{i:040x}" for i in range(1, 6)]
    deadlines: list[float] = []
    start = 1_000_000.0
    tick = [start]

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        deadline = kwargs.get("deadline")
        if isinstance(deadline, (int, float)):
            deadlines.append(deadline)
        tick[0] += 200.0
        if "/pulls?" in path:
            body = [{"head": {"sha": s}} for s in heads]
        elif "/statuses" in path:
            body = []
        elif "/check-runs" in path:
            body = {"check_runs": [check_item(1, "Build", "success")]}
        elif "/actions/runs" in path:
            body = {"workflow_runs": [wf_run(1)]}
        elif path == f"repos/{REPO}":
            body = {"default_branch": "main"}
        elif path.endswith("/commits/main"):
            body = {"sha": "d" * 40}
        else:
            body = {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}}
        return 200, {}, json.dumps(body).encode()

    def now() -> float:
        return tick[0]

    cfg = parse_config(
        repository=REPO, required_json='["Build"]', targets="open-pulls", max_targets=5
    )
    err = io.StringIO()
    rc = run_sweep(
        cfg, transport=transport, now=now, sleep=lambda _: None, out=io.StringIO(), err=err
    )
    assert rc == 1
    assert "Session deadline" in err.getvalue()
    assert deadlines and all(d <= SESSION_DEADLINE_SECONDS for d in deadlines)


def test_sweep_open_pulls_and_default_branch(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    pulls = [{"head": {"sha": SHA}}, {"head": {"sha": "c" * 40}}]
    answers = {
        f"repos/{REPO}/pulls?state=open&per_page=100": pulls,
        f"repos/{REPO}": {"default_branch": "main"},
        f"repos/{REPO}/commits/main": {"sha": "d" * 40},
        CHECKS_URL.replace(SHA, SHA): {"check_runs": [check_item(1, "Build", "success")]},
        CHECKS_URL.replace(SHA, "c" * 40): {"check_runs": [check_item(2, "Build", "success")]},
        CHECKS_URL.replace(SHA, "d" * 40): {"check_runs": [check_item(3, "Build", "success")]},
        RUNS_URL: {"workflow_runs": [wf_run(1)]},
        RUNS_URL.replace(SHA, "c" * 40): {"workflow_runs": [wf_run(2)]},
        RUNS_URL.replace(SHA, "d" * 40): {"workflow_runs": [wf_run(3)]},
        STATUSES_URL: [],
        STATUSES_URL.replace(SHA, "c" * 40): [],
        STATUSES_URL.replace(SHA, "d" * 40): [],
        COMMIT_URL: {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}},
    }
    for path in (COMMIT_URL, COMMIT_URL.replace(SHA, "c" * 40), COMMIT_URL.replace(SHA, "d" * 40)):
        answers.setdefault(path, {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}})
    cfg = parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build"]',
        targets="open-pulls",
        max_targets=1,
    )
    for name, value in tree.forge(answers).items():
        monkeypatch.setenv(name, value)
    rc = run_judge(
        cfg,
        now=Clock(start=required_checks._iso_epoch("2026-11-21T12:00:00Z")).now,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert rc == 0
    assert len([c for c in tree.forge_calls() if "/pulls?" in c]) == 1


def test_forge_post_error_names_method(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_post(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args, 1, b"HTTP/2.0 502 Bad Gateway\r\n\r\n", b"gh: HTTP 502"
        )

    monkeypatch.setattr(forge, "_gh_run", fail_post)
    with pytest.raises(forge.ResponseError, match="POST"):
        forge.request(
            "repos/x/statuses/sha", method="POST", fields={"state": "success", "context": "x"}
        )


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
