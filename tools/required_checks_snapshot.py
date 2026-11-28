# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Evidence snapshot ordering and required-check verdict rules (ADR-0108)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge
from required_checks_config import (
    DEFAULT_APP,
    MAX_CONTEXTS,
    MAX_LOGGED_FOREIGN_APPS,
    PR_EVENTS,
    PROPOSAL_EVENTS,
    TARGETS_OPEN_PULLS,
    Config,
    Evaluation,
    EvaluationRetry,
    Snapshot,
    _int_id,
    _iso_epoch,
    _truncate_description,
    status_context,
)
from required_checks_session import (
    ApiCapReachedError,
    ApiFailureError,
    ApiSession,
    PostFailureError,
    _exclude_self_statuses,
    _is_definitive_rejection,
    _post_age_problem,
    _should_post,
    _status_preflight,
    _verify_post,
    post_commit_status,
    read_statuses,
)

CHECK_RUN_MAX_PAGES = 20
WORKFLOW_RUN_MAX_PAGES = 10
MAX_CHECK_RUNS = CHECK_RUN_MAX_PAGES * forge.PER_PAGE
MAX_WORKFLOW_RUNS = WORKFLOW_RUN_MAX_PAGES * forge.PER_PAGE


def _override[T](name: str, default: T) -> T:
    facade = sys.modules.get("required_checks")
    return getattr(facade, name, default) if facade is not None else default


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
    admit = _override("_check_run_admitted", _check_run_admitted)
    by_name: dict[str, dict] = {}
    ignored: list[str] = []
    seen_ignored: set[str] = set()
    for run in check_runs[:MAX_CHECK_RUNS]:
        admitted_name, foreign_note = admit(run, admitted_suites, apps, self_context)
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


def _read_workflow_runs(session: ApiSession, config: Config) -> list[dict]:
    """Fail closed when the Actions workflow listing saturates the allowed pages."""
    max_pages = _override("WORKFLOW_RUN_MAX_PAGES", WORKFLOW_RUN_MAX_PAGES)
    runs_path = f"repos/{config.repository}/actions/runs?head_sha={config.sha}"
    rows_seen = 0
    row_cap = max_pages * forge.PER_PAGE

    def fetch(path: str) -> object:
        nonlocal rows_seen
        data = session.request_json("workflow-runs", path)
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        if isinstance(rows, list):
            rows_seen += len(rows)
            if rows_seen >= row_cap:
                raise ApiFailureError(
                    f"GitHub API workflow-runs cap reached: {max_pages} full "
                    f"pages ({row_cap} entries) on {config.sha}."
                )
        return data

    return forge.paged(runs_path, "workflow_runs", max_pages=max_pages, fetch=fetch)


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
    excluded = _override("foreign_suites", foreign_suites)(
        workflow_runs, config.event_name, config.ref
    )
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
    reason_fn = _override("exempt_reason", exempt_reason)
    missing: list[str] = []
    pending: list[str] = []
    for name in config.required[:MAX_CONTEXTS]:
        if reason_fn(name, config):
            continue
        run, status = _records_for(name, snapshot, config)
        if run is None and status is None:
            missing.append(name)
        elif _is_context_pending(run, status):
            pending.append(name)
    return missing, pending


def decide(
    snapshot: Snapshot,
    config: Config,
    now: float,
    anchor: float,
) -> tuple[str, str]:
    """Return (state, description) with state in success, failure, or pending."""
    reason_fn = _override("exempt_reason", exempt_reason)
    failures: list[str] = []
    for name in config.required[:MAX_CONTEXTS]:
        run, status = _records_for(name, snapshot, config)
        if reason_fn(name, config) or _terminal_failure(run, status):
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
    reason = _override("exempt_reason", exempt_reason)(name, config)
    run, status = _records_for(name, snapshot, config)
    if reason:
        return _judge_exempt(name, reason, run, status)
    if run is None and status is None:
        return False, f"{name}: never reported on SHA {config.sha}"
    run_problem = _override("_owed_run_problem", _owed_run_problem)(run)
    status_problem = _override("_owed_status_problem", _owed_status_problem)(status)
    problems = [p for p in (run_problem, status_problem) if p]
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
    decide_fn = _override("decide", decide)
    state, description = decide_fn(snapshot, config, session.now(), snapshot.anchor)
    return Evaluation(snapshot, state, description, read_started)


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
    if should_post or session.withdrawal_reserved or unconfirmed:
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
