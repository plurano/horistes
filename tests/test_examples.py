# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Fenced examples: supported languages are parsed without executing their code."""

from __future__ import annotations

import shutil
import subprocess
import sys

import examples
import pytest
from conftest import ROOT, Tree

POSIX_HEREDOC_INPUTS = {
    "body": "cat <<EOF\nline\n<EOF>\nnot code (\nEOF\n",
    "after_body": "cat <<EOF\nline\n<EOF>\nnot code (\nEOF\ngit ls-remote origin <branch>\n",
    "dash": "cat <<-EOF\n\t<EOF>\nnot code (\n\tEOF\ngit ls-remote origin <branch>\n",
}
POSIX_FALSE_HEREDOC_INPUTS = {
    "single_quote": "echo '<<EOF'\necho <issue-id>\n",
    "double_quote": 'echo "<<EOF"\necho <issue-id>\n',
    "comment": "true # cat <<EOF\necho <issue-id>\n",
    "escaped": "echo \\<<EOF\necho <issue-id>\n",
    "arithmetic": "echo $((1 << 2))\necho <issue-id>\n",
    "multiline_single_quote": "echo 'a\n<<EOF'\necho <issue-id>\n",
}
SH_EXIT_INPUT = "exit 24"
SH_ALIAS_INPUT = "echo x"
SH_BRACKETS_INPUT = "echo [broken toml or yaml: [["


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
    assert "README.md:2: toml:" in done.stderr
    assert "Traceback" not in done.stderr


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
    assert "README.md:2: yaml:" in done.stderr
    assert "Traceback" not in done.stderr


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
    done = check(tree, {"README.md": f"```sh\n{SH_BRACKETS_INPUT}\n```\n"})
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
    assert f"README.md:2: {language}:" in done.stderr
    assert "Traceback" not in done.stderr


def test_valid_json_python_bash_and_sh_pass(tree: Tree):
    """Four valid examples are counted, with Python and shell code never executed."""
    content = (
        '```json\n{"a": 1}\n```\n'
        '```python\nraise RuntimeError("parse only")\n```\n'
        "```bash\nexit 23\n```\n"
        f"```sh\n{SH_EXIT_INPUT}\n```\n"
    )
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "4 examples verified" in done.stdout


