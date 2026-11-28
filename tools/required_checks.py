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
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, auto
from math import ceil, floor
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

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
MAX_RUN_NUMBER = 10**9
SESSION_DEADLINE_SECONDS = 8 * 60
WITHDRAWAL_RESERVE_SECONDS = 30
WORK_DEADLINE_SECONDS = SESSION_DEADLINE_SECONDS - WITHDRAWAL_RESERVE_SECONDS
POST_FRESH_SECONDS = 10
SWEEP_MAX_CALLS = 250
TRIAGE_MAX_CALLS = 120
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
PUSH_CONTEXT_SUFFIX = " (push)"
DEFAULT_SELF_WORKFLOW = ".github/workflows/required-checks.yml"
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
PROPOSAL_EVENTS = PR_EVENTS | {"merge_group"}
TARGETS_SHA = "sha"
TARGETS_OPEN_PULLS = "open-pulls"
ALLOWED_TARGETS = frozenset({TARGETS_SHA, TARGETS_OPEN_PULLS})
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
INT_RE = re.compile(r"^-?[0-9]{1,6}$")
SELF_WORKFLOW_RE = re.compile(r"^\.github/workflows/[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml$")
HTTPS_URL_RE = re.compile(r"^https://", re.IGNORECASE)
WORKFLOW_REF_SUFFIX_RE = re.compile(r"[^\s\x00-\x1f\x7f]+")
MAX_JSON_DIAGNOSTIC = 120


class InputError(Exception):
    """Malformed or out-of-range caller input detected before any API call."""


class ApiFailureError(Exception):
    """Unrecoverable API error, transient 5xx timeout, or rate-limit bound exceeded."""


class WorkDeadlineError(ApiFailureError):
    """A read, retry or delay cannot finish before the session work cutoff."""


class ServerBackoffDeadlineError(WorkDeadlineError):
    """A server-error retry backoff does not fit before the session work cutoff."""


class TransportFailureError(ApiFailureError):
    """A transport attempt ended without an HTTP answer; the request may have reached GitHub."""


class ApiCapReachedError(Exception):
    """Sweep session reached SWEEP_MAX_CALLS before every target was judged."""


class PostFailureError(ApiFailureError):
    """A status write failed with an HTTP response."""

    def __init__(self, label: str, response: forge.ResponseError) -> None:
        super().__init__(f"GitHub API error ({label}): {response}")
        self.response = response


@dataclass(frozen=True, kw_only=True)
class RawInputs:
    """Caller inputs before normalization and validation."""

    repository: str
    event_name: str = ""
    ref: str = ""
    sha: str = ""
    commit_sha: str = ""
    pr_head_sha: str = ""
    required_json: str
    pull_request_only_json: str = "[]"
    not_owed_json: str = "[]"
    apps_json: str = "{}"
    bound_minutes: str | int = BOUND_MINUTES_DEFAULT
    context: str = DEFAULT_CONTEXT
    self_workflow: str = DEFAULT_SELF_WORKFLOW
    targets: str = TARGETS_SHA
    max_targets: str | int = MAX_TARGETS_DEFAULT
    run_number: str | int = ""
    only_if_pending: str | bool = False
    target_url: str = ""


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
    run_number: int
    only_if_pending: bool
    target_url: str


def status_context(config: Config) -> str:
    """Return the status context for the event's check policy."""
    if config.event_name in PROPOSAL_EVENTS:
        return config.context
    return f"{config.context}{PUSH_CONTEXT_SUFFIX}"


@dataclass(frozen=True)
class Snapshot:
    """Newest check runs and commit statuses on the target SHA."""

    checks_by_name: dict[str, dict]
    statuses_by_context: dict[str, dict]
    ignored_foreign_apps: tuple[str, ...]
    anchor: float
    self_status: dict | None = None


@dataclass(frozen=True)
class Evaluation:
    """A verdict and the start time of its evidence read."""

    snapshot: Snapshot
    state: str
    description: str
    read_started: float


class EvaluationRetry(Enum):
    """Reasons an evaluation can start over once."""

    PREFLIGHT = auto()
    POST = auto()
    CONTESTED = auto()


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


def _bounded_reason(err: BaseException) -> str:
    """One short line describing a decode failure, never echoing the payload."""
    if isinstance(err, RecursionError):
        return "nesting too deep"
    lines = str(err).splitlines()
    text = lines[0] if lines else type(err).__name__
    return text[:MAX_JSON_DIAGNOSTIC]


def _load_json(label: str, raw: str) -> object:
    """Decode strict JSON for a named input or raise InputError."""
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (TypeError, ValueError, RecursionError) as err:
        raise InputError(f"{label}: invalid JSON ({_bounded_reason(err)})") from err


def _parse_int_range(label: str, raw: str | int, low: int, high: int) -> int:
    """Parse a whole integer within [low, high] or raise InputError."""
    if isinstance(raw, bool):
        raise InputError(f"{label}: must be a whole number from {low} to {high}, got {raw!r}")
    if isinstance(raw, int):
        if not -999_999 <= raw <= 999_999:
            raise InputError(f"{label}: integer exceeds six digits")
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
    word = raw.strip().lower()
    if word == "true":
        return True
    if word == "false":
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


def _path_from_workflow_ref(raw: str, repository: str = "") -> str:
    """Workflow path from GITHUB_WORKFLOW_REF, or empty when unset.

    The value is `<owner>/<repo>/<workflow path>@<ref>`. The prefix must be a valid
    owner/name and, when `repository` is given, name that repository. The ref after the
    first `@` must be non-empty and free of whitespace and control characters.
    """
    text = raw.strip()
    if not text:
        return ""
    path_part, at, ref = text.partition("@")
    parts = path_part.split("/", 2)
    if len(parts) < 3:
        raise InputError(f"github-workflow-ref: {raw!r} has no workflow path")
    prefix = f"{parts[0]}/{parts[1]}"
    if not REPO_RE.fullmatch(prefix) or parts[1] in (".", ".."):
        raise InputError(f"github-workflow-ref: {raw!r} has an invalid repository prefix")
    if repository and prefix.casefold() != repository.strip().casefold():
        raise InputError(
            f"github-workflow-ref: {raw!r} names {prefix!r}, not repository {repository!r}"
        )
    if not at or not WORKFLOW_REF_SUFFIX_RE.fullmatch(ref):
        raise InputError(f"github-workflow-ref: {raw!r} has a missing or malformed @ref suffix")
    return _parse_self_workflow(parts[2])


def _fork_head_repository(event_path: str) -> str:
    """Head repository full name from the forge event payload, or empty."""
    path = Path(event_path)
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return ""
    if not isinstance(payload, dict):
        return ""
    pull = payload.get("pull_request")
    if not isinstance(pull, dict):
        return ""
    head = pull.get("head")
    if not isinstance(head, dict):
        return ""
    repo = head.get("repo")
    if not isinstance(repo, dict):
        return ""
    name = repo.get("full_name")
    return name.strip() if isinstance(name, str) else ""


