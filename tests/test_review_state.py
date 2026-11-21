# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Replay the fleet gate's incomplete-review cases without CLI or ledger readers."""

import copy
import json
from pathlib import Path

import pytest
from review_state import (
    MAX_COMMENTS,
    MAX_LINES,
    MAX_SUMMARY_CHARS,
    is_bot_dependency,
    review_incomplete,
)

FIXTURES = Path(__file__).parent / "fixtures/review-state"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _case(name, pr, expected, me=""):
    return pytest.param(copy.deepcopy(pr), me, expected, id=name)


PASSED_TABLE = "\n".join(
    (
        "<!-- pre_merge_checks_walkthrough_start -->",
        "<details><summary><strong>Pre-merge checks: 5 passed</strong></summary>",
        "| Check name | Status | Explanation |",
        "| :--- | :---: | :--- |",
        (
            "| Description Check | ![Passed](https://storage.googlea"
            "pis.com/coderabbit_public_assets/pre-merge-checks-passe"
            "d.svg) | Check skipped - CodeRabbit’s high-level summar"
            "y is enabled. |"
        ),
        (
            "| Title check | ![Passed](https://storage.googleapis.co"
            "m/coderabbit_public_assets/pre-merge-checks-passed.svg)"
            " | The title names the main change: a go fix check is a"
            "dded to the pre-push hook. |"
        ),
        (
            "| Docstring Coverage | ![Passed](https://storage.google"
            "apis.com/coderabbit_public_assets/pre-merge-checks-pass"
            "ed.svg) | No eligible functions found in the changed fi"
            "les. Skipping docstring coverage check. |"
        ),
        (
            "| Linked Issues check | ![Passed](https://storage.googl"
            "eapis.com/coderabbit_public_assets/pre-merge-checks-pas"
            "sed.svg) | Check skipped because no linked issues were "
            "found for this pull request. |"
        ),
        (
            "| Out of Scope Changes check | ![Passed](https://storag"
            "e.googleapis.com/coderabbit_public_assets/pre-merge-che"
            "cks-passed.svg) | Check skipped because no linked issue"
            "s were found for this pull request. |"
        ),
        "</details>",
    )
)


def _fixture_golusoris_1266() -> dict:
    "Measured golusoris/golusoris #1266 at head 4fff004 (all 5 pre-merge checks passed)."
    return {
        "number": 1266,
        "headRefOid": "4fff004d61625647b16f68da7fed03fdc30cc7df",
        "author": {"login": "lusoris"},
        "reviewThreads": {"nodes": []},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "coderabbitai"},
                    "createdAt": "2026-10-31T19:02:54Z",
                    "body": PASSED_TABLE,
                }
            ]
        },
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": "68432148f65434757c98aee0be195d8d0cf137e3",
                        "committedDate": "2026-10-31T18:50:31Z",
                        "messageHeadline": "ci(hooks): run the go fix check in pre-push",
                    }
                },
                {
                    "commit": {
                        "oid": "4fff004d61625647b16f68da7fed03fdc30cc7df",
                        "committedDate": "2026-10-31T19:02:42Z",
                        "messageHeadline": "Merge branch 'main' into fix/prepush-go-fix",
                    }
                },
            ]
        },
        "reviews": {"nodes": []},
    }


LIMIT_BODIES = [
    (
        "walkthrough",
        (
            "No actionable comments were generated in the recent review.\n"
            "Incomplete reads are not published (publish runs only when result.complete). "
            "Rate-limit deferrals return before fallback."
        ),
        False,
    ),
    ("walkthrough-line", "Rate-limit deferrals return before fallback.", False),
    ("summary-topic", "## Summary\nTopics: rate limits, retry budgets, fallback.", False),
    ("table-file", "| File | Change |\n| `tests/rate-limit-test.py` | Added coverage |", False),
    (
        "pre-merge-table",
        ("Pre-merge checks\n| rate-limit_test.py | ![Passed](passed.svg) | Rate limits covered |"),
        False,
    ),
    ("limit-handling", "Rate limit exceeded handling now retries safely.", False),
    ("client-limit", "Rate limit exceeded for client 123; retry handling added.", False),
    ("embedded-notice", "Tests assert 'Rate limit exceeded' on exhaustion.", False),
    (
        "nonzero-allowance",
        (
            "Included review availability: Your plan provides up to 10 "
            "included reviews per hour; 9 remain after this review."
        ),
        False,
    ),
    (
        "rate-exceeded-wait",
        "Rate limit exceeded. Please wait 10 minutes before requesting another review.",
        True,
    ),
    (
        "rate-reached-head",
        "Rate limit reached for commit 4fff004d61625647b16f68da7fed03fdc30cc7df. Review skipped.",
        True,
    ),
    ("review-reached", "review limit reached", True),
    ("review-exceeded", "Review limit was exceeded.", True),
    ("rate-hit", "Rate-limit hit.", True),
    ("reviews-paused", "reviews paused", True),
    ("zero-allowance", "0 remain after this review", True),
    (
        "coderabbit-header",
        (
            "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
            "> [!WARNING]\n> ## Rate limit exceeded\n> Please wait 10 minutes."
        ),
        True,
    ),
    (
        "coderabbit-allowance",
        (
            "Included review availability: Your plan provides up to 1 "
            "included review per hour; 0 remain after this review."
        ),
        True,
    ),
    ("bold-notice", "**Review limit reached**. Please wait.", True),
    ("crlf-header", "> ## Rate limit exceeded\r\n> Please wait 10 minutes.", True),
    (
        "copilot-quota",
        (
            "Copilot was unable to review this pull request because "
            "the user who requested the review has insufficient rema"
            "ining quota. Try requesting a review at a lower effort "
            "level."
        ),
        True,
    ),
]


def _limit_notice_cases():
    """Distinguish bot limit notices from rate-limit subjects in review summaries."""
    pr = _fixture_golusoris_1266()
    cases = []
    for name, body, expected in LIMIT_BODIES[:MAX_COMMENTS]:
        pr["comments"]["nodes"] = [
            {"author": {"login": "coderabbitai"}, "createdAt": "2026-11-14T04:25:24Z", "body": body}
        ]
        cases.append(_case(f"limit-notice-{name}", pr, expected))
    return cases


def _limit_notice_order_cases():
    "Preserve notice freshness, head matching, table supersession, and review bodies."
    pr = _fixture_golusoris_1266()
    table = copy.deepcopy(pr["comments"]["nodes"][0])
    pr["commits"]["nodes"] = [{"commit": {"committedDate": "2026-11-14T04:00:00Z"}}]
    comment = {
        "author": {"login": "coderabbitai"},
        "createdAt": "2026-11-14T04:01:00Z",
        "body": "Rate limit exceeded. Please wait 10 minutes.",
    }
    pr["comments"]["nodes"] = [comment]
    cases = [_case("limit-notice-newer-than-commit", pr, True)]
    comment["createdAt"] = "2026-11-14T04:00:00Z"
    cases.append(_case("limit-notice-same-time-as-commit", pr, True))
    comment["createdAt"] = "2026-11-14T03:59:00Z"
    cases.append(_case("limit-notice-older-than-commit", pr, False))
    comment["body"] = f"Rate limit reached for commit {pr['headRefOid']}. Review skipped."
    cases.append(_case("limit-notice-old-names-head", pr, True))
    table["createdAt"] = "2026-11-14T04:02:00Z"
    pr["comments"]["nodes"].append(table)
    cases.append(_case("limit-notice-head-named-newer-table-holds", pr, True))
    table["createdAt"] = comment["createdAt"]
    cases.append(_case("limit-notice-same-time-table-keeps-notice", pr, True))
    comment["author"]["login"] = "lusoris"
    cases.append(_case("limit-notice-author-ignored", pr, False))
    comment["author"]["login"] = "copilot"
    comment["submittedAt"] = comment["createdAt"]
    pr["reviews"]["nodes"] = [comment]
    pr["comments"]["nodes"] = []
    cases.append(_case("limit-notice-review-body", pr, True))
    return cases


