# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Answer whether a pull request review is incomplete (an empty string means complete).

Verbatim extract of the review-incomplete reader of the fleet planning gate.
This file is its canonical home; later gate changes come here by pull request.
Callers pass a GraphQL pullRequest node or its data/repository envelope.
"""

import re

MAX_COMMENTS = 100
MAX_COMMITS = 100
MAX_REVIEWS = 100
MAX_LINES = 2000
MAX_SNIPPET = 160

PRE_MERGE_MARKER = re.compile(r"pre[-_ ]merge[-_ ]checks", re.I)
STATUS_CELL_RE = re.compile(
    r"pre-merge-checks-(passed|warning|failed)\.svg|!\[(passed|warning|failed)\]",
    re.I,
)
LIMIT_NOTICE_RE = re.compile(
    # Notice lines only: walkthrough prose and table cells can discuss limits.
    r"(?im)^[ \t]*(?:>[ \t]*)?(?:#{1,6}[ \t]+)?(?:\*\*)?"
    r"(?:(?:rate[- ]limit|review[- ]limit)[ \t]+(?:was[ \t]+)?(?:reached|exceeded|hit)"
    r"(?:[ \t]+for[ \t]+commit[ \t]+[0-9a-f]{7,40})?"
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
    return [x for x in raw[:bound] if isinstance(x, dict)]


def _last_commit_at(pr: dict) -> str:
    """Newest committedDate across commits in the pull request."""
    latest = ""
    for node in _nodes(pr.get("commits"), MAX_COMMITS):
        commit = node.get("commit") if isinstance(node.get("commit"), dict) else node
        ts = str(commit.get("committedDate") or "").strip()
        if ts > latest:
            latest = ts
    return latest


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
        cells = [c.strip() for c in line.strip("|").split("|")]
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
    author = _login(pr).lower()
    caller = str(me or "").strip().lower()
    return {login for login in (author, caller) if login}


def _all_comments(pr: dict) -> list[dict]:
    """PR comments and body-bearing reviews normalized with createdAt."""
    out = list(_nodes(pr.get("comments"), MAX_COMMENTS))
    for rev in _nodes(pr.get("reviews"), MAX_REVIEWS):
        if str(rev.get("body") or "").strip():
            out.append(
                {
                    "url": rev.get("url") or "",
                    "author": rev.get("author"),
                    "createdAt": rev.get("submittedAt") or "",
                    "body": rev.get("body") or "",
                }
            )
    return out[: MAX_COMMENTS + MAX_REVIEWS]


def _paused_block(lines: list[str], notice_idx: int) -> list[str]:
    """Bound a pause by paired markers, or by its heading's section and quote."""
    markers = [
        (idx, line.strip().casefold())
        for idx, line in enumerate(lines)
        if line.strip().casefold() in (PAUSED_START.casefold(), PAUSED_END.casefold())
    ]
    if markers:
        if len(markers) % 2 or any(
            marker != (PAUSED_START if idx % 2 == 0 else PAUSED_END).casefold()
            for idx, (_line_idx, marker) in enumerate(markers)
        ):
            return []
        for idx in range(0, len(markers), 2):
            start, end = markers[idx][0], markers[idx + 1][0]
            if start < notice_idx < end:
                return lines[start + 1 : end]
        return []
    heading = PAUSED_HEADING_RE.fullmatch(lines[notice_idx])
    if not heading:
        return []
    end = len(lines)
    for idx in range(notice_idx + 1, len(lines)):
        line = lines[idx].lstrip(" \t")
        if heading.group("quote") and not line.startswith(">"):
            end = idx
            break
        content = line.removeprefix(">").lstrip(" \t")
        if re.match(r"#{1,6}[ \t]+|<!--", content):
            end = idx
            break
    return lines[notice_idx + 1 : end]


def _paused_head_reviewed(body: str, head: str) -> bool:
    """Exempt only pause notices with unambiguous, block-local evidence for the full head."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    lines = body.splitlines()
    if len(lines) > MAX_LINES:
        return False
    notices = [
        (idx, match) for idx, line in enumerate(lines) if (match := LIMIT_NOTICE_RE.search(line))
    ]
    if not notices or any(not match.group("paused") for _idx, match in notices):
        return False
    reviewed = {
        match.group(1).lower() for line in lines if (match := LAST_REVIEWED_RE.fullmatch(line))
    }
    if reviewed != {head}:
        return False
    return all(
        any(LAST_REVIEWED_RE.fullmatch(line) for line in _paused_block(lines, idx))
        for idx, _match in notices
    )


def _allowance_head_reviewed(body: str, head: str) -> bool:
    """Require exactly one reviewed range ending at the full head in an allowance summary."""
    if not re.fullmatch(r"[0-9a-f]{40}", head) or len(body.splitlines()) > MAX_LINES:
        return False
    ranges = list(REVIEWED_RANGE_RE.finditer(body))
    return len(ranges) == 1 and ranges[0].group(2).lower() == head


def _completed_review_at(pr: dict, comments: list[dict], answerers: set[str]) -> str:
    """Newest completed review evidence; skipped reviews and author replies cannot cover pushes."""
    latest = ""
    reviews = _nodes(pr.get("reviews"), MAX_REVIEWS)
    for comment in (comments + reviews)[: MAX_COMMENTS + 2 * MAX_REVIEWS]:
        if _login(comment).lower() in answerers:
            continue
        body = str(comment.get("body") or "")
        notices = list(LIMIT_NOTICE_RE.finditer(body))
        if any(not notice.group("allowance") for notice in notices):
            continue
        submitted = str(comment.get("submittedAt") or "").strip()
        formal_review = submitted and comment.get("state") in {
            "COMMENTED",
            "APPROVED",
            "CHANGES_REQUESTED",
        }
        if formal_review or notices or pre_merge_rows(body):
            latest = max(latest, submitted or str(comment.get("createdAt") or "").strip())
    return latest


def review_incomplete(pr: dict, me: str = "") -> str:
    """Report skipped head reviews or exhausted allowances followed by an unreviewed push."""
    node = _pr_node(pr)
    answerers = _answerers(node, me)
    head = str(node.get("headRefOid") or "").strip().lower()
    last_commit = _last_commit_at(node)
    comments = _all_comments(node)
    completed_at = _completed_review_at(node, comments, answerers)
    newest_table_at = ""
    for comment in comments[: MAX_COMMENTS + MAX_REVIEWS]:
        if pre_merge_rows(comment.get("body", "")):
            c_at = str(comment.get("createdAt") or "").strip()
            if c_at > newest_table_at:
                newest_table_at = c_at
    for comment in comments[: MAX_COMMENTS + MAX_REVIEWS]:
        who = _login(comment).lower()
        if who in answerers:
            continue
        body = str(comment.get("body") or "")
        notices = list(LIMIT_NOTICE_RE.finditer(body))
        if not notices:
            continue
        if _paused_head_reviewed(body, head):
            continue
        c_at = str(comment.get("createdAt") or "").strip()
        if all(notice.group("allowance") for notice in notices):
            if _allowance_head_reviewed(body, head):
                continue
            incomplete = bool(
                c_at and last_commit and c_at < last_commit and completed_at < last_commit
            )
        else:
            if newest_table_at and c_at < newest_table_at:
                continue
            names_head = bool(head and head[:7] in body.lower())
            incomplete = names_head or not last_commit or c_at >= last_commit
        if incomplete:
            return (
                f"review incomplete: {_login(comment) or 'reviewer'} "
                f"limit reached at {c_at or 'head'}"
            )
    return ""