def _parse_required(raw: str) -> tuple[str, ...]:
    """Parse the non-empty list of unique required context names."""
    data = _load_json("required", raw)
    if not isinstance(data, list):
        raise InputError("required: must be a JSON list of context names")
    if not data:
        raise InputError("required: must not be empty")
    if len(data) > MAX_CONTEXTS:
        raise InputError(f"required: {len(data)} entries exceeds limit {MAX_CONTEXTS}")
    seen: dict[str, str] = {}
    names: list[str] = []
    for item in data[:MAX_CONTEXTS]:
        name = _validate_context_name("required", item)
        folded = name.casefold()
        if seen.get(folded) == name:
            raise InputError(f"required: duplicate context name {name!r}")
        if folded in seen:
            raise InputError(f"required: {seen[folded]!r} and {name!r} differ only by case")
        seen[folded] = name
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


def _judge_context_keys(context: str) -> tuple[str, str]:
    return context.casefold(), f"{context}{PUSH_CONTEXT_SUFFIX}".casefold()


def _reject_self_context(context: str, required: tuple[str, ...]) -> None:
    own = _judge_context_keys(context)
    for name in required[:MAX_CONTEXTS]:
        if name.casefold() in own:
            raise InputError(f"required: context {name!r} must not appear in required")


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


def _parse_run_number(raw: RawInputs) -> int:
    if raw.targets == TARGETS_SHA:
        return 0
    value = raw.run_number
    if isinstance(value, str):
        digits = value.strip().lstrip("0")
        if re.fullmatch(r"[0-9]{1,10}", digits):
            value = int(digits)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_RUN_NUMBER:
        return value
    raise InputError(f"run-number: open-pulls requires a whole number from 1 to {MAX_RUN_NUMBER}")


def parse_policy(
    required_json: str,
    pull_request_only_json: str,
    not_owed_json: str,
    apps_json: str,
    context: str = DEFAULT_CONTEXT,
) -> tuple[tuple[str, ...], frozenset[str], frozenset[str], dict[str, str]]:
    """Parse required names, proposal-only names, exemptions, and app overrides."""
    required = _parse_required(required_json)
    allowed = frozenset(required)
    pr_only = _parse_subset("pull-request-only", pull_request_only_json, allowed)
    not_owed = _parse_subset("not-owed", not_owed_json, allowed)
    apps = _parse_apps(apps_json, allowed)
    _reject_self_context(context, required)
    return required, pr_only, not_owed, apps


def _parse_config_fields(raw: RawInputs) -> Config:
    """Validate check policy and build the normalized configuration."""
    ctx = _parse_context(raw.context)
    required, pr_only, not_owed, apps = parse_policy(
        raw.required_json, raw.pull_request_only_json, raw.not_owed_json, raw.apps_json, ctx
    )
    bound = _parse_int_range(
        "bound-minutes", raw.bound_minutes, BOUND_MINUTES_MIN, BOUND_MINUTES_MAX
    )
    self_workflow = _parse_self_workflow(raw.self_workflow)
    return Config(
        repository=raw.repository,
        sha=raw.sha,
        event_name=raw.event_name,
        ref=raw.ref,
        required=required,
        pull_request_only=pr_only,
        not_owed=not_owed,
        apps=apps,
        bound_minutes=bound,
        context=ctx,
        self_workflow=self_workflow,
        targets=raw.targets,
        max_targets=_parse_int_range(
            "max-targets", raw.max_targets, MAX_TARGETS_MIN, MAX_TARGETS_MAX
        ),
        run_number=_parse_run_number(raw),
        only_if_pending=_parse_bool("only-if-pending", raw.only_if_pending),
        target_url=_parse_target_url(raw.target_url),
    )


def parse_config(raw: RawInputs) -> Config:
    """Validate every caller input before any forge API request is made."""
    repo = raw.repository.strip()
    if not REPO_RE.fullmatch(repo) or repo.split("/", 1)[1] in (".", ".."):
        raise InputError(f"repository: invalid owner/name {raw.repository!r}")
    event = raw.event_name.strip()
    mode = raw.targets.strip().lower()
    if mode not in ALLOWED_TARGETS:
        raise InputError(f"targets: {raw.targets!r} must be sha or open-pulls")
    if mode == TARGETS_SHA and not event:
        raise InputError("event: event name must not be empty")
    sha = (
        _resolve_sha(event, raw.sha, raw.commit_sha, raw.pr_head_sha) if mode == TARGETS_SHA else ""
    )
    return _parse_config_fields(
        replace(raw, repository=repo, event_name=event, ref=raw.ref.strip(), sha=sha, targets=mode)
    )


def _decode_json(label: str, body: bytes) -> object:
    try:
        return json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as err:
        raise ApiFailureError(f"GitHub API error ({label}): {_bounded_reason(err)}") from err