head = "2afde8915e64fc845709914081d84f75c31b226f"
older = "a" * 40
start = "<!-- This is an auto-generated comment: review paused by coderabbit.ai -->"
end = "<!-- end of auto-generated comment: review paused by coderabbit.ai -->"
heading = "> ## Reviews paused"
sha_line = f"> - Last reviewed commit: {head}"
block = (
    f"{start}\n> [!NOTE]\n{heading}\n"
    "> It looks like this branch is under active development. To avoid overwhelming you "
    "with review comments due to an influx of new commits, "
    "CodeRabbit has automatically paused this review.\n"
    f"{sha_line}\n> Use the following commands to manage reviews:\n"
    "> - `@coderabbitai resume` to resume automatic reviews.\n"
    f"> - `@coderabbitai review` to trigger a single review.\n{end}"
)
bodies = [
    ("replay-head-reviewed", block, head, False),
    ("replay-older-sha", block, older, True),
    ("crlf-block", block.replace("\n", "\r\n"), head, False),
    ("markers-without-heading", block.replace(heading, "Reviews paused"), head, False),
    ("uppercase-sha", block.replace(head, head.upper()), head, False),
    ("uppercase-head", block, head.upper(), False),
    ("no-sha", block.replace(sha_line, ""), head, True),
    ("short-sha", block.replace(head, head[:12]), head, True),
    ("long-sha", block.replace(head, head + "a"), head, True),
    ("malformed-sha", block.replace(head, "g" + head[1:]), head, True),
    ("empty-head", block, "", True),
    ("short-head", block, head[:12], True),
    ("sha-outside-markers", block.replace(sha_line, "") + "\n" + sha_line, head, True),
    ("sha-before-markers", sha_line + "\n" + block.replace(sha_line, ""), head, True),
    ("conflicting-shas", block + f"\nLast reviewed commit: {older}", head, True),
    (
        "conflicting-shas-in-block",
        block.replace(sha_line, sha_line + f"\n> - Last reviewed commit: {older}"),
        head,
        True,
    ),
    ("same-sha-repeated", block.replace(sha_line, sha_line + "\n" + sha_line.upper()), head, False),
    ("heading-only", f"{heading}\n>\n{sha_line}", head, False),
    ("plain-heading", f"## Reviews paused\n\nLast reviewed commit: {head}", head, False),
    ("sha-outside-quote", f"{heading}\n> Paused.\nLast reviewed commit: {head}", head, True),
    ("sha-after-next-heading", f"{heading}\n> ## Summary\n{sha_line}", head, True),
    (
        "sha-after-plain-heading",
        f"## Reviews paused\n## Summary\nLast reviewed commit: {head}",
        head,
        True,
    ),
    ("sha-before-heading", f"{sha_line}\n{heading}", head, True),
    ("unclosed-marker", block.replace(end, ""), head, True),
    ("unopened-marker", block.replace(start, ""), head, True),
    ("second-pause-without-sha", block + "\n## Reviews paused", head, True),
    ("second-block-without-sha", block + "\n" + block.replace(sha_line, ""), head, True),
    ("both-blocks-reviewed", block + "\n" + block, head, False),
    ("too-many-lines", block + "\n>" * MAX_LINES, head, True),
]
limits = [
    "Rate limit reached.",
    "Rate limit exceeded.",
    "Rate limit hit.",
    "Review limit reached.",
    "Review limit exceeded.",
    "Review limit hit.",
    "0 remain after this review",
    "> ## Rate limit exceeded",
    (
        "Copilot was unable to review this pull request because "
        "the user who requested the review has insufficient rema"
        "ining quota."
    ),
]


def _paused_notice_bodies():
    return bodies + [
        (f"real-limit-{idx}", block + "\n" + notice, head, True)
        for idx, notice in enumerate(limits[:MAX_COMMENTS])
    ]


def _paused_notice_cases():
    """Check paused-block evidence through the normal limit reader."""
    pr = _fixture_golusoris_1266()
    cases = []
    for name, body, current_head, expected in _paused_notice_bodies()[:MAX_COMMENTS]:
        pr["headRefOid"] = current_head
        pr["comments"]["nodes"] = [
            {"author": {"login": "coderabbitai"}, "createdAt": "2026-11-14T04:25:24Z", "body": body}
        ]
        cases.append(_case(f"paused-notice-{name}", pr, expected))
    _name, block, head, _expected = _paused_notice_bodies()[0]
    warning = _fixture_golusoris_1266()
    warning["headRefOid"] = head
    warning["comments"]["nodes"][0]["body"] = (
        PASSED_TABLE.replace("![Passed]", "![Warning]", 1) + "\n" + block
    )
    cases.append(_case("paused-notice-keeps-warning", warning, False))
    warning["reviewThreads"]["nodes"] = [
        {
            "isResolved": False,
            "comments": {
                "nodes": [
                    {"author": {"login": "reviewer"}, "body": "Still needs fixing."},
                ]
            },
        }
    ]
    cases.append(_case("paused-notice-keeps-thread", warning, False))
    warning["comments"]["nodes"].append(
        {
            "author": {"login": "copilot"},
            "createdAt": "2026-11-14T04:25:24Z",
            "body": "Rate limit exceeded.",
        }
    )
    cases.append(_case("paused-notice-keeps-other-comment-limit", warning, True))
    return cases


def _promo_cases():
    """Promo variants carry no incomplete-review notice; retain the gate's case names."""
    fixture = _fixture("review-gate-coderabbit-promo-and-allowance-bodies.json")["promo"]
    pr = fixture["pr"]
    comment = pr["reviewThreads"]["nodes"][0]["comments"]["nodes"][-1]
    body = comment["body"]
    start = "<!-- This is an auto-generated comment: tweet message by coderabbit.ai -->"
    end = "<!-- end of auto-generated comment: tweet message by coderabbit.ai -->"
    variants = [
        ("promo-1958-no-ask", body),
        ("promo-outside-question-kept", body + "\nCan you verify the fix?"),
        ("promo-original-outside-request-kept", body + fixture["outside_request"]),
        ("promo-unterminated-kept", body.replace(end, "")),
        ("promo-near-start-kept", body.replace(start, start.upper())),
        ("promo-near-end-kept", body.replace(end, end.upper())),
        ("promo-unmarked-kept", body.replace(start, "").replace(end, "")),
        ("promo-multiple-pairs", body + body),
    ]
    cases = []
    for name, variant in variants[:MAX_COMMENTS]:
        comment["body"] = variant
        pr["comments"]["nodes"] = [dict(comment)]
        cases.append(_case(name, pr, False, "lusoris"))
    return cases


