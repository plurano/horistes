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
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
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
CHECKS_URL = f"repos/{REPO}/commits/{SHA}/check-runs?filter=all&per_page=100&page=1"
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


def _parse_config(
    *, repository: str = REPO, required_json: str = '["Build"]', **raw: Any
) -> required_checks.Config:
    return parse_config(
        required_checks.RawInputs(repository=repository, required_json=required_json, **raw)
    )


@dataclass(frozen=True)
class JudgeOptions:
    required: list[str] | tuple[str, ...] | str = ("Build",)
    event: str = "push"
    ref: str = "refs/heads/main"
    pull_request_only: list[str] | tuple[str, ...] | str = ()
    not_owed: list[str] | tuple[str, ...] | str = ()
    apps: dict[str, str] | tuple[()] | str = ()
    bound_minutes: int | str = BOUND_MINUTES_DEFAULT
    only_if_pending: bool | str = False
    runs: list[dict] | None = None


def _fixture_json(value: list[str] | tuple[str, ...] | str) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def invoke_with_forge(
    tree: Tree,
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, object],
    *,
    options: JudgeOptions | None = None,
) -> tuple[int, str, str, Clock]:
    """Run `run_judge` through `forge.request` backed by `conftest.FAKE_CLIENT`."""
    if options is None:
        options = JudgeOptions()
    answers = dict(answers)
    if options.runs is not None:
        answers[RUNS_URL] = {"workflow_runs": options.runs}
    else:
        block = answers.get(RUNS_URL)
        listed = block.get("workflow_runs") if isinstance(block, dict) else None
        if not listed:
            branch = "feat" if options.event in ("pull_request", "pull_request_target") else "main"
            answers[RUNS_URL] = {
                "workflow_runs": [wf_run(1, event=options.event, head_branch=branch)]
            }
    for name, value in tree.forge(answers).items():
        monkeypatch.setenv(name, value)
    cfg = _parse_config(
        repository=REPO,
        event_name=options.event,
        ref=options.ref,
        sha=SHA,
        required_json=_fixture_json(options.required),
        pull_request_only_json=_fixture_json(options.pull_request_only),
        not_owed_json=_fixture_json(options.not_owed),
        apps_json=options.apps if isinstance(options.apps, str) else json.dumps(dict(options.apps)),
        bound_minutes=options.bound_minutes,
        only_if_pending=options.only_if_pending,
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
    assert "wait-minutes" not in inputs
    assert "rate-limit-wait-minutes" not in inputs


def test_workflow_job_timeout_steps_and_caller_template_contract() -> None:
    doc = ciwf.load(WORKFLOW_PATH)
    job = doc["jobs"]["required-checks"]
    assert job["name"] == "Required checks"
    assert job["runs-on"] == "${{ inputs.runner }}"
    assert job["timeout-minutes"] == 10
    assert job["permissions"] == {
        "contents": "read",
        "checks": "read",
        "actions": "read",
        "statuses": "write",
    }

    steps = job["steps"]
    assert steps[0]["if"] == "github.event.pull_request.draft == true"
    assert steps[1]["uses"] == CHECKOUT.split()[0]
    assert steps[1]["with"]["repository"] == "${{ job.workflow_repository }}"
    assert steps[1]["with"]["ref"] == "${{ job.workflow_sha }}"
    assert steps[1]["with"]["persist-credentials"] is False
    assert "self-test" in steps[2]["run"]
    assert "judge" in steps[3]["run"]

    sweep = ciwf.load(WORKFLOW_PATH.with_name("required-checks-sweep.yml"))
    assert (
        sweep["jobs"]["required-checks"]["steps"][-1]["env"]["REQUIRED_CHECKS_RUN_NUMBER"]
        == "${{ github.run_number }}"
    )

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
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-13 category D: fixed self-test argv
    done_self = subprocess.run(cmd_self, capture_output=True, text=True, check=False, timeout=30)
    assert done_self.returncode == 0, done_self.stdout + done_self.stderr
    assert all(line.startswith("ok ") for line in done_self.stdout.splitlines())

    fixture_tree = Tree(tmp_path / "forge-standin")
    env = fixture_tree.forge(base_answers(checks=[check_item(1, "Gate")], runs=[wf_run(1)]))
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
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-13 category D: fixed judge argv
    done_judge = subprocess.run(
        cmd_judge, capture_output=True, text=True, check=False, timeout=30, env=run_env
    )
    assert done_judge.returncode == 0, done_judge.stdout + done_judge.stderr
    assert "OK (success): Gate" in done_judge.stdout

    (isolated / "tools/results.py").unlink()
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-13 category D: fixed self-test argv
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
    cfg = _parse_config(
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
    cfg_max = _parse_config(
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
        cfg_edge = _parse_config(
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
        _parse_config(**kwargs)
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
        options=JudgeOptions(required=["Active", "Missing"], bound_minutes=1),
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
        options=JudgeOptions(
            required=["Build", "SameSecond", "CodeQL"], apps={"CodeQL": "github-advanced-security"}
        ),
    )
    assert rc == 0, err
    assert "Build (other-app, expected github-actions)" in out
    assert "CodeQL (github-actions, expected github-advanced-security)" in out

    rc_bad, out_bad, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(8, "CodeQL", "success", app=None)]),
        options=JudgeOptions(
            required=["CodeQL"], apps={"CodeQL": "github-advanced-security"}, bound_minutes=1
        ),
    )
    assert rc_bad == 0
    assert "CodeQL" in out_bad or "deadline" in out_bad


def test_foreign_app_logging_caps_at_twenty() -> None:
    names = [f"check-{i}" for i in range(22)]
    cfg = _parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json=json.dumps(names),
    )
    checks = [check_item(i, name, "success", app="foreign") for i, name in enumerate(names, 1)]
    checks += [check_item(100 + i, name, "success") for i, name in enumerate(names, 1)]
    by_name, ignored = required_checks.newest_check_runs(
        checks, {1}, cfg.apps, frozenset(cfg.required), cfg.context
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
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks, runs=runs),
        options=JudgeOptions(required=["Gate"], event=event, ref=ref),
    )
    assert rc == 0, err
    assert f"Posted success for required-checks on {SHA}" in out


def test_commit_statuses_3d(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses = [
        status_item(1, "ext/ci", "pending", updated_at="2026-11-21T12:00:00Z"),
        status_item(2, "ext/ci", "failure", updated_at="2026-11-21T12:01:00Z"),
        status_item(3, "ext/ci", "success", updated_at="2026-11-21T12:02:00Z"),
    ]
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(statuses=statuses),
        options=JudgeOptions(required=["ext/ci"]),
    )
    assert rc == 0, err
    assert "OK (success): ext/ci" in out

    for bad_state in ("failure", "error"):
        rc_bad, out_bad, _, _ = invoke_with_forge(
            tree,
            monkeypatch,
            base_answers(statuses=[status_item(10, "ext/ci", bad_state)]),
            options=JudgeOptions(required=["ext/ci"], bound_minutes=1),
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
        options=JudgeOptions(required=["Dual"]),
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
        options=JudgeOptions(
            required=["Build", "PR-Only", "PR-Absent", "Not-Owed-Skip", "Not-Owed-Absent"],
            pull_request_only=["PR-Only", "PR-Absent"],
            not_owed=["Not-Owed-Skip", "Not-Owed-Absent"],
            event="push",
        ),
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
        options=JudgeOptions(
            required=["PR-Only"], pull_request_only=["PR-Only"], event="pull_request"
        ),
    )
    assert rc_pr == 0
    assert "PR-Only: skipped" in out_pr

    rc_fail, out_fail, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(
            checks=[check_item(1, "Build", "success"), check_item(2, "Optional", "failure")]
        ),
        options=JudgeOptions(required=["Build", "Optional"], not_owed=["Optional"]),
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
        options=JudgeOptions(required=["Build", "Optional"], not_owed=["Optional"]),
    )
    assert rc_sfail == 0
    assert "Optional: status error (reported although not owed)" in out_sfail


