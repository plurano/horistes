# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions: an incomplete entry, an expired or too long one and a disabled rule all fail."""

from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT, Tree

CONFIG = ".github/zizmor.yml"
RUFF = "ruff.toml"
GOOD = (
    "---\nrules:\n  concurrency-limits:\n    ignore:\n"
    "      - old.yml # expires 2026-12-31 category D: written by another tool\n"
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
        '[lint.per-file-ignores]\n"hook.py" = ["ISC001"] # expires 2026-12-31 category D: test\n'
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
    replacement = " # expires 2026-12-31 category D: written by another tool"
    done = check(tree, GOOD.replace(replacement, ""))
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_entry_without_a_reason_fails(tree):
    done = check(tree, GOOD.replace("written by another tool", ""))
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
    '    category: "C"\n'
    '    entered: "2026-10-01"\n'
    '    expires: "2026-10-11"\n'
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
    assert own(tree, LIST, "2026-10-11").returncode == 0
    done = own(tree, LIST, "2026-10-12")
    assert done.returncode == 1
    assert f"{OWN}: entry 1 expired on 2026-10-11" in done.stderr


def test_own_entry_may_end_ten_days_ahead_for_category_c_and_not_one_more(tree):
    assert own(tree, LIST, "2026-10-01").returncode == 0
    done = own(tree, LIST, "2026-09-30")
    assert done.returncode == 1
    assert "more than 10 days ahead" in done.stderr


@pytest.mark.parametrize(
    "field", ["rule", "path", "issue", "reason", "expires", "category", "entered"]
)
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
        ('"2026-10-11"', "2026-10-11", "has no `expires`"),
        ("owner/engine#1212", "the engine's issue 1212", "not as owner/name#number"),
        (SUBJECT, f".github/../{SUBJECT}", "is no file of the repository"),
        (SUBJECT, ".github/rulesets", "is no file of the repository"),
        ("    reason:", "    until: never\n    reason:", "has the unknown field `until`"),
        ('category: "C"', 'category: "X"', "not one of A, B, C, D"),
    ],
    ids=[
        "empty reason",
        "day unquoted",
        "issue as prose",
        "path with ..",
        "folder",
        "field",
        "bad category",
    ],
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
        '    category: "C"\n'
        '    entered: "2026-10-01"\n'
        '    expires: "2026-10-11"\n'
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


def test_tree_git_add_ignores_inherited_git_index_file(tmp_path, monkeypatch):
    """Tree.git add stages into the tree's own index when GIT_INDEX_FILE names a foreign one."""
    foreign = foreign_index(tmp_path)
    index_bytes = (foreign.root / ".git" / "index").read_bytes()

    monkeypatch.setenv("GIT_INDEX_FILE", str(foreign.root / ".git" / "index"))
    fresh = Tree(tmp_path / "fresh")
    fresh.git("init", "-b", "main")
    fresh.write({RUFF: '[lint]\nselect = ["E"]\n'})
    fresh.git("add", "--", RUFF)

    assert (foreign.root / ".git" / "index").read_bytes() == index_bytes
    assert fresh.git("ls-files") == RUFF


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
        LIST.replace("2026-10-11", "2026-13-41"),
    ],
    ids=["unreadable", "no list", "second key", "no mapping", "no date"],
)
def test_own_list_that_cannot_be_read_is_an_error(tree, text):
    assert own(tree, text).returncode == 3


def test_accepted_finding_more_than_allowed_days_ahead_fails(tree):
    assert check(tree, GOOD, "2026-09-02").returncode == 0
    done = check(tree, GOOD, "2026-09-01")
    assert done.returncode == 1
    assert "more than 120 days ahead" in done.stderr


def test_both_places_are_counted_and_a_finding_in_either_fails(tree):
    tree.write({CONFIG: GOOD})
    done = own(tree, LIST)
    assert done.returncode == 0, done.stderr
    assert "1 accepted finding(s) and 1 exception(s)" in done.stdout
    assert tree.tool("exceptions.py", "check", "--today", "2027-01-01").stderr.count("expired") == 2


def _with_category(
    cat: str,
    entered: str,
    expires: str,
    reason: str = "the engine declares a name no run reports",
) -> str:
    replacement = (
        f"    reason: {reason}\n"
        f'    category: "{cat}"\n'
        f'    entered: "{entered}"\n'
        f'    expires: "{expires}"\n'
    )
    target = (
        "    reason: the engine declares a name no run reports\n"
        '    category: "C"\n'
        '    entered: "2026-10-01"\n'
        '    expires: "2026-10-11"\n'
    )
    return "---\nexceptions:\n" + ENTRY.replace(target, replacement)


