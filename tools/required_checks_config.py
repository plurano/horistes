# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Configuration, check-policy, and caller input validation for `required_checks` (ADR-0108).

Uses the Python standard library only so `python3 -I -B` runs from a sparse checkout.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

FLOOR = (3, 12)
MAX_CONTEXTS = 500
BOUND_MINUTES_MIN = 1
BOUND_MINUTES_MAX = 330
BOUND_MINUTES_DEFAULT = 240
MAX_TARGETS_MIN = 1
MAX_TARGETS_MAX = 100
MAX_TARGETS_DEFAULT = 30
MAX_RUN_NUMBER = 10**9
OPEN_PULLS_MAX = 100
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


def below_floor(version: tuple[int, ...], floor: tuple[int, int] = FLOOR) -> str | None:
    """Refusal when running on a Python older than `floor`, else None."""
    if tuple(version[:2]) >= floor:
        return None
    return (
        f"the required-checks judge needs Python {floor[0]}.{floor[1]} or newer; "
        f"this python3 is {version[0]}.{version[1]}"
    )


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


def _truncate_description(text: str) -> str:
    if len(text) <= STATUS_DESCRIPTION_MAX:
        return text
    return text[: STATUS_DESCRIPTION_MAX - 3] + "..."


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


def config_from_cli(args: argparse.Namespace, environ: Mapping[str, str]) -> Config:
    """Resolve CLI options and environment variables into a validated `Config`."""
    prefix = "REQUIRED_CHECKS_"
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
            run_number=_env_or(args.run_number, environ, f"{prefix}RUN_NUMBER", ""),
            only_if_pending=_env_or(
                args.only_if_pending, environ, f"{prefix}ONLY_IF_PENDING", "false"
            ),
            target_url=_env_or(args.target_url, environ, f"{prefix}TARGET_URL", ""),
        )
    )
    return _apply_workflow_ref(config, environ)
