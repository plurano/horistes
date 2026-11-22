# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Judge required check runs and commit statuses on a commit in one pass (ADR-0108).

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
BOUND_MINUTES_MIN = 1
BOUND_MINUTES_MAX = 330
BOUND_MINUTES_DEFAULT = 240
MAX_TARGETS_MIN = 1
MAX_TARGETS_MAX = 100
MAX_TARGETS_DEFAULT = 30
SESSION_DEADLINE_SECONDS = 8 * 60
MAX_RATE_LIMIT_SECONDS = 300
API_BACKOFF_SECONDS = (30, 60, 120)
MAX_RATE_LIMIT_WAITS_PER_CALL = 6
MAX_CALL_ATTEMPTS = len(API_BACKOFF_SECONDS) + MAX_RATE_LIMIT_WAITS_PER_CALL + 1
CHECK_RUN_MAX_PAGES = 20
STATUS_MAX_PAGES = 20
WORKFLOW_RUN_MAX_PAGES = 10
OPEN_PULLS_MAX = 100
MAX_CHECK_RUNS = CHECK_RUN_MAX_PAGES * forge.PER_PAGE
MAX_STATUSES = STATUS_MAX_PAGES * forge.PER_PAGE
MAX_WORKFLOW_RUNS = WORKFLOW_RUN_MAX_PAGES * forge.PER_PAGE
MAX_LOGGED_FOREIGN_APPS = 20
MAX_SELF_TESTS = 48
# REST reference does not state a limit; fleet gate uses 140 characters.
STATUS_DESCRIPTION_MAX = 140
DEFAULT_APP = "github-actions"
DEFAULT_CONTEXT = "required-checks"
DEFAULT_SELF_WORKFLOW = ".github/workflows/required-checks.yml"
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
TARGETS_SHA = "sha"
TARGETS_OPEN_PULLS = "open-pulls"
ALLOWED_TARGETS = frozenset({TARGETS_SHA, TARGETS_OPEN_PULLS})
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
INT_RE = re.compile(r"^-?[0-9]+$")
SELF_WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
HTTPS_URL_RE = re.compile(r"^https://", re.IGNORECASE)


class InputError(Exception):
    """Malformed or out-of-range caller input detected before any API call."""


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
    bound_minutes: int
    context: str
    self_workflow: str
    targets: str
    max_targets: int
    only_if_pending: bool
    target_url: str


@dataclass(frozen=True)
class Snapshot:
    """Newest check runs and commit statuses on the target SHA."""

    checks_by_name: dict[str, dict]
    statuses_by_context: dict[str, dict]
    ignored_foreign_apps: tuple[str, ...]
    anchor: float


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


def _parse_bool(label: str, raw: str | bool) -> bool:
    """Parse true/false caller booleans."""
    if isinstance(raw, bool):
        return raw
    if not isinstance(raw, str):
        raise InputError(f"{label}: must be true or false, got {raw!r}")
    token = raw.strip().lower()
    if token == "true":
        return True
    if token == "false":
        return False
    raise InputError(f"{label}: must be true or false, got {raw!r}")


def _validate_context_name(label: str, item: object) -> str:
    """Require a non-empty single-line string context name."""
    if not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item:
        raise InputError(f"{label}: context names must be non-empty single-line strings")
    return item


def _parse_context(raw: str) -> str:
    """Parse the status context to post."""
    return _validate_context_name("context", raw.strip())


def _parse_self_workflow(raw: str) -> str:
    """Validate a workflow file path under .github/workflows/."""
    path = raw.strip()
    if not SELF_WORKFLOW_RE.fullmatch(path):
        raise InputError(
            f"self-workflow: {path!r} must match .github/workflows/<name>.yml or .yaml"
        )
    return path


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


def _reject_self_context(context: str, required: frozenset[str], label: str) -> None:
    if context in required:
        raise InputError(f"{label}: context {context!r} must not appear in required")


def _parse_target_url(raw: str) -> str:
    """Optional target URL for the posted status; only https:// is sent."""
    url = raw.strip()
    if not url:
        return ""
    if not HTTPS_URL_RE.match(url):
        raise InputError(f"target-url: {url!r} must start with https://")
    return url


def _resolve_sha(event_name: str, explicit_sha: str, commit_sha: str, pr_head_sha: str) -> str:
    """Select and validate the commit SHA to judge for the current event."""
    if explicit_sha:
        chosen = explicit_sha.strip()
    elif event_name in PR_EVENTS:
        chosen = pr_head_sha.strip()
    else:
        chosen = commit_sha.strip()
    if not SHA_RE.fullmatch(chosen):
        raise InputError(f"sha: invalid commit SHA {chosen!r}")
    return chosen