def _allowance_cases():
    """Last included review completes its head; only subsequent unreviewed pushes hold."""
    pr = _fixture("review-gate-coderabbit-promo-and-allowance-bodies.json")["allowance"]["pr"]
    cases = [_case("allowance-576-current-head-complete", pr, False)]
    commit = pr["commits"]["nodes"][0]["commit"]
    comment = pr["comments"]["nodes"][0]
    commit["committedDate"] = comment["createdAt"]
    cases.append(_case("allowance-same-time-complete", pr, False))
    commit["committedDate"] = "2026-11-14T20:48:00Z"
    commit["oid"] = pr["headRefOid"] = "a" * 40
    cases.append(_case("allowance-576-next-push-incomplete", pr, True))
    reviewed = {
        "author": {"login": "coderabbitai"},
        "state": "COMMENTED",
        "submittedAt": "2026-11-14T20:49:00Z",
        "body": "",
    }
    pr["reviews"]["nodes"] = [reviewed]
    cases.append(_case("allowance-later-review-without-head-oid-holds", pr, True))
    reviewed["commit"] = {"oid": pr["headRefOid"]}
    cases.append(_case("allowance-later-review-on-head-clears", pr, False))
    reviewed["author"] = {"login": "copilot"}
    cases.append(_case("allowance-later-review-other-login-on-head-holds", pr, True))
    reviewed["author"] = {"login": "coderabbitai"}
    reviewed["submittedAt"] = "2026-11-14T20:46:00Z"
    cases.append(_case("allowance-head-review-older-than-notice-holds", pr, True))
    reviewed["submittedAt"] = "2026-11-14T20:49:00Z"
    reviewed.pop("commit")
    reviewed["commit"] = {"oid": "c" * 40}
    cases.append(_case("allowance-later-review-old-oid-holds", pr, True))
    reviewed.pop("commit")
    reviewed["state"] = "PENDING"
    cases.append(_case("allowance-pending-review-insufficient", pr, True))
    reviewed["state"] = "COMMENTED"
    reviewed["author"] = {"login": "lusoris"}
    cases.append(_case("allowance-author-review-insufficient", pr, True))
    reviewed["author"] = {"login": "coderabbitai"}
    reviewed["body"] = "Rate limit exceeded. Review skipped."
    cases.append(_case("allowance-skipped-review-insufficient", pr, True))
    reviewed["body"] = ""
    reviewed["submittedAt"] = "2026-11-14T20:47:30Z"
    cases.append(_case("allowance-review-before-push-insufficient", pr, True))
    pr["reviews"]["nodes"] = []
    later = dict(comment, createdAt="2026-11-14T20:49:00Z")
    pr["comments"]["nodes"].append(later)
    cases.append(_case("allowance-later-summary-old-range-holds", pr, True))
    later["createdAt"] = "2026-11-14T20:47:30Z"
    cases.append(_case("allowance-later-table-before-push-insufficient", pr, True))
    later["createdAt"] = commit["committedDate"]
    cases.append(_case("allowance-table-at-push-old-range-holds", pr, True))
    pr["comments"]["nodes"].pop()
    comment["body"] = "Rate limit exceeded. Review skipped."
    commit["committedDate"] = "2026-11-14T20:40:00Z"
    cases.append(_case("allowance-576-real-limit-incomplete", pr, True))
    comment["body"] += "\n0 remain after this review"
    cases.append(_case("allowance-mixed-real-limit-incomplete", pr, True))
    return cases


def _allowance_range_cases():
    """Rewritten allowance summaries cover only one unambiguous full reviewed head."""
    fixture = FIXTURES / "cr1976-summary-allowance-6900214744.md"
    body = fixture.read_text(encoding="utf-8")
    head = "793a759aea1c963feace48e67e6cf3590a37e3dd"
    other = "a" * 40
    range_line = next(
        line
        for line in body.splitlines()[:MAX_LINES]
        if line.startswith("Reviewing files that changed")
    )
    variants = [
        ("fixture-head", body, head, False),
        ("other-head", body, other, True),
        ("missing-range", body.replace(range_line, ""), head, True),
        (
            "short-range-head",
            body.replace(range_line, range_line.replace(head, head[:7])),
            head,
            True,
        ),
        ("conflicting-ranges", body + "\n" + range_line.replace(head, other), head, True),
        ("duplicate-range", body + "\n" + range_line, head, True),
        ("short-pr-head", body, head[:7], True),
        (
            "long-range-head",
            body.replace(range_line, range_line.replace(head, head + "0")),
            head,
            True,
        ),
        ("range-keeps-rate-limit", body + "\nRate limit reached.", head, True),
        ("range-keeps-pause", body + "\n## Reviews paused", head, True),
    ]
    cases = []
    for name, summary, current_head, expected in variants[:MAX_LINES]:
        pr = {
            "headRefOid": current_head,
            "author": {"login": "lusoris"},
            "commits": {
                "nodes": [
                    {"commit": {"oid": current_head, "committedDate": "2026-11-16T07:44:00Z"}}
                ]
            },
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "coderabbitai"},
                        "body": summary,
                        "createdAt": "2026-11-15T19:45:04Z",
                        "updatedAt": "2026-11-16T11:28:43Z",
                    }
                ]
            },
        }
        cases.append(_case(f"allowance-range-{name}", pr, expected))
    return cases


def _thread_and_limit_cases():
    """Keep only the limit half of the gate's thread-and-limit cases."""
    pr = _fixture_golusoris_1266()
    pr["comments"]["nodes"] = [
        {
            "author": {"login": "coderabbitai"},
            "createdAt": "2026-10-31T19:05:00Z",
            "body": (
                "Rate limit reached for commit 4fff004d61625647b16f68da7"
                "fed03fdc30cc7df. Review skipped."
            ),
        }
    ]
    return [_case("review-limit-notice-no-item", pr, True, "lusoris")]


@pytest.mark.parametrize(
    ("pr", "me", "expected"),
    _limit_notice_cases()
    + _limit_notice_order_cases()
    + _paused_notice_cases()
    + _promo_cases()
    + _allowance_cases()
    + _allowance_range_cases()
    + _thread_and_limit_cases(),
)
def test_review_incomplete_gate_cases(pr, me, expected):
    assert bool(adr92_reader().review_incomplete(pr, me)) is expected


@pytest.mark.parametrize("envelope", [False, True], ids=["node", "data-repository-envelope"])
def test_review_incomplete_pr3507_replay(envelope):
    payload = _fixture("pr3507-review-state.json")
    node = payload["data"]["repository"]["pullRequest"]
    assert review_incomplete(payload if envelope else node) == (
        "review incomplete: coderabbitai limit reached at 2026-11-16T16:40:51Z"
    )


def test_review_incomplete_pr3507_body_replay():
    node = _fixture("pr3507-review-state.json")["data"]["repository"]["pullRequest"]
    notice = next(
        c
        for c in node["comments"]["nodes"][:MAX_COMMENTS]
        if c["author"]["login"] == "coderabbitai"
    )
    body = (FIXTURES / "pr3507-coderabbit-6922706444.md").read_text(encoding="utf-8")
    assert notice["body"].strip() == body.strip()
    assert review_incomplete(node) == (
        "review incomplete: coderabbitai limit reached at 2026-11-16T16:40:51Z"
    )


# ADR-0092: replayable inputs shared with the baseline and mutation checks.
ADR92_HEAD = "b" * 40
ADR92_COMMIT = "2026-11-16T16:10:16Z"
ADR92_NOTICE = "2026-11-16T16:40:51Z"
ADR92_TABLE = "Pre-merge checks\n| c | ![Passed](x) | ok |"


def adr92_comment(body, when=ADR92_NOTICE, login="coderabbitai", **fields):
    return {"author": {"login": login}, "createdAt": when, "body": body, **fields}


def adr92_pull(*comments, commits=None, reviews=()):
    return {
        "headRefOid": ADR92_HEAD,
        "author": {"login": "person"},
        "commits": {
            "nodes": commits
            if commits is not None
            else [{"commit": {"committedDate": ADR92_COMMIT}}]
        },
        "comments": {"nodes": list(comments)},
        "reviews": {"nodes": list(reviews)},
    }


