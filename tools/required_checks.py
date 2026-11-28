# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Judge required check runs and commit statuses on a commit in one pass (ADR-0108).

This module and its `required_checks_*` siblings use the Python standard library
and the sibling `forge` reader only, so they run under `python3 -I -B` from a
sparse checkout without `.venv`.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from math import ceil
from pathlib import Path
from typing import TextIO
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge
from required_checks_config import (
    ALLOWED_TARGETS,
    BOUND_MINUTES_DEFAULT,
    BOUND_MINUTES_MAX,
    BOUND_MINUTES_MIN,
    DEFAULT_APP,
    DEFAULT_CONTEXT,
    DEFAULT_SELF_WORKFLOW,
    FLOOR,
    MAX_CONTEXTS,
    MAX_LOGGED_FOREIGN_APPS,
    MAX_RUN_NUMBER,
    MAX_SELF_TESTS,
    MAX_TARGETS_DEFAULT,
    MAX_TARGETS_MAX,
    MAX_TARGETS_MIN,
    OPEN_PULLS_MAX,
    PR_EVENTS,
    PROPOSAL_EVENTS,
    PUSH_CONTEXT_SUFFIX,
    SHA_RE,
    STATUS_DESCRIPTION_MAX,
    TARGETS_OPEN_PULLS,
    TARGETS_SHA,
    Config,
    Evaluation,
    EvaluationRetry,
    InputError,
    RawInputs,
    Snapshot,
    _build_parser,
    _fork_head_repository,
    _iso_epoch,
    _load_json,
    _path_from_workflow_ref,
    below_floor,
    config_from_cli,
    parse_config,
    parse_policy,
    status_context,
)
from required_checks_selftest import run_self_test
from required_checks_session import (
    API_BACKOFF_SECONDS,
    MAX_CALL_ATTEMPTS,
    MAX_RATE_LIMIT_SECONDS,
    MAX_RATE_LIMIT_WAITS_PER_CALL,
    MAX_STATUSES,
    POST_FRESH_SECONDS,
    SESSION_DEADLINE_SECONDS,
    STATUS_MAX_PAGES,
    WITHDRAWAL_RESERVE_SECONDS,
    WORK_DEADLINE_SECONDS,
    ApiCapReachedError,
    ApiFailureError,
    ApiSession,
    PostFailureError,
    ServerBackoffDeadlineError,
    TransportFailureError,
    WorkDeadlineError,
    _all_statuses,
    _has_contender,
    _own_post_is_newest_non_success,
    _post_is_contested,
    _read_status_page_one,
    _should_post,
    _status_timestamp,
    newest_statuses,
    post_commit_status,
    read_statuses,
)
from required_checks_snapshot import (
    CHECK_RUN_MAX_PAGES,
    MAX_CHECK_RUNS,
    MAX_WORKFLOW_RUNS,
    WORKFLOW_RUN_MAX_PAGES,
    _check_run_admitted,
    _check_run_key,
    _owed_run_problem,
    _owed_status_problem,
    _warn_api_cap,
    _withdraw_unverified_success,
    decide,
    evaluate_one,
    exempt_reason,
    foreign_suites,
    is_foreign_run,
    judge_context,
    judge_snapshot,
    newest_check_runs,
    owed_waiting,
    read_snapshot,
    self_workflow_suites,
)

SWEEP_MAX_CALLS = 250
TRIAGE_MAX_CALLS = 120

__all__ = [
    "ALLOWED_TARGETS",
    "API_BACKOFF_SECONDS",
    "ApiCapReachedError",
    "ApiFailureError",
    "ApiSession",
    "BOUND_MINUTES_DEFAULT",
    "BOUND_MINUTES_MAX",
    "BOUND_MINUTES_MIN",
    "CHECK_RUN_MAX_PAGES",
    "Config",
    "DEFAULT_APP",
    "DEFAULT_CONTEXT",
    "DEFAULT_SELF_WORKFLOW",
    "Evaluation",
    "EvaluationRetry",
    "FLOOR",
    "InputError",
    "MAX_CALL_ATTEMPTS",
    "MAX_CHECK_RUNS",
    "MAX_CONTEXTS",
    "MAX_LOGGED_FOREIGN_APPS",
    "MAX_RATE_LIMIT_SECONDS",
    "MAX_RATE_LIMIT_WAITS_PER_CALL",
    "MAX_RUN_NUMBER",
    "MAX_SELF_TESTS",
    "MAX_STATUSES",
    "MAX_TARGETS_DEFAULT",
    "MAX_TARGETS_MAX",
    "MAX_TARGETS_MIN",
    "MAX_WORKFLOW_RUNS",
    "OPEN_PULLS_MAX",
    "POST_FRESH_SECONDS",
    "PROPOSAL_EVENTS",
    "PR_EVENTS",
    "PUSH_CONTEXT_SUFFIX",
    "PostFailureError",
    "RawInputs",
    "SESSION_DEADLINE_SECONDS",
    "SHA_RE",
    "STATUS_DESCRIPTION_MAX",
    "STATUS_MAX_PAGES",
    "SWEEP_MAX_CALLS",
    "ServerBackoffDeadlineError",
    "Snapshot",
    "TARGETS_OPEN_PULLS",
    "TARGETS_SHA",
    "TRIAGE_MAX_CALLS",
    "TransportFailureError",
    "WITHDRAWAL_RESERVE_SECONDS",
    "WORKFLOW_RUN_MAX_PAGES",
    "WORK_DEADLINE_SECONDS",
    "WorkDeadlineError",
    "_all_statuses",
    "_check_run_admitted",
    "_check_run_key",
    "_default_branch_head",
    "_has_contender",
    "_iso_epoch",
    "_load_json",
    "_owed_run_problem",
    "_owed_status_problem",
    "_own_post_is_newest_non_success",
    "_path_from_workflow_ref",
    "_post_is_contested",
    "_should_post",
    "_sweep_evaluation_order",
    "_sweep_pull_heads",
    "_withdraw_unverified_success",
    "below_floor",
    "decide",
    "evaluate_one",
    "exempt_reason",
    "foreign_suites",
    "is_foreign_run",
    "judge_context",
    "judge_snapshot",
    "main",
    "newest_check_runs",
    "newest_statuses",
    "owed_waiting",
    "parse_config",
    "parse_policy",
    "post_commit_status",
    "read_snapshot",
    "read_statuses",
    "run_judge",
    "run_sweep",
    "self_test",
    "self_workflow_suites",
    "status_context",
]


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


def self_test(out: TextIO = sys.stdout) -> int:
    """Run planted positive and negative cases in memory; return 0 when all match."""
    return run_self_test(sys.modules[__name__], out)


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """CLI entrypoint for `self-test` and `judge`."""
    refusal = below_floor(tuple(sys.version_info), FLOOR)
    if refusal is not None:
        err.write(f"::error::{refusal}\n")
        return 2
    args = _build_parser().parse_args(argv)
    if args.command == "self-test":
        return self_test(out)
    environ = os.environ if env is None else env
    try:
        config = config_from_cli(args, environ)
    except InputError as exc:
        err.write(f"::error title=Invalid required-checks input::{exc}\n")
        return 2
    return run_judge(config, out=out, err=err, event_path=environ.get("GITHUB_EVENT_PATH", ""))


if __name__ == "__main__":
    sys.exit(main())
