#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions carry an expiry, a MEL category and a reason; one reader judges all kinds.

exceptions.py check [--root DIR] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

CONFIG = ".github/zizmor.yml"
ACTIONLINT = ".github/actionlint.yaml"
OWN = "exceptions.yaml"
RUFF = "ruff.toml"
BASELINE = "suppression-baseline.json"

RUFF_TOP = frozenset({"line-length", "target-version", "lint"})
RUFF_LINT = frozenset({"select", "ignore", "per-file-ignores", "isort"})
FIELDS = ("rule", "path", "issue", "reason", "expires", "category", "entered")
ALLOWED = frozenset(FIELDS)
CATEGORY_DAYS = {"B": 3, "C": 10, "D": 120}
INTERVAL_PATTERN = re.compile(r"\binterval\s+(\d+)\s+days\b")
ISSUE = re.compile(r"^[\w.-]+/[\w.-]+#[1-9]\d*$")

SHORT_WORD = "no" + "sem"
LONG_WORD = SHORT_WORD + "grep"
OPEN_WORD = "no" + "opengrep"
WORDS = {"sem": SHORT_WORD, "sg": LONG_WORD, "SG": LONG_WORD.upper(), "og": OPEN_WORD}

REUSE_START = "REUSE-Ignore" + "Start"
REUSE_END = "REUSE-Ignore" + "End"

OPENGREP_IGNORE_WORD = r"\b(?:" + "|".join((SHORT_WORD, LONG_WORD, OPEN_WORD)) + r")\b"
OPENGREP_AFTER_CODE = re.compile(OPENGREP_IGNORE_WORD, re.IGNORECASE)
OPENGREP_OWN_LINE = re.compile(r"^[^a-zA-Z0-9]*\s*" + OPENGREP_IGNORE_WORD, re.IGNORECASE)

