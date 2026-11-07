# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Exceptions: an incomplete entry, an expired or too long one and a disabled rule all fail."""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import ROOT

CONFIG = ".github/zizmor.yml"
GOOD = (
    "---\nrules:\n  concurrency-limits:\n    ignore:\n"
    "      - old.yml # expires 2026-12-31: written by another tool\n"
)
TODAY = "2026-10-11"


def check(tree, text, today=TODAY):
    return tree.write({CONFIG: text}).tool("exceptions.py", "check", "--today", today)


def test_no_configuration_passes(tree):
    done = tree.tool("exceptions.py", "check", "--today", TODAY)
    assert done.returncode == 0
    assert "0 accepted" in done.stdout


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


def own(tree, text, today=TODAY, subject=SUBJECT):
    files = {OWN: text} | ({subject: "{}\n"} if subject else {})
    return tree.write(files).tool("exceptions.py", "check", "--today", today)


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
