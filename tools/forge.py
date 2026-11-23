# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Reads the runs of a trial from the forge into the shape verdict.py judges.

The trial reader issues GET requests through the forge's command-line client
(`gh api`). It asks no host but the forge;
the download of an artifact is answered with a redirect to the forge's
artifact storage, which the client follows. The client is taken from the
environment variable GH when it is set.

Shape of the result:

    {"source": "live forge", "repository": "owner/name", "errors": [],
     "subjects": [{"commit": ..., "event": ..., "ref": ..., "occurrence": 1,
                   "not_in_tree": [".github/workflows/..."],
                   "checks": [{"name": ..., "workflow": ...,
                               "attempts": [{"attempt": 1, "status": ...,
                                             "conclusion": ..., "failed_steps": [],
                                             "annotations": [], "seconds": 12.0}],
                               "result": {...}}]}]}

A check named in `artifacts` (check name to artifact name) carries the result
its latest attempt uploaded under that name, in the record tools/results.py
describes. It is read only for an attempt that concluded success. Of the
artifacts of that name only those created since the attempt started count:
after a re-run that uploads nothing, the artifact of the earlier attempt is
not taken for it.

A subject is one triggering event on one commit. The forge gives the runs
of one event no shared identifier, so runs are grouped by commit, event and
branch, and where one workflow ran several times for such a group (a draft
marked ready, a reopened pull request) its n-th run in time belongs to the
n-th subject.

`not_in_tree` names the watched workflow files that did not run on a push
and that the pushed commit does not hold; the key is left out when there is
none. The forge starts a run from the workflow files of the commit the
event names, and for a push that is the commit the run names. The reader
asks for the listing of .github/workflows at that commit: a file is named
only when that listing came, is complete, holds every watched workflow that
did run, and does not hold the file. A pull request runs the files of its
merge commit, which a run does not name, so no file is named there.

A request that fails is recorded under "errors" and ends the reading; the
caller must treat a result with errors as unusable.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from urllib.parse import quote

import results

PER_PAGE = 100
MAX_PAGES = 10
RETRIES = 4
WAIT_SECONDS = 20
DEADLINE_SECONDS = 60
HEADER_BLOCKS = 10
MAX_HEADER_LINES = 200
WORKFLOW_FOLDER = ".github/workflows"
# The forge's listing of a folder holds at most this many entries (REST reference,
# "Get repository content"); a listing that long may be cut.
FOLDER_LIMIT = 1000
# Events whose run names the commit its workflow files were read from.
READ_AT_COMMIT = ("push",)
HTTP_STATUS = re.compile(r"\bHTTP (\d{3})\b")
HEADERS = (
    "-H",
    "Accept: application/vnd.github+json",
    "-H",
    "X-GitHub-Api-Version: 2022-11-28",
)


class ForgeError(Exception):
    """A request the forge did not answer with readable data."""


