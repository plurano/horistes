# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Common support for the offline Codacy Opengrep gate.

adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at 21f590a3535f (EUPL-1.2)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
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


CANARY_WANT = (
    (AUDIT_RULE, CANARY_FILE),
    (TAINT_RULE, INTRAFILE_FILE),
    (AUDIT_RULE, INTRAFILE_FILE),
)


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


LITERAL_RE = re.compile(
    r'"""(?:\\.|(?!""")[^\\]|\\\Z)*(?:"""|\Z)'
    r"|'''(?:\\.|(?!''')[^\\]|\\\Z)*(?:'''|\Z)"
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


def run_checked(
    tool: str, call: Callable[[], subprocess.CompletedProcess[str]]
) -> subprocess.CompletedProcess[str]:
    """Map process creation and deadline failures through one error boundary."""
    try:
        return call()
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ScanError(f"{tool} did not complete: {err}") from err


def git_env(environ: dict[str, str]) -> dict[str, str]:
    """Return ENVIRON without GIT_* variables: GIT_DIR, GIT_INDEX_FILE and the like would point
    git elsewhere.
    """
    return {key: value for key, value in environ.items() if not key.startswith("GIT_")}


def git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run git with isolated repository selection and a fixed deadline."""
    if shutil.which("git") is None:
        raise ScanError("git not found on PATH")
    return run_checked(
        "git",
        lambda: subprocess.run(
            ["git", *args],
            cwd=root,
            env=git_env(dict(os.environ)),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        ),
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


def pretty(key: str) -> str:
    """Render a NUL-joined key for a diagnostic."""
    return key.replace("\0", " | ")


def cli_env(environ: Mapping[str, str], home: Path) -> dict[str, str]:
    """Isolate CLI git selection and Codacy configuration in the scratch home."""
    return {
        **{
            key: value
            for key, value in git_env(dict(environ)).items()
            if not key.startswith("CODACY_")
        },
        "CODACY_GLOBAL_DIR": str(home),
        "SEMGREP_LOG_FILE": str(home / "opengrep.log"),
    }


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


def emit(message: str, *, error: bool = False) -> None:
    """Write one diagnostic without routing data through a shell."""
    stream = sys.stderr if error else sys.stdout
    stream.write(message + "\n")


def npm_run(root: Path) -> subprocess.CompletedProcess[str]:
    """Install exactly the lockfile, without lifecycle scripts or registry extras."""
    return run_checked(
        "npm",
        lambda: subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=root / PACKAGE,
            capture_output=True,
            text=True,
            timeout=NPM_TIMEOUT_SECONDS,
            check=False,
        ),
    )


def engine_run(wrapper: Path, home: Path) -> subprocess.CompletedProcess[str]:
    """Check the wrapped engine under the same environment as analysis."""
    return run_checked(
        "sh",
        lambda: subprocess.run(
            ["sh", str(wrapper), "--version"],
            cwd=home,
            env=cli_env(os.environ, home),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        ),
    )


def cli_run(
    root: Path, cli: Path, cwd: Path, home: Path, report: Path
) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI with the checked-in rules and fixed offline options."""
    return run_checked(
        "node",
        lambda: subprocess.run(
            [
                "node",
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
            ],
            cwd=cwd,
            env=cli_env(os.environ, home),
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        ),
    )


MAX_SOURCE_BYTES = 1_000_000
MAX_REPORT_ITEMS = 2000
MAX_PACKAGES = 512
LOCK_TRIES = 100
LOCK_SLEEP_SECONDS = 0.1
INSTALL_LOCK = PACKAGE + "/.install.lock"
