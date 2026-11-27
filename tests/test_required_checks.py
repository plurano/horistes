# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for the reusable required-checks judge and forge `--include` extensions (ADR-0108)."""

from __future__ import annotations

import ast
import io
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, dataclass, replace
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
PUSH_CONTEXT = "required-checks (push)"
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
    started_at: str | None = "2026-11-21T12:00:00Z",
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


def _forge_env(tree: Tree, answers: dict[str, object]) -> dict[str, str]:
    """Build `tree.forge` stand-in that echoes POSTed commit statuses onto page 1."""
    env = tree.forge(answers)
    fake_script = tree.root / "fake_gh.py"
    created_hdr = "HTTP/2.0 201 Created\\r\\nContent-Type: application/json\\r\\n\\r\\n"
    old_post = "\n".join(
        [
            "if post_fields is not None:",
            '    body = json.dumps({"id": 1, "state": post_fields.get("state", "pending")})',
            f'    sys.stdout.write(f"{created_hdr}{{body}}")',
            "    sys.exit(0)",
            "",
        ]
    )
    new_post = "\n".join(
        [
            "if post_fields is not None:",
            '    forge_path = os.environ["FAKE_FORGE"]',
            '    with open(forge_path, encoding="utf-8") as source:',
            "        stored = json.load(source)",
            '    status_id = int(stored.get("_next_status_id", 1000)) + 1',
            '    stored["_next_status_id"] = status_id',
            '    stamp = "2026-11-21T12:00:00Z"',
            '    row = {"id": status_id, "created_at": stamp, "updated_at": stamp, **post_fields}',
            '    if endpoint and "/statuses/" in endpoint:',
            '        repo_prefix, target_sha = endpoint.split("/statuses/", 1)',
            '        suffix = "statuses?per_page=100&page=1"',
            '        statuses_key = f"{repo_prefix}/commits/{target_sha}/{suffix}"',
            "        existing = stored.get(statuses_key)",
            "        if isinstance(existing, list):",
            "            stored[statuses_key] = [row, *existing][:100]",
            '    with open(forge_path, "w", encoding="utf-8") as target:',
            "        json.dump(stored, target)",
            "    body = json.dumps(row)",
            f'    sys.stdout.write(f"{created_hdr}{{body}}")',
            "    sys.exit(0)",
            "",
        ]
    )
    script_text = fake_script.read_text(encoding="utf-8")
    assert old_post in script_text
    fake_script.write_text(script_text.replace(old_post, new_post, 1), encoding="utf-8")
    return env


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
    for name, value in _forge_env(tree, answers).items():
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


def _forge_post_records(tree: Tree) -> list[list[str]]:
    log = tree.root / "forge.log"
    if not log.exists():
        return []
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [call for call in calls if "POST" in call]


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


@pytest.mark.parametrize(
    ("raw", "repository", "expected"),
    [
        (
            f"{REPO}/.github/workflows/licence-check.yml@refs/heads/release@2",
            REPO,
            ".github/workflows/licence-check.yml",
        ),
        (
            f"{REPO}/.github/workflows/licence-check.yml@refs/pull/1/head",
            "",
            ".github/workflows/licence-check.yml",
        ),
        (
            f"acme/widget/.github/workflows/required-checks-caller.yml@{SHA}",
            "acme/widget",
            ".github/workflows/required-checks-caller.yml",
        ),
        ("  ", REPO, ""),
    ],
    ids=["branch-with-at", "pull-ref", "consumer-sha-ref", "unset"],
)
def test_workflow_ref_path_splits_at_first_at_sign(
    raw: str, repository: str, expected: str
) -> None:
    assert required_checks._path_from_workflow_ref(raw, repository) == expected


@pytest.mark.parametrize(
    ("raw", "repository", "match"),
    [
        (f"{REPO}@refs/heads/main", REPO, "has no workflow path"),
        (f"{REPO}/.github/workflows/licence-check.yml", REPO, "github-workflow-ref:"),
        (f"{REPO}/.github/workflows/licence-check.yml@", REPO, "github-workflow-ref:"),
        (f"{REPO}/.github/workflows/licence-check.yml@   ", REPO, "github-workflow-ref:"),
        (
            f"{REPO}/.github/workflows/licence-check.yml@refs/heads/bad ref",
            REPO,
            "github-workflow-ref:",
        ),
        ("//.github/workflows/licence-check.yml@refs/heads/main", "", "github-workflow-ref:"),
        (
            "owner/../.github/workflows/licence-check.yml@refs/heads/main",
            "",
            "github-workflow-ref:",
        ),
        (
            "other/shared/.github/workflows/required-checks-sweep.yml@refs/heads/main",
            "acme/widget",
            "github-workflow-ref:",
        ),
    ],
    ids=[
        "no-path",
        "missing-at-ref",
        "empty-ref",
        "trailing-space-ref",
        "whitespace-in-ref",
        "empty-owner-repo",
        "dotdot-repo",
        "foreign-repo-prefix",
    ],
)
def test_workflow_ref_without_path_is_rejected(raw: str, repository: str, match: str) -> None:
    with pytest.raises(InputError, match=match):
        required_checks._path_from_workflow_ref(raw, repository)


def test_cli_rejects_mismatched_workflow_ref_before_polling(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in tree.forge({}).items():
        monkeypatch.setenv(name, value)
    err = io.StringIO()
    rc = main(
        ["judge", "--repository", "acme/widget", "--event", "push", "--sha", SHA],
        env={
            "REQUIRED_JSON": '["Gate"]',
            "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/required-checks.yml@refs/heads/main",
        },
        out=io.StringIO(),
        err=err,
    )
    assert rc == 2
    assert "Invalid required-checks input" in err.getvalue()
    assert "github-workflow-ref:" in err.getvalue()
    assert tree.forge_calls() == []


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
    env = _forge_env(fixture_tree, base_answers(checks=[check_item(1, "Gate")], runs=[wf_run(1)]))
    run_env = {
        **os.environ,
        **env,
        "REPOSITORY": REPO,
        "EVENT_NAME": "push",
        "REF": "refs/heads/main",
        "COMMIT_SHA": SHA,
        "REQUIRED_JSON": '["Gate"]',
        "GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/ci.yml@refs/heads/main",
    }
    cmd_judge = [sys.executable, "-I", "-B", script, "judge"]
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-13 category D: fixed judge argv
    done_judge = subprocess.run(
        cmd_judge, capture_output=True, text=True, check=False, timeout=30, env=run_env
    )
    assert done_judge.returncode == 0, done_judge.stdout + done_judge.stderr
    assert "OK (success): Gate" in done_judge.stdout
    posts = _forge_post_records(fixture_tree)
    assert len(posts) == 1
    method = posts[0].index("-X")
    assert posts[0][method + 1 : method + 3] == ["POST", f"repos/{REPO}/statuses/{SHA}"]
    assert "state=success" in posts[0]

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
    context = (
        "required-checks"
        if event in ("pull_request", "pull_request_target", "merge_group")
        else PUSH_CONTEXT
    )
    assert f"Posted success for {context} on {SHA}" in out


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
    forge_env = _forge_env(tree, answers)
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
    recovered_posts = _forge_post_records(tree)
    assert len(recovered_posts) == 1
    assert "state=success" in recovered_posts[0]

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
        STATUSES_URL: [status_item(1, PUSH_CONTEXT, "success")],
    }
    rc, out, err, clock = invoke_with_forge(
        tree,
        monkeypatch,
        answers,
        options=JudgeOptions(required=["A", "B"], bound_minutes=240),
    )
    assert rc == 0, err
    assert clock.sleeps == []
    assert f"Posted pending for {PUSH_CONTEXT} on {SHA}" in out
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
    for name, value in _forge_env(tree, answers).items():
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

    forge_env = _forge_env(tree, answers)
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
    prior_success = [status_item(0, PUSH_CONTEXT, "success")]
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
        "context": PUSH_CONTEXT,
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
    statuses = [status_item(1, PUSH_CONTEXT, "success")]
    answers = base_answers(statuses=statuses)
    answers[CHECKS_URL] = {"fail": "check-runs should not be read"}
    rc, out, _, _ = invoke_with_forge(
        tree, monkeypatch, answers, options=JudgeOptions(required=["Build"], only_if_pending=True)
    )
    assert rc == 0
    assert f"{SHA}: {PUSH_CONTEXT} already settled" in out
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
    posted_by_sha: dict[str, list[dict]] = {}
    next_id = 1000

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal next_id
        record.append((method, path))
        if method == "POST":
            next_id += 1
            sha = path.rsplit("/", 1)[1]
            raw_fields = kwargs.get("fields")
            fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
            stamp = "2026-11-23T12:00:00Z"
            row = {"id": next_id, "created_at": stamp, "updated_at": stamp, **fields}
            posted_by_sha.setdefault(sha, []).insert(0, row)
            return 201, {}, json.dumps(row).encode()
        body = _sweep_response_body(path, heads, settled)
        if (path.endswith("/statuses") or "/statuses?" in path) and isinstance(body, list):
            sha = path.split("/commits/")[1].split("/")[0]
            body = [*posted_by_sha.get(sha, ()), *body][: forge.PER_PAGE]
        return 200, {}, json.dumps(body).encode()

    return transport


