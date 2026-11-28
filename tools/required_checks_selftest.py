# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""In-memory planted positive and negative self-tests for `required_checks` (ADR-0108)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge
from required_checks_config import (
    DEFAULT_APP,
    MAX_SELF_TESTS,
    Config,
    RawInputs,
    Snapshot,
    _iso_epoch,
    parse_config,
)


class _FakeClock:
    """Deterministic simulated clock for self-test cases."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.current = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


def _ok_reply(payload: object) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps(payload).encode("utf-8")


def _self_test_transport(
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
    *,
    posted_states: list[str] | None = None,
) -> Callable[..., tuple[int, dict[str, str], bytes]]:
    """Build an in-memory transport serving canned check-runs, workflow-runs and statuses."""
    pending_errors = list(errors or [])
    status_list = list(statuses or [])
    posts = [] if posted_states is None else posted_states

    def serve(
        path: str, *, method: str = "GET", fields: Mapping[str, str] | None = None, **_: object
    ):
        if method == "POST":
            body = dict(fields or {})
            state = body.get("state", "")
            posts.append(state)
            stamp = "2026-11-21T12:00:00Z"
            row = {"id": 1000 + len(posts), "created_at": stamp, "updated_at": stamp, **body}
            status_list.insert(0, row)
            return _ok_reply(row)
        if pending_errors:
            raise pending_errors.pop(0)
        base = path.split("?", 1)[0]
        if base.endswith("/check-runs"):
            return _ok_reply({"check_runs": check_runs})
        if "/actions/runs" in base:
            return _ok_reply({"workflow_runs": workflow_runs or []})
        if base.endswith("/statuses"):
            return _ok_reply(status_list)
        if "/commits/" in base:
            return _ok_reply({"commit": {"committer": {"date": "2026-11-21T10:00:00Z"}}})
        if base.endswith("/pulls"):
            return _ok_reply([])
        return _ok_reply(status_list)

    return serve


def _check_run(
    run_id: int,
    name: str,
    conclusion: str | None = "success",
    *,
    app: str = DEFAULT_APP,
    suite_id: int = 1,
    started_at: str = "2026-11-21T12:00:00Z",
    status: str | None = None,
) -> dict:
    default_status = "in_progress" if conclusion is None else "completed"
    return {
        "id": run_id,
        "name": name,
        "status": default_status if status is None else status,
        "conclusion": conclusion,
        "started_at": started_at,
        "app": {"slug": app},
        "check_suite": {"id": suite_id},
    }


def _decide_case(
    judge: ModuleType,
    config: Config,
    *,
    snapshot: Snapshot,
    now: float,
) -> tuple[str, str]:
    return judge.decide(snapshot, config, now, snapshot.anchor)


def _test_workflow_run(config: Config) -> dict:
    return {
        "check_suite_id": 1,
        "event": config.event_name,
        "head_branch": config.ref.removeprefix("refs/heads/"),
    }


def _run_case(
    judge: ModuleType,
    config: Config,
    *,
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
    expected_state: str | None = None,
) -> int:
    clock = _FakeClock(start=_iso_epoch("2026-11-21T12:00:00Z"))
    sink = io.StringIO()
    runs = workflow_runs if workflow_runs is not None else [_test_workflow_run(config)]
    posts: list[str] = []
    transport = _self_test_transport(check_runs, runs, statuses, errors, posted_states=posts)
    rc = judge.run_judge(
        config,
        transport=transport,
        now=clock.now,
        sleep=clock.sleep,
        out=sink,
        err=sink,
    )
    if rc != 0 or expected_state is None:
        return rc
    return int(posts != [expected_state])


def _self_test_success_case(judge: ModuleType, base_kw: dict[str, str | int]) -> int:
    config = parse_config(
        RawInputs(
            **base_kw,
            required_json='["Build","Scan","Status","PR Gate","Optional"]',
            pull_request_only_json='["PR Gate"]',
            not_owed_json='["Optional"]',
            apps_json='{"Scan":"code-scanning"}',
        )
    )
    return _run_case(
        judge,
        config,
        expected_state="success",
        check_runs=[
            _check_run(1, "Build", "success"),
            _check_run(2, "Scan", "success", app="code-scanning"),
            _check_run(3, "PR Gate", "skipped"),
        ],
        statuses=[{"id": 1, "context": "Status", "state": "success"}],
    )


def _self_test_outcome_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for check runs, apps and statuses."""
    kw: dict[str, str | int] = {**base_kw, "bound_minutes": 240}
    build = parse_config(RawInputs(**kw, required_json='["Build"]'))
    short_build = replace(build, bound_minutes=1)
    return (
        ("owed check and status pass", lambda: _self_test_success_case(judge, kw), 0),
        (
            "absent owed check fails",
            lambda: _run_case(judge, short_build, expected_state="failure", check_runs=[]),
            0,
        ),
        (
            "skipped owed check fails",
            lambda: _run_case(
                judge,
                build,
                expected_state="failure",
                check_runs=[_check_run(1, "Build", "skipped")],
            ),
            0,
        ),
        (
            "wrong app check run fails",
            lambda: _run_case(
                judge,
                short_build,
                expected_state="failure",
                check_runs=[_check_run(1, "Build", "success", app="impostor-app")],
            ),
            0,
        ),
        (
            "failed commit status fails",
            lambda: _run_case(
                judge,
                parse_config(RawInputs(**kw, required_json='["External"]')),
                expected_state="failure",
                check_runs=[],
                statuses=[{"id": 1, "context": "External", "state": "failure"}],
            ),
            0,
        ),
    )


