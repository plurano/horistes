# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Report support for the offline Codacy Opengrep gate.

adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at 21f590a3535f (EUPL-1.2)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import Counter
from pathlib import Path

from codacy_opengrep_common import (
    CANARY_FILE,
    CANARY_WANT,
    FIXTURES,
    INTRAFILE_FILE,
    LITERAL_RE,
    MAX_CONTENT_CHARS,
    MAX_FILES,
    MAX_REPORT_ITEMS,
    MAX_SOURCE_BYTES,
    PATH_RE,
    ScanError,
    git,
    pretty,
)

FIELD_PREFIXES = frozenset({"f", "rf", "fr", "t", "rt", "tr"})


def string_prefix(text: str, start: int) -> str:
    """Return the lower-cased identifier characters, at most three, before index START."""
    begin = start
    for _ in range(min(start, 3)):
        if not (text[begin - 1].isalnum() or text[begin - 1] == "_"):
            break
        begin -= 1
    return text[begin:start].lower()


def open_field(text: str, match: re.Match[str]) -> bool:
    """Tell whether MATCH is an f- or t-string that ends while a replacement field is open.

    Since Python 3.12 (PEP 701) a field may hold a literal in the outer quotes, so the quote
    that closed MATCH may instead open that inner literal.
    """
    if string_prefix(text, match.start()) not in FIELD_PREFIXES:
        return False
    body = match.group(0).replace("{{", "").replace("}}", "")
    return body.count("{") > body.count("}")


def normalised_source(text: str) -> str:
    """Collapse whitespace runs outside Python string literals to one space; keep literals
    verbatim. Where an f- or t-string has no boundary this pass can trust, keep the whole
    text apart from its ends.
    """
    parts, last = [], 0
    for match in LITERAL_RE.finditer(text):
        if open_field(text, match):
            return text.strip()
        parts += [re.sub(r"\s+", " ", text[last : match.start()]), match.group(0)]
        last = match.end()
    parts.append(re.sub(r"\s+", " ", text[last:]))
    return "".join(parts).strip()


def fingerprint(issue: object) -> str:
    """Return the line-number-independent key of one Codacy issue."""
    if not isinstance(issue, dict):
        raise ScanError("report issue is not an object")
    pattern = issue.get("patternId")
    path = issue.get("filePath")
    content = issue.get("multilineContent")
    if not isinstance(content, list):
        content = [issue.get("lineContent")]
    if (
        not isinstance(pattern, str)
        or not isinstance(path, str)
        or not all(isinstance(line, str) for line in content)
    ):
        raise ScanError("report issue lacks patternId, filePath, or source content")
    text = "\n".join(content)
    if len(text) > MAX_CONTENT_CHARS:
        raise ScanError(
            f"report issue in {path} carries {len(text)} source characters, "
            f"at most {MAX_CONTENT_CHARS}"
        )
    digest = hashlib.sha256(normalised_source(text).encode("utf-8")).hexdigest()
    return f"{pattern}\0{path}\0{digest}"


def capability_problems(report: dict[str, object], version: str) -> list[str]:
    """Return why the report did not come from the pinned Opengrep with the Codacy pattern list."""
    capability = report.get("capability")
    ready = capability.get("ready") if isinstance(capability, dict) else None
    semgrep = (
        [item for item in ready if isinstance(item, dict) and item.get("toolId") == "Semgrep"]
        if isinstance(ready, list)
        else []
    )
    if len(semgrep) != 1:
        return ["report lacks one ready Semgrep capability"]
    problems = []
    if semgrep[0].get("configSource") != "codacy":
        problems.append(
            f"rules came from configSource {semgrep[0].get('configSource')!r}, "
            "not the checked-in Codacy pattern list"
        )
    if semgrep[0].get("version") != version:
        problems.append(f"Opengrep {semgrep[0].get('version')} ran, the pin is {version}")
    return problems