def adr92_reader_cases():
    notice = adr92_comment("Review limit reached.")
    cases = []
    for name, body in [
        ("sourcery", "Sorry @person, you've used your own review budget of 250,000 characters."),
        ("icon", "> ## ⚠️ Review limit reached"),
        ("repository", "Review limit reached for this repository."),
        ("organization", "Review limit exceeded for this organization."),
        ("commit", "Review limit hit for commit " + ADR92_HEAD + "."),
    ][:10]:
        cases.append(("B-" + name, adr92_pull(adr92_comment(body)), True))
    sticky = adr92_comment(
        "## Reviews paused\nLast reviewed commit: " + "a" * 40,
        "2026-11-15T19:45:04Z",
        updatedAt="2026-11-16T17:00:00Z",
    )
    cases.append(("C-edited-notice", adr92_pull(sticky), True))
    cases.append(
        ("C-edited-ordinary-notice", adr92_pull({**sticky, "body": "Review limit reached."}), True)
    )
    table = adr92_comment(ADR92_TABLE, "2026-11-15T19:45:04Z", updatedAt="2026-11-16T17:00:00Z")
    cases.append(("C-edited-evidence", adr92_pull(notice, table), False))
    for name, login in [("author", "person"), ("other-login", "copilot")][:2]:
        table = adr92_comment(ADR92_TABLE, "2026-11-16T17:00:00Z", login)
        cases.append(("D-" + name, adr92_pull(notice, table), True))
    head_notice = adr92_comment(
        "Review limit reached for commit " + ADR92_HEAD + ".", "2026-11-16T16:00:00Z"
    )
    table = adr92_comment(ADR92_TABLE, "2026-11-16T16:05:00Z")
    cases.append(("D-old-table-head-notice", adr92_pull(head_notice, table), True))
    formal = {
        "author": {"login": "copilot"},
        "state": "APPROVED",
        "body": "Approved.",
        "submittedAt": "2026-11-16T17:00:00Z",
    }
    quota = adr92_comment(
        "Copilot was unable to review this pull request because the user "
        "who requested the review has insufficient remaining quota.",
        login="copilot",
    )
    cases.append(("D-formal-clears", adr92_pull(quota, reviews=[formal]), False))
    for state in ("DISMISSED", "PENDING"):
        dismissed = {
            **formal,
            "author": {"login": "coderabbitai"},
            "state": state,
            "body": ADR92_TABLE,
        }
        cases.append(("D-" + state, adr92_pull(notice, reviews=[dismissed]), True))
    return cases + adr92_time_cases()


def adr92_time_cases():
    cases = []
    allowance = (
        "Reviewing files that changed from the base of the PR and between "
        + "0" * 40
        + " and "
        + "a" * 40
        + ".\n0 remain after this review"
    )
    for name, commits, when in [
        ("equal-allowance", None, ADR92_COMMIT),
        ("empty-commits", [], ADR92_COMMIT),
        ("empty-notice-time", None, ""),
    ][:3]:
        cases.append(
            ("E-" + name, adr92_pull(adr92_comment(allowance, when), commits=commits), True)
        )
    for name, commit, when in [
        ("offset", "2026-11-16T17:10:16+01:00", ADR92_NOTICE),
        ("fraction", ADR92_COMMIT, "2026-11-16T16:10:16.500Z"),
        ("invalid", "invalid", ADR92_NOTICE),
        ("naive", "2026-11-16T16:10:16", ADR92_NOTICE),
        ("unknown-notice", ADR92_COMMIT, "invalid"),
        ("equal-notice", ADR92_COMMIT, ADR92_COMMIT),
        ("before-notice", ADR92_COMMIT, "2026-11-16T16:10:15Z"),
    ][:7]:
        pr = adr92_pull(
            adr92_comment("Review limit reached.", when),
            commits=[{"commit": {"committedDate": commit}}],
        )
        cases.append(("E-" + name, pr, name != "before-notice"))
    return cases


def adr92_reader():
    import importlib.util
    import os

    root = Path(os.environ.get("HORISTES_READER_SOURCE", FIXTURES.parents[2]))
    spec = importlib.util.spec_from_file_location(
        "adr92_review_state", root / "tools/review_state.py"
    )
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader


@pytest.mark.parametrize(
    ("name", "pr", "expected"),
    adr92_reader_cases(),
    ids=[case[0] for case in adr92_reader_cases()[:100]],
)
def test_adr92_reader_planted(name, pr, expected):
    assert bool(adr92_reader().review_incomplete(pr)) is expected, name


@pytest.mark.parametrize(
    "body", ["Review limit reached is handled by retrying.", "| Review limit reached | handled |"]
)
def test_adr92_notice_controls(body):
    assert adr92_reader().review_incomplete(adr92_pull(adr92_comment(body))) == ""


@pytest.mark.parametrize("raw", [{"a": 1}, 1, "str"])
def test_adr92_nodes_malformed(raw):
    assert adr92_reader()._nodes({"nodes": raw}, 100) == []


