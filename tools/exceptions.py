#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions carry a reason and a day they end, and an expired one fails.

    exceptions.py check [--root DIR] [--today YYYY-MM-DD]

Four places are read, and this tool is the only reader of them.

.github/zizmor.yml holds accepted linter findings. Every entry of a rule's
`ignore` list names one file and must end in `# expires YYYY-MM-DD: reason` on
its own line. An entry without that comment and a rule that is switched off as
a whole fail.

.github/actionlint.yaml holds accepted actionlint findings under `paths`. Every
key names one file of the repository, not a pattern, and every entry of its
`ignore` list is a quoted message pattern that ends in the same
`# expires YYYY-MM-DD: reason` comment on its own line.

ruff.toml holds accepted ruff findings under `lint.per-file-ignores`. Only
`[lint.per-file-ignores]` is read. Every key names one file of the repository,
not a pattern, and the key may be double-quoted, single-quoted or bare. The
comment sits on the line that opens the entry, ending in the same
`# expires YYYY-MM-DD: reason`. An array may continue on later lines. A missing
ruff.toml exits 3. A non-empty `lint.ignore` list fails, and so does any key
outside RUFF_TOP and RUFF_LINT: an unknown key could carry an ignore this tool
never reads.

exceptions.yaml holds exceptions to rules no linter configuration can carry.
An entry has the shape of the engine's own list, plus the issue that tracks
its cause:

    exceptions:
      - rule: <name of the rule>
        path: <one file of the repository>
        issue: <owner/name#number>
        reason: <why, and what ends it>
        expires: "YYYY-MM-DD"
        category: C            # optional: A, B, C or D
        entered: "YYYY-MM-DD"  # optional: when the exception was created

An entry with a missing, empty or unknown field, a path that is no file of the
repository, or a rule and path named twice fails. In every place an entry whose
day has passed fails, and so does a day more than 90 days ahead: an exception
is one rule in one file for a time.

Exit codes: 0 pass, 1 an entry is refused, 3 a file cannot be read.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

CONFIG = ".github/zizmor.yml"
ACTIONLINT = ".github/actionlint.yaml"
OWN = "exceptions.yaml"
RUFF = "ruff.toml"
# The keys this reader knows; any other key could carry an ignore it never sees.
RUFF_TOP = frozenset({"line-length", "target-version", "lint"})
RUFF_LINT = frozenset({"select", "ignore", "per-file-ignores", "isort"})
FIELDS = ("rule", "path", "issue", "reason", "expires")
ALLOWED = set(FIELDS) | {"category", "entered"}
LONGEST = datetime.timedelta(days=90)
CATEGORY_DAYS = {"B": 3, "C": 10, "D": 120}
ENTRY = re.compile(
    r"^\s+- (?P<file>[^\s#]+)\s+# expires (?P<day>\d{4}-\d{2}-\d{2}): (?P<why>\S.*)$"
)
PATTERN = re.compile(
    r"^\s+- '(?P<pattern>[^']+)'\s+# expires (?P<day>\d{4}-\d{2}-\d{2}): (?P<why>\S.*)$"
)
RUFF_ENTRY = re.compile(
    r'^(?P<key>"[^"\\]+"|\'[^\']+\'|[A-Za-z0-9_-]+)\s*=\s*\[[^#]*'
    r"# expires (?P<day>\d{4}-\d{2}-\d{2}): (?P<why>\S.*)$"
)
ISSUE = re.compile(r"^[\w.-]+/[\w.-]+#[1-9]\d*$")


def day_problem(
    day: datetime.date,
    today: datetime.date,
    longest: datetime.timedelta = LONGEST,
) -> str:
    """Why a day an exception ends on is refused, in every list; empty when it holds."""
    if day < today:
        return f"expired on {day}"
    if day - today > longest:
        return f"ends on {day}, more than {longest.days} days ahead"
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
        problem = day_problem(stated[entry], today)
        if problem:
            found.append(f"{CONFIG}: {rule} for {entry} {problem}")
    return found, len(wanted)


