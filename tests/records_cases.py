# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Shared decision-record fixtures and plain repository builders."""

from __future__ import annotations

import subprocess

from conftest import ROOT, Tree

NO_POINTER = "names no tracked file, command, test function or link"


RESTATED = "restated code, cite the file instead:"


APP = '''\
"""Retry a forge read."""

# Run make verify BASE_REF=origin/main before a release.
# See tools/app.py read_with_retries(read) for the loop.

DEADLINE_SECONDS = timeout_for("forge")
RETRY_LIMIT = 3


def read_with_retries(read):
    for attempt in range(RETRY_LIMIT):
        if read(attempt):
            return True
    return False
'''


APP_TESTS = """\
def test_app_retries():
    assert True
"""


OLD = "docs/adr/0001-the-app-retries.md"


NEW = "docs/adr/0002-the-app-waits-for-the-forge.md"


OLD_RECORD = """\
# ADR-0001: The app retries

- **Status**: Accepted
- **Date**: 2026-11-01

## Context

A forge read fails now and then.

## Decision

The app retries a failed read three times.

## Consequences

A read takes longer when the forge is slow.
"""


NEW_RECORD = """\
# ADR-0002: The app waits for the forge

- **Status**: Accepted
- **Date**: 2026-11-16

## Context

The forge answers slowly while it recalculates.

## Decision

The app waits for the forge before it retries (`tools/app.py` `read_with_retries()`).

## Consequences

A slow forge delays a release; `test_app_retries` holds the order.
"""


UNPOINTED = "The app waits for the forge before it retries."


POINTED = "The app waits for the forge before it retries (`tools/app.py` `read_with_retries()`)."


def base_repo(tree: Tree) -> Tree:
    """A base commit on main holding the app, its test and one accepted record."""
    tree.write(
        {
            "tools/app.py": APP,
            "tests/test_app.py": APP_TESTS,
            "docs/adr/README.md": "# Decision records\n",
            OLD: OLD_RECORD,
        }
    )
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "base")
    return tree


def change(repo: Tree, files: dict[str, str], base: str = "main") -> subprocess.CompletedProcess:
    """Commit files on the topic branch, then check the records against base."""
    repo.git("checkout", "-q", "-B", "topic")
    repo.write(files)
    repo.git("add", "-A")
    repo.git("commit", "-q", "--allow-empty", "-m", "change")
    return repo.tool("records.py", "check", "--base-ref", base)


def failures(done: subprocess.CompletedProcess) -> list[str]:
    return [line for line in done.stderr.splitlines() if line.startswith("FAIL: ")]


ADR_0066 = "docs/adr/0066-a-train-write-holds-the-landing-order.md"


RECORD_FIXTURES = ROOT / "tests/fixtures/records"