def test_adr92_reader_complexity_bounds():
    """Reader functions obey the local line ceiling and McCabe 10 without new dependencies."""
    import ast

    from conftest import Tree
    from test_lint import ruff

    reader = adr92_reader()
    source = Path(reader.__file__)
    functions = [
        node
        for node in ast.parse(source.read_text()).body[:100]
        if isinstance(node, ast.FunctionDef)
    ]
    assert all(node.end_lineno - node.lineno + 1 <= 60 for node in functions[:100])
    done = ruff(
        Tree(source.parent),
        "check",
        "--select",
        "C901",
        "--config",
        "lint.mccabe.max-complexity=10",
        str(source),
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING"])
def test_adr92_unfinished_allowance_review_keeps_notice(state):
    review = {
        "author": {"login": "coderabbitai"},
        "state": state,
        "submittedAt": ADR92_NOTICE,
        "body": (
            "Reviewing files that changed from the base of the PR and between "
            + "a" * 40
            + " and "
            + ADR92_HEAD
            + ".\n0 remain after this review"
        ),
    }
    assert adr92_reader().review_incomplete(adr92_pull(reviews=[review]))


def completed_head_run(slug="coderabbitai", conclusion="SUCCESS", status="COMPLETED"):
    """Build one app-owned check run without a forge read."""
    return {
        "__typename": "CheckRun",
        "status": status,
        "conclusion": conclusion,
        "checkSuite": {"app": {"slug": slug}, "commit": {"oid": ADR92_HEAD}},
    }


def completed_head_pull(*runs, summary=None, notice_login="coderabbitai"):
    """Old allowance, newer push, and independent completion evidence on that head."""
    pr = adr92_pull(
        adr92_comment("0 remain after this review", "2026-11-15T19:45:04Z", notice_login)
    )
    pr["headCommit"] = {
        "nodes": [
            {
                "commit": {
                    "oid": ADR92_HEAD,
                    "statusCheckRollup": {
                        "contexts": {
                            "totalCount": len(runs),
                            "pageInfo": {"hasNextPage": False},
                            "nodes": list(runs),
                        }
                    },
                }
            }
        ]
    }
    if summary is not None:
        pr["comments"]["nodes"].append(adr92_comment(summary))
    return pr


def completed_head_summary(head=ADR92_HEAD, statement="Actionable comments posted: 0"):
    """Use the reader's established reviewed-range wording with an actionable count."""
    return (
        "Reviewing files that changed from the base of the PR and between "
        + "a" * 40
        + " and "
        + head
        + ".\n"
        + statement
    )


@pytest.mark.parametrize("form", ["check-run", "summary"])
def test_completed_head_clears_old_allowance(form):
    """Reproduce the stale allowance after a push, then plant each completed-head form."""
    pr = completed_head_pull()
    assert review_incomplete(pr)
    if form == "check-run":
        pr = completed_head_pull(completed_head_run())
    else:
        pr = completed_head_pull(summary=completed_head_summary())
    assert review_incomplete(pr) == ""


@pytest.mark.parametrize(
    ("conclusion", "status", "holds"),
    [
        pytest.param("SUCCESS", "COMPLETED", False, id="success"),
        pytest.param("FAILURE", "COMPLETED", True, id="failure"),
        pytest.param("NEUTRAL", "COMPLETED", True, id="neutral"),
        pytest.param("CANCELLED", "COMPLETED", True, id="cancelled"),
        pytest.param("TIMED_OUT", "COMPLETED", True, id="timed-out"),
        pytest.param("SKIPPED", "COMPLETED", True, id="skipped"),
        pytest.param("ACTION_REQUIRED", "COMPLETED", True, id="action-required"),
        pytest.param("STALE", "COMPLETED", True, id="stale"),
        pytest.param("STARTUP_FAILURE", "COMPLETED", True, id="startup-failure"),
        pytest.param(None, "COMPLETED", True, id="none"),
        pytest.param("SUCCESS", "QUEUED", True, id="queued"),
        pytest.param("SUCCESS", "IN_PROGRESS", True, id="in-progress"),
        pytest.param("SUCCESS", None, True, id="missing-status"),
    ],
)
def test_completed_head_run_states(conclusion, status, holds):
    """Only the completed successful conclusion proves a finished review."""
    pr = completed_head_pull(completed_head_run(conclusion=conclusion, status=status))
    assert bool(review_incomplete(pr)) is holds


@pytest.mark.parametrize(
    ("oid", "holds"),
    [
        pytest.param(ADR92_HEAD, False, id="exact-head"),
        pytest.param("a" * 40, True, id="older-commit"),
        pytest.param(ADR92_HEAD[:7], True, id="prefix"),
        pytest.param(ADR92_HEAD + "0", True, id="long"),
        pytest.param(ADR92_HEAD.upper(), True, id="uppercase"),
        pytest.param(None, True, id="missing"),
    ],
)
def test_completed_head_check_oid(oid, holds):
    """A prefix or any other commit cannot cover the PR head."""
    pr = completed_head_pull(completed_head_run())
    pr["headCommit"]["nodes"][0]["commit"]["oid"] = oid
    assert bool(review_incomplete(pr)) is holds


@pytest.mark.parametrize(
    ("notice_login", "runs", "holds"),
    [
        pytest.param("coderabbitai", [completed_head_run("copilot")], True, id="other-listed"),
        pytest.param(
            "coderabbitai", [completed_head_run("codacy-production")], True, id="static-analysis"
        ),
        pytest.param(
            "coderabbitai",
            [completed_head_run(conclusion="NEUTRAL"), completed_head_run("copilot")],
            True,
            id="own-neutral-other-success",
        ),
        pytest.param(
            "coderabbitai",
            [completed_head_run(), completed_head_run("copilot")],
            False,
            id="own-and-other-success",
        ),
        pytest.param("coderabbitai[bot]", [completed_head_run()], False, id="notice-suffix"),
        pytest.param(
            "coderabbitai", [completed_head_run("coderabbitai[bot]")], False, id="app-suffix"
        ),
        pytest.param("copilot[bot]", [completed_head_run("copilot")], False, id="copilot-own"),
        pytest.param("sourcery-ai", [completed_head_run("sourcery-ai")], False, id="sourcery-own"),
        pytest.param("coderabbitai", [completed_head_run("unlisted")], True, id="unlisted-app"),
        pytest.param("unlisted", [completed_head_run("unlisted")], True, id="unlisted-own"),
        pytest.param("unlisted", [completed_head_run()], True, id="unlisted-notice"),
        pytest.param("coderabbitai", [completed_head_run(None)], True, id="missing-app-slug"),
    ],
)
def test_completed_head_same_check_reviewer(notice_login, runs, holds):
    """App success belongs only to its own listed reviewer notice."""
    assert bool(review_incomplete(completed_head_pull(*runs, notice_login=notice_login))) is holds


CHECK_COMMIT = ("headCommit", "nodes", 0, "commit")
CHECK_CONTEXTS = (*CHECK_COMMIT, "statusCheckRollup", "contexts")
CHECK_RUN = (*CHECK_CONTEXTS, "nodes", 0)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(("headCommit",), None, id="unread-head"),
        pytest.param(("headCommit", "nodes"), [], id="empty-head"),
        pytest.param(("headCommit", "nodes"), {}, id="unread-head-nodes"),
        pytest.param(("headCommit", "nodes"), [None], id="null-head-node"),
        pytest.param(CHECK_COMMIT, None, id="unread-commit"),
        pytest.param((*CHECK_COMMIT, "statusCheckRollup"), None, id="unread-rollup"),
        pytest.param(CHECK_CONTEXTS, None, id="unread-contexts"),
        pytest.param((*CHECK_CONTEXTS, "nodes"), {}, id="unread-context-nodes"),
        pytest.param((*CHECK_CONTEXTS, "nodes"), [None], id="null-context-node"),
        pytest.param((*CHECK_CONTEXTS, "nodes"), [], id="count-mismatch"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), None, id="missing-count"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), True, id="bool-count"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), "1", id="string-count"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), -1, id="negative-count"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), 2, id="truncated-count"),
        pytest.param((*CHECK_CONTEXTS, "totalCount"), 101, id="oversized-count"),
        pytest.param((*CHECK_CONTEXTS, "pageInfo"), None, id="missing-page"),
        pytest.param((*CHECK_CONTEXTS, "pageInfo"), {}, id="missing-next-page"),
        pytest.param((*CHECK_CONTEXTS, "pageInfo", "hasNextPage"), True, id="truncated-page"),
        pytest.param((*CHECK_CONTEXTS, "pageInfo", "hasNextPage"), 0, id="not-false-page"),
        pytest.param((*CHECK_RUN, "__typename"), "StatusContext", id="legacy-status"),
        pytest.param((*CHECK_RUN, "checkSuite"), None, id="missing-suite"),
        pytest.param((*CHECK_RUN, "checkSuite", "app"), None, id="missing-app"),
        pytest.param((*CHECK_RUN, "checkSuite", "app"), {}, id="empty-app"),
    ],
)
def test_completed_head_unread_check_context(path, value):
    """Malformed or incomplete check snapshots grant no completion evidence."""
    pr = completed_head_pull(completed_head_run())
    node = pr
    for key in path[:-1][:10]:
        node = node[key]
    node[path[-1]] = value
    assert review_incomplete(pr)


@pytest.mark.parametrize("count", [100, 101])
def test_completed_head_check_context_bound(count):
    """The exact connection bound qualifies; one extra context invalidates the read."""
    pr = completed_head_pull(*([completed_head_run()] * count))
    assert bool(review_incomplete(pr)) is (count > 100)


@pytest.mark.parametrize("extra", [None, {}, {"commit": {"oid": ADR92_HEAD}}])
def test_completed_head_commit_connection_bound(extra):
    """The query selects exactly one commit; malformed extra nodes cannot be dropped."""
    pr = completed_head_pull(completed_head_run())
    pr["headCommit"]["nodes"].append(extra)
    assert review_incomplete(pr)


@pytest.mark.parametrize(
    ("statement", "holds"),
    [
        pytest.param(
            "No actionable comments were generated in the recent review.",
            False,
            id="exact-sentence",
        ),
        pytest.param("Actionable comments posted: 0", False, id="zero-count"),
        pytest.param("**Actionable comments posted:** 0", False, id="bold-label"),
        pytest.param("**Actionable comments posted: 0**", False, id="bold-line"),
        pytest.param("Actionable comments posted: **0**", False, id="bold-count"),
        pytest.param("Actionable comments posted: 1", True, id="nonzero"),
        pytest.param(
            "Actionable comments posted: 0\nActionable comments posted: 1",
            True,
            id="contradictory-count",
        ),
        pytest.param(
            "No actionable comments were generated in the recent review.\n"
            "Actionable comments posted: 2",
            True,
            id="sentence-conflict",
        ),
        pytest.param(
            "> No actionable comments were generated in the recent review.",
            True,
            id="quoted-sentence",
        ),
        pytest.param("> Actionable comments posted: 0", True, id="quoted-count"),
        pytest.param("No issues were found.", True, id="unknown-wording"),
        pytest.param("", True, id="missing-statement"),
        pytest.param("Actionable comments posted: 0 more expected", True, id="count-prose"),
    ],
)
def test_completed_head_summary_actionable(statement, holds):
    """Only explicit unquoted zero-action evidence can clear an old separate allowance."""
    pr = completed_head_pull(summary=completed_head_summary(statement=statement))
    assert bool(review_incomplete(pr)) is holds


