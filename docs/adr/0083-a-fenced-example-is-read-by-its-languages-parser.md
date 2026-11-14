<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0083: A fenced example is read by its language's parser

- **Status**: Accepted
- **Date**: 2026-11-14
- **Authors**: Lusoris

## Context

Gap audit C15-02 identified missing link and example checks. PR 62
(`a68dbc0`) added [tools/links.py](../../tools/links.py), and PR 122
(`1e035b6`) added [tools/examples.py](../../tools/examples.py) for YAML and
TOML. Both checks run through [Makefile](../../Makefile).

At `b511cf9`, tracked Markdown opened 24 bash fences, 7 sh fences and 2 JSON
fences, with no Python fence. The example checker ignored those languages.
The planted malformed JSON, Python, bash and sh examples now held in
[tests/test_examples.py](../../tests/test_examples.py) each returned exit 0
and `0 examples verified` with that version of
[tools/examples.py](../../tools/examples.py).

The generated [.paperclip/rules.md](../../.paperclip/rules.md) push example
ends in `HEAD:refs/heads/paperclip/<issue-id>`. Reading it unchanged with
`bash -n` fails because the placeholder looks like shell redirection.
`test_paperclip_placeholder_passes` in
[tests/test_examples.py](../../tests/test_examples.py) keeps that example
byte for byte inside a bash fence.

## Decision

[tools/examples.py](../../tools/examples.py) dispatches each supported fence
through `CHECKERS` to its language's parser:

| Fence language | Parser |
| :--- | :--- |
| `yaml`, `yml` | PyYAML `safe_load_all` |
| `toml` | `tomllib.loads` |
| `json` | `json.loads`, refusing `NaN`, `Infinity` and `-Infinity` |
| `python`, `py` | `compile` to a code object that is never run (syntax and statement placement) |
| `bash` | `bash -n`, reading stdin |
| `sh`, `shell` | `sh -n`, reading stdin |

The shell checkers in [tools/examples.py](../../tools/examples.py) replace
`<([A-Za-z][A-Za-z0-9_.:/-]*)>` with the captured bare word before parsing.
Substitution skips heredoc bodies, preserving their contents and line numbers;
`test_shell_heredoc_placeholders_pass` and
`test_shell_input_preserves_pending_heredocs` in
[tests/test_examples.py](../../tests/test_examples.py) cover placeholders after
heredoc terminators, tab-stripped terminators, quoted delimiters and here-strings.
A space after `<` and an empty `<>` do not
match; `test_spaced_input_redirection_passes` and
`test_empty_placeholder_fails` in
[tests/test_examples.py](../../tests/test_examples.py) cover those boundaries.

The check parses code and never runs it. Shell subprocesses receive only
`PATH` and `LC_ALL=C`, with a 30-second deadline. A missing interpreter or a
timeout fails the block. Diagnostics name the Markdown file, opening fence
line and language. These rules live in
[tools/examples.py](../../tools/examples.py); valid, malformed, missing-shell
and timeout cases live in [tests/test_examples.py](../../tests/test_examples.py).

Supported blocks above `MAX_EXAMPLE_CHARS` (100 000 characters) fail before
parsing and still count as checked examples. A parser's depth or digit limit
is a problem of the block, never a traceback. The size guard and parser error
handling live in [tools/examples.py](../../tools/examples.py);
`test_example_size_boundary`, `test_oversized_example_skips_parser`,
`test_json_parser_limits_fail`, `test_json_depth_limit_is_a_problem`,
`test_python_statement_and_parser_limits_fail` and
`test_python_parser_error_is_a_problem` in
[tests/test_examples.py](../../tests/test_examples.py) cover these limits.

## Consequences

Malformed examples in all supported languages now fail the existing example
check in [Makefile](../../Makefile). The tests in
[tests/test_examples.py](../../tests/test_examples.py) require each block to
be counted, including aliases, and check refusal without depending on shell
error wording. Languages without a parser remain ignored.

Shell syntax follows the interpreter installed on the runner. `sh` is dash
on the Ubuntu runner selected by
[.github/workflows/ci.yml](../../.github/workflows/ci.yml), while the
2026-11-14 workstation check used bash for `sh`. The shell tests in
[tests/test_examples.py](../../tests/test_examples.py) therefore assert file,
line, language and status rather than interpreter-specific messages.

Where `/bin/sh` is bash, `sh -n` also accepts bash syntax in `sh` and `shell`
blocks: a here-string (`<<<`) in an `sh` test case passed the workstation
gate and failed under dash on CI. `test_sh_inputs_parse_as_posix` in
[tests/test_examples.py](../../tests/test_examples.py) therefore also parses
every `sh` pass case and every tracked `sh` or `shell` block with
`shfmt -ln posix`. Where shfmt is not installed it skips with that reason,
and CI's dash parses POSIX directly.

The gate already needs a POSIX shell (`SHELL := /bin/sh` at
[Makefile:5](../../Makefile#L5)), and
[.github/workflows/ci.yml](../../.github/workflows/ci.yml) runs only on
`ubuntu-26.04`. A missing `bash` or `sh` therefore continues to fail the block,
never skip it; `test_missing_shell_interpreter_fails` in
[tests/test_examples.py](../../tests/test_examples.py) keeps that platform rule.

The placeholder rule in [tools/examples.py](../../tools/examples.py) accepts
only word placeholders. A fence with a non-word placeholder such as `<a b>`
must be written as `text`; it cannot rely on shell placeholder substitution.
Parsing also does not prove that an example works when run, as demonstrated
by the deliberately nonzero exits in `test_valid_json_python_bash_and_sh_pass`
in [tests/test_examples.py](../../tests/test_examples.py).