def _is_definitive_rejection(err: forge.ResponseError) -> bool:
    """Whether an HTTP response error guarantees GitHub stored no commit status."""
    status = err.status
    return isinstance(status, int) and not isinstance(status, bool) and 400 <= status < 500


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
        max_api_calls: int | None = None,
    ) -> None:
        self.deadline = deadline
        self.work_deadline = deadline - WITHDRAWAL_RESERVE_SECONDS
        self.now = now
        self.sleep = sleep
        self.transport = transport
        self.out = out
        self.max_api_calls = max_api_calls
        self.api_calls = 0
        self.cap_reached = False
        self.withdrawal_reserved = False
        self.rate_limit_waited_seconds = 0.0
        self.rate_limit_wait_count = 0

    def _within_rate_limit_bounds(self, delay: int | float, waits: int) -> bool:
        if waits >= MAX_RATE_LIMIT_WAITS_PER_CALL:
            return False
        if self.rate_limit_waited_seconds + delay > MAX_RATE_LIMIT_SECONDS:
            return False
        return self.now() + delay < self.work_deadline

    def _wait_rate_limit(
        self, label: str, err: forge.ResponseError, delay: int | float, waits: int
    ) -> None:
        message = (
            f"GitHub API rate limit: {label} still answers HTTP {err.status}. "
            "No check result was judged; rerun this job."
        )
        if self.now() + delay >= self.work_deadline:
            raise WorkDeadlineError(message) from err
        if not self._within_rate_limit_bounds(delay, waits):
            raise ApiFailureError(message) from err
        self._check_api_cap(label)
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
        if self.now() + seconds >= self.work_deadline:
            raise ServerBackoffDeadlineError(
                f"Transient GitHub API failure: {label} still answers HTTP {err.status} "
                f"at the {WORK_DEADLINE_SECONDS} s session work cutoff. "
                "No check result was judged; rerun this job."
            ) from err
        self._check_api_cap(label)
        self.out.write(
            f"::warning::{label}: GitHub API HTTP {err.status}, retrying in {seconds} s\n"
        )
        self.sleep(seconds)

    def _call_deadline(self, label: str, *, withdrawal: bool = False) -> float:
        """Per-call transport deadline capped by the remaining work or withdrawal budget."""
        limit = self.deadline if withdrawal else self.work_deadline
        remaining = limit - self.now()
        if remaining <= 0:
            bound = (
                f"Session deadline of {SESSION_DEADLINE_SECONDS} s"
                if withdrawal
                else f"Session deadline work cutoff of {WORK_DEADLINE_SECONDS} s"
            )
            error_type = ApiFailureError if withdrawal else WorkDeadlineError
            raise error_type(
                f"{bound} reached during {label}. No check result was judged; rerun this job."
            )
        return min(forge.DEADLINE_SECONDS, remaining)

    def _check_api_cap(self, label: str, *, reserve: int = 0) -> None:
        reserve = max(reserve, int(self.withdrawal_reserved))
        if self.max_api_calls is not None and self.api_calls + reserve >= self.max_api_calls:
            self.cap_reached = True
            raise ApiCapReachedError(
                f"Sweep API call cap of {self.max_api_calls} reached during {label}."
            )

    def _request_bytes(
        self,
        label: str,
        path: str,
        method: str,
        *,
        reserve: int = 0,
        withdrawal: bool = False,
        **kw: object,
    ) -> bytes:
        """Count every transport attempt, including failures and retries."""
        self._check_api_cap(label, reserve=reserve)
        deadline = self._call_deadline(label, withdrawal=withdrawal)
        self.api_calls += 1
        try:
            _, _, body = self.transport(path, method=method, deadline=deadline, **kw)
            return body
        except forge.ResponseError:
            raise
        except forge.ForgeError as err:
            raise TransportFailureError(f"GitHub API error ({label}): {err}") from err

    def wait_to_retry_post(self, failure: PostFailureError) -> None:
        """Wait once for an HTTP write failure that can be retried from fresh evidence."""
        err = failure.response
        delay = forge.rate_limit_delay(err.status, err.headers, err.message, 0, self.now())
        if delay is not None:
            self._wait_rate_limit("post-status", err, delay, 0)
        elif err.status is not None and 500 <= err.status < 600:
            self._wait_server_error("post-status", err, 0)
        else:
            raise failure

    def _post_json(
        self,
        label: str,
        path: str,
        method: str,
        *,
        withdrawal: bool = False,
        reserve_withdrawal: bool | None = None,
        **kw: object,
    ) -> object:
        """Send one non-GET request and update withdrawal reservation before decoding."""
        try:
            # Verification and a possible withdrawal each need one call.
            reserve = 0 if withdrawal else 2
            body = self._request_bytes(
                label, path, method, reserve=reserve, withdrawal=withdrawal, **kw
            )
        except forge.ResponseError as err:
            if reserve_withdrawal and not _is_definitive_rejection(err):
                self.withdrawal_reserved = True
            raise PostFailureError(label, err) from err
        except TransportFailureError:
            if reserve_withdrawal:
                self.withdrawal_reserved = True
            raise
        if reserve_withdrawal:
            self.withdrawal_reserved = True
        return _decode_json(label, body)

    def request_json(
        self,
        label: str,
        path: str,
        *,
        method: str = "GET",
        withdrawal: bool = False,
        reserve_withdrawal: bool | None = None,
        **kw: object,
    ) -> object:
        """Decode one JSON endpoint. Only GET requests retry within the session."""
        if method != "GET":
            return self._post_json(
                label,
                path,
                method,
                withdrawal=withdrawal,
                reserve_withdrawal=reserve_withdrawal,
                **kw,
            )
        server_attempts = 0
        rate_limit_waits = 0
        for _ in range(MAX_CALL_ATTEMPTS):
            try:
                return _decode_json(label, self._request_bytes(label, path, method, **kw))
            except forge.ResponseError as err:
                delay = forge.rate_limit_delay(
                    err.status, err.headers, err.message, rate_limit_waits, self.now()
                )
                if delay is not None:
                    self._wait_rate_limit(label, err, delay, rate_limit_waits)
                    rate_limit_waits += 1
                    continue
                if err.status is not None and 500 <= err.status < 600:
                    self._wait_server_error(label, err, server_attempts)
                    server_attempts += 1
                    continue
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


def _workflow_anchor(
    workflow_runs: list[dict], self_workflow: str, excluded_suites: set[int]
) -> float | None:
    """Earliest created_at among non-self workflow runs in eligible suites."""
    earliest: float | None = None
    for run in workflow_runs[:MAX_WORKFLOW_RUNS]:
        if (
            run.get("path") == self_workflow
            or _int_id(run.get("check_suite_id")) in excluded_suites
        ):
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
    admitted_suites: set[int],
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
    suite_id = _int_id(suite.get("id")) if isinstance(suite, dict) else 0
    if expected_app == DEFAULT_APP and suite_id not in admitted_suites:
        return None, None
    return name, None


def _check_run_epoch(run: dict) -> float:
    """Epoch of the run's `started_at`, or 0.0 when absent or unparsable;
    `created_at` and `completed_at` never stand in.
    """
    return _iso_epoch(run.get("started_at"))


def _check_run_key(run: dict) -> tuple[int, float, int]:
    epoch = _check_run_epoch(run)
    run_id = _int_id(run.get("id"))
    return (0, epoch, run_id) if epoch > 0 else (1, 0.0, run_id)


def _newer_check_run(candidate: dict, current: dict) -> bool:
    return _check_run_key(candidate) >= _check_run_key(current)


def _check_run_completed(run: dict) -> bool:
    return run.get("status") == "completed" and _check_run_epoch(run) > 0


def newest_check_runs(
    check_runs: list[dict],
    admitted_suites: set[int],
    apps: Mapping[str, str],
    required_names: frozenset[str],
    self_context: str,
) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Select the newest check run per name from its expected app and, for Actions, own suite."""
    by_name: dict[str, dict] = {}
    ignored: list[str] = []
    seen_ignored: set[str] = set()
    for run in check_runs[:MAX_CHECK_RUNS]:
        admitted_name, foreign_note = _check_run_admitted(run, admitted_suites, apps, self_context)
        raw_name = run.get("name")
        if foreign_note and raw_name in required_names and raw_name not in seen_ignored:
            seen_ignored.add(str(raw_name))
            ignored.append(foreign_note)
        if admitted_name is None:
            continue
        current = by_name.get(admitted_name)
        if current is None or _newer_check_run(run, current):
            by_name[admitted_name] = run
    return by_name, tuple(ignored)


def _status_timestamp(entry: Mapping[str, object]) -> float:
    return max(_iso_epoch(entry.get("updated_at")), _iso_epoch(entry.get("created_at")))


def _all_statuses(statuses: list[dict]) -> dict[str, dict]:
    """Newest commit status per folded context, including both judge contexts."""
    by_context: dict[str, dict] = {}
    ranks: dict[str, tuple[float, int]] = {}
    for entry in statuses[:MAX_STATUSES]:
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            continue
        context = context.casefold()
        rank = (_status_timestamp(entry), _int_id(entry.get("id")))
        if context not in ranks or rank >= ranks[context]:
            ranks[context] = rank
            by_context[context] = entry
    return by_context


def _exclude_self_statuses(statuses: dict[str, dict], context: str) -> None:
    for own in _judge_context_keys(context):
        statuses.pop(own, None)


def newest_statuses(statuses: list[dict], self_context: str) -> dict[str, dict]:
    """Select the newest commit status per context except the judge's contexts."""
    recent = _all_statuses(statuses)
    _exclude_self_statuses(recent, self_context)
    return recent