class ResponseError(ForgeError):
    """An HTTP error from `gh api --include` carrying status, headers and message."""

    def __init__(
        self,
        detail: str,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        message: str = "",
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.headers = headers or {}
        self.message = message


def _gh_run(
    args: list[str], *, token: str | None = None, deadline: float = DEADLINE_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded `gh api` command and return its completed process."""
    command = [*shlex.split(os.environ.get("GH", "gh")), "api", *args]
    env = {**os.environ, "GH_TOKEN": token} if token is not None else None
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args, dangerous-subprocess-use-audit expires 2027-03-09 category D: direct gh api argv with no shell and a binary override for test stand-ins  # noqa: E501
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        timeout=deadline,
        env=env,
    )


def download(path: str, *, token: str | None = None) -> bytes:
    """The body of one GET as it came; a rate limit is waited out a few times."""
    for attempt in range(RETRIES):
        try:
            done = _gh_run([*HEADERS, path], token=token)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"GET {path}: {err}") from err
        if done.returncode == 0:
            return done.stdout
        complaint = done.stderr.decode("utf-8", "replace")
        if "rate limit" not in complaint.lower():
            raise ForgeError(f"GET {path}: {complaint.strip()[:300] or done.returncode}")
        if attempt + 1 < RETRIES:
            time.sleep(WAIT_SECONDS * (attempt + 1))
    raise ForgeError(f"GET {path}: rate limit, {RETRIES} attempts used")


def _split_http_block(raw: bytes) -> tuple[bytes, bytes] | None:
    """Split one HTTP status-and-header block from the bytes that follow it."""
    crlf = raw.find(b"\r\n\r\n")
    lf = raw.find(b"\n\n")
    if crlf >= 0 and (lf < 0 or crlf < lf):
        return raw[:crlf], raw[crlf + 4 :]
    if lf >= 0:
        return raw[:lf], raw[lf + 2 :]
    return None


def _parse_header_block(head_bytes: bytes) -> tuple[int, dict[str, str]] | None:
    """Parse one HTTP status line and header block into (status, lowercase_headers)."""
    lines = head_bytes.decode("iso-8859-1").splitlines()
    parts = lines[0].split() if lines else []
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    headers: dict[str, str] = {}
    for line in lines[1 : MAX_HEADER_LINES + 1]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return int(parts[1]), headers


def _parse_included(raw: bytes) -> tuple[int, dict[str, str], bytes] | None:
    """Parse `gh api --include` stdout into (status, lowercase_headers, body)."""
    current = raw
    for _ in range(HEADER_BLOCKS):
        split = _split_http_block(current) if current.startswith(b"HTTP/") else None
        parsed = _parse_header_block(split[0]) if split is not None else None
        if split is None or parsed is None:
            return None
        status, headers = parsed
        body = split[1]
        if (100 <= status < 200 or 300 <= status < 400) and body.startswith(b"HTTP/"):
            current = body
            continue
        return status, headers, body
    return None


def _json_message(body: bytes) -> str:
    """Extract a top-level string `message` from JSON bytes, or empty string."""
    if not body:
        return ""
    try:
        decoded = json.loads(body)
    except ValueError:
        return ""
    msg = decoded.get("message") if isinstance(decoded, dict) else None
    return msg.strip() if isinstance(msg, str) else ""


def _error_message(body: bytes, stderr: str) -> str:
    """Combine any JSON API `message` field with `gh` stderr."""
    body_msg = _json_message(body)
    if body_msg and stderr and body_msg not in stderr:
        return f"{body_msg} ({stderr})"
    return body_msg or stderr


ALLOWED_METHODS = frozenset({"GET", "POST"})


def request(
    path: str,
    *,
    method: str = "GET",
    fields: Mapping[str, str] | None = None,
    token: str | None = None,
    deadline: float = DEADLINE_SECONDS,
) -> tuple[int, dict[str, str], bytes]:
    """One `gh api --include` call. Writes require explicit `method="POST"`."""
    if method not in ALLOWED_METHODS:
        raise ValueError(f"unsupported HTTP method {method!r}")
    args = ["--include", *HEADERS, "-X", method, path]
    if fields:
        for key, value in fields.items():
            args.extend(["-f", f"{key}={value}"])
    try:
        done = _gh_run(args, token=token, deadline=deadline)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ForgeError(f"{method} {path}: {err}") from err
    stderr = done.stderr.decode("utf-8", "replace").strip()
    parsed = _parse_included(done.stdout)
    if parsed is not None:
        status, headers, body = parsed
        if done.returncode == 0 and 200 <= status < 300:
            return status, headers, body
        message = _error_message(body, stderr)
        detail = message[:300] or str(done.returncode)
        raise ResponseError(
            f"{method} {path}: HTTP {status}: {detail}",
            status=status,
            headers=headers,
            message=message,
        )
    if done.returncode == 0:
        raise ForgeError(f"{method} {path}: missing HTTP status in --include output")
    match = HTTP_STATUS.search(stderr)
    status = int(match.group(1)) if match else None
    raise ResponseError(
        f"{method} {path}: {stderr[:300] or done.returncode}",
        status=status,
        headers={},
        message=stderr,
    )


def _finite_number(raw: str | None) -> float | None:
    """Parse a finite float from a header string, or None when absent or invalid."""
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def is_rate_limit(status: int | None, headers: Mapping[str, str], message: str) -> bool:
    """Whether an HTTP 403 or 429 answer carries a primary or secondary rate-limit signal."""
    if status not in (403, 429):
        return False
    return (
        "retry-after" in headers
        or headers.get("x-ratelimit-remaining") == "0"
        or "rate limit" in message.lower()
    )


def rate_limit_delay(
    status: int | None,
    headers: Mapping[str, str],
    message: str,
    repeat: int,
    now: float,
) -> int | float | None:
    """Seconds to wait for a rate-limited response, or None when not a rate limit."""
    if not is_rate_limit(status, headers, message):
        return None
    retry_after = _finite_number(headers.get("retry-after"))
    reset = _finite_number(headers.get("x-ratelimit-reset"))
    if headers.get("x-ratelimit-remaining") == "0" and reset is not None:
        reset_delay = max(0, int(reset) - math.floor(now)) + 5
        if retry_after is None or reset_delay >= retry_after:
            return reset_delay
    if retry_after is not None and retry_after >= 0:
        return int(retry_after) if retry_after.is_integer() else retry_after
    return min(60 * (2**repeat), 960)


def api(path: str, *, token: str | None = None) -> object:
    """The decoded answer of one GET."""
    try:
        return json.loads(download(path, token=token))
    except ValueError as err:
        raise ForgeError(f"GET {path}: answer is not JSON ({err})") from err


def paged(
    path: str,
    key: str | None,
    *,
    max_pages: int = MAX_PAGES,
    fetch: Callable[[str], object] | None = None,
) -> list[dict]:
    """Every entry of a listing; `key` names the list inside the answer, None when it is the answer.

    One additional read distinguishes an exactly full listing from overflow.
    """
    reader = api if fetch is None else fetch
    entries: list[dict] = []
    joint = "&" if "?" in path else "?"
    for page in range(1, max_pages + 2):
        answer = reader(f"{path}{joint}per_page={PER_PAGE}&page={page}")
        batch = answer.get(key) if key and isinstance(answer, dict) else answer
        if not isinstance(batch, list):
            raise ForgeError(f"GET {path}: no list{' ' + key if key else ''} in the answer")
        if page > max_pages and batch:
            raise ForgeError(f"GET {path}: more than {max_pages * PER_PAGE} entries")
        entries.extend(entry for entry in batch if isinstance(entry, dict))
        if len(batch) < PER_PAGE:
            break
    return entries


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(job: dict) -> float | None:
    started, ended = _time(job.get("started_at")), _time(job.get("completed_at"))
    if started is None or ended is None:
        return None
    return max(0.0, (ended - started).total_seconds())


def _result(repository: str, run: object, name: str, started: object) -> dict:
    """What a job that started at `started` uploaded as the artifact `name` of its run."""
    since = _time(started)
    listing = f"repos/{repository}/actions/runs/{run}/artifacts?name={quote(name, safe='')}"
    mine = []
    for artifact in paged(listing, "artifacts"):
        created = _time(artifact.get("created_at"))
        if since is None or created is None:
            raise ForgeError(f"GET {listing}: no time to tell the attempts of {name!r} apart")
        if artifact.get("name") == name and created >= since:
            mine.append((created, artifact))
    if not mine:
        return {"state": "none", "artifact": name}
    newest = max(mine, key=lambda entry: entry[0])[1]
    if newest.get("expired"):
        return {"state": "expired", "artifact": name}
    size = newest.get("size_in_bytes")
    if not isinstance(size, int) or size > results.MAX_BYTES:
        detail = f"archive of {size} bytes, at most {results.MAX_BYTES} are read"
        return {"state": "unreadable", "artifact": name, "detail": detail}
    data = download(f"repos/{repository}/actions/artifacts/{newest.get('id')}/zip")
    try:
        read = results.from_archive(data, str(newest.get("digest") or ""))
    except results.ResultError as err:
        return {"state": "unreadable", "artifact": name, "detail": str(err)}
    return {"state": "read", "artifact": name, **read}


def _attempt(repository: str, number: int, job: dict) -> dict:
    """One attempt of one job: its conclusion and what names the reason of a failure."""
    failed = [
        str(step.get("name"))
        for step in job.get("steps") or []
        if isinstance(step, dict) and step.get("conclusion") == "failure"
    ]
    notes = []
    if job.get("conclusion") in ("failure", "timed_out"):
        for note in paged(f"repos/{repository}/check-runs/{job.get('id')}/annotations", None):
            notes.append(" ".join(str(note[k]) for k in ("title", "message") if note.get(k)))
    return {
        "attempt": number,
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "failed_steps": failed,
        "annotations": notes,
        "seconds": _seconds(job),
    }


def _checks(repository: str, run: dict, artifacts: dict[str, str]) -> list[dict]:
    """The jobs of one run as checks, each with every attempt of the run and its result."""
    checks: dict[str, dict] = {}
    started: dict[str, object] = {}
    for number in range(1, int(run.get("run_attempt") or 1) + 1):
        path = f"repos/{repository}/actions/runs/{run['id']}/attempts/{number}/jobs"
        for job in paged(path, "jobs"):
            name = str(job.get("name"))
            check = checks.setdefault(
                name, {"name": name, "workflow": run.get("path"), "attempts": []}
            )
            check["attempts"].append(_attempt(repository, number, job))
            started[name] = job.get("started_at")
    for name, check in checks.items():
        if name in artifacts and check["attempts"][-1]["conclusion"] == "success":
            check["result"] = _result(repository, run["id"], artifacts[name], started[name])
    return list(checks.values())


def _groups(runs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Runs as subjects: (commit, event, ref, occurrence) with the runs that belong to it."""
    grouped: dict[tuple, dict[str, list[dict]]] = {}
    for run in sorted(runs, key=lambda r: (str(r.get("created_at")), r["id"])):
        key = (run.get("head_sha"), run.get("event"), run.get("head_branch"))
        grouped.setdefault(key, {}).setdefault(str(run.get("path")), []).append(run)
    subjects = []
    for (commit, event, ref), per_workflow in grouped.items():
        for index in range(max(len(found) for found in per_workflow.values())):
            subject = {"commit": commit, "event": event, "ref": ref, "occurrence": index + 1}
            members = [found[index] for found in per_workflow.values() if index < len(found)]
            subjects.append((subject, members))
    return subjects


def _not_in_tree(repository: str, subject: dict, ran: set[str], workflows: set[str]) -> list[str]:
    """The watched workflow files without a run that the commit of a push does not hold."""
    silent = sorted(p for p in workflows - ran if posixpath.dirname(p) == WORKFLOW_FOLDER)
    if subject["event"] not in READ_AT_COMMIT or not silent:
        return []
    commit = quote(str(subject["commit"]), safe="")
    path = f"repos/{repository}/contents/{WORKFLOW_FOLDER}?ref={commit}"
    listing = api(path)
    if not isinstance(listing, list) or len(listing) >= FOLDER_LIMIT:
        raise ForgeError(f"GET {path}: no complete listing of the folder")
    held = {str(entry.get("path")) for entry in listing if isinstance(entry, dict)}
    lost = sorted(ran - held)
    if lost:
        raise ForgeError(f"GET {path}: the listing lacks {lost[0]}, whose run this commit started")
    return [path for path in silent if path not in held]


def collect(
    repository: str,
    branches: list[str],
    workflows: set[str],
    artifacts: dict[str, str] | None = None,
) -> dict:
    """The runs of the named workflow files on the named branches, as subjects with checks.

    `artifacts` names, per check that uploads a result, the artifact that holds it.
    """
    data = {"source": "live forge", "repository": repository, "errors": [], "subjects": []}
    try:
        runs = {}
        for branch in branches:
            listing = f"repos/{repository}/actions/runs?branch={quote(branch, safe='')}"
            for run in paged(listing, "workflow_runs"):
                if run.get("path") in workflows and "id" in run:
                    runs[run["id"]] = run
        for subject, members in _groups(list(runs.values())):
            subject["checks"] = []
            for run in members:
                subject["checks"].extend(_checks(repository, run, artifacts or {}))
            ran = {str(run.get("path")) for run in members}
            gone = _not_in_tree(repository, subject, ran, workflows)
            if gone:
                subject["not_in_tree"] = gone
            data["subjects"].append(subject)
    except ForgeError as err:
        data["errors"].append(str(err))
    return data
