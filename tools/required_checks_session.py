# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Bounded forge HTTP session and commit-status read/write verification (ADR-0108)."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from math import floor
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge
from required_checks_config import (
    MAX_CONTEXTS,
    Config,
    Evaluation,
    Snapshot,
    _bounded_reason,
    _int_id,
    _iso_epoch,
    _judge_context_keys,
    _reject_constant,
    status_context,
)

SESSION_DEADLINE_SECONDS = 8 * 60
WITHDRAWAL_RESERVE_SECONDS = 30
WORK_DEADLINE_SECONDS = SESSION_DEADLINE_SECONDS - WITHDRAWAL_RESERVE_SECONDS
POST_FRESH_SECONDS = 10
MAX_RATE_LIMIT_SECONDS = 300
API_BACKOFF_SECONDS = (30, 60, 120)
MAX_RATE_LIMIT_WAITS_PER_CALL = 6
MAX_CALL_ATTEMPTS = len(API_BACKOFF_SECONDS) + MAX_RATE_LIMIT_WAITS_PER_CALL + 1
STATUS_MAX_PAGES = 20
MAX_STATUSES = STATUS_MAX_PAGES * forge.PER_PAGE
_WITHDRAWABLE_STATES = frozenset({"failure", "pending"})


def _override[T](name: str, default: T) -> T:
    facade = sys.modules.get("required_checks")
    return getattr(facade, name, default) if facade is not None else default


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
        max_waits = _override("MAX_RATE_LIMIT_WAITS_PER_CALL", MAX_RATE_LIMIT_WAITS_PER_CALL)
        if waits >= max_waits:
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
) -> tuple[dict[str, dict], bool, list[dict] | None]:
    """Return recent statuses, completeness, and the first page to recheck if paginated."""
    first_page, complete = _read_status_page_one(session, config)

    def covered(rows: list[dict]) -> bool:
        return any(_status_in_context(row, key) for row in rows[:MAX_STATUSES])

    if complete or covered(first_page):
        return _all_statuses(first_page), complete, None
    statuses, complete, _ = _read_status_pages(session, config, 2, list(first_page), covered)
    return _all_statuses(statuses), complete, first_page


def _status_preflight(session: ApiSession, config: Config, evaluation: Evaluation) -> str:
    """Read through the first judge status or end of the listing before posting."""
    key = status_context(config).casefold()
    recent, complete, first_page = _preflight_statuses(session, config, key)
    newest = recent.get(key)
    if newest is None and not complete:
        return "the newest judge status is outside the pre-post status page"
    # The snapshot read the statuses before the checks and this read follows them.
    # Statuses are immutable and ids grow, so any judge status created in between ranks
    # newest and differs from the snapshot's.
    # Do not compare the forge timestamp with the runner clock: they are different
    # time domains and the stamp of an existing status is not the server's current time.
    if newest != evaluation.snapshot.self_status:
        return "the judge status moved during the evidence read"
    if _owed_status_changed(config, evaluation.snapshot, recent, complete=complete):
        return "required commit status evidence changed during the evidence read"
    # Do not accept evidence assembled across pages if the listing head shifted.
    if first_page is not None:
        final_page, final_complete = _read_status_page_one(session, config)
        if final_complete or final_page != first_page:
            return "the first status page changed during the pre-post status read"
    return _post_age_problem(session, evaluation)


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
    if not complete and not _statuses_covered(rows, owed_keys, oldest_contender):
        initial_rows = list(rows)
        rows, complete, confirmed = _read_status_pages(
            session,
            config,
            2,
            rows,
            lambda read_rows: _statuses_covered(read_rows, owed_keys, oldest_contender),
        )
        if not confirmed:
            return True, False
        final_page1, final_complete = _read_status_page_one(session, config)
        if final_complete or final_page1 != initial_rows:
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