def read_statuses(session: ApiSession, repository: str, sha: str) -> dict[str, dict]:
    """Commit statuses on `sha`, including the self context."""
    path = f"repos/{repository}/commits/{sha}/statuses"
    try:
        statuses = forge.paged(
            path,
            None,
            max_pages=STATUS_MAX_PAGES,
            fetch=lambda p: session.request_json("statuses", p),
        )
    except forge.ForgeError as err:
        raise ApiFailureError(f"GitHub API error (statuses): {err}") from err
    return _all_statuses(statuses)


def _read_workflow_runs(session: ApiSession, config: Config) -> list[dict]:
    """Fail closed when the Actions workflow listing saturates the allowed pages."""
    runs_path = f"repos/{config.repository}/actions/runs?head_sha={config.sha}"
    rows_seen = 0
    row_cap = WORKFLOW_RUN_MAX_PAGES * forge.PER_PAGE

    def fetch(path: str) -> object:
        nonlocal rows_seen
        data = session.request_json("workflow-runs", path)
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        if isinstance(rows, list):
            rows_seen += len(rows)
            if rows_seen >= row_cap:
                raise ApiFailureError(
                    f"GitHub API workflow-runs cap reached: {WORKFLOW_RUN_MAX_PAGES} full "
                    f"pages ({row_cap} entries) on {config.sha}."
                )
        return data

    return forge.paged(runs_path, "workflow_runs", max_pages=WORKFLOW_RUN_MAX_PAGES, fetch=fetch)


def read_snapshot(session: ApiSession, config: Config) -> Snapshot | None:
    """Read current evidence. Skip a settled success when the caller requests it."""
    statuses = read_statuses(session, config.repository, config.sha)
    self_status = statuses.get(status_context(config).casefold())
    _exclude_self_statuses(statuses, config.context)
    if (
        config.only_if_pending
        and not session.withdrawal_reserved
        and self_status
        and self_status.get("state") == "success"
    ):
        return None
    checks_path = f"repos/{config.repository}/commits/{config.sha}/check-runs?filter=all"
    try:
        check_runs = forge.paged(
            checks_path,
            "check_runs",
            max_pages=CHECK_RUN_MAX_PAGES,
            fetch=lambda p: session.request_json("check-runs", p),
        )
        workflow_runs = _read_workflow_runs(session, config)
    except forge.ForgeError as err:
        raise ApiFailureError(f"GitHub API error: {err}") from err
    excluded = foreign_suites(workflow_runs, config.event_name, config.ref)
    if config.targets == TARGETS_OPEN_PULLS:
        excluded |= self_workflow_suites(workflow_runs, config.self_workflow)
    listed = {_int_id(run.get("check_suite_id")) for run in workflow_runs[:MAX_WORKFLOW_RUNS]}
    admitted = listed - excluded - {0}
    by_name, ignored = newest_check_runs(
        check_runs,
        admitted,
        config.apps,
        frozenset(config.required),
        config.context,
    )
    anchor = _workflow_anchor(workflow_runs, config.self_workflow, excluded)
    if anchor is None:
        anchor = _commit_anchor(session, config.repository, config.sha)
    return Snapshot(
        checks_by_name=by_name,
        statuses_by_context=statuses,
        ignored_foreign_apps=ignored,
        anchor=anchor,
        self_status=self_status,
    )


def exempt_reason(name: str, config: Config) -> str:
    """Exemption reason for `name` on this event, or empty string when `name` is owed."""
    if name in config.not_owed:
        return "not owed"
    if name in config.pull_request_only and config.event_name not in PROPOSAL_EVENTS:
        return f"pull-request-only on {config.event_name}"
    return ""


def _records_for(name: str, snapshot: Snapshot, config: Config) -> tuple[dict | None, dict | None]:
    """Return (check_run, commit_status) applicable to `name`."""
    run = snapshot.checks_by_name.get(name)
    status = None if name in config.apps else snapshot.statuses_by_context.get(name.casefold())
    return run, status


def _is_context_pending(run: dict | None, status: dict | None) -> bool:
    run_pending = run is not None and not _check_run_completed(run)
    status_pending = status is not None and status.get("state") == "pending"
    return run_pending or status_pending


def _terminal_failure(run: dict | None, status: dict | None) -> bool:
    check_failed = (
        run is not None and _check_run_completed(run) and run.get("conclusion") != "success"
    )
    status_failed = status is not None and status.get("state") not in ("pending", "success")
    return check_failed or status_failed


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
        run, status = _records_for(name, snapshot, config)
        if exempt_reason(name, config) or _terminal_failure(run, status):
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
    if run is not None and _check_run_completed(run):
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
    if not _check_run_completed(run):
        status = run.get("status")
        state = "pending" if status == "completed" else (status or "pending")
        return f"still {state}"
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
    fields = {"state": state, "context": status_context(config), "description": description}
    if config.target_url:
        fields["target_url"] = config.target_url
    return fields


def _should_post(newest: Mapping[str, object] | None, state: str, description: str) -> bool:
    if state == "pending":
        return newest is not None and newest.get("state") in ("success", "failure")
    if not newest:
        return True
    return newest.get("state") != state or newest.get("description") != description


def post_commit_status(
    session: ApiSession, config: Config, state: str, description: str, *, withdrawal: bool = False
) -> int:
    """Attempt a single status POST and return the created status ID."""
    path = f"repos/{config.repository}/statuses/{config.sha}"
    fields = _status_fields(config, state, description)
    answer = session.request_json(
        "post-status",
        path,
        method="POST",
        withdrawal=withdrawal,
        reserve_withdrawal=state == "success",
        fields=fields,
    )
    status_id = answer.get("id") if isinstance(answer, dict) else None
    if not isinstance(status_id, int) or isinstance(status_id, bool):
        raise ApiFailureError(f"GitHub API error (post-status): POST {path}: no integer id")
    session.out.write(f"Posted {state} for {status_context(config)} on {config.sha}\n")
    return status_id


