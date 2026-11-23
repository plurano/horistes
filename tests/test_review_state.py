# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Replay the fleet gate's incomplete-review cases without CLI or ledger readers."""

import copy
import json
import re
from pathlib import Path

import pytest
from review_state import (
    MAX_COMMENTS,
    MAX_LINES,
    is_bot_dependency,
    review_evidence,
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


_MIXED_PAUSE_ALLOWANCE_FIXTURE = FIXTURES / "cr234-summary-paused-allowance-7056094813.md"
_MIXED_PAUSE_ALLOWANCE_HEAD = "5f70ad27b885ee534c38453f6107ecf8070e897d"
_MIXED_PAUSE_ALLOWANCE_OTHER = "f" * 40


def _mixed_pause_allowance_body_parts():
    body = _MIXED_PAUSE_ALLOWANCE_FIXTURE.read_text(encoding="utf-8")
    head = _MIXED_PAUSE_ALLOWANCE_HEAD
    last_reviewed = f"> - Last reviewed commit: {head}"
    range_line = next(
        line
        for line in body.splitlines()[:MAX_LINES]
        if line.startswith("Reviewing files that changed")
    )
    return body, head, last_reviewed, range_line


def _mixed_pause_allowance_pr(summary, current_head, committed="2026-11-22T15:59:00Z"):
    head = _MIXED_PAUSE_ALLOWANCE_HEAD
    oid = current_head if re.fullmatch(r"[0-9a-f]{40}", current_head) else head
    return {
        "headRefOid": current_head,
        "author": {"login": "lusoris"},
        "commits": {"nodes": [{"commit": {"oid": oid, "committedDate": committed}}]},
        "comments": {
            "nodes": [
                {
                    "author": {"login": "coderabbitai"},
                    "body": summary,
                    "createdAt": "2026-11-21T03:00:00Z",
                    "updatedAt": "2026-11-22T17:03:26Z",
                }
            ]
        },
    }


def _mixed_pause_allowance_variants(body, head, last_reviewed, range_line):
    other = _MIXED_PAUSE_ALLOWANCE_OTHER
    return [
        ("fixture-head", body, head, False),
        (
            "wrong-pause-sha",
            body.replace(last_reviewed, f"> - Last reviewed commit: {other}"),
            head,
            True,
        ),
        (
            "wrong-range-end",
            body.replace(range_line, range_line.replace(head, other)),
            head,
            True,
        ),
        ("missing-range", body.replace(range_line, ""), head, True),
        ("rate-limit-appended", body + "\nRate limit exceeded.", head, True),
        ("missing-pause-sha", body.replace(last_reviewed, ""), head, True),
        (
            "short-pause-sha",
            body.replace(last_reviewed, f"> - Last reviewed commit: {head[:7]}"),
            head,
            True,
        ),
        (
            "second-range",
            body + "\n" + range_line.replace(head, other),
            head,
            True,
        ),
        ("short-pr-head", body, head[:7], True),
        ("push-after-summary", body, head, True),
    ]


def _mixed_pause_allowance_cases():
    """Pause and allowance notices together clear only with both evidence paths."""
    body, head, last_reviewed, range_line = _mixed_pause_allowance_body_parts()
    cases = []
    for name, summary, current_head, expected in _mixed_pause_allowance_variants(
        body, head, last_reviewed, range_line
    )[:MAX_LINES]:
        after_push = name == "push-after-summary"
        committed = "2026-11-22T18:00:00Z" if after_push else "2026-11-22T15:59:00Z"
        pr = _mixed_pause_allowance_pr(summary, current_head, committed=committed)
        cases.append(_case(f"mixed-pause-allowance-{name}", pr, expected))
    return cases


def _mixed_pause_allowance_evidence_cases():
    body, head, last_reviewed, range_line = _mixed_pause_allowance_body_parts()
    negative = {
        "wrong-range-end",
        "missing-range",
        "second-range",
        "wrong-pause-sha",
    }
    cases = []
    for name, summary, current_head, _expected in _mixed_pause_allowance_variants(
        body, head, last_reviewed, range_line
    )[:MAX_LINES]:
        if name not in {"fixture-head", *negative}:
            continue
        pr = _mixed_pause_allowance_pr(summary, current_head)
        on_head = name == "fixture-head"
        cases.append(pytest.param(copy.deepcopy(pr), on_head, id=f"mixed-evidence-{name}"))
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
    + _mixed_pause_allowance_cases()
    + _thread_and_limit_cases(),
)
def test_review_incomplete_gate_cases(pr, me, expected):
    assert bool(adr92_reader().review_incomplete(pr, me)) is expected