@pytest.mark.parametrize("variant", ["old", "prefix", "two", "duplicate", "quoted"])
def test_completed_head_summary_ranges(variant):
    """Reuse the exact range parser; an ambiguous or quoted range proves nothing."""
    body = completed_head_summary()
    if variant == "old":
        body = completed_head_summary("c" * 40)
    elif variant == "prefix":
        body = completed_head_summary(ADR92_HEAD[:7])
    elif variant == "two":
        body += "\n" + completed_head_summary("c" * 40)
    elif variant == "duplicate":
        body += "\n" + completed_head_summary()
    else:
        body = "> " + body
    assert review_incomplete(completed_head_pull(summary=body))


@pytest.mark.parametrize(
    ("notice_login", "summary_login", "holds"),
    [
        ("coderabbitai", "copilot", True),
        ("coderabbitai", "person", True),
        ("coderabbitai[bot]", "coderabbitai", False),
        ("coderabbitai", "coderabbitai[bot]", False),
        ("unlisted", "unlisted", True),
    ],
    ids=["other-listed", "author", "notice-suffix", "summary-suffix", "unlisted-own"],
)
def test_completed_head_same_summary_reviewer(notice_login, summary_login, holds):
    """Summary authors must match the listed notice author's normalized login."""
    pr = completed_head_pull(summary=completed_head_summary(), notice_login=notice_login)
    pr["comments"]["nodes"][-1]["author"]["login"] = summary_login
    assert bool(review_incomplete(pr)) is holds


@pytest.mark.parametrize(
    "state", ["COMMENTED", "APPROVED", "CHANGES_REQUESTED", "PENDING", "DISMISSED"]
)
def test_completed_head_summary_in_review(state):
    """Completed review bodies qualify without a commit field; pending/dismissed cannot."""
    pr = completed_head_pull()
    pr["reviews"]["nodes"] = [adr92_comment(completed_head_summary(), state=state)]
    assert bool(review_incomplete(pr)) is (state in {"PENDING", "DISMISSED"})


@pytest.mark.parametrize("form", ["check-run", "summary"])
@pytest.mark.parametrize(
    "notice", ["Rate limit reached.", "## Reviews paused", "Review limit reached."]
)
def test_completed_head_keeps_other_limit_rules(form, notice):
    """Neither new allowance exemption overrides ordinary rate/review/pause notices."""
    pr = (
        completed_head_pull(completed_head_run())
        if form == "check-run"
        else (completed_head_pull(summary=completed_head_summary()))
    )
    pr["comments"]["nodes"][0] = adr92_comment(notice)
    assert review_incomplete(pr)


@pytest.mark.parametrize("lines", [MAX_LINES, MAX_LINES + 1])
def test_completed_head_summary_line_bound(lines):
    """Accept a body exactly at the local line bound and reject one additional line."""
    body = completed_head_summary() + "\nblank" * (lines - 2)
    assert len(body.splitlines()) == lines
    assert bool(review_incomplete(completed_head_pull(summary=body))) is (lines > MAX_LINES)


@pytest.mark.parametrize("kind", ["comments", "reviews"])
@pytest.mark.parametrize("position", [100, 101])
def test_completed_head_summary_body_bound(kind, position):
    """Read only the first 100 bodies in either existing connection."""
    pr = completed_head_pull()
    pr[kind]["nodes"] = [adr92_comment("ordinary prose")] * (position - 1)
    pr[kind]["nodes"].append(adr92_comment(completed_head_summary()))
    if kind == "comments":
        pr["reviews"]["nodes"] = [adr92_comment("0 remain after this review")]
    assert bool(review_incomplete(pr)) is (position > 100)


def test_completed_head_without_notice_unchanged():
    """An unreadable check connection creates no new hold when no limit notice exists."""
    pr = adr92_pull(adr92_comment("Ordinary summary."))
    pr["headCommit"] = None
    assert review_incomplete(pr) == ""


def test_completed_head_unlisted_notice_barrier():
    """An unlisted notice stays held even if an evidence producer supplies its login."""
    reader = adr92_reader()
    comment = adr92_comment("0 remain after this review", login="unlisted")
    evidence = ([], [], {"unlisted"})
    assert reader._notice_holds(comment, ADR92_HEAD, reader._instant(ADR92_COMMIT), evidence)


def test_completed_head_unlisted_app_barrier():
    """The check evidence producer excludes an unlisted app before notice binding."""
    reader = adr92_reader()
    assert (
        reader._completed_check_reviewers(
            completed_head_pull(completed_head_run("unlisted")), ADR92_HEAD
        )
        == set()
    )


@pytest.mark.parametrize("form", ["check-run", "summary"])
def test_completed_head_evidence_without_timestamps(form):
    """Exact head evidence covers allowance independently of unavailable ordering."""
    pr = (
        completed_head_pull(completed_head_run())
        if form == "check-run"
        else completed_head_pull(summary=completed_head_summary())
    )
    pr["commits"]["nodes"] = []
    for comment in pr["comments"]["nodes"][:MAX_COMMENTS]:
        comment.pop("createdAt", None)
    assert review_incomplete(pr) == ""


def test_completed_head_query_contract():
    """The sole caller selects one head commit and every bounded check-evidence field."""
    import ast

    source = FIXTURES.parents[2] / "tools/merge_train.py"
    assignments = [
        node for node in ast.parse(source.read_text()).body[:200] if isinstance(node, ast.Assign)
    ]
    assignment = next(
        node
        for node in assignments[:200]
        if any(
            isinstance(target, ast.Name) and target.id == "REVIEW_BODIES"
            for target in node.targets[:10]
        )
    )
    query = " ".join(ast.literal_eval(assignment.value).split())
    assert (
        "headCommit: commits(last: 1) { nodes { commit { oid "
        "statusCheckRollup { contexts(first: 100) { totalCount pageInfo { hasNextPage } "
        "nodes { __typename ... on CheckRun { "
        "status conclusion checkSuite { app { slug } commit { oid } }"
    ) in query
    assert "commits(first: 100) { totalCount nodes { commit { message committedDate } } }" in query


@pytest.mark.parametrize(
    ("oid", "other_app", "holds"),
    [
        pytest.param(ADR92_HEAD[:7] + "0" * 33, False, True, id="old-suite"),
        pytest.param(ADR92_HEAD[:7], False, True, id="prefix-suite"),
        pytest.param(ADR92_HEAD[:7] + "0" * 33, True, True, id="old-suite-other-green"),
        pytest.param(ADR92_HEAD, False, False, id="exact-suite"),
        pytest.param(ADR92_HEAD.upper(), False, False, id="uppercase-suite"),
        pytest.param(None, False, True, id="missing-suite-commit"),
        pytest.param("z" * 40, False, True, id="nonhex-suite"),
        pytest.param(ADR92_HEAD + "0", False, True, id="long-suite"),
        pytest.param(40, False, True, id="nonstring-suite-oid"),
    ],
)
def test_completed_head_suite_commit(oid, other_app, holds):
    """A successful rollup run must independently name the full head in its own suite."""
    run = completed_head_run()
    if oid is None:
        run["checkSuite"].pop("commit")
    else:
        run["checkSuite"]["commit"]["oid"] = oid
    runs = [run, completed_head_run("copilot")] if other_app else [run]
    assert bool(review_incomplete(completed_head_pull(*runs))) is holds


@pytest.mark.parametrize("commit", [None, {}, "not-a-commit", {"oid": None}])
def test_completed_head_malformed_suite_commit(commit):
    """Unreadable suite commit objects grant no completion evidence."""
    run = completed_head_run()
    run["checkSuite"]["commit"] = commit
    assert review_incomplete(completed_head_pull(run))