def report_lists(report: object) -> tuple[list[object], list[object], dict[str, object]]:
    """Return the errors, the issues and the one successful Semgrep result of REPORT, else raise."""
    if not isinstance(report, dict):
        raise ScanError("report is not a JSON object")
    for name in ("issues", "errors", "skippedFiles", "patternSummary", "toolResults"):
        value = report.get(name)
        if isinstance(value, list) and len(value) > MAX_REPORT_ITEMS:
            raise ScanError(f"report {name} exceeds {MAX_REPORT_ITEMS} items")
    skipped = report.get("skippedFiles", [])
    if not isinstance(skipped, list) or skipped:
        raise ScanError("report skippedFiles must be an empty list")
    errors, results, issues = report.get("errors"), report.get("toolResults"), report.get("issues")
    if (
        not isinstance(errors, list)
        or not isinstance(results, list)
        or not isinstance(issues, list)
    ):
        raise ScanError("report lacks errors, toolResults, or issues lists")
    semgrep = [
        item for item in results if isinstance(item, dict) and item.get("toolId") == "Semgrep"
    ]
    if len(semgrep) != 1 or semgrep[0].get("status") != "success":
        raise ScanError("report does not contain one successful Semgrep result")
    return errors, issues, semgrep[0]


def report_problems(
    report: object,
    files: list[str],
    baseline: Counter[str],
    version: str,
    enforce_stale: bool = True,
) -> list[str]:
    """Return report, provenance, coverage, new-finding, and stale-entry failures.

    Findings count per key: each finding past the baselined count is new, each one short is stale.
    """
    try:
        errors, issues, semgrep = report_lists(report)
        actual = Counter(fingerprint(issue) for issue in issues)
    except ScanError as err:
        return [str(err)]
    problems = capability_problems(report, version) + [f"tool error: {item}" for item in errors]
    if semgrep.get("filesAnalyzed") != len(files):
        problems.append(
            f"report covered {semgrep.get('filesAnalyzed')} of {len(files)} passed files"
        )
    allowed = set(files)
    problems.extend(
        f"report names an unpassed file: {issue['filePath']}"
        for issue in issues
        if issue["filePath"] not in allowed
    )
    problems.extend(
        f"new finding: {pretty(item)}" for item in sorted((actual - baseline).elements())
    )
    if enforce_stale:
        problems.extend(
            f"stale baseline entry: {pretty(item)}"
            for item in sorted((baseline - actual).elements())
        )
    return problems


def canary_problems(report: object, baseline: Counter[str], version: str) -> list[str]:
    """Return [] when REPORT shows each CANARY_WANT defect as exactly one new finding, and
    nothing else.
    """
    problems = report_problems(
        report, [CANARY_FILE, INTRAFILE_FILE], baseline, version, enforce_stale=False
    )
    found = [
        any(item.startswith(f"new finding: {rule} | {file} | ") for item in problems)
        for rule, file in CANARY_WANT
    ]
    if len(problems) == len(CANARY_WANT) and all(found):
        return []
    wanted = [f"{rule.rsplit('.', 1)[-1]} in {file}" for rule, file in CANARY_WANT]
    return [f"canary: want exactly one new finding each for {wanted}, got {problems}"]


def tracked_files(root: Path) -> list[str]:
    """List bounded tracked and unignored new Python targets, excluding the recorded fixtures;
    refuse a name that is no plain relative path.
    """
    result = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py")
    if result.returncode != 0:
        raise ScanError(f"git ls-files failed: {result.stderr.strip()}")
    names = [
        name for name in result.stdout.split("\0") if name and not name.startswith(FIXTURES + "/")
    ]
    if not names or len(names) > MAX_FILES:
        raise ScanError(f"{len(names)} Python files to scan, expected 1 to {MAX_FILES}")
    odd = [name for name in names if PATH_RE.fullmatch(name) is None or ".." in name.split("/")]
    if odd:
        raise ScanError(f"tracked Python files with names the scan copy refuses: {odd[:5]}")
    return sorted(name for name in names if target_exists(root / name))


def read_sources(root: Path, names: list[str]) -> dict[str, str]:
    """Read bounded regular source files without following target symlinks."""
    return {name: read_source(root / name) for name in names}


def read_source(path: Path) -> str:
    """Reject symlinks, non-regular files and files past the CLI size ceiling."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ScanError(f"scan target {path}: not a regular file")
            raw = source.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            raise ScanError(f"scan target {path}: exceeds {MAX_SOURCE_BYTES} bytes")
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(f"cannot read a scan target {path}: {err}") from err


def save_report(directory: Path | None, name: str, report: dict[str, object]) -> None:
    """Write REPORT as DIRECTORY/NAME when --report-out names a directory."""
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(
            json.dumps(report, indent=2, sort_keys=True).replace(
                "nose" + "mgrep", r"\u006eosemgrep"
            )
            + "\n",
            encoding="utf-8",
        )


def target_exists(path: Path) -> bool:
    """Skip index entries deleted from the worktree, preserving symlinks for rejection."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