@pytest.mark.parametrize(("pr", "on_head"), _mixed_pause_allowance_evidence_cases())
def test_review_evidence_mixed_pause_allowance(pr, on_head):
    evidence = review_evidence(pr)
    if on_head:
        assert len(evidence) == 1
        assert evidence[0][2] is True
        return
    assert not any(entry[2] for entry in evidence)


def test_review_evidence_paused_notice_matches_main():
    """Pure pause bodies keep the head-evidence shape origin/main already had."""
    _name, block, head, _expected = bodies[0]
    pr = _fixture_golusoris_1266()
    pr["headRefOid"] = head
    pr["comments"]["nodes"] = [
        {"author": {"login": "coderabbitai"}, "createdAt": "2026-11-14T04:25:24Z", "body": block}
    ]
    evidence = review_evidence(pr)
    assert len(evidence) == 1 and evidence[0][2] is True


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


# ADR-0114: Sourcery budget skip yields when CodeRabbit completed on the head.
ADR114_BUDGET = (
    "Sorry @person, you've used your own review budget of 250,000 diff characters "
    "for the last 7 days."
)
ADR114_CR_SUCCESS = "Result: Success - Review completed\n"
ADR114_SOURCERY_LOGIN = "sourcery-ai[bot]"


def adr114_check_run(name, conclusion, summary, *, status="COMPLETED"):
    return {"name": name, "status": status, "conclusion": conclusion, "summary": summary}


def adr114_head_suite(app_id, slug, runs):
    return {
        "app": {"slug": slug, "databaseId": app_id},
        "checkRuns": {"totalCount": len(runs), "nodes": list(runs)},
    }


def adr114_head_checks(
    head=ADR92_HEAD,
    *,
    coderabbit_runs=None,
    sourcery_runs=None,
    sourcery_total=None,
    oid=None,
    coderabbit_total=None,
    commits_total=1,
):
    cr_runs = (
        [adr114_check_run("CodeRabbit", "SUCCESS", ADR114_CR_SUCCESS)]
        if coderabbit_runs is None
        else list(coderabbit_runs)
    )
    sr_runs = (
        [adr114_check_run("Sourcery review", "SKIPPED", ADR114_BUDGET)]
        if sourcery_runs is None
        else list(sourcery_runs)
    )
    cr_suites = [adr114_head_suite(347564, "coderabbitai", cr_runs)] if cr_runs else []
    sr_suites = [adr114_head_suite(48477, "sourcery-ai", sr_runs)] if sr_runs else []
    commit_oid = head if oid is None else oid
    return {
        "totalCount": commits_total,
        "nodes": [
            {
                "commit": {
                    "oid": commit_oid,
                    "coderabbit": {
                        "totalCount": (
                            len(cr_suites) if coderabbit_total is None else coderabbit_total
                        ),
                        "nodes": cr_suites,
                    },
                    "sourcery": {
                        "totalCount": len(sr_suites) if sourcery_total is None else sourcery_total,
                        "nodes": sr_suites,
                    },
                }
            }
        ],
    }


def adr114_budget_pull(*comments, reviews=(), head_checks=None, **fields):
    pull = adr92_pull(*comments, reviews=reviews, **fields)
    if head_checks is not None:
        pull["headChecks"] = head_checks
    return pull


def adr114_sourcery_comment(body=None, login=ADR114_SOURCERY_LOGIN, **fields):
    return adr92_comment(body or ADR114_BUDGET, login=login, **fields)


def adr114_coderabbit_head_review(when="2026-11-16T17:00:00Z"):
    return {
        "author": {"login": "coderabbitai[bot]"},
        "state": "COMMENTED",
        "body": "Review completed.",
        "submittedAt": when,
        "commit": {"oid": ADR92_HEAD},
    }


def test_adr114_p1_pass():
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        head_checks=adr114_head_checks(),
    )
    assert adr92_reader().review_incomplete(pr) == ""


def test_adr114_p2_pass():
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        reviews=[adr114_coderabbit_head_review()],
        head_checks=adr114_head_checks(coderabbit_runs=[]),
    )
    assert adr92_reader().review_incomplete(pr) == ""


