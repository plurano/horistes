# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests read the outside linters the Makefile passes through conftest.external and rooted only.

The Makefile passes each tool as a path relative to the checkout
(`REUSE=.venv/bin/reuse`). A test that reads such a variable itself and runs
the tool in a planted tree resolves the path against that tree and fails under
`make test` while a bare pytest run, with the variable unset, passes.
"""

from __future__ import annotations

import ast
import re

import pytest
from conftest import ROOT

DIRECT_CALLS = frozenset({"os.environ.get", "os.getenv", "environ.get", "getenv"})
DIRECT_MAPS = frozenset({"os.environ", "environ"})


def tool_variables(makefile: str) -> frozenset[str]:
    """The variables the `test` recipe of the Makefile sets to a tool path."""
    recipe = makefile.split("\ntest:", 1)[1].split("\n\n", 1)[0]
    return frozenset(re.findall(r"\b([A-Z][A-Z0-9_]*)=\"\$\(", recipe))


def environ_reads(source: str, names: frozenset[str]) -> list[int]:
    """The lines of `source` that read one of `names` from the environment directly."""
    tree = ast.parse(source)
    os_names: set[str] = set()
    environ_names: set[str] = set()
    getenv_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    os_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name == "environ":
                    environ_names.add(alias.asname or alias.name)
                elif alias.name == "getenv":
                    getenv_names.add(alias.asname or alias.name)

    call_targets = DIRECT_CALLS | {
        *(f"{n}.environ.get" for n in os_names),
        *(f"{n}.getenv" for n in os_names),
        *(f"{n}.get" for n in environ_names),
        *getenv_names,
    }
    map_targets = DIRECT_MAPS | {
        *(f"{n}.environ" for n in os_names),
        *environ_names,
    }

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            key = node.args[0] if node.args else None
            direct = ast.unparse(node.func) in call_targets
        elif isinstance(node, ast.Subscript):
            key = node.slice
            direct = ast.unparse(node.value) in map_targets
        else:
            continue
        if direct and isinstance(key, ast.Constant) and key.value in names:
            found.append(node.lineno)
    return sorted(found)


NAMES = tool_variables((ROOT / "Makefile").read_text(encoding="utf-8"))


def test_makefile_test_recipe_passes_tool_variables():
    assert {"REUSE", "ZIZMOR", "ACTIONLINT", "RUFF"} <= NAMES
    assert "PYTEST_ADDOPTS" not in NAMES


def test_no_test_reads_a_tool_variable_directly():
    files = sorted(path for path in (ROOT / "tests").glob("*.py") if path.name != "conftest.py")
    assert files
    found = {
        path.name: lines
        for path in files
        if (lines := environ_reads(path.read_text(encoding="utf-8"), NAMES))
    }
    assert found == {}


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ('import os\nos.environ.get("REUSE")\n', [2]),
        ('import os\ntool = os.environ["ZIZMOR"]\n', [2]),
        ('from os import getenv\n\ngetenv("ACTIONLINT", "actionlint")\n', [3]),
        ('import os\nos.environ.get("ZIZMOR_OFFLINE")\n', []),
        ('tool = external("REUSE", "reuse")\n', []),
        ("text = 'os.environ.get(\"REUSE\")'\n", []),
        ('import os as x\nx.environ.get("REUSE")\n', [2]),
        ('import os as x\nx.getenv("REUSE")\n', [2]),
        ('import os as x\ntool = x.environ["REUSE"]\n', [2]),
        ('from os import environ as e\ne.get("REUSE")\n', [2]),
        ('from os import environ as e\ntool = e["REUSE"]\n', [2]),
        ('from os import getenv as g\ng("REUSE")\n', [2]),
        ('import os as x\nx.environ.get("OTHER_VAR")\n', []),
        ('from os import getenv as g\ng("OTHER_VAR")\n', []),
    ],
    ids=[
        "get",
        "subscript",
        "getenv",
        "other-variable",
        "helper",
        "string",
        "alias-os-environ-get",
        "alias-os-getenv",
        "alias-os-subscript",
        "alias-environ-get",
        "alias-environ-subscript",
        "alias-getenv",
        "alias-os-other-variable",
        "alias-getenv-other-variable",
    ],
)
def test_environ_reads_3d(source, lines):
    assert environ_reads(source, NAMES) == lines