def _parse_config_fields(
    *,
    required_json: str,
    pull_request_only_json: str,
    not_owed_json: str,
    apps_json: str,
    bound_minutes: str | int,
    context: str,
    self_workflow: str,
    max_targets: str | int,
    only_if_pending: str | bool,
    target_url: str,
) -> tuple[
    tuple[str, ...],
    frozenset[str],
    frozenset[str],
    dict[str, str],
    int,
    str,
    str,
    int,
    bool,
    str,
]:
    """Parse list, bound, and posting fields shared by every targets mode."""
    required = _parse_required(required_json)
    allowed = frozenset(required)
    pr_only = _parse_subset("pull-request-only", pull_request_only_json, allowed)
    not_owed = _parse_subset("not-owed", not_owed_json, allowed)
    apps = _parse_apps(apps_json, allowed)
    bound = _parse_int_range("bound-minutes", bound_minutes, BOUND_MINUTES_MIN, BOUND_MINUTES_MAX)
    ctx = _parse_context(context)
    self_wf = _parse_self_workflow(self_workflow)
    _reject_self_context(ctx, allowed, "required")
    _reject_self_context(ctx, pr_only, "pull-request-only")
    _reject_self_context(ctx, not_owed, "not-owed")
    max_t = _parse_int_range("max-targets", max_targets, MAX_TARGETS_MIN, MAX_TARGETS_MAX)
    pending_only = _parse_bool("only-if-pending", only_if_pending)
    url = _parse_target_url(target_url)
    return required, pr_only, not_owed, apps, bound, ctx, self_wf, max_t, pending_only, url


def _config_from_parsed(
    repo: str,
    event: str,
    ref: str,
    resolved_sha: str,
    mode: str,
    parsed: tuple[
        tuple[str, ...],
        frozenset[str],
        frozenset[str],
        dict[str, str],
        int,
        str,
        str,
        int,
        bool,
        str,
    ],
) -> Config:
    required, pr_only, not_owed, apps, bound, ctx, self_wf, max_t, pending_only, url = parsed
    return Config(
        repository=repo,
        sha=resolved_sha,
        event_name=event,
        ref=ref,
        required=required,
        pull_request_only=pr_only,
        not_owed=not_owed,
        apps=apps,
        bound_minutes=bound,
        context=ctx,
        self_workflow=self_wf,
        targets=mode,
        max_targets=max_t,
        only_if_pending=pending_only,
        target_url=url,
    )


def parse_config(
    *,
    repository: str,
    event_name: str = "",
    ref: str = "",
    sha: str = "",
    commit_sha: str = "",
    pr_head_sha: str = "",
    required_json: str,
    pull_request_only_json: str = "[]",
    not_owed_json: str = "[]",
    apps_json: str = "{}",
    bound_minutes: str | int = BOUND_MINUTES_DEFAULT,
    context: str = DEFAULT_CONTEXT,
    self_workflow: str = DEFAULT_SELF_WORKFLOW,
    targets: str = TARGETS_SHA,
    max_targets: str | int = MAX_TARGETS_DEFAULT,
    only_if_pending: str | bool = False,
    target_url: str = "",
) -> Config:
    """Validate every caller input before any forge API request is made."""
    repo = repository.strip()
    if not REPO_RE.fullmatch(repo) or repo.split("/", 1)[1] in (".", ".."):
        raise InputError(f"repository: invalid owner/name {repository!r}")
    event = event_name.strip()
    mode = targets.strip().lower()
    if mode not in ALLOWED_TARGETS:
        raise InputError(f"targets: {targets!r} must be sha or open-pulls")
    if mode == TARGETS_SHA and not event:
        raise InputError("event: event name must not be empty")
    resolved_sha = _resolve_sha(event, sha, commit_sha, pr_head_sha) if mode == TARGETS_SHA else ""
    parsed = _parse_config_fields(
        required_json=required_json,
        pull_request_only_json=pull_request_only_json,
        not_owed_json=not_owed_json,
        apps_json=apps_json,
        bound_minutes=bound_minutes,
        context=context,
        self_workflow=self_workflow,
        max_targets=max_targets,
        only_if_pending=only_if_pending,
        target_url=target_url,
    )
    return _config_from_parsed(repo, event, ref.strip(), resolved_sha, mode, parsed)


