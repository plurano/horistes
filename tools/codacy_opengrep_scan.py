# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Run Codacy's checked-in Python Opengrep rules offline against a shrink-only baseline.

Adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at
21f590a3535f (EUPL-1.2). Installation alone may fetch pinned tools.
The scratch scan checks the 753b7aa / fc53740 shape and intrafile taint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

BASELINE = "tools/codacy_opengrep/baseline.json"
CONFIG = "tools/codacy_opengrep/opengrep-python.config.json"
FIXTURES = "tools/codacy_opengrep/testdata"
PACKAGE = "tools/codacy_opengrep"
TOOL_VERSIONS = "tools/codacy_opengrep/tool-versions.env"
CLI_PACKAGE = "@codacy/analysis-cli"
CANARY_FILE = "tests/test_examples.py"
INTRAFILE_SOURCE = "tools/codacy_opengrep/testdata/intrafile-canary.py.txt"
INTRAFILE_FILE = "tools/intrafile_canary.py"  # where the scan copy holds INTRAFILE_SOURCE
TAINT_RULE = (
    "Semgrep_python.lang.security.audit.dangerous-subprocess-use-tainted-env-args."
    "dangerous-subprocess-use-tainted-env-args"
)
AUDIT_RULE = (
    "Semgrep_python.lang.security.audit.dangerous-subprocess-use-audit."
    "dangerous-subprocess-use-audit"
)
# (rule, file): each planted defect must be exactly one new finding, and nothing else may be new.
CANARY_WANT = (
    (AUDIT_RULE, CANARY_FILE),
    (TAINT_RULE, INTRAFILE_FILE),
    (AUDIT_RULE, INTRAFILE_FILE),
)
# Codacy cloud runs Opengrep with --taint-intrafile; the CLI plugin does not pass it, so this
# wrapper does (issue 186).
WRAPPER = """#!/bin/sh
if [ "$1" = scan ]; then
  shift
  exec {binary} scan --taint-intrafile "$@"
fi
exec {binary} "$@"
"""
DOWNLOAD_PREFIX = "https://github.com/opengrep/opengrep/releases/download/v"
BASELINE_VERSION = 2
BASELINE_KEYS = frozenset(
    {
        "patternId",
        "filePath",
        "contentSha256",
        "count",
        "category",
        "entered",
        "expires",
        "issue",
        "reason",
    }
)
KEPT_FIELDS = ("entered", "category", "issue", "reason")  # a kept entry may not change these
CATEGORY_DAYS = {"B": 3, "C": 10, "D": 120}  # MEL categories; A states its own interval
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
COUNT_RE = re.compile(r"[1-9][0-9]?")
ISSUE_RE = re.compile(r"[1-9][0-9]{0,5}")
PATH_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]{0,250}\.py")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^-]{0,199}")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# Python string literals: triple-quoted (may run to the end of the reported text), then one-line.
LITERAL_RE = re.compile(
    r'"""(?:\\.|(?!""").)*(?:"""|\Z)'
    r"|'''(?:\\.|(?!''').)*(?:'''|\Z)"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'",
    re.DOTALL,
)
REASON_CHARS = (20, 300)
MAX_BASELINE_ENTRIES = 512
MAX_CONTENT_CHARS = 64 * 1024
MAX_FILES = 4096
GIT_TIMEOUT_SECONDS = 30
CLI_TIMEOUT_SECONDS = 240
TOOL_TIMEOUT_MS = 210_000
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_DEADLINE_SECONDS = 180
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_READS = 1 << 20
ROOT = Path(__file__).resolve().parents[1]
CANARY_OLD = '        return subprocess.CompletedProcess(["sh", "-n"], 0, "", "")'
CANARY_NEW = '        return subprocess.CompletedProcess(argv, 0, "", "")'
ADAPTER = "node_modules/@codacy/tools-opengrep-1/dist/metadata.js"
PREFERRED_RE = re.compile(r'preferredVersion:\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
NPM_TIMEOUT_SECONDS = 240


class ScanError(Exception):
    """Signal a report, baseline, or scanner result that cannot be trusted."""


def load_json(path: Path) -> object:
    """Load one UTF-8 JSON document or raise a scanner error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ScanError(f"cannot read {path}: {err}") from err


def run_capture(
    argv: list[str],
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
    *,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one argument list (never a shell) under a deadline and capture its text output.

    STDIN carries data, such as an object name for git cat-file, so that data never becomes
    an argument.
    """
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ScanError(f"{Path(argv[0]).name} did not complete: {err}") from err


def git_env(environ: dict[str, str]) -> dict[str, str]:
    """Return ENVIRON without GIT_* variables: GIT_DIR, GIT_INDEX_FILE and the like would point
    git elsewhere.
    """
    return {key: value for key, value in environ.items() if not key.startswith("GIT_")}


def git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git, resolved on PATH, on the repository at ROOT; STDIN feeds batch input."""
    exe = shutil.which("git")
    if exe is None:
        raise ScanError("git not found on PATH")
    return run_capture(
        [exe, *args], root, GIT_TIMEOUT_SECONDS, git_env(dict(os.environ)), stdin=stdin
    )


def config_tool(root: Path) -> dict[str, object]:
    """Return the one tool entry of the checked-in config; refuse a fallback to local rule files."""
    data = load_json(root / CONFIG)
    tools = data.get("tools") if isinstance(data, dict) else None
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
        raise ScanError(f"{CONFIG}: want exactly one tool entry")
    if tools[0].get("useLocalConfigurationFile") is not False:
        raise ScanError(
            f"{CONFIG}: useLocalConfigurationFile must be false, or the CLI reads .semgrep.yml"
        )
    return tools[0]


def config_ids(root: Path) -> list[str]:
    """Return the unique, sorted Semgrep pattern IDs from the checked-in config."""
    patterns = config_tool(root).get("patterns")
    ids = (
        [item.get("patternId") if isinstance(item, dict) else None for item in patterns]
        if isinstance(patterns, list)
        else []
    )
    if not ids or not all(isinstance(item, str) for item in ids):
        raise ScanError(f"{CONFIG}: invalid pattern list")
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ScanError(f"{CONFIG}: pattern IDs must be sorted and unique")
    return ids


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


def shrink_problems(root: Path, ref: str, today: date) -> list[str]:
    """Compare the baseline with its version at REF; a REF without one is the first baseline."""
    commit = resolve_commit(root, ref)
    current = load_baseline(root, today)
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


def pretty(key: str) -> str:
    """Render a NUL-joined key for a diagnostic."""
    return key.replace("\0", " | ")


def normalised_source(text: str) -> str:
    """Collapse whitespace runs outside Python string literals to one space; keep literals
    verbatim.
    """
    parts, last = [], 0
    for match in LITERAL_RE.finditer(text):
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
    return sorted(names)


def tool_versions(root: Path) -> dict[str, str]:
    """Read the exact Opengrep release pin without sourcing shell text."""
    values = {}
    for line in (root / TOOL_VERSIONS).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    required = ("OPENGREP_VERSION", "OPENGREP_SHA256", "OPENGREP_URL")
    if any(not values.get(key) for key in required):
        raise ScanError(f"{TOOL_VERSIONS}: missing Opengrep pin")
    if not VERSION_RE.fullmatch(values["OPENGREP_VERSION"]) or not SHA_RE.fullmatch(
        values["OPENGREP_SHA256"]
    ):
        raise ScanError(f"{TOOL_VERSIONS}: OPENGREP_VERSION or OPENGREP_SHA256 malformed")
    if not values["OPENGREP_URL"].startswith(f"{DOWNLOAD_PREFIX}{values['OPENGREP_VERSION']}/"):
        raise ScanError(
            f"{TOOL_VERSIONS}: OPENGREP_URL must start with "
            f"{DOWNLOAD_PREFIX}{values['OPENGREP_VERSION']}/"
        )
    return values


def copy_capped(response: object, output: object, digest: object) -> int:
    """Copy RESPONSE into OUTPUT and DIGEST in bounded reads under MAX_DOWNLOAD_BYTES; return
    the size.
    """
    size = 0
    for _ in range(MAX_READS):
        chunk = response.read(CHUNK_BYTES)
        if not chunk:
            return size
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            raise ScanError(f"download passed {MAX_DOWNLOAD_BYTES} bytes")
        digest.update(chunk)
        output.write(chunk)
    raise ScanError(f"download did not end within {MAX_READS} reads")


def fetch_into(
    url: str, destination: Path, opener: Callable[..., object], outcome: dict[str, object]
) -> None:
    """Download URL to DESTINATION; record the sha256 and size, or the error, in OUTCOME."""
    digest = hashlib.sha256()
    try:
        with (
            opener(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as output,
        ):
            outcome["size"] = copy_capped(response, output, digest)
        outcome["sha256"] = digest.hexdigest()
    except (OSError, ScanError) as err:
        outcome["error"] = f"download failed: {err}"


def fetch_within(
    url: str, destination: Path, deadline: float, opener: Callable[..., object]
) -> tuple[str, int]:
    """Download URL in a worker thread and give up once DEADLINE seconds pass, however the
    socket behaves.

    Serves the installation of the pinned Opengrep binary.

    A server that trickles bytes just inside the socket timeout cannot hold the scan past
    DEADLINE; the abandoned daemon thread ends with the process.
    """
    outcome: dict[str, object] = {}
    worker = threading.Thread(
        target=fetch_into,
        args=(url, destination, opener, outcome),
        name="opengrep-download",
        daemon=True,
    )
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise ScanError(f"download did not finish within {deadline} s")
    if "error" in outcome:
        raise ScanError(str(outcome["error"]))
    return str(outcome["sha256"]), int(outcome["size"])


def write_wrapper(directory: Path, binary: Path) -> Path:
    """Write DIRECTORY/opengrep, the WRAPPER the CLI runs in place of BINARY, and return its
    path.
    """
    wrapper = directory / "opengrep"
    wrapper.write_text(WRAPPER.format(binary=shlex.quote(str(binary))), encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def cli_path(root: Path) -> Path:
    """Return the installed CLI after checking it is the exact version package.json pins."""
    package = root / PACKAGE
    wanted = load_json(package / "package.json")
    dependencies = wanted.get("dependencies") if isinstance(wanted, dict) else None
    pin = dependencies.get(CLI_PACKAGE) if isinstance(dependencies, dict) else None
    if not isinstance(pin, str) or not VERSION_RE.fullmatch(pin):
        raise ScanError(f"{PACKAGE}/package.json: {CLI_PACKAGE} needs an exact x.y.z pin")
    installed_file = package / "node_modules" / CLI_PACKAGE / "package.json"
    cli = package / "node_modules/.bin/codacy-analysis"
    if not installed_file.is_file() or not cli.is_file():
        raise ScanError(f"{CLI_PACKAGE} not installed: run make opengrep-tools")
    installed = load_json(installed_file)
    if not isinstance(installed, dict) or installed.get("version") != pin:
        raise ScanError(
            f"{CLI_PACKAGE} {installed.get('version') if isinstance(installed, dict) else '?'} "
            f"installed, the pin is {pin}"
        )
    return cli


def remove_generated_config(cwd: Path) -> None:
    """Remove the ignored rule file the CLI writes for every scan, and its empty parents."""
    generated = cwd / ".codacy/generated/Semgrep/opengrep-rules.yaml"
    generated.unlink(missing_ok=True)
    for directory in (generated.parent, generated.parent.parent, generated.parent.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            return


def cli_env(environ: Mapping[str, str], home: Path) -> dict[str, str]:
    """Return ENVIRON without CODACY_* variables, plus CODACY_GLOBAL_DIR=HOME.

    A token there would let the CLI reach Codacy; the scan uses the checked-in config only.
    """
    return {
        **{key: value for key, value in environ.items() if not key.startswith("CODACY_")},
        "CODACY_GLOBAL_DIR": str(home),
        "SEMGREP_LOG_FILE": str(home / "opengrep.log"),
    }


def live_report(root: Path, cwd: Path, home: Path) -> dict[str, object]:
    """Run the pinned CLI over every file of the scan copy CWD with the verified Opengrep under
    HOME; return its report.

    No file name reaches the command line: the CLI lists the copy with git ls-files.
    """
    cli = cli_path(root)
    with tempfile.TemporaryDirectory(prefix="horistes-opengrep-report-") as temporary:
        report = Path(temporary) / "report.json"
        command = [
            str(cli),
            "analyze",
            ".",
            "--tool",
            "Semgrep",
            "--config-file",
            str(root / CONFIG),
            "--output-format",
            "json",
            "--output",
            str(report),
            "--max-file-size",
            "1MB",
            "--fail-if-missing",
            "--parallel-tools",
            "1",
            "--tool-timeout",
            str(TOOL_TIMEOUT_MS),
            "--no-log",
            "--no-update-notifier",
        ]
        try:
            result = run_capture(command, cwd, CLI_TIMEOUT_SECONDS, cli_env(os.environ, home))
        finally:
            remove_generated_config(cwd)
        if result.returncode not in (0, 1) or not report.is_file():
            raise ScanError(
                f"Codacy CLI failed (exit {result.returncode}): {result.stderr[-2000:]}"
            )
        data = load_json(report)
    if not isinstance(data, dict):
        raise ScanError("Codacy CLI report is not a JSON object")
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("repositoryRoot", None)  # a local path; recorded fixtures stay host-neutral
    return data


def copy_report(root: Path, home: Path, sources: dict[str, str]) -> dict[str, object]:
    """Write SOURCES (path to text) into a scratch repository, add them all, and scan it; the
    CLI needs a repository.
    """
    with tempfile.TemporaryDirectory(prefix="horistes-opengrep-copy-") as temporary:
        scratch = Path(temporary)
        for name, source in sources.items():
            target = scratch / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        for args in (
            ("init", "-q"),
            ("add", "--all", "--force"),
        ):  # --force: a global excludes file must not drop a target
            result = git(scratch, *args)
            if result.returncode != 0:
                raise ScanError(f"scan copy: git {args[0]} failed: {result.stderr.strip()}")
        return live_report(root, scratch, home)


def read_sources(root: Path, names: list[str]) -> dict[str, str]:
    """Return the text of each NAME under ROOT, keyed by NAME."""
    try:
        return {name: (root / name).read_text(encoding="utf-8") for name in names}
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(f"cannot read a scan target: {err}") from err


def canary_report(root: Path, home: Path) -> dict[str, object]:
    """Scan the two planted canary sources at their paths in a scan copy."""
    planted = canary_source(root)
    return copy_report(
        root,
        home,
        {
            CANARY_FILE: planted,
            INTRAFILE_FILE: read_sources(root, [INTRAFILE_SOURCE])[INTRAFILE_SOURCE],
        },
    )


def save_report(directory: Path | None, name: str, report: dict[str, object]) -> None:
    """Write REPORT as DIRECTORY/NAME when --report-out names a directory."""
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def canary_check(
    root: Path, home: Path, baseline: Counter[str], version: str, report_out: Path | None
) -> list[str]:
    """Replay the planted 753b7aa / fc53740 shape live and return why it was not exactly one
    new finding.
    """
    try:
        canary = canary_report(root, home)
    except (
        ScanError
    ) as err:  # the tree scan still runs: a planted tree also breaks the canary derivation
        return [str(err)]
    save_report(report_out, "canary-report.json", canary)
    return canary_problems(canary, baseline, version)


def scan(root: Path, today: date, report_out: Path | None) -> list[str]:
    """Run the canary first, then scan every tracked Python file; return the failures."""
    versions = tool_versions(root)
    version = versions["OPENGREP_VERSION"]
    baseline = baseline_counts(load_baseline(root, today))
    targets = tracked_files(root)
    check_install(root)
    with codacy_home(versions) as home:
        problems = canary_check(root, home, baseline, version, report_out)
        report = copy_report(root, home, read_sources(root, targets))
        save_report(report_out, "tree-report.json", report)
    problems += report_problems(report, targets, baseline, version)
    if not problems:
        emit(
            "codacy-opengrep: canary: the 753b7aa / fc53740 shape and the intrafile taint "
            f"each one new finding; {len(targets)} files, 0 new, 0 stale"
        )
    return problems


def id_drift(listed: list[str], configured: list[str]) -> tuple[list[str], list[str]]:
    """Return the IDs LISTED adds to CONFIGURED and the IDs it removes, each sorted."""
    return sorted(set(listed) - set(configured)), sorted(set(configured) - set(listed))


def check_drift(root: Path, listing: Path) -> None:
    """Reject any listing hash, count, or ID-set drift from the mirror config."""
    raw = listing.read_bytes()
    listed = raw.decode("utf-8").splitlines()
    configured = config_ids(root)
    data = load_json(root / CONFIG)
    metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
    if (
        not isinstance(metadata, dict)
        or metadata.get("listingSha256") != hashlib.sha256(raw).hexdigest()
        or metadata.get("listingCount") != len(listed)
    ):
        raise ScanError("listing metadata hash or count differs")
    added, removed = id_drift(listed, configured)
    if listed != sorted(listed) or len(listed) != len(set(listed)) or added or removed:
        raise ScanError(f"listing drift: added={added}, removed={removed}")


def parse_today(text: str) -> date:
    """Parse --today as YYYY-MM-DD."""
    parsed = parse_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text}")
    return parsed


def emit(message: str, *, error: bool = False) -> None:
    """Write one diagnostic without routing data through a shell."""
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")


def canary_source(root: Path) -> str:
    """Plant the argv CompletedProcess shape only when its anchor is unique."""
    try:
        text = (root / CANARY_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(f"canary: cannot read {CANARY_FILE}: {err}") from err
    if text.count(CANARY_OLD) != 1:
        raise ScanError("canary: source anchor changed; re-derive the canary")
    return text.replace(CANARY_OLD, CANARY_NEW)


def cache_path(versions: dict[str, str]) -> Path:
    """Locate the content-addressed binary without contacting a host."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "horistes/opengrep" / versions["OPENGREP_SHA256"] / "opengrep"


def file_sha256(path: Path) -> str:
    """Hash a local file using the stdlib streaming digest."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def npm_stamp(root: Path) -> tuple[Path, str]:
    """Return the npm installation stamp and the current lockfile digest."""
    package = root / PACKAGE
    return package / "node_modules/.horistes-stamp", file_sha256(package / "package-lock.json")


def stamp_current(root: Path) -> bool:
    """Refuse missing, stale or unreadable npm stamps."""
    stamp, expected = npm_stamp(root)
    return stamp.is_file() and stamp.read_bytes().strip() == expected.encode("ascii")


def preferred_version(root: Path, versions: dict[str, str]) -> None:
    """Read the adapter's one literal preferredVersion without importing code."""
    text = (root / PACKAGE / ADAPTER).read_text(encoding="utf-8")
    preferred = PREFERRED_RE.findall(text)
    if preferred != [versions["OPENGREP_VERSION"]]:
        raise ScanError(
            "Opengrep pin differs from adapter preferredVersion; run make opengrep-tools"
        )


def cached_binary(versions: dict[str, str]) -> Path:
    """Require the pinned binary's bytes in the cache, including on every scan."""
    binary = cache_path(versions)
    if not binary.is_file() or file_sha256(binary) != versions["OPENGREP_SHA256"]:
        raise ScanError("Opengrep cache missing or sha256 differs; run make opengrep-tools")
    return binary


def check_install(root: Path) -> None:
    """Verify the lock stamp, exact CLI, adapter pin and cache entirely offline."""
    try:
        if not stamp_current(root):
            raise ScanError("npm stamp missing or stale")
        cli_path(root)
        versions = tool_versions(root)
        preferred_version(root, versions)
        cached_binary(versions)
    except (OSError, UnicodeDecodeError, ScanError) as err:
        raise ScanError(f"installation check failed: {err}; run make opengrep-tools") from err


def install_npm(root: Path) -> bool:
    """Install locked dependencies only when their stamp changed; return whether installed."""
    if stamp_current(root):
        return False
    npm = shutil.which("npm")
    if npm is None:
        raise ScanError("npm not found on PATH; run make opengrep-tools")
    result = run_capture(
        [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        root / PACKAGE,
        NPM_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ScanError(f"npm ci failed (exit {result.returncode})")
    cli_path(root)
    preferred_version(root, tool_versions(root))
    stamp, expected = npm_stamp(root)
    stamp.write_text(expected + "\n", encoding="utf-8")
    return True


def install_binary(versions: dict[str, str]) -> bool:
    """Atomically install verified bytes; leave an existing cache intact on failure."""
    destination = cache_path(versions)
    if destination.is_file() and file_sha256(destination) == versions["OPENGREP_SHA256"]:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="horistes-opengrep-install-", dir=destination.parent
    ) as temp:
        pending = Path(temp) / "opengrep"
        digest, size = fetch_within(
            versions["OPENGREP_URL"],
            pending,
            DOWNLOAD_DEADLINE_SECONDS,
            urllib.request.urlopen,
        )
        if digest != versions["OPENGREP_SHA256"] or file_sha256(pending) != digest:
            raise ScanError(f"Opengrep sha256 mismatch ({size} bytes)")
        pending.chmod(0o755)
        pending.replace(destination)
    return True


def install(root: Path) -> None:
    """Provision the npm lock and binary cache; a current install is a no-op."""
    npm_changed = install_npm(root)
    cli_path(root)
    versions = tool_versions(root)
    preferred_version(root, versions)
    binary_changed = install_binary(versions)
    check_install(root)
    state = (
        "installed pinned tools" if npm_changed or binary_changed else "installation current; no-op"
    )
    emit(f"codacy-opengrep: {state}")


@contextmanager
def codacy_home(versions: dict[str, str]) -> Iterator[Path]:
    """Copy and reverify the cached engine before wrapping it in the isolated home."""
    binary = cached_binary(versions)
    with tempfile.TemporaryDirectory(prefix="horistes-opengrep-") as temporary:
        home = Path(temporary) / "global"
        tools = home / "tools/Semgrep"
        tools.mkdir(parents=True)
        copied = tools / "opengrep-release"
        shutil.copyfile(binary, copied)
        if file_sha256(copied) != versions["OPENGREP_SHA256"]:
            raise ScanError("copied Opengrep sha256 mismatch")
        copied.chmod(0o755)
        wrapper = write_wrapper(tools, copied)
        result = run_capture([str(wrapper), "--version"], home, 30, cli_env(os.environ, home))
        if result.returncode != 0 or result.stdout.strip() != versions["OPENGREP_VERSION"]:
            raise ScanError(
                "cached Opengrep cannot run at its pinned version; refusing CLI auto-repair"
            )
        yield home


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse one mode and the shared date/report options."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="install pinned npm and binary tools")
    mode.add_argument("--check-install", action="store_true", help="verify installed tools offline")
    mode.add_argument("--drift", type=Path, metavar="LISTING", help="compare listing offline")
    mode.add_argument("--base-ref", metavar="REF", help="refuse growth against REF")
    parser.add_argument("--today", type=parse_today, default=datetime.now(UTC).date())
    parser.add_argument("--report-out", type=Path, metavar="DIR")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Dispatch the selected mode; report every failure and platform skip explicitly."""
    args = parse_args(argv)
    try:
        if args.drift:
            check_drift(ROOT, args.drift)
            emit("Opengrep listing: exact match")
            return 0
        system, machine = platform.system(), platform.machine()
        if system != "Linux" or machine != "x86_64":
            emit(
                "codacy-opengrep: skipped: the Opengrep pin is a Linux x64 binary "
                f"({system} {machine})"
            )
            return 0
        if args.install:
            install(ROOT)
            return 0
        if args.check_install:
            check_install(ROOT)
            return 0
        if args.base_ref:
            problems = shrink_problems(ROOT, args.base_ref, args.today)
        else:
            problems = scan(ROOT, args.today, args.report_out)
    except (ScanError, OSError, UnicodeDecodeError) as err:
        problems = [str(err)]
    for problem in problems:
        emit(f"codacy-opengrep: {problem}", error=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