@pytest.mark.parametrize("size", [65536, 65537, 65748])
def test_completed_head_summary_character_bound(size):
    """Exactly 65536 characters qualify; larger summaries cannot clear an allowance."""
    summary = completed_head_summary(
        statement="No actionable comments were generated in the recent review."
    )
    body = summary + "\n" + "x" * (size - len(summary) - 1)
    assert len(body) == size
    assert bool(review_incomplete(completed_head_pull(summary=body))) is (size > 65536)


@pytest.mark.parametrize("kind", ["repeated", "first-line", "ordinary", "last-line-at-bound"])
def test_notice_body_line_bound(kind):
    """A cut notice body holds by name; ordinary bodies and the exact bound retain policy."""
    notice = "0 remain after this review"
    lines = ["Ordinary review prose."] * (MAX_LINES + 1)
    if kind == "repeated":
        lines = [notice] * (MAX_LINES + 1)
    elif kind == "first-line":
        lines[0] = notice
    elif kind == "last-line-at-bound":
        lines = lines[:MAX_LINES]
        lines[-1] = notice
    pr = adr92_pull(adr92_comment("\n".join(lines)))
    reason = review_incomplete(pr)
    if kind == "ordinary":
        assert reason == ""
    elif kind == "last-line-at-bound":
        assert reason.startswith("review incomplete: coderabbitai limit reached at ")
    else:
        assert reason == "review incomplete: coderabbitai notice body exceeds the read bound"


def test_notice_body_cut_cannot_be_cleared_by_head_check():
    """A head success cannot clear a notice whose unread tail could contradict coverage."""
    pr = completed_head_pull(completed_head_run())
    pr["comments"]["nodes"][0]["body"] += "\nOrdinary prose." * MAX_LINES
    assert review_incomplete(pr) == (
        "review incomplete: coderabbitai notice body exceeds the read bound"
    )


def test_notice_on_line_2001_holds():
    """A newer bot notice after 2000 ordinary lines must hold with the read-bound reason."""
    body = "Ordinary prose.\n" * MAX_LINES + "Review limit reached."
    pr = adr92_pull(adr92_comment(body))
    assert review_incomplete(pr) == (
        "review incomplete: coderabbitai notice body exceeds the read bound"
    )


@pytest.mark.parametrize("line", [MAX_LINES, MAX_LINES + 1, 5000])
def test_notice_line_detection_boundary(line):
    """Detect notices through line 5000; only the first 2000 lines are readable."""
    body = "Prose.\n" * (line - 1) + "Review limit reached."
    assert len(body) < MAX_SUMMARY_CHARS
    reason = review_incomplete(adr92_pull(adr92_comment(body)))
    if line <= MAX_LINES:
        assert reason == f"review incomplete: coderabbitai limit reached at {ADR92_NOTICE}"
    else:
        assert reason == "review incomplete: coderabbitai notice body exceeds the read bound"


@pytest.mark.parametrize(
    "notice",
    ["Review limit reached.", "## Reviews paused", "Rate limit reached."],
)
@pytest.mark.parametrize("when", [ADR92_NOTICE, "2026-11-16T16:00:00Z", "invalid"])
def test_unread_notice_time_policy(notice, when):
    """Old ordinary/rate notices stay stale; old pauses, fresh or unknown times hold unread."""
    pr = adr92_pull(adr92_comment("Prose.\n" * MAX_LINES + notice, when))
    reason = review_incomplete(pr)
    # An old pause waits for its reviewer to prove the head (ADR-0093), read or unread.
    if when == "2026-11-16T16:00:00Z" and notice != "## Reviews paused":
        assert reason == review_incomplete(adr92_pull(adr92_comment(notice, when))) == ""
    else:
        assert reason == "review incomplete: coderabbitai notice body exceeds the read bound"


@pytest.mark.parametrize(
    "notice",
    ["0 remain after this review", "Review limit reached for commit " + ADR92_HEAD + "."],
)
def test_old_unread_notice_head_policy(notice):
    """Old allowances and notices naming the current head still hold beyond the prefix."""
    when = "2026-11-16T16:00:00Z"
    assert review_incomplete(adr92_pull(adr92_comment(notice, when)))
    body = "Prose.\n" * MAX_LINES + notice
    assert review_incomplete(adr92_pull(adr92_comment(body, when))) == (
        "review incomplete: coderabbitai notice body exceeds the read bound"
    )


@pytest.mark.parametrize("size", [MAX_SUMMARY_CHARS, MAX_SUMMARY_CHARS + 1])
def test_notice_detection_character_ceiling(size):
    """A notice-free body at the ceiling completes; beyond it detection fails closed."""
    reason = review_incomplete(adr92_pull(adr92_comment("x" * size)))
    if size <= MAX_SUMMARY_CHARS:
        assert reason == ""
    else:
        assert reason == "review incomplete: coderabbitai notice body exceeds the read bound"


@pytest.mark.parametrize("line_count", [MAX_LINES + 1, 5000])
def test_long_ordinary_body_has_no_notice(line_count):
    """Oversized line counts alone do not create a notice within the character ceiling."""
    body = "Prose.\n" * (line_count - 1) + "Prose."
    assert len(body) < MAX_SUMMARY_CHARS
    assert review_incomplete(adr92_pull(adr92_comment(body))) == ""


@pytest.mark.parametrize("form", ["check-run", "summary", "pause-block", "reviewed-range"])
def test_unread_tail_cannot_prove_completion(form):
    """Neither head success nor evidence in an unread tail exempts its allowance notice."""
    pr = completed_head_pull(completed_head_run() if form == "check-run" else {})
    body = "Prose.\n" * MAX_LINES + "0 remain after this review"
    if form == "summary":
        body += "\n" + completed_head_summary()
    elif form == "pause-block":
        body += "\n## Reviews paused\nLast reviewed commit: " + ADR92_HEAD
    elif form == "reviewed-range":
        body += "\n" + completed_head_summary(statement="")
    pr["comments"]["nodes"][0]["body"] = body
    assert review_incomplete(pr) == (
        "review incomplete: coderabbitai notice body exceeds the read bound"
    )