class ApiSession:
    """Bounded forge client with 5xx outage backoff and rate-limit budget tracking."""

    def __init__(
        self,
        *,
        deadline: float,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable[..., tuple[int, dict[str, str], bytes]] = forge.request,
        out: TextIO = sys.stdout,
    ) -> None:
        self.deadline = deadline
        self.now = now
        self.sleep = sleep
        self.transport = transport
        self.out = out
        self.rate_limit_waited_seconds = 0.0
        self.rate_limit_wait_count = 0

    def _within_rate_limit_bounds(self, delay: int | float, waits: int) -> bool:
        if waits >= MAX_RATE_LIMIT_WAITS_PER_CALL:
            return False
        if self.rate_limit_waited_seconds + delay > MAX_RATE_LIMIT_SECONDS:
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
                "after backoff budget. No check result was judged; rerun this job."
            ) from err
        seconds = API_BACKOFF_SECONDS[attempt]
        if self.now() + seconds > self.deadline:
            raise ApiFailureError(
                f"Transient GitHub API failure: {label} still answers HTTP {err.status} "
                "at the session deadline. No check result was judged; rerun this job."
            ) from err
        self.out.write(
            f"::warning::{label}: GitHub API HTTP {err.status}, retrying in {seconds} s\n"
        )
        self.sleep(seconds)

    def _call_deadline(self) -> float:
        """Per-call transport deadline capped by the remaining session budget."""
        remaining = self.deadline - self.now()
        if remaining <= 0:
            raise ApiFailureError(
                f"Session deadline of {SESSION_DEADLINE_SECONDS} s reached before the next "
                "GitHub API call. No check result was judged; rerun this job."
            )
        return min(forge.DEADLINE_SECONDS, remaining)

    def request_json(self, label: str, path: str, *, method: str = "GET", **kw: object) -> object:
        """Fetch and decode one JSON endpoint with bounded rate-limit and 5xx retries."""
        server_attempts = 0
        rate_limit_waits = 0
        for _ in range(MAX_CALL_ATTEMPTS):
            if self.now() >= self.deadline:
                raise ApiFailureError(
                    f"Session deadline of {SESSION_DEADLINE_SECONDS} s reached during {label}. "
                    "No check result was judged; rerun this job."
                )
            try:
                call_deadline = self._call_deadline()
                _, _, body = self.transport(path, method=method, deadline=call_deadline, **kw)
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


def self_workflow_suites(workflow_runs: list[dict], self_workflow: str) -> set[int]:
    """Check-suite ids owned by runs of this judge's own workflow file."""
    suites: set[int] = set()
    for run in workflow_runs[:MAX_WORKFLOW_RUNS]:
        if run.get("path") == self_workflow:
            suite_id = _int_id(run.get("check_suite_id"))
            if suite_id:
                suites.add(suite_id)
    return suites


def _workflow_anchor(workflow_runs: list[dict], self_workflow: str) -> float | None:
    """Earliest created_at among non-self workflow runs."""
    earliest: float | None = None
    for run in workflow_runs[:MAX_WORKFLOW_RUNS]:
        if run.get("path") == self_workflow:
            continue
        stamp = _iso_epoch(run.get("created_at"))
        if stamp <= 0:
            continue
        if earliest is None or stamp < earliest:
            earliest = stamp
    return earliest


def _commit_anchor(session: ApiSession, repository: str, sha: str) -> float:
    """Committer date of the commit when no workflow runs exist."""
    path = f"repos/{repository}/commits/{sha}"
    data = session.request_json("commit", path)
    commit = data.get("commit") if isinstance(data, dict) else None
    if not isinstance(commit, dict):
        return session.now()
    committer = commit.get("committer")
    if not isinstance(committer, dict):
        return session.now()
    stamp = _iso_epoch(committer.get("date"))
    return stamp if stamp > 0 else session.now()