def _warn_api_cap(err: TextIO, exc: ApiCapReachedError) -> int:
    err.write(f"::warning::{exc}\n")
    return 0


def _read_and_decide(session: ApiSession, config: Config, out: TextIO) -> Evaluation | None:
    """Read evidence and record when the read used for the verdict began."""
    read_started = session.now()
    snapshot = read_snapshot(session, config)
    if snapshot is None:
        out.write(f"{config.sha}: {status_context(config)} already settled\n")
        return None
    judge_snapshot(snapshot, config, out)
    state, description = decide(snapshot, config, session.now(), snapshot.anchor)
    return Evaluation(snapshot, state, description, read_started)


def _post_age_problem(session: ApiSession, evaluation: Evaluation) -> str:
    age = session.now() - evaluation.read_started
    if age > POST_FRESH_SECONDS:
        return f"snapshot read started {age:.0f} s ago (limit {POST_FRESH_SECONDS} s)"
    return ""


def _owed_status_changed(
    config: Config, snapshot: Snapshot, recent: Mapping[str, dict], *, complete: bool
) -> bool:
    for name in config.required[:MAX_CONTEXTS]:
        key = name.casefold()
        if name in config.apps or (key not in recent and not complete):
            continue
        if recent.get(key) != snapshot.statuses_by_context.get(key):
            return True
    return False


def _read_status_page(session: ApiSession, config: Config, page: int) -> tuple[list[dict], bool]:
    """Return a commit-status page and whether it exhausts the listing."""
    path = (
        f"repos/{config.repository}/commits/{config.sha}/statuses"
        f"?per_page={forge.PER_PAGE}&page={page}"
    )
    data = session.request_json("statuses", path)
    if not isinstance(data, list):
        raise ApiFailureError(f"GitHub API error (statuses): GET {path}: no list in the answer")
    complete = len(data) < forge.PER_PAGE
    return [entry for entry in data[: forge.PER_PAGE] if isinstance(entry, dict)], complete


def _read_status_page_one(session: ApiSession, config: Config) -> tuple[list[dict], bool]:
    return _read_status_page(session, config, 1)


def _status_in_context(entry: Mapping[str, object], key: str) -> bool:
    context = entry.get("context")
    return isinstance(context, str) and context.casefold() == key


def _read_status_pages(
    session: ApiSession,
    config: Config,
    start_page: int,
    statuses: list[dict],
    covered: Callable[[list[dict]], bool],
) -> tuple[list[dict], bool, bool]:
    """Read status pages from `start_page` until `complete`, `covered`, or `STATUS_MAX_PAGES`."""
    if covered(statuses):
        return statuses, False, True
    for page in range(start_page, STATUS_MAX_PAGES + 1):
        rows, complete = _read_status_page(session, config, page)
        statuses.extend(rows)
        if complete or covered(statuses):
            return statuses, complete, True
    return statuses, False, False


def _preflight_statuses(
    session: ApiSession, config: Config, key: str
) -> tuple[dict[str, dict], bool]:
    statuses, complete, _ = _read_status_pages(
        session,
        config,
        1,
        [],
        lambda rows: any(_status_in_context(row, key) for row in rows[:MAX_STATUSES]),
    )
    return _all_statuses(statuses), complete


def _status_preflight(session: ApiSession, config: Config, evaluation: Evaluation) -> str:
    """Read through the first judge status or end of the listing before posting."""
    key = status_context(config).casefold()
    recent, complete = _preflight_statuses(session, config, key)
    newest = recent.get(key)
    if newest is None and not complete:
        return "the newest judge status is outside the pre-post status page"
    # The snapshot read the statuses before the checks and this read follows them.
    # Statuses are immutable and ids grow, so any judge status created in between ranks
    # newest and differs from the snapshot's. Identity equality proves nothing moved.
    # Do not compare the forge timestamp with the runner clock: they are different
    # time domains and the stamp of an existing status is not the server's current time.
    if newest != evaluation.snapshot.self_status:
        return "the judge status moved during the evidence read"
    if _owed_status_changed(config, evaluation.snapshot, recent, complete=complete):
        return "required commit status evidence changed during the evidence read"
    return _post_age_problem(session, evaluation)


_WITHDRAWABLE_STATES = frozenset({"failure", "pending"})


def _is_status_id(value: object, expected: int) -> bool:
    """Exact positive integer id match; bool, float, str, None and non-positive ids never match.

    Plain `==` or set membership would let True match 1 and 1.0 match 1.
    """
    return expected > 0 and _int_id(value) == expected


def _own_contender_cutoff(
    rows: list[dict], key: str, status_id: int, checked_at: float, read_started: float
) -> int | None:
    """Map the judge's server `created_at` stamp back by runner elapsed time to the read start."""
    for entry in rows[: forge.PER_PAGE]:
        if _status_in_context(entry, key) and _is_status_id(entry.get("id"), status_id):
            own_created = _iso_epoch(entry.get("created_at"))
            if own_created <= 0:
                return None
            return floor(own_created - max(0.0, checked_at - read_started))
    return None


def _owed_status_keys(config: Config) -> frozenset[str]:
    return frozenset(
        name.casefold() for name in config.required[:MAX_CONTEXTS] if name not in config.apps
    )


def _statuses_covered(rows: list[dict], owed_keys: frozenset[str], oldest_contender: int) -> bool:
    """Confirm every owed status context appears and the last row read precedes the cutoff."""
    if not rows or _status_timestamp(rows[-1]) >= oldest_contender:
        return False
    if not owed_keys:
        return True
    seen: set[str] = set()
    for entry in rows[:MAX_STATUSES]:
        context = entry.get("context")
        if isinstance(context, str) and context:
            seen.add(context.casefold())
    return owed_keys <= seen


def _excluded_status_ids(
    status_id: int, self_status: Mapping[str, object] | None
) -> frozenset[int]:
    """Exclude the created status and the judge status the snapshot read, because preflight
    confirmed it was the newest before the POST.
    """
    prior_id = _int_id(self_status.get("id")) if self_status is not None else 0
    # Only positive exact ints are excluded, so a malformed row id (0 via _int_id) never matches.
    return frozenset(i for i in (_int_id(status_id), prior_id) if i > 0)


def _has_contender(
    rows: list[dict], key: str, excluded_ids: frozenset[int], oldest_contender: int
) -> bool:
    for entry in rows[:MAX_STATUSES]:
        if (
            _status_in_context(entry, key)
            and _int_id(entry.get("id")) not in excluded_ids
            and _iso_epoch(entry.get("created_at")) >= oldest_contender
        ):
            return True
    return False