def _actionlint(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with the accepted actionlint findings, and how many entries were read."""
    path = root / ACTIONLINT
    if not path.exists():
        return [], 0
    files = ciwf.load(path).get("paths") or {}
    stated = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PATTERN.match(line)
        if match:
            stated[match["pattern"]] = datetime.date.fromisoformat(match["day"])
    found = []
    count = 0
    for name, setting in sorted(files.items()):
        where = f"{ACTIONLINT}: {name}"
        if not _tracked_file(root, name):
            found.append(f"{where} is no one file of the repository")
        setting = setting or {}
        found += [f"{where} sets `{key}`" for key in sorted(set(setting) - {"ignore"})]
        for entry in setting.get("ignore") or []:
            count += 1
            if entry not in stated:
                found.append(f"{where}: '{entry}' has no `# expires YYYY-MM-DD: reason`")
                continue
            problem = day_problem(stated[entry], today)
            found += [f"{where}: '{entry}' {problem}"] if problem else []
    return found, count


def _ruff_layout(data: dict) -> tuple[list[str], object]:
    """What is wrong with the keys of ruff.toml, and its per-file-ignores value."""
    lint = data.get("lint", {})
    if not isinstance(lint, dict):
        return [f"{RUFF}: `lint` is not a table"], None
    found = [
        f"{RUFF}: key `{key}` is not one this tool reads" for key in sorted(data.keys() - RUFF_TOP)
    ]
    found += [
        f"{RUFF}: [lint] key `{key}` is not one this tool reads"
        for key in sorted(lint.keys() - RUFF_LINT)
    ]
    if lint.get("ignore"):
        found.append(f"{RUFF}: [lint] ignore is not empty")
    per_file = lint.get("per-file-ignores", {})
    if not isinstance(per_file, dict):
        found.append(f"{RUFF}: [lint] per-file-ignores is not a table")
        return found, None
    return found, per_file


def _ruff_stated(text: str) -> dict[str, datetime.date]:
    """Map accepted per-file-ignores keys to their stated expiration day."""
    stated: dict[str, datetime.date] = {}
    current_table = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            current_table = stripped.split("#", 1)[0].strip()
            continue
        if current_table != "[lint.per-file-ignores]":
            continue
        match = RUFF_ENTRY.match(line)
        if match:
            key = next(iter(tomllib.loads(f"{match['key']} = 0")))
            stated[key] = datetime.date.fromisoformat(match["day"])
    return stated


def _ruff(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with accepted ruff findings, and how many entries were read."""
    path = root / RUFF
    try:
        text = path.read_text(encoding="utf-8")
        found, per_file = _ruff_layout(tomllib.loads(text))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    if per_file is None:
        return found, 0
    stated = _ruff_stated(text)
    for key in sorted(per_file.keys()):
        where = f"{RUFF}: {key}"
        if not _tracked_file(root, key):
            found.append(f"{where} is no one file of the repository")
        if key not in stated:
            found.append(f"{where} has no `# expires YYYY-MM-DD: reason`")
        elif problem := day_problem(stated[key], today):
            found.append(f"{where} {problem}")
    return found, len(per_file)


def _entry_problems(entry: object, root: Path, today: datetime.date) -> list[str]:
    """What is wrong with one entry of the repository's own list."""
    if not isinstance(entry, dict):
        return ["is no mapping"]
    unknown = sorted(set(entry) - ALLOWED)
    found = [f"has the unknown field `{key}`" for key in unknown]
    empty = [key for key in FIELDS if not isinstance(entry.get(key), str) or not entry[key].strip()]
    found += [f"has no `{key}` (a text that is not empty)" for key in empty]
    if "issue" not in empty and not ISSUE.match(entry["issue"]):
        found.append(f"names the issue `{entry['issue']}`, not as owner/name#number")
    if "path" not in empty and not _tracked_file(root, entry["path"]):
        found.append(f"names `{entry['path']}`, which is no file of the repository")
    cat = entry.get("category")
    cap = LONGEST
    if isinstance(cat, str) and cat in CATEGORY_DAYS:
        cap = max(LONGEST, datetime.timedelta(days=CATEGORY_DAYS[cat]))
    if "expires" not in empty:
        problem = day_problem(datetime.date.fromisoformat(entry["expires"]), today, longest=cap)
        found += [problem] if problem else []
    if "category" in entry and cat not in ("A", "B", "C", "D"):
        found.append(f"names `{cat}` as category, not one of A, B, C, D")
    valid_entered = False
    if "entered" in entry:
        entered = entry["entered"]
        if not isinstance(entered, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entered):
            found.append(f"names `{entered}` as entered, not YYYY-MM-DD")
        else:
            try:
                datetime.date.fromisoformat(entered)
                valid_entered = True
            except ValueError:
                found.append(f"names `{entered}` as entered, not YYYY-MM-DD")
    has_interval = isinstance(cat, str) and cat in CATEGORY_DAYS
    if has_interval and "expires" not in empty and "entered" in entry and valid_entered:
        interval = datetime.timedelta(days=CATEGORY_DAYS[cat])
        expected = datetime.date.fromisoformat(entered) + interval
        actual = datetime.date.fromisoformat(entry["expires"])
        if actual != expected:
            found.append(f"expires {actual} but category {cat} entered {entered} gives {expected}")
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


def issues(root: Path, rule: str) -> list[str]:
    """Issues the repository's own list names for one rule, for a text that must name them."""
    path = root / OWN
    entries = ciwf.load(path).get("exceptions") if path.exists() else None
    if not isinstance(entries, list):
        return []
    named = {
        entry.get("issue")
        for entry in entries
        if isinstance(entry, dict) and entry.get("rule") == rule
    }
    return sorted(issue for issue in named if isinstance(issue, str))


def problems(root: Path, today: datetime.date) -> tuple[list[str], int, int]:
    """What is wrong in the four places, and how many entries they hold."""
    findings, accepted = _findings(root, today)
    linted, ignored = _actionlint(root, today)
    ruffed, ruff_count = _ruff(root, today)
    own, excepted = _own(root, today)
    return findings + linted + ruffed + own, accepted + ignored + ruff_count, excepted


def main() -> int:
    """Check accepted linter findings and exceptions, exiting non-zero on failure."""
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
    max_days = max(LONGEST.days, *(CATEGORY_DAYS.values() or [LONGEST.days]))
    ciwf.say(
        f"PASS: {accepted} accepted finding(s) and {excepted} exception(s), "
        f"each with a reason and a day at most {max_days} days ahead"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
