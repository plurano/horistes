# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The repository's own callers equal the rendered templates except allow-listed parts."""

from __future__ import annotations

import difflib
import json
import re
import sys

import pytest
import yaml
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import render

CALL_USES = re.compile(r"^    uses: \S+(?: # \S+)?$")
REQUIRED_LINE = re.compile(r"^      required: ")
RULESET = ROOT / ".github/rulesets/main.json"
OWN_CONTEXT = {
    **render.SAMPLE,
    "Licence": {
        "CopyrightText": "2026 Lusoris <lusoris@pm.me>",
        "Identifier": "EUPL-1.2",
    },
    "RequiredChecks": {
        "Required": (
            '["Python 3.12 floor", "Fuzz the result reader", "Verification gate", '
            '"CodeQL", "Licence", "Action pins", "Sign-off"]'
        ),
        "PullRequestOnly": "[]",
        "NotOwed": "[]",
        "Apps": "{}",
    },
}
CALLERS = [
    ("templates/callers/licence.yml.tmpl", ".github/workflows/licence-check.yml"),
    ("templates/callers/scorecard.yml.tmpl", ".github/workflows/scorecard-check.yml"),
    ("templates/callers/sign-off.yml.tmpl", ".github/workflows/sign-off-check.yml"),
]

BASE_INLINE = """\
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
---
# Header comment line 1
# Header comment line 2
name: Inline test
jobs:
  call:
    # A comment inside jobs
    uses: ./.github/workflows/inline.yml
    with:
      runner: ubuntu-26.04
  tail:
    uses: ./.github/workflows/other.yml
    with:
      required: '["A"]'
"""


def comparable(text: str) -> list[str]:
    """The lines of a caller with allow-listed copyright, headers, uses, and required."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    if lines and lines[0].startswith("# SPDX-FileCopyrightText: "):
        out.append("# SPDX-FileCopyrightText: <ALLOW-LISTED>")
        i = 1

    while i < len(lines):
        line = lines[i]
        if line == "---":
            out.append(line)
            i += 1
            has_comments = False
            while i < len(lines) and lines[i].startswith("#"):
                has_comments = True
                i += 1
            if has_comments:
                out.append("# <ALLOW-LISTED-HEADER-COMMENT>")
            continue

        if CALL_USES.match(line):
            path = re.search(r"uses: (\S+)", line).group(1)  # type: ignore[union-attr]
            leaf = path.split("/")[-1].split("@")[0]
            out.append(f"    uses: <ALLOW-LISTED:{leaf}>")
            i += 1
            continue

        if REQUIRED_LINE.match(line):
            out.append("      required: <ALLOW-LISTED>")
            i += 1
            continue

        out.append(line)
        i += 1

    return out


@pytest.mark.parametrize(
    ("template_path", "own_path"),
    CALLERS,
    ids=["licence", "scorecard", "sign-off"],
)
def test_own_caller_equals_rendered_template(template_path: str, own_path: str) -> None:
    """The repository's own caller matches the rendered template except allow-listed parts."""
    context = (
        OWN_CONTEXT
        if own_path.endswith(("licence-check.yml", "sign-off-check.yml"))
        else render.SAMPLE
    )
    template_text = (ROOT / template_path).read_text(encoding="utf-8")
    rendered = render.render(template_text, context, template_path)
    own = (ROOT / own_path).read_text(encoding="utf-8")

    own_lines = comparable(own)
    rendered_lines = comparable(rendered)
    diff = "\n".join(
        difflib.unified_diff(
            rendered_lines,
            own_lines,
            fromfile=f"rendered/{template_path}",
            tofile=own_path,
            lineterm="",
        )
    )
    assert own_lines == rendered_lines, f"caller and rendered template differ:\n{diff}"


def _ruleset_required_contexts() -> set[str]:
    data = json.loads(RULESET.read_text(encoding="utf-8"))
    for rule in data.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters") or {}
        checks = params.get("required_status_checks") or []
        return {
            str(item["context"]) for item in checks if isinstance(item, dict) and "context" in item
        }
    raise AssertionError("ruleset has no required_status_checks rule")


@pytest.mark.parametrize(
    "own_path",
    [".github/workflows/licence-check.yml", ".github/workflows/sign-off-check.yml"],
)
def test_own_tail_required_matches_ruleset(own_path: str) -> None:
    """Each own required-checks tail lists exactly the ruleset required contexts."""
    doc = yaml.safe_load((ROOT / own_path).read_text(encoding="utf-8"))
    tail = doc["jobs"]["required-checks"]
    listed = set(json.loads(tail["with"]["required"]))
    assert listed == _ruleset_required_contexts()


def test_comparable_equal_planted_cases() -> None:
    """Planted cases where only copyright, header block, uses or required differ."""
    variant_copyright = BASE_INLINE.replace(
        "# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>",
        "# SPDX-FileCopyrightText: 2026 Other <other@example.org>",
    )
    assert comparable(BASE_INLINE) == comparable(variant_copyright)

    variant_header = BASE_INLINE.replace(
        "---\n# Header comment line 1\n# Header comment line 2\n",
        "---\n# Single different header comment\n",
    )
    assert comparable(BASE_INLINE) == comparable(variant_header)

    variant_uses = BASE_INLINE.replace(
        "./.github/workflows/inline.yml",
        "owner/shared/.github/workflows/inline.yml@commit # v1.0.0",
    )
    assert comparable(BASE_INLINE) == comparable(variant_uses)

    variant_required = BASE_INLINE.replace("required: '[\"A\"]'", "required: '[\"B\"]'")
    assert comparable(BASE_INLINE) == comparable(variant_required)


def test_comparable_unequal_planted_cases() -> None:
    """Planted cases where disallowed differences cause inequality in both directions."""
    variant_jobs_comment = BASE_INLINE.replace(
        "    # A comment inside jobs",
        "    # A different comment inside jobs",
    )
    assert comparable(BASE_INLINE) != comparable(variant_jobs_comment)

    variant_with = BASE_INLINE.replace(
        "      runner: ubuntu-26.04",
        "      runner: ubuntu-24.04",
    )
    assert comparable(BASE_INLINE) != comparable(variant_with)

    variant_tail_if = BASE_INLINE.replace(
        "  tail:\n    uses:",
        "  tail:\n    if: always()\n    uses:",
    )
    assert comparable(BASE_INLINE) != comparable(variant_tail_if)

    second_uses_variant = BASE_INLINE.replace(
        "uses: ./.github/workflows/other.yml",
        "uses: ./.github/workflows/different.yml",
    )
    assert comparable(BASE_INLINE) != comparable(second_uses_variant)