NOSEMGREP_FLEET = re.compile(
    r"^#\s*" + LONG_WORD + r":\s*(?P<rules>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
    r"\s+expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

NOSEC_DIRECTIVE = re.compile(r"#\s*nosec")
NOSEC_FLEET = re.compile(
    r"^#\s*nosec(?::|\s+)\s*(?P<rules>B\d{3}(?:[ ,]+B\d{3})*)"
    r"\s+(?:#\s*)?expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

NOQA_DIRECTIVE = re.compile(r"#\s*noqa\b")
NOQA_FLEET = re.compile(
    r"^#\s*noqa:\s*(?P<rules>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
    r"\s+expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

TYPE_IGNORE_DIRECTIVE = re.compile(r"#\s*type:\s*ignore\b")
TYPE_IGNORE_FLEET = re.compile(
    r"^#\s*type:\s*ignore(?:\[(?P<rules>[^\]]+)\])?"
    r"\s+#\s*expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

PYLINT_DIRECTIVE = re.compile(r"#\s*pylint:\s*disable(?:-next|-msg)?\b")
PYLINT_FLEET = re.compile(
    r"^#\s*pylint:\s*disable(?:-next|-msg)?=(?P<rules>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
    r"\s+expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

YAMLLINT_DIRECTIVE = re.compile(r"#\s*yamllint\s+disable(?:-line|-file)?\b")
ZIZMOR_DIRECTIVE = re.compile(r"#\s*zizmor:\s*ignore\b")
MARKDOWNLINT_DIRECTIVE = re.compile(r"<!--\s*markdownlint-disable(?:-line|-next-line|-file)?\b")
SHELLCHECK_DIRECTIVE = re.compile(r"#\s*shellcheck\s+disable=")

MARKDOWNLINT_FLEET = re.compile(
    r"^<!--\s*markdownlint-disable(?:-line|-next-line|-file)?(?::)?\s*"
    r"(?P<rules>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
    r"\s+expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>.+?)\s*-->$"
)

SHELLCHECK_FLEET = re.compile(
    r"^#\s*shellcheck\s+disable=(?P<rules>[A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
    r"\s+expires\s+(?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)

CONFIG_ENTRY = re.compile(
    r"^\s+- (?P<file>[^\s#]+)\s+# expires (?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)
ACTIONLINT_ENTRY = re.compile(
    r"^\s+- '(?P<pattern>[^']+)'\s+# expires (?P<day>\d{4}-\d{2}-\d{2})"
    r"\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)
RUFF_ENTRY = re.compile(
    r'^(?P<key>"[^"\\]+"|\'[^\']+\'|[A-Za-z0-9_-]+)\s*=\s*\[[^#]*'
    r"# expires (?P<day>\d{4}-\d{2}-\d{2})\s+category\s+(?P<cat>[A-Z]):\s+(?P<why>[^\n]+)$"
)


def _reason_problem(why: str) -> str:
    """Validate that reason is non-empty, contains at least one letter and no commas."""
    stripped = why.strip()
    if not stripped:
        return "reason is empty"
    if "," in stripped:
        return "reason contains a comma"
    if not re.search(r"[A-Za-z]", stripped):
        return "reason has no letter"
    return ""


def category_interval(category: str, reason: str) -> tuple[int | None, str]:
    """Compute and validate interval in days for a category and reason."""
    if category in CATEGORY_DAYS:
        return CATEGORY_DAYS[category], ""
    if category != "A":
        return None, f"category `{category}` is not one of A, B, C, D"
    match = INTERVAL_PATTERN.search(reason)
    if not match:
        return None, "category A needs 'interval N days' in reason"
    interval = int(match.group(1))
    if 1 <= interval <= 90:
        return interval, ""
    return None, f"category A interval {interval} must be between 1 and 90 days"


def category_problem(
    day: datetime.date,
    category: str,
    reason: str,
    today: datetime.date,
) -> str:
    """Why an expiration day is refused under fleet MEL rules; empty when valid."""
    if day < today:
        return f"expired on {day}"
    interval, prob = category_interval(category, reason)
    if prob:
        return prob
    if interval is None:
        return f"category `{category}` has no interval"
    if day - today > datetime.timedelta(days=interval):
        return f"ends on {day}, more than {interval} days ahead"
    return ""


def _check_fleet_match(match: re.Match[str], today: datetime.date) -> str:
    """Check reason, category and expiry from a matched fleet directive."""
    why_prob = _reason_problem(match.group("why"))
    if why_prob:
        return why_prob
    cat = match.group("cat")
    if cat not in ("A", "B", "C", "D"):
        return f"category `{cat}` is not one of A, B, C, D"
    try:
        day = datetime.date.fromisoformat(match.group("day"))
    except ValueError as err:
        raise ciwf.ReadError(f"{match.group('day')}: {err}") from err
    return category_problem(day, cat, match.group("why").strip(), today)


def _is_covered(
    rules_str: str,
    file_path: str,
    valid_exceptions: set[tuple[str, str]],
) -> bool:
    """Check whether every rule in rules_str is covered by valid_exceptions for file_path."""
    clean = rules_str.strip()
    if clean.startswith("[") and clean.endswith("]"):
        clean = clean[1:-1].strip()
    rules = [r.strip() for r in re.split(r"[,\s]+", clean) if r.strip()]
    if not rules:
        return False
    return all((r, file_path) in valid_exceptions for r in rules)


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
        match = CONFIG_ENTRY.match(line)
        if match:
            stated[match["file"]] = match
    for rule, entry in wanted:
        if entry not in stated:
            found.append(
                f"{CONFIG}: {rule} for {entry} has no "
                "`# expires YYYY-MM-DD category <A|B|C|D>: reason`"
            )
            continue
        prob = _check_fleet_match(stated[entry], today)
        if prob:
            found.append(f"{CONFIG}: {rule} for {entry} {prob}")
    return found, len(wanted)


def _actionlint(root: Path, today: datetime.date) -> tuple[list[str], int]:
    """What is wrong with accepted actionlint findings, and how many entries were read."""
    path = root / ACTIONLINT
    if not path.exists():
        return [], 0
    files = ciwf.load(path).get("paths") or {}
    stated = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ACTIONLINT_ENTRY.match(line)
        if match:
            stated[match["pattern"]] = match
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
                found.append(
                    f"{where}: '{entry}' has no `# expires YYYY-MM-DD category <A|B|C|D>: reason`"
                )
                continue
            prob = _check_fleet_match(stated[entry], today)
            if prob:
                found.append(f"{where}: '{entry}' {prob}")
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


def _ruff_stated(text: str) -> dict[str, re.Match[str]]:
    """Map accepted per-file-ignores keys to their stated match object."""
    stated: dict[str, re.Match[str]] = {}
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
            stated[key] = match
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
            found.append(f"{where} has no `# expires YYYY-MM-DD category <A|B|C|D>: reason`")
        elif prob := _check_fleet_match(stated[key], today):
            found.append(f"{where} {prob}")
    return found, len(per_file)


def _parse_entry_date(val: object, field: str, found: list[str]) -> datetime.date | None:
    """Parse an ISO date string from an entry field, recording errors."""
    if not isinstance(val, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
        found.append(f"names `{val}` as {field}, not YYYY-MM-DD")
        return None
    try:
        return datetime.date.fromisoformat(val)
    except ValueError as err:
        raise ciwf.ReadError(f"names `{val}` as {field}, not YYYY-MM-DD: {err}") from err


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
    if "category" not in empty and cat not in ("A", "B", "C", "D"):
        found.append(f"names `{cat}` as category, not one of A, B, C, D")
    ent_d = (
        _parse_entry_date(entry.get("entered"), "entered", found)
        if "entered" not in empty
        else None
    )
    exp_d = (
        _parse_entry_date(entry.get("expires"), "expires", found)
        if "expires" not in empty
        else None
    )
    interval, interval_prob = (
        category_interval(str(cat), str(entry.get("reason", "")))
        if isinstance(cat, str) and cat in ("A", "B", "C", "D")
        else (None, "")
    )
    if interval_prob:
        found.append(interval_prob)
    if interval is not None and ent_d is not None and exp_d is not None:
        expected = ent_d + datetime.timedelta(days=interval)
        if exp_d != expected:
            found.append(
                f"expires {exp_d} but category {cat} entered {entry['entered']} gives {expected}"
            )
    if exp_d is not None and isinstance(cat, str) and cat in ("A", "B", "C", "D"):
        prob = category_problem(exp_d, cat, str(entry.get("reason", "")), today)
        if prob and prob != interval_prob:
            found.append(prob)
    return found


def _tracked_file(root: Path, name: str) -> bool:
    """Whether a name is a plain relative path to a file below root and tracked by git."""
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", f":(literal){name}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=ciwf.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        raise ciwf.ReadError(f"{root}: {err}") from err
    if proc.returncode == 0:
        return True
    if proc.returncode == 1 and "did not match" in proc.stderr:
        return False
    message = proc.stderr.strip() or f"git exited with {proc.returncode}"
    raise ciwf.ReadError(f"{root}: {message}")


def _tracked_files(root: Path) -> list[str]:
    """Every tracked file below root, from git ls-files."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=False,
            check=False,
            timeout=30,
            env=ciwf.git_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        raise ciwf.ReadError(f"{root}: {err}") from err
    if proc.returncode != 0:
        err_msg = proc.stderr.decode("utf-8", "replace").strip()
        message = err_msg or f"git exited with {proc.returncode}"
        raise ciwf.ReadError(f"{root}: {message}")
    return [p.decode("utf-8") for p in proc.stdout.split(b"\0") if p]


def _own(root: Path, today: datetime.date) -> tuple[list[str], int, dict[tuple[str, str], dict]]:
    """What is wrong with the repository's own list, and how many entries were read."""
    path = root / OWN
    if not path.exists():
        return [], 0, {}
    doc = ciwf.load(path)
    entries = doc.get("exceptions")
    if set(doc) != {"exceptions"} or not isinstance(entries, list):
        raise ciwf.ReadError(f"{path}: must hold one list, `exceptions`")
    found = []
    seen = set()
    valid_map = {}
    for number, entry in enumerate(entries, 1):
        where = f"{OWN}: entry {number}"
        entry_probs = _entry_problems(entry, root, today)
        found += [f"{where} {problem}" for problem in entry_probs]
        key = (entry.get("rule"), entry.get("path")) if isinstance(entry, dict) else None
        if key in seen:
            found.append(f"{where} names rule and path of an entry before it")
        else:
            if not entry_probs and key:
                valid_map[key] = entry
            seen.add(key)
    return found, len(entries), valid_map


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


def codacy_problems(text: str) -> list[str]:
    """Check .codacy.yml: engines must not define exclude_paths."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return []
    problems = []
    for engine, config in sorted(engines.items()):
        if isinstance(config, dict) and "exclude_paths" in config:
            problems.append(f"engines.{engine}.exclude_paths is not permitted")
    return problems


def token_problems(tok: tokenize.TokenInfo, name: str) -> list[str]:
    """Refuse an opengrep ignore word outside a comment."""
    found = OPENGREP_AFTER_CODE.search(tok.string)
    if not found:
        return []
    line = tok.start[0] + tok.string.count("\n", 0, found.start())
    return [f"{name}:{line}: invalid opengrep ignore word outside a comment: {tok.string.strip()}"]


def _check_directive(
    comment: str,
    pattern: re.Pattern[str],
    kind: str,
    where: str,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
    path: str,
    rules_pattern: str,
) -> list[str]:
    """Check one inline comment directive against fleet format or covered exceptions."""
    match = pattern.match(comment)
    if match:
        prob = _check_fleet_match(match, today)
        return [f"{where}: invalid {kind} suppression: {prob}"] if prob else []
    rules_match = re.match(rules_pattern, comment)
    if rules_match and _is_covered(rules_match.group(1), path, valid_exceptions):
        return []
    return [f"{where}: invalid {kind} suppression: {comment}"]


def comment_problems(
    comment: str,
    after_code: bool,
    where: str,
    today: datetime.date | None = None,
    valid_exceptions: set[tuple[str, str]] | None = None,
    path: str = "",
    counts: dict[str, int] | None = None,
) -> list[str]:
    """Read one comment as linters read it; verify fleet format or exception coverage."""
    now = today or datetime.date.today()
    valid = valid_exceptions or set()
    problems: list[str] = []
    opengrep = OPENGREP_AFTER_CODE if after_code else OPENGREP_OWN_LINE
    if opengrep.search(comment):
        if counts is not None:
            counts[LONG_WORD] = counts.get(LONG_WORD, 0) + 1
        rule_pat = r"^#\s*" + LONG_WORD + r":\s*([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
        probs = _check_directive(
            comment, NOSEMGREP_FLEET, "opengrep", where, now, valid, path, rule_pat
        )
        problems.extend(probs)
    if NOSEC_DIRECTIVE.search(comment):
        if counts is not None:
            counts["nosec"] = counts.get("nosec", 0) + 1
        rule_pat = r"^#\s*nosec(?::|\s+)\s*(B\d{3}(?:[ ,]+B\d{3})*)"
        probs = _check_directive(comment, NOSEC_FLEET, "nosec", where, now, valid, path, rule_pat)
        problems.extend(probs)
    noqa_match = NOQA_DIRECTIVE.search(comment)
    if noqa_match:
        if counts is not None:
            counts["noqa"] = counts.get("noqa", 0) + 1
        noqa_comment = comment[noqa_match.start() :]
        rule_pat = r"^#\s*noqa:\s*([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
        probs = _check_directive(
            noqa_comment, NOQA_FLEET, "noqa", where, now, valid, path, rule_pat
        )
        problems.extend(probs)
    if TYPE_IGNORE_DIRECTIVE.search(comment):
        if counts is not None:
            counts["type: ignore"] = counts.get("type: ignore", 0) + 1
        rule_pat = r"^#\s*type:\s*ignore(?:\[([^\]]+)\])?"
        probs = _check_directive(
            comment, TYPE_IGNORE_FLEET, "type: ignore", where, now, valid, path, rule_pat
        )
        problems.extend(probs)
    if PYLINT_DIRECTIVE.search(comment):
        if counts is not None:
            counts["pylint"] = counts.get("pylint", 0) + 1
        rule_pat = (
            r"^#\s*pylint:\s*disable(?:-next|-msg)?="
            r"([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
        )
        probs = _check_directive(comment, PYLINT_FLEET, "pylint", where, now, valid, path, rule_pat)
        problems.extend(probs)
    return problems


def _has_trailing_noqa_e501(comment: str) -> bool:
    """Whether comment ends with # noqa: ... containing E501."""
    match = re.search(r"#\s*noqa:\s*([A-Za-z0-9_.,\s]+)$", comment)
    if not match:
        return False
    codes = {c.strip() for c in re.split(r"[,\s]+", match.group(1)) if c.strip()}
    return "E501" in codes


def suppression_problems(
    text: str,
    name: str,
    today: datetime.date | None = None,
    valid_exceptions: set[tuple[str, str]] | None = None,
    counts: dict[str, int] | None = None,
) -> list[str]:
    """Tokenize Python text and verify that all suppression comments meet requirements."""
    problems: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                problems.extend(token_problems(tok, name))
                continue
            after_code = bool(tok.line[: tok.start[1]].strip())
            where = f"{name}:{tok.start[0]}"
            line_probs = comment_problems(
                tok.string.strip(),
                after_code,
                where,
                today=today,
                valid_exceptions=valid_exceptions,
                path=name,
                counts=counts,
            )
            if (
                not after_code
                and NOSEMGREP_FLEET.match(tok.string.strip())
                and len(tok.line.rstrip("\r\n")) > 100
            ):
                covered = (
                    valid_exceptions is not None
                    and ("E501", name) in valid_exceptions
                    and _has_trailing_noqa_e501(tok.string.strip())
                )
                if not covered:
                    line_probs.append(f"{where}: {LONG_WORD} line exceeds 100 columns")
            problems.extend(line_probs)
    except tokenize.TokenError as err:
        problems.append(f"{name}:1: tokenize error: {err}")
    return problems


def yaml_problems(
    text: str,
    name: str,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
    counts: dict[str, int],
) -> list[str]:
    """Check YAML comments for invalid or unapproved suppressions."""
    problems = []
    for line_no, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{line_no}"
        if YAMLLINT_DIRECTIVE.search(line):
            problems.append(f"{where}: invalid yamllint suppression: {line.strip()}")
        elif ZIZMOR_DIRECTIVE.search(line):
            problems.append(f"{where}: invalid zizmor suppression: {line.strip()}")
        elif OPENGREP_AFTER_CODE.search(line):
            counts[LONG_WORD] = counts.get(LONG_WORD, 0) + 1
            rule_pat = r"#\s*" + LONG_WORD + r":\s*([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)"
            probs = _check_directive(
                line.strip(),
                NOSEMGREP_FLEET,
                "opengrep",
                where,
                today,
                valid_exceptions,
                name,
                rule_pat,
            )
            problems.extend(probs)
    return problems


def md_problems(
    text: str,
    name: str,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
    counts: dict[str, int],
) -> list[str]:
    """Check Markdown files for markdownlint directives."""
    problems = []
    for line_no, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{line_no}"
        if not MARKDOWNLINT_DIRECTIVE.search(line):
            continue
        counts["markdownlint"] = counts.get("markdownlint", 0) + 1
        match = MARKDOWNLINT_FLEET.search(line.strip())
        if match:
            prob = _check_fleet_match(match, today)
            if prob:
                problems.append(f"{where}: invalid markdownlint suppression: {prob}")
            continue
        extract = re.search(
            r"<!--\s*markdownlint-disable(?:-line|-next-line|-file)?(?::)?\s*(.*?)\s*-->",
            line,
        )
        rules_str = extract.group(1) if extract else ""
        if not rules_str or not _is_covered(rules_str, name, valid_exceptions):
            problems.append(
                f"{where}: invalid markdownlint suppression: "
                f"not in fleet format and not covered by {OWN}"
            )
    return problems


def sh_problems(
    text: str,
    name: str,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
    counts: dict[str, int],
) -> list[str]:
    """Check shell scripts for shellcheck directives."""
    problems = []
    for line_no, line in enumerate(text.splitlines(), 1):
        where = f"{name}:{line_no}"
        if not SHELLCHECK_DIRECTIVE.search(line):
            continue
        counts["shellcheck"] = counts.get("shellcheck", 0) + 1
        match = SHELLCHECK_FLEET.search(line.strip())
        if match:
            prob = _check_fleet_match(match, today)
            if prob:
                problems.append(f"{where}: invalid shellcheck suppression: {prob}")
            continue
        extract = re.search(
            r"#\s*shellcheck\s+disable=([A-Za-z0-9_.-]+(?:\s*,\s*[A-Za-z0-9_.-]+)*)",
            line,
        )
        rules_str = extract.group(1) if extract else ""
        if not rules_str or not _is_covered(rules_str, name, valid_exceptions):
            problems.append(
                f"{where}: invalid shellcheck suppression: "
                f"not in fleet format and not covered by {OWN}"
            )
    return problems


def _reuse_problems(name: str, text: str) -> list[str]:
    """Refuse REUSE ignore markers in any tracked text file."""
    if REUSE_START not in text and REUSE_END not in text:
        return []
    problems = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if REUSE_START in line or REUSE_END in line:
            problems.append(
                f"{name}:{line_no}: invalid REUSE suppression: "
                "marker not permitted, use REUSE.toml override"
            )
    return problems


def _scan_tracked_file(
    rel: str,
    root: Path,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
    counts: dict[str, int],
) -> tuple[list[str], int]:
    """Scan one tracked file for suppression directives and invalid patterns."""
    file_path = root / rel
    if not file_path.is_file():
        return [], 0
    try:
        text = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [], 1
    except OSError as err:
        raise ciwf.ReadError(f"{rel}: {err}") from err
    file_probs = _reuse_problems(rel, text)
    if rel == ".codacy.yml":
        file_probs.extend(f"{rel}: {p}" for p in codacy_problems(text))
    if rel.endswith(".py"):
        file_probs.extend(suppression_problems(text, rel, today, valid_exceptions, counts))
    elif rel.endswith((".yml", ".yaml", ".yml.tmpl", ".yaml.tmpl")):
        file_probs.extend(yaml_problems(text, rel, today, valid_exceptions, counts))
    elif rel.endswith(".md"):
        file_probs.extend(md_problems(text, rel, today, valid_exceptions, counts))
    elif rel.endswith((".sh", ".bash")):
        file_probs.extend(sh_problems(text, rel, today, valid_exceptions, counts))
    return file_probs, 0


def _tree_directives(
    root: Path,
    today: datetime.date,
    valid_exceptions: set[tuple[str, str]],
) -> tuple[list[str], dict[str, int], int]:
    """Scan all tracked files below root for directives and tally their counts by kind."""
    problems: list[str] = []
    counts: dict[str, int] = {}
    skipped = 0
    for rel in _tracked_files(root):
        file_probs, file_skipped = _scan_tracked_file(rel, root, today, valid_exceptions, counts)
        problems.extend(file_probs)
        skipped += file_skipped
    return problems, counts, skipped


def _ratchet_problems(root: Path, counts: dict[str, int]) -> list[str]:
    """Compare observed directive line counts against suppression-baseline.json."""
    path = root / BASELINE
    if not path.exists():
        if any(counts.values()):
            return [f"{BASELINE}: does not exist"]
        return []
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    if not isinstance(baseline, dict):
        return [f"{BASELINE}: must be a JSON object"]
    problems = []
    for kind in sorted(baseline.keys()):
        val = baseline[kind]
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            problems.append(f"{BASELINE}: {kind} is not a count")
    if problems:
        return problems
    for kind in sorted(counts.keys() | baseline.keys()):
        actual = counts.get(kind, 0)
        base = baseline.get(kind, 0)
        if actual > base:
            problems.append(f"{kind} grew from {base} to {actual}")
        elif actual < base:
            problems.append(f"lower {kind} in suppression-baseline.json to {actual}")
    return problems


def problems(root: Path, today: datetime.date) -> tuple[list[str], int, int, int]:
    """What is wrong in the four places and tracked files, and how many entries they hold."""
    findings, accepted = _findings(root, today)
    linted, ignored = _actionlint(root, today)
    ruffed, ruff_count = _ruff(root, today)
    own, excepted, valid_map = _own(root, today)
    tree_probs, counts, skipped = _tree_directives(root, today, set(valid_map.keys()))
    ratchet_probs = _ratchet_problems(root, counts)
    all_problems = findings + linted + ruffed + own + tree_probs + ratchet_probs
    return all_problems, accepted + ignored + ruff_count, excepted, skipped


def main() -> int:
    """Check accepted linter findings and exceptions, exiting non-zero on failure."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--today", type=datetime.date.fromisoformat, default=datetime.date.today())
    args = parser.parse_args()
    try:
        found, accepted, excepted, skipped = problems(args.root, args.today)
    except (ciwf.ReadError, ValueError) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    for line in found:
        ciwf.complain(f"FAIL: {line}")
    if found:
        return 1
    max_days = max(CATEGORY_DAYS.values())
    skipped_text = f", {skipped} tracked file(s) skipped as not UTF-8" if skipped else ""
    ciwf.say(
        f"PASS: {accepted} accepted finding(s) and {excepted} exception(s), "
        f"each with a reason and a day at most {max_days} days ahead{skipped_text}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
