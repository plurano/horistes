# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Poll and judge required check runs and commit statuses on a commit (ADR-0108).

This module uses the Python standard library and the sibling `forge` reader only,
so it runs under `python3 -I -B` from a sparse checkout without `.venv`.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge

FLOOR = (3, 12)
MAX_CONTEXTS = 500
WAIT_MINUTES_MIN = 1
WAIT_MINUTES_MAX = 240
RATE_LIMIT_MINUTES_MIN = 0
RATE_LIMIT_MINUTES_MAX = 90
POLL_MIN_SECONDS = 30
POLL_MAX_SECONDS = 240
API_BACKOFF_SECONDS = (30, 60, 120, 240, 240, 210)
MAX_RATE_LIMIT_WAITS_PER_CALL = 6
MAX_CALL_ATTEMPTS = len(API_BACKOFF_SECONDS) + MAX_RATE_LIMIT_WAITS_PER_CALL + 1
CHECK_RUN_MAX_PAGES = 20
STATUS_MAX_PAGES = 20
WORKFLOW_RUN_MAX_PAGES = 10
MAX_CHECK_RUNS = CHECK_RUN_MAX_PAGES * forge.PER_PAGE
MAX_STATUSES = STATUS_MAX_PAGES * forge.PER_PAGE
MAX_WORKFLOW_RUNS = WORKFLOW_RUN_MAX_PAGES * forge.PER_PAGE
MAX_LOGGED_FOREIGN_APPS = 20
MAX_SELF_TESTS = 32
DEFAULT_APP = "github-actions"
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
INT_RE = re.compile(r"^-?[0-9]+$")


class InputError(Exception):
    """Malformed or out-of-range caller input detected before polling."""


class ApiFailureError(Exception):
    """Unrecoverable API error, transient 5xx timeout, or rate-limit bound exceeded."""


@dataclass(frozen=True)
class Config:
    """Validated inputs for one required-checks run."""

    repository: str
    sha: str
    event_name: str
    ref: str
    required: tuple[str, ...]
    pull_request_only: frozenset[str]
    not_owed: frozenset[str]
    apps: dict[str, str]
    wait_minutes: int
    rate_limit_wait_minutes: int


@dataclass(frozen=True)
class Snapshot:
    """Newest admitted check runs and newest commit statuses on the target SHA."""

    checks_by_name: dict[str, dict]
    statuses_by_context: dict[str, dict]
    ignored_foreign_apps: tuple[str, ...]


def below_floor(version: tuple[int, ...]) -> str | None:
    """Refusal when running on a Python older than FLOOR, else None."""
    if tuple(version[:2]) >= FLOOR:
        return None
    return (
        f"the required-checks judge needs Python {FLOOR[0]}.{FLOOR[1]} or newer; "
        f"this python3 is {version[0]}.{version[1]}"
    )


def _reject_constant(token: str) -> None:
    raise ValueError(f"{token} is not valid JSON")


def _load_json(label: str, raw: str) -> object:
    """Decode strict JSON for a named input or raise InputError."""
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as err:
        raise InputError(f"{label}: invalid JSON ({err})") from err