def _own_post_is_newest_non_success(
    rows: list[dict], key: str, status_id: int, state: str, excluded_ids: frozenset[int]
) -> bool:
    """Whether the posted non-success status is verifiably the newest visible judge status.

    The row must carry the posted integer id and rank newest in the judge context. No other
    success row may tie or follow it, except the excluded ids.
    """
    if state not in _WITHDRAWABLE_STATES:
        return False
    own = _all_statuses(rows).get(key)
    if own is None or not _is_status_id(own.get("id"), status_id) or own.get("state") != state:
        return False
    stamp = _status_timestamp(own)
    for entry in rows[:MAX_STATUSES]:
        if (
            _status_in_context(entry, key)
            and _int_id(entry.get("id")) not in excluded_ids
            and entry.get("state") == "success"
            and _status_timestamp(entry) >= stamp
        ):
            return False
    return True


def _verify_post(
    session: ApiSession, config: Config, evaluation: Evaluation, status_id: int
) -> tuple[bool, bool]:
    """Return (contested, newest_own_non_success) from server-clock timestamps and covered pages.

    The second flag is True only when the contest is proven harmless to withdrawal: the
    created failure or pending status is the newest visible judge status.
    """
    checked_at = session.now()
    rows, complete = _read_status_page_one(session, config)
    key = status_context(config).casefold()
    oldest_contender = _own_contender_cutoff(
        rows, key, status_id, checked_at, evaluation.read_started
    )
    if oldest_contender is None:
        return True, False
    owed_keys = _owed_status_keys(config)
    if not complete:
        rows, complete, confirmed = _read_status_pages(
            session,
            config,
            2,
            rows,
            lambda read_rows: _statuses_covered(read_rows, owed_keys, oldest_contender),
        )
        if not confirmed:
            return True, False
    if _owed_status_changed(config, evaluation.snapshot, _all_statuses(rows), complete=complete):
        return True, False
    excluded_ids = _excluded_status_ids(status_id, evaluation.snapshot.self_status)
    if not _has_contender(rows, key, excluded_ids, oldest_contender):
        return False, False
    return True, _own_post_is_newest_non_success(
        rows, key, status_id, evaluation.state, excluded_ids
    )


def _post_is_contested(
    session: ApiSession, config: Config, evaluation: Evaluation, status_id: int
) -> bool:
    """Detect judge writes via server-clock timestamps or status changes across covered pages."""
    return _verify_post(session, config, evaluation, status_id)[0]


def _report_verdict(config: Config, evaluation: Evaluation, out: TextIO) -> None:
    if evaluation.state == "failure":
        out.write(f"{config.sha}: {evaluation.description}\n")
    elif evaluation.state == "success":
        out.write("All owed required checks reported success.\n")


def _attempt_post(
    session: ApiSession,
    config: Config,
    evaluation: Evaluation,
    *,
    can_retry: bool,
    unconfirmed: list[PostFailureError] | None = None,
) -> int | None:
    """Return the created status ID, or None after waiting to retry from fresh evidence."""
    try:
        return post_commit_status(session, config, evaluation.state, evaluation.description)
    except PostFailureError as exc:
        if not can_retry:
            raise
        # A non-definitive failure may or may not have stored a status. Keep it visible
        # until a later evaluation proves the outcome.
        if unconfirmed is not None and not _is_definitive_rejection(exc.response):
            unconfirmed.append(exc)
        session.wait_to_retry_post(exc)
        return None


def _report_no_post(config: Config, evaluation: Evaluation, out: TextIO) -> None:
    newest = evaluation.snapshot.self_status
    kept = (newest or {}).get("state") or "no status"
    out.write(
        f"No status post: {status_context(config)} on {config.sha} keeps {kept}; "
        f"verdict {evaluation.state}\n"
    )


def _evaluate_attempt(
    session: ApiSession,
    config: Config,
    out: TextIO,
    *,
    can_retry_post: bool,
    unconfirmed: list[PostFailureError] | None = None,
) -> tuple[EvaluationRetry, str] | None:
    """Judge once and return a retry reason if another evaluation is needed."""
    evaluation = _read_and_decide(session, config, out)
    if evaluation is None:
        return None
    should_post = _should_post(
        evaluation.snapshot.self_status, evaluation.state, evaluation.description
    )
    if should_post or session.withdrawal_reserved:
        stale_reason = _post_age_problem(session, evaluation) or _status_preflight(
            session, config, evaluation
        )
        if stale_reason:
            return EvaluationRetry.PREFLIGHT, f"Posting skipped for {config.sha}: {stale_reason}."
    if not should_post:
        session.withdrawal_reserved = False
        _report_no_post(config, evaluation, out)
        _report_verdict(config, evaluation, out)
        return None
    status_id = _attempt_post(
        session, config, evaluation, can_retry=can_retry_post, unconfirmed=unconfirmed
    )
    if status_id is None:
        return EvaluationRetry.POST, ""
    contested, newest_own = _verify_post(session, config, evaluation, status_id)
    if contested:
        if newest_own:
            # A verified visible newest failure or pending status already replaced any
            # ambiguous success, so no withdrawal call is owed.
            session.withdrawal_reserved = False
        return EvaluationRetry.CONTESTED, f"Status write contested again for {config.sha}."
    session.withdrawal_reserved = False
    _report_verdict(config, evaluation, out)
    return None


def _withdraw_unverified_success(session: ApiSession, config: Config) -> None:
    """Spend the reserved call to replace an unresolved success with pending."""
    if not session.withdrawal_reserved:
        return
    session.withdrawal_reserved = False
    description = _truncate_description("contested write; the next judgment decides")
    try:
        post_commit_status(session, config, "pending", description, withdrawal=True)
    except (ApiCapReachedError, ApiFailureError) as exc:
        raise ApiFailureError(
            f"Could not withdraw the status for {config.sha}; a success may rest on "
            f"superseded evidence: {exc}"
        ) from exc


def _evaluate_retries(
    session: ApiSession,
    config: Config,
    out: TextIO,
    unconfirmed: list[PostFailureError] | None = None,
) -> str:
    """Allow one re-evaluation per retry reason.

    An unconfirmed POST failure is cleared only when an evaluation finishes without a
    retry reason, which proves the outcome.
    """
    retried: set[EvaluationRetry] = set()
    for _ in range(1 + len(EvaluationRetry)):
        retry = _evaluate_attempt(
            session,
            config,
            out,
            can_retry_post=EvaluationRetry.POST not in retried,
            unconfirmed=unconfirmed,
        )
        if retry is None:
            if unconfirmed is not None:
                unconfirmed.clear()
            return ""
        reason, warning = retry
        if reason in retried:
            return warning
        retried.add(reason)
        if reason in (EvaluationRetry.CONTESTED, EvaluationRetry.POST):
            # After an uncertain or contested write, a settled success seen by the
            # only_if_pending shortcut may be stale. Re-read current Build evidence
            # before deciding the retry is done. The first pass keeps the fast path,
            # and the existing retry, call and time caps still bound the loop.
            config = replace(config, only_if_pending=False)
    return ""