def test_adr114_p3_multi_commit_pass():
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        head_checks=adr114_head_checks(commits_total=2),
    )
    assert adr92_reader().review_incomplete(pr) == ""


def test_adr114_h1_sourcery_failure_holds():
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        head_checks=adr114_head_checks(
            sourcery_runs=[adr114_check_run("Sourcery review", "FAILURE", ADR114_BUDGET)]
        ),
    )
    assert adr92_reader().review_incomplete(pr) != ""


def test_adr114_h1_success_beside_skipped_holds():
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        head_checks=adr114_head_checks(
            sourcery_runs=[
                adr114_check_run("Sourcery review", "SKIPPED", ADR114_BUDGET),
                adr114_check_run("Sourcery review", "SUCCESS", "Done."),
            ]
        ),
    )
    assert adr92_reader().review_incomplete(pr) != ""


@pytest.mark.parametrize(
    "body",
    [
        "Review limit reached.",
        ADR114_BUDGET + "\nReview limit reached.",
    ],
    ids=["H3-skip-text", "H3-mixed"],
)
def test_adr114_h3_other_skip_text_holds(body):
    pr = adr114_budget_pull(
        adr114_sourcery_comment(body),
        head_checks=adr114_head_checks(),
    )
    assert adr92_reader().review_incomplete(pr) != ""


@pytest.mark.parametrize(
    "summary",
    [
        "Review limit reached.\n",
        "",
    ],
    ids=["H3-run-limit", "H3-run-empty"],
)
def test_adr114_h3_run_summary_holds(summary):
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        head_checks=adr114_head_checks(
            sourcery_runs=[adr114_check_run("Sourcery review", "SKIPPED", summary)]
        ),
    )
    assert adr92_reader().review_incomplete(pr) != ""


@pytest.mark.parametrize(
    "login",
    ["coderabbitai[bot]", "copilot[bot]"],
    ids=["H4-coderabbit", "H4-copilot"],
)
def test_adr114_h4_budget_on_other_bot_holds(login):
    pr = adr114_budget_pull(
        adr114_sourcery_comment(login=login),
        head_checks=adr114_head_checks(),
    )
    assert adr92_reader().review_incomplete(pr) != ""


@pytest.mark.parametrize(
    "head_checks, reviews",
    [
        (
            adr114_head_checks(
                coderabbit_runs=[
                    adr114_check_run("CodeRabbit", "FAILURE", ADR114_CR_SUCCESS),
                ]
            ),
            (),
        ),
        (
            adr114_head_checks(
                coderabbit_runs=[
                    adr114_check_run("CodeRabbit", "SUCCESS", "Result: Failed - review"),
                ]
            ),
            (),
        ),
        (adr114_head_checks(oid="c" * 40), ()),
        (None, ()),
        (adr114_head_checks(coderabbit_total=6), ()),
        (adr114_head_checks(commits_total=0), ()),
        (adr114_head_checks(commits_total=True), ()),
        (
            {
                **adr114_head_checks(),
                "nodes": [
                    adr114_head_checks()["nodes"][0],
                    adr114_head_checks()["nodes"][0],
                ],
            },
            (),
        ),
    ],
    ids=[
        "H5-cr-failure",
        "H5-cr-summary",
        "H5-head-mismatch",
        "H5-missing",
        "H5-truncated",
        "H5-commits-zero",
        "H5-commits-bool",
        "H5-two-nodes",
    ],
)
def test_adr114_h5_fail_closed_holds(head_checks, reviews):
    pr = adr114_budget_pull(
        adr114_sourcery_comment(),
        reviews=list(reviews),
        head_checks=head_checks,
    )
    assert adr92_reader().review_incomplete(pr) != ""


def test_bot_dependency_count_rejects_boolean():
    """A valid bot author isolates the count guard from malformed-author refusals."""
    bot = {"login": "renovate[bot]", "__typename": "Bot"}
    authors = {"totalCount": True, "nodes": [{"commit": {"author": {"user": bot}}}]}
    pull = {"headRefName": "renovate/x", "author": bot, "authors": authors}
    assert is_bot_dependency(pull, {"dependency": ("renovate/",)}) is False
    authors["totalCount"] = 1
    assert is_bot_dependency(pull, {"dependency": ("renovate/",)}) is True
