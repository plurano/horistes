# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Review settling, bot dependency exemption, and incomplete-review evidence.

Started as an extract of the fleet planning gate reader; now diverges by ADR-0092.
This file holds the train's review-settle rules and canonical evidence reader.
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
    r"|Sorry @(?P<budget>[^\s,]+, you've used your own review budget[^\r\n]*)"
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
MAX_HEAD_CHECK_SUITES = 5
MAX_HEAD_CHECK_RUNS = 5
CODERABBIT_APP_ID = 347564
CODERABBIT_SLUG = "coderabbitai"
CODERABBIT_RUN = "CodeRabbit"
CODERABBIT_SUCCESS_LINE = "Result: Success - Review completed"
SOURCERY_APP_ID = 48477
SOURCERY_SLUG = "sourcery-ai"
SOURCERY_RUN = "Sourcery review"


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


def _pause_allowance_notices(body: str) -> tuple[list, bool, bool]:
    """Limit notices in body and whether the body is mixed pause plus allowance only."""
    lines = body.splitlines()
    if len(lines) > MAX_LINES:
        return [], False, False
    notices = [
        (idx, match)
        for idx, line in enumerate(lines[:MAX_LINES])
        if (match := LIMIT_NOTICE_RE.search(line))
    ]
    if not notices:
        return notices, False, False
    foreign = any(
        not match.group("paused") and not match.group("allowance")
        for _idx, match in notices[:MAX_LINES]
    )
    has_paused = any(match.group("paused") for _idx, match in notices[:MAX_LINES])
    has_allowance = any(match.group("allowance") for _idx, match in notices[:MAX_LINES])
    mixed = has_paused and has_allowance and not foreign
    return notices, mixed, foreign


