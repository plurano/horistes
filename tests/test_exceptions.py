# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions: an incomplete entry, an expired or too long one and a disabled rule all fail."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from conftest import ROOT, Tree

CONFIG = ".github/zizmor.yml"
RUFF = "ruff.toml"
GOOD = (
    "---\nrules:\n  concurrency-limits:\n    ignore:\n"
    "      - old.yml # expires 2026-12-31: written by another tool\n"
)
TODAY = "2026-10-11"


@pytest.fixture(autouse=True)
def _default_ruff_toml(tree):
    """Write minimal ruff.toml and track written files in a fixture git repository."""
    tree.git("init", "-b", "main")
    orig_write = tree.write

    def write_and_track(files: dict[str, str]):
        orig_write(files)
        if files and (tree.root / ".git").exists():
            tree.git("add", "--", *files.keys())
        return tree

    tree.write = write_and_track
    tree.write({RUFF: '[lint]\nselect = ["E"]\n'})


def check(tree, text, today=TODAY):
    return tree.write({CONFIG: text}).tool("exceptions.py", "check", "--today", today)


def test_tree_holding_only_default_ruff_toml_passes(tree):
    """A tree holding only the minimal ruff.toml configuration passes."""
    done = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done.returncode == 0
    assert "0 accepted" in done.stdout


def test_missing_ruff_toml_exits_3(tree):
    """Missing ruff.toml causes check to exit with code 3 naming the path."""
    (tree.root / RUFF).unlink()
    done = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done.returncode == 3
    assert str(tree.root / RUFF) in done.stderr


def test_root_that_is_no_git_repository_exits_3(tree):
    """A root that is not a git repository causes check to exit with code 3."""
    shutil.rmtree(tree.root / ".git")
    (tree.root / "hook.py").write_text("# hook\n")
    (tree.root / RUFF).write_text(
        '[lint.per-file-ignores]\n"hook.py" = ["ISC001"] # expires 2026-12-31: test\n'
    )
    done = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done.returncode == 3
    assert "not a git repository" in done.stderr


def test_entry_with_reason_and_a_day_ahead_passes(tree):
    done = check(tree, GOOD)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_entry_passes_on_its_last_day_and_fails_the_day_after(tree):
    assert check(tree, GOOD, "2026-12-31").returncode == 0
    done = check(tree, GOOD, "2027-01-01")
    assert done.returncode == 1
    assert "expired on 2026-12-31" in done.stderr


def test_entry_without_the_comment_fails(tree):
    done = check(tree, GOOD.replace(" # expires 2026-12-31: written by another tool", ""))
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_entry_without_a_reason_fails(tree):
    done = check(tree, GOOD.replace(": written by another tool", ":"))
    assert done.returncode == 1


def test_second_entry_is_not_covered_by_the_first(tree):
    done = check(tree, GOOD + "      - other.yml\n")
    assert done.returncode == 1
    assert "other.yml" in done.stderr


def test_rule_switched_off_as_a_whole_fails(tree):
    done = check(tree, "---\nrules:\n  unpinned-uses:\n    disable: true\n")
    assert done.returncode == 1
    assert "switched off" in done.stderr


def test_unreadable_configuration_is_an_error(tree):
    done = check(tree, "---\nrules: [\n")
    assert done.returncode == 3


def test_a_day_that_is_no_date_is_an_error(tree):
    done = check(tree, GOOD.replace("2026-12-31", "2026-13-41"))
    assert done.returncode == 3


OWN = "exceptions.yaml"
SUBJECT = ".github/rulesets/main.json"
ENTRY = (
    "  - rule: required-check-is-reported\n"
    f"    path: {SUBJECT}\n"
    "    issue: owner/engine#1212\n"
    "    reason: the engine declares a name no run reports\n"
    '    expires: "2026-12-31"\n'
)
LIST = "---\nexceptions:\n" + ENTRY


def own(tree, text, today=TODAY, subject=SUBJECT, env=None):
    """Write exception files into tree and run exceptions.py check."""
    files = {OWN: text} | ({subject: "{}\n"} if subject else {})
    return tree.write(files).tool("exceptions.py", "check", "--today", today, env=env)


