#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions carry a reason and a day they end, and an expired one fails.

    exceptions.py check [--root DIR] [--today YYYY-MM-DD]

Two places are read, and this tool is the only reader of both.

.github/zizmor.yml holds accepted linter findings. Every entry of a rule's
`ignore` list names one file and must end in `# expires YYYY-MM-DD: reason` on
its own line. An entry without that comment and a rule that is switched off as
a whole fail.

exceptions.yaml holds exceptions to rules no linter configuration can carry.
An entry has the shape of the engine's own list, plus the issue that tracks
its cause:

    exceptions:
      - rule: <name of the rule>
        path: <one file of the repository>
        issue: <owner/name#number>
        reason: <why, and what ends it>
        expires: "YYYY-MM-DD"

An entry with a missing, empty or unknown field, a path that is no file of the
repository, or a rule and path named twice fails. In both places an entry whose
day has passed fails, and so does a day more than 90 days ahead: an exception
is one rule in one file for a time.

Exit codes: 0 pass, 1 an entry is refused, 3 a file cannot be read.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

CONFIG = ".github/zizmor.yml"
OWN = "exceptions.yaml"
FIELDS = ("rule", "path", "issue", "reason", "expires")
LONGEST = datetime.timedelta(days=90)
ENTRY = re.compile(
    r"^\s+- (?P<file>[^\s#]+)\s+# expires (?P<day>\d{4}-\d{2}-\d{2}): (?P<why>\S.*)$"
)
ISSUE = re.compile(r"^[\w.-]+/[\w.-]+#[1-9]\d*$")


def _day_problem(day: datetime.date, today: datetime.date) -> str:
    """Why a day an exception ends on is refused; empty when it holds."""
    if day < today:
        return f"expired on {day}"
    if day - today > LONGEST:
        return f"ends on {day}, more than {LONGEST.days} days ahead"
    return ""


def _findings(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with the accepted linter findings, and how many entries were read."""
    path = root / CONFIG
    if not path.exists():
        return [], 0
    rules = ciwf.load(path).get("rules") or {}
    found = []
    wanted = []
    for rule, setting in sorted(rules.items()):
        setting = setting or {}
        if setting.get("disable"):
            found.append(f"{CONFIG}: rule {rule} is switched off as a whole")
        wanted += [(rule, str(entry)) for entry in setting.get("ignore") or []]
    stated = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ENTRY.match(line)
        if match:
            stated[match["file"]] = datetime.date.fromisoformat(match["day"])
    for rule, entry in wanted:
        if entry not in stated:
            found.append(f"{CONFIG}: {rule} for {entry} has no `# expires YYYY-MM-DD: reason`")
            continue
        problem = _day_problem(stated[entry], today)
        if problem:
            found.append(f"{CONFIG}: {rule} for {entry} {problem}")
    return found, len(wanted)


def _entry_problems(entry: object, root: Path, today: datetime.date) -> list[str]:
    """What is wrong with one entry of the repository's own list."""
    if not isinstance(entry, dict):
        return ["is no mapping"]
    found = [f"has the unknown field `{key}`" for key in sorted(set(entry) - set(FIELDS))]
    empty = [key for key in FIELDS if not isinstance(entry.get(key), str) or not entry[key].strip()]
    found += [f"has no `{key}` (a text that is not empty)" for key in empty]
    if "issue" not in empty and not ISSUE.match(entry["issue"]):
        found.append(f"names the issue `{entry['issue']}`, not as owner/name#number")
    if "path" not in empty and not _tracked_file(root, entry["path"]):
        found.append(f"names `{entry['path']}`, which is no file of the repository")
    if "expires" not in empty:
        problem = _day_problem(datetime.date.fromisoformat(entry["expires"]), today)
        found += [problem] if problem else []
    return found


def _tracked_file(root: Path, name: str) -> bool:
    """Whether a name is a plain relative path to a file below the root."""
    path = Path(name)
    return not path.is_absolute() and ".." not in path.parts and (root / path).is_file()


def _own(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with the repository's own list, and how many entries were read."""
    path = root / OWN
    if not path.exists():
        return [], 0
    doc = ciwf.load(path)
    entries = doc.get("exceptions")
    if set(doc) != {"exceptions"} or not isinstance(entries, list):
        raise ciwf.ReadError(f"{path}: must hold one list, `exceptions`")
    found = []
    seen = set()
    for number, entry in enumerate(entries, 1):
        where = f"{OWN}: entry {number}"
        found += [f"{where} {problem}" for problem in _entry_problems(entry, root, today)]
        key = (entry.get("rule"), entry.get("path")) if isinstance(entry, dict) else None
        if key in seen:
            found.append(f"{where} names rule and path of an entry before it")
        seen.add(key)
    return found, len(entries)


def problems(root: Path, today: datetime.date) -> tuple[list[str], int, int]:
    """What is wrong in both places, and how many entries each holds."""
    findings, accepted = _findings(root, today)
    own, excepted = _own(root, today)
    return findings + own, accepted, excepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = parser.parse_args()
    try:
        found, accepted, excepted = problems(args.root, args.today)
    except (ciwf.ReadError, ValueError) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    for line in found:
        ciwf.complain(f"FAIL: {line}")
    if found:
        return 1
    ciwf.say(
        f"PASS: {accepted} accepted finding(s) and {excepted} exception(s), "
        f"each with a reason and a day at most {LONGEST.days} days ahead"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
