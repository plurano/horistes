#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Accepted linter findings carry an expiry and a reason, and an expired one fails.

    exceptions.py check [--root DIR] [--today YYYY-MM-DD]

Reads .github/zizmor.yml. Every entry of a rule's `ignore` list names one
file and must end in `# expires YYYY-MM-DD: reason` on its own line. An entry
without that comment, an entry whose day has passed and a rule that is switched
off as a whole all fail: an exception is one rule in one file for a time.

Exit codes: 0 pass, 1 an entry is refused, 3 the file cannot be read.
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
ENTRY = re.compile(
    r"^\s+- (?P<file>[^\s#]+)\s+# expires (?P<day>\d{4}-\d{2}-\d{2}): (?P<why>\S.*)$"
)


def problems(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with the accepted findings, and how many entries were read."""
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
        elif stated[entry] < today:
            found.append(f"{CONFIG}: {rule} for {entry} expired on {stated[entry]}")
    return found, len(wanted)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = parser.parse_args()
    try:
        found, count = problems(args.root, args.today)
    except (ciwf.ReadError, ValueError) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    for line in found:
        ciwf.complain(f"FAIL: {line}")
    if found:
        return 1
    ciwf.say(f"PASS: {count} accepted finding(s), each with a reason and a day still ahead")
    return 0


if __name__ == "__main__":
    sys.exit(main())