def test_own_entry_with_every_field_passes(tree):
    done = own(tree, LIST)
    assert done.returncode == 0, done.stderr
    assert "0 accepted finding(s) and 1 exception(s)" in done.stdout


def test_own_entry_passes_on_its_last_day_and_fails_the_day_after(tree):
    assert own(tree, LIST, "2026-12-31").returncode == 0
    done = own(tree, LIST, "2027-01-01")
    assert done.returncode == 1
    assert f"{OWN}: entry 1 expired on 2026-12-31" in done.stderr


def test_own_entry_may_end_ninety_days_ahead_and_not_one_more(tree):
    assert own(tree, LIST, "2026-10-02").returncode == 0
    done = own(tree, LIST, "2026-10-01")
    assert done.returncode == 1
    assert "more than 90 days ahead" in done.stderr


@pytest.mark.parametrize("field", ["rule", "path", "issue", "reason", "expires"])
def test_own_entry_without_a_field_fails(tree, field):
    lines = [line for line in LIST.splitlines() if not line.lstrip(" -").startswith(field + ":")]
    text = "\n".join(lines).replace("\n    rule:", "\n  - rule:", 1) + "\n"
    if field == "rule":
        text = text.replace("    path:", "  - path:", 1)
    done = own(tree, text)
    assert done.returncode == 1, done.stdout
    assert f"entry 1 has no `{field}`" in done.stderr


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        ("the engine declares a name no run reports", '""', "has no `reason`"),
        ('"2026-12-31"', "2026-12-31", "has no `expires`"),
        ("owner/engine#1212", "the engine's issue 1212", "not as owner/name#number"),
        (SUBJECT, f".github/../{SUBJECT}", "is no file of the repository"),
        (SUBJECT, ".github/rulesets", "is no file of the repository"),
        ("    reason:", "    until: never\n    reason:", "has the unknown field `until`"),
    ],
    ids=["empty reason", "day unquoted", "issue as prose", "path with ..", "folder", "field"],
)
def test_own_entry_with_a_bad_field_fails(tree, old, new, said):
    done = own(tree, LIST.replace(old, new))
    assert done.returncode == 1, done.stdout
    assert said in done.stderr


def test_own_entry_with_an_absolute_path_fails(tree):
    done = own(tree, LIST.replace(SUBJECT, str(tree.root / SUBJECT)))
    assert done.returncode == 1
    assert "is no file of the repository" in done.stderr


def test_own_entry_for_a_file_that_is_gone_fails(tree):
    done = own(tree, LIST, subject=None)
    assert done.returncode == 1
    assert "is no file of the repository" in done.stderr


def test_own_entry_path_naming_untracked_file_fails(tree):
    """An own-list path naming an untracked file fails."""
    (tree.root / "untracked.json").write_text("{}\n")
    entry = (
        "---\nexceptions:\n"
        "  - rule: required-check-is-reported\n"
        "    path: untracked.json\n"
        "    issue: owner/engine#1212\n"
        "    reason: untracked file test\n"
        '    expires: "2026-12-31"\n'
    )
    done = own(tree, entry, subject=None)
    assert done.returncode == 1
    assert (
        "exceptions.yaml: entry 1 names `untracked.json`, which is no file of the repository"
        in done.stderr
    )


def foreign_index(tmp_path):
    """Make a repository outside the tree whose index holds docs/a.md."""
    foreign = Tree(tmp_path / "foreign")
    foreign.git("init", "-b", "main")
    foreign.write({"docs/a.md": "# foreign doc\n"})
    foreign.git("add", "docs/a.md")
    return foreign


def untracked_doc(tree, tmp_path):
    """Point tree at a fresh repository holding docs/a.md untracked."""
    tree.root = tmp_path / "own"
    tree.git("init", "-b", "main")
    tree.write({RUFF: '[lint]\nselect = ["E"]\n'})
    (tree.root / "docs").mkdir(parents=True, exist_ok=True)
    (tree.root / "docs/a.md").write_text("# own untracked doc\n")


def test_own_entry_untracked_file_with_foreign_git_dir_fails(tree, tmp_path):
    """An untracked file listed in exceptions fails even if tracked in foreign GIT_DIR."""
    foreign = foreign_index(tmp_path)

    untracked_doc(tree, tmp_path)

    entry = LIST.replace(SUBJECT, "docs/a.md")
    done = own(tree, entry, subject=None, env={"GIT_DIR": str(foreign.root / ".git")})
    assert done.returncode == 1
    assert (
        "exceptions.yaml: entry 1 names `docs/a.md`, which is no file of the repository"
        in done.stderr
    )