@pytest.mark.parametrize(
    ("language", "content"), [("yml", "a: 1"), ("py", "x = 1"), ("shell", SH_ALIAS_INPUT)]
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
    assert "Traceback" not in done.stderr


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
    assert "Traceback" not in done.stderr


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
        if argv == ["bash", "-n"]:
            return subprocess.CompletedProcess(["bash", "-n"], 0, "", "")
        return subprocess.CompletedProcess(["sh", "-n"], 0, "", "")

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


@pytest.mark.parametrize("content", ['{"a": NaN}', "[Infinity]", "[-Infinity]"])
def test_non_json_constants_fail(tree: Tree, content: str):
    """Nonstandard numeric constants fail with a block diagnostic and no traceback."""
    done = check(tree, {"README.md": f"# Test\n```json\n{content}\n```\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md:2: json:" in done.stderr
    assert "is not JSON" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize(
    "content", ["1" * 5000, "[" * 200000 + "]" * 200000], ids=["digits", "depth"]
)
def test_json_parser_limits_fail(tree: Tree, content: str):
    """Digit and depth limits fail with a block diagnostic and no traceback."""
    done = check(tree, {"README.md": f"# Test\n```json\n{content}\n```\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md:2: json:" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize(
    "content", ["return 1", "break", "x=" + "-" * 200000 + "1"], ids=["return", "break", "complex"]
)
def test_python_statement_and_parser_limits_fail(tree: Tree, content: str):
    """Invalid statement placement and parser complexity fail without a traceback."""
    done = check(tree, {"README.md": f"# Test\n```python\n{content}\n```\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md:2: python:" in done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize(
    ("content", "language"),
    [
        pytest.param(content, language, id=f"{name}-{language}")
        for name, content in POSIX_HEREDOC_INPUTS.items()
        for language in ["bash", "sh"]
    ]
    + [pytest.param("cat <<< word\necho <name>\n", "bash", id="here_string-bash")],
)
def test_shell_heredoc_placeholders_pass(tree: Tree, language: str, content: str):
    """Heredoc bodies remain literal while placeholders in shell code are normalized."""
    done = check(tree, {"README.md": f"```{language}\n{content}```\n"})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


@pytest.mark.parametrize("language", ["bash", "sh"])
@pytest.mark.parametrize("case", POSIX_FALSE_HEREDOC_INPUTS)
def test_shell_false_heredoc_operators_pass(tree: Tree, language: str, case: str):
    """Quoted, commented, escaped and arithmetic operators do not open heredoc bodies."""
    content = POSIX_FALSE_HEREDOC_INPUTS[case]
    done = check(tree, {"README.md": f"```{language}\n{content}```\n"})
    assert done.returncode == 0, done.stderr
    assert "1 examples verified" in done.stdout


def test_sh_inputs_parse_as_posix():
    """Successful sh fixtures and tracked sh fences also parse on bash-as-sh hosts."""
    if shutil.which("shfmt") is None:
        pytest.skip("shfmt absent; CI /bin/sh is dash, which parses POSIX directly")
    inputs = [
        ("test_valid_json_python_bash_and_sh_pass", SH_EXIT_INPUT),
        ("test_language_alias_is_checked[shell]", SH_ALIAS_INPUT),
        ("test_shell_block_is_now_checked", SH_BRACKETS_INPUT),
    ]
    inputs.extend(
        (f"test_shell_heredoc_placeholders_pass[{name}-sh]", content)
        for name, content in POSIX_HEREDOC_INPUTS.items()
    )
    inputs.extend(
        (f"test_shell_false_heredoc_operators_pass[{name}-sh]", content)
        for name, content in POSIX_FALSE_HEREDOC_INPUTS.items()
    )
    for path in examples.tracked_markdown(ROOT):
        for start_line, lang, content in examples.fenced_blocks(path.read_text(encoding="utf-8")):
            if examples.CHECKERS.get(lang) is examples._check_sh:
                inputs.append((f"{path.relative_to(ROOT).as_posix()}:{start_line}", content))
    failures = []
    for source, content in inputs:
        done = subprocess.run(
            ["shfmt", "-ln", "posix"],
            input=examples._shell_input(content),
            capture_output=True,
            text=True,
            timeout=examples.DEADLINE_SECONDS,
            check=False,
        )
        if done.returncode != 0:
            first_line = next(iter(done.stderr.splitlines()), f"exit {done.returncode}")
            failures.append(f"{source}: {first_line}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("extra", [0, 1], ids=["exact", "over"])
def test_example_size_boundary(tree: Tree, extra: int):
    """Exactly the character limit passes and one extra character fails as a block problem."""
    content = '"' + "a" * (examples.MAX_EXAMPLE_CHARS - 2 + extra) + '"'
    markdown = f"# Test\n```json\n{content}\n```\n"
    assert len(examples.fenced_blocks(markdown)[0][2]) == examples.MAX_EXAMPLE_CHARS + extra
    done = check(tree, {"README.md": markdown})
    if extra:
        assert done.returncode == 1, done.stdout
        assert "README.md:2: json:" in done.stderr
        assert f"longer than {examples.MAX_EXAMPLE_CHARS} characters" in done.stderr
        assert "Traceback" not in done.stderr
    else:
        assert done.returncode == 0, done.stderr
        assert "1 examples verified" in done.stdout


def test_json_depth_limit_is_a_problem():
    """Direct JSON parser depth failures become diagnostics even without the size guard."""
    assert examples._check_json("[" * 200000 + "]" * 200000) is not None


@pytest.mark.parametrize("error", [SyntaxError, ValueError, RecursionError, MemoryError])
def test_python_parser_error_is_a_problem(monkeypatch, error):
    """Compiler failures become parser diagnostics without running the code object."""

    def failed_compile(*args, **kwargs):
        raise error("source too complex")

    monkeypatch.setattr(examples, "compile", failed_compile, raising=False)
    assert examples._check_python("x = 1") == "source too complex"


@pytest.mark.parametrize(
    "content",
    [
        "cat <<'ONE' <<-\"TWO\"\r\n<ONE>\r\nONE\r\n\t<TWO>\r\n\tTWO\r\necho <name>\r\n",
        "echo '<<X' \"#\" <<EOF\n<a>\nEOF\necho <name>\n",
        "cat <<EOF # <<X\n<a>\nEOF\necho <name>\n",
        'echo "a\\"\n<<X" <<EOF\n<a>\nEOF\necho <name>\n',
        "echo $# ${#x} <<EOF\n<a>\nEOF\necho <name>\n",
    ],
    ids=["fifo_crlf_tabs", "quoted_operators", "comment", "escaped_quote", "parameter_hash"],
)
def test_shell_input_preserves_pending_heredocs(content: str):
    """Real heredoc bodies stay byte for byte; quoted, commented or `$#` text queues none."""
    expected = content.replace("echo <name>", "echo name")
    assert examples._shell_input(content) == expected


def test_oversized_example_skips_parser(tree: Tree, monkeypatch):
    """An oversized supported block is counted and refused before its parser runs."""

    def unexpected_parser(content):
        pytest.fail("oversized block reached parser")

    tree.write({"README.md": "```json\n" + "1" * (examples.MAX_EXAMPLE_CHARS + 1) + "\n```\n"})
    monkeypatch.setitem(examples.CHECKERS, "json", unexpected_parser)
    problems, count = examples.check_examples(tree.root, [tree.root / "README.md"])
    assert count == 1
    assert problems == [f"README.md:1: json: longer than {examples.MAX_EXAMPLE_CHARS} characters"]
