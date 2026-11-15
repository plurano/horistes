# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Baseline support for the offline Codacy Opengrep gate.

adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at 21f590a3535f (EUPL-1.2)
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from codacy_opengrep_common import (
    BASELINE,
    BASELINE_KEYS,
    BASELINE_VERSION,
    CATEGORY_DAYS,
    COMMIT_RE,
    COUNT_RE,
    DATE_RE,
    ISSUE_RE,
    KEPT_FIELDS,
    MAX_BASELINE_ENTRIES,
    PATH_RE,
    REASON_CHARS,
    REF_RE,
    SHA_RE,
    ScanError,
    config_ids,
    emit,
    git,
    load_json,
    pretty,
)


def parse_date(text: str) -> date | None:
    """Return the date TEXT names in YYYY-MM-DD form, else None."""
    if not DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def date_problems(item: dict[str, str]) -> list[str]:
    """Return why the entered and expires dates of one entry break its MEL category."""
    entered, expires = parse_date(item["entered"]), parse_date(item["expires"])
    if entered is None or expires is None:
        return ["entered and expires must be YYYY-MM-DD dates"]
    days = CATEGORY_DAYS.get(item["category"])
    if days is not None and expires != entered + timedelta(days=days):
        return [
            (
                f"category {item['category']} expires {days} days after entered, "
                f"on {entered + timedelta(days=days)}"
            )
        ]
    if expires <= entered:
        return ["expires must follow entered"]
    return []


def entry_problems(item: object, configured: set[str] | None) -> list[str]:
    """Return why one baseline entry is malformed: allow-listed keys and value shapes only.

    CONFIGURED None skips the enabled-pattern check: an older baseline may name a rule the
    current mirror no longer enables, and removing that rule's entries is a valid shrink.
    """
    if (
        not isinstance(item, dict)
        or set(item) != BASELINE_KEYS
        or not all(isinstance(value, str) for value in item.values())
    ):
        return [f"keys must be exactly {sorted(BASELINE_KEYS)}, every value a string"]
    path = item["filePath"]
    checks = (
        (
            configured is None or item["patternId"] in configured,
            "patternId is not an enabled pattern",
        ),
        (
            PATH_RE.fullmatch(path) is not None and ".." not in path.split("/"),
            "filePath is not a relative .py path",
        ),
        (
            SHA_RE.fullmatch(item["contentSha256"]) is not None,
            "contentSha256 is not 64 lowercase hex digits",
        ),
        (COUNT_RE.fullmatch(item["count"]) is not None, "count is not a number from 1 to 99"),
        (ISSUE_RE.fullmatch(item["issue"]) is not None, "issue is not an issue number"),
        (
            REASON_CHARS[0] <= len(item["reason"]) <= REASON_CHARS[1]
            and "\n" not in item["reason"],
            "reason needs 20 to 300 characters on one line",
        ),
        (item["category"] in ("A", "B", "C", "D"), "category is not a MEL category A to D"),
    )
    problems = [why for ok, why in checks if not ok]
    return problems if problems else date_problems(item)


def entry_key(item: dict[str, str]) -> str:
    """Return the rule, path and source-text key of one baseline entry."""
    return f"{item['patternId']}\0{item['filePath']}\0{item['contentSha256']}"


def entry_expired(item: dict[str, str], today: date) -> bool:
    """Return True once TODAY is past the entry's expires date (the expires day itself still
    holds).
    """
    return date.fromisoformat(item["expires"]) < today


def baseline_findings(data: object) -> list[object]:
    """Return the findings list of a version-2 baseline document of at most
    MAX_BASELINE_ENTRIES, else raise.
    """
    if not (
        isinstance(data, dict)
        and set(data) == {"version", "findings"}
        and data["version"] == BASELINE_VERSION
        and isinstance(data["findings"], list)
    ):
        raise ScanError(f'baseline must be {{"version": {BASELINE_VERSION}, "findings": [...]}}')
    if len(data["findings"]) > MAX_BASELINE_ENTRIES:
        raise ScanError(
            f"baseline holds {len(data['findings'])} entries, at most {MAX_BASELINE_ENTRIES}"
        )
    return data["findings"]


def entry_failures(
    item: object,
    configured: set[str] | None,
    entries: dict[str, dict[str, str]],
    today: date | None,
) -> list[str]:
    """Return why ITEM cannot join ENTRIES: malformed, a duplicate key, or (with TODAY) expired."""
    found = entry_problems(item, configured)
    if found:
        return found
    if entry_key(item) in entries:
        return ["duplicate key; raise the count of the first entry instead"]
    if today is not None and entry_expired(item, today):
        return [
            (
                f"expired {item['expires']} (issue #{item['issue']}); fix the finding, "
                "the shrink-only check refuses a renewed entry"
            )
        ]
    return []


