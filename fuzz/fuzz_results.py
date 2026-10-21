# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Fuzz target for results.from_sarif."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

with atheris.instrument_imports():
    import results  # noqa: E402


def TestOneInput(data: bytes) -> None:  # noqa: N802
    """from_sarif returns a dict or raises ResultError; anything else is a crash."""
    with contextlib.suppress(results.ResultError):
        results.from_sarif(data)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