def _check_run_admitted(
    run: dict,
    excluded_suites: set[int],
    apps: Mapping[str, str],
    self_context: str,
) -> tuple[str | None, str | None]:
    """Return (admitted_name, foreign_app_note) for one check-run entry."""
    name = run.get("name")
    if not isinstance(name, str) or not name or name == self_context:
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
    self_context: str,
) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Select the newest check run per name from its expected app outside foreign suites."""
    by_name: dict[str, dict] = {}
    ranks: dict[str, tuple[float, int]] = {}
    ignored: list[str] = []
    seen_ignored: set[str] = set()
    for run in check_runs[:MAX_CHECK_RUNS]:
        admitted_name, foreign_note = _check_run_admitted(run, excluded_suites, apps, self_context)
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


def newest_statuses(statuses: list[dict], self_context: str) -> dict[str, dict]:
    """Select the newest commit status per context except the self context."""
    by_context: dict[str, dict] = {}
    ranks: dict[str, tuple[float, int]] = {}
    for entry in statuses[:MAX_STATUSES]:
        context = entry.get("context")
        if not isinstance(context, str) or not context or context == self_context:
            continue
        stamp = entry.get("updated_at") or entry.get("created_at")
        rank = (_iso_epoch(stamp), _int_id(entry.get("id")))
        if context not in ranks or rank >= ranks[context]:
            ranks[context] = rank
            by_context[context] = entry
    return by_context


def _all_statuses(statuses: list[dict]) -> dict[str, dict]:
    """Newest commit status per context, including the self context."""
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


def read_statuses(session: ApiSession, repository: str, sha: str) -> dict[str, dict]:
    """Commit statuses on `sha`, including the self context."""
    path = f"repos/{repository}/commits/{sha}/statuses"
    statuses = forge.paged(
        path,
        None,
        max_pages=STATUS_MAX_PAGES,
        fetch=lambda p: session.request_json("statuses", p),
    )
    return _all_statuses(statuses)


def read_snapshot(
    session: ApiSession,
    config: Config,
    statuses: list[dict] | Mapping[str, dict] | None = None,
) -> Snapshot:
    """Read check runs and workflow runs; reuse commit statuses when already fetched."""
    checks_path = f"repos/{config.repository}/commits/{config.sha}/check-runs"
    runs_path = f"repos/{config.repository}/actions/runs?head_sha={config.sha}"
    try:
        check_runs = forge.paged(
            checks_path,
            "check_runs",
            max_pages=CHECK_RUN_MAX_PAGES,
            fetch=lambda p: session.request_json("check-runs", p),
        )
        workflow_runs = forge.paged(
            runs_path,
            "workflow_runs",
            max_pages=WORKFLOW_RUN_MAX_PAGES,
            fetch=lambda p: session.request_json("workflow-runs", p),
        )
        if statuses is None:
            status_list = list(read_statuses(session, config.repository, config.sha).values())
        elif isinstance(statuses, Mapping):
            status_list = list(statuses.values())
        else:
            status_list = list(statuses)
    except forge.ForgeError as err:
        raise ApiFailureError(f"GitHub API error: {err}") from err
    excluded = foreign_suites(workflow_runs, config.event_name, config.ref)
    excluded |= self_workflow_suites(workflow_runs, config.self_workflow)
    by_name, ignored = newest_check_runs(
        check_runs,
        excluded,
        config.apps,
        frozenset(config.required),
        config.context,
    )
    anchor = _workflow_anchor(workflow_runs, config.self_workflow)
    if anchor is None:
        anchor = _commit_anchor(session, config.repository, config.sha)
    return Snapshot(
        checks_by_name=by_name,
        statuses_by_context=newest_statuses(status_list, config.context),
        ignored_foreign_apps=ignored,
        anchor=anchor,
    )


def exempt_reason(name: str, config: Config) -> str:
    """Exemption reason for `name` on this event, or empty string when `name` is owed."""
    if name in config.not_owed:
        return "not owed"
    if name in config.pull_request_only and config.event_name not in PR_EVENTS:
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


def _truncate_description(text: str) -> str:
    if len(text) <= STATUS_DESCRIPTION_MAX:
        return text
    return text[: STATUS_DESCRIPTION_MAX - 3] + "..."


def decide(
    snapshot: Snapshot,
    config: Config,
    now: float,
    anchor: float,
) -> tuple[str, str]:
    """Return (state, description) with state in success, failure, or pending."""
    failures: list[str] = []
    for name in config.required[:MAX_CONTEXTS]:
        reason = exempt_reason(name, config)
        run, status = _records_for(name, snapshot, config)
        if reason:
            passed, line = judge_context(name, snapshot, config)
            if not passed:
                failures.append(line)
            continue
        if run is None and status is None:
            continue
        if _is_context_pending(run, status):
            continue
        passed, line = judge_context(name, snapshot, config)
        if not passed:
            failures.append(line)
    if failures:
        desc = failures[0]
        if len(failures) > 1:
            desc = f"{desc} (+{len(failures) - 1} more)"
        return "failure", _truncate_description(desc)
    missing, waiting_pending = owed_waiting(snapshot, config)
    waiting = missing + waiting_pending
    if not waiting:
        return "success", _truncate_description("All owed required checks reported success.")
    shown = ", ".join(waiting[:5])
    if len(waiting) > 5:
        shown = f"{shown} (+{len(waiting) - 5} more)"
    if now - anchor <= config.bound_minutes * 60:
        text = f"waiting on {len(waiting)}: {shown}"
        return "pending", _truncate_description(text)
    text = f"deadline of {config.bound_minutes} min passed; waiting on: {shown}"
    return "failure", _truncate_description(text)


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


def _status_fields(config: Config, state: str, description: str) -> dict[str, str]:
    fields = {"state": state, "context": config.context, "description": description}
    if config.target_url:
        fields["target_url"] = config.target_url
    return fields


def _should_post(
    statuses: Mapping[str, dict], config: Config, state: str, description: str
) -> bool:
    newest = statuses.get(config.context)
    if not newest:
        return True
    return not (
        str(newest.get("state") or "") == state
        and str(newest.get("description") or "") == description
    )


def post_commit_status(
    session: ApiSession,
    config: Config,
    state: str,
    description: str,
    statuses: Mapping[str, dict],
) -> None:
    """POST a commit status when state or description changed."""
    if not _should_post(statuses, config, state, description):
        session.out.write(
            f"No status post: {config.context} on {config.sha} already has {state!r}\n"
        )
        return
    path = f"repos/{config.repository}/statuses/{config.sha}"
    fields = _status_fields(config, state, description)
    session.request_json("post-status", path, method="POST", fields=fields)
    session.out.write(f"Posted {state} for {config.context} on {config.sha}\n")


def evaluate_one(
    session: ApiSession,
    config: Config,
    out: TextIO,
    err: TextIO,
    *,
    statuses_map: Mapping[str, dict] | None = None,
) -> int:
    """Read, decide and post for one SHA; return 1 only on an evaluation error."""
    if statuses_map is None:
        try:
            statuses_map = read_statuses(session, config.repository, config.sha)
        except ApiFailureError as exc:
            err.write(f"::error::{exc}\n")
            return 1
    newest = statuses_map.get(config.context)
    if config.only_if_pending and newest:
        settled = str(newest.get("state") or "")
        if settled in ("success", "failure"):
            out.write(f"{config.sha}: {config.context} already settled\n")
            return 0
    try:
        snapshot = read_snapshot(session, config, statuses_map)
    except ApiFailureError as exc:
        err.write(f"::error::{exc}\n")
        return 1
    judge_snapshot(snapshot, config, out)
    state, description = decide(snapshot, config, session.now(), snapshot.anchor)
    try:
        post_commit_status(session, config, state, description, statuses_map)
    except ApiFailureError as exc:
        err.write(f"::error::{exc}\n")
        return 1
    if state == "failure":
        out.write(f"{config.sha}: {description}\n")
        return 0
    if state == "success":
        out.write("All owed required checks reported success.\n")
    return 0


def _open_pull_head_shas(session: ApiSession, repository: str) -> list[str]:
    """Distinct head SHAs from one open-pulls page, at most OPEN_PULLS_MAX entries."""
    path = f"repos/{repository}/pulls?state=open&per_page={OPEN_PULLS_MAX}"
    pulls = session.request_json("open-pulls", path)
    if not isinstance(pulls, list):
        raise ApiFailureError("open pulls response was not a list")
    heads: list[str] = []
    for item in pulls[:OPEN_PULLS_MAX]:
        if not isinstance(item, dict):
            continue
        head = item.get("head")
        if not isinstance(head, dict):
            continue
        sha = head.get("sha")
        if isinstance(sha, str) and SHA_RE.fullmatch(sha) and sha not in heads:
            heads.append(sha)
    return heads


def _head_priority(statuses_map: Mapping[str, dict], context: str) -> tuple[bool, float]:
    """Sort key: never-judged heads first, then longest since the last judgment."""
    entry = statuses_map.get(context)
    if not entry:
        return (False, 0.0)
    stamp = _iso_epoch(entry.get("updated_at") or entry.get("created_at"))
    return (True, stamp)


def _sweep_evaluation_order(
    session: ApiSession,
    repository: str,
    heads: list[str],
    context: str,
    max_targets: int,
    out: TextIO,
) -> list[tuple[str, dict[str, dict]]]:
    """Read each head once, skip settled, return up to `max_targets` unsettled heads."""
    pending: list[tuple[str, dict[str, dict], tuple[bool, float], int]] = []
    for order, sha in enumerate(heads):
        statuses_map = read_statuses(session, repository, sha)
        newest = statuses_map.get(context)
        if newest and str(newest.get("state") or "") in ("success", "failure"):
            out.write(f"{sha}: {context} already settled\n")
            continue
        pending.append((sha, statuses_map, _head_priority(statuses_map, context), order))
    pending.sort(key=lambda row: (row[2], row[3]))
    return [(sha, statuses_map) for sha, statuses_map, _, _ in pending[:max_targets]]


def _default_branch_head(session: ApiSession, repository: str) -> str:
    """Head SHA of the repository default branch."""
    repo = session.request_json("repository", f"repos/{repository}")
    if not isinstance(repo, dict):
        raise ApiFailureError("repository response was not an object")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise ApiFailureError("repository has no default_branch")
    commit = session.request_json("default-commit", f"repos/{repository}/commits/{branch}")
    if not isinstance(commit, dict):
        raise ApiFailureError("default branch commit response was not an object")
    sha = commit.get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ApiFailureError("default branch commit has no sha")
    return sha


def run_sweep(
    config: Config,
    *,
    transport: Callable[..., tuple[int, dict[str, str], bytes]] = forge.request,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Judge open pull heads and the default branch head in one session."""
    session = ApiSession(
        deadline=now() + SESSION_DEADLINE_SECONDS,
        now=now,
        sleep=sleep,
        transport=transport,
        out=out,
    )
    sweep_cfg = Config(
        **{**config.__dict__, "only_if_pending": True, "targets": TARGETS_OPEN_PULLS}
    )
    any_error = False
    try:
        heads = _open_pull_head_shas(session, config.repository)
        to_eval = _sweep_evaluation_order(
            session,
            config.repository,
            heads,
            config.context,
            config.max_targets,
            out,
        )
        default_sha = _default_branch_head(session, config.repository)
    except ApiFailureError as exc:
        err.write(f"::error::{exc}\n")
        return 1
    for sha, statuses_map in to_eval:
        if session.now() >= session.deadline:
            err.write(
                f"::error::Session deadline of {SESSION_DEADLINE_SECONDS} s reached before "
                f"judging {sha}. No check result was judged; rerun this job.\n"
            )
            return 1
        one = Config(**{**sweep_cfg.__dict__, "sha": sha, "event_name": "pull_request"})
        if evaluate_one(session, one, out, err, statuses_map=statuses_map) != 0:
            any_error = True
            break
    if not any_error:
        if session.now() >= session.deadline:
            err.write(
                f"::error::Session deadline of {SESSION_DEADLINE_SECONDS} s reached before "
                "judging the default branch. No check result was judged; rerun this job.\n"
            )
            return 1
        default_cfg = Config(
            **{**sweep_cfg.__dict__, "sha": default_sha, "event_name": "push", "ref": ""}
        )
        if evaluate_one(session, default_cfg, out, err) != 0:
            any_error = True
    return 1 if any_error else 0