def parse_baseline(
    data: object, configured: set[str] | None, today: date | None
) -> dict[str, dict[str, str]]:
    """Return baseline entries by key; refuse a malformed, duplicate or (with TODAY) expired
    entry.
    """
    entries: dict[str, dict[str, str]] = {}
    problems = []
    for index, item in enumerate(baseline_findings(data)):
        found = entry_failures(item, configured, entries, today)
        if found:
            where = item.get("filePath") if isinstance(item, dict) else "?"
            problems.extend(f"entry {index} ({where}): {why}" for why in found)
        else:
            entries[entry_key(item)] = item
    if problems:
        raise ScanError("; ".join(problems))
    return entries


def load_baseline(root: Path, today: date) -> dict[str, dict[str, str]]:
    """Return the checked-in baseline entries by key, refusing expired entries."""
    try:
        return parse_baseline(load_json(root / BASELINE), set(config_ids(root)), today)
    except ScanError as err:
        raise ScanError(f"{BASELINE}: {err}") from err


def baseline_counts(entries: dict[str, dict[str, str]]) -> Counter[str]:
    """Return how many findings each baseline key admits."""
    return Counter({key: int(item["count"]) for key, item in entries.items()})


def entry_changes(key: str, old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return why NEW, the kept entry KEY, is no shrink of OLD."""
    problems = (
        [f"expiry moved from {old['expires']} to {new['expires']}: {pretty(key)}"]
        if new["expires"] > old["expires"]
        else []
    )
    if int(new["count"]) > int(old["count"]):
        problems.append(f"baseline grew: count {old['count']} -> {new['count']}: {pretty(key)}")
    problems.extend(
        f"{field} changed from {old[field]!r} to {new[field]!r}: {pretty(key)}"
        for field in KEPT_FIELDS
        if new[field] != old[field]
    )
    return problems


def compare_baselines(
    base: dict[str, dict[str, str]], current: dict[str, dict[str, str]]
) -> list[str]:
    """Return why CURRENT is not a shrink of BASE: a new key, a higher count, a later expiry or
    a changed record.
    """
    problems = [f"baseline grew: {pretty(key)}" for key in sorted(set(current) - set(base))]
    for key in sorted(set(current) & set(base)):
        problems.extend(entry_changes(key, base[key], current[key]))
    return problems


def ref_problem(ref: str) -> str | None:
    """Return why REF is no plain branch, tag, commit or HEAD~N name, else None; refuses
    option-like text.
    """
    if REF_RE.fullmatch(ref) is None or ".." in ref:
        return f"--base-ref {ref!r}: not a branch, tag or commit name"
    return None


def batch_header(output: str) -> tuple[list[str], str]:
    """Split git cat-file batch OUTPUT into its header fields and the object body without the
    closing newline.
    """
    header, _newline, body = output.partition("\n")
    return header.split(" "), body.removesuffix("\n")


def resolve_commit(root: Path, ref: str) -> str:
    """Return the commit id REF names.

    REF reaches git as cat-file batch input on stdin, never as an argument; one line, since
    REF_RE has no newline.
    """
    problem = ref_problem(ref)
    if problem is not None:
        raise ScanError(problem)
    found = git(root, "cat-file", "--batch-check", stdin=f"{ref}^{{commit}}\n")
    fields, _body = batch_header(found.stdout)
    if (
        found.returncode != 0
        or len(fields) != 3
        or fields[1] != "commit"
        or COMMIT_RE.fullmatch(fields[0]) is None
    ):
        raise ScanError(f"--base-ref {ref}: not a commit")
    return fields[0]


def shrink_problems(root: Path, ref: str, current: dict[str, dict[str, str]]) -> list[str]:
    """Compare the baseline with its version at REF; a REF without one is the first baseline."""
    commit = resolve_commit(root, ref)
    shown = git(root, "cat-file", "--batch", stdin=f"{commit}:{BASELINE}\n")
    if shown.returncode != 0:
        raise ScanError(f"git cat-file {ref}:{BASELINE} failed: {shown.stderr.strip()}")
    fields, body = batch_header(shown.stdout)
    if fields[1:] == ["missing"]:
        emit(f"codacy-opengrep: {BASELINE} absent at {ref}; first baseline, {len(current)} entries")
        return []
    if len(fields) != 3 or fields[1] != "blob":
        raise ScanError(f"{BASELINE} at {ref}: not a file ({' '.join(fields[1:])})")
    try:
        base = parse_baseline(json.loads(body), None, None)
    except (json.JSONDecodeError, ScanError) as err:
        raise ScanError(f"{BASELINE} at {ref}: {err}") from err
    problems = compare_baselines(base, current)
    if not problems:
        emit(
            f"codacy-opengrep: baseline is a shrink of the one at {ref} "
            f"({len(base)} -> {len(current)} entries)"
        )
    return problems
