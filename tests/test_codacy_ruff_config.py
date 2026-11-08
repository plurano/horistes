# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Codacy's Ruff reads the rule set the repository's ruff reads.

Codacy's wrapper runs `ruff check --config <file>` with the first of
pyproject.toml, ruff.toml or .ruff.toml it lists at the root
(codacy/codacy-ruff, src/codacy_ruff.py, find_ruff_configFile). The
listing order is the file system's, so each of them that exists must
resolve to the settings of ruff.toml.
"""

from __future__ import annotations

import pytest
from conftest import ROOT
from test_lint import PY_GOOD, PY_ISORT_BLOCK, ruff

CODACY_PICKS = ("pyproject.toml", "ruff.toml", ".ruff.toml")
SECTION = "[tool.ruff]"


def root_configs() -> dict[str, str]:
    """Each root config file Codacy's Ruff may pick, as committed."""
    present = [name for name in CODACY_PICKS if (ROOT / name).is_file()]
    return {name: (ROOT / name).read_text(encoding="utf-8") for name in present}


def without_ruff_section(text: str) -> str:
    """pyproject.toml as it stood before it pointed at ruff.toml."""
    assert text.count(SECTION) == 1
    return text.split(SECTION)[0]


def settings(tree, config: str) -> list[str]:
    """Ruff's resolved settings with this config file, less the line naming the file."""
    shown = ruff(tree, "check", "--no-cache", "--config", config, "--show-settings", "sample.py")
    assert shown.returncode == 0, shown.stdout + shown.stderr
    return [line for line in shown.stdout.splitlines() if not line.startswith("Settings path:")]


def drift(tree) -> list[str]:
    """The config files in the tree whose settings differ from those of ruff.toml."""
    tree.write({"sample.py": PY_GOOD})
    expected = settings(tree, "ruff.toml")
    present = [name for name in CODACY_PICKS if (tree.root / name).is_file()]
    return [name for name in present if settings(tree, name) != expected]


def test_every_root_config_codacy_may_pick_resolves_to_ruff_toml(tree):
    """Each committed root config gives ruff the settings of ruff.toml."""
    configs = root_configs()
    assert "ruff.toml" in configs
    assert drift(tree.write(configs)) == []


def test_planted_pyproject_without_ruff_section_drifts(tree):
    """A pyproject.toml with no [tool.ruff] runs ruff on its defaults."""
    configs = root_configs()
    configs["pyproject.toml"] = without_ruff_section(configs["pyproject.toml"])
    assert drift(tree.write(configs)) == ["pyproject.toml"]
    assert "linter.line_length = 88" in settings(tree, "pyproject.toml")


@pytest.mark.parametrize(
    ("planted", "drifted"),
    [
        ('extend = "ruff.toml"\n', []),
        ('extend = "ruff.toml"\nline-length = 88\n', [".ruff.toml"]),
        ("line-length = 100\n", [".ruff.toml"]),
    ],
    ids=["extends-ruff-toml", "extends-and-overrides", "stands-alone"],
)
def test_a_third_root_config_must_extend_ruff_toml(tree, planted, drifted):
    """A .ruff.toml beside the others passes only when it adds nothing to ruff.toml."""
    assert drift(tree.write({**root_configs(), ".ruff.toml": planted})) == drifted


def test_codacy_style_run_sorts_tomllib_as_stdlib_both_directions(tree):
    """The import block Codacy flagged passes through pyproject.toml, and fails on 3.10.

    Codacy pins ruff 0.16.8; ruff 0.16 defaults to target 3.10 (measured on
    0.16.10), and tomllib is stdlib from 3.11 on. The planted case names
    that target, so the result holds on any ruff version.
    """
    configs = {**root_configs(), "sample.py": PY_ISORT_BLOCK}
    codacy = ("check", "--no-cache", "--config", "pyproject.toml", "--select", "I001", "sample.py")
    passes = ruff(tree.write(configs), *codacy)
    assert passes.returncode == 0, passes.stdout + passes.stderr

    cut = without_ruff_section(configs["pyproject.toml"])
    configs["pyproject.toml"] = f'{cut}{SECTION}\ntarget-version = "py310"\n'
    fails = ruff(tree.write(configs), *codacy)
    assert fails.returncode != 0
    assert "I001" in fails.stdout


def test_a_ruff_toml_ruff_refuses_fails_instead_of_matching(tree):
    """Ruff refusing every config gives equal empty settings; that fails, never passes."""
    configs = {**root_configs(), "ruff.toml": "not-a-setting = 1\n"}
    with pytest.raises(AssertionError, match="not-a-setting"):
        drift(tree.write(configs))