def test_pull_request_target_sha_resolution_3d() -> None:
    cfg_sha1 = _parse_config(
        repository=REPO,
        event_name="pull_request_target",
        ref="refs/heads/main",
        commit_sha="0" * 40,
        pr_head_sha=SHA,
        required_json='["Build"]',
    )
    assert cfg_sha1.sha == SHA

    cfg_sha256 = _parse_config(
        repository=REPO,
        event_name="pull_request_target",
        commit_sha="0" * 40,
        pr_head_sha=f"  {SHA256}\n",
        required_json='["Build"]',
    )
    assert cfg_sha256.sha == SHA256

    cfg_explicit = _parse_config(
        repository=REPO,
        event_name="pull_request_target",
        sha="f" * 40,
        commit_sha="0" * 40,
        pr_head_sha=SHA,
        required_json='["Build"]',
    )
    assert cfg_explicit.sha == "f" * 40

    with pytest.raises(InputError, match="sha: invalid commit SHA ''") as err_pr:
        _parse_config(
            repository=REPO,
            event_name="pull_request",
            commit_sha=SHA,
            pr_head_sha="",
            required_json='["Build"]',
        )
    for empty_head in ("", "   "):
        with pytest.raises(InputError, match="sha: invalid commit SHA ''") as err_prt:
            _parse_config(
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
        checks=[check_item(1, "Build", "success"), check_item(2, "PR-Only", "success")],
        runs=[wf_run(1, "pull_request_target")],
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
            options=JudgeOptions(
                required=["Build", "PR-Only"],
                pull_request_only=["PR-Only"],
                event=pr_event,
                bound_minutes=1,
            ),
        )
        assert rc_abs == 0
        assert f"PR-Only: never reported on SHA {SHA}" in out_abs or "deadline" in out_abs

    rc_mg, out_mg, err_mg, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "success")]),
        options=JudgeOptions(
            required=["Build", "PR-Only"],
            pull_request_only=["PR-Only"],
            event="merge_group",
            bound_minutes=1,
        ),
    )
    assert rc_mg == 0, err_mg
    assert "deadline of 1 min passed; waiting on: PR-Only" in out_mg


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
    start_epoch = int(required_checks._iso_epoch("2026-11-21T12:00:00Z"))
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
    answers = {
        **base_answers(checks=queued),
        STATUSES_URL: [status_item(1, "required-checks", "success")],
    }
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        answers,
        options=JudgeOptions(required=["A", "B"], bound_minutes=240),
    )
    assert rc == 0, err
    assert clock.sleeps == []
    assert f"Posted pending for required-checks on {SHA}" in out
    target = f"repos/{REPO}/statuses/{SHA}"
    assert target in tree.forge_calls()
    calls = (tree.root / "forge.log").read_text(encoding="utf-8").splitlines()
    assert any("POST" in line and target in line and "state=pending" in line for line in calls)


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
    cfg = _parse_config(
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

    pr_cfg = _parse_config(
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
    runs = [
        wf_run(1, suite_id=self_suite, path=required_checks.DEFAULT_SELF_WORKFLOW),
        wf_run(2, suite_id=10),
    ]
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=checks, runs=runs),
        options=JudgeOptions(required=["Gate"]),
    )
    assert rc == 0, err
    assert "All owed required checks reported success." in out
    with pytest.raises(InputError, match="required:"):
        _parse_config(
            repository=REPO,
            event_name="push",
            sha=SHA,
            required_json='["Build","required-checks"]',
        )


