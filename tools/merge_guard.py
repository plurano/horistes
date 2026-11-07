#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Refuse a pull request merge whose merge state is not CLEAN.

    merge_guard.py check <number> [--accept STATE ...]

The forge reports a merge state for every pull request. CLEAN means every
required check passed and no rule blocks; anything else means at least one
check or rule is not satisfied. A session that lands a pull request through
`gh pr merge` bypasses the merge train and its state check, so this guard
runs the same check locally.

Exit codes: 0 CLEAN (or an accepted state), 1 refused, 3 cannot read.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

DEADLINE_SECONDS = 30


def merge_state(number: int) -> str:
    """The merge state the forge reports for one pull request."""
    proc = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "mergeStateStatus",
            "--jq",
            ".mergeStateStatus",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=DEADLINE_SECONDS,
    )
    if proc.returncode != 0:
        raise OSError(f"gh pr view failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check",))
    parser.add_argument("number", type=int)
    parser.add_argument(
        "--accept",
        nargs="*",
        default=[],
        help="states accepted besides CLEAN",
    )
    args = parser.parse_args(argv)
    allowed = {"CLEAN"} | {s.upper() for s in args.accept}
    try:
        state = merge_state(args.number)
    except (OSError, subprocess.TimeoutExpired) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    if state in allowed:
        ciwf.say(f"PASS: PR {args.number} merge state {state}")
        return 0
    ciwf.complain(f"FAIL: PR {args.number} merge state {state}, not one of {sorted(allowed)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
