# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Fenced code examples: every fenced YAML and TOML block parses without error."""

from __future__ import annotations

import subprocess
import sys

from conftest import ROOT, Tree


def check(tree: Tree, files: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    tree.write(files)
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "fixture")
    return tree.tool("examples.py", *args)


def test_current_repository_examples_pass():
    """Every fenced YAML and TOML block in the repository Markdown files parses."""
    done = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools" / "examples.py"), "--root", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout


def test_planted_broken_toml_fails(tree: Tree):
    """A broken TOML block fails with exit 1 and names the file and line."""
    planted = {"README.md": ('# Test\n```toml\n[section\nkey = "missing bracket"\n```\n')}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "README.md:2" in done.stderr
    assert "toml" in done.stderr


def test_valid_yaml_and_toml_pass(tree: Tree):
    """A Markdown file with valid YAML and TOML blocks passes."""
    content = (
        "# Valid examples\n\n"
        "```yaml\n"
        "name: test\n"
        "items:\n"
        "  - one\n"
        "  - two\n"
        "```\n\n"
        "```toml\n"
        "[package]\n"
        'name = "demo"\n'
        'version = "1.0.0"\n'
        "```\n"
    )
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout
    assert "2 examples verified" in done.stdout


def test_broken_yaml_fails(tree: Tree):
    """A broken YAML block fails with exit 1 and names the file and line."""
    planted = {"README.md": ("# Test\n```yaml\nfoo: [bar\nbaz\n```\n")}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "README.md:2" in done.stderr
    assert "yaml" in done.stderr


def test_unfenced_or_other_languages_ignored(tree: Tree):
    """Non-YAML/TOML blocks and plain text are ignored."""
    content = (
        "# Heading\n\n"
        "```sh\n"
        "echo [broken toml or yaml: [[\n"
        "```\n\n"
        "```text\n"
        "[not a toml table\n"
        "```\n"
    )
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout
    assert "0 examples verified" in done.stdout


def test_mutation_without_tomllib_loads_planted_case_passes_proving_guard(tree: Tree):
    """Replacing tomllib.loads with pass causes the planted failure to pass (proving guard)."""
    source = (ROOT / "tools" / "examples.py").read_text(encoding="utf-8")
    mutated = source.replace("tomllib.loads(content)", "pass")
    assert mutated != source
    mutant_tool = tree.root / "mutant_examples.py"
    mutant_tool.write_text(mutated, encoding="utf-8")
    planted = {"README.md": ('# Test\n```toml\n[section\nkey = "missing bracket"\n```\n')}
    tree.write(planted)
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "fixture")
    done = subprocess.run(
        [sys.executable, "-B", str(mutant_tool), "--root", str(tree.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