def test_status_posting_records_post_and_skips_churn(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = required_checks._iso_epoch("2026-11-21T10:00:00Z")
    cfg = _parse_config(
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
    prior_success = [status_item(0, "required-checks", "success")]
    pending_snapshot = {
        **base_answers(checks=[check_item(1, "Build", None)]),
        STATUSES_URL: prior_success,
    }
    rc1, _, _, _ = invoke_with_forge(
        tree,
        monkeypatch,
        pending_snapshot,
        options=JudgeOptions(required=["Build"], bound_minutes=240),
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
        options=JudgeOptions(required=["Build"], bound_minutes=240),
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
        tree, monkeypatch, answers, options=JudgeOptions(required=["Build"], only_if_pending=True)
    )
    assert rc == 0
    assert f"{SHA}: required-checks already settled" in out
    assert CHECKS_URL not in tree.forge_calls()


def test_target_url_https_only_and_http_omitted(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_https = _parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build"]',
        target_url="https://example.com/run/1",
    )
    assert cfg_https.target_url.startswith("https://")
    with pytest.raises(InputError, match="target-url"):
        _parse_config(
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
        options=JudgeOptions(required=["Build"]),
    )
    assert rc == 0
    log = (tree.root / "forge.log").read_text(encoding="utf-8")
    assert "target_url=http" not in log


def _statuses_get_count(calls: list[str]) -> int:
    return sum(1 for path in calls if "/commits/" in path and "/statuses" in path)


def test_statuses_read_at_snapshot_and_around_post(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, _, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        base_answers(checks=[check_item(1, "Build", "success")]),
        options=JudgeOptions(required=["Build"]),
    )
    assert rc == 0, err
    assert _statuses_get_count(tree.forge_calls()) == 3


def test_parse_config_open_pulls_without_sha() -> None:
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
    )
    assert cfg.targets == "open-pulls"
    assert cfg.sha == ""


def test_parse_config_helper_defaults_to_test_repository_and_build() -> None:
    cfg = _parse_config(sha=SHA, event_name="push")
    assert cfg.repository == REPO
    assert cfg.required == ("Build",)


@pytest.mark.parametrize("value", [None, "0", "-1", "1000000001", "abc", " 1 ", "1000000000"])
def test_sweep_run_number_validation(value: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    judged: list[required_checks.Config] = []

    def judge(config: required_checks.Config, **_: object) -> int:
        judged.append(config)
        return 0

    monkeypatch.setattr(required_checks, "run_judge", judge)
    env = {"REPOSITORY": REPO, "REQUIRED_JSON": '["Build"]'}
    if value is not None:
        env["REQUIRED_CHECKS_RUN_NUMBER"] = value
    err = io.StringIO()
    rc = main(["judge", "--targets", "open-pulls"], env=env, out=io.StringIO(), err=err)
    if value in (" 1 ", "1000000000"):
        assert rc == 0, err.getvalue()
        assert [config.run_number for config in judged] == [int(value)]
    else:
        assert rc == 2
        assert "run-number:" in err.getvalue()
        assert not judged


def test_cli_run_number_overrides_environment_and_sha_mode_ignores_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    judged: list[int] = []

    def judge(config: required_checks.Config, **_: object) -> int:
        judged.append(config.run_number)
        return 0

    monkeypatch.setattr(required_checks, "run_judge", judge)
    env = {"REPOSITORY": REPO, "REQUIRED_JSON": '["Build"]', "REQUIRED_CHECKS_RUN_NUMBER": "abc"}
    assert main(["judge", "--targets", "open-pulls", "--run-number", "2"], env=env) == 0
    assert main(["judge", "--event", "push", "--sha", SHA], env=env) == 0
    assert judged == [2, 0]


def test_sha_mode_still_requires_valid_sha() -> None:
    with pytest.raises(InputError, match="sha: invalid commit SHA ''"):
        _parse_config(
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
        options=JudgeOptions(required=["Build"]),
    )
    assert rc == 0
    assert f"{SHA}:" in out
    assert "Build: failure" in out
    assert "Required checks did not pass" not in err
    assert _statuses_get_count(tree.forge_calls()) == 3
    assert "POST" in (tree.root / "forge.log").read_text(encoding="utf-8")


def test_decide_foreign_app_only_at_bound() -> None:
    anchor = required_checks._iso_epoch("2026-11-21T10:00:00Z")
    cfg = _parse_config(
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


def _sweep_response_body(path: str, heads: list[str], settled: set[str]) -> object:
    if "/pulls?" in path:
        return [{"head": {"sha": sha}} for sha in heads]
    if path.endswith("/statuses") or "/statuses?" in path:
        sha = path.split("/commits/")[1].split("/")[0]
        if sha in settled:
            return [status_item(1, "required-checks", "success", updated_at="2026-11-22T10:00:00Z")]
        return []
    if "/check-runs" in path:
        return {"check_runs": [check_item(1, "Build", "failure")]}
    if "/actions/runs" in path:
        event = "push" if f"head_sha={'d' * 40}&" in path else "pull_request"
        return {"workflow_runs": [wf_run(1, event=event)]}
    if path == f"repos/{REPO}":
        return {"default_branch": "main"}
    if path.endswith("/commits/main"):
        return {"sha": "d" * 40}
    if "/commits/" in path:
        return {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}}
    return {}


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
        body = _sweep_response_body(path, heads, settled)
        if method == "POST":
            return 201, {}, json.dumps({"id": 1, "state": "failure"}).encode()
        return 200, {}, json.dumps(body).encode()

    return transport


def test_sweep_judges_all_unsettled_when_first_thirty_settled() -> None:
    heads = [f"{i:040x}" for i in range(1, 41)]
    settled = set(heads[:30])
    record: list[tuple[str, str]] = []
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
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
    assert _statuses_get_count([p for _, p in record]) == len(heads) + 3 * (len(unsettled) + 1)


def test_sweep_evaluates_never_judged_first_up_to_max_targets() -> None:
    heads = [f"{i:040x}" for i in range(1, 46)]
    record: list[tuple[str, str]] = []
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
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
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
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
    assert _statuses_get_count(paths) <= OPEN_PULLS_MAX + 3 * (MAX_TARGETS_DEFAULT + 1)
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


def _classify_sweep_call(method: str, path: str) -> str:
    if method == "POST" and "/statuses/" in path:
        return "post"
    if "/pulls?" in path:
        return "open-pulls"
    if path == f"repos/{REPO}":
        return "repository"
    if path.endswith("/commits/main"):
        return "default-commit"
    if "/statuses" in path:
        return "statuses"
    if "/check-runs" in path:
        return "check-runs"
    if "/actions/runs" in path:
        return "workflow-runs"
    if "/commits/" in path:
        return "commit"
    return "other"


SWEEP_CALL_KINDS = (
    "open-pulls",
    "repository",
    "default-commit",
    "statuses",
    "check-runs",
    "workflow-runs",
    "commit",
    "post",
    "other",
)
SWEEP_FIXED_BODIES: dict[str, object] = {
    "repository": {"default_branch": "main"},
    "default-commit": {"sha": "d" * 40},
    "commit": {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}},
}


def _sweep_page_rows(path: str, page_bound: bool, last: int, rows: list[Any]) -> list[Any] | None:
    """Full pages below the page budget, an empty page at it; None when one short page is read."""
    if not page_bound:
        return None
    match = re.search(r"[?&]page=(\d+)", path)
    assert match is not None, path
    return rows if int(match.group(1)) < last else []


def _sweep_body(kind: str, path: str, heads: list[str], page_bound: bool) -> object:
    if kind == "open-pulls":
        return [{"head": {"sha": s}} for s in heads]
    if kind == "statuses":
        full = [
            status_item(1, "required-checks", "pending", updated_at="2000-01-01T00:00:00Z"),
            *(status_item(i, "ext", "pending") for i in range(2, forge.PER_PAGE + 1)),
        ]
        rows = _sweep_page_rows(path, page_bound, required_checks.STATUS_MAX_PAGES, full)
        return [] if rows is None else rows
    if kind == "check-runs":
        full = [check_item(i, "Build", "success", app="scan-app") for i in range(forge.PER_PAGE)]
        rows = _sweep_page_rows(path, page_bound, required_checks.CHECK_RUN_MAX_PAGES, full)
        return {"check_runs": [check_item(1, "Build", app="scan-app")] if rows is None else rows}
    if kind == "workflow-runs":
        full = [wf_run(i, path="other.yml", created_at="") for i in range(1, forge.PER_PAGE + 1)]
        rows = _sweep_page_rows(path, page_bound, required_checks.WORKFLOW_RUN_MAX_PAGES, full)
        return {"workflow_runs": [] if rows is None else rows}
    return SWEEP_FIXED_BODIES.get(kind, {})


def _counting_sweep_transport(
    heads: list[str],
    *,
    page_bound: bool,
    refresh_clock: Clock | None = None,
) -> tuple[Any, dict[str, int]]:
    """Stub forge for run_sweep that counts every call per kind, unknown calls under "other"."""
    counts = dict.fromkeys(SWEEP_CALL_KINDS, 0)
    statuses_per_path: dict[str, int] = {}

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        kind = _classify_sweep_call(method, path)
        counts[kind] += 1
        if kind == "statuses" and refresh_clock is not None:
            statuses_per_path[path] = statuses_per_path.get(path, 0) + 1
            prepost = 2 if f"/commits/{SWEEP_FIXED_BODIES['default-commit']['sha']}/" in path else 3
            if statuses_per_path[path] == prepost:
                refresh_clock.current += required_checks.POST_FRESH_SECONDS + 1
        if kind == "post":
            return 201, {}, json.dumps({"id": 1, "state": "pending"}).encode()
        return 200, {}, json.dumps(_sweep_body(kind, path, heads, page_bound)).encode()

    return transport, counts


def _sweep_count_config() -> required_checks.Config:
    return _parse_config(
        repository=REPO,
        required_json='["Build"]',
        apps_json='{"Build":"scan-app"}',
        targets="open-pulls",
        run_number=1,
    )


@pytest.mark.parametrize("page_bound", [False, True])
def test_sweep_transport_call_counts_measured_at_defaults(page_bound: bool) -> None:
    heads = [f"{i:040x}" for i in range(1, OPEN_PULLS_MAX + 1)]
    transport, counts = _counting_sweep_transport(heads, page_bound=page_bound)
    err = io.StringIO()
    rc = run_sweep(
        _sweep_count_config(),
        transport=transport,
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "::error::" not in err.getvalue()
    assert "Sweep API call cap" in err.getvalue()
    assert counts["other"] == 0
    assert sum(counts.values()) == required_checks.SWEEP_MAX_CALLS
    assert counts["open-pulls"] == 1
    if page_bound:
        assert counts["statuses"] == required_checks.SWEEP_MAX_CALLS - 1
        assert counts["post"] == 0
    else:
        assert counts["check-runs"] == 21
        assert counts["post"] == 21


@pytest.mark.parametrize(
    ("page_bound", "status_pages", "expected_total"), [(False, 1, 320), (True, 20, 3677)]
)
def test_sweep_capless_call_counts(
    page_bound: bool, status_pages: int, expected_total: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 10_000)
    heads = [f"{i:040x}" for i in range(1, OPEN_PULLS_MAX + 1)]
    transport, counts = _counting_sweep_transport(heads, page_bound=page_bound)
    err = io.StringIO()
    rc = run_sweep(
        _sweep_count_config(),
        transport=transport,
        now=lambda: 1.0e9,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert err.getvalue() == ""
    assert sum(counts.values()) == expected_total
    eval_heads = MAX_TARGETS_DEFAULT + 1
    assert counts["statuses"] == (OPEN_PULLS_MAX + eval_heads) * status_pages + 2 * eval_heads
    assert counts["post"] == counts["commit"] == eval_heads


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
            event = "push" if f"head_sha={'d' * 40}&" in path else "pull_request"
            body = {"workflow_runs": [wf_run(1, event=event)]}
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
    start = required_checks._iso_epoch("2026-11-23T12:00:00Z")
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
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
        max_targets=4,
    )
    out = io.StringIO()
    rc = run_sweep(
        cfg,
        transport=_mixed_sweep_transport(heads, status_by_sha, record=record),
        now=lambda: start,
        sleep=lambda _: None,
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0
    stdout = out.getvalue()
    assert stdout.count(f"{heads[0]}: {ctx} already settled") == 1
    assert f"{heads[1]}: {ctx} already settled" not in stdout
    settled_shas = {heads[0]}
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
    start = required_checks._iso_epoch("2026-11-23T12:00:00Z")
    ctx = "required-checks"
    judged_newer = status_item(1, ctx, "pending", updated_at="2026-11-22T12:00:00Z")
    judged_older = status_item(2, ctx, "pending", updated_at="2026-11-22T11:00:00Z")
    judged_tie_a = status_item(3, ctx, "pending", updated_at="2026-11-22T10:00:00Z")
    judged_tie_b = status_item(4, ctx, "pending", updated_at="2026-11-22T10:00:00Z")
    heads = [f"{(i + 10):040x}" for i in range(1, 5)]
    pull_order = [heads[0], heads[1], heads[2], heads[3]]
    status_by_sha = {heads[0]: judged_newer, heads[1]: judged_older}
    record: list[tuple[str, str]] = []
    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
        max_targets=3,
    )
    run_sweep(
        cfg,
        transport=_mixed_sweep_transport(pull_order, status_by_sha, record=record),
        now=lambda: start,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    pull_checks = [
        p.split("/commits/")[1].split("/")[0]
        for m, p in record
        if m == "GET" and "/check-runs" in p and p.split("/commits/")[1].split("/")[0] in heads
    ]
    never_judged = [heads[2], heads[3]]
    assert pull_checks == never_judged + [heads[1]]
    assert heads[0] not in pull_checks

    tie_record: list[tuple[str, str]] = []
    tie_heads = [f"{(i + 20):040x}" for i in range(1, 3)]
    tie_status = {tie_heads[0]: judged_tie_a, tie_heads[1]: judged_tie_b}
    tie_cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
        max_targets=2,
    )
    run_sweep(
        tie_cfg,
        transport=_mixed_sweep_transport(tie_heads, tie_status, record=tie_record),
        now=lambda: start,
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
            event = "push" if f"head_sha={'d' * 40}&" in path else "pull_request"
            body = {"workflow_runs": [wf_run(1, event=event)]}
        elif path == f"repos/{REPO}":
            body = {"default_branch": "main"}
        elif path.endswith("/commits/main"):
            body = {"sha": "d" * 40}
        else:
            body = {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}}
        return 200, {}, json.dumps(body).encode()

    def now() -> float:
        return tick[0]

    cfg = _parse_config(
        repository=REPO,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
        max_targets=5,
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
        RUNS_URL: {"workflow_runs": [wf_run(1, event="pull_request")]},
        RUNS_URL.replace(SHA, "c" * 40): {"workflow_runs": [wf_run(2, event="pull_request")]},
        RUNS_URL.replace(SHA, "d" * 40): {"workflow_runs": [wf_run(3)]},
        STATUSES_URL: [],
        STATUSES_URL.replace(SHA, "c" * 40): [],
        STATUSES_URL.replace(SHA, "d" * 40): [],
        COMMIT_URL: {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}},
    }
    for path in (COMMIT_URL, COMMIT_URL.replace(SHA, "c" * 40), COMMIT_URL.replace(SHA, "d" * 40)):
        answers.setdefault(path, {"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}})
    cfg = _parse_config(
        repository=REPO,
        event_name="push",
        sha=SHA,
        required_json='["Build"]',
        targets="open-pulls",
        run_number=1,
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
        # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-13 category D: fixed POST argv
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
        _parse_config(
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
        options=JudgeOptions(required=["Build", "Optional"], not_owed=["Optional"]),
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


GATES = ("Licence", "Secret scan", "Sign-off", "Workflow check")


def test_last_tail_posts_success_after_four_gates(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1: tails 1-3 post nothing while waiting; tail 4 posts success once."""
    posts: list[str] = []
    answers = base_answers(
        checks=[check_item(i, name, "success") for i, name in enumerate(GATES, 1)]
    )
    answers[STATUSES_URL] = []

    for idx, finished in enumerate((1, 2, 3, 4)):
        subset = list(GATES[:finished])
        checks = [check_item(i, name, "success") for i, name in enumerate(subset, 1)]
        snap = base_answers(checks=checks, statuses=[])
        rc, out, err, _ = invoke_with_forge(
            tree,
            monkeypatch,
            snap,
            options=JudgeOptions(
                required=list(GATES), event="pull_request", ref="refs/pull/1/head"
            ),
        )
        assert rc == 0, err
        log = (tree.root / "forge.log").read_text(encoding="utf-8")
        post_lines = [line for line in log.splitlines() if "state=success" in line]
        posts.append(post_lines[-1] if post_lines else "")
        if idx < 3:
            assert "POST" not in log or "state=success" not in log
        else:
            assert "state=success" in log
            assert "All owed required checks reported success." in out


def test_fork_tail_makes_no_api_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """T2: fork pull_request on sha target prints notice and uses no transport."""
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "pull_request": {
                    "head": {"repo": {"full_name": "fork/other"}, "sha": SHA},
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = _parse_config(
        repository=REPO,
        event_name="pull_request",
        sha=SHA,
        required_json=json.dumps(["Build"]),
    )
    calls: list[str] = []

    def transport(path: str, **_: object) -> tuple[int, dict[str, str], bytes]:
        calls.append(path)
        return 200, {}, b"{}"

    err = io.StringIO()
    rc = required_checks.run_judge(
        cfg,
        transport=transport,
        now=lambda: 1.0,
        sleep=lambda _: None,
        err=err,
        event_path=str(event),
    )
    assert rc == 0
    assert calls == []
    assert "Fork pull request" in err.getvalue()


def test_tail_sha_counts_own_gate_result_check_run(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: sha target still counts the gate caller result when GITHUB_WORKFLOW_REF is set."""
    gate_suite = 7
    checks = [check_item(1, "Licence", "success", suite_id=gate_suite)]
    runs = [
        wf_run(
            1, event="pull_request", suite_id=gate_suite, path=".github/workflows/licence-check.yml"
        )
    ]
    answers = {
        **base_answers(checks=checks, runs=runs),
        STATUSES_URL: [status_item(1, "required-checks", "success")],
    }
    rc, out, err, _ = invoke_with_forge(
        tree,
        monkeypatch,
        answers,
        options=JudgeOptions(required=["Licence"], event="pull_request", ref="refs/pull/1/head"),
    )
    assert rc == 0, err
    assert "OK (success): Licence" in out
    # Planted mutant (manual): in read_snapshot, apply self_workflow_suites on sha
    # targets too → same fixture then omits Licence and posts pending instead.


def test_pending_posts_only_over_success(tree: Tree, monkeypatch: pytest.MonkeyPatch) -> None:
    """T4: pending verdict posts only when the existing status is success."""
    queued = [check_item(1, "Build", None, status="queued")]
    over_none = base_answers(checks=queued)
    rc0, _, err0, _ = invoke_with_forge(
        tree, monkeypatch, over_none, options=JudgeOptions(required=["Build"])
    )
    assert rc0 == 0
    assert "POST" not in (tree.root / "forge.log").read_text(encoding="utf-8")

    over_success = {
        **over_none,
        STATUSES_URL: [status_item(1, "required-checks", "success")],
    }
    rc1, _, _, _ = invoke_with_forge(
        tree, monkeypatch, over_success, options=JudgeOptions(required=["Build"])
    )
    assert rc1 == 0
    assert "state=pending" in (tree.root / "forge.log").read_text(encoding="utf-8")

    over_failure = {**over_none, STATUSES_URL: [status_item(2, "required-checks", "failure")]}
    rc2, out2, _, _ = invoke_with_forge(
        tree, monkeypatch, over_failure, options=JudgeOptions(required=["Build"])
    )
    assert rc2 == 0
    assert "Posted pending" not in out2


@pytest.mark.parametrize("refresh", [False, True], ids=["snapshot", "freshness-refresh"])
def test_sweep_api_cap_every_call_site(refresh: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every call index stops at the cap, including preflight and refresh reads."""
    heads = [f"{i:040x}" for i in range(1, OPEN_PULLS_MAX + 1)]
    setup_calls = 1 + len(heads) + 2
    calls_per_eval = 12 if refresh else 7
    eval_heads = MAX_TARGETS_DEFAULT + 1
    complete_sweep_calls = setup_calls + calls_per_eval * eval_heads
    for cap in range(2, complete_sweep_calls):
        monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
        clock = Clock(1.0e9)
        transport, counts = _counting_sweep_transport(
            heads, page_bound=False, refresh_clock=clock if refresh else None
        )
        err = io.StringIO()
        rc = run_sweep(
            _sweep_count_config(),
            transport=transport,
            now=clock.now,
            sleep=clock.sleep,
            out=io.StringIO(),
            err=err,
        )
        assert rc == 0, f"cap={cap}: {err.getvalue()}"
        assert "Sweep API call cap" in err.getvalue(), err.getvalue()
        # The last target can leave a single call unused to reserve verification.
        expected_calls = cap - int(cap == complete_sweep_calls - 1)
        assert sum(counts.values()) == expected_calls
        assert counts["post"] == max(0, (cap - setup_calls) // calls_per_eval)


def test_sweep_call_count_at_fourteen_open_pulls() -> None:
    """Measured sweep at 14 open pull heads, one page per read and commit fallback."""
    heads = [f"{i:040x}" for i in range(1, 15)]
    transport, counts = _counting_sweep_transport(heads, page_bound=False)
    err = io.StringIO()
    assert (
        run_sweep(
            _sweep_count_config(),
            transport=transport,
            now=lambda: 1.0e9,
            sleep=lambda _: None,
            out=io.StringIO(),
            err=err,
        )
        == 0
    ), err.getvalue()
    assert counts["post"] == 15
    assert sum(counts.values()) == 122


def test_forge_review_request_documents_explicit_post() -> None:
    assert 'method="POST"' in (forge.request.__doc__ or "")


@pytest.mark.parametrize("overflow", [False, True], ids=["exact-cap", "extra-row"])
def test_forge_review_paged_confirms_overflow(overflow: bool) -> None:
    calls: list[str] = []
    first_page = [{"id": item} for item in range(forge.PER_PAGE)]
    extra_page = [{"id": forge.PER_PAGE}] if overflow else []

    def fetch(path: str) -> list[dict]:
        calls.append(path)
        return first_page if path.endswith("page=1") else extra_page

    if overflow:
        with pytest.raises(forge.ForgeError, match="more than 100 entries"):
            forge.paged("listing", None, max_pages=1, fetch=fetch)
    else:
        assert forge.paged("listing", None, max_pages=1, fetch=fetch) == first_page
    assert calls == ["listing?per_page=100&page=1", "listing?per_page=100&page=2"]


@pytest.mark.parametrize(
    ("retry_after", "reset", "expected"),
    [("1.5", "121", 26), ("30", "121", 30), ("0", "99", 5)],
    ids=["reset-wins", "retry-after-wins", "past-reset"],
)
def test_forge_review_rate_limit_uses_larger_delay(
    retry_after: str, reset: str, expected: int
) -> None:
    headers = {
        "retry-after": retry_after,
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": reset,
    }
    assert forge.rate_limit_delay(403, headers, "", 0, 100.0) == expected


def test_forge_review_subprocess_has_both_targeted_exceptions() -> None:
    source = (ROOT / "tools/forge.py").read_text(encoding="utf-8")
    ignore_word = "no" + "semgrep"
    directive = re.search(
        rf"# {ignore_word}: (.+) expires 2027-03-09 category D:[^\n]+\n\s*return subprocess\.run\(",
        source,
    )
    assert directive is not None
    rules = {rule.strip() for rule in directive[1].split(",")}
    assert rules == {
        (
            "python.lang.security.audit.dangerous-subprocess-use-tainted-env-args."
            "dangerous-subprocess-use-tainted-env-args"
        ),
        "dangerous-subprocess-use-audit",
    }


def test_forge_review_subprocess_arguments_share_the_suppressed_line() -> None:
    source = (ROOT / "tools/forge.py").read_text(encoding="utf-8")
    call = "subprocess" + ".run"
    assert (
        f"return {call}(command, capture_output=True, check=False, timeout=deadline, env=env)"
        in source
    )


# Review regressions use in-memory API replies so races and backoffs are deterministic.
REVIEW_EPOCH = required_checks._iso_epoch("2026-11-21T12:00:00Z")
REVIEW_POST_PATH = f"repos/{REPO}/statuses/{SHA}"


class ReviewTransport:
    def __init__(self) -> None:
        self.answers: dict[str, object] = base_answers()
        self.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build")]}
        self.answers[RUNS_URL] = {"workflow_runs": [wf_run(1)]}
        self.replies: dict[str, Iterator[object]] = {}
        self.calls: list[tuple[str, str]] = []
        self.posts: list[tuple[str, str]] = []
        self.post_errors: Iterator[forge.ResponseError] = iter(())
        self.clock = Clock(REVIEW_EPOCH)
        self.advance_on_reply: dict[str, Iterator[int]] = {}
        self.before_post: Callable[[], None] | None = None
        self.post_answers: Iterator[object] = iter(())

    def __call__(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, path))
        if method == "POST":
            return self._post(path, kwargs["fields"])
        if "/check-runs?" in path and path not in self.answers and path not in self.replies:
            path = path.replace("/check-runs?per_page=", "/check-runs?filter=all&per_page=")
        answer = self.answers.get(path, [])
        if path in self.replies:
            answer = next(self.replies[path], answer)
        if path in self.advance_on_reply:
            seconds = next(self.advance_on_reply[path], 0)
            self.clock.current += seconds
        return 200, {}, json.dumps(answer).encode()

    def _post(self, path: str, fields: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        if self.before_post is not None:
            self.before_post()
        self.posts.append((path, fields["state"]))
        error = next(self.post_errors, None)
        if error is not None:
            raise error
        sha = path.rsplit("/", 1)[1]
        statuses_path = STATUSES_URL.replace(SHA, sha)
        stamp = datetime.fromtimestamp(self.clock.now(), UTC).isoformat(timespec="seconds")
        row = {**status_item(1000 + len(self.posts), fields["context"], updated_at=stamp), **fields}
        statuses = self.answers.get(statuses_path, [])
        assert isinstance(statuses, list)
        self.answers[statuses_path] = [row, *statuses][: forge.PER_PAGE]
        return 201, {}, json.dumps(next(self.post_answers, row)).encode()


def _review_config(**overrides: Any) -> required_checks.Config:
    return _parse_config(
        **{
            "repository": REPO,
            "event_name": "push",
            "ref": "refs/heads/main",
            "sha": SHA,
            "required_json": '["Build"]',
            **overrides,
        }
    )


def _review_judge(transport: ReviewTransport, **overrides: Any) -> tuple[int, str, str, Clock]:
    clock = transport.clock
    out, err = io.StringIO(), io.StringIO()
    rc = run_judge(
        _review_config(**overrides),
        transport=transport,
        now=clock.now,
        sleep=clock.sleep,
        out=out,
        err=err,
    )
    return rc, out.getvalue(), err.getvalue(), clock


@pytest.mark.parametrize(
    ("event", "expected"),
    [("push", "success"), ("pull_request", "failure"), ("merge_group", "failure")],
)
def test_review_signoff_owed_on_proposal_events(event: str, expected: str) -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": []}
    rc, _, err, _ = _review_judge(
        fake,
        event_name=event,
        required_json='["Sign-off"]',
        bound_minutes=1,
        pull_request_only_json='["Sign-off"]',
    )
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, expected)]


@pytest.mark.parametrize("moves_twice", [False, True])
def test_review_prepost_newer_self_status_refreshes_verdict(moves_twice: bool) -> None:
    fake = ReviewTransport()
    first = status_item(41, "required-checks", "failure", updated_at="2026-11-21T12:00:01Z")
    latest = status_item(42, "required-checks", "failure", updated_at="2026-11-21T12:00:02Z")
    fake.replies[STATUSES_URL] = iter([[], [first], [first], [latest if moves_twice else first]])
    fake.advance_on_reply[STATUSES_URL] = iter([0, 1, 0, int(moves_twice)])
    fake.replies[CHECKS_URL] = iter(
        [
            {"check_runs": [check_item(1, "Build")]},
            {"check_runs": [check_item(2, "Build", "failure")]},
        ]
    )
    rc, _, err, clock = _review_judge(fake)
    assert rc == 0, err
    assert clock.sleeps == []
    assert fake.posts == ([] if moves_twice else [(REVIEW_POST_PATH, "failure")])
    assert sum("/check-runs?" in path for _, path in fake.calls) == 2
    if moves_twice:
        assert "::warning::" in err


@pytest.mark.parametrize(
    ("app", "suite_id", "event", "expected"),
    [
        ("github-actions", 777, "push", "failure"),
        ("github-actions", None, "push", "failure"),
        ("github-actions", 1, "push", "success"),
        ("scan-app", 1, "schedule", "success"),
    ],
)
def test_review_actions_checks_require_eligible_suite(
    app: str, suite_id: int | None, event: str, expected: str
) -> None:
    fake = ReviewTransport()
    fake.answers[RUNS_URL] = {"workflow_runs": [wf_run(1, event)]}
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", app=app, suite_id=suite_id)]}
    rc, out, err, _ = _review_judge(fake, bound_minutes=1, apps_json=json.dumps({"Build": app}))
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, expected)]
    if expected == "failure":
        assert "deadline of 1 min passed; waiting on: Build" in out


def test_review_full_workflow_listing_refuses_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ReviewTransport()
    monkeypatch.setattr(required_checks, "WORKFLOW_RUN_MAX_PAGES", 1)
    fake.answers[RUNS_URL] = {"workflow_runs": [wf_run(1)] * forge.PER_PAGE}
    fake.answers[RUNS_URL.replace("page=1", "page=2")] = {"workflow_runs": []}
    rc, _, err, _ = _review_judge(fake)
    assert rc == 1
    assert "::error::" in err and "workflow" in err and "cap" in err.lower()
    assert fake.posts == []


def test_review_requests_all_checks_to_find_eligible_run() -> None:
    fake = ReviewTransport()
    fake.answers[RUNS_URL] = {"workflow_runs": [wf_run(1), wf_run(2, "schedule", suite_id=2)]}
    other_check = check_item(2, "Build", "failure", suite_id=2)
    eligible = check_item(1, "Build")
    fake.answers[CHECKS_URL.replace("filter=all&", "")] = {"check_runs": [other_check]}
    fake.answers[CHECKS_URL] = {"check_runs": [other_check, eligible]}
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert ("GET", CHECKS_URL) in fake.calls
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


def test_review_foreign_and_self_runs_do_not_age_owed_checks() -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", None)]}
    fake.answers[RUNS_URL] = {
        "workflow_runs": [
            wf_run(1, created_at="2026-11-21T12:00:00Z"),
            wf_run(2, "schedule", suite_id=2, created_at="2026-11-21T08:00:00Z"),
            wf_run(
                3,
                suite_id=3,
                created_at="2026-11-21T08:00:00Z",
                path=required_checks.DEFAULT_SELF_WORKFLOW,
            ),
        ]
    }
    rc, _, err, _ = _review_judge(fake, bound_minutes=1)
    assert rc == 0, err
    assert fake.posts == []


@pytest.mark.parametrize(
    ("conclusion", "state", "expected"),
    [
        ("failure", "pending", "failure"),
        (None, "error", "failure"),
        (None, "pending", "pending"),
        ("success", "pending", "pending"),
    ],
)
def test_review_terminal_failure_beats_pending_peer(
    conclusion: str | None, state: str, expected: str
) -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", conclusion)]}
    fake.answers[STATUSES_URL] = [
        status_item(1, "Build", state),
        status_item(2, "required-checks", "success"),
    ]
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, expected)]


@pytest.mark.parametrize(
    ("error_status", "repeat", "wait", "expected_rc"),
    [(502, False, 30, 0), (429, False, 20, 0), (502, True, 30, 1), (403, False, 0, 1)],
)
def test_review_post_retry_rereads_before_next_attempt(
    error_status: int, repeat: bool, wait: int, expected_rc: int
) -> None:
    fake = ReviewTransport()
    headers = {"retry-after": "20"} if error_status == 429 else {}
    error = forge.ResponseError("write failed", status=error_status, headers=headers)
    fake.post_errors = iter([error] * (2 if repeat else 1))
    fake.replies[CHECKS_URL] = iter(
        [
            {"check_runs": [check_item(1, "Build")]},
            {"check_runs": [check_item(2, "Build", "failure")]},
        ]
    )
    rc, _, err, clock = _review_judge(fake)
    assert clock.sleeps == ([wait] if wait else [])
    expected_posts = [(REVIEW_POST_PATH, "success")]
    if wait:
        expected_posts.append((REVIEW_POST_PATH, "failure"))
    assert fake.posts == expected_posts
    assert rc == expected_rc, err
    assert ("::error::" in err) == (expected_rc == 1)


@pytest.mark.parametrize(("limit", "expected_attempts", "fails"), [(1, 1, True), (2, 2, False)])
def test_review_api_cap_counts_failed_attempts(
    limit: int, expected_attempts: int, fails: bool
) -> None:
    attempts = []
    clock = Clock(REVIEW_EPOCH)

    def read(path: str, **kwargs: object) -> tuple[int, dict[str, str], bytes]:
        attempts.append(path)
        if len(attempts) == 1:
            raise forge.ResponseError("read failed", status=502)
        return 200, {}, b"[]"

    session = ApiSession(
        now=clock.now,
        sleep=clock.sleep,
        transport=read,
        deadline=REVIEW_EPOCH + 120,
        max_api_calls=limit,
        out=io.StringIO(),
    )
    if fails:
        with pytest.raises(required_checks.ApiCapReachedError):
            session.request_json("statuses", STATUSES_URL)
    else:
        assert session.request_json("statuses", STATUSES_URL) == []
    assert session.api_calls == len(attempts) == expected_attempts


def _review_sweep_transport(
    heads: list[str],
    *,
    app: str = "github-actions",
    conclusion: str | None = "success",
) -> ReviewTransport:
    fake = ReviewTransport()
    fake.answers.update(
        {
            f"repos/{REPO}/pulls?state=open&per_page=100": [
                {"head": {"sha": sha}} for sha in heads
            ],
            f"repos/{REPO}": {"default_branch": "main"},
            f"repos/{REPO}/commits/main": {"sha": SHA},
        }
    )
    for sha in [*heads, SHA]:
        fake.answers[CHECKS_URL.replace(SHA, sha)] = {
            "check_runs": [check_item(1, "Build", conclusion, app=app)]
        }
        event = "push" if sha == SHA else "pull_request"
        fake.answers[RUNS_URL.replace(SHA, sha)] = {"workflow_runs": [wf_run(1, event=event)]}
        fake.answers[STATUSES_URL.replace(SHA, sha)] = []
    return fake


def _review_sweep(
    fake: ReviewTransport, *, run_number: int = 1, **overrides: Any
) -> tuple[int, str]:
    err = io.StringIO()
    cfg = _review_config(targets="open-pulls", run_number=run_number, **overrides)
    rc = run_sweep(
        cfg, transport=fake, now=fake.clock.now, sleep=fake.clock.sleep, out=io.StringIO(), err=err
    )
    return rc, err.getvalue()


def test_review_sweep_reads_statuses_after_triage() -> None:
    pull = "1" * 40
    fake = _review_sweep_transport([pull])
    owed = status_item(1, "Build", "failure")
    fake.replies[STATUSES_URL.replace(SHA, pull)] = iter([[], [owed], [owed]])
    rc, err = _review_sweep(fake)
    assert rc == 0, err
    assert (REVIEW_POST_PATH.replace(SHA, pull), "failure") in fake.posts


def test_review_sweep_rechecks_failures_after_unsettled_heads() -> None:
    first, second, waiting, success = ("1" * 40, "2" * 40, "3" * 40, "4" * 40)
    fake = _review_sweep_transport([second, success, first, waiting], app="external")
    for id_, sha, state in [(1, first, "failure"), (2, second, "failure"), (3, success, "success")]:
        stamp = f"2026-11-21T10:00:0{id_}Z"
        fake.answers[STATUSES_URL.replace(SHA, sha)] = [
            status_item(id_, "required-checks", state, updated_at=stamp)
        ]
    rc, err = _review_sweep(fake, max_targets=2, apps_json='{"Build":"external"}')
    assert rc == 0, err
    pulls = [(path, state) for path, state in fake.posts if path != REVIEW_POST_PATH]
    assert pulls == [(REVIEW_POST_PATH.replace(SHA, sha), "success") for sha in (waiting, first)]


def _review_evaluated_pulls(fake: ReviewTransport) -> list[str]:
    return [
        path.split("/commits/")[1].split("/")[0]
        for method, path in fake.calls
        if method == "GET" and "/check-runs?" in path and f"/commits/{SHA}/" not in path
    ]


def test_review_sweep_rotates_thirty_one_waiting_heads() -> None:
    heads = [f"{i:040x}" for i in range(1, 32)]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    for run_number, expected in [(1, heads[:30]), (2, heads[30:] + heads[:29])]:
        fake.calls.clear()
        rc, err = _review_sweep(fake, run_number=run_number, apps_json='{"Build":"external"}')
        assert rc == 0, err
        assert _review_evaluated_pulls(fake) == expected
    assert fake.posts == []


def test_review_sweep_rotates_thirty_one_unchanged_failures() -> None:
    heads = [f"{i:040x}" for i in range(1, 32)]
    fake = _review_sweep_transport(heads, conclusion="failure")
    for index, sha in enumerate(heads):
        row = status_item(
            index + 1, "required-checks", "failure", updated_at=f"2026-11-21T11:00:{index:02d}Z"
        )
        fake.answers[STATUSES_URL.replace(SHA, sha)] = [{**row, "description": "Build: failure"}]
    for run_number in (1, 2):
        rc, err = _review_sweep(fake, run_number=run_number)
        assert rc == 0, err
    assert not any(path != REVIEW_POST_PATH for path, _ in fake.posts)
    assert set(_review_evaluated_pulls(fake)) == set(heads)


def test_review_sweep_rotates_failure_in_with_thirty_waiting_heads() -> None:
    heads = [f"{i:040x}" for i in range(1, 32)]
    recovered = heads[-1]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    fake.answers[STATUSES_URL.replace(SHA, recovered)] = [
        status_item(1, "required-checks", "failure")
    ]
    fake.answers[CHECKS_URL.replace(SHA, recovered)] = {
        "check_runs": [check_item(2, "Build", app="external")]
    }
    for run_number in (1, 2):
        rc, err = _review_sweep(fake, run_number=run_number, apps_json='{"Build":"external"}')
        assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH.replace(SHA, recovered), "success")]


@pytest.mark.parametrize("run_number", [1, 2, 10**9])
def test_review_sweep_keeps_order_at_exact_budget(run_number: int) -> None:
    heads = [f"{i:040x}" for i in range(1, 31)]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    rc, err = _review_sweep(fake, run_number=run_number, apps_json='{"Build":"external"}')
    assert rc == 0, err
    assert _review_evaluated_pulls(fake) == heads


def test_review_default_branch_excludes_newer_release_push() -> None:
    fake = _review_sweep_transport([])
    fake.answers[RUNS_URL] = {
        "workflow_runs": [
            wf_run(1),
            wf_run(2, suite_id=2, head_branch="release"),
        ]
    }
    fake.answers[CHECKS_URL] = {
        "check_runs": [
            check_item(1, "Build", "failure"),
            check_item(2, "Build", suite_id=2, started_at="2026-11-21T12:01:00Z"),
        ]
    }
    rc, err = _review_sweep(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


@pytest.mark.parametrize("sweep", [False, True])
def test_review_bad_status_shape_reports_api_error(sweep: bool) -> None:
    fake = _review_sweep_transport([SHA])
    fake.answers[STATUSES_URL] = {"wrong": []}
    if sweep:
        rc, err = _review_sweep(fake)
    else:
        rc, _, err, _ = _review_judge(fake)
    assert rc == 1
    assert "::error::" in err and "no list" in err
    assert fake.posts == []


@pytest.mark.parametrize("digits", ["0000001", "9" * 5_000], ids=["seven", "conversion-limit"])
def test_review_oversized_integer_reports_invalid_input(digits: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    rc = main(
        ["judge", "--bound-minutes", digits],
        env={
            "REPOSITORY": REPO,
            "EVENT_NAME": "push",
            "COMMIT_SHA": SHA,
            "REQUIRED_JSON": '["Build"]',
        },
        out=out,
        err=err,
    )
    assert rc == 2
    assert "Invalid required-checks input" in err.getvalue() and "bound-minutes" in err.getvalue()


def test_review_positive_self_test_rejects_failure_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_decide = required_checks.decide

    def replace_success(*args: Any) -> tuple[str, str]:
        state, description = real_decide(*args)
        return ("failure", "planted bad verdict") if state == "success" else (state, description)

    monkeypatch.setattr(required_checks, "decide", replace_success)
    out = io.StringIO()
    assert self_test(out) == 1
    assert "FAIL owed check and status pass" in out.getvalue()


def test_review_tree_forge_calls_extracts_post_endpoint(tree: Tree) -> None:
    get_arguments = ["api", "--include", STATUSES_URL]
    post_arguments = ["api", "--include", "-X", "POST", REVIEW_POST_PATH, "-f", "state=pending"]
    tree.write({"forge.log": "\n".join(map(json.dumps, [get_arguments, post_arguments])) + "\n"})
    assert tree.forge_calls() == [STATUSES_URL, REVIEW_POST_PATH]


def test_review_config_uses_frozen_raw_inputs() -> None:
    raw_type = getattr(required_checks, "RawInputs", None)
    assert raw_type is not None
    raw: Any = raw_type(repository=REPO, event_name="push", sha=SHA, required_json='["Build"]')
    assert parse_config(raw).required == ("Build",)
    with pytest.raises(FrozenInstanceError):
        raw.sha = SHA256


def test_review_subprocess_audit_exceptions_cover_fixed_argv() -> None:
    source = Path(__file__).read_text(encoding="utf-8").splitlines()
    markers = ("subprocess." + "run(", "return subprocess." + "CompletedProcess(")
    calls = [index for index, line in enumerate(source) if any(key in line for key in markers)]
    assert len(calls) == 4
    ignore_word = "no" + "semgrep"
    for index in calls:
        assert (
            f"# {ignore_word}: dangerous-subprocess-use-audit expires 2027-03-13 category D:"
            in source[index - 1]
        )


@pytest.mark.parametrize("elapsed", [10, 11])
def test_freshness_rereads_then_skips_stale_post(elapsed: int) -> None:
    fake = ReviewTransport()
    fake.advance_on_reply[STATUSES_URL] = iter([0, elapsed, 0, elapsed])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    if elapsed == required_checks.POST_FRESH_SECONDS:
        assert fake.posts == [(REVIEW_POST_PATH, "success")]
    else:
        assert f"snapshot read started {elapsed} s ago (limit 10 s)" in err
        assert not fake.posts
        assert sum("/check-runs?" in path for _, path in fake.calls) == 2


def test_review_slow_workflow_read_refreshes_checks_before_post() -> None:
    fake = ReviewTransport()

    def workflow_replies() -> Iterator[object]:
        fake.clock.current += required_checks.POST_FRESH_SECONDS + 1
        fake.answers[CHECKS_URL] = {"check_runs": [check_item(2, "Build", "failure")]}
        yield fake.answers[RUNS_URL]

    fake.replies[RUNS_URL] = workflow_replies()
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]
    assert fake.calls.count(("GET", CHECKS_URL)) == 2


def _interleave_sweep_failure(fake: ReviewTransport, created_at: str) -> None:
    fake.before_post = None
    assert fake.calls[-2] == ("GET", STATUSES_URL)
    fake.clock.current = max(fake.clock.now(), required_checks._iso_epoch(created_at))
    failed = status_item(41, "required-checks", "failure", updated_at=created_at)
    fake.answers[STATUSES_URL] = [failed]
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(2, "Build", "failure")]}


@pytest.mark.parametrize("only_if_pending", [False, True])
def test_review_post_verification_repairs_interleaved_failure(only_if_pending: bool) -> None:
    fake = ReviewTransport()
    fake.clock.current += 0.5
    fake.before_post = lambda: _interleave_sweep_failure(fake, "2026-11-21T12:00:01Z")
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 2])
    rc, _, err, _ = _review_judge(fake, only_if_pending=only_if_pending)
    assert rc == 0 and not err, err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]
    statuses = fake.answers[STATUSES_URL]
    assert isinstance(statuses, list)
    assert required_checks._all_statuses(statuses)["required-checks"]["state"] == "failure"


def test_review_post_verification_reads_page_once_without_contention() -> None:
    fake = ReviewTransport()
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    post = fake.calls.index(("POST", REVIEW_POST_PATH))
    assert fake.calls[post + 1 :] == [("GET", STATUSES_URL)]


@pytest.mark.parametrize("own_id_visible", [False, True])
def test_review_post_verification_checks_full_page_for_own_status(own_id_visible: bool) -> None:
    fake = ReviewTransport()
    foreign = [status_item(i, "other") for i in range(1, forge.PER_PAGE + 1)]
    if own_id_visible:
        foreign[-1] = status_item(1001, "required-checks", "success")
    fake.replies[STATUSES_URL] = iter([[], [], foreign])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert fake.calls.count(("GET", CHECKS_URL)) == (1 if own_id_visible else 2)


def test_review_second_contested_post_warns_and_stops() -> None:
    fake = ReviewTransport()
    fake.before_post = lambda: _interleave_sweep_failure(fake, "2026-11-21T12:00:00Z")
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0
    assert f"::warning::Status write contested again for {SHA}." in err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]
    assert fake.calls.count(("GET", CHECKS_URL)) == 2


@pytest.mark.parametrize(
    ("created_at", "contested"), [("2026-11-21T12:00:00Z", True), ("2026-11-21T11:59:59Z", False)]
)
def test_review_post_verification_timestamp_boundary(created_at: str, contested: bool) -> None:
    fake = ReviewTransport()
    fake.clock.current += 0.5
    fake.before_post = lambda: _interleave_sweep_failure(fake, created_at)
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    expected = [(REVIEW_POST_PATH, "success")]
    if contested:
        expected.append((REVIEW_POST_PATH, "failure"))
    assert fake.posts == expected


@pytest.mark.parametrize(
    "answer", [{}, {"id": "1"}, {"id": True}], ids=["missing", "string", "bool"]
)
def test_review_post_answer_requires_integer_id(answer: object) -> None:
    fake = ReviewTransport()
    fake.post_answers = iter([answer])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 1
    assert "post-status" in err and "integer id" in err and SHA in err


def test_review_prepost_owed_status_change_refreshes_verdict() -> None:
    fake = ReviewTransport()
    previously = status_item(1, "Build")
    latest = status_item(2, "Build", "failure", updated_at="2026-11-21T12:00:01Z")
    fake.replies[STATUSES_URL] = iter([[previously], [latest], [latest], [latest]])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


def test_review_sweep_dedupes_failure_that_still_applies() -> None:
    pull = "1" * 40
    fake = _review_sweep_transport([pull], conclusion="failure")
    existing = {
        **status_item(1, "required-checks", "failure"),
        "description": "Build: failure",
    }
    fake.answers[STATUSES_URL.replace(SHA, pull)] = [existing]
    rc, err = _review_sweep(fake)
    assert rc == 0, err
    assert ("GET", CHECKS_URL.replace(SHA, pull)) in fake.calls
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


@pytest.mark.parametrize(
    ("has_workflow_stamp", "post", "expected_calls"),
    [(True, True, 6), (False, True, 7), (True, False, 3), (False, False, 4)],
)
def test_review_tail_call_count(has_workflow_stamp: bool, post: bool, expected_calls: int) -> None:
    fake = ReviewTransport()
    stamp = "2026-11-21T10:00:00Z" if has_workflow_stamp else ""
    fake.answers[RUNS_URL] = {"workflow_runs": [wf_run(1, created_at=stamp)]}
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", "success" if post else None)]}
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == ([(REVIEW_POST_PATH, "success")] if post else [])
    assert len(fake.calls) == expected_calls


def _review_judge_function(name: str) -> ast.FunctionDef:
    source = (ROOT / "tools/required_checks.py").read_text(encoding="utf-8")
    return next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_review_judge_function_limits() -> None:
    source = (ROOT / "tools/required_checks.py").read_text(encoding="utf-8")
    functions = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef)]
    decisions = (ast.If, ast.IfExp, ast.For, ast.While, ast.ExceptHandler)
    for function in functions:
        branches = sum(isinstance(node, decisions) for node in ast.walk(function))
        assert branches <= 9, function.name
        assert function.end_lineno is not None
        assert function.end_lineno - function.lineno + 1 <= 60, function.name


def test_review_bool_parser_does_not_assign_token() -> None:
    function = _review_judge_function("_parse_bool")
    assert all(
        node.id != "token"
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