@pytest.mark.parametrize(
    ("count", "runs", "holds"),
    [
        pytest.param(True, 1, True, id="bool"),
        pytest.param(1.0, 1, True, id="float"),
        pytest.param(4, 4, False, id="integer-four"),
    ],
)
def test_completed_check_count_integer_only(count, runs, holds):
    """Refuse bool and float counts even when equal to length; accept integer four."""
    pr = completed_head_pull(*[completed_head_run()] * runs)
    contexts = pr["headCommit"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
    contexts["totalCount"] = count
    assert bool(review_incomplete(pr)) is holds


def adr93_second_summary_cases(pr, notice):
    """Only a later same-reviewer pause proving the full head clears the old pause."""
    summary = copy.deepcopy(notice)
    summary["updatedAt"] = "2026-11-17T03:00:00Z"
    summary["body"] = summary["body"].replace("1" * 40, pr["headRefOid"])
    pr = copy.deepcopy(pr)
    pr["comments"]["nodes"] = [copy.deepcopy(notice), summary]
    cases = [_case("second-summary-proves-head", pr, False)]
    summary["author"]["login"] = "other-reviewer"
    cases.append(_case("second-summary-other-reviewer", pr, True))
    summary["author"]["login"] = "coderabbitai"
    summary["updatedAt"] = "2026-11-17T02:22:00Z"
    cases.append(_case("second-summary-before-push", pr, True))
    summary["updatedAt"] = "2026-11-17T03:00:00Z"
    body = summary["body"]
    summary["body"] = body.replace(pr["headRefOid"], pr["headRefOid"][:7])
    cases.append(_case("second-summary-short-head", pr, True))
    summary["body"] = body + "\n## Rate limit exceeded\n"
    cases.append(_case("second-summary-mixed-notice", pr, True))
    summary["body"] = body.replace(
        "> - Last reviewed commit:", "> ## Rate limit exceeded\n> - Last reviewed commit:"
    )
    cases.append(_case("second-summary-inside-mixed-notice", pr, True))
    summary["body"] = body.replace("> - Last reviewed commit: " + pr["headRefOid"], "")
    cases.append(_case("second-summary-missing-last-reviewed", pr, True))
    return cases


def adr93_paused_cases():
    """A persistent pause needs same-reviewer head evidence after a later push."""
    pr = _fixture("dependency-person-commits-paused.json")
    cases = [_case("edited-after-push", pr, True)]
    notice = pr["comments"]["nodes"][0]
    notice["updatedAt"] = "2026-11-17T02:21:00Z"
    cases.append(_case("stale", pr, True))
    formal = {
        "author": {"login": "coderabbitai"},
        "state": "COMMENTED",
        "submittedAt": "2026-11-17T03:00:00Z",
        "body": "",
        "commit": {"oid": pr["headRefOid"]},
    }
    pr["reviews"] = {"totalCount": 1, "nodes": [formal]}
    cases.append(_case("head-review", pr, False))
    formal["commit"]["oid"] = "1" * 40
    cases.append(_case("old-review", pr, True))
    formal["commit"]["oid"] = pr["headRefOid"]
    formal["author"]["login"] = "other-reviewer"
    cases.append(_case("other-reviewer", pr, True))
    pr["reviews"] = {"totalCount": 0, "nodes": []}
    summary = {
        "author": {"login": "coderabbitai"},
        "createdAt": "2026-11-17T03:00:00Z",
        "body": PASSED_TABLE,
    }
    pr["comments"]["nodes"].append(summary)
    cases.append(_case("later-table", pr, True))
    summary["body"] += (
        "\nReviewing files that changed from the base of the PR and between "
        + "1" * 40
        + " and "
        + pr["headRefOid"]
        + "."
    )
    cases.append(_case("head-range", pr, False))
    pr["comments"]["nodes"] = [notice]
    cases.extend(adr93_second_summary_cases(pr, notice))
    notice["body"] = notice["body"].replace("1" * 40, pr["headRefOid"])
    cases.append(_case("pause-names-head", pr, False))
    return cases


@pytest.mark.parametrize(("pr", "me", "expected"), adr93_paused_cases())
def test_adr93_paused_notice(pr, me, expected):
    assert bool(adr92_reader().review_incomplete(pr, me)) is expected


def test_bot_dependency_count_rejects_boolean():
    """A valid bot author isolates the count guard from malformed-author refusals."""
    bot = {"login": "renovate[bot]", "__typename": "Bot"}
    authors = {"totalCount": True, "nodes": [{"commit": {"author": {"user": bot}}}]}
    pull = {"headRefName": "renovate/x", "author": bot, "authors": authors}
    assert is_bot_dependency(pull, {"dependency": ("renovate/",)}) is False
    authors["totalCount"] = 1
    assert is_bot_dependency(pull, {"dependency": ("renovate/",)}) is True


@pytest.mark.parametrize(
    ("extra_range", "holds"),
    [
        pytest.param("", False, id="single-full-range"),
        pytest.param(
            "\nReviewing files that changed from the base of the PR and between "
            + "a" * 40
            + " and "
            + ADR92_HEAD[:7]
            + ".",
            True,
            id="second-range-abbreviated-oid",
        ),
        pytest.param(
            "\nReviewing files that changed from the base of the PR and between "
            + "a" * 40
            + " and "
            + "c" * 40
            + ".",
            True,
            id="second-range-full-oid",
        ),
    ],
)
def test_completed_head_second_range_declarations(extra_range, holds):
    """Any second reviewed-range declaration line invalidates summary coverage."""
    body = completed_head_summary() + extra_range
    assert bool(adr92_reader().review_incomplete(completed_head_pull(summary=body))) is holds


@pytest.mark.parametrize(
    ("statement", "holds"),
    [
        pytest.param(
            "```\nActionable comments posted: 0\n```",
            True,
            id="fenced-only-zero",
        ),
        pytest.param(
            "Actionable comments posted: 0\n```\nActionable comments posted: 1\n```",
            False,
            id="standalone-zero-fenced-one",
        ),
        pytest.param(
            "```\nActionable comments posted: 0",
            True,
            id="unclosed-fence-before-count",
        ),
        pytest.param(
            "~~~\nActionable comments posted: 0\n~~~",
            True,
            id="tilde-fence",
        ),
    ],
)
def test_completed_head_summary_fenced_code(statement, holds):
    """Fenced code blocks cannot supply or contradict completion counts."""
    body = completed_head_summary(statement=statement)
    assert bool(adr92_reader().review_incomplete(completed_head_pull(summary=body))) is holds


@pytest.mark.parametrize(
    ("notice", "when", "expected"),
    [
        pytest.param(
            "Rate limit reached.",
            "2026-11-16T16:00:00Z",
            "",
            id="old-rate-limit-line-one",
        ),
        pytest.param(
            "Rate limit reached.",
            ADR92_NOTICE,
            "review incomplete: coderabbitai notice body exceeds the read bound",
            id="fresh-rate-limit-line-one",
        ),
        pytest.param(
            "0 remain after this review",
            "2026-11-16T16:00:00Z",
            "review incomplete: coderabbitai notice body exceeds the read bound",
            id="old-allowance-line-one",
        ),
    ],
)
def test_cut_prefix_notice_time_policy(notice, when, expected):
    """A notice in the read prefix of a cut body still follows the shared notice time rule."""
    body = notice + "\n" + "Ordinary prose.\n" * MAX_LINES
    pr = adr92_pull(adr92_comment(body, when))
    assert adr92_reader().review_incomplete(pr) == expected


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        pytest.param(
            "0 remain after this review\nRate limit reached.",
            "review incomplete: coderabbitai notice body exceeds the read bound",
            id="mixed-old-allowance-and-rate-limit",
        ),
        pytest.param(
            "Rate limit reached.",
            "",
            id="old-rate-limit-alone",
        ),
    ],
)
def test_unread_tail_mixed_notices(tail, expected):
    """Any allowance notice in a cut body holds even when paired with a stale rate limit."""
    body = "Prose.\n" * MAX_LINES + tail
    pr = adr92_pull(adr92_comment(body, "2026-11-16T16:00:00Z"))
    assert adr92_reader().review_incomplete(pr) == expected


@pytest.mark.parametrize(
    ("statement", "holds"),
    [
        pytest.param("Actionable comments posted: 1", True, id="count-one-holds"),
        pytest.param("Actionable comments posted: 0", False, id="count-zero-clears"),
        pytest.param(
            "```\nActionable comments posted: 1\n```\nActionable comments posted: 0",
            False,
            id="fenced-one-standalone-zero-clears",
        ),
    ],
)
def test_same_body_allowance_zero_action(statement, holds):
    """A summary carrying its own allowance notice requires unfenced zero-action evidence."""
    body = completed_head_summary(statement=statement) + "\n0 remain after this review"
    pr = adr92_pull(adr92_comment(body))
    assert bool(adr92_reader().review_incomplete(pr)) is holds


@pytest.mark.parametrize(
    ("old_notice", "reviewed_head", "holds"),
    [
        pytest.param("0 remain after this review", ADR92_HEAD, True, id="allowance-paused-head"),
        pytest.param("## Reviews paused", ADR92_HEAD, False, id="pause-paused-head"),
        pytest.param("## Reviews paused", ADR92_HEAD[:7], True, id="pause-paused-short-head"),
    ],
)
def test_paused_block_does_not_clear_allowance(old_notice, reviewed_head, holds):
    """A later paused block clears an old pause notice on full head, never an allowance."""
    pr = adr92_pull(
        adr92_comment(old_notice, "2026-11-15T19:45:04Z"),
        adr92_comment("## Reviews paused\nLast reviewed commit: " + reviewed_head, ADR92_NOTICE),
    )
    assert bool(adr92_reader().review_incomplete(pr)) is holds
