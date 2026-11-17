# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Answer whether a pull request review is incomplete (an empty string means complete).

Started as an extract of the fleet planning gate reader; now diverges by ADR-0092.
This file is the canonical reader for the train.
Callers pass a GraphQL pullRequest node or its data/repository envelope.
"""

import re
from datetime import UTC, datetime

MAX_COMMENTS = 100
MAX_COMMITS = 100
MAX_REVIEWS = 100
MAX_LINES = 2000
# A markdown row needs only name, status, and explanation; bound hostile wide tables.
MAX_CELLS = 32
MAX_SNIPPET = 160
COMPLETED_STATES = frozenset({"COMMENTED", "APPROVED", "CHANGES_REQUESTED"})

PRE_MERGE_MARKER = re.compile(r"pre[-_ ]merge[-_ ]checks", re.I)
STATUS_CELL_RE = re.compile(
    r"pre-merge-checks-(passed|warning|failed)\.svg|!\[(passed|warning|failed)\]",
    re.I,
)
LIMIT_NOTICE_RE = re.compile(
    # Notice lines only: walkthrough prose and table cells can discuss limits.
    r"(?im)^[ \t]*(?:>[ \t]*)?(?:#{1,6}[ \t]+(?:[^\w\s][ \t]*){0,4})?(?:\*\*)?"
    r"(?:(?:rate[- ]limit|review[- ]limit)[ \t]+(?:was[ \t]+)?(?:reached|exceeded|hit)"
    r"(?:[ \t]+for[ \t]+(?:this[ \t]+(?:repository|organization)"
    r"|commit[ \t]+[0-9a-f]{7,40}))?"
    r"|Sorry @[^\s,]+, you've used your own review budget[^\r\n]*"
    r"|(?P<paused>reviews[ \t]+paused)"
    r"|(?P<allowance>(?:Included review availability:[^\r\n]*;[ \t]*)?"
    r"0[ \t]+remain[ \t]+after[ \t]+this[ \t]+review)"
    r"|Copilot was unable to review this pull request because the user "
    r"who requested the review has "
    r"insufficient remaining quota)"
    r"(?:\*\*)?(?:[.!:](?:[ \t]|\r?$)|[ \t]*\r?$)"
)
PAUSED_START = "<!-- This is an auto-generated comment: review paused by coderabbit.ai -->"
PAUSED_END = "<!-- end of auto-generated comment: review paused by coderabbit.ai -->"
PAUSED_HEADING_RE = re.compile(
    r"^[ \t]*(?P<quote>>[ \t]*)?#{1,6}[ \t]+(?:\*\*)?reviews[ \t]+paused(?:\*\*)?[.!:]?[ \t]*$",
    re.IGNORECASE,
)
LAST_REVIEWED_RE = re.compile(
    r"^[ \t]*(?:>[ \t]*)?(?:-[ \t]+)?Last reviewed commit:[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE,
)
# Source: tests/fixtures/review-state/cr1976-summary-allowance-6900214744.md
# (CodeRabbit's reviewed range).
REVIEWED_RANGE_RE = re.compile(
    r"^[ \t]*Reviewing files that changed from the base of the PR and between "
    r"([0-9a-f]{40}) and ([0-9a-f]{40})\.[ \t]*\r?$",
    re.IGNORECASE | re.MULTILINE,
)


def _pr_node(pr: dict) -> dict:
    """Unwrap GraphQL data/repository/pullRequest envelope when present."""
    node = pr if isinstance(pr, dict) else {}
    for key in ("data", "repository", "pullRequest"):
        inner = node.get(key) if isinstance(node, dict) else None
        if isinstance(inner, dict):
            node = inner
    return node if isinstance(node, dict) else {}


def _login(node: dict) -> str:
    """Extract author login from a comment, review, or pullRequest dict."""
    if not isinstance(node, dict):
        return ""
    author = node.get("author")
    if isinstance(author, dict):
        return str(author.get("login") or "").strip()
    return str(author or "").strip()


def _nodes(conn, bound: int) -> list[dict]:
    """Bounded list of dict nodes from a GraphQL connection or list."""
    if isinstance(conn, dict):
        raw = conn.get("nodes") or []
    elif isinstance(conn, list):
        raw = conn
    else:
        raw = []
    return [x for x in raw[:bound] if isinstance(x, dict)] if isinstance(raw, list) else []


def _instant(value) -> datetime | None:
    """Parse an aware ISO timestamp as a UTC instant; malformed or naive is unknown."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _body_at(comment: dict) -> datetime | None:
    """Latest body edit; a supplied malformed timestamp cannot prove freshness."""
    original = comment.get("submittedAt") or comment.get("createdAt")
    created = _instant(original)
    if "updatedAt" not in comment:
        return created
    updated = _instant(comment["updatedAt"])
    return max(created, updated) if created is not None and updated is not None else None