def _self_test_event_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for foreign-event suite exclusion."""
    kw: dict[str, str | int] = {**base_kw, "bound_minutes": 240}
    return (
        (
            "other-event schedule failure ignored when own run succeeds",
            lambda: _run_case(
                judge,
                parse_config(RawInputs(**kw, required_json='["Build"]')),
                expected_state="success",
                check_runs=[
                    _check_run(
                        1, "Build", "success", suite_id=10, started_at="2026-11-21T12:00:00Z"
                    ),
                    _check_run(
                        2, "Build", "skipped", suite_id=20, started_at="2026-11-21T12:05:00Z"
                    ),
                ],
                workflow_runs=[
                    {"id": 10, "event": "push", "head_branch": "main", "check_suite_id": 10},
                    {"id": 20, "event": "schedule", "head_branch": "main", "check_suite_id": 20},
                ],
            ),
            0,
        ),
        (
            "other-event schedule success does not satisfy owed check",
            lambda: _run_case(
                judge,
                parse_config(RawInputs(**{**kw, "bound_minutes": 1}, required_json='["Build"]')),
                expected_state="failure",
                check_runs=[_check_run(2, "Build", "success", suite_id=20)],
                workflow_runs=[
                    {"id": 20, "event": "schedule", "head_branch": "main", "check_suite_id": 20},
                ],
            ),
            0,
        ),
    )


def _self_test_limit_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for API rate-limit bounds."""
    kw: dict[str, str | int] = {**base_kw, "bound_minutes": 10}
    rl_err = forge.ResponseError(
        "rate limit", status=429, headers={"retry-after": "120"}, message="API rate limit exceeded"
    )
    return (
        (
            "rate-limit wait within budget recovers",
            lambda: _run_case(
                judge,
                parse_config(RawInputs(**kw, required_json='["Build"]')),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err],
            ),
            0,
        ),
        (
            "rate-limit wait over budget fails without judging",
            lambda: _run_case(
                judge,
                parse_config(RawInputs(**kw, required_json='["Build"]')),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err, rl_err, rl_err, rl_err],
            ),
            1,
        ),
    )


