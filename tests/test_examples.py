# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Fenced examples: supported languages are parsed without executing their code."""

from __future__ import annotations

import subprocess
import sys

import examples
import pytest
from conftest import ROOT, Tree


def check(tree: Tree, files: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    tree.write(files)
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "fixture")
    return tree.tool("examples.py", *args)


def test_current_repository_examples_pass():
    """Every supported fenced block in the repository Markdown files parses."""
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs examples on ROOT
    done = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools" / "examples.py"), "--root", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
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


def test_languages_without_a_parser_are_ignored(tree: Tree):
    """Text and diff blocks without a registered parser are ignored."""
    content = (
        "# Heading\n\n"
        "```diff\n"
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


def test_shell_block_is_now_checked(tree: Tree):
    """A shell command containing unmatched data brackets is valid and counted."""
    done = check(tree, {"README.md": "```sh\necho [broken toml or yaml: [[\n```\n"})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


@pytest.mark.parametrize(
    ("language", "content"),
    [
        ("json", '{"a": 1,}'),
        ("python", "def f(:"),
        ("bash", "if true; then echo x"),
        ("sh", "for x in a b; do echo $x"),
    ],
)
def test_planted_broken_example_fails(tree: Tree, language: str, content: str):
    """A broken example fails and names the opening fence and language."""
    done = check(tree, {"README.md": f"# Test\n```{language}\n{content}\n```\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md:2" in done.stderr
    assert f": {language}:" in done.stderr


def test_valid_json_python_bash_and_sh_pass(tree: Tree):
    """Four valid examples are counted, with Python and shell code never executed."""
    content = (
        '```json\n{"a": 1}\n```\n'
        '```python\nraise RuntimeError("parse only")\n```\n'
        "```bash\nexit 23\n```\n"
        "```sh\nexit 24\n```\n"
    )
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "4 examples verified" in done.stdout


@pytest.mark.parametrize(
    ("language", "content"), [("yml", "a: 1"), ("py", "x = 1"), ("shell", "echo x")]
)
def test_language_alias_is_checked(tree: Tree, language: str, content: str):
    """Each supported alias dispatches to its parser and counts the block."""
    done = check(tree, {"README.md": f"```{language}\n{content}\n```\n"})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


def test_paperclip_placeholder_passes(tree: Tree):
    """The generated paperclip push example parses with its word placeholder."""
    content = "```bash\ngit push origin HEAD:refs/heads/paperclip/<issue-id>\n```\n"
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


def test_spaced_input_redirection_passes(tree: Tree):
    """A space after < keeps input redirection valid without opening its file."""
    done = check(tree, {"README.md": "```bash\ncat < input.txt\n```\n"})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


def test_empty_placeholder_fails(tree: Tree):
    """Empty <> stays an incomplete redirection and bash refuses it."""
    done = check(tree, {"README.md": "```bash\necho <>\n```\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md:1: bash:" in done.stderr


@pytest.mark.parametrize("language", ["bash", "sh"])
def test_missing_shell_interpreter_fails(tree: Tree, tmp_path, language: str):
    """A missing interpreter fails even when the Markdown path is explicit."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    tree.write({"README.md": f"```{language}\necho x\n```\n"})
    done = tree.tool("examples.py", "check", "README.md", env={"PATH": str(empty_bin)})
    assert done.returncode == 1, done.stdout
    assert f"{language} not found" in done.stderr
    assert f"README.md:1: {language}:" in done.stderr


@pytest.mark.parametrize("checker", [examples._check_bash, examples._check_sh])
def test_shell_parse_call_is_isolated(monkeypatch, checker):
    """Shell parsing uses literal syntax-only argv, normalized stdin and a minimal env."""

    def parsed(argv, **kwargs):
        assert argv in (["bash", "-n"], ["sh", "-n"])
        assert kwargs["input"] == "echo issue-id\ncat < input.txt"
        assert kwargs["timeout"] == 30
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        assert set(kwargs["env"]) == {"PATH", "LC_ALL"}
        assert kwargs["env"]["LC_ALL"] == "C"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(examples.subprocess, "run", parsed)
    assert checker("echo <issue-id>\ncat < input.txt") is None


@pytest.mark.parametrize("checker", [examples._check_bash, examples._check_sh])
def test_shell_timeout_is_a_problem(monkeypatch, checker):
    """An interpreter deadline becomes a parse problem rather than a file error."""

    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("shell", 30)

    monkeypatch.setattr(examples.subprocess, "run", timed_out)
    assert checker("echo x") == "timed out after 30 s"


@pytest.mark.parametrize(
    ("stderr", "expected"), [("\n  \nsyntax problem\nmore\n", "syntax problem"), ("", "exit 2")]
)
def test_shell_error_uses_first_nonempty_line_or_exit(stderr: str, expected: str):
    """A failed shell reports its first nonempty error line or its exit code."""
    done = subprocess.CompletedProcess(["sh", "-n"], 2, "", stderr)
    assert examples._shell_error(done) == expected


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
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs mutant on tree
    done = subprocess.run(
        [sys.executable, "-B", str(mutant_tool), "--root", str(tree.root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