def _evaluate_one(
    session: ApiSession,
    config: Config,
    out: TextIO,
    err: TextIO,
    unconfirmed: list[PostFailureError] | None = None,
) -> None:
    try:
        warning = _evaluate_retries(session, config, out, unconfirmed)
    except (ApiCapReachedError, ApiFailureError):
        _withdraw_unverified_success(session, config)
        raise
    _withdraw_unverified_success(session, config)
    if warning:
        err.write(f"::warning::{warning}\n")


def evaluate_one(session: ApiSession, config: Config, out: TextIO, err: TextIO) -> int:
    """Read, decide and post for one SHA; return 1 only on an evaluation error."""
    unconfirmed: list[PostFailureError] = []
    try:
        _evaluate_one(session, config, out, err, unconfirmed)
    except ApiCapReachedError as exc:
        cap_rc = _warn_api_cap(err, exc)
        if unconfirmed:
            err.write(f"::error::{unconfirmed[0]}\n")
            return 1
        return cap_rc
    except ApiFailureError as exc:
        err.write(f"::error::{exc}\n")
        return 1
    if unconfirmed:
        err.write(f"::error::{unconfirmed[0]}\n")
        return 1
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


def _read_triage_statuses(
    session: ApiSession, config: Config, heads: list[str], errors: list[str]
) -> dict[str, dict]:
    """Read one status page per head until the triage attempt budget runs out."""
    key = status_context(config).casefold()
    judged: dict[str, dict] = {}
    for sha in heads[:OPEN_PULLS_MAX]:
        try:
            statuses, _ = _read_status_page_one(session, replace(config, sha=sha))
        except ApiCapReachedError:
            break
        except ServerBackoffDeadlineError as exc:
            if session.now() >= session.work_deadline:
                raise
            errors.append(f"Triage status read for {sha} failed; optional triage stopped: {exc}")
            break
        except WorkDeadlineError:
            raise
        except ApiFailureError as exc:
            if session.now() >= session.work_deadline:
                raise
            errors.append(f"Triage status read for {sha} failed; it ranks as unknown: {exc}")
            continue
        newest = _all_statuses(statuses).get(key)
        if newest is not None:
            judged[sha] = newest
    return judged


def _sweep_triage(
    session: ApiSession, config: Config, heads: list[str], errors: list[str]
) -> dict[str, dict]:
    """Cap triage independently so the same session can still judge targets."""
    sweep_cap = session.max_api_calls
    triage_cap = session.api_calls + TRIAGE_MAX_CALLS
    session.max_api_calls = triage_cap if sweep_cap is None else min(sweep_cap, triage_cap)
    try:
        return _read_triage_statuses(session, config, heads, errors)
    finally:
        session.max_api_calls = sweep_cap
        session.cap_reached = False


def _triage_priority(status: dict | None) -> tuple[int, float]:
    if status is None:
        return 0, 0.0
    states = {"failure": 2, "success": 3}
    return states.get(str(status.get("state")), 1), _status_timestamp(status)


def _sweep_evaluation_order(
    session: ApiSession, config: Config, heads: list[str], errors: list[str]
) -> list[str]:
    """Build the priority list, rotating this run's balanced block and filling to max_targets."""
    pr_config = replace(config, event_name="pull_request")
    judged = _sweep_triage(session, pr_config, heads, errors)
    candidates = sorted(heads[:OPEN_PULLS_MAX], key=lambda sha: _triage_priority(judged.get(sha)))
    if not candidates:
        return []
    n = len(candidates)
    budget = config.max_targets
    blocks = ceil(n / budget)
    index = config.run_number - 1
    block = index % blocks
    cycle = index // blocks
    lo = block * n // blocks
    hi = (block + 1) * n // blocks
    members = candidates[lo:hi]
    start = cycle % len(members)
    fill = (candidates[hi:] + candidates[:lo])[: budget - len(members)]
    return members[start:] + members[:start] + fill