def _last_commit_at(pr: dict) -> datetime | None:
    """Newest commit instant; an absent or unknown commit time makes the result unknown."""
    times = []
    for node in _nodes(pr.get("commits"), MAX_COMMITS)[:MAX_COMMITS]:
        commit = node.get("commit") if isinstance(node.get("commit"), dict) else node
        instant = _instant(commit.get("committedDate"))
        if instant is None:
            return None
        times.append(instant)
    return max(times) if times else None


def pre_merge_rows(body: str) -> list[tuple[str, str, str]]:
    """[(check_name, status, explanation)] from a Pre-merge checks markdown table."""
    text = str(body or "")
    if not PRE_MERGE_MARKER.search(text):
        return []
    rows = []
    for raw in text.splitlines()[:MAX_LINES]:
        line = raw.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")[:MAX_CELLS]]
        if len(cells) < 2 or not cells[0]:
            continue
        hit = STATUS_CELL_RE.search(cells[1])
        if not hit:
            continue
        status = (hit.group(1) or hit.group(2) or "").lower()
        explanation = " ".join((cells[2] if len(cells) >= 3 else "").split())[:MAX_SNIPPET]
        rows.append((cells[0], status, explanation))
    return rows


def _answerers(pr: dict, me: str) -> set[str]:
    """Author plus caller on another author's PR; own PRs retain one identity."""
    author = _login(pr).lower().removesuffix("[bot]")
    caller = str(me or "").strip().lower()
    return {login for login in (author, caller)[:2] if login}


def _all_comments(pr: dict) -> list[dict]:
    """Comments and all reviews, retaining state, commit, and body edit times."""
    return (_nodes(pr.get("comments"), MAX_COMMENTS) + _nodes(pr.get("reviews"), MAX_REVIEWS))[
        : MAX_COMMENTS + MAX_REVIEWS
    ]


def _marker_block(lines: list[str], notice_idx: int, markers: list) -> list[str]:
    """Only paired, alternating markers establish a pause block."""
    if len(markers) % 2 or any(
        marker != (PAUSED_START if idx % 2 == 0 else PAUSED_END).casefold()
        for idx, (_line_idx, marker) in enumerate(markers[:MAX_LINES])
    ):
        return []
    for idx in range(0, min(len(markers), MAX_LINES), 2):
        start, end = markers[idx][0], markers[idx + 1][0]
        if start < notice_idx < end:
            return lines[start + 1 : end][:MAX_LINES]
    return []


def _paused_block(lines: list[str], notice_idx: int) -> list[str]:
    """Bound a pause by paired markers, or by its heading's section and quote."""
    markers = [
        (idx, line.strip().casefold())
        for idx, line in enumerate(lines[:MAX_LINES])
        if line.strip().casefold() in (PAUSED_START.casefold(), PAUSED_END.casefold())
    ]
    if markers:
        return _marker_block(lines, notice_idx, markers)
    heading = PAUSED_HEADING_RE.fullmatch(lines[notice_idx])
    if not heading:
        return []
    end = min(len(lines), MAX_LINES)
    for idx in range(notice_idx + 1, end):
        line = lines[idx].lstrip(" \t")
        if heading.group("quote") and not line.startswith(">"):
            end = idx
            break
        content = line.removeprefix(">").lstrip(" \t")
        if re.match(r"#{1,6}[ \t]+|<!--", content):
            end = idx
            break
    return lines[notice_idx + 1 : end][:MAX_LINES]