def _paused_head_reviewed(body: str, head: str) -> bool:
    """Exempt only pause notices with unambiguous, block-local evidence for the full head."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    notices, mixed, foreign = _pause_allowance_notices(body)
    if not notices or foreign:
        return False
    pause_notices = [(idx, match) for idx, match in notices[:MAX_LINES] if match.group("paused")]
    if not pause_notices:
        return False
    lines = body.splitlines()
    reviewed = {
        match.group(1).lower()
        for line in lines[:MAX_LINES]
        if (match := LAST_REVIEWED_RE.fullmatch(line))
    }
    if reviewed != {head}:
        return False
    pause_ok = all(
        any(LAST_REVIEWED_RE.fullmatch(line) for line in _paused_block(lines, idx)[:MAX_LINES])
        for idx, _match in pause_notices[:MAX_LINES]
    )
    if mixed:
        return pause_ok and _allowance_head_reviewed(body, head)
    return pause_ok


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
        paused_head = _paused_head_reviewed(body, head)
        if _non_allowance_notice(body) and not paused_head:
            continue
        on_head = paused_head or _head_evidence(comment, head)
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


def _bounded_connection(conn, bound: int) -> tuple[list[dict], bool]:
    """Read a bounded GraphQL connection when totalCount matches every returned node."""
    if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
        return [], False
    count = conn.get("totalCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return [], False
    nodes = conn["nodes"]
    if len(nodes) > bound or count > bound or count != len(nodes):
        return [], False
    if any(not isinstance(node, dict) for node in nodes[:bound]):
        return [], False
    return nodes[:bound], True


def _budget_notice_only(body: str) -> bool:
    """Every limit notice in the body is the Sourcery budget line."""
    notices = list(LIMIT_NOTICE_RE.finditer(body))[:MAX_LINES]
    return bool(notices) and all(match.group("budget") for match in notices[:MAX_LINES])


def _latest_commit_page(conn) -> dict | None:
    """Read the latest commit page when it returns exactly one commit node."""
    if not isinstance(conn, dict):
        return None
    nodes = conn.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict):
        return None
    count = conn.get("totalCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    return nodes[0]


def _head_checks_commit(head_checks) -> dict | None:
    """The sole commit node under headChecks when it is fully readable."""
    node = _latest_commit_page(head_checks)
    if node is None:
        return None
    commit = node.get("commit")
    return commit if isinstance(commit, dict) else None


def _suite_app_matches(suite: dict, database_id: int, slug: str) -> bool:
    """Whether a check suite names the expected forge app."""
    app = suite.get("app") if isinstance(suite, dict) else None
    return (
        isinstance(app, dict) and app.get("databaseId") == database_id and app.get("slug") == slug
    )


def _run_summary_first_line(summary) -> str:
    """First line of a check-run summary; an empty summary has no first line."""
    if not isinstance(summary, str):
        return ""
    lines = summary.splitlines()
    return lines[0] if lines else ""


def _coderabbit_evidence_on_head(evidence: list) -> bool:
    """Whether review evidence records a timed CodeRabbit completion on the head."""
    for login, when, on_head in evidence[: MAX_COMMENTS + MAX_REVIEWS]:
        if login == CODERABBIT_SLUG and when is not None and on_head:
            return True
    return False


def _coderabbit_check_on_head(head_checks) -> bool:
    """Whether headChecks records a successful CodeRabbit check run on the head commit."""
    commit = _head_checks_commit(head_checks)
    if commit is None:
        return False
    suites, ok = _bounded_connection(commit.get("coderabbit"), MAX_HEAD_CHECK_SUITES)
    if not ok:
        return False
    for suite in suites[:MAX_HEAD_CHECK_SUITES]:
        if not _suite_app_matches(suite, CODERABBIT_APP_ID, CODERABBIT_SLUG):
            continue
        runs, runs_ok = _bounded_connection(suite.get("checkRuns"), MAX_HEAD_CHECK_RUNS)
        if not runs_ok:
            return False
        for run in runs[:MAX_HEAD_CHECK_RUNS]:
            if (
                run.get("name") == CODERABBIT_RUN
                and run.get("status") == "COMPLETED"
                and run.get("conclusion") == "SUCCESS"
                and _run_summary_first_line(run.get("summary")) == CODERABBIT_SUCCESS_LINE
            ):
                return True
    return False


def _sourcery_budget_skip_on_head(head_checks) -> bool:
    """Whether every Sourcery review run on the head is a skipped budget summary."""
    commit = _head_checks_commit(head_checks)
    if commit is None:
        return False
    suites, ok = _bounded_connection(commit.get("sourcery"), MAX_HEAD_CHECK_SUITES)
    if not ok:
        return False
    found = False
    for suite in suites[:MAX_HEAD_CHECK_SUITES]:
        if not _suite_app_matches(suite, SOURCERY_APP_ID, SOURCERY_SLUG):
            continue
        runs, runs_ok = _bounded_connection(suite.get("checkRuns"), MAX_HEAD_CHECK_RUNS)
        if not runs_ok:
            return False
        for run in runs[:MAX_HEAD_CHECK_RUNS]:
            if run.get("name") != SOURCERY_RUN:
                continue
            found = True
            first = _run_summary_first_line(run.get("summary"))
            match = LIMIT_NOTICE_RE.search(first)
            if (
                run.get("status") != "COMPLETED"
                or run.get("conclusion") != "SKIPPED"
                or not match
                or not match.group("budget")
            ):
                return False
    return found


def _sourcery_budget_exempt(comment: dict, head: str, evidence: list, node: dict) -> bool:
    """Drop a Sourcery budget notice when CodeRabbit finished and Sourcery skipped on the head."""
    login = _login(comment).lower().removesuffix("[bot]")
    if login != SOURCERY_SLUG:
        return False
    body = str(comment.get("body") or "")
    if not _budget_notice_only(body) or not re.fullmatch(r"[0-9a-f]{40}", head):
        return False
    head_checks = node.get("headChecks") if isinstance(node, dict) else None
    commit = _head_checks_commit(head_checks)
    if commit is None or str(commit.get("oid") or "").strip().lower() != head:
        return False
    coderabbit = _coderabbit_evidence_on_head(evidence) or _coderabbit_check_on_head(head_checks)
    return bool(coderabbit and _sourcery_budget_skip_on_head(head_checks))


def _notice_holds(
    comment: dict, head: str, last_commit, evidence: list, node: dict | None = None
) -> bool:
    """Apply head coverage, reviewer scope, and instant ordering to one notice body."""
    body = str(comment.get("body") or "")
    notices = list(LIMIT_NOTICE_RE.finditer(body))[:MAX_LINES]
    if not notices:
        return False
    when = _body_at(comment)
    if when is None or last_commit is None:
        return True
    allowance = all(notice.group("allowance") for notice in notices[:MAX_LINES])
    paused = any(notice.group("paused") for notice in notices[:MAX_LINES])
    _notices, mixed, _foreign = _pause_allowance_notices(body)
    covers = _notice_covers_head(body, head, allowance)
    stale_push = mixed and last_commit > when
    if _can_complete(comment) and covers and not stale_push:
        return False
    names_head = bool(head and head[:7] in body.lower())
    login = _login(comment).lower().removesuffix("[bot]")
    if _later_evidence(evidence, login, when, last_commit, allowance or paused or names_head):
        return False
    holds = allowance or paused or names_head or when >= last_commit
    pr = node if isinstance(node, dict) else {}
    if holds and _sourcery_budget_exempt(comment, head, evidence, pr):
        return False
    return holds


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
        if _notice_holds(comment, head, last_commit, evidence, node):
            when = _body_at(comment)
            stamp = when.isoformat().replace("+00:00", "Z") if when is not None else "head"
            return f"review incomplete: {_login(comment) or 'reviewer'} limit reached at {stamp}"
    return ""


_REVIEW_HOLD = frozenset({"CHANGES_REQUESTED", "COMMENTED"})


def _thread_replies_only(review):
    """Whether a review has no body and all comments are fetched thread replies."""
    comments = review.get("comments") or {}
    nodes = comments.get("nodes") or []
    return (
        not (review.get("body") or "").strip()
        and bool(nodes)
        and comments.get("totalCount") == len(nodes)
        and all(node.get("replyTo") for node in nodes)
    )


def _person_reply(review):
    """Whether a person's review on an app PR answers the reviews before it:
    an approval, or thread replies only.
    """
    if (review.get("author") or {}).get("__typename") != "User":
        return False
    if review.get("state") == "APPROVED":
        return True
    if review.get("state") != "COMMENTED":
        return False
    return _thread_replies_only(review)


def _is_bot_author(author):
    """Whether an author is a bot account: GraphQL type Bot, or a [bot] login."""
    if not isinstance(author, dict):
        return False
    return author.get("__typename") == "Bot" or (author.get("login") or "").endswith("[bot]")


BOT_COMMIT_READS = 50


def _commit_user(node):
    """Read the commit user without treating malformed author data as a bot."""
    for field in ("commit", "author", "user"):
        if not isinstance(node, dict):
            return None
        node = node.get(field)
    if not isinstance(node, dict) or not isinstance(node.get("login"), str):
        return None
    return node


def is_bot_dependency(pull, conf):
    """Exempt only wholly bot-authored dependency updates.

    A person's commit needs review even if a bot opened the PR or pushed last.
    Missing, malformed, or truncated author reads fail closed to full review.
    """
    if not pull["headRefName"].startswith(conf["dependency"]) or not _is_bot_author(
        pull.get("author")
    ):
        return False
    authors = pull.get("authors")
    if not isinstance(authors, dict):
        return False
    total, nodes = authors.get("totalCount"), authors.get("nodes")
    if isinstance(total, bool) or not isinstance(total, int) or not 1 <= total <= BOT_COMMIT_READS:
        return False
    if not isinstance(nodes, list) or len(nodes) != total:
        return False
    return all(_is_bot_author(_commit_user(node)) for node in nodes[:BOT_COMMIT_READS])


def _responds(review, pr_author, app_author):
    """Whether a head review answers: person reply on app PRs, author login otherwise."""
    if app_author:
        return _person_reply(review)
    author = review.get("author") or {}
    return author.get("login") == pr_author


def _standing_change_request(on_head):
    """The newest person change request on the head that the same person has not
    approved since, or None.
    """
    approvals = {}
    for review in on_head:
        author = review.get("author") or {}
        if author.get("__typename") == "User" and review.get("state") == "APPROVED":
            login = author.get("login")
            at = datetime.fromisoformat(review["submittedAt"])
            approvals[login] = max(approvals.get(login, at), at)
    standing = []
    for review in on_head:
        author = review.get("author") or {}
        at = datetime.fromisoformat(review["submittedAt"])
        if (
            author.get("__typename") == "User"
            and review.get("state") == "CHANGES_REQUESTED"
            and approvals.get(author.get("login"), at) <= at
        ):
            standing.append(review)
    return max(standing, key=lambda r: datetime.fromisoformat(r["submittedAt"]), default=None)


def _on_head_reviews(pull, connection="reviews"):
    """Submitted head reviews from the named connection."""
    head_oid = pull["headRefOid"]
    reviews = pull.get(connection, {}).get("nodes") or []
    return [
        r
        for r in reviews
        if (r.get("commit") or {}).get("oid") == head_oid and r.get("submittedAt")
    ]


def _review_timing_hold(on_head, pr_author, app_author, push_at):
    """Why a holding head review needs a strictly later push or reply, or None."""
    external = [
        r
        for r in on_head
        if not _responds(r, pr_author, app_author) and r.get("state") in _REVIEW_HOLD
    ]
    if not external:
        return None
    newest_ext = max(datetime.fromisoformat(r["submittedAt"]) for r in external)
    responses = (
        datetime.fromisoformat(r["submittedAt"])
        for r in on_head
        if _responds(r, pr_author, app_author)
    )
    settle_at = max(push_at, max(responses, default=push_at))
    if newest_ext >= settle_at:
        return (
            f"review settle: review at {newest_ext:%H:%M:%SZ} on head, "
            f"last response at {settle_at:%H:%M:%SZ}"
        )
    return None


def review_settle(pull, conf, now):
    """Why a pull request waits for its reviews to settle, or None.

    A submitted holding review on the current head at or after the author's
    last push or response holds the PR from landing; a tie holds on every PR.
    On an app PR, a person thread reply or approval answers earlier reviews;
    a new person review holds. A person's change request holds until that
    person approves later. An app's review, including the authoring app's
    own review, never answers on an app PR (ADR-0081).
    Opinions read separately; truncated reads hold app PRs.
    Bot dependency PRs with every commit bot-authored skip this check.
    Approved, dismissed, and pending reviews do not hold.
    """
    if is_bot_dependency(pull, conf):
        return None
    author_field = pull.get("author") or {}
    pr_author = author_field.get("login", "") if isinstance(author_field, dict) else ""
    app_author = _is_bot_author(author_field)
    nodes = pull["latest"]["nodes"]
    if not nodes:
        return None
    push_at = datetime.fromisoformat(nodes[-1]["commit"]["committedDate"])
    on_head = _on_head_reviews(pull)
    timing_hold = _review_timing_hold(on_head, pr_author, app_author, push_at)
    if timing_hold:
        return timing_hold
    if app_author:
        opinions = pull["opinions"]
        total, read = opinions["totalCount"], len(opinions["nodes"])
        if total > read:
            return f"review settle: {total} approvals or change requests, {read} read"
        standing = _standing_change_request(_on_head_reviews(pull, "opinions"))
        if standing:
            login = standing["author"]["login"]
            at = datetime.fromisoformat(standing["submittedAt"])
            return f"review settle: change request by {login} at {at:%H:%M:%SZ} on head stands"
    return None