def run_judge(
    config: Config,
    *,
    transport: Callable[..., tuple[int, dict[str, str], bytes]] = forge.request,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Execute one-shot judgment for a validated Config."""
    if config.targets == TARGETS_OPEN_PULLS:
        return run_sweep(config, transport=transport, now=now, sleep=sleep, out=out, err=err)
    session = ApiSession(
        deadline=now() + SESSION_DEADLINE_SECONDS,
        now=now,
        sleep=sleep,
        transport=transport,
        out=out,
    )
    return evaluate_one(session, config, out, err)


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
    commit_date: str | None = None,
) -> Callable[..., tuple[int, dict[str, str], bytes]]:
    """Build an in-memory transport serving canned check-runs, workflow-runs and statuses."""
    pending_errors = list(errors or [])
    status_list = list(statuses or [])
    posts: list[dict[str, str]] = []

    def _base(path: str) -> str:
        return path.split("?", 1)[0]

    def serve(
        path: str, *, method: str = "GET", fields: Mapping[str, str] | None = None, **_: object
    ):
        if method == "POST":
            posts.append(dict(fields or {}))
            return _ok_reply({"id": len(posts), "state": (fields or {}).get("state")})
        if pending_errors:
            raise pending_errors.pop(0)
        base = _base(path)
        if base.endswith("/check-runs"):
            return _ok_reply({"check_runs": check_runs})
        if "/actions/runs" in base:
            return _ok_reply({"workflow_runs": workflow_runs or []})
        if base.endswith("/statuses"):
            return _ok_reply(status_list)
        if "/commits/" in base and "/check-runs" not in base:
            date = commit_date or "2026-11-21T10:00:00Z"
            return _ok_reply({"commit": {"committer": {"date": date}}})
        if base.endswith("/pulls"):
            return _ok_reply([])
        return _ok_reply(status_list)

    serve.posts = posts  # type: ignore[attr-defined]
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
    config: Config,
    *,
    snapshot: Snapshot,
    now: float,
) -> tuple[str, str]:
    return decide(snapshot, config, now, snapshot.anchor)


def _run_case_output(
    config: Config,
    *,
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
    anchor_runs: list[dict] | None = None,
) -> tuple[int, str]:
    clock = _FakeClock(start=_iso_epoch("2026-11-21T12:00:00Z"))
    sink = io.StringIO()
    runs = workflow_runs if workflow_runs is not None else anchor_runs or []
    transport = _self_test_transport(
        check_runs, runs, statuses, errors, commit_date="2026-11-21T10:00:00Z"
    )
    rc = run_judge(
        config,
        transport=transport,
        now=clock.now,
        sleep=clock.sleep,
        out=sink,
        err=sink,
    )
    return rc, sink.getvalue()


def _run_case(
    config: Config,
    *,
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
    errors: list[forge.ResponseError] | None = None,
    anchor_runs: list[dict] | None = None,
) -> int:
    rc, _ = _run_case_output(
        config,
        check_runs=check_runs,
        workflow_runs=workflow_runs,
        statuses=statuses,
        errors=errors,
        anchor_runs=anchor_runs,
    )
    return rc


def _failure_verdict_case(
    config: Config,
    *,
    check_runs: list[dict],
    workflow_runs: list[dict] | None = None,
    statuses: list[dict] | None = None,
) -> int:
    """Return 0 when the run exits cleanly and names a failure verdict line."""
    rc, text = _run_case_output(
        config,
        check_runs=check_runs,
        workflow_runs=workflow_runs,
        statuses=statuses,
    )
    if rc != 0 or f"{config.sha}:" not in text:
        return 1
    return 0


def _self_test_outcome_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for check runs, apps and statuses."""
    kw = {**base_kw, "bound_minutes": 240}
    return (
        (
            "owed check and status pass",
            lambda: _run_case(
                parse_config(
                    **kw,
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
            lambda: _failure_verdict_case(
                parse_config(**{**kw, "bound_minutes": 1}, required_json='["Build"]'),
                check_runs=[],
            ),
            0,
        ),
        (
            "skipped owed check fails",
            lambda: _failure_verdict_case(
                parse_config(**kw, required_json='["Build"]'),
                check_runs=[_check_run(1, "Build", "skipped")],
            ),
            0,
        ),
        (
            "wrong app check run fails",
            lambda: _failure_verdict_case(
                parse_config(**{**kw, "bound_minutes": 1}, required_json='["Build"]'),
                check_runs=[_check_run(1, "Build", "success", app="impostor-app")],
            ),
            0,
        ),
        (
            "failed commit status fails",
            lambda: _failure_verdict_case(
                parse_config(**kw, required_json='["External"]'),
                check_runs=[],
                statuses=[{"id": 1, "context": "External", "state": "failure"}],
            ),
            0,
        ),
    )


def _self_test_event_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for foreign-event suite exclusion."""
    kw = {**base_kw, "bound_minutes": 240}
    return (
        (
            "other-event schedule failure ignored when own run succeeds",
            lambda: _run_case(
                parse_config(**kw, required_json='["Build"]'),
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
            lambda: _failure_verdict_case(
                parse_config(**{**kw, "bound_minutes": 1}, required_json='["Build"]'),
                check_runs=[_check_run(2, "Build", "success", suite_id=20)],
                workflow_runs=[
                    {"id": 20, "event": "schedule", "head_branch": "main", "check_suite_id": 20},
                ],
            ),
            0,
        ),
    )


def _self_test_limit_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for API rate-limit bounds."""
    kw = {**base_kw, "bound_minutes": 10}
    rl_err = forge.ResponseError(
        "rate limit", status=429, headers={"retry-after": "120"}, message="API rate limit exceeded"
    )
    return (
        (
            "rate-limit wait within budget recovers",
            lambda: _run_case(
                parse_config(**kw, required_json='["Build"]'),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err],
            ),
            0,
        ),
        (
            "rate-limit wait over budget fails without judging",
            lambda: _run_case(
                parse_config(**kw, required_json='["Build"]'),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err, rl_err, rl_err, rl_err],
            ),
            1,
        ),
    )


def _self_test_pr_only_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for pull-request-only contexts across events."""
    pr_target_kw = {
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
                parse_config(
                    **base_kw,
                    required_json='["Build","PR Gate"]',
                    pull_request_only_json='["PR Gate"]',
                ),
                check_runs=[_check_run(1, "Build", "success")],
            ),
            0,
        ),
        (
            "pull-request-only absent on pull_request_target fails",
            lambda: _failure_verdict_case(
                parse_config(
                    **pr_target_kw,
                    required_json='["Build","PR Gate"]',
                    pull_request_only_json='["PR Gate"]',
                ),
                check_runs=[_check_run(1, "Build", "success")],
            ),
            0,
        ),
    )


def _self_test_vmafx_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """Five SHAs with queued owed siblings each return pending; sleep is never called."""
    names = ["A", "B", "C", "D", "E"]

    def five_shas_no_sleep() -> bool:
        checks = [_check_run(i + 1, name, None, status="queued") for i, name in enumerate(names)]
        for idx in range(5):
            sha = f"{idx:040x}"
            clock = _FakeClock(start=_iso_epoch("2026-11-21T12:00:00Z"))
            merged = dict(base_kw)
            merged.update(sha=sha, required_json=json.dumps(names), bound_minutes=240)
            cfg = parse_config(**merged)
            transport = _self_test_transport(
                checks, [], [], None, commit_date="2026-11-21T10:00:00Z"
            )
            rc = run_judge(
                cfg,
                transport=transport,
                now=clock.now,
                sleep=clock.sleep,
                out=io.StringIO(),
                err=io.StringIO(),
            )
            if rc != 0 or clock.sleeps:
                return False
            snap = read_snapshot(
                ApiSession(
                    deadline=clock.now() + 60, now=clock.now, sleep=clock.sleep, transport=transport
                ),
                cfg,
            )
            state, _ = decide(snap, cfg, clock.now(), snap.anchor)
            if state != "pending":
                return False
        return True

    return (("vmafx five queued SHAs pending without sleep", five_shas_no_sleep, True),)


def _self_test_decide_cases(
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """Bounded decide() cases."""
    anchor = 1_700_000_000.0

    def snap_with(*checks: dict, anchor_val: float = anchor) -> Snapshot:
        by_name = {str(c["name"]): c for c in checks}
        return Snapshot(by_name, {}, (), anchor_val)

    cfg = parse_config(**base_kw, required_json='["X","Y"]', bound_minutes=60)

    def all_success() -> bool:
        state, _ = _decide_case(
            cfg,
            snapshot=snap_with(_check_run(1, "X", "success"), _check_run(2, "Y", "success")),
            now=anchor + 10,
        )
        return state == "success"

    def fail_while_queued() -> bool:
        state, _ = _decide_case(
            cfg,
            snapshot=snap_with(_check_run(1, "X", "failure"), _check_run(2, "Y", None)),
            now=anchor + 10,
        )
        return state == "failure"

    def pending_at_bound() -> bool:
        state, _ = _decide_case(
            cfg,
            snapshot=snap_with(_check_run(1, "Y", None)),
            now=anchor + 60 * 60,
        )
        return state == "pending"

    def failure_after_bound() -> bool:
        state, desc = _decide_case(
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
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], bool], bool], ...]:
    """decide() at the bound when the only producer is a foreign app."""
    anchor = 1_700_000_000.0
    app_cfg = parse_config(
        **base_kw,
        required_json='["Scan"]',
        apps_json='{"Scan":"code-scanning"}',
        bound_minutes=60,
    )
    empty = Snapshot({}, {}, (), anchor)

    def foreign_app_pending_at_bound() -> bool:
        state, _ = decide(empty, app_cfg, anchor + 60 * 60, anchor)
        return state == "pending"

    def foreign_app_failure_after_bound() -> bool:
        state, desc = decide(empty, app_cfg, anchor + 60 * 60 + 1, anchor)
        return state == "failure" and "deadline" in desc

    return (
        ("decide foreign-app-only pending at bound", foreign_app_pending_at_bound, True),
        ("decide foreign-app-only failure after bound", foreign_app_failure_after_bound, True),
    )


def self_test(out: TextIO = sys.stdout) -> int:
    """Run planted positive and negative cases in memory; return 0 when all match."""
    base_kw = {
        "repository": "plurano/horistes",
        "sha": "a" * 40,
        "event_name": "push",
        "ref": "refs/heads/main",
    }
    cases: list[tuple[str, Callable[[], int] | Callable[[], bool], int | bool]] = [
        *_self_test_outcome_cases(base_kw),
        *_self_test_event_cases(base_kw),
        *_self_test_limit_cases(base_kw),
        *_self_test_pr_only_cases(base_kw),
    ]
    bool_cases = (
        *_self_test_vmafx_cases(base_kw),
        *_self_test_decide_cases(base_kw),
        *_self_test_foreign_app_decide_cases(base_kw),
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Judge required check runs and commit statuses in one pass (ADR-0108)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", help="Prove the judge on planted positive and negative cases")
    judge = sub.add_parser("judge", help="Judge required checks on a commit")
    judge.add_argument("--repository", default=None)
    judge.add_argument("--event", default=None)
    judge.add_argument("--ref", default=None)
    judge.add_argument("--sha", default=None)
    judge.add_argument("--required", default=None)
    judge.add_argument("--pull-request-only", default=None)
    judge.add_argument("--not-owed", default=None)
    judge.add_argument("--apps", default=None)
    judge.add_argument("--bound-minutes", default=None)
    judge.add_argument("--context", default=None)
    judge.add_argument("--self-workflow", default=None)
    judge.add_argument("--targets", default=None)
    judge.add_argument("--max-targets", default=None)
    judge.add_argument("--only-if-pending", default=None)
    judge.add_argument("--target-url", default=None)
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
    prefix = "REQUIRED_CHECKS_"
    try:
        config = parse_config(
            repository=_env_or(args.repository, environ, "REPOSITORY", ""),
            event_name=_env_or(
                args.event, environ, f"{prefix}EVENT", environ.get("EVENT_NAME", "")
            ),
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
            bound_minutes=_env_or(
                args.bound_minutes, environ, f"{prefix}BOUND_MINUTES", str(BOUND_MINUTES_DEFAULT)
            ),
            context=_env_or(args.context, environ, f"{prefix}CONTEXT", DEFAULT_CONTEXT),
            self_workflow=_env_or(
                args.self_workflow, environ, f"{prefix}SELF_WORKFLOW", DEFAULT_SELF_WORKFLOW
            ),
            targets=_env_or(args.targets, environ, f"{prefix}TARGETS", TARGETS_SHA),
            max_targets=_env_or(
                args.max_targets, environ, f"{prefix}MAX_TARGETS", str(MAX_TARGETS_DEFAULT)
            ),
            only_if_pending=_env_or(
                args.only_if_pending, environ, f"{prefix}ONLY_IF_PENDING", "false"
            ),
            target_url=_env_or(args.target_url, environ, f"{prefix}TARGET_URL", ""),
        )
    except InputError as exc:
        err.write(f"::error title=Invalid required-checks input::{exc}\n")
        return 2
    return run_judge(config, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