def test_sweep_judges_never_judged_before_successes() -> None:
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
        now=lambda: required_checks._iso_epoch("2026-11-23T12:00:00Z"),
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
    assert _statuses_get_count([p for _, p in record]) == len(heads) + 3 * (MAX_TARGETS_DEFAULT + 1)


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
    assert full_evals == ["d" * 40, *heads[:30]]


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

    session = ApiSession(
        deadline=start + required_checks.WITHDRAWAL_RESERVE_SECONDS + 20.0,
        now=lambda: start,
        transport=transport,
    )
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
        sha = SWEEP_FIXED_BODIES["default-commit"]["sha"]
        context = PUSH_CONTEXT if f"/commits/{sha}/" in path else "required-checks"
        full = [
            status_item(1, context, "pending", updated_at="2000-01-01T00:00:00Z"),
            *(
                status_item(i, "ext", "pending", updated_at="2000-01-01T00:00:00Z")
                for i in range(2, forge.PER_PAGE + 1)
            ),
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
    posted_by_sha: dict[str, list[dict]] = {}
    next_id = 1000

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal next_id
        kind = _classify_sweep_call(method, path)
        counts[kind] += 1
        if kind == "statuses" and refresh_clock is not None:
            statuses_per_path[path] = statuses_per_path.get(path, 0) + 1
            prepost = 2 if f"/commits/{SWEEP_FIXED_BODIES['default-commit']['sha']}/" in path else 3
            if statuses_per_path[path] == prepost:
                refresh_clock.current += required_checks.POST_FRESH_SECONDS + 1
        if kind == "post":
            next_id += 1
            sha = path.rsplit("/", 1)[1]
            raw_fields = kwargs.get("fields")
            fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
            stamp = "2001-09-09T01:46:40Z"
            row = {"id": next_id, "created_at": stamp, "updated_at": stamp, **fields}
            posted_by_sha.setdefault(sha, []).insert(0, row)
            return 201, {}, json.dumps(row).encode()
        body = _sweep_body(kind, path, heads, page_bound)
        if kind == "statuses" and path.endswith("page=1") and isinstance(body, list):
            sha = path.split("/commits/")[1].split("/")[0]
            body = [*posted_by_sha.get(sha, ()), *body][: forge.PER_PAGE]
        return 200, {}, json.dumps(body).encode()

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
    expected_total = (
        required_checks.SWEEP_MAX_CALLS if page_bound else required_checks.SWEEP_MAX_CALLS - 2
    )
    assert sum(counts.values()) == expected_total
    assert counts["open-pulls"] == 1
    if page_bound:
        assert counts["statuses"] == 164
        assert counts["check-runs"] == 59
        assert counts["post"] == 2
    else:
        assert counts["check-runs"] == 21
        assert counts["post"] == 20


@pytest.mark.parametrize(
    ("page_bound", "status_pages", "expected_total"), [(False, 1, 320), (True, 20, 1777)]
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
    assert counts["statuses"] == OPEN_PULLS_MAX + eval_heads * (status_pages + 2)
    assert counts["post"] == counts["commit"] == eval_heads


def _mixed_sweep_transport(
    pull_order: list[str],
    status_by_sha: dict[str, dict],
    *,
    record: list[tuple[str, str]],
) -> Any:
    posted_by_sha: dict[str, list[dict]] = {}
    next_id = 1000

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal next_id
        record.append((method, path))
        if "/pulls?" in path:
            body: object = [{"head": {"sha": s}} for s in pull_order]
        elif method == "POST" and "/statuses/" in path:
            next_id += 1
            sha = path.rsplit("/", 1)[1]
            raw_fields = kwargs.get("fields")
            fields = dict(raw_fields) if isinstance(raw_fields, dict) else {}
            stamp = "2026-11-23T12:00:00Z"
            row = {"id": next_id, "created_at": stamp, "updated_at": stamp, **fields}
            posted_by_sha.setdefault(sha, []).insert(0, row)
            return 201, {}, json.dumps(row).encode()
        elif "/statuses" in path:
            sha = path.split("/commits/")[1].split("/")[0]
            initial = [status_by_sha[sha]] if sha in status_by_sha else []
            body = [*posted_by_sha.get(sha, ()), *initial][: forge.PER_PAGE]
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


def test_sweep_priority_rotates_successes_after_pending_and_failures() -> None:
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
    rc = run_sweep(
        replace(cfg, run_number=2),
        transport=_mixed_sweep_transport(heads, status_by_sha, record=record),
        now=lambda: start,
        sleep=lambda _: None,
        out=io.StringIO(),
        err=io.StringIO(),
    )
    assert rc == 0
    check_shas = [
        p.split("/commits/")[1].split("/")[0]
        for m, p in record
        if m == "GET" and "/check-runs" in p
    ]
    assert check_shas == ["d" * 40, heads[2], heads[1], heads[0], heads[4]]


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
    for name, value in _forge_env(tree, answers).items():
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
    for finished in (1, 2, 3, 4):
        subset = list(GATES[:finished])
        checks = [check_item(i, name, "success") for i, name in enumerate(subset, 1)]
        snap = base_answers(checks=checks, statuses=[])
        previous_count = len(_forge_post_records(tree))
        rc, out, err, _ = invoke_with_forge(
            tree,
            monkeypatch,
            snap,
            options=JudgeOptions(
                required=list(GATES), event="pull_request", ref="refs/pull/1/head"
            ),
        )
        assert rc == 0, err
        new_posts = _forge_post_records(tree)[previous_count:]
        if finished < len(GATES):
            assert new_posts == []
        else:
            assert len(new_posts) == 1
            assert "state=success" in new_posts[0]
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


def test_tail_sha_counts_own_gate_result_check_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3: sha target still counts the gate caller result when GITHUB_WORKFLOW_REF is set."""
    gate_suite = 7
    checks = [check_item(1, "Licence", "success", suite_id=gate_suite)]
    runs = [
        wf_run(
            1, event="pull_request", suite_id=gate_suite, path=".github/workflows/licence-check.yml"
        )
    ]
    fake = ReviewTransport()
    fake.answers.update(base_answers(checks=checks, runs=runs))
    out, err = io.StringIO(), io.StringIO()

    def judge(config: required_checks.Config, **kwargs: Any) -> int:
        assert config.self_workflow == ".github/workflows/licence-check.yml"
        return run_judge(config, transport=fake, now=fake.clock.now, **kwargs)

    monkeypatch.setattr(required_checks, "run_judge", judge)
    rc = main(
        [
            "judge",
            "--repository",
            REPO,
            "--event",
            "pull_request",
            "--sha",
            SHA,
            "--required",
            '["Licence"]',
        ],
        env={"GITHUB_WORKFLOW_REF": f"{REPO}/.github/workflows/licence-check.yml@refs/pull/1/head"},
        out=out,
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "OK (success): Licence" in out.getvalue()
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


@pytest.mark.parametrize("previous", [None, "pending", "success", "failure"])
def test_pending_replaces_only_a_terminal_status(previous: str | None) -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", None, status="queued")]}
    if previous is not None:
        fake.answers[STATUSES_URL] = [status_item(1, "required-checks", previous)]
    fake.answers[RUNS_URL] = {"workflow_runs": [wf_run(1, event="pull_request")]}
    rc, out, err, _ = _review_judge(fake, event_name="pull_request")
    assert rc == 0, err
    if previous in ("success", "failure"):
        assert fake.posts == [(REVIEW_POST_PATH, "pending")]
    else:
        assert fake.posts == []
        kept = previous or "no status"
        assert f"No status post: required-checks on {SHA} keeps {kept}; verdict pending" in out


def _expected_capped_sweep_calls(cap: int, setup_calls: int, calls_per_eval: int) -> int:
    if cap <= setup_calls:
        return cap
    rem = (cap - setup_calls) % calls_per_eval
    if rem == 0:
        return cap - 2
    if rem == calls_per_eval - 1:
        return cap - 1
    return cap


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
        # A pull head whose POST cannot reserve verification + withdrawal stops the sweep.
        assert sum(counts.values()) == _expected_capped_sweep_calls(
            cap, setup_calls, calls_per_eval
        ), f"cap={cap}"
        assert counts["post"] == max(0, (cap - setup_calls - 1) // calls_per_eval), f"cap={cap}"


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
    def __init__(
        self,
        *,
        status_history: list[dict] | None = None,
        server_offset: float = 0.0,
    ) -> None:
        self.answers: dict[str, object] = base_answers()
        self.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build")]}
        self.answers[RUNS_URL] = {"workflow_runs": [wf_run(1)]}
        self.replies: dict[str, Iterator[object]] = {}
        self.calls: list[tuple[str, str]] = []
        self.posts: list[tuple[str, str]] = []
        self.post_errors: Iterator[forge.ResponseError | None] = iter(())
        self.clock = Clock(REVIEW_EPOCH)
        self.advance_on_reply: dict[str, Iterator[int]] = {}
        self.before_post: Callable[[], None] | None = None
        self.post_answers: Iterator[object] = iter(())
        self.status_history: list[dict] | None = status_history
        self.server_offset: float = server_offset

    def __call__(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append((method, path))
        if method == "POST":
            return self._post(path, kwargs["fields"])
        if "/check-runs?" in path and path not in self.answers and path not in self.replies:
            path = path.replace("/check-runs?per_page=", "/check-runs?filter=all&per_page=")
        if self.status_history is not None and path.startswith(
            f"repos/{REPO}/commits/{SHA}/statuses?"
        ):
            match = re.search(r"[?&]page=(\d+)", path)
            assert match is not None, path
            page = int(match.group(1))
            start = (page - 1) * forge.PER_PAGE
            answer: object = self.status_history[start : start + forge.PER_PAGE]
        else:
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
        stamp = datetime.fromtimestamp(self.clock.now() + self.server_offset, UTC).isoformat(
            timespec="seconds"
        )
        row = {**status_item(1000 + len(self.posts), fields["context"], updated_at=stamp), **fields}
        if self.status_history is not None and sha == SHA:
            self.status_history.insert(0, row)
        else:
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
    first = status_item(41, PUSH_CONTEXT, "failure", updated_at="2026-11-21T12:00:01Z")
    latest = status_item(42, PUSH_CONTEXT, "failure", updated_at="2026-11-21T12:00:02Z")
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
        status_item(2, PUSH_CONTEXT, "success"),
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
    if error_status == 502 and repeat:
        expected_posts.append((REVIEW_POST_PATH, "pending"))
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


@pytest.mark.parametrize("failed_index", [0, 1], ids=["default-branch", "first-pull"])
def test_sweep_continues_after_check_run_listing_error(failed_index: int) -> None:
    heads = [SHA, "1" * 40, "2" * 40]
    failed = heads[failed_index]
    fake = _review_sweep_transport(heads[1:])
    fake.answers[CHECKS_URL.replace(SHA, failed)] = {}

    rc, err = _review_sweep(fake)

    assert rc == 1
    assert err.count("::error::") == 1 and failed in err and "no list check_runs" in err
    assert fake.posts == [
        (REVIEW_POST_PATH.replace(SHA, sha), "success") for sha in heads if sha != failed
    ]


def test_sweep_api_cap_preserves_evaluation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    heads = ["1" * 40, "2" * 40]
    fake = _review_sweep_transport(heads)
    fake.answers[CHECKS_URL] = {}
    # Setup, triage, then the default head's status and malformed check-run listing.
    cap = 3 + len(heads) + 2
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)

    rc, err = _review_sweep(fake)

    assert rc == 1
    assert err.count("::error::") == 1 and SHA in err
    assert f"::warning::Sweep API call cap of {cap} reached; 2 target(s) not judged." in err
    assert fake.posts == []


def test_sweep_deadline_after_default_branch_stops_before_pulls() -> None:
    pull = "1" * 40
    fake = _review_sweep_transport([pull])
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, SESSION_DEADLINE_SECONDS])

    rc, err = _review_sweep(fake)

    assert rc == 1
    assert f"reached before judging {pull}" in err
    assert ("GET", CHECKS_URL.replace(SHA, pull)) not in fake.calls
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


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
    for run_number, expected in [(1, heads[:30]), (2, heads[15:] + heads[:14])]:
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


@pytest.mark.parametrize(("run_number", "start"), [(1, 0), (2, 1), (10**9, 9)])
def test_review_sweep_keeps_order_at_exact_budget(run_number: int, start: int) -> None:
    heads = [f"{i:040x}" for i in range(1, 31)]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    rc, err = _review_sweep(fake, run_number=run_number, apps_json='{"Build":"external"}')
    assert rc == 0, err
    assert _review_evaluated_pulls(fake) == [*heads[start:], *heads[:start]]


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
    failed = status_item(41, PUSH_CONTEXT, "failure", updated_at=created_at)
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
    assert required_checks._all_statuses(statuses)[PUSH_CONTEXT]["state"] == "failure"


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
        foreign[-1] = status_item(1001, PUSH_CONTEXT, "success")
    fake.replies[STATUSES_URL] = iter([[], [], foreign])
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert fake.calls.count(("GET", CHECKS_URL)) == (1 if own_id_visible else 2)


def test_review_second_contested_failure_warns_without_withdrawal() -> None:
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
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]


def test_review_prepost_owed_status_change_refreshes_verdict() -> None:
    fake = ReviewTransport()
    previously = status_item(1, "Build")
    latest = status_item(2, "Build", "failure", updated_at="2026-11-21T12:00:01Z")
    # GitHub keeps the status history append-only: the posted row lands beside latest.
    fake.answers[STATUSES_URL] = [latest]
    fake.replies[STATUSES_URL] = iter([[previously]])
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


@pytest.mark.parametrize(
    ("stamps", "expected"),
    [
        (("2026-11-21T12:00:00Z", None), 9),
        ((None, None), 9),
        (("2026-11-21T12:00:00Z", "2026-11-21T12:00:00Z"), 9),
    ],
)
def test_check_run_rerun_uses_newer_id_if_unstamped_or_tied(
    stamps: tuple[str | None, str | None], expected: int
) -> None:
    completed = check_item(5, "Build", started_at=stamps[0])
    queued = check_item(9, "Build", None, status="queued", started_at=stamps[1])
    config = _review_config()
    for runs in ([completed, queued], [queued, completed]):
        selected, ignored = required_checks.newest_check_runs(
            runs, {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == expected
        snapshot = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        assert decide(snapshot, config, REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"


@pytest.mark.parametrize(
    ("older_context", "newer_context"),
    [("Build", "build"), ("build", "Build"), ("Build", "Build")],
    ids=["lowercase-newer", "uppercase-newer", "identical"],
)
def test_newest_commit_status_wins_across_context_case(
    older_context: str, newer_context: str
) -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [
        status_item(1, older_context),
        status_item(2, newer_context, "failure"),
    ]
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


def test_check_run_names_stay_case_sensitive() -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {
        "check_runs": [check_item(1, "Build"), check_item(2, "build", "failure")]
    }
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


def test_judge_status_is_self_status_across_context_case() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT.upper())]
    rc, _, err, _ = _review_judge(fake, only_if_pending=True)
    assert rc == 0, err
    assert fake.calls == [("GET", STATUSES_URL)]
    assert fake.posts == []


def test_both_judge_statuses_are_excluded_from_evidence() -> None:
    statuses = [
        status_item(1, "REQUIRED-CHECKS"),
        status_item(2, PUSH_CONTEXT.upper()),
        status_item(3, "Build"),
    ]
    assert required_checks.newest_statuses(statuses, "required-checks") == {"build": statuses[2]}


@pytest.mark.parametrize(
    ("required_json", "message"),
    [
        ('["Build","build"]', "differ only by case"),
        ('["REQUIRED-CHECKS"]', "must not appear in required"),
        ('["Required-Checks (PUSH)"]', "must not appear in required"),
    ],
)
def test_policy_rejects_case_collisions_and_both_self_contexts(
    required_json: str, message: str
) -> None:
    with pytest.raises(InputError, match=message):
        _review_config(required_json=required_json)


def _full_unrelated_status_page() -> list[dict]:
    return [status_item(i, "other") for i in range(1, forge.PER_PAGE + 1)]


def _status_page(page: int) -> str:
    return STATUSES_URL.removesuffix("1") + str(page)


def test_preflight_reads_short_second_page_when_no_judge_status_exists() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = _full_unrelated_status_page()
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    post_index = fake.calls.index(("POST", REVIEW_POST_PATH))
    assert fake.calls[post_index - 2 : post_index] == [
        ("GET", STATUSES_URL),
        ("GET", _status_page(2)),
    ]
    assert fake.calls.count(("GET", _status_page(2))) == 3
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


def test_preflight_compares_judge_status_on_second_page() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = _full_unrelated_status_page()
    older = status_item(101, PUSH_CONTEXT, "failure")
    latest = status_item(102, PUSH_CONTEXT.upper(), "failure")
    fake.replies[_status_page(2)] = iter([[older], [latest], [latest], [latest]])
    fake.replies[CHECKS_URL] = iter(
        [
            {"check_runs": [check_item(1, "Build")]},
            {"check_runs": [check_item(2, "Build", "failure")]},
        ]
    )
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and not err, err
    assert fake.calls.count(("GET", _status_page(2))) == 5
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


def test_preflight_refuses_at_status_page_bound() -> None:
    fake = ReviewTransport()
    for page in range(1, required_checks.STATUS_MAX_PAGES + 1):
        fake.answers[_status_page(page)] = _full_unrelated_status_page()
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0, err
    assert "the newest judge status is outside the pre-post status page" in err
    assert fake.posts == []
    assert fake.calls.count(("GET", _status_page(required_checks.STATUS_MAX_PAGES))) == 4
    assert fake.calls.count(("GET", _status_page(required_checks.STATUS_MAX_PAGES + 1))) == 2


def _contested_status_write(
    fake: ReviewTransport, *, cap: int | None = None, conclusion: str = "success"
) -> tuple[int, str]:
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", conclusion)]}
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=io.StringIO(),
        max_api_calls=cap,
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    assert session.api_calls == len(fake.calls)
    return rc, err.getvalue()


def _competing_push_status() -> dict:
    return status_item(42, PUSH_CONTEXT.upper(), "failure")


class ContestedVerificationTransport(ReviewTransport):
    """Live fake answers, except a newer judge status appears at verification of the first POSTs.

    The default `_post_is_contested` runs unchanged. After an accepted non-pending POST, the
    very next statuses read (the verification read) also lists a newer contender, which is kept
    in the fake's history so later snapshots see it too.
    """

    def __init__(self, contests: int) -> None:
        super().__init__()
        self.contests_left = contests
        self.contested_posts = 0
        self.verify_next = False

    def __call__(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        verifying = self.verify_next
        self.verify_next = False
        reply = super().__call__(path, method, **kwargs)
        if method == "POST":
            # A raised POST error never reaches this line, so only accepted writes are verified.
            self.verify_next = self.contests_left > 0 and kwargs["fields"]["state"] != "pending"
            return reply
        if verifying and path.startswith(f"repos/{REPO}/commits/{SHA}/statuses?"):
            return self._with_contender(path, reply)
        return reply

    def _with_contender(
        self, path: str, reply: tuple[int, dict[str, str], bytes]
    ) -> tuple[int, dict[str, str], bytes]:
        status, headers, body = reply
        rows = json.loads(body)
        own = rows[0]  # the row the fake just created for the accepted POST
        assert own["id"] == 1000 + len(self.posts), own
        self.contests_left -= 1
        self.contested_posts += 1
        # Same server timestamp as the judge's own write, a newer id, and a different state.
        contender = status_item(
            5000 + self.contested_posts,
            own["context"].upper(),
            "failure" if own["state"] != "failure" else "success",
            updated_at=own["created_at"],
        )
        rows = [contender, *rows]
        self.answers[path] = rows
        # Start the next evidence read after the previous status timestamp.
        self.clock.current += 2
        return status, headers, json.dumps(rows).encode()


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_contested_post_at_cap_withdraws_only_success(conclusion: str) -> None:
    fake = ReviewTransport()
    fake.replies[STATUSES_URL] = iter([[], [], [_competing_push_status()]])
    rc, err = _contested_status_write(fake, cap=7, conclusion=conclusion)
    assert rc == 0 and "::warning::Sweep API call cap of 7" in err
    states = ["success", "pending"] if conclusion == "success" else ["failure"]
    assert fake.posts == [(REVIEW_POST_PATH, state) for state in states]
    assert len(fake.calls) == 7
    if conclusion == "success":
        assert (
            fake.answers[STATUSES_URL][0]["description"]
            == "contested write; the next judgment decides"
        )


def test_second_contested_success_is_withdrawn() -> None:
    fake = ReviewTransport()
    contender = _competing_push_status()
    fake.replies[STATUSES_URL] = iter([[], [], [contender], [contender], [contender], [contender]])
    rc, err = _contested_status_write(fake)
    assert rc == 0 and f"::warning::Status write contested again for {SHA}." in err
    assert fake.posts == [(REVIEW_POST_PATH, state) for state in ("success", "success", "pending")]


@pytest.mark.parametrize("withdrawal_fails", [False, True])
def test_contested_success_with_retry_api_failure_withdraws_or_reports_risk(
    withdrawal_fails: bool,
) -> None:
    fake = ReviewTransport()
    fake.replies[STATUSES_URL] = iter([[], [], [_competing_push_status()], {}])
    if withdrawal_fails:
        fake.post_errors = iter([None, forge.ResponseError("blocked", status=403)])
    rc, err = _contested_status_write(fake)
    assert rc == 1
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    if withdrawal_fails:
        assert SHA in err and "::error::" in err and "may rest on superseded evidence" in err
    else:
        assert "::error::" in err and "no list" in err


def test_sweep_thirteen_deep_status_histories_still_evaluates_heads() -> None:
    heads = [f"{i:040x}" for i in range(1, 14)]
    fake = _review_sweep_transport(heads, app="scan-app")
    for sha in heads:
        for page in range(1, required_checks.STATUS_MAX_PAGES):
            fake.answers[_status_page(page).replace(SHA, sha)] = _full_unrelated_status_page()
    rc, err = _review_sweep(fake, apps_json='{"Build":"scan-app"}')
    assert rc == 0, err
    repo_read = fake.calls.index(("GET", f"repos/{REPO}"))
    assert fake.calls[1:repo_read] == [("GET", STATUSES_URL.replace(SHA, sha)) for sha in heads]
    assert fake.posts[0] == (REVIEW_POST_PATH, "success")
    assert fake.posts[1] == (REVIEW_POST_PATH.replace(SHA, heads[0]), "success")
    assert _review_evaluated_pulls(fake)


@pytest.mark.parametrize("case", ["head-error-continues", "error-at-work-cutoff-stops"])
def test_sweep_triage_read_error_ranks_head_unknown(case: str) -> None:
    first, second = "1" * 40, "2" * 40
    fake = _review_sweep_transport([first, second])
    first_statuses = STATUSES_URL.replace(SHA, first)
    second_statuses = STATUSES_URL.replace(SHA, second)
    fake.answers[second_statuses] = [status_item(1, "required-checks")]
    failures_left = len(API_BACKOFF_SECONDS) + 1

    def transport(path: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        nonlocal failures_left
        if path == first_statuses and failures_left:
            failures_left -= 1
            if case == "error-at-work-cutoff-stops":
                fake.clock.sleep(required_checks.WORK_DEADLINE_SECONDS + 1)
            raise forge.ResponseError("bad gateway", status=502)
        return fake(path, **kwargs)

    err = io.StringIO()
    rc = run_sweep(
        _review_config(targets="open-pulls", run_number=1),
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 1
    message = err.getvalue()
    assert message.count("::error::") == 1
    if case == "error-at-work-cutoff-stops":
        assert "::error::Transient GitHub API failure: statuses still answers HTTP 502" in message
        assert "session work cutoff" in message
        assert "ranks as unknown" not in message
        assert fake.posts == []
    else:
        assert f"Triage status read for {first} failed; it ranks as unknown" in message
        assert fake.calls.index(("GET", second_statuses)) < fake.calls.index(
            ("GET", f"repos/{REPO}")
        )
        assert _review_evaluated_pulls(fake) == [first, second]
        assert fake.posts == [
            (REVIEW_POST_PATH.replace(SHA, sha), "success") for sha in (SHA, first, second)
        ]


@pytest.mark.parametrize(
    "case",
    ["default-branch-error-keeps-line", "cutoff-after-kept-error", "cap-after-kept-error"],
)
def test_sweep_keeps_triage_error_when_sweep_stops(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = "1" * 40, "2" * 40
    fake = _review_sweep_transport([first, second])
    first_statuses = STATUSES_URL.replace(SHA, first)
    second_statuses = STATUSES_URL.replace(SHA, second)
    repo_path = f"repos/{REPO}"
    # One open-pulls read, the first head's attempts, and the second head's read.
    cap = 1 + (1 + len(API_BACKOFF_SECONDS)) + 1
    if case == "cap-after-kept-error":
        monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)

    def transport(path: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        reply = fake(path, **kwargs)
        if path == first_statuses or (
            case == "default-branch-error-keeps-line" and path == repo_path
        ):
            raise forge.ResponseError("bad gateway", status=502)
        if case == "cutoff-after-kept-error" and path == second_statuses:
            fake.clock.sleep(required_checks.WORK_DEADLINE_SECONDS + 1)
            raise forge.ResponseError("bad gateway", status=502)
        return reply

    err = io.StringIO()
    rc = run_sweep(
        _review_config(targets="open-pulls", run_number=1),
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    lines = err.getvalue().splitlines()
    assert rc == 1, lines
    assert len(lines) == 2, lines
    assert lines[0].startswith(
        f"::error::Triage status read for {first} failed; it ranks as unknown"
    )
    if case == "cap-after-kept-error":
        assert lines[1] == f"::warning::Sweep API call cap of {cap} reached during repository."
        assert len(fake.calls) == cap and fake.calls[-1] == ("GET", second_statuses)
    elif case == "cutoff-after-kept-error":
        assert lines[1].startswith("::error::") and "session work cutoff" in lines[1]
        assert "ranks as unknown" not in lines[1]
    else:
        assert lines[1].startswith(
            "::error::Transient GitHub API failure: repository still answers HTTP 502"
        )
    assert fake.posts == []


@pytest.mark.parametrize(
    ("error", "elapsed", "stops_triage"),
    [
        (forge.ResponseError("bad gateway", status=502), 420, True),
        (
            forge.ResponseError("rate limit", status=429, headers={"retry-after": "20"}),
            430,
            True,
        ),
        (
            forge.ResponseError("rate limit", status=429, headers={"retry-after": "360"}),
            100,
            True,
        ),
        (
            forge.ResponseError("rate limit", status=429, headers={"retry-after": "19"}),
            430,
            False,
        ),
        (
            forge.ResponseError("rate limit", status=429, headers={"retry-after": "301"}),
            0,
            False,
        ),
    ],
    ids=[
        "502-backoff-hits-cutoff",
        "429-delay-hits-cutoff",
        "429-long-delay-crosses-cutoff",
        "429-delay-one-second-before-cutoff",
        "429-over-wait-budget-before-cutoff",
    ],
)
def test_sweep_triage_retry_delay_crossing_work_deadline_stops_across_heads(
    error: forge.ResponseError, elapsed: int, stops_triage: bool
) -> None:
    first, second, third = "1" * 40, "2" * 40, "3" * 40
    fake = _review_sweep_transport([first, second, third])
    first_statuses = STATUSES_URL.replace(SHA, first)
    second_statuses = STATUSES_URL.replace(SHA, second)
    third_statuses = STATUSES_URL.replace(SHA, third)
    seen_second = 0

    def transport(path: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        nonlocal seen_second
        if path == first_statuses and ("GET", first_statuses) not in fake.calls:
            fake.calls.append(("GET", path))
            raise forge.ResponseError("missing", status=404)
        if path == second_statuses and seen_second == 0:
            seen_second += 1
            fake.clock.current = REVIEW_EPOCH + elapsed
            fake.calls.append(("GET", path))
            raise error
        return fake(path, **kwargs)

    err = io.StringIO()
    rc = run_sweep(
        _review_config(targets="open-pulls", run_number=1),
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    lines = err.getvalue().splitlines()
    assert rc == 1, lines
    assert lines[0].startswith(f"::error::Triage status read for {first} failed")
    if stops_triage:
        assert fake.clock.now() < REVIEW_EPOCH + required_checks.WORK_DEADLINE_SECONDS
        assert ("GET", third_statuses) not in fake.calls
        assert ("GET", f"repos/{REPO}") not in fake.calls
        assert len(lines) == 2 and f"Triage status read for {second}" not in err.getvalue()
        assert fake.posts == []
    else:
        assert ("GET", third_statuses) in fake.calls


def test_sweep_caps_triage_at_exact_retry_attempt_boundary() -> None:
    heads = [f"{i:040x}" for i in range(1, OPEN_PULLS_MAX + 1)]
    fake = _review_sweep_transport(heads)
    paths = [STATUSES_URL.replace(SHA, sha) for sha in heads]
    retry_once = set(paths[:22])
    for path in paths:
        fake.answers[path] = [status_item(1, "REQUIRED-CHECKS")]
    calls: list[str] = []

    def retrying(path: str, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
        calls.append(path)
        if path in retry_once:
            retry_once.remove(path)
            raise forge.ResponseError("rate limited", status=429, headers={"retry-after": "0"})
        return fake(path, **kwargs)

    err = io.StringIO()
    rc = run_sweep(
        _sweep_count_config(),
        transport=retrying,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0, err.getvalue()
    repo_read = calls.index(f"repos/{REPO}")
    assert repo_read - 1 == 120
    assert calls[1:repo_read] == [
        path for path in paths[:98] for _ in range(2 if path in paths[:22] else 1)
    ]
    evaluation_reads = [path for path in calls[repo_read:] if "/check-runs?" in path]
    assert evaluation_reads[:2] == [CHECKS_URL, CHECKS_URL.replace(SHA, heads[98])]


def test_sweep_rejudges_previous_success_after_fork_rerun() -> None:
    fork_sha = "1" * 40
    fake = _review_sweep_transport([fork_sha])
    fake.answers[f"repos/{REPO}/pulls?state=open&per_page=100"] = [
        {"head": {"sha": fork_sha, "repo": {"full_name": "fork/horistes"}}}
    ]
    checks = CHECKS_URL.replace(SHA, fork_sha)
    fork_post = REVIEW_POST_PATH.replace(SHA, fork_sha)
    assert _review_sweep(fake) == (0, "")
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (fork_post, "success")]
    fake.calls.clear()
    assert _review_sweep(fake, run_number=2) == (0, "")
    assert ("GET", checks) in fake.calls
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (fork_post, "success")]
    fake.answers[checks] = {"check_runs": [check_item(2, "Build", "failure")]}
    assert _review_sweep(fake, run_number=3) == (0, "")
    assert fake.posts[-1] == (fork_post, "failure")


def test_sweep_rejudges_default_branch_success_after_rerun() -> None:
    fake = _review_sweep_transport([])
    assert _review_sweep(fake) == (0, "")
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(2, "Build", "failure")]}
    assert _review_sweep(fake, run_number=2) == (0, "")
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]


@pytest.mark.parametrize(
    ("event", "expected_context", "expected_state"),
    [("push", PUSH_CONTEXT, "success"), ("merge_group", "required-checks", "failure")],
)
def test_judge_does_not_reuse_other_event_policy_status(
    event: str, expected_context: str, expected_state: str
) -> None:
    fake = _review_sweep_transport([])
    other_context = PUSH_CONTEXT if event == "merge_group" else "required-checks"
    fake.answers[STATUSES_URL] = [status_item(1, other_context, "success"), status_item(2, "Build")]
    rc, _, err, _ = _review_judge(
        fake,
        event_name=event,
        required_json='["Build","Sign-off"]',
        pull_request_only_json='["Sign-off"]',
        bound_minutes=1,
        only_if_pending=True,
    )
    assert rc == 0, err
    assert fake.posts == [(REVIEW_POST_PATH, expected_state)]
    assert fake.answers[STATUSES_URL][0]["context"] == expected_context


def test_sweep_posts_proposal_failure_when_same_sha_passes_push_policy() -> None:
    fake = _review_sweep_transport([SHA])
    fake.answers[STATUSES_URL] = [status_item(2, "Build")]
    policy = {
        "required_json": '["Build","Sign-off"]',
        "pull_request_only_json": '["Sign-off"]',
        "bound_minutes": 1,
    }
    rc, _, err, _ = _review_judge(fake, **policy)
    assert rc == 0, err
    assert fake.answers[STATUSES_URL][0]["context"] == PUSH_CONTEXT
    rc, err = _review_sweep(fake, **policy)
    assert rc == 0, err
    statuses = required_checks._all_statuses(fake.answers[STATUSES_URL])
    assert statuses[PUSH_CONTEXT]["state"] == "success"
    assert statuses["required-checks"]["state"] == "failure"
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]


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


def _deep_failure_sweep_transport(
    count: int, *, check_pages: int = 5
) -> tuple[ReviewTransport, list[str]]:
    heads = [f"{i:040x}" for i in range(1, count + 1)]
    fake = _review_sweep_transport(heads, conclusion="failure")
    fake.answers[STATUSES_URL] = [
        {**status_item(999, PUSH_CONTEXT, "failure"), "description": "Build: failure"}
    ]
    full_checks = [check_item(i, "Build", "failure") for i in range(1, forge.PER_PAGE + 1)]
    tail_checks = [check_item(forge.PER_PAGE * check_pages + 1, "Build", "failure")]
    for index, sha in enumerate(heads[:OPEN_PULLS_MAX]):
        stamp = f"2026-11-21T11:{index // 60:02d}:{index % 60:02d}Z"
        row = status_item(index + 1, "required-checks", "failure", updated_at=stamp)
        fake.answers[STATUSES_URL.replace(SHA, sha)] = [{**row, "description": "Build: failure"}]
        base = CHECKS_URL.replace(SHA, sha).removesuffix("1")
        for page in range(1, check_pages):
            fake.answers[f"{base}{page}"] = {"check_runs": full_checks}
        fake.answers[f"{base}{check_pages}"] = {"check_runs": tail_checks}
    return fake, heads


def _sweep_judged_pulls(
    fake: ReviewTransport, *, run_number: int, **overrides: Any
) -> tuple[int, list[str], str]:
    out, err = io.StringIO(), io.StringIO()
    cfg = _review_config(targets="open-pulls", run_number=run_number, **overrides)
    rc = run_sweep(
        cfg, transport=fake, now=fake.clock.now, sleep=fake.clock.sleep, out=out, err=err
    )
    judged = re.findall(r"No status post: required-checks on ([0-9a-f]{40}) keeps ", out.getvalue())
    return rc, judged, err.getvalue()


def test_sweep_capped_blocks_reach_all_sixty_unchanged_failures() -> None:
    fake, heads = _deep_failure_sweep_transport(60)
    blocks = 2
    block_size = len(heads) // blocks
    judged_counts: list[int] = []
    seen: set[str] = set()
    min_judged = 26
    bound = (block_size - min_judged + 1) * blocks
    for run_number in range(1, bound + 1):
        rc, judged, err = _sweep_judged_pulls(fake, run_number=run_number)
        assert rc == 0
        assert "Sweep API call cap of 250 reached" in err
        assert min_judged <= len(judged) < block_size
        judged_counts.append(len(judged))
        seen.update(judged)
    assert min(judged_counts) == min_judged
    assert seen == set(heads)
    assert fake.posts == []


def test_sweep_block_rotation_boundaries_and_max_run_number() -> None:
    heads = [f"{i:040x}" for i in range(1, MAX_TARGETS_DEFAULT + 2)]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    seen: list[str] = []
    for run_number in (1, 2):
        rc, judged, err = _sweep_judged_pulls(
            fake, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and err == ""
        seen.extend(judged)
    assert set(seen) == set(heads)

    rc, max_run_judged, err = _sweep_judged_pulls(
        fake, run_number=required_checks.MAX_RUN_NUMBER, apps_json='{"Build":"external"}'
    )
    assert rc == 0 and err == ""
    assert max_run_judged == [heads[30], *heads[15:30], *heads[:14]]


def test_sweep_block_tail_unjudged_in_pass_zero_and_judged_by_pass_s_minus_k() -> None:
    fake, heads = _deep_failure_sweep_transport(60)
    blocks = 2
    s = len(heads) // blocks
    tails = {heads[s - 1], heads[-1]}
    pass_zero_seen: set[str] = set()
    k = s
    for run_number in range(1, blocks + 1):
        rc, judged, err = _sweep_judged_pulls(fake, run_number=run_number)
        assert rc == 0 and "Sweep API call cap of 250 reached" in err
        k = min(k, len(judged))
        pass_zero_seen.update(judged)
    assert k < s
    assert tails.isdisjoint(pass_zero_seen)

    later_seen = set(pass_zero_seen)
    target_pass = s - k
    for run_number in range(blocks + 1, (target_pass + 1) * blocks + 1):
        rc, judged, err = _sweep_judged_pulls(fake, run_number=run_number)
        assert rc == 0 and "Sweep API call cap of 250 reached" in err
        later_seen.update(judged)
    assert tails <= later_seen


def test_sweep_partial_prefix_bound_at_k_zero_and_k_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = [f"{i:040x}" for i in range(1, 5)]
    fake_zero = _review_sweep_transport(heads, app="external", conclusion=None)
    # 1 open-pulls + 4 triage + 2 default-head + 4 default snapshot (no post) = 11 calls -> k = 0.
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 11)
    judged_at_k_zero: set[str] = set()
    for run_number in range(1, (2 + 1) * 2 + 1):
        rc, judged, err = _sweep_judged_pulls(
            fake_zero, run_number=run_number, max_targets=2, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and "2 target(s) not judged." in err
        assert judged == []
        judged_at_k_zero.update(judged)
    assert judged_at_k_zero == set()

    fake_one = _review_sweep_transport(heads, app="external", conclusion=None)
    # 11 + 4 calls for the first pull head's snapshot (no post) = 15 calls -> k = 1.
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 15)
    judged_at_k_one: set[str] = set()
    for run_number in range(1, 2 * 2 + 1):
        rc, judged, err = _sweep_judged_pulls(
            fake_one, run_number=run_number, max_targets=2, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and "1 target(s) not judged." in err
        assert len(judged) == 1
        judged_at_k_one.update(judged)
    assert judged_at_k_one == set(heads)

    adr_text = ADR_PATH.read_text(encoding="utf-8")
    sweep_line = next(
        line for line in adr_text.splitlines() if line.startswith("- **Sweep ordering**")
    )
    defence_line = next(
        line for line in adr_text.splitlines() if line.startswith("- **Defence 2.**")
    )
    assert "1 <= k <= s" in sweep_line and "k = 0" in sweep_line
    assert "1 <= k <= s" in defence_line and "k = 0" in defence_line


def test_contested_failure_then_contested_success_is_withdrawn() -> None:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {
        "check_runs": [check_item(1, "Build", "failure"), check_item(2, "Lint", "success")]
    }
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])

    def interleave_posts() -> None:
        if not fake.posts:
            first = status_item(41, PUSH_CONTEXT, "failure", updated_at="2026-11-21T12:00:00Z")
            fake.answers[STATUSES_URL] = [first]
            fake.answers[CHECKS_URL] = {
                "check_runs": [check_item(3, "Build", "success"), check_item(2, "Lint", "success")]
            }
            return
        fake.before_post = None
        second = status_item(42, PUSH_CONTEXT, "failure", updated_at="2026-11-21T12:00:01Z")
        existing = fake.answers[STATUSES_URL]
        assert isinstance(existing, list)
        fake.answers[STATUSES_URL] = [second, *existing]
        fake.answers[CHECKS_URL] = {
            "check_runs": [check_item(3, "Build", "success"), check_item(4, "Lint", "failure")]
        }

    fake.before_post = interleave_posts
    rc, _, err, _ = _review_judge(fake, required_json='["Build","Lint"]')
    assert rc == 0
    assert f"::warning::Status write contested again for {SHA}." in err
    assert fake.posts == [
        (REVIEW_POST_PATH, "failure"),
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH, "pending"),
    ]
    statuses = fake.answers[STATUSES_URL]
    assert isinstance(statuses, list)
    standing = required_checks._all_statuses(statuses)[PUSH_CONTEXT]
    assert standing["state"] == "pending"
    assert standing["description"] == "contested write; the next judgment decides"


def test_sweep_fills_over_budget_blocks_to_max_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads31 = [f"{i:040x}" for i in range(1, 32)]
    fake31 = _review_sweep_transport(heads31, app="external", conclusion=None)
    expected31 = (
        (1, heads31[:30]),
        (2, heads31[15:] + heads31[:14]),
        (3, [*heads31[1:15], heads31[0], *heads31[15:30]]),
    )
    for run_number, expected in expected31:
        rc, judged, err = _sweep_judged_pulls(
            fake31, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and err == ""
        assert judged == expected

    heads60 = [f"{i:040x}" for i in range(1, 61)]
    fake60 = _review_sweep_transport(heads60, app="external", conclusion=None)
    for run_number, expected in ((1, heads60[:30]), (2, heads60[30:])):
        rc, judged, err = _sweep_judged_pulls(
            fake60, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and err == ""
        assert judged == expected

    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 10_000)
    heads100 = [f"{i:040x}" for i in range(1, 101)]
    fake100 = _review_sweep_transport(heads100, app="external", conclusion=None)
    expected100 = (
        (1, heads100[0:25] + heads100[25:30]),
        (2, heads100[25:50] + heads100[50:55]),
        (3, heads100[50:75] + heads100[75:80]),
        (4, heads100[75:100] + heads100[0:5]),
    )
    for run_number, expected in expected100:
        rc, judged, err = _sweep_judged_pulls(
            fake100, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert rc == 0 and err == ""
        assert judged == expected


def test_sweep_cap_cuts_fill_before_block_members() -> None:
    fake, heads = _deep_failure_sweep_transport(31, check_pages=6)
    rc1, judged1, err1 = _sweep_judged_pulls(fake, run_number=1)
    assert rc1 == 0 and "4 target(s) not judged." in err1
    assert judged1 == [*heads[:15], *heads[15:26]]

    rc2, judged2, err2 = _sweep_judged_pulls(fake, run_number=2)
    assert rc2 == 0 and "4 target(s) not judged." in err2
    assert judged2 == [*heads[15:31], *heads[:10]]


def test_withdrawal_uses_reserved_deadline_window_after_work_cutoff() -> None:
    fake = ReviewTransport()
    post_deadlines: list[float] = []
    reserve = getattr(required_checks, "WITHDRAWAL_RESERVE_SECONDS", 30)

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "POST":
            deadline = kwargs.get("deadline")
            if isinstance(deadline, (int, float)):
                post_deadlines.append(float(deadline))
            reply = fake(path, method=method, **kwargs)
            if len(post_deadlines) == 1:
                fake.clock.current = REVIEW_EPOCH + SESSION_DEADLINE_SECONDS - reserve + 10
            return reply
        return fake(path, method=method, **kwargs)

    out, err = io.StringIO(), io.StringIO()
    rc = run_judge(
        _review_config(),
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=out,
        err=err,
    )
    assert getattr(required_checks, "WITHDRAWAL_RESERVE_SECONDS", None) == 30
    assert rc == 1
    assert f"{SESSION_DEADLINE_SECONDS - reserve} s" in err.getvalue()
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    assert len(post_deadlines) == 2
    assert 0 < post_deadlines[1] <= reserve
    assert post_deadlines[1] == 20.0


def test_withdrawal_refused_past_session_deadline_reports_risk() -> None:
    fake = ReviewTransport()

    def advance_past_session_deadline() -> None:
        fake.before_post = None
        fake.clock.current = REVIEW_EPOCH + SESSION_DEADLINE_SECONDS + 1

    fake.before_post = advance_past_session_deadline
    out, err = io.StringIO(), io.StringIO()
    rc = run_judge(
        _review_config(),
        transport=fake,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=out,
        err=err,
    )
    assert getattr(required_checks, "WITHDRAWAL_RESERVE_SECONDS", None) == 30
    assert rc == 1
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert "a success may rest on superseded evidence" in err.getvalue()


def test_work_cutoff_boundary_allows_one_second_before_and_refuses_at_cutoff() -> None:
    reserve = getattr(required_checks, "WITHDRAWAL_RESERVE_SECONDS", 30)
    cutoff = REVIEW_EPOCH + SESSION_DEADLINE_SECONDS - reserve
    captured: list[float] = []

    def transport(
        path: str, method: str = "GET", **kwargs: object
    ) -> tuple[int, dict[str, str], bytes]:
        deadline = kwargs.get("deadline")
        if isinstance(deadline, (int, float)):
            captured.append(float(deadline))
        return 200, {}, b"[]"

    before = ApiSession(
        deadline=REVIEW_EPOCH + SESSION_DEADLINE_SECONDS,
        now=lambda: cutoff - 1,
        transport=transport,
    )
    assert before.request_json("statuses", STATUSES_URL) == []
    assert captured == [1.0]

    at_cutoff = ApiSession(
        deadline=REVIEW_EPOCH + SESSION_DEADLINE_SECONDS,
        now=lambda: cutoff,
        transport=transport,
    )
    with pytest.raises(ApiFailureError, match=rf"{SESSION_DEADLINE_SECONDS - reserve} s"):
        at_cutoff.request_json("statuses", STATUSES_URL)
    assert captured == [1.0]

    pull = "1" * 40
    sweep_fake = _review_sweep_transport([pull])
    sweep_fake.advance_on_reply[STATUSES_URL] = iter([0, 0, SESSION_DEADLINE_SECONDS - reserve])
    rc, err = _review_sweep(sweep_fake)
    assert rc == 1
    assert f"{SESSION_DEADLINE_SECONDS - reserve} s" in err
    assert f"reached before judging {pull}" in err


def test_check_run_total_order_selects_unstamped_across_permutations() -> None:
    run_5 = check_item(5, "Build", "success", started_at="2026-11-21T12:00:02Z")
    run_6 = check_item(6, "Build", None, status="queued", started_at=None)
    run_9 = check_item(9, "Build", "failure", started_at="2026-11-21T12:00:01Z")
    config = _review_config()
    orders = list(itertools.permutations((run_5, run_6, run_9)))
    for order in orders[:6]:
        selected, ignored = required_checks.newest_check_runs(
            list(order), {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == 6
        snapshot = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        assert decide(snapshot, config, REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"

    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [run_6, run_9, run_5]}
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "pending")]


def test_unstamped_completed_check_run_counts_as_pending() -> None:
    unstamped_ok = check_item(7, "Build", "success", status="completed", started_at=None)
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [unstamped_ok]}
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
    rc, out, err, _ = _review_judge(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "pending")]
    assert "OK (success): Build" not in out

    unstamped_fail = check_item(8, "Build", "failure", status="completed", started_at=None)
    snap = required_checks.Snapshot({"Build": unstamped_fail}, {}, (), REVIEW_EPOCH)
    assert decide(snap, _review_config(), REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"


def test_check_run_equal_stamps_compare_by_id() -> None:
    stamp = "2026-11-21T12:00:05Z"
    older = check_item(4, "Build", "failure", started_at=stamp)
    newer = check_item(8, "Build", "success", started_at=stamp)
    config = _review_config()
    for runs in ([older, newer], [newer, older]):
        selected, ignored = required_checks.newest_check_runs(
            runs, {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == 8
        snap = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        assert decide(snap, config, REVIEW_EPOCH, REVIEW_EPOCH)[0] == "success"

    unstamped_older = check_item(4, "Build", "failure", status="completed", started_at=None)
    unstamped_newer = check_item(8, "Build", "success", status="completed", started_at=None)
    for runs in ([unstamped_older, unstamped_newer], [unstamped_newer, unstamped_older]):
        selected, ignored = required_checks.newest_check_runs(
            runs, {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == 8
        snap = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        assert decide(snap, config, REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"


def _run_non_json_post_case(
    conclusion: str,
) -> tuple[int, str, ReviewTransport, ApiSession]:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", conclusion)]}
    post_count = 0

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal post_count
        if method == "POST":
            post_count += 1
            if post_count == 1:
                fake.posts.append((path, kwargs["fields"]["state"]))
                return 201, {}, b"<html>201 Created</html>"
        return fake(path, method=method, **kwargs)

    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=transport,
        out=io.StringIO(),
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    return rc, err.getvalue(), fake, session


def test_success_post_non_json_body_withdraws_and_reports_decode_error() -> None:
    rc, err, fake, session = _run_non_json_post_case("success")
    assert rc == 1
    assert "post-status" in err and "Expecting value" in err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    assert session.withdrawal_reserved is False
    assert session.api_calls == 6


def test_success_post_422_sets_no_reservation_and_posts_no_withdrawal() -> None:
    fake = ReviewTransport()
    fake.post_errors = iter(
        [forge.ResponseError("POST failed: HTTP 422", status=422, message="Unprocessable Entity")]
    )
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=io.StringIO(),
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    assert rc == 1
    assert "422" in err.getvalue()
    assert session.withdrawal_reserved is False
    assert fake.posts == [(REVIEW_POST_PATH, "success")]


def test_failure_post_non_json_body_posts_no_withdrawal() -> None:
    rc, err, fake, session = _run_non_json_post_case("failure")
    assert rc == 1
    assert "post-status" in err and "Expecting value" in err
    assert session.withdrawal_reserved is False
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]


def test_sweep_cap_counts_unjudged_when_exhausted_by_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    fake = _review_sweep_transport(heads)
    fake.answers[CHECKS_URL] = {}
    cap = 6 + 2
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
    rc, err = _review_sweep(fake)
    assert rc == 1
    assert f"::warning::Sweep API call cap of {cap} reached; 3 target(s) not judged." in err
    assert fake.posts == []
    assert len(fake.calls) == cap


def test_sweep_cap_counts_interrupted_head_as_unjudged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    fake = _review_sweep_transport(heads)
    cap = 6 + 6 + 6 + 2
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
    rc, err = _review_sweep(fake)
    assert rc == 0
    assert f"::warning::Sweep API call cap of {cap} reached; 2 target(s) not judged." in err
    assert fake.posts == [
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH.replace(SHA, heads[0]), "success"),
    ]


def test_sweep_cap_excludes_head_finishing_at_exact_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    settled_desc = "All owed required checks reported success."
    fake = _review_sweep_transport(heads)
    for idx, sha in enumerate(heads):
        row = status_item(
            idx + 1, "required-checks", "success", updated_at=f"2026-11-21T10:00:0{idx}Z"
        )
        fake.answers[STATUSES_URL.replace(SHA, sha)] = [{**row, "description": settled_desc}]
    cap = 6 + 6 + 3 + 3
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
    rc, err = _review_sweep(fake)
    assert rc == 0
    assert f"::warning::Sweep API call cap of {cap} reached; 1 target(s) not judged." in err
    assert len(fake.calls) == cap

    fake_all = _review_sweep_transport(heads)
    for idx, sha in enumerate(heads):
        row = status_item(
            idx + 1, "required-checks", "success", updated_at=f"2026-11-21T10:00:0{idx}Z"
        )
        fake_all.answers[STATUSES_URL.replace(SHA, sha)] = [{**row, "description": settled_desc}]
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap + 3)
    rc_all, err_all = _review_sweep(fake_all)
    assert rc_all == 0 and err_all == ""


def _run_transport_failure_post_case(
    conclusion: str,
) -> tuple[int, str, ReviewTransport, ApiSession]:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", conclusion)]}
    post_count = 0

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal post_count
        if method == "POST":
            post_count += 1
            if post_count == 1:
                fake.posts.append((path, kwargs["fields"]["state"]))
                raise forge.ForgeError(f"POST {path}: timed out after send")
        return fake(path, method=method, **kwargs)

    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=transport,
        out=io.StringIO(),
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    return rc, err.getvalue(), fake, session


def test_success_post_transport_failure_withdraws_and_reports_error() -> None:
    rc, err, fake, session = _run_transport_failure_post_case("success")
    assert rc == 1
    assert "::error::GitHub API error (post-status):" in err
    assert "timed out after send" in err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    assert fake.answers[STATUSES_URL][0]["state"] == "pending"
    assert (
        fake.answers[STATUSES_URL][0]["description"] == "contested write; the next judgment decides"
    )
    assert session.withdrawal_reserved is False
    assert session.api_calls == 6


def test_failure_post_transport_failure_posts_no_withdrawal() -> None:
    rc, err, fake, session = _run_transport_failure_post_case("failure")
    assert rc == 1
    assert "timed out after send" in err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]
    assert session.withdrawal_reserved is False
    assert session.api_calls == 5


def test_success_post_refused_before_send_posts_no_withdrawal() -> None:
    fake = ReviewTransport()
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=io.StringIO(),
        max_api_calls=6,
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    assert rc == 0
    assert "Sweep API call cap of 6" in err.getvalue()
    assert fake.posts == []
    assert session.withdrawal_reserved is False
    assert session.api_calls == 4

    fake_cut = ReviewTransport()
    fake_cut.advance_on_reply[STATUSES_URL] = iter([0, 5])
    session_cut = ApiSession(
        deadline=fake_cut.clock.now() + required_checks.WITHDRAWAL_RESERVE_SECONDS + 5,
        now=fake_cut.clock.now,
        sleep=fake_cut.clock.sleep,
        transport=fake_cut,
        out=io.StringIO(),
    )
    err_cut = io.StringIO()
    rc_cut = required_checks.evaluate_one(session_cut, _review_config(), io.StringIO(), err_cut)
    assert rc_cut == 1
    assert "reached during post-status" in err_cut.getvalue()
    assert not any(method == "POST" for method, _ in fake_cut.calls)
    assert fake_cut.posts == []
    assert session_cut.withdrawal_reserved is False


def test_sweep_rotates_single_block_within_max_targets() -> None:
    heads = [f"{i:040x}" for i in range(1, 31)]
    fake = _review_sweep_transport(heads, app="external", conclusion=None)
    fake.answers[STATUSES_URL.replace(SHA, heads[0])] = [
        status_item(1, "required-checks", "pending", updated_at="2026-11-21T11:00:00Z")
    ]
    fake.answers[STATUSES_URL.replace(SHA, heads[1])] = [
        status_item(2, "required-checks", "pending", updated_at="2026-11-21T10:00:00Z")
    ]
    priority = [*heads[2:], heads[1], heads[0]]

    rc1, judged1, err1 = _sweep_judged_pulls(
        fake, run_number=1, max_targets=30, apps_json='{"Build":"external"}'
    )
    assert rc1 == 0 and err1 == ""
    assert judged1 == priority

    rc2, judged2, err2 = _sweep_judged_pulls(
        fake, run_number=2, max_targets=30, apps_json='{"Build":"external"}'
    )
    assert rc2 == 0 and err2 == ""
    assert judged2 == [*priority[1:], priority[0]]


def test_sweep_single_and_empty_candidate_rotation_boundaries() -> None:
    only = "1" * 40
    fake_one = _review_sweep_transport([only], app="external", conclusion=None)
    for run_number in (1, 2, 7):
        rc, judged, err = _sweep_judged_pulls(
            fake_one, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert (rc, judged, err) == (0, [only], "")

    fake_empty = _review_sweep_transport([], app="external", conclusion=None)
    for run_number in (1, 2):
        rc, judged, err = _sweep_judged_pulls(
            fake_empty, run_number=run_number, apps_json='{"Build":"external"}'
        )
        assert (rc, judged, err) == (0, [], "")


def test_sweep_capped_thirty_heads_rotates_uncovered_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, heads = _deep_failure_sweep_transport(30, check_pages=6)
    rc1, judged1, err1 = _sweep_judged_pulls(fake, run_number=1, max_targets=30)
    rc2, judged2, err2 = _sweep_judged_pulls(fake, run_number=2, max_targets=30)
    assert rc1 == 0 and "4 target(s) not judged." in err1 and len(judged1) == 26
    assert rc2 == 0 and "4 target(s) not judged." in err2 and len(judged2) == 26
    assert judged1 == heads[:26] and judged2 == heads[1:27]
    uncovered1 = [sha for sha in heads if sha not in judged1]
    uncovered2 = [sha for sha in heads if sha not in judged2]
    assert uncovered1 == heads[26:30]
    assert uncovered2 == [heads[0], *heads[27:30]]
    assert uncovered1 != uncovered2

    seen = set(judged1) | set(judged2)
    for run_number in range(3, 30 - 26 + 2):
        rc_n, judged_n, _ = _sweep_judged_pulls(fake, run_number=run_number, max_targets=30)
        assert rc_n == 0 and len(judged_n) == 26
        seen.update(judged_n)
    assert seen == set(heads)

    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 36 + 29 * 8 + 1)
    _, judged29_1, _ = _sweep_judged_pulls(fake, run_number=1, max_targets=30)
    _, judged29_2, _ = _sweep_judged_pulls(fake, run_number=2, max_targets=30)
    assert len(judged29_1) == 29 and len(judged29_2) == 29
    assert set(judged29_1) | set(judged29_2) == set(heads)


def test_sweep_cap_on_refused_post_stops_before_next_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    fake = _review_sweep_transport(heads)
    cap = 18
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
    rc, err = _review_sweep(fake)
    assert rc == 0
    assert f"::warning::Sweep API call cap of {cap} reached; 3 target(s) not judged." in err
    assert not any(heads[1] in path or heads[2] in path for _, path in fake.calls[6:])
    assert len(fake.calls) == 16

    fake_err = _review_sweep_transport(heads)
    fake_err.answers[CHECKS_URL] = {}
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 14)
    rc_err, err_out = _review_sweep(fake_err)
    assert rc_err == 1
    assert "::warning::Sweep API call cap of 14 reached; 3 target(s) not judged." in err_out
    assert not any(heads[1] in path or heads[2] in path for _, path in fake_err.calls[6:])


def test_sweep_cap_on_refused_post_skips_404_on_next_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    fake = _review_sweep_transport(heads)
    head2_statuses = STATUSES_URL.replace(SHA, heads[1])
    seen_head2 = 0

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal seen_head2
        if method == "GET" and path == head2_statuses:
            seen_head2 += 1
            if seen_head2 >= 2:
                raise forge.ResponseError("GET statuses: HTTP 404", status=404, message="Not Found")
        return fake(path, method=method, **kwargs)

    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", 18)
    err = io.StringIO()
    cfg = _review_config(targets="open-pulls", run_number=1)
    rc = run_sweep(
        cfg,
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0
    assert "::error::" not in err.getvalue() and "404" not in err.getvalue()
    assert "::warning::Sweep API call cap of 18 reached; 3 target(s) not judged." in err.getvalue()
    assert seen_head2 == 1


@pytest.mark.parametrize("reserved_left", [1, 2], ids=["one-reserved-call", "two-reserved-calls"])
def test_sweep_default_branch_reserved_post_cap_stops_before_pulls(
    reserved_left: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    heads = ["1" * 40, "2" * 40, "3" * 40]
    fake = _review_sweep_transport(heads)
    head1_statuses = STATUSES_URL.replace(SHA, heads[0])
    seen_head1 = 0

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal seen_head1
        if method == "GET" and path == head1_statuses:
            seen_head1 += 1
            if seen_head1 >= 2:
                raise forge.ResponseError("GET statuses: HTTP 404", status=404, message="Not Found")
        return fake(path, method=method, **kwargs)

    # 1 open-pulls + 3 triage + 2 default-head + 3 default snapshot + 1 preflight = 10 GETs.
    cap = 10 + reserved_left
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap)
    err = io.StringIO()
    rc = run_sweep(
        _review_config(targets="open-pulls", run_number=1),
        transport=transport,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0, err.getvalue()
    assert "::error::" not in err.getvalue()
    assert f"::warning::Sweep API call cap of {cap} reached during post-status." in err.getvalue()
    assert (
        f"::warning::Sweep API call cap of {cap} reached; 3 target(s) not judged." in err.getvalue()
    )
    assert seen_head1 == 1
    assert fake.posts == []
    assert len(fake.calls) == 10

    fake_empty = _review_sweep_transport([])
    cap_empty = 7 + reserved_left
    monkeypatch.setattr(required_checks, "SWEEP_MAX_CALLS", cap_empty)
    rc_empty, err_empty = _review_sweep(fake_empty)
    assert rc_empty == 0
    assert f"Sweep API call cap of {cap_empty} reached during post-status." in err_empty
    assert "not judged" not in err_empty
    assert len(fake_empty.calls) == 7


def test_short_verification_page_missing_own_id_retries_then_withdraws() -> None:
    other = [status_item(5, "other")]
    fake_once = ReviewTransport()
    fake_once.replies[STATUSES_URL] = iter([[], [], other])
    rc1, err1 = _contested_status_write(fake_once)
    assert rc1 == 0 and err1 == ""
    assert fake_once.calls.count(("GET", CHECKS_URL)) == 2
    assert fake_once.posts == [(REVIEW_POST_PATH, "success")]

    fake_twice = ReviewTransport()
    fake_twice.replies[STATUSES_URL] = iter([[], [], other, [], [], other])
    rc2, err2 = _contested_status_write(fake_twice)
    assert rc2 == 0
    assert f"::warning::Status write contested again for {SHA}." in err2
    assert fake_twice.posts == [
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH, "pending"),
    ]
    assert fake_twice.answers[STATUSES_URL][0]["state"] == "pending"
    assert (
        fake_twice.answers[STATUSES_URL][0]["description"]
        == "contested write; the next judgment decides"
    )


def test_short_verification_page_with_own_id_accepts_without_reread() -> None:
    fake = ReviewTransport()
    short_with_own = [status_item(5, "other"), status_item(1001, PUSH_CONTEXT, "success")]
    fake.replies[STATUSES_URL] = iter([[], [], short_with_own])
    rc, err = _contested_status_write(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert fake.calls.count(("GET", CHECKS_URL)) == 1
    assert _statuses_get_count([path for _, path in fake.calls]) == 3


def _run_post_response_error_case(
    errors: list[forge.ResponseError], *, conclusion: str = "success"
) -> tuple[int, str, str, ReviewTransport, ApiSession]:
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", conclusion)]}
    fake.post_errors = iter(errors)
    out, err = io.StringIO(), io.StringIO()
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=out,
    )
    rc = required_checks.evaluate_one(session, _review_config(), out, err)
    return rc, out.getvalue(), err.getvalue(), fake, session


def test_success_post_response_error_without_status_withdraws_and_reports_error() -> None:
    error = forge.ResponseError(
        f"POST {REVIEW_POST_PATH}: TLS connection reset",
        status=None,
        message="TLS connection reset",
    )
    rc, _, err, fake, session = _run_post_response_error_case([error])
    assert rc == 1
    assert "::error::GitHub API error (post-status):" in err
    assert "TLS connection reset" in err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    assert fake.answers[STATUSES_URL][0]["state"] == "pending"
    assert (
        fake.answers[STATUSES_URL][0]["description"] == "contested write; the next judgment decides"
    )
    assert session.withdrawal_reserved is False
    assert session.api_calls == 6
    assert required_checks.TransportFailureError.__doc__ == (
        "A transport attempt ended without an HTTP answer; the request may have reached GitHub."
    )


def test_success_post_502_twice_withdraws_after_retry_and_reports_error() -> None:
    err_502 = forge.ResponseError(
        "POST failed: HTTP 502: Bad Gateway", status=502, message="Bad Gateway"
    )
    rc, _, err, fake, session = _run_post_response_error_case([err_502, err_502])
    assert rc == 1
    assert fake.clock.sleeps == [30]
    assert "::error::GitHub API error (post-status):" in err
    assert "HTTP 502" in err and "Bad Gateway" in err
    assert fake.posts == [
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH, "success"),
        (REVIEW_POST_PATH, "pending"),
    ]
    assert fake.answers[STATUSES_URL][0]["state"] == "pending"
    assert (
        fake.answers[STATUSES_URL][0]["description"] == "contested write; the next judgment decides"
    )
    assert session.withdrawal_reserved is False
    assert session.api_calls == 11


def _stored_502_case(
    *, only_if_pending: bool, retry_conclusion: str = "success"
) -> tuple[int, str, str, ReviewTransport, ApiSession, list[bool]]:
    fake = ReviewTransport()
    fake.replies[CHECKS_URL] = iter(
        [
            {"check_runs": [check_item(1, "Build", "success")]},
            {"check_runs": [check_item(2, "Build", retry_conclusion)]},
        ]
    )
    reserved_on_get: list[bool] = []
    session: ApiSession

    def store_then_502(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and len(fake.posts) == 1:
            reserved_on_get.append(session.withdrawal_reserved)
        reply = fake(path, method=method, **kwargs)
        if method == "POST" and len(fake.posts) == 1:
            raise forge.ResponseError("POST failed: HTTP 502", status=502, message="Bad Gateway")
        return reply

    out, err = io.StringIO(), io.StringIO()
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=store_then_502,
        out=out,
    )
    cfg = _review_config(only_if_pending=only_if_pending)
    rc = required_checks.evaluate_one(session, cfg, out, err)
    return rc, out.getvalue(), err.getvalue(), fake, session, reserved_on_get


def test_success_post_502_stored_by_github_clears_reservation_only_on_fresh_decision() -> None:
    rc, out, err, fake, session, reserved_on_get = _stored_502_case(only_if_pending=False)
    assert rc == 0 and err == ""
    assert fake.clock.sleeps == [30]
    assert reserved_on_get == [True, True, True, True]
    assert session.withdrawal_reserved is False
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert f"No status post: {PUSH_CONTEXT} on {SHA} keeps success; verdict success" in out
    assert fake.answers[STATUSES_URL][0]["state"] == "success"
    assert session.api_calls == 9

    rc_skip, out_skip, err_skip, fake_skip, session_skip, skip_gets = _stored_502_case(
        only_if_pending=True
    )
    assert rc_skip == 0 and err_skip == ""
    assert fake_skip.clock.sleeps == [30]
    assert skip_gets == [True, True, True, True]
    assert session_skip.withdrawal_reserved is False
    assert fake_skip.posts == [(REVIEW_POST_PATH, "success")]
    assert f"No status post: {PUSH_CONTEXT} on {SHA} keeps success; verdict success" in out_skip
    assert fake_skip.answers[STATUSES_URL][0]["state"] == "success"
    assert session_skip.api_calls == 9


def test_failure_post_response_error_without_status_posts_no_withdrawal() -> None:
    error = forge.ResponseError(
        f"POST {REVIEW_POST_PATH}: TLS connection reset",
        status=None,
        message="TLS connection reset",
    )
    rc, _, err, fake, session = _run_post_response_error_case([error], conclusion="failure")
    assert rc == 1
    assert "TLS connection reset" in err
    assert fake.posts == [(REVIEW_POST_PATH, "failure")]
    assert session.withdrawal_reserved is False
    assert session.api_calls == 5


@pytest.mark.parametrize(
    ("status", "repeats", "expected_sleeps", "expected_states", "expected_calls"),
    [
        (399, 1, [], ["success", "pending"], 6),
        (400, 1, [], ["success"], 5),
        (499, 1, [], ["success"], 5),
        (500, 2, [30], ["success", "success", "pending"], 11),
    ],
)
def test_success_post_definitive_rejection_status_boundaries(
    status: int,
    repeats: int,
    expected_sleeps: list[float],
    expected_states: list[str],
    expected_calls: int,
) -> None:
    err_item = forge.ResponseError(
        f"POST failed: HTTP {status}", status=status, message=f"status {status}"
    )
    rc, _, err, fake, session = _run_post_response_error_case([err_item] * repeats)
    assert rc == 1
    assert f"HTTP {status}" in err
    assert fake.clock.sleeps == expected_sleeps
    assert fake.posts == [(REVIEW_POST_PATH, state) for state in expected_states]
    assert session.withdrawal_reserved is False
    assert session.api_calls == expected_calls


def test_check_run_missing_started_at_with_other_stamps_counts_as_pending() -> None:
    unstamped = check_item(7, "Build", "success", status="completed", started_at=None)
    unstamped["created_at"] = "2026-11-21T12:00:00Z"
    unstamped["completed_at"] = "2026-11-21T12:10:00Z"
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [unstamped]}
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
    rc, out, err, _ = _review_judge(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "pending")]
    assert "OK (success): Build" not in out

    snap = required_checks.Snapshot({"Build": unstamped}, {}, (), REVIEW_EPOCH)
    assert decide(snap, _review_config(), REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"


def test_check_run_missing_started_at_beats_queued_rerun_and_stays_pending() -> None:
    run_a = check_item(5, "Build", "success", status="completed", started_at=None)
    run_a["completed_at"] = "2026-11-21T12:10:00Z"
    rerun_b = check_item(9, "Build", None, status="queued", started_at="2026-11-21T12:05:00Z")
    config = _review_config()
    for runs in ([run_a, rerun_b], [rerun_b, run_a]):
        selected, ignored = required_checks.newest_check_runs(
            runs, {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == 5
        snap = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        verdict, _ = decide(snap, config, REVIEW_EPOCH, REVIEW_EPOCH)
        assert verdict == "pending"
        fake = ReviewTransport()
        fake.answers[CHECKS_URL] = {"check_runs": runs}
        fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
        rc, _, err, _ = _review_judge(fake)
        assert rc == 0 and err == ""
        assert fake.posts == [(REVIEW_POST_PATH, "pending")]


def test_check_run_started_at_judges_and_orders_ignoring_completed_at() -> None:
    lone = check_item(5, "Build", "success", status="completed", started_at="2026-11-21T12:00:00Z")
    lone["completed_at"] = "2026-11-21T12:10:00Z"
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [lone]}
    rc, out, err, _ = _review_judge(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert "OK (success): Build" in out

    run_a = check_item(5, "Build", "success", status="completed", started_at="2026-11-21T12:00:00Z")
    run_a["completed_at"] = "2026-11-21T12:10:00Z"
    run_b = check_item(9, "Build", "failure", status="completed", started_at="2026-11-21T12:05:00Z")
    run_b["completed_at"] = "2026-11-21T12:06:00Z"
    config = _review_config()
    for runs in ([run_a, run_b], [run_b, run_a]):
        selected, ignored = required_checks.newest_check_runs(
            runs, {1}, {}, frozenset({"Build"}), "required-checks"
        )
        assert selected["Build"]["id"] == 9
        snap = required_checks.Snapshot(selected, {}, ignored, REVIEW_EPOCH)
        assert decide(snap, config, REVIEW_EPOCH, REVIEW_EPOCH)[0] == "failure"
        fake_order = ReviewTransport()
        fake_order.answers[CHECKS_URL] = {"check_runs": runs}
        rc_order, _, err_order, _ = _review_judge(fake_order)
        assert rc_order == 0 and err_order == ""
        assert fake_order.posts == [(REVIEW_POST_PATH, "failure")]


@pytest.mark.parametrize(
    "bad_started_at",
    ["", "not-a-time", "2026-11-21T12:00:00"],
    ids=["empty", "unparsable", "zoneless"],
)
def test_check_run_unparsable_started_at_with_completed_at_counts_as_pending(
    bad_started_at: str,
) -> None:
    run = check_item(7, "Build", "success", status="completed", started_at=bad_started_at)
    run["completed_at"] = "2026-11-21T12:10:00Z"
    assert required_checks._check_run_key(run) == (1, 0.0, 7)
    fake = ReviewTransport()
    fake.answers[CHECKS_URL] = {"check_runs": [run]}
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
    rc, out, err, _ = _review_judge(fake)
    assert rc == 0 and err == ""
    assert fake.posts == [(REVIEW_POST_PATH, "pending")]
    assert "OK (success): Build" not in out
    snap = required_checks.Snapshot({"Build": run}, {}, (), REVIEW_EPOCH)
    assert decide(snap, _review_config(), REVIEW_EPOCH, REVIEW_EPOCH)[0] == "pending"


def _prepend_external_failure(fake: ReviewTransport) -> None:
    fake.before_post = None
    existing = fake.answers[STATUSES_URL]
    assert isinstance(existing, list)
    failed = status_item(2, "External", "failure", updated_at="2026-11-21T12:00:00Z")
    fake.answers[STATUSES_URL] = [failed, *existing]


def test_post_verification_reevaluates_when_required_status_changes() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [status_item(1, "External", "success")]
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])
    fake.before_post = lambda: _prepend_external_failure(fake)
    rc, out, err, _ = _review_judge(fake, required_json='["Build", "External"]')
    assert rc == 0 and err == ""
    assert fake.calls.count(("GET", CHECKS_URL)) == 2
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]
    statuses = fake.answers[STATUSES_URL]
    assert isinstance(statuses, list)
    assert required_checks._all_statuses(statuses)[PUSH_CONTEXT]["state"] == "failure"
    assert "External: status failure" in out


def test_post_verification_keeps_success_when_required_status_unchanged() -> None:
    baseline = ReviewTransport()
    rc_base, _, err_base, _ = _review_judge(baseline)
    assert rc_base == 0 and err_base == ""

    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [status_item(1, "External", "success")]
    rc, _, err, _ = _review_judge(fake, required_json='["Build", "External"]')
    assert rc == 0 and err == ""
    assert fake.calls.count(("GET", CHECKS_URL)) == 1
    assert fake.posts == [(REVIEW_POST_PATH, "success")]
    assert len(fake.calls) == len(baseline.calls) == 6


@dataclass(frozen=True)
class _ServerClockCase:
    server_offset: float
    checks_elapsed: int
    competitor_delta: int
    expected_states: tuple[str, ...]


def _iso_seconds(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


@pytest.mark.parametrize(
    "case",
    [
        _ServerClockCase(-5.0, 0, 0, ("success", "failure")),
        _ServerClockCase(-5.0, 0, -1, ("success",)),
        _ServerClockCase(0.0, 3, 1, ("success", "failure")),
    ],
    ids=[
        "runner-ahead-competitor-after-preflight",
        "runner-ahead-competitor-before-read-start",
        "competitor-mid-window-elapsed",
    ],
)
def test_post_verification_contender_uses_server_clock(case: _ServerClockCase) -> None:
    fake = ReviewTransport(server_offset=case.server_offset)
    server_read_start = fake.clock.now() + fake.server_offset
    if case.checks_elapsed:
        fake.advance_on_reply[CHECKS_URL] = iter([case.checks_elapsed])
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])

    def interleave() -> None:
        stamp = (
            _iso_seconds(fake.clock.now() + fake.server_offset)
            if case.competitor_delta == 0
            else _iso_seconds(server_read_start + case.competitor_delta)
        )
        _interleave_sweep_failure(fake, stamp)

    fake.before_post = interleave
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0 and err == "", err
    assert fake.posts == [(REVIEW_POST_PATH, state) for state in case.expected_states]


@pytest.mark.parametrize(
    "mode",
    [
        "short-page-external-gone",
        "full-page-external-unchanged-on-page-two",
        "full-page-external-failed-on-page-two",
        "full-page-bound-unconfirmed",
    ],
)
def test_post_verification_absent_required_status_depends_on_page_completeness(
    mode: str,
) -> None:
    fake = ReviewTransport(status_history=[status_item(1, "External", "success")])
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])

    def mutate_before_post() -> None:
        fake.before_post = None
        assert fake.status_history is not None
        if mode == "short-page-external-gone":
            fake.status_history = [status_item(10, "other")]
            return
        if mode == "full-page-external-unchanged-on-page-two":
            others = [status_item(10 + idx, "other") for idx in range(forge.PER_PAGE)]
            fake.status_history = [*others, *fake.status_history]
            return
        if mode == "full-page-external-failed-on-page-two":
            failed = status_item(2, "External", "failure")
            others = [status_item(10 + idx, "other") for idx in range(forge.PER_PAGE)]
            fake.status_history = [*others, failed, *fake.status_history]
            return
        bound_count = required_checks.STATUS_MAX_PAGES * forge.PER_PAGE
        others = [status_item(10 + idx, "other") for idx in range(bound_count)]
        fake.status_history = [*others, *fake.status_history]

    fake.before_post = mutate_before_post
    rc, _, err, _ = _review_judge(fake, required_json='["Build", "External"]')
    assert fake.status_history is not None
    standing = required_checks._all_statuses(fake.status_history)[PUSH_CONTEXT]["state"]
    if mode == "short-page-external-gone":
        assert rc == 0 and err == "", err
        assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
        assert standing == "pending"
    elif mode == "full-page-external-unchanged-on-page-two":
        assert rc == 0 and err == "", err
        assert fake.posts == [(REVIEW_POST_PATH, "success")]
        assert standing == "success"
        assert fake.calls.count(("GET", CHECKS_URL)) == 1
        post_index = fake.calls.index(("POST", REVIEW_POST_PATH))
        assert ("GET", _status_page(2)) in fake.calls[post_index + 1 :]
    elif mode == "full-page-external-failed-on-page-two":
        assert rc == 0 and err == "", err
        assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]
        assert standing == "failure"
    else:
        assert rc == 1 and "more than" in err
        assert standing != "success"
        assert standing == "pending"
        assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]


def test_post_verification_reads_contender_displaced_to_page_two() -> None:
    fake = ReviewTransport(status_history=[])
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 1])

    def displace_contender_to_page_two() -> None:
        fake.before_post = None
        assert fake.status_history is not None
        stamp = _iso_seconds(fake.clock.now() + fake.server_offset)
        failed = status_item(41, PUSH_CONTEXT, "failure", updated_at=stamp)
        others = [
            status_item(100 + idx, "other", updated_at=stamp) for idx in range(forge.PER_PAGE)
        ]
        fake.status_history = [*others, failed, *fake.status_history]
        fake.answers[CHECKS_URL] = {"check_runs": [check_item(2, "Build", "failure")]}

    fake.before_post = displace_contender_to_page_two
    rc, _, err, _ = _review_judge(
        fake,
        required_json='["Build"]',
        apps_json='{"Build": "github-actions"}',
    )
    assert rc == 0 and err == "", err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]


@dataclass(frozen=True)
class _SnapshotJudgeStatusCase:
    prior_state: str
    keep_id: bool = True
    competitor: str = ""
    expected_states: tuple[str, ...] = ("success",)
    expected_checks_gets: int = 1
    expect_clean_err: bool = True


def _interleave_idless_sweep_failure(fake: ReviewTransport, created_at: str) -> None:
    fake.before_post = None
    assert fake.calls[-2] == ("GET", STATUSES_URL)
    fake.clock.current = max(fake.clock.now(), required_checks._iso_epoch(created_at))
    failed = status_item(41, PUSH_CONTEXT, "failure", updated_at=created_at)
    failed.pop("id")
    existing = fake.answers[STATUSES_URL]
    assert isinstance(existing, list)
    fake.answers[STATUSES_URL] = [failed, *existing]
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(2, "Build", "failure")]}


@pytest.mark.parametrize(
    "case",
    [
        _SnapshotJudgeStatusCase("failure"),
        _SnapshotJudgeStatusCase("pending"),
        _SnapshotJudgeStatusCase(
            "failure",
            competitor="id",
            expected_states=("success", "failure"),
            expected_checks_gets=2,
            expect_clean_err=False,
        ),
        _SnapshotJudgeStatusCase(
            "failure",
            keep_id=False,
            competitor="idless",
            expected_states=("success", "failure"),
            expected_checks_gets=2,
            expect_clean_err=False,
        ),
    ],
    ids=[
        "prior-failure-in-read-second",
        "prior-pending-in-read-second",
        "prior-plus-competitor",
        "id-less-prior-and-competitor",
    ],
)
def test_post_verification_ignores_snapshot_judge_status(case: _SnapshotJudgeStatusCase) -> None:
    fake = ReviewTransport()
    prior = status_item(40, PUSH_CONTEXT, case.prior_state)
    if not case.keep_id:
        prior.pop("id")
    fake.answers[STATUSES_URL] = [prior]
    if case.competitor == "id":
        fake.before_post = lambda: _interleave_sweep_failure(fake, "2026-11-21T12:00:00Z")
    elif case.competitor == "idless":
        fake.before_post = lambda: _interleave_idless_sweep_failure(fake, "2026-11-21T12:00:00Z")
    rc, _, err, _ = _review_judge(fake)
    assert rc == 0
    if case.expect_clean_err:
        assert err == ""
    assert fake.posts == [(REVIEW_POST_PATH, state) for state in case.expected_states]
    assert fake.calls.count(("GET", CHECKS_URL)) == case.expected_checks_gets


def test_post_verification_required_status_change_withdraws_when_retry_fails() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [status_item(1, "External", "success")]
    fake.advance_on_reply[STATUSES_URL] = iter([0, 0, required_checks.WORK_DEADLINE_SECONDS])
    fake.before_post = lambda: _prepend_external_failure(fake)
    rc, _, err, _ = _review_judge(fake, required_json='["Build", "External"]')
    assert rc == 1
    assert "Session deadline work cutoff" in err
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    statuses = fake.answers[STATUSES_URL]
    assert isinstance(statuses, list)
    assert required_checks._all_statuses(statuses)[PUSH_CONTEXT]["state"] == "pending"


def test_reserved_withdrawal_bypasses_settled_shortcut_and_posts_failure() -> None:
    rc, out, err, fake, session, reserved_on_get = _stored_502_case(
        only_if_pending=True, retry_conclusion="failure"
    )
    assert rc == 0 and err == ""
    assert fake.clock.sleeps == [30]
    assert reserved_on_get == [True, True, True, True]
    assert session.withdrawal_reserved is False
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "failure")]
    assert fake.answers[STATUSES_URL][0]["state"] == "failure"
    assert "Build: failure" in out


def test_settled_success_shortcut_skips_checks_when_unreserved() -> None:
    fake = ReviewTransport()
    fake.answers[STATUSES_URL] = [status_item(1, PUSH_CONTEXT, "success")]
    out, err = io.StringIO(), io.StringIO()
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=out,
    )
    assert session.withdrawal_reserved is False
    rc = required_checks.evaluate_one(session, _review_config(only_if_pending=True), out, err)
    assert rc == 0 and err.getvalue() == ""
    assert f"{SHA}: {PUSH_CONTEXT} already settled" in out.getvalue()
    assert fake.calls == [("GET", STATUSES_URL)]
    assert fake.posts == []
    assert session.withdrawal_reserved is False


def test_withdrawal_supersedes_concurrent_success_when_ownership_is_ambiguous() -> None:
    fake = ReviewTransport()
    post_count = 0

    def transport(
        path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        nonlocal post_count
        if method == "POST" and post_count == 0:
            post_count += 1
            fake.calls.append((method, path))
            fake.posts.append((path, kwargs["fields"]["state"]))
            fake.answers[STATUSES_URL] = [status_item(42, PUSH_CONTEXT, "success")]
            raise forge.ForgeError(f"POST {path}: timed out after send")
        return fake(path, method=method, **kwargs)

    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=transport,
        out=io.StringIO(),
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    assert rc == 1
    assert "timed out after send" in err.getvalue()
    assert fake.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    post_indices = [idx for idx, (method, _) in enumerate(fake.calls) if method == "POST"]
    assert len(post_indices) == 2
    between = fake.calls[post_indices[0] + 1 : post_indices[1]]
    assert not any(method == "GET" and "/statuses" in path for method, path in between)
    assert between == []
    statuses = fake.answers[STATUSES_URL]
    assert isinstance(statuses, list)
    standing = required_checks._all_statuses(statuses)[PUSH_CONTEXT.casefold()]
    assert standing["state"] == "pending"
    assert standing["description"] == "contested write; the next judgment decides"
    assert session.withdrawal_reserved is False


@dataclass(frozen=True)
class _UnconfirmedPostCase:
    conclusion: str
    status: int
    ending: str
    expect_withdrawal: bool


def _configure_warning_ending(fake: ReviewTransport, case: _UnconfirmedPostCase) -> int | None:
    if case.ending == "cap":
        # Attempt 1 spends 3 snapshot GETs + 1 preflight GET = 4 calls. The verdict POST
        # needs 2 reserved calls (4 + 2 < 7), so it is really sent and answered with the
        # 502/429 (call 5). Attempt 2 then reads statuses (call 6) and the cap stops it at
        # the checks read (reserved case) or the workflow-run read (unreserved case). A cap
        # of 6 would refuse the first POST and the injected error would never happen.
        # The reserved withdrawal is sent with reserve 0 at call 6 < 7.
        return 7
    if case.ending == "stale":
        fake.advance_on_reply[STATUSES_URL] = iter([0, 0, 11, 11])
        return None
    # Attempt 1: the injected POST error. Attempts 2 and 3: accepted POST, then the transport
    # lists a newer contender at verification (see ContestedVerificationTransport).
    return None


@pytest.mark.parametrize(
    "case",
    [
        _UnconfirmedPostCase("success", 502, "cap", True),
        _UnconfirmedPostCase("failure", 502, "cap", False),
        _UnconfirmedPostCase("success", 429, "cap", False),
        _UnconfirmedPostCase("success", 502, "contested", True),
        _UnconfirmedPostCase("failure", 429, "contested", False),
        _UnconfirmedPostCase("success", 502, "stale", True),
        _UnconfirmedPostCase("failure", 502, "stale", False),
    ],
    ids=[
        "success-502-then-cap-withdraws",
        "failure-502-then-cap-no-withdrawal",
        "success-429-then-cap-no-withdrawal",
        "success-502-then-contested-withdraws",
        "failure-429-then-contested-no-withdrawal",
        "success-502-then-stale-withdraws",
        "failure-502-then-stale-no-withdrawal",
    ],
)
def test_unconfirmed_retryable_post_failure_preserved_across_warning_exits(
    case: _UnconfirmedPostCase,
) -> None:
    fake = ContestedVerificationTransport(contests=2 if case.ending == "contested" else 0)
    fake.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", case.conclusion)]}
    headers = {"retry-after": "10"} if case.status == 429 else {}
    fake.post_errors = iter(
        [forge.ResponseError(f"POST HTTP {case.status}", status=case.status, headers=headers)]
    )
    cap = _configure_warning_ending(fake, case)
    session = ApiSession(
        deadline=fake.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        transport=fake,
        out=io.StringIO(),
        max_api_calls=cap,
    )
    err = io.StringIO()
    rc = required_checks.evaluate_one(session, _review_config(), io.StringIO(), err)
    err_text = err.getvalue()
    sha = _review_config().sha
    # The injected 502/429 must really have answered the first verdict POST.
    assert next(fake.post_errors, None) is None
    verdict_posts = [post for post in fake.posts if post[1] == case.conclusion]
    assert verdict_posts[0] == (REVIEW_POST_PATH, case.conclusion)
    assert len(verdict_posts) == {"cap": 1, "stale": 1, "contested": 3}[case.ending]
    # The contests were really exercised through the default verification read.
    assert fake.contested_posts == {"cap": 0, "stale": 0, "contested": 2}[case.ending]
    expected_warning = {
        "cap": "Sweep API call cap of 7 reached",
        "stale": f"Posting skipped for {sha}",
        "contested": f"Status write contested again for {sha}",
    }[case.ending]
    assert f"::warning::{expected_warning}" in err_text
    error_line = f"::error::GitHub API error (post-status): POST HTTP {case.status}"
    if case.status >= 500:
        # An unknown 5xx write stays unconfirmed, so the warning exit is a failed judgement.
        assert rc == 1, err_text
        assert error_line in err_text
    else:
        # A 429 is a definitive non-write: the warning-only exit stays green.
        assert rc == 0, err_text
        assert "::error::" not in err_text
    assert session.withdrawal_reserved is False
    if case.expect_withdrawal:
        assert fake.posts[-1] == (REVIEW_POST_PATH, "pending")
    else:
        assert (REVIEW_POST_PATH, "pending") not in fake.posts


def test_retryable_post_failure_proven_success_and_withdrawal_boundaries() -> None:
    # Boundary 1: 502 on POST, next attempt contested once, third attempt uncontested -> proven.
    fake_proven = ContestedVerificationTransport(contests=1)
    fake_proven.post_errors = iter([forge.ResponseError("POST HTTP 502", status=502)])
    # Attempt 1: 502. Attempt 2: accepted POST (id 1002), verification lists a contender.
    # Attempt 3: accepted POST (id 1003), verification is live; the contender is the snapshot's
    # own prior status and is excluded, so the write is proven.
    rc_ok, _, err_ok, _ = _review_judge(fake_proven)
    assert rc_ok == 0 and err_ok == ""
    assert next(fake_proven.post_errors, None) is None
    assert fake_proven.contested_posts == 1 and fake_proven.contests_left == 0
    assert fake_proven.posts == [(REVIEW_POST_PATH, "success")] * 3

    # Boundary 2: failure POST gets 502 with only_if_pending=True while peer writes stale success.
    fake_peer = ReviewTransport()
    fake_peer.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", "failure")]}
    peer_ok = status_item(77, PUSH_CONTEXT, "success")
    fake_peer.replies[STATUSES_URL] = iter([[], [], [peer_ok], [peer_ok]])
    fake_peer.post_errors = iter([forge.ResponseError("POST HTTP 502", status=502)])
    rc_peer, _, err_peer, _ = _review_judge(fake_peer, only_if_pending=True)
    assert rc_peer == 0 and err_peer == ""
    # The 502 failure write was really attempted. The retry snapshot then sees the peer's
    # settled success with no withdrawal reserved, so the only_if_pending shortcut applies
    # and no second failure is posted.
    assert next(fake_peer.post_errors, None) is None
    assert fake_peer.posts == [(REVIEW_POST_PATH, "failure")]

    # Boundary 3: success POST gets 502, retry hits cap, and withdrawal itself fails -> risk error.
    fake_risk = ReviewTransport()
    fake_risk.post_errors = iter(
        [
            forge.ResponseError("POST HTTP 502", status=502),
            forge.ResponseError("POST HTTP 403", status=403),
        ]
    )
    session_risk = ApiSession(
        deadline=fake_risk.clock.now() + SESSION_DEADLINE_SECONDS,
        now=fake_risk.clock.now,
        sleep=fake_risk.clock.sleep,
        transport=fake_risk,
        out=io.StringIO(),
        max_api_calls=7,
    )
    err_risk = io.StringIO()
    assert (
        required_checks.evaluate_one(session_risk, _review_config(), io.StringIO(), err_risk) == 1
    )
    assert "a success may rest on superseded evidence" in err_risk.getvalue()


RECURSION_MARKER = b"<recursion-marker-7f3a>"


class _BadBodySweep:
    """Sweep transport serving ``bad_body`` for the second statuses and checks reads."""

    def __init__(self, fake: Any, first: str, second: str, bad_body: bytes) -> None:
        self.fake = fake
        self.bad_body = bad_body
        self.first_statuses = STATUSES_URL.replace(SHA, first)
        self.second_statuses = STATUSES_URL.replace(SHA, second)
        self.triage_done = False
        self.served = 0

    def __call__(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        if path == f"repos/{REPO}":
            self.triage_done = True
        if not self.triage_done and path == self.first_statuses:
            self.fake.calls.append((method, path))
            raise forge.ResponseError("missing", status=404)
        if not self.triage_done and path == self.second_statuses:
            self.fake.calls.append((method, path))
            self.served += 1
            return 200, {}, self.bad_body
        if self.triage_done and path == CHECKS_URL:
            self.fake.calls.append((method, path))
            self.served += 1
            return 200, {}, self.bad_body
        return self.fake(path, method=method, **kwargs)


_BAD_BODY_PARAMS = pytest.mark.parametrize(
    "bad_body",
    [RECURSION_MARKER, b"[NaN]", b"{bad"],
    ids=["deep-json", "nan-json", "malformed-json"],
)

_SWEEP_SHAS = ("1" * 40, "2" * 40, "3" * 40)


class _DecoderProbe:
    """json.loads stand-in: only the marker payload exhausts the decoder stack.

    Every other input goes to the real decoder with all of its arguments.
    """

    def __init__(self, real_loads: Any) -> None:
        self.real_loads = real_loads
        self.hits = 0

    def __call__(self, s: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(s, str):
            raw = s.encode("utf-8", "surrogatepass")
        elif isinstance(s, (bytes, bytearray)):
            raw = bytes(s)
        else:
            raw = b""
        if RECURSION_MARKER in raw:
            self.hits += 1
            raise RecursionError("maximum recursion depth exceeded")
        return self.real_loads(s, *args, **kwargs)


@pytest.fixture
def decoder_probe(monkeypatch: pytest.MonkeyPatch) -> _DecoderProbe:
    import json

    probe = _DecoderProbe(json.loads)
    monkeypatch.setattr(json, "loads", probe)
    return probe


def _assert_decoder_hits(
    probe: _DecoderProbe, bad_body: bytes, before: int, minimum: int = 1
) -> None:
    gained = probe.hits - before
    if bad_body == RECURSION_MARKER:
        assert gained >= minimum
    else:
        assert gained == 0


class _BadPostTransport:
    """Review transport whose first accepted POST returns ``bad_body``."""

    def __init__(self, fake: Any, bad_body: bytes) -> None:
        self.fake = fake
        self.bad_body = bad_body
        self.served = 0

    def __call__(
        self, path: str, method: str = "GET", **kwargs: Any
    ) -> tuple[int, dict[str, str], bytes]:
        reply = self.fake(path, method=method, **kwargs)
        if method == "POST" and len(self.fake.posts) == 1:
            self.served += 1
            return 201, {}, self.bad_body
        return reply


def _run_bad_sweep(bad_body: bytes) -> tuple[int, str, Any, _BadBodySweep]:
    first, second, third = _SWEEP_SHAS
    fake = _review_sweep_transport([first, second, third])
    fake.answers[CHECKS_URL.replace(SHA, third)] = {
        "check_runs": [check_item(1, "Build", "failure")]
    }
    sweep = _BadBodySweep(fake, first, second, bad_body)
    err = io.StringIO()
    rc = run_sweep(
        _review_config(targets="open-pulls", run_number=1),
        transport=sweep,
        now=fake.clock.now,
        sleep=fake.clock.sleep,
        out=io.StringIO(),
        err=err,
    )
    return rc, err.getvalue(), fake, sweep


@_BAD_BODY_PARAMS
def test_bad_api_json_does_not_stop_sweep_continuation(
    bad_body: bytes, decoder_probe: _DecoderProbe
) -> None:
    first, second, third = _SWEEP_SHAS
    rc, err_text, fake, sweep = _run_bad_sweep(bad_body)
    assert rc == 1
    assert f"Triage status read for {first} failed" in err_text
    assert f"Triage status read for {second} failed" in err_text
    assert "GitHub API error (check-runs):" in err_text
    assert (REVIEW_POST_PATH.replace(SHA, third), "failure") in fake.posts
    # The bad payload was served on both intended reads (second statuses, checks)
    # through the normal transport, and reached the decoder.
    assert sweep.served >= 2
    _assert_decoder_hits(decoder_probe, bad_body, 0, minimum=sweep.served)


@_BAD_BODY_PARAMS
def test_bad_accepted_post_body_withdraws_success_to_pending(
    bad_body: bytes, decoder_probe: _DecoderProbe
) -> None:
    fake_post = ReviewTransport()
    transport = _BadPostTransport(fake_post, bad_body)
    err_post = io.StringIO()
    rc_post = run_judge(
        _review_config(),
        transport=transport,
        now=fake_post.clock.now,
        sleep=fake_post.clock.sleep,
        out=io.StringIO(),
        err=err_post,
    )
    assert rc_post == 1 and "GitHub API error (post-status):" in err_post.getvalue()
    assert fake_post.posts == [(REVIEW_POST_PATH, "success"), (REVIEW_POST_PATH, "pending")]
    assert transport.served == 1
    _assert_decoder_hits(decoder_probe, bad_body, 0)


@_BAD_BODY_PARAMS
def test_bad_event_json_does_not_suppress_independent_check_failure(
    bad_body: bytes, tmp_path: Path, decoder_probe: _DecoderProbe
) -> None:
    bad_event = tmp_path / "event.json"
    bad_event.write_bytes(bad_body)
    fake_pr = ReviewTransport()
    fake_pr.answers[CHECKS_URL] = {"check_runs": [check_item(1, "Build", "failure")]}
    fake_pr.answers[RUNS_URL] = {"workflow_runs": [wf_run(1, "pull_request", head_branch="feat")]}
    rc_pr = run_judge(
        _review_config(event_name="pull_request"),
        transport=fake_pr,
        now=fake_pr.clock.now,
        sleep=fake_pr.clock.sleep,
        out=io.StringIO(),
        err=io.StringIO(),
        event_path=str(bad_event),
    )
    assert rc_pr == 0 and fake_pr.posts == [(REVIEW_POST_PATH, "failure")]
    _assert_decoder_hits(decoder_probe, bad_body, 0)