def test_own_entry_untracked_file_with_foreign_git_index_fails(tree, tmp_path):
    """An untracked file listed in exceptions fails even if tracked in foreign GIT_INDEX_FILE."""
    foreign = foreign_index(tmp_path)

    untracked_doc(tree, tmp_path)

    entry = LIST.replace(SUBJECT, "docs/a.md")
    done = own(
        tree,
        entry,
        subject=None,
        env={"GIT_INDEX_FILE": str(foreign.root / ".git" / "index")},
    )
    assert done.returncode == 1
    assert (
        "exceptions.yaml: entry 1 names `docs/a.md`, which is no file of the repository"
        in done.stderr
    )


def test_own_entry_tracked_file_with_foreign_git_dir_passes(tree, tmp_path):
    """A tracked file in own tree passes even if foreign GIT_DIR does not track it."""
    foreign = Tree(tmp_path / "foreign")
    foreign.git("init", "-b", "main")

    tree.root = tmp_path / "own"
    tree.git("init", "-b", "main")
    tree.write({RUFF: '[lint]\nselect = ["E"]\n', "docs/a.md": "# own tracked doc\n"})

    entry = LIST.replace(SUBJECT, "docs/a.md")
    done = own(tree, entry, subject=None, env={"GIT_DIR": str(foreign.root / ".git")})
    assert done.returncode == 0, done.stderr
    assert "0 accepted finding(s) and 1 exception(s)" in done.stdout


def test_tree_git_init_ignores_inherited_git_dir(tmp_path, monkeypatch):
    """Tree.git init creates fresh repo and preserves foreign HEAD and index when GIT_DIR is set."""
    foreign = foreign_index(tmp_path)

    head_bytes = (foreign.root / ".git" / "HEAD").read_bytes()
    index_bytes = (foreign.root / ".git" / "index").read_bytes()

    monkeypatch.setenv("GIT_DIR", str(foreign.root / ".git"))
    fresh = Tree(tmp_path / "fresh")
    fresh.git("init", "-b", "main")

    assert (fresh.root / ".git").is_dir()
    assert (foreign.root / ".git" / "HEAD").read_bytes() == head_bytes
    assert (foreign.root / ".git" / "index").read_bytes() == index_bytes


def test_own_entry_named_twice_fails_and_another_path_does_not(tree):
    done = own(tree, LIST + ENTRY)
    assert done.returncode == 1
    assert "entry 2 names rule and path of an entry before it" in done.stderr
    tree.write({"other.json": "{}\n"})
    assert own(tree, LIST + ENTRY.replace(SUBJECT, "other.json")).returncode == 0


@pytest.mark.parametrize(
    "text",
    [
        "---\nexceptions: [\n",
        "---\nexceptions:\n  rule: one\n",
        LIST + "tiers:\n  - tests/**\n",
        "---\n- rule: one\n",
        LIST.replace("2026-12-31", "2026-13-41"),
    ],
    ids=["unreadable", "no list", "second key", "no mapping", "no date"],
)
def test_own_list_that_cannot_be_read_is_an_error(tree, text):
    assert own(tree, text).returncode == 3


def test_accepted_finding_more_than_ninety_days_ahead_fails(tree):
    assert check(tree, GOOD, "2026-10-02").returncode == 0
    done = check(tree, GOOD, "2026-10-01")
    assert done.returncode == 1
    assert "more than 90 days ahead" in done.stderr


def test_both_places_are_counted_and_a_finding_in_either_fails(tree):
    tree.write({CONFIG: GOOD})
    done = own(tree, LIST)
    assert done.returncode == 0, done.stderr
    assert "1 accepted finding(s) and 1 exception(s)" in done.stdout
    assert tree.tool("exceptions.py", "check", "--today", "2027-01-01").stderr.count("expired") == 2


def _with_category(cat: str, entered: str, expires: str) -> str:
    replacement = f'    category: {cat}\n    entered: "{entered}"\n    expires: "{expires}"\n'
    return "---\nexceptions:\n" + ENTRY.replace('    expires: "2026-12-31"\n', replacement)


