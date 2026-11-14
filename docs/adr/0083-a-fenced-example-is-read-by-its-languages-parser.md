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
| `json` | `json.loads` |
| `python`, `py` | `ast.parse` |
| `bash` | `bash -n`, reading stdin |
| `sh`, `shell` | `sh -n`, reading stdin |

The shell checkers in [tools/examples.py](../../tools/examples.py) replace
`<([A-Za-z][A-Za-z0-9_.:/-]*)>` with the captured bare word before parsing.
This preserves line numbers. A space after `<` and an empty `<>` do not
match; `test_spaced_input_redirection_passes` and
`test_empty_placeholder_fails` in
[tests/test_examples.py](../../tests/test_examples.py) cover those boundaries.

The check parses code and never runs it. Shell subprocesses receive only
`PATH` and `LC_ALL=C`, with a 30-second deadline. A missing interpreter or a
timeout fails the block. Diagnostics name the Markdown file, opening fence
line and language. These rules live in
[tools/examples.py](../../tools/examples.py); valid, malformed, missing-shell
and timeout cases live in [tests/test_examples.py](../../tests/test_examples.py).

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

The placeholder rule in [tools/examples.py](../../tools/examples.py) accepts
only word placeholders. A fence with a non-word placeholder such as `<a b>`
must be written as `text`; it cannot rely on shell placeholder substitution.
Parsing also does not prove that an example works when run, as demonstrated
by the deliberately nonzero exits in `test_valid_json_python_bash_and_sh_pass`
in [tests/test_examples.py](../../tests/test_examples.py).