def _self_test_pr_only_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for pull-request-only contexts across events."""
    pr_target_kw: dict[str, str | int] = {
        "repository": base_kw["repository"],
        "event_name": "pull_request_target",
        "ref": base_kw["ref"],
        "commit_sha": "0" * 40,
        "pr_head_sha": base_kw["sha"],
        "bound_minutes": 1,
    }
    return (
        (
            "pull-request-only absent on push passes",
            lambda: _run_case(
                judge,
                parse_config(
                    RawInputs(
                        **base_kw,
                        required_json='["Build","PR Gate"]',
                        pull_request_only_json='["PR Gate"]',
                    )
                ),
                expected_state="success",
                check_runs=[_check_run(1, "Build", "success")],
            ),
            0,
        ),
        (
            "pull-request-only absent on pull_request_target fails",
            lambda: _run_case(
                judge,
                parse_config(
                    RawInputs(
                        **pr_target_kw,
                        required_json='["Build","PR Gate"]',
                        pull_request_only_json='["PR Gate"]',
                    )
                ),
                expected_state="failure",
                check_runs=[_check_run(1, "Build", "success")],
            ),
            0,
        ),
    )


def _self_test_vmafx_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """Five SHAs with queued owed siblings each return pending; sleep is never called."""
    names = ["A", "B", "C", "D", "E"]

    def five_shas_no_sleep() -> bool:
        checks = [_check_run(i + 1, name, None, status="queued") for i, name in enumerate(names)]
        for idx in range(5):
            sha = f"{idx:040x}"
            clock = _FakeClock(start=_iso_epoch("2026-11-21T12:00:00Z"))
            merged: dict[str, str | int] = dict(base_kw)
            merged.update(sha=sha, required_json=json.dumps(names), bound_minutes=240)
            cfg = parse_config(RawInputs(**merged))
            transport = _self_test_transport(checks, [_test_workflow_run(cfg)])
            rc = judge.run_judge(
                cfg,
                transport=transport,
                now=clock.now,
                sleep=clock.sleep,
                out=io.StringIO(),
                err=io.StringIO(),
            )
            if rc != 0 or clock.sleeps:
                return False
            snap = judge.read_snapshot(
                judge.ApiSession(
                    deadline=clock.now() + 60, now=clock.now, sleep=clock.sleep, transport=transport
                ),
                cfg,
            )
            if snap is None:
                return False
            state, _ = judge.decide(snap, cfg, clock.now(), snap.anchor)
            if state != "pending":
                return False
        return True

    return (("vmafx five queued SHAs pending without sleep", five_shas_no_sleep, True),)


def _self_test_decide_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """Bounded decide() cases."""
    anchor = 1_700_000_000.0

    def snap_with(*checks: dict, anchor_val: float = anchor) -> Snapshot:
        by_name = {str(c["name"]): c for c in checks}
        return Snapshot(by_name, {}, (), anchor_val)

    cfg = parse_config(RawInputs(**base_kw, required_json='["X","Y"]', bound_minutes=60))

    def all_success() -> bool:
        state, _ = _decide_case(
            judge,
            cfg,
            snapshot=snap_with(_check_run(1, "X", "success"), _check_run(2, "Y", "success")),
            now=anchor + 10,
        )
        return state == "success"

    def fail_while_queued() -> bool:
        state, _ = _decide_case(
            judge,
            cfg,
            snapshot=snap_with(_check_run(1, "X", "failure"), _check_run(2, "Y", None)),
            now=anchor + 10,
        )
        return state == "failure"

    def pending_at_bound() -> bool:
        state, _ = _decide_case(
            judge,
            cfg,
            snapshot=snap_with(_check_run(1, "Y", None)),
            now=anchor + 60 * 60,
        )
        return state == "pending"

    def failure_after_bound() -> bool:
        state, desc = _decide_case(
            judge,
            cfg,
            snapshot=snap_with(_check_run(1, "Y", None)),
            now=anchor + 60 * 60 + 1,
        )
        return state == "failure" and "deadline" in desc

    return (
        ("decide all owed success", all_success, True),
        ("decide failure while sibling queued", fail_while_queued, True),
        ("decide pending at bound", pending_at_bound, True),
        ("decide failure one second after bound", failure_after_bound, True),
    )


def _self_test_foreign_app_decide_cases(
    judge: ModuleType,
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """decide() at the bound when the only producer is a foreign app."""
    anchor = 1_700_000_000.0
    app_cfg = parse_config(
        RawInputs(
            **base_kw,
            required_json='["Scan"]',
            apps_json='{"Scan":"code-scanning"}',
            bound_minutes=60,
        )
    )
    empty = Snapshot({}, {}, (), anchor)

    def foreign_app_pending_at_bound() -> bool:
        state, _ = judge.decide(empty, app_cfg, anchor + 60 * 60, anchor)
        return state == "pending"

    def foreign_app_failure_after_bound() -> bool:
        state, desc = judge.decide(empty, app_cfg, anchor + 60 * 60 + 1, anchor)
        return state == "failure" and "deadline" in desc

    return (
        ("decide foreign-app-only pending at bound", foreign_app_pending_at_bound, True),
        ("decide foreign-app-only failure after bound", foreign_app_failure_after_bound, True),
    )


def run_self_test(judge: ModuleType, out: TextIO = sys.stdout) -> int:
    """Run planted positive and negative cases in memory; return 0 when all match."""
    base_kw = {
        "repository": "plurano/horistes",
        "sha": "a" * 40,
        "event_name": "push",
        "ref": "refs/heads/main",
    }
    cases: list[tuple[str, Callable[[], int] | Callable[[], bool], int | bool]] = [
        *_self_test_outcome_cases(judge, base_kw),
        *_self_test_event_cases(judge, base_kw),
        *_self_test_limit_cases(judge, base_kw),
        *_self_test_pr_only_cases(judge, base_kw),
    ]
    bool_cases = (
        *_self_test_vmafx_cases(judge, base_kw),
        *_self_test_decide_cases(judge, base_kw),
        *_self_test_foreign_app_decide_cases(judge, base_kw),
    )
    passed = True
    for label, fn, expected in cases[:MAX_SELF_TESTS]:
        matches = fn() == expected
        out.write(f"{'ok' if matches else 'FAIL'} {label}\n")
        passed = passed and matches
    for label, fn, expected in bool_cases:
        matches = fn() == expected
        out.write(f"{'ok' if matches else 'FAIL'} {label}\n")
        passed = passed and matches
    return 0 if passed else 1
