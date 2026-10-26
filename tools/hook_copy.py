# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Compare caller hook copies against the authoritative horistes source files."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

DEFAULT_NAMES = ("commit-msg.sh", "commit-msg_test.sh")
MAX_FILES = 16


def _sha256(path: Path) -> str:
    """Compute the SHA-256 digest of a file."""
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def check_copies(source_dir: Path, copy_dir: Path, names: list[str]) -> int:
    """Compare listed files in copy_dir against source_dir.

    Returns:
        0: all equal
        1: drift, missing copy, or symlinked copy
        2: usage or IO error on source
    """
    if not source_dir.is_dir():
        sys.stderr.write(f"ERROR: source directory does not exist: {source_dir}\n")
        return 2

    if len(names) > MAX_FILES:
        sys.stderr.write(f"ERROR: file count exceeds maximum of {MAX_FILES}\n")
        return 2

    drift = False
    for name in names[:MAX_FILES]:
        source_file = source_dir / name
        copy_file = copy_dir / name

        if not source_file.is_file():
            sys.stderr.write(f"ERROR: source file does not exist: {source_file}\n")
            return 2

        if copy_file.is_symlink():
            sys.stderr.write(f"FAIL: {name}: copy is a symlink\n")
            drift = True
            continue

        if not copy_file.is_file():
            sys.stderr.write(f"FAIL: {name}: copy does not exist: {copy_file}\n")
            drift = True
            continue

        try:
            source_digest = _sha256(source_file)
            copy_digest = _sha256(copy_file)
        except OSError as err:
            sys.stderr.write(f"ERROR: reading files failed: {err}\n")
            return 2

        if source_digest != copy_digest:
            sys.stderr.write(
                f"FAIL: {name}: digest drift\n"
                f"  source sha256: {source_digest}\n"
                f"  copy   sha256: {copy_digest}\n"
            )
            drift = True

    if drift:
        return 1

    sys.stdout.write(f"PASS: {len(names)} hook file(s) identical\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entrypoint for the hook copy checker."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Check hook copies against source")
    check_parser.add_argument("--source", type=Path, required=True, help="Source hooks directory")
    check_parser.add_argument("--copy", type=Path, required=True, help="Caller hooks directory")
    check_parser.add_argument(
        "--name",
        nargs="+",
        action="extend",
        default=None,
        help="Hook file names to check (default: commit-msg.sh, commit-msg_test.sh)",
    )

    args = parser.parse_args(argv)
    if args.command == "check":
        names = args.name if args.name is not None else list(DEFAULT_NAMES)
        try:
            return check_copies(args.source, args.copy, names)
        except OSError as err:
            sys.stderr.write(f"ERROR: {err}\n")
            return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