def test_category_c_plus_eleven_days_refused(tree):
    done = own(tree, _with_category("C", "2026-10-11", "2026-10-22"))
    assert done.returncode == 1, done.stdout
    assert "category C" in done.stderr
    assert "2026-10-21" in done.stderr


def test_category_c_plus_ten_days_ok(tree):
    done = own(tree, _with_category("C", "2026-10-11", "2026-10-21"))
    assert done.returncode == 0, done.stderr


def test_category_a_skips_interval_check(tree):
    done = own(tree, _with_category("A", "2026-10-11", "2026-12-31"))
    assert done.returncode == 0, done.stderr


def test_category_d_plus_120_days_ok(tree):
    done = own(tree, _with_category("D", "2026-10-01", "2027-01-29"))
    assert done.returncode == 0, done.stderr


def test_category_d_wrong_interval_refused(tree):
    done = own(tree, _with_category("D", "2026-10-01", "2027-01-30"))
    assert done.returncode == 1, done.stdout
    assert "category D" in done.stderr
    assert "2027-01-29" in done.stderr


def test_mutation_empty_category_days_lets_wrong_interval_pass(tree):
    source = (ROOT / "tools" / "exceptions.py").read_text(encoding="utf-8")
    mutated = source.replace('CATEGORY_DAYS = {"B": 3, "C": 10, "D": 120}', "CATEGORY_DAYS = {}")
    assert mutated != source
    mutant = tree.root / "tools" / "mutant_exceptions.py"
    mutant.parent.mkdir(parents=True, exist_ok=True)
    mutant.write_text(mutated, encoding="utf-8")
    for dep in ("ciwf.py",):
        src = ROOT / "tools" / dep
        if src.exists():
            dst = tree.root / "tools" / dep
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    files = {
        OWN: _with_category("C", "2026-10-11", "2026-10-22"),
        SUBJECT: "{}\n",
    }
    tree.write(files)
    done = subprocess.run(
        [sys.executable, "-B", str(mutant), "check", "--root", str(tree.root), "--today", TODAY],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr


LINTED = ".github/actionlint.yaml"
WORKFLOW = ".github/workflows/train.yml"
IGNORE = (
    "---\npaths:\n  .github/workflows/train.yml:\n    ignore:\n"
    "      - 'property \"x\" is not defined' # expires 2026-12-31: linter lacks x\n"
)


def linted(tree, text, today=TODAY):
    tree.write({WORKFLOW: "---\n", LINTED: text})
    return tree.tool("exceptions.py", "check", "--today", today)


def test_actionlint_ignore_with_reason_and_a_day_ahead_passes(tree):
    done = linted(tree, IGNORE)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_actionlint_ignore_fails_the_day_after_it_ends(tree):
    assert linted(tree, IGNORE, "2026-12-31").returncode == 0
    done = linted(tree, IGNORE, "2027-01-01")
    assert done.returncode == 1
    assert "expired on 2026-12-31" in done.stderr


@pytest.mark.parametrize(
    "old, new, said",
    [
        (" # expires 2026-12-31: linter lacks x", "", "has no `# expires"),
        (": linter lacks x", ":", "has no `# expires"),
        ("2026-12-31", "2027-03-01", "more than 90 days ahead"),
        ("workflows/train.yml", "workflows/*.yml", "is no one file"),
        ("    ignore:\n", "    other: 1\n    ignore:\n", "sets `other`"),
    ],
)
def test_actionlint_ignore_without_its_bounds_fails(tree, old, new, said):
    done = linted(tree, IGNORE.replace(old, new))
    assert done.returncode == 1
    assert said in done.stderr


def test_actionlint_settings_without_paths_pass(tree):
    done = linted(tree, "---\nself-hosted-runner:\n  labels:\n    - ubuntu-26.04\n")
    assert done.returncode == 0, done.stderr
    assert "0 accepted" in done.stdout


def test_actionlint_config_path_naming_untracked_file_fails(tree):
    """An actionlint config path naming an untracked file fails."""
    (tree.root / "untracked.yml").write_text("# untracked\n")
    text = (
        "---\npaths:\n  untracked.yml:\n    ignore:\n"
        "      - 'property \"x\" is not defined' # expires 2026-12-31: test\n"
    )
    done = linted(tree, text)
    assert done.returncode == 1
    assert ".github/actionlint.yaml: untracked.yml is no one file of the repository" in done.stderr


RUFF = "ruff.toml"
HOOK = ".config/agent/hooks/block_evasion.py"
RUFF_IGNORE = (
    "[lint.per-file-ignores]\n"
    f'"{HOOK}" = ["ISC001", "ISC004"] # expires 2026-12-31: praetor-managed hook\n'
)


def ruffed(tree, text, today=TODAY):
    """Write ruff.toml and the tracked hook file, then run exceptions.py check."""
    tree.write({HOOK: "# hook\n", RUFF: text})
    return tree.tool("exceptions.py", "check", "--today", today)


def test_ruff_entry_with_comment_and_a_day_ahead_passes(tree):
    """An entry with comment and a day ahead passes."""
    done = ruffed(tree, RUFF_IGNORE)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_entry_passes_on_its_last_day_and_fails_the_day_after(tree):
    """An entry passes on its last day and fails the day after."""
    assert ruffed(tree, RUFF_IGNORE, "2026-12-31").returncode == 0
    done = ruffed(tree, RUFF_IGNORE, "2027-01-01")
    assert done.returncode == 1
    assert "expired on 2026-12-31" in done.stderr


def test_ruff_entry_missing_comment_fails(tree):
    """An entry without an expiration comment fails."""
    done = ruffed(tree, f'[lint.per-file-ignores]\n"{HOOK}" = ["ISC001", "ISC004"]\n')
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_ruff_key_naming_pattern_or_no_file_fails(tree):
    """A key that names a pattern or no file fails."""
    pattern = RUFF_IGNORE.replace(HOOK, ".config/agent/hooks/*.py")
    done_pat = ruffed(tree, pattern)
    assert done_pat.returncode == 1
    assert "is no one file" in done_pat.stderr

    missing = RUFF_IGNORE.replace(HOOK, "no_such_file.py")
    done_missing = ruffed(tree, missing)
    assert done_missing.returncode == 1
    assert "is no one file" in done_missing.stderr


def test_ruff_key_naming_untracked_file_fails(tree):
    """A per-file-ignores key naming a file that is not tracked by git fails."""
    (tree.root / "untracked.py").write_text("# untracked\n")
    text = (
        "[lint.per-file-ignores]\n"
        '"untracked.py" = ["ISC001"] # expires 2026-12-31: untracked file\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "ruff.toml: untracked.py is no one file of the repository" in done.stderr


def test_ruff_key_naming_untracked_file_passes_after_git_add(tree):
    """A per-file-ignores key naming a file passes once tracked by git."""
    (tree.root / "untracked.py").write_text("# untracked\n")
    text = (
        "[lint.per-file-ignores]\n"
        '"untracked.py" = ["ISC001"] # expires 2026-12-31: untracked file\n'
    )
    done_fail = ruffed(tree, text)
    assert done_fail.returncode == 1
    assert "is no one file of the repository" in done_fail.stderr
    tree.git("add", "untracked.py")
    done_pass = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done_pass.returncode == 0, done_pass.stderr
    assert "1 accepted" in done_pass.stdout


def test_ruff_non_empty_lint_ignore_fails(tree):
    """A non-empty [lint] ignore list fails."""
    done = ruffed(tree, '[lint]\nignore = ["E501"]\n')
    assert done.returncode == 1
    assert "[lint] ignore is not empty" in done.stderr


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            '[lint]\nextend-ignore = ["E501"]\n', "[lint] key `extend-ignore`", id="extend-ignore"
        ),
        pytest.param(
            "[lint]\nextend-per-file-ignores = {}\n",
            "[lint] key `extend-per-file-ignores`",
            id="extend-per-file",
        ),
        pytest.param(
            'per-file-ignores = {"a.py" = ["E501"]}\n',
            "key `per-file-ignores`",
            id="top-level-per-file",
        ),
        pytest.param('extend-ignore = ["E501"]\n', "key `extend-ignore`", id="top-level-extend"),
        pytest.param("lint = 1\n", "`lint` is not a table", id="lint-not-a-table"),
        pytest.param(
            "[lint]\nper-file-ignores = 1\n",
            "[lint] per-file-ignores is not a table",
            id="per-file-not-a-table",
        ),
    ],
)
def test_ruff_key_outside_the_allow_list_fails(tree, text, message):
    """A key the tool does not read fails, at the top level and in [lint]."""
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert f"ruff.toml: {message}" in done.stderr


