# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Scan support for the offline Codacy Opengrep gate.

adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at 21f590a3535f (EUPL-1.2)
"""

from __future__ import annotations

import argparse
import platform
import sys
import tempfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

from codacy_opengrep_baseline import baseline_counts, load_baseline, parse_date, shrink_problems
from codacy_opengrep_common import (
    CANARY_FILE,
    CANARY_NEW,
    CANARY_OLD,
    FIXTURES,
    INTRAFILE_FILE,
    INTRAFILE_SOURCE,
    ROOT,
    ScanError,
    check_drift,
    cli_run,
    emit,
    git,
    load_json,
)
from codacy_opengrep_install import (
    check_install,
    cli_path,
    codacy_home,
    install,
    install_lock,
    tool_versions,
)
from codacy_opengrep_report import (
    canary_problems,
    read_sources,
    report_problems,
    save_report,
    tracked_files,
)


def remove_generated_config(cwd: Path) -> None:
    """Remove the ignored rule file the CLI writes for every scan, and its empty parents."""
    generated = cwd / ".codacy/generated/Semgrep/opengrep-rules.yaml"
    generated.unlink(missing_ok=True)
    for directory in (generated.parent, generated.parent.parent, generated.parent.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            return


def live_report(root: Path, cwd: Path, home: Path) -> dict[str, object]:
    """Run the pinned CLI over every file of the scan copy CWD with the verified Opengrep under
    HOME; return its report.

    No file name reaches the command line: the CLI lists the copy with git ls-files.
    """
    cli = cli_path(root)
    with tempfile.TemporaryDirectory(prefix="horistes-opengrep-report-") as temporary:
        report = Path(temporary) / "report.json"
        try:
            result = cli_run(root, cli, cwd, home, report)
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


def canary_check(
    root: Path, home: Path, baseline: Counter[str], version: str, report_out: Path | None
) -> list[str]:
    """Replay the planted 753b7aa / fc53740 shape live and return why it was not exactly one
    new finding.
    """
    try:
        canary = canary_report(root, home)
    except ScanError as err:
        # The tree scan still runs: a planted tree also breaks the canary derivation.
        return [str(err)]
    save_report(report_out, "canary-report.json", canary)
    return canary_problems(canary, baseline, version)


def scan(
    root: Path, today: date, report_out: Path | None, base_ref: str | None = None
) -> list[str]:
    """Run the canary first, then scan every tracked Python file; return the failures."""
    with install_lock(root):
        versions = tool_versions(root)
        version = versions["OPENGREP_VERSION"]
        snapshot = load_baseline(root, today)
        baseline = baseline_counts(snapshot)
        targets = tracked_files(root)
        check_install(root)
        with codacy_home(versions) as home:
            problems = canary_check(root, home, baseline, version, report_out)
            report = copy_report(root, home, read_sources(root, targets))
            save_report(report_out, "tree-report.json", report)
        problems += report_problems(report, targets, baseline, version)
        if base_ref is not None:
            problems += shrink_problems(root, base_ref, snapshot)
        if not problems:
            emit(
                "codacy-opengrep: canary: the 753b7aa / fc53740 shape and the intrafile taint "
                f"each one new finding; {len(targets)} files, 0 new, 0 stale, "
                f"baseline {len(snapshot)}"
            )
        return problems


def parse_today(text: str) -> date:
    """Parse --today as YYYY-MM-DD."""
    parsed = parse_date(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text}")
    return parsed


def canary_source(root: Path) -> str:
    """Plant the argv CompletedProcess shape only when its anchor is unique."""
    try:
        text = (root / CANARY_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        raise ScanError(f"canary: cannot read {CANARY_FILE}: {err}") from err
    if text.count(CANARY_OLD) != 1:
        raise ScanError("canary: source anchor changed; re-derive the canary")
    return text.replace(CANARY_OLD, CANARY_NEW)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse one mode and the shared date/report options."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--install", action="store_true", help="install pinned npm and binary tools")
    mode.add_argument("--check-install", action="store_true", help="verify installed tools offline")
    mode.add_argument("--drift", type=Path, metavar="LISTING", help="compare listing offline")
    parser.add_argument("--base-ref", metavar="REF", help="refuse growth against REF")
    parser.add_argument("--today", type=parse_today, default=datetime.now(UTC).date())
    parser.add_argument("--report-out", type=Path, metavar="DIR")
    args = parser.parse_args(argv)
    if args.base_ref and (args.install or args.check_install or args.drift):
        parser.error("--base-ref is a scan option")
    return args


def listing_for(args: argparse.Namespace) -> Path | None:
    """Return the listing the selected mode compares with the config, None for install modes.

    The comparison reads only checked-in files, so it runs before the platform skip.
    """
    if args.drift:
        return args.drift
    if args.install or args.check_install:
        return None
    return ROOT / FIXTURES / "enabled-python-ids.txt"


def main(argv: list[str]) -> int:
    """Dispatch the selected mode; report every failure and platform skip explicitly."""
    args = parse_args(argv)
    try:
        listing = listing_for(args)
        if listing is not None:
            check_drift(ROOT, listing)
            emit("Opengrep listing: exact match")
        if args.drift:
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
            with install_lock(ROOT):
                check_install(ROOT)
            return 0
        problems = scan(ROOT, args.today, args.report_out, args.base_ref)
    except (ScanError, OSError, UnicodeDecodeError) as err:
        problems = [str(err)]
    for problem in problems:
        emit(f"codacy-opengrep: {problem}", error=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