def test_category_c_plus_eleven_days_refused(tree):
    done = own(tree, _with_category("C", "2026-10-11", "2026-10-22"))
    assert done.returncode == 1, done.stdout
    assert "category C" in done.stderr
    assert "2026-10-21" in done.stderr


def test_category_c_plus_ten_days_ok(tree):
    done = own(tree, _with_category("C", "2026-10-11", "2026-10-21"))
    assert done.returncode == 0, done.stderr


def test_category_a_with_stated_interval_ok(tree):
    entry = _with_category("A", "2026-10-11", "2026-11-20", reason="interval 40 days test")
    done = own(tree, entry)
    assert done.returncode == 0, done.stderr


def test_category_a_missing_interval_fails(tree):
    done = own(tree, _with_category("A", "2026-10-11", "2026-11-20", reason="no interval"))
    assert done.returncode == 1, done.stdout
    assert done.stderr.count("category A needs 'interval N days' in reason") == 1


def test_category_a_interval_above_ninety_fails(tree):
    entry = _with_category("A", "2026-10-11", "2027-01-10", reason="interval 91 days test")
    done = own(tree, entry)
    assert done.returncode == 1, done.stdout
    assert done.stderr.count("between 1 and 90 days") == 1


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
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs mutant on tree
    done = subprocess.run(
        [sys.executable, "-B", str(mutant), "check", "--root", str(tree.root), "--today", TODAY],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode != 0


LINTED = ".github/actionlint.yaml"
WORKFLOW = ".github/workflows/train.yml"
IGNORE = (
    "---\npaths:\n  .github/workflows/train.yml:\n    ignore:\n"
    "      - 'property \"x\" is not defined' # expires 2026-12-31 category D: linter lacks x\n"
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
        (" # expires 2026-12-31 category D: linter lacks x", "", "has no `# expires"),
        (": linter lacks x", ":", "has no `# expires"),
        ("2026-12-31", "2027-03-01", "more than 120 days ahead"),
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
        "      - 'property \"x\" is not defined' # expires 2026-12-31 category D: test\n"
    )
    done = linted(tree, text)
    assert done.returncode == 1
    assert ".github/actionlint.yaml: untracked.yml is no one file of the repository" in done.stderr


RUFF = "ruff.toml"
HOOK = ".config/agent/hooks/block_evasion.py"
RUFF_IGNORE = (
    "[lint.per-file-ignores]\n"
    f'"{HOOK}" = ["ISC001", "ISC004"] # expires 2026-12-31 category D: praetor-managed hook\n'
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
        '"untracked.py" = ["ISC001"] # expires 2026-12-31 category D: untracked file\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "ruff.toml: untracked.py is no one file of the repository" in done.stderr


def test_ruff_key_naming_untracked_file_passes_after_git_add(tree):
    """A per-file-ignores key naming a file passes once tracked by git."""
    (tree.root / "untracked.py").write_text("# untracked\n")
    text = (
        "[lint.per-file-ignores]\n"
        '"untracked.py" = ["ISC001"] # expires 2026-12-31 category D: untracked file\n'
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
        f'\'{HOOK}\' = ["ISC001", "ISC004"] # expires 2026-12-31 category D: single-quoted key\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_bare_key_passes(tree):
    """A bare key naming a tracked file in per-file-ignores passes."""
    tree.write({"hook_file": "# tracked\n"})
    text = (
        "[lint.per-file-ignores]\n"
        'hook_file = ["ISC001"] # expires 2026-12-31 category D: bare key\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 0, done.stderr
    assert "1 accepted" in done.stdout


def test_ruff_comment_counts_for_the_key_toml_decodes(tree):
    """A comment on the literal key '"<hook>"' does not count for the bare hook key."""
    text = (
        "[lint.per-file-ignores]\n"
        f'\'"{HOOK}"\' = ["ISC001"] # expires 2026-12-31 category D: key holds quote marks\n'
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
        f'"{HOOK}" = [ # expires 2026-12-31 category D: multiline array\n'
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
        "] # expires 2026-12-31 category D: closing line comment\n"
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
        f'"{HOOK}" = ["ISC001"] # expires 2026-12-31 category D: under isort\n'
    )
    done = ruffed(tree, text)
    assert done.returncode == 1
    assert "has no `# expires" in done.stderr


def test_ruff_header_with_spaces_fails(tree):
    """A header spelled `[ lint.per-file-ignores ]` fails to collect stated entries."""
    text = (
        "[ lint.per-file-ignores ]\n"
        f'"{HOOK}" = ["ISC001", "ISC004"] # expires 2026-12-31 category D: praetor-managed hook\n'
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
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs check on ROOT
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)


def test_real_repository_ruff_toml_passes_today_and_fails_after_expiry():
    """The real repository check passes on 2026-11-09 and fails on 2026-11-19."""
    done_pass = real_check("2026-11-09")
    assert done_pass.returncode == 0, done_pass.stderr
    assert "accepted finding(s)" in done_pass.stdout
    done_fail = real_check("2026-11-19")
    assert done_fail.returncode == 1
    assert (
        "ruff.toml: .config/agent/hooks/block_evasion.py expired on 2026-11-18" in done_fail.stderr
    )


def test_category_problem_3d():
    """3D unit tests for category_problem under fleet MEL rules."""
    import exceptions

    today = datetime.date(2026, 11, 9)

    # Missing / bad category -> refused
    assert "not one of A, B, C, D" in exceptions.category_problem(today, "X", "reason", today)

    # Expired -> refused
    yesterday = today - datetime.timedelta(days=1)
    assert exceptions.category_problem(yesterday, "B", "reason", today) == f"expired on {yesterday}"

    # Category D: exactly 120 days -> ok; 121 days -> refused
    d_120 = today + datetime.timedelta(days=120)
    d_121 = today + datetime.timedelta(days=121)
    assert exceptions.category_problem(d_120, "D", "reason", today) == ""
    assert f"ends on {d_121}, more than 120 days ahead" in exceptions.category_problem(
        d_121, "D", "reason", today
    )

    # Category B: exactly 3 days -> ok; 4 days -> refused
    b_3 = today + datetime.timedelta(days=3)
    b_4 = today + datetime.timedelta(days=4)
    assert exceptions.category_problem(b_3, "B", "reason", today) == ""
    assert f"ends on {b_4}, more than 3 days ahead" in exceptions.category_problem(
        b_4, "B", "reason", today
    )

    # Category C: exactly 10 days -> ok; 11 days -> refused
    c_10 = today + datetime.timedelta(days=10)
    c_11 = today + datetime.timedelta(days=11)
    assert exceptions.category_problem(c_10, "C", "reason", today) == ""
    assert f"ends on {c_11}, more than 10 days ahead" in exceptions.category_problem(
        c_11, "C", "reason", today
    )

    # Category A: without interval N days -> refused
    assert "category A needs 'interval N days' in reason" in exceptions.category_problem(
        today, "A", "no interval here", today
    )
    # Category A: interval 91 -> refused
    assert "between 1 and 90 days" in exceptions.category_problem(
        today, "A", "interval 91 days", today
    )
    # Category A: interval 90 -> exactly 90 days -> ok
    a_90 = today + datetime.timedelta(days=90)
    assert exceptions.category_problem(a_90, "A", "interval 90 days allowed", today) == ""
    # Category A: interval 10 days, 11 days ahead -> refused
    a_11 = today + datetime.timedelta(days=11)
    assert f"ends on {a_11}, more than 10 days ahead" in exceptions.category_problem(
        a_11, "A", "interval 10 days allowed", today
    )


def test_reason_problem_3d():
    """3D unit tests for reason validation."""
    import exceptions

    assert exceptions._reason_problem("") == "reason is empty"
    assert exceptions._reason_problem("   ") == "reason is empty"
    assert exceptions._reason_problem("bad, reason with comma") == "reason contains a comma"
    assert exceptions._reason_problem("1234567") == "reason has no letter"
    assert exceptions._reason_problem("valid reason text") == ""


def test_yaml_suppressions_3d(tree):
    """3D tests for YAML file suppressions: yamllint/zizmor refused, opengrep fleet format."""
    import exceptions

    today = datetime.date(2026, 11, 9)

    # yamllint directive refused
    yaml_yamllint = "steps:\n  # yamllint disable rule:line-length\n  - run: echo 1\n"
    probs = exceptions.yaml_problems(yaml_yamllint, "test.yml", today, set(), {})
    assert len(probs) == 1
    assert "invalid yamllint suppression" in probs[0]

    # zizmor directive refused
    yaml_zizmor = "steps:\n  # zizmor: ignore[rule]\n  - run: echo 1\n"
    probs = exceptions.yaml_problems(yaml_zizmor, "test.yml", today, set(), {})
    assert len(probs) == 1
    assert "invalid zizmor suppression" in probs[0]

    # opengrep fleet format in YAML passes
    sg = exceptions.LONG_WORD
    yaml_sg_ok = f"steps:\n  # {sg}: some-rule expires 2026-11-12 category B: valid reason\n"
    counts = {}
    probs = exceptions.yaml_problems(yaml_sg_ok, "test.yml", today, set(), counts)
    assert probs == []
    assert counts.get(sg) == 1

    # opengrep bad format in YAML fails
    yaml_sg_bad = f"steps:\n  # {sg}: some-rule bad format\n"
    probs = exceptions.yaml_problems(yaml_sg_bad, "test.yml", today, set(), {})
    assert len(probs) == 1
    assert "invalid opengrep suppression" in probs[0]


def test_markdown_suppressions_3d(tree):
    """3D tests for Markdown comment suppressions."""
    import exceptions

    today = datetime.date(2026, 11, 9)

    # Without format and without entry -> red
    md_bad = "<!-- markdownlint-disable MD013 -->\n# Title\n"
    probs = exceptions.md_problems(md_bad, "test.md", today, set(), {})
    assert len(probs) == 1
    assert "invalid markdownlint suppression" in probs[0]

    # Valid fleet format -> green
    md_fleet = (
        "<!-- markdownlint-disable MD013 expires 2026-11-12 category B: line length issue -->\n"
        "# Title\n"
    )
    counts = {}
    probs = exceptions.md_problems(md_fleet, "test.md", today, set(), counts)
    assert probs == []
    assert counts.get("markdownlint") == 1

    # Covered by valid exception -> green
    valid_map = {("MD013", "test.md"): {"rule": "MD013", "path": "test.md"}}
    probs = exceptions.md_problems(md_bad, "test.md", today, set(valid_map.keys()), {})
    assert probs == []

    # Covered by expired exception -> red (expired entries not in valid_map)
    probs_expired = exceptions.md_problems(md_bad, "test.md", today, set(), {})
    assert len(probs_expired) == 1


def test_shell_suppressions_3d(tree):
    """3D tests for Shell comment suppressions."""
    import exceptions

    today = datetime.date(2026, 11, 9)

    # Without fleet format -> red
    sh_bad = "# shellcheck disable=SC2086\necho $var\n"
    probs = exceptions.sh_problems(sh_bad, "test.sh", today, set(), {})
    assert len(probs) == 1
    assert "invalid shellcheck suppression" in probs[0]

    # With valid fleet format -> green
    sh_ok = "# shellcheck disable=SC2086 expires 2026-11-12 category B: word splitting\necho $var\n"
    counts = {}
    probs = exceptions.sh_problems(sh_ok, "test.sh", today, set(), counts)
    assert probs == []
    assert counts.get("shellcheck") == 1


def test_reuse_marker_refused_3d(tree):
    """3D test: leftover REUSE markers are refused in tracked text files."""
    import exceptions

    start_marker = exceptions.REUSE_START
    end_marker = exceptions.REUSE_END

    probs_start = exceptions._reuse_problems("file.txt", f"header\n<{start_marker}>\nbody\n")
    assert len(probs_start) == 1
    assert "invalid REUSE suppression" in probs_start[0]

    probs_end = exceptions._reuse_problems("file.txt", f"body\n<{end_marker}>\nfooter\n")
    assert len(probs_end) == 1
    assert "invalid REUSE suppression" in probs_end[0]


def test_ratchet_3d(tree):
    """3D test: ratchet detects growth and shrinkages against suppression-baseline.json."""
    import exceptions

    sg = exceptions.LONG_WORD
    # Baseline matches counts -> pass
    tree.write({"suppression-baseline.json": f'{{"noqa": 2, "{sg}": 10}}\n'})
    probs = exceptions._ratchet_problems(tree.root, {"noqa": 2, sg: 10})
    assert probs == []

    # Growth -> fail
    probs_grow = exceptions._ratchet_problems(tree.root, {"noqa": 3, sg: 10})
    assert len(probs_grow) == 1
    assert "noqa grew from 2 to 3" in probs_grow[0]

    # Shrinkage -> fail
    probs_shrink = exceptions._ratchet_problems(tree.root, {"noqa": 1, sg: 10})
    assert len(probs_shrink) == 1
    assert "lower noqa in suppression-baseline.json to 1" in probs_shrink[0]


def test_reuse_lint_override_3d(tmp_path):
    """3D test: reuse lint passes with override table, fails without override table."""
    import os

    reuse_bin = (
        os.environ.get("REUSE") or shutil.which("reuse") or str(ROOT / ".venv" / "bin" / "reuse")
    )
    if not Path(reuse_bin).exists() and not shutil.which(str(reuse_bin)):
        pytest.skip("reuse binary not found")

    dest = tmp_path / "repo"
    dest.mkdir()
    shutil.copy(ROOT / "REUSE.toml", dest / "REUSE.toml")
    shutil.copytree(ROOT / "LICENSES", dest / "LICENSES")
    shutil.copytree(ROOT / "templates", dest / "templates")
    (dest / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_render.py", dest / "tests" / "test_render.py")

    subprocess.run(["git", "init", "-b", "main"], cwd=dest, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, capture_output=True, check=True)

    # 1. With override table -> rc 0
    proc1 = subprocess.run([str(reuse_bin), "lint"], cwd=dest, capture_output=True, text=True)
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr

    # 2. Mutate REUSE.toml: remove the override table
    toml_text = (dest / "REUSE.toml").read_text(encoding="utf-8")
    assert 'precedence = "override"' in toml_text
    blocks = toml_text.split("[[annotations]]")
    kept_blocks = [b for b in blocks if 'precedence = "override"' not in b]
    (dest / "REUSE.toml").write_text("[[annotations]]".join(kept_blocks), encoding="utf-8")

    # Run reuse lint again -> must fail with invalid SPDX expressions
    proc2 = subprocess.run([str(reuse_bin), "lint"], cwd=dest, capture_output=True, text=True)
    assert proc2.returncode != 0
    assert "Invalid SPDX" in proc2.stdout or "invalid spdx" in proc2.stdout.lower()


def test_scan_tracked_file_oserror_exits_3(tree):
    """An unreadable tracked file raises ReadError and causes check to exit 3 naming the file."""
    tree.write({"unreadable.py": "# test\n"})
    (tree.root / "unreadable.py").chmod(0o000)
    try:
        done = tree.tool("exceptions.py", "check", "--today", TODAY)
        assert done.returncode == 3
        assert "unreadable.py" in done.stderr
    finally:
        (tree.root / "unreadable.py").chmod(0o644)


def test_tracked_non_utf8_file_is_skipped(tree):
    """A tracked non-UTF-8 file is skipped, rc is 0, count in stdout, baseline unchanged."""
    tree.write({"suppression-baseline.json": '{"noqa": 0}\n'})
    binary_file = tree.root / "binary.png"
    binary_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe")
    tree.git("add", "binary.png")
    done = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done.returncode == 0, done.stderr
    assert "1 tracked file(s) skipped as not UTF-8" in done.stdout


def test_markdown_fleet_format_with_hyphen_reason_passes(tree):
    """A markdownlint directive in fleet format whose reason holds a hyphen passes."""
    import exceptions

    today = datetime.date(2026, 11, 9)
    md_hyphen = (
        "<!-- markdownlint-disable MD013 expires 2026-11-12 category B: engine-owned line -->\n"
        "# Title\n"
    )
    counts: dict[str, int] = {}
    probs = exceptions.md_problems(md_hyphen, "test.md", today, set(), counts)
    assert probs == []
    assert counts.get("markdownlint") == 1


def test_own_duplicate_entry_retains_first_in_valid_map(tree):
    """Two entries for the same rule and path keep the first entry in valid_map."""
    import exceptions

    entry1 = (
        "  - rule: required-check-is-reported\n"
        f"    path: {SUBJECT}\n"
        "    issue: owner/engine#1212\n"
        "    reason: first entry\n"
        '    category: "C"\n'
        '    entered: "2026-10-01"\n'
        '    expires: "2026-10-11"\n'
    )
    entry2 = (
        "  - rule: required-check-is-reported\n"
        f"    path: {SUBJECT}\n"
        "    issue: owner/engine#1212\n"
        "    reason: second entry\n"
        '    category: "C"\n'
        '    entered: "2026-10-01"\n'
        '    expires: "2026-10-11"\n'
    )
    tree.write({OWN: "---\nexceptions:\n" + entry1 + entry2, SUBJECT: "{}\n"})
    found, count, valid_map = exceptions._own(tree.root, datetime.date(2026, 10, 11))
    assert count == 2
    assert any("names rule and path of an entry before it" in p for p in found)
    key = ("required-check-is-reported", SUBJECT)
    assert key in valid_map
    assert valid_map[key]["reason"] == "first entry"


@pytest.mark.parametrize(
    "bad_value",
    ['"two"', "-1", "true"],
    ids=["string", "negative", "bool"],
)
def test_ratchet_baseline_value_not_count_fails(tree, bad_value):
    """A baseline value that is not a non-negative int is reported as not a count."""
    import exceptions

    tree.write({"suppression-baseline.json": f'{{"noqa": {bad_value}}}\n'})
    probs = exceptions._ratchet_problems(tree.root, {"noqa": 0})
    assert probs == ["suppression-baseline.json: noqa is not a count"]