def test_ruff_known_keys_pass(tree):
    """Every key of the allow-lists, with an empty ignore list, passes."""
    text = (
        'line-length = 100\ntarget-version = "py312"\n[lint]\nselect = ["E"]\nignore = []\n'
        "[lint.isort]\nforce-sort-within-sections = false\n" + RUFF_IGNORE
    )
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_single_quoted_key_passes(tree):
    """A single-quoted key in per-file-ignores passes."""
    text = (
        "[lint.per-file-ignores]\n"
        f'\'{HOOK}\' = ["ISC001", "ISC004"] # expires 2026-12-31: single-quoted key\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_bare_key_passes(tree):
    """A bare key naming a tracked file in per-file-ignores passes."""
    tree.write({"hook_file": "# tracked\n"})
    text = '[lint.per-file-ignores]\nhook_file = ["ISC001"] # expires 2026-12-31: bare key\n'
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_comment_counts_for_the_key_toml_decodes(tree):
    """A comment on the literal key '"<hook>"' does not count for the bare hook key."""
    text = (
        "[lint.per-file-ignores]\n"
        f'\'"{HOOK}"\' = ["ISC001"] # expires 2026-12-31: key holds quote marks\n'
        f'"{HOOK}" = ["ISC001"]\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert f"ruff.toml: {HOOK} has no `# expires" in done.stderr
    assert f'ruff.toml: "{HOOK}" is no one file of the repository' in done.stderr


def test_ruff_multiline_array_comment_on_opening_line_passes(tree):
    """A multiline array with the comment on the opening line passes."""
    text = (
        "[lint.per-file-ignores]\n"
        f'"{HOOK}" = [ # expires 2026-12-31: multiline array\n'
        '    "ISC001",\n'
        '    "ISC004",\n'
        "]\n"
    )
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_multiline_array_comment_on_closing_line_fails(tree):
    """A multiline array with the comment only on the closing line fails."""
    text = (
        "[lint.per-file-ignores]\n"
        f'"{HOOK}" = [\n'
        '    "ISC001",\n'
        '    "ISC004",\n'
        "] # expires 2026-12-31: closing line comment\n"
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_ruff_entry_under_isort_instead_of_per_file_ignores_fails(tree):
    """An entry with comment placed under [lint.isort] instead of per-file-ignores fails."""
    text = (
        "[lint.per-file-ignores]\n"
        f'"{HOOK}" = ["ISC001"]\n'
        "[lint.isort]\n"
        f'"{HOOK}" = ["ISC001"] # expires 2026-12-31: under isort\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_ruff_header_with_spaces_fails(tree):
    """A header spelled `[ lint.per-file-ignores ]` fails to collect stated entries."""
    text = (
        "[ lint.per-file-ignores ]\n"
        f'"{HOOK}" = ["ISC001", "ISC004"] # expires 2026-12-31: praetor-managed hook\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_ruff_unreadable_toml_exits_3(tree):
    """Unreadable TOML configuration causes check to exit with code 3."""
    done = ruffed(tree, "[lint\nunclosed = [\n")
    assert done.returncode == 3


def real_check(day: str) -> subprocess.CompletedProcess:
    """Run exceptions.py check on the repository itself as if today were this day."""
    command = [sys.executable, "-B", str(ROOT / "tools" / "exceptions.py"), "check"]
    command += ["--root", str(ROOT), "--today", day]
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)


def test_real_repository_ruff_toml_passes_today_and_fails_after_expiry():
    """The real repository ruff.toml passes on 2026-11-08 and fails on 2026-11-19."""
    done_pass = real_check("2026-11-08")
    assert done_pass.returncode == 0, done_pass.stderr
    assert "accepted finding(s)" in done_pass.stdout
    done_fail = real_check("2026-11-19")
    assert done_fail.returncode == 1
    assert (
        "ruff.toml: .config/agent/hooks/block_evasion.py expired on 2026-11-18" in done_fail.stderr
    )