def _paused_head_reviewed(body: str, head: str) -> bool:
    """Exempt only pause notices with unambiguous, block-local evidence for the full head."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    lines = body.splitlines()
    if len(lines) > MAX_LINES:
        return False
    notices = [
        (idx, match)
        for idx, line in enumerate(lines[:MAX_LINES])
        if (match := LIMIT_NOTICE_RE.search(line))
    ]
    if not notices or any(not match.group("paused") for _idx, match in notices[:MAX_LINES]):
        return False
    reviewed = {
        match.group(1).lower()
        for line in lines[:MAX_LINES]
        if (match := LAST_REVIEWED_RE.fullmatch(line))
    }
    if reviewed != {head}:
        return False
    return all(
        any(LAST_REVIEWED_RE.fullmatch(line) for line in _paused_block(lines, idx)[:MAX_LINES])
        for idx, _match in notices[:MAX_LINES]
    )


def _allowance_head_reviewed(body: str, head: str) -> bool:
    """Require exactly one reviewed range ending at the full head in an allowance summary."""
    if not re.fullmatch(r"[0-9a-f]{40}", head) or len(body.splitlines()) > MAX_LINES:
        return False
    ranges = list(REVIEWED_RANGE_RE.finditer(body))
    return len(ranges) == 1 and ranges[0].group(2).lower() == head


def _non_allowance_notice(body: str) -> bool:
    """A skipped review cannot also prove completed review evidence."""
    return any(
        not match.group("allowance") for match in list(LIMIT_NOTICE_RE.finditer(body))[:MAX_LINES]
    )


def _head_evidence(comment: dict, head: str) -> bool:
    """Allow only a completed formal review of the full head or an exact reviewed range."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    commit = comment.get("commit")
    formal = comment.get("state") in COMPLETED_STATES and isinstance(commit, dict)
    return bool(formal and commit.get("oid") == head) or _allowance_head_reviewed(
        str(comment.get("body") or ""), head
    )


def _can_complete(comment: dict) -> bool:
    """Issue comments or known completed review states may carry completion evidence."""
    if "state" not in comment:
        return True
    state = comment["state"]
    return isinstance(state, str) and state in COMPLETED_STATES


def review_evidence(node: dict, me: str = "") -> list[tuple[str, datetime | None, bool]]:
    """One scoped evidence reader for both notice clearing and the opt-in head barrier."""
    answerers = _answerers(node, me)
    head = str(node.get("headRefOid") or "").strip().lower()
    evidence = []
    for comment in _all_comments(node)[: MAX_COMMENTS + MAX_REVIEWS]:
        login = _login(comment).lower().removesuffix("[bot]")
        if not login or login in answerers:
            continue
        if not _can_complete(comment):
            continue
        body = str(comment.get("body") or "")
        if _non_allowance_notice(body):
            continue
        on_head = _head_evidence(comment, head)
        if comment.get("state") in COMPLETED_STATES or pre_merge_rows(body) or on_head:
            evidence.append((login, _body_at(comment), on_head))
    return evidence


def _later_evidence(evidence: list, login: str, notice_at, last_commit, head_only: bool) -> bool:
    """Unknown instants cannot establish a later completion from the same reviewer."""
    if notice_at is None or last_commit is None:
        return False
    for who, when, on_head in evidence[: MAX_COMMENTS + MAX_REVIEWS]:
        if who != login or when is None:
            continue
        if when > notice_at and when >= last_commit and (on_head or not head_only):
            return True
    return False


def _notice_covers_head(body: str, head: str, allowance: bool) -> bool:
    """Only the explicit pause block or allowance range can exempt its own notice."""
    return _paused_head_reviewed(body, head) or (allowance and _allowance_head_reviewed(body, head))


def _notice_holds(comment: dict, head: str, last_commit, evidence: list) -> bool:
    """Apply head coverage, reviewer scope, and instant ordering to one notice body."""
    body = str(comment.get("body") or "")
    notices = list(LIMIT_NOTICE_RE.finditer(body))[:MAX_LINES]
    if not notices:
        return False
    when = _body_at(comment)
    if when is None or last_commit is None:
        return True
    allowance = all(notice.group("allowance") for notice in notices[:MAX_LINES])
    if _can_complete(comment) and _notice_covers_head(body, head, allowance):
        return False
    names_head = bool(head and head[:7] in body.lower())
    login = _login(comment).lower().removesuffix("[bot]")
    if _later_evidence(evidence, login, when, last_commit, allowance or names_head):
        return False
    return allowance or names_head or when >= last_commit


def review_incomplete(pr: dict, me: str = "") -> str:
    """Report skipped reviews with per-login completion and fail-closed time comparisons."""
    node = _pr_node(pr)
    answerers = _answerers(node, me)
    head = str(node.get("headRefOid") or "").strip().lower()
    last_commit = _last_commit_at(node)
    evidence = review_evidence(node, me)
    comments = sorted(
        _all_comments(node),
        key=lambda c: _body_at(c) or datetime.max.replace(tzinfo=UTC),
        reverse=True,
    )
    for comment in comments[: MAX_COMMENTS + MAX_REVIEWS]:
        if _login(comment).lower().removesuffix("[bot]") in answerers:
            continue
        if _notice_holds(comment, head, last_commit, evidence):
            when = _body_at(comment)
            stamp = when.isoformat().replace("+00:00", "Z") if when is not None else "head"
            return f"review incomplete: {_login(comment) or 'reviewer'} limit reached at {stamp}"
    return ""
