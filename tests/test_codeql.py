# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""CodeQL runs on every pull request: its pull_request trigger has no path filter."""

from __future__ import annotations

import copy

import pytest
import yaml
from conftest import ROOT

WORKFLOW = ROOT / ".github/workflows/codeql.yml"


def triggers(doc: dict) -> dict:
    """Return the trigger mapping; YAML 1.1 loads the key `on` as True."""
    on = doc["on"] if "on" in doc else doc.get(True)
    assert isinstance(on, dict), "the workflow declares no trigger mapping"
    return on


def every_pull_request(doc: dict) -> bool:
    """Return whether the pull_request trigger exists and carries no path filter."""
    on = triggers(doc)
    if "pull_request" not in on:
        return False
    pull = on["pull_request"] or {}
    return "paths" not in pull and "paths-ignore" not in pull


def load() -> dict:
    """Load the CodeQL workflow with the safe loader."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_codeql_runs_on_every_pull_request() -> None:
    """The CodeQL workflow runs on every pull request, unfiltered by path."""
    assert every_pull_request(load())


@pytest.mark.parametrize("key", ["paths", "paths-ignore"])
def test_predicate_refuses_planted_path_filters(key: str) -> None:
    """A planted path filter on the pull_request trigger is refused."""
    planted = copy.deepcopy(load())
    on = triggers(planted)
    on["pull_request"] = dict(on["pull_request"] or {}, **{key: ["docs/**"]})
    assert not every_pull_request(planted)