def _default_branch_head(session: ApiSession, repository: str) -> tuple[str, str]:
    """Head SHA and name of the repository default branch."""
    repo = session.request_json("repository", f"repos/{repository}")
    if not isinstance(repo, dict):
        raise ApiFailureError("repository response was not an object")
    branch = repo.get("default_branch")
    if not isinstance(branch, str) or not branch.strip():
        raise ApiFailureError("repository has no default_branch")
    segment = quote(branch, safe="")
    commit = session.request_json("default-commit", f"repos/{repository}/commits/{segment}")
    if not isinstance(commit, dict):
        raise ApiFailureError("default branch commit response was not an object")
    sha = commit.get("sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ApiFailureError("default branch commit has no sha")
    return sha, branch


def _sweep_deadline_passed(session: ApiSession, err: TextIO, what: str) -> bool:
    """Refuse the next sweep target once the session work cutoff has passed."""
    if session.now() < session.work_deadline:
        return False
    err.write(
        f"::error::Session deadline work cutoff of {WORK_DEADLINE_SECONDS} s reached before "
        f"judging {what}. No check result was judged; rerun this job.\n"
    )
    return True


def _warn_unjudged_targets(err: TextIO, count: int) -> None:
    err.write(
        f"::warning::Sweep API call cap of {SWEEP_MAX_CALLS} reached; "
        f"{count} target(s) not judged.\n"
    )


def _sweep_default_branch(
    session: ApiSession,
    sweep_cfg: Config,
    default_head: tuple[str, str],
    out: TextIO,
    err: TextIO,
) -> int:
    """Judge the default branch head with its push policy in the sweep's session."""
    sha, branch = default_head
    default_cfg = replace(
        sweep_cfg, sha=sha, event_name="push", ref=f"refs/heads/{branch}", only_if_pending=False
    )
    return evaluate_one(session, default_cfg, out, err)


def _sweep_pull_heads(
    session: ApiSession,
    sweep_cfg: Config,
    to_eval: list[str],
    result: int,
    out: TextIO,
    err: TextIO,
) -> int:
    """Judge selected pull heads until the cap or work cutoff stops the sweep."""
    unjudged = len(to_eval[:MAX_TARGETS_MAX])
    for sha in to_eval[:MAX_TARGETS_MAX]:
        if session.api_calls >= SWEEP_MAX_CALLS:
            _warn_unjudged_targets(err, unjudged)
            return result
        if _sweep_deadline_passed(session, err, sha):
            return 1
        session.cap_reached = False
        one = replace(sweep_cfg, sha=sha, event_name="pull_request")
        if evaluate_one(session, one, out, err) != 0:
            result = 1
        if session.cap_reached:
            _warn_unjudged_targets(err, unjudged)
            return result
        unjudged -= 1
    return result


def _write_triage_errors(err: TextIO, errors: list[str]) -> bool:
    for line in errors[:OPEN_PULLS_MAX]:
        err.write(f"::error::{line}\n")
    return bool(errors)


def run_sweep(
    config: Config,
    *,
    transport: Callable[..., tuple[int, dict[str, str], bytes]] = forge.request,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Judge the default head and selected pull heads, retaining evaluation errors."""
    start = now()
    session = ApiSession(
        deadline=start + SESSION_DEADLINE_SECONDS,
        now=now,
        sleep=sleep,
        transport=transport,
        out=out,
        max_api_calls=SWEEP_MAX_CALLS,
    )
    sweep_cfg = replace(config, only_if_pending=False, targets=TARGETS_OPEN_PULLS)
    triage_errors: list[str] = []
    try:
        heads = _open_pull_head_shas(session, config.repository)
        to_eval = _sweep_evaluation_order(session, config, heads, triage_errors)
        default_head = _default_branch_head(session, config.repository)
    except ApiCapReachedError as exc:
        had_triage_errors = _write_triage_errors(err, triage_errors)
        result = _warn_api_cap(err, exc)
        return 1 if had_triage_errors else result
    except ApiFailureError as exc:
        _write_triage_errors(err, triage_errors)
        err.write(f"::error::{exc}\n")
        return 1
    had_triage_errors = _write_triage_errors(err, triage_errors)
    if _sweep_deadline_passed(session, err, "the default branch"):
        return 1
    result = _sweep_default_branch(session, sweep_cfg, default_head, out, err)
    if session.cap_reached:
        # The default branch consumed the quota, including the calls a POST reserves.
        if to_eval:
            _warn_unjudged_targets(err, len(to_eval[:MAX_TARGETS_MAX]))
        return 1 if had_triage_errors else result
    result = _sweep_pull_heads(session, sweep_cfg, to_eval, result, out, err)
    return 1 if had_triage_errors else result


def run_judge(
    config: Config,
    *,
    transport: Callable[..., tuple[int, dict[str, str], bytes]] = forge.request,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    event_path: str = "",
) -> int:
    """Execute one-shot judgment for a validated Config."""
    if config.targets == TARGETS_OPEN_PULLS:
        return run_sweep(config, transport=transport, now=now, sleep=sleep, out=out, err=err)
    if config.event_name in PR_EVENTS:
        head_repo = _fork_head_repository(event_path)
        if head_repo and head_repo != config.repository:
            err.write(
                "::notice::Fork pull request head; the required-checks tail does not post "
                "here. The scheduled sweep judges this commit.\n"
            )
            return 0
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
    config: Config,
    *,
    snapshot: Snapshot,
    now: float,
) -> tuple[str, str]:
    return decide(snapshot, config, now, snapshot.anchor)


def _test_workflow_run(config: Config) -> dict:
    return {
        "check_suite_id": 1,
        "event": config.event_name,
        "head_branch": config.ref.removeprefix("refs/heads/"),
    }


def _run_case(
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
    rc = run_judge(
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


def _self_test_success_case(base_kw: dict[str, str]) -> int:
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
    base_kw: dict[str, str],
) -> tuple[tuple[str, Callable[[], int], int], ...]:
    """Positive and negative self-test cases for check runs, apps and statuses."""
    kw = {**base_kw, "bound_minutes": 240}
    build = parse_config(RawInputs(**kw, required_json='["Build"]'))
    short_build = replace(build, bound_minutes=1)
    return (
        ("owed check and status pass", lambda: _self_test_success_case(kw), 0),
        (
            "absent owed check fails",
            lambda: _run_case(short_build, expected_state="failure", check_runs=[]),
            0,
        ),
        (
            "skipped owed check fails",
            lambda: _run_case(
                build,
                expected_state="failure",
                check_runs=[_check_run(1, "Build", "skipped")],
            ),
            0,
        ),
        (
            "wrong app check run fails",
            lambda: _run_case(
                short_build,
                expected_state="failure",
                check_runs=[_check_run(1, "Build", "success", app="impostor-app")],
            ),
            0,
        ),
        (
            "failed commit status fails",
            lambda: _run_case(
                parse_config(RawInputs(**kw, required_json='["External"]')),
                expected_state="failure",
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
                parse_config(RawInputs(**kw, required_json='["Build"]')),
                check_runs=[_check_run(1, "Build", "success")],
                errors=[rl_err],
            ),
            0,
        ),
        (
            "rate-limit wait over budget fails without judging",
            lambda: _run_case(
                parse_config(RawInputs(**kw, required_json='["Build"]')),
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
            cfg = parse_config(RawInputs(**merged))
            transport = _self_test_transport(checks, [_test_workflow_run(cfg)])
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
            if snap is None:
                return False
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

    cfg = parse_config(RawInputs(**base_kw, required_json='["X","Y"]', bound_minutes=60))

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
        RawInputs(
            **base_kw,
            required_json='["Scan"]',
            apps_json='{"Scan":"code-scanning"}',
            bound_minutes=60,
        )
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
    judge.add_argument("--run-number", default=None)
    judge.add_argument("--only-if-pending", default=None)
    judge.add_argument("--target-url", default=None)
    return parser


def _env_or(arg: str | None, env: Mapping[str, str], key: str, default: str) -> str:
    if arg is not None:
        return arg
    return env.get(key, default)


def _apply_workflow_ref(config: Config, environ: Mapping[str, str]) -> Config:
    """Take the judge's own workflow path from GITHUB_WORKFLOW_REF when the runner sets it."""
    workflow_ref = environ.get("GITHUB_WORKFLOW_REF", "").strip()
    if not workflow_ref:
        return config
    parsed = _path_from_workflow_ref(workflow_ref, config.repository)
    if not parsed:
        return config
    return Config(**{**config.__dict__, "self_workflow": parsed})


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
            RawInputs(
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
                    args.bound_minutes,
                    environ,
                    f"{prefix}BOUND_MINUTES",
                    str(BOUND_MINUTES_DEFAULT),
                ),
                context=_env_or(args.context, environ, f"{prefix}CONTEXT", DEFAULT_CONTEXT),
                self_workflow=_env_or(
                    args.self_workflow, environ, f"{prefix}SELF_WORKFLOW", DEFAULT_SELF_WORKFLOW
                ),
                targets=_env_or(args.targets, environ, f"{prefix}TARGETS", TARGETS_SHA),
                max_targets=_env_or(
                    args.max_targets, environ, f"{prefix}MAX_TARGETS", str(MAX_TARGETS_DEFAULT)
                ),
                run_number=_env_or(args.run_number, environ, f"{prefix}RUN_NUMBER", ""),
                only_if_pending=_env_or(
                    args.only_if_pending, environ, f"{prefix}ONLY_IF_PENDING", "false"
                ),
                target_url=_env_or(args.target_url, environ, f"{prefix}TARGET_URL", ""),
            )
        )
        config = _apply_workflow_ref(config, environ)
    except InputError as exc:
        err.write(f"::error title=Invalid required-checks input::{exc}\n")
        return 2
    return run_judge(config, out=out, err=err, event_path=environ.get("GITHUB_EVENT_PATH", ""))


if __name__ == "__main__":
    sys.exit(main())