def _parse_int_range(label: str, raw: str | int, low: int, high: int) -> int:
    """Parse a whole integer within [low, high] or raise InputError."""
    if isinstance(raw, bool):
        raise InputError(f"{label}: must be a whole number from {low} to {high}, got {raw!r}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and INT_RE.fullmatch(raw.strip()):
        value = int(raw.strip())
    else:
        raise InputError(f"{label}: must be a whole number from {low} to {high}, got {raw!r}")
    if not low <= value <= high:
        raise InputError(f"{label}: {value} is out of range {low}..{high}")
    return value


def _validate_context_name(label: str, item: object) -> str:
    """Require a non-empty single-line string context name."""
    if not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item:
        raise InputError(f"{label}: context names must be non-empty single-line strings")
    return item


def _parse_required(raw: str) -> tuple[str, ...]:
    """Parse the non-empty list of unique required context names."""
    data = _load_json("required", raw)
    if not isinstance(data, list):
        raise InputError("required: must be a JSON list of context names")
    if not data:
        raise InputError("required: must not be empty")
    if len(data) > MAX_CONTEXTS:
        raise InputError(f"required: {len(data)} entries exceeds limit {MAX_CONTEXTS}")
    seen: set[str] = set()
    names: list[str] = []
    for item in data[:MAX_CONTEXTS]:
        name = _validate_context_name("required", item)
        if name in seen:
            raise InputError(f"required: duplicate context name {name!r}")
        seen.add(name)
        names.append(name)
    return tuple(names)


def _parse_subset(label: str, raw: str, allowed: frozenset[str]) -> frozenset[str]:
    """Parse a JSON list of context names that must each appear in `required`."""
    data = _load_json(label, raw)
    if not isinstance(data, list):
        raise InputError(f"{label}: must be a JSON list of context names")
    if len(data) > MAX_CONTEXTS:
        raise InputError(f"{label}: {len(data)} entries exceeds limit {MAX_CONTEXTS}")
    chosen: set[str] = set()
    for item in data[:MAX_CONTEXTS]:
        if not isinstance(item, str):
            raise InputError(f"{label}: every entry must be a string")
        if item not in allowed:
            raise InputError(f"{label}: unknown context {item!r} is not in required")
        chosen.add(item)
    return frozenset(chosen)


def _parse_apps(raw: str, allowed: frozenset[str]) -> dict[str, str]:
    """Parse a JSON mapping of required context name to expected GitHub App slug."""
    data = _load_json("apps", raw)
    if not isinstance(data, dict):
        raise InputError("apps: must be a JSON object mapping context name to app slug")
    if len(data) > MAX_CONTEXTS:
        raise InputError(f"apps: {len(data)} entries exceeds limit {MAX_CONTEXTS}")
    mapping: dict[str, str] = {}
    for key, slug in list(data.items())[:MAX_CONTEXTS]:
        if key not in allowed:
            raise InputError(f"apps: unknown context {key!r} is not in required")
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise InputError(f"apps: invalid app slug {slug!r} for {key!r}")
        mapping[key] = slug
    return mapping


def _resolve_sha(event_name: str, explicit_sha: str, commit_sha: str, pr_head_sha: str) -> str:
    """Select and validate the commit SHA to judge for the current event."""
    if explicit_sha:
        chosen = explicit_sha.strip()
    elif event_name == "pull_request":
        chosen = pr_head_sha.strip()
    else:
        chosen = commit_sha.strip()
    if not SHA_RE.fullmatch(chosen):
        raise InputError(f"sha: invalid commit SHA {chosen!r}")
    return chosen


def parse_config(
    *,
    repository: str,
    event_name: str,
    ref: str = "",
    sha: str = "",
    commit_sha: str = "",
    pr_head_sha: str = "",
    required_json: str,
    pull_request_only_json: str = "[]",
    not_owed_json: str = "[]",
    apps_json: str = "{}",
    wait_minutes: str | int = 60,
    rate_limit_wait_minutes: str | int = 30,
) -> Config:
    """Validate every caller input before any forge API request is made."""
    repo = repository.strip()
    if not REPO_RE.fullmatch(repo) or repo.split("/", 1)[1] in (".", ".."):
        raise InputError(f"repository: invalid owner/name {repository!r}")
    event = event_name.strip()
    if not event:
        raise InputError("event: event name must not be empty")
    resolved_sha = _resolve_sha(event, sha, commit_sha, pr_head_sha)
    required = _parse_required(required_json)
    allowed = frozenset(required)
    pr_only = _parse_subset("pull-request-only", pull_request_only_json, allowed)
    not_owed = _parse_subset("not-owed", not_owed_json, allowed)
    apps = _parse_apps(apps_json, allowed)
    wait_m = _parse_int_range("wait-minutes", wait_minutes, WAIT_MINUTES_MIN, WAIT_MINUTES_MAX)
    rl_m = _parse_int_range(
        "rate-limit-wait-minutes",
        rate_limit_wait_minutes,
        RATE_LIMIT_MINUTES_MIN,
        RATE_LIMIT_MINUTES_MAX,
    )
    return Config(
        repository=repo,
        sha=resolved_sha,
        event_name=event,
        ref=ref.strip(),
        required=required,
        pull_request_only=pr_only,
        not_owed=not_owed,
        apps=apps,
        wait_minutes=wait_m,
        rate_limit_wait_minutes=rl_m,
    )


class ApiSession:
    """Bounded forge GET client with 5xx outage backoff and rate-limit budget tracking."""

    def __init__(
        self,
        *,
        deadline: float,
        max_rate_limit_seconds: float,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[[str], tuple[int, dict[str, str], bytes]] = forge.request,
        out: TextIO = sys.stdout,
    ) -> None:
        self.deadline = deadline
        self.max_rate_limit_seconds = max_rate_limit_seconds
        self.now = now
        self.sleep = sleep
        self.transport = transport
        self.out = out
        self.rate_limit_waited_seconds = 0.0
        self.rate_limit_wait_count = 0

    def _within_rate_limit_bounds(self, delay: int | float, waits: int) -> bool:
        if waits >= MAX_RATE_LIMIT_WAITS_PER_CALL or self.max_rate_limit_seconds <= 0:
            return False
        if self.rate_limit_waited_seconds + delay > self.max_rate_limit_seconds:
            return False
        return self.now() + delay <= self.deadline

    def _wait_rate_limit(
        self, label: str, err: forge.ResponseError, delay: int | float, waits: int
    ) -> None:
        if not self._within_rate_limit_bounds(delay, waits):
            raise ApiFailureError(
                f"GitHub API rate limit: {label} still answers HTTP {err.status}. "
                "No check result was judged; rerun this job."
            ) from err
        self.out.write(f"::warning::{label}: GitHub API HTTP {err.status}, retrying in {delay} s\n")
        self.rate_limit_wait_count += 1
        self.rate_limit_waited_seconds += delay
        self.sleep(delay)

    def _wait_server_error(self, label: str, err: forge.ResponseError, attempt: int) -> None:
        if attempt >= len(API_BACKOFF_SECONDS):
            raise ApiFailureError(
                f"Transient GitHub API failure: {label} still answers HTTP {err.status} "
                "after 15 minutes. No check result was judged; rerun this job."
            ) from err
        seconds = API_BACKOFF_SECONDS[attempt]
        if self.now() + seconds > self.deadline:
            raise ApiFailureError(
                f"Transient GitHub API failure: {label} still answers HTTP {err.status} "
                "at the wait deadline. No check result was judged; rerun this job."
            ) from err
        self.out.write(
            f"::warning::{label}: GitHub API HTTP {err.status}, retrying in {seconds} s\n"
        )
        self.sleep(seconds)

    def get_json(self, label: str, path: str) -> object:
        """Fetch and decode one JSON endpoint with bounded rate-limit and 5xx retries."""
        server_attempts = 0
        rate_limit_waits = 0
        for _ in range(MAX_CALL_ATTEMPTS):
            try:
                _, _, body = self.transport(path)
                return json.loads(body)
            except forge.ResponseError as err:
                delay = forge.rate_limit_delay(
                    err.status, err.headers, err.message, rate_limit_waits, self.now()
                )
                if delay is not None:
                    self._wait_rate_limit(label, err, delay, rate_limit_waits)
                    rate_limit_waits += 1
                    continue
                if err.status is not None and err.status >= 500:
                    self._wait_server_error(label, err, server_attempts)
                    server_attempts += 1
                    continue
                raise ApiFailureError(f"GitHub API error ({label}): {err}") from err
            except (forge.ForgeError, ValueError) as err:
                raise ApiFailureError(f"GitHub API error ({label}): {err}") from err
        raise ApiFailureError(f"GitHub API error ({label}): retry bound reached")


def _iso_epoch(value: object) -> float:
    """Convert an ISO-8601 timestamp string to epoch seconds, or 0.0 when absent."""
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return parsed.timestamp() if parsed.tzinfo is not None else 0.0


def _int_id(value: object) -> int:
    """Numeric object id for deterministic tie-breaking, or 0 when absent."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_foreign_run(run: dict, event_name: str, own_branch: str) -> bool:
    """Whether a workflow run on the SHA belongs to another event or another branch."""
    run_event = str(run.get("event") or "")
    if event_name in PR_EVENTS:
        return run_event not in PR_EVENTS
    allowed = {"push", "workflow_dispatch"} if event_name == "workflow_dispatch" else {event_name}
    if run_event not in allowed:
        return True
    head_branch = run.get("head_branch")
    return bool(own_branch and isinstance(head_branch, str) and head_branch != own_branch)


def foreign_suites(workflow_runs: list[dict], event_name: str, ref: str) -> set[int]:
    """Check-suite ids of workflow runs triggered by another event or branch on the SHA."""
    own_branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    excluded: set[int] = set()
    for run in workflow_runs[:MAX_WORKFLOW_RUNS]:
        suite_id = _int_id(run.get("check_suite_id"))
        if suite_id and is_foreign_run(run, event_name, own_branch):
            excluded.add(suite_id)
    return excluded


def _check_run_admitted(
    run: dict, excluded_suites: set[int], apps: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """Return (admitted_name, foreign_app_note) for one check-run entry."""
    name = run.get("name")
    if not isinstance(name, str) or not name:
        return None, None
    expected_app = apps.get(name, DEFAULT_APP)
    app = run.get("app")
    actual_app = app.get("slug") if isinstance(app, dict) else None
    if actual_app != expected_app:
        return None, f"{name} ({actual_app or 'missing'}, expected {expected_app})"
    suite = run.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, dict) else None
    if suite_id in excluded_suites:
        return None, None
    return name, None


def _check_run_rank(run: dict) -> tuple[float, int]:
    stamp = run.get("started_at") or run.get("created_at") or run.get("completed_at")
    return (_iso_epoch(stamp), _int_id(run.get("id")))


def newest_check_runs(
    check_runs: list[dict],
    excluded_suites: set[int],
    apps: Mapping[str, str],
    required_names: frozenset[str],
) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Select the newest check run per name from its expected app outside foreign suites."""
    by_name: dict[str, dict] = {}
    ranks: dict[str, tuple[float, int]] = {}
    ignored: list[str] = []
    seen_ignored: set[str] = set()
    for run in check_runs[:MAX_CHECK_RUNS]:
        admitted_name, foreign_note = _check_run_admitted(run, excluded_suites, apps)
        raw_name = run.get("name")
        if foreign_note and raw_name in required_names and raw_name not in seen_ignored:
            seen_ignored.add(str(raw_name))
            ignored.append(foreign_note)
        if admitted_name is None:
            continue
        rank = _check_run_rank(run)
        if admitted_name not in ranks or rank >= ranks[admitted_name]:
            ranks[admitted_name] = rank
            by_name[admitted_name] = run
    return by_name, tuple(ignored)


def newest_statuses(statuses: list[dict]) -> dict[str, dict]:
    """Select the newest commit status per context."""
    by_context: dict[str, dict] = {}
    ranks: dict[str, tuple[float, int]] = {}
    for entry in statuses[:MAX_STATUSES]:
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            continue
        stamp = entry.get("updated_at") or entry.get("created_at")
        rank = (_iso_epoch(stamp), _int_id(entry.get("id")))
        if context not in ranks or rank >= ranks[context]:
            ranks[context] = rank
            by_context[context] = entry
    return by_context


def read_snapshot(session: ApiSession, config: Config) -> Snapshot:
    """Read check runs, workflow runs and commit statuses; re-read check runs after a limit wait."""
    checks_path = f"repos/{config.repository}/commits/{config.sha}/check-runs"
    runs_path = f"repos/{config.repository}/actions/runs?head_sha={config.sha}"
    statuses_path = f"repos/{config.repository}/commits/{config.sha}/statuses"
    try:
        check_runs = forge.paged(
            checks_path,
            "check_runs",
            max_pages=CHECK_RUN_MAX_PAGES,
            fetch=lambda p: session.get_json("check-runs", p),
        )
        waits_before = session.rate_limit_wait_count
        workflow_runs = forge.paged(
            runs_path,
            "workflow_runs",
            max_pages=WORKFLOW_RUN_MAX_PAGES,
            fetch=lambda p: session.get_json("workflow-runs", p),
        )
        statuses = forge.paged(
            statuses_path,
            None,
            max_pages=STATUS_MAX_PAGES,
            fetch=lambda p: session.get_json("statuses", p),
        )
        if session.rate_limit_wait_count != waits_before:
            check_runs = forge.paged(
                checks_path,
                "check_runs",
                max_pages=CHECK_RUN_MAX_PAGES,
                fetch=lambda p: session.get_json("check-runs", p),
            )
    except forge.ForgeError as err:
        raise ApiFailureError(f"GitHub API error: {err}") from err
    excluded = foreign_suites(workflow_runs, config.event_name, config.ref)
    by_name, ignored = newest_check_runs(
        check_runs, excluded, config.apps, frozenset(config.required)
    )
    return Snapshot(
        checks_by_name=by_name,
        statuses_by_context=newest_statuses(statuses),
        ignored_foreign_apps=ignored,
    )


def exempt_reason(name: str, config: Config) -> str:
    """Exemption reason for `name` on this event, or empty string when `name` is owed."""
    if name in config.not_owed:
        return "not owed"
    if name in config.pull_request_only and config.event_name != "pull_request":
        return f"pull-request-only on {config.event_name}"
    return ""


def _records_for(name: str, snapshot: Snapshot, config: Config) -> tuple[dict | None, dict | None]:
    """Return (check_run, commit_status) applicable to `name`."""
    run = snapshot.checks_by_name.get(name)
    status = None if name in config.apps else snapshot.statuses_by_context.get(name)
    return run, status


def _is_context_pending(run: dict | None, status: dict | None) -> bool:
    run_pending = run is not None and run.get("status") != "completed"
    status_pending = status is not None and status.get("state") == "pending"
    return run_pending or status_pending


def owed_waiting(snapshot: Snapshot, config: Config) -> tuple[list[str], list[str]]:
    """Return (missing, pending) lists among owed contexts."""
    missing: list[str] = []
    pending: list[str] = []
    for name in config.required[:MAX_CONTEXTS]:
        if exempt_reason(name, config):
            continue
        run, status = _records_for(name, snapshot, config)
        if run is None and status is None:
            missing.append(name)
        elif _is_context_pending(run, status):
            pending.append(name)
    return missing, pending


def snapshot_fingerprint(snapshot: Snapshot, config: Config) -> tuple[object, ...]:
    """Fingerprint of the judged set for adaptive poll backoff."""
    items: list[object] = []
    for name in config.required[:MAX_CONTEXTS]:
        run, status = _records_for(name, snapshot, config)
        run_pair = (run.get("status"), run.get("conclusion")) if run is not None else None
        state = status.get("state") if status is not None else None
        items.append((name, run_pair, state))
    return tuple(items)


def _waiting_summary(missing: list[str], pending: list[str]) -> str:
    parts: list[str] = []
    for label, names in (("active", pending), ("unregistered", missing)):
        if names:
            extra = f" (+{len(names) - 3} more)" if len(names) > 3 else ""
            parts.append(f"{label}: {', '.join(names[:3])}{extra}")
    return "; ".join(parts)


def poll_until_settled(session: ApiSession, config: Config, out: TextIO) -> Snapshot:
    """Poll from 30 s doubling to 240 s while unchanged, resetting to 30 s on change."""
    max_polls = (config.wait_minutes * 60) // POLL_MIN_SECONDS + 2
    previous_fp: tuple[object, ...] | None = None
    interval = POLL_MIN_SECONDS
    snapshot = read_snapshot(session, config)
    for step in range(max_polls):
        if step > 0:
            snapshot = read_snapshot(session, config)
        fp = snapshot_fingerprint(snapshot, config)
        interval = min(interval * 2, POLL_MAX_SECONDS) if fp == previous_fp else POLL_MIN_SECONDS
        previous_fp = fp
        missing, pending = owed_waiting(snapshot, config)
        remaining = session.deadline - session.now()
        if (not missing and not pending) or remaining <= 0:
            return snapshot
        sleep_for = min(float(interval), remaining)
        out.write(f"Waiting ({_waiting_summary(missing, pending)}); next poll in {sleep_for:g} s\n")
        session.sleep(sleep_for)
    return snapshot


def _judge_exempt(
    name: str, reason: str, run: dict | None, status: dict | None
) -> tuple[bool, str]:
    """Judge an exempt context: absent, skipped, pending or success pass; failures fail."""
    if run is not None and run.get("status") == "completed":
        conclusion = str(run.get("conclusion") or "completed")
        if conclusion not in {"success", "skipped"}:
            return False, f"{name}: {conclusion} (ran although {reason})"
    if status is not None:
        state = str(status.get("state") or "unknown")
        if state not in {"success", "pending"}:
            return False, f"{name}: status {state} (reported although {reason})"
    return True, f"OK ({reason}): {name}"


def _owed_run_problem(run: dict | None) -> str:
    if run is None:
        return ""
    if run.get("status") != "completed":
        return f"still {run.get('status') or 'pending'}"
    if run.get("conclusion") != "success":
        return str(run.get("conclusion") or "completed")
    return ""


def _owed_status_problem(status: dict | None) -> str:
    if status is None or status.get("state") == "success":
        return ""
    return f"status {status.get('state') or 'unknown'}"


def judge_context(name: str, snapshot: Snapshot, config: Config) -> tuple[bool, str]:
    """Judge one context in `required`, returning (passed, log_or_failure_line)."""
    reason = exempt_reason(name, config)
    run, status = _records_for(name, snapshot, config)
    if reason:
        return _judge_exempt(name, reason, run, status)
    if run is None and status is None:
        return False, f"{name}: never reported on SHA {config.sha}"
    problems = [p for p in (_owed_run_problem(run), _owed_status_problem(status)) if p]
    if problems:
        return False, f"{name}: {', '.join(problems)}"
    return True, f"OK (success): {name}"


def judge_snapshot(snapshot: Snapshot, config: Config, out: TextIO) -> list[str]:
    """Write per-context lines and return failure descriptions."""
    for entry in snapshot.ignored_foreign_apps[:MAX_LOGGED_FOREIGN_APPS]:
        out.write(f"Ignored check run from unexpected producer: {entry}\n")
    extra_ignored = len(snapshot.ignored_foreign_apps) - MAX_LOGGED_FOREIGN_APPS
    if extra_ignored > 0:
        out.write(f"Ignored {extra_ignored} additional foreign-producer check names\n")
    failures: list[str] = []
    for name in config.required[:MAX_CONTEXTS]:
        passed, line = judge_context(name, snapshot, config)
        if passed:
            out.write(f"{line}\n")
        else:
            failures.append(line)
    return failures


def run_judge(
    config: Config,
    *,
    transport: Callable[[str], tuple[int, dict[str, str], bytes]] = forge.request,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Execute polling and judgment for a validated Config; return 0 on pass, 1 on failure."""
    start = now()
    session = ApiSession(
        deadline=start + config.wait_minutes * 60,
        max_rate_limit_seconds=config.rate_limit_wait_minutes * 60,
        now=now,
        sleep=sleep,
        transport=transport,
        out=out,
    )
    try:
        snapshot = poll_until_settled(session, config, out)
    except ApiFailureError as exc:
        err.write(f"::error::{exc}\n")
        return 1
    failures = judge_snapshot(snapshot, config, out)
    if failures:
        joined = "\n  - ".join(failures)
        err.write(f"::error::Required checks did not pass:\n  - {joined}\n")
        return 1
    out.write("All owed required checks reported success.\n")
    return 0


class _FakeClock:
    """Deterministic simulated clock for self-test cases."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.current = start

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.current += seconds


def _ok_reply(payload: object) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps(payload).encode("utf-8")


def _self_test_transport(
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
) -> Callable[[str], tuple[int, dict[str, str], bytes]]:
    """Build an in-memory transport serving canned check-runs, workflow-runs and statuses."""
    pending_errors = list(errors or [])

    def serve(path: str) -> tuple[int, dict[str, str], bytes]:
        if pending_errors:
            raise pending_errors.pop(0)
        if "/check-runs?" in path:
            return _ok_reply({"check_runs": check_runs})
        if "/actions/runs?" in path:
            return _ok_reply({"workflow_runs": workflow_runs or []})
        return _ok_reply(statuses or [])

    return serve


def _check_run(
    run_id: int,
    name: str,
    conclusion: str | None = "success",
    *,
    app: str = DEFAULT_APP,
    suite_id: int = 1,
    started_at: str = "2026-11-21T12:00:00Z",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": "in_progress" if conclusion is None else "completed",
        "conclusion": conclusion,
        "started_at": started_at,
        "app": {"slug": app},
        "check_suite": {"id": suite_id},
    }


def _run_case(
    config: Config,
    *,
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
) -> int:
    clock = _FakeClock()
    sink = io.StringIO()
    return run_judge(
        config,
        transport=_self_test_transport(check_runs, workflow_runs, statuses, errors),
        now=clock.now,
        sleep=clock.sleep,
        out=sink,
        err=sink,
    )


def _self_test_outcome_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for check runs, apps and statuses."""
    return (
        (
            "owed check and status pass",
            lambda: _run_case(
                parse_config(
                    **base_kw,
                    required_json='["Build","Scan","Status","PR Gate","Optional"]',
                    pull_request_only_json='["PR Gate"]',
                    not_owed_json='["Optional"]',
                    apps_json='{"Scan":"code-scanning"}',
                ),
                check_runs=[
                    _check_run(1, "Build", "success"),
                    _check_run(2, "Scan", "success", app="code-scanning"),
                    _check_run(3, "PR Gate", "skipped"),
                ],
                statuses=[{"id": 1, "context": "Status", "state": "success"}],
            ),
            0,
        ),
        (
            "absent owed check fails",
            lambda: _run_case(
                parse_config(**base_kw, required_json='["Build"]', wait_minutes=1),
                check_runs=[],
            ),
            1,
        ),
        (
            "skipped owed check fails",
            lambda: _run_case(
                parse_config(**base_kw, required_json='["Build"]'),
                check_runs=[_check_run(1, "Build", "skipped")],
            ),
            1,
        ),
        (
            "wrong app check run fails",
            lambda: _run_case(
                parse_config(**base_kw, required_json='["Build"]', wait_minutes=1),
                check_runs=[_check_run(1, "Build", "success", app="impostor-app")],
            ),
            1,
        ),
        (
            "failed commit status fails",
            lambda: _run_case(
                parse_config(**base_kw, required_json='["External"]'),
                check_runs=[],
                statuses=[{"id": 1, "context": "External", "state": "failure"}],
            ),
            1,
        ),
    )


def _self_test_event_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for foreign-event suite exclusion."""
    return (
        (
            "other-event schedule failure ignored when own run succeeds",
            lambda: _run_case(
                parse_config(**base_kw, required_json='["Build"]'),
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
                parse_config(**base_kw, required_json='["Build"]', wait_minutes=1),
                check_runs=[_check_run(2, "Build", "success", suite_id=20)],
                workflow_runs=[
                    {"id": 20, "event": "schedule", "head_branch": "main", "check_suite_id": 20},
                ],
            ),
            1,
        ),
    )


def _self_test_limit_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for API rate-limit bounds."""
    rl_err = forge.ResponseError(
        "rate limit", status=429, headers={"retry-after": "120"}, message="API rate limit exceeded"
    )
    return (
        (
            "rate-limit wait within budget recovers",
            lambda: _run_case(
                parse_config(
                    **base_kw, required_json='["Build"]', wait_minutes=10, rate_limit_wait_minutes=5
                ),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err],
            ),
            0,
        ),
        (
            "rate-limit wait over budget fails without judging",
            lambda: _run_case(
                parse_config(
                    **base_kw, required_json='["Build"]', wait_minutes=10, rate_limit_wait_minutes=1
                ),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err],
            ),
            1,
        ),
    )


def self_test(out: TextIO = sys.stdout) -> int:
    """Run planted positive and negative cases in memory; return 0 when all match."""
    base_kw = {
        "repository": "plurano/horistes",
        "sha": "a" * 40,
        "event_name": "push",
        "ref": "refs/heads/main",
    }
    cases = (
        *_self_test_outcome_cases(base_kw),
        *_self_test_event_cases(base_kw),
        *_self_test_limit_cases(base_kw),
    )
    passed = True
    for label, fn, expected in cases[:MAX_SELF_TESTS]:
        matches = fn() == expected
        out.write(f"{'ok' if matches else 'FAIL'} {label}\n")
        passed = passed and matches
    return 0 if passed else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll and judge required check runs and commit statuses (ADR-0108)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", help="Prove the judge on planted positive and negative cases")
    judge = sub.add_parser("judge", help="Poll and judge required checks on a commit")
    judge.add_argument("--repository", default=None)
    judge.add_argument("--event", default=None)
    judge.add_argument("--ref", default=None)
    judge.add_argument("--sha", default=None)
    judge.add_argument("--required", default=None)
    judge.add_argument("--pull-request-only", default=None)
    judge.add_argument("--not-owed", default=None)
    judge.add_argument("--apps", default=None)
    judge.add_argument("--wait-minutes", default=None)
    judge.add_argument("--rate-limit-wait-minutes", default=None)
    return parser


def _env_or(arg: str | None, env: Mapping[str, str], key: str, default: str) -> str:
    if arg is not None:
        return arg
    return env.get(key, default)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """CLI entrypoint for `self-test` and `judge`."""
    refusal = below_floor(tuple(sys.version_info))
    if refusal is not None:
        err.write(f"::error::{refusal}\n")
        return 2
    args = _build_parser().parse_args(argv)
    if args.command == "self-test":
        return self_test(out)
    environ = os.environ if env is None else env
    try:
        config = parse_config(
            repository=_env_or(args.repository, environ, "REPOSITORY", ""),
            event_name=_env_or(args.event, environ, "EVENT_NAME", ""),
            ref=_env_or(args.ref, environ, "REF", ""),
            sha=_env_or(args.sha, environ, "SHA", ""),
            commit_sha=environ.get("COMMIT_SHA", ""),
            pr_head_sha=environ.get("PR_HEAD_SHA", ""),
            required_json=_env_or(args.required, environ, "REQUIRED_JSON", ""),
            pull_request_only_json=_env_or(
                args.pull_request_only, environ, "PULL_REQUEST_ONLY_JSON", "[]"
            ),
            not_owed_json=_env_or(args.not_owed, environ, "NOT_OWED_JSON", "[]"),
            apps_json=_env_or(args.apps, environ, "APPS_JSON", "{}"),
            wait_minutes=_env_or(args.wait_minutes, environ, "WAIT_MINUTES", "60"),
            rate_limit_wait_minutes=_env_or(
                args.rate_limit_wait_minutes, environ, "RATE_LIMIT_WAIT_MINUTES", "30"
            ),
        )
    except InputError as exc:
        err.write(f"::error title=Invalid required-checks input::{exc}\n")
        return 2
    return run_judge(config, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
