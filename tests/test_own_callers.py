# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The repository's own callers equal the rendered templates except allow-listed parts."""

from __future__ import annotations

import difflib
import re
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import render

CALL_USES = re.compile(r"^    uses: \S+(?: # \S+)?$")
CALLERS = [
    ("templates/callers/licence.yml.tmpl", ".github/workflows/licence-check.yml"),
    ("templates/callers/scorecard.yml.tmpl", ".github/workflows/scorecard-check.yml"),
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
"""


def comparable(text: str) -> list[str]:
    """The lines of a caller with three allow-listed parts replaced by markers."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    if lines and lines[0].startswith("# SPDX-FileCopyrightText: "):
        out.append("# SPDX-FileCopyrightText: <ALLOW-LISTED>")
        i = 1

    replaced_uses = False
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

        if not replaced_uses and CALL_USES.match(line):
            out.append("    uses: <ALLOW-LISTED>")
            replaced_uses = True
            i += 1
            continue

        out.append(line)
        i += 1

    return out


@pytest.mark.parametrize(
    ("template_path", "own_path"),
    CALLERS,
    ids=["licence", "scorecard"],
)
def test_own_caller_equals_rendered_template(template_path: str, own_path: str) -> None:
    """The repository's own caller matches the rendered template except allow-listed parts."""
    template_text = (ROOT / template_path).read_text(encoding="utf-8")
    rendered = render.render(template_text, render.SAMPLE, template_path)
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


def test_comparable_equal_planted_cases() -> None:
    """Planted cases where only copyright, header block or uses line differ."""
    variant_copyright = BASE_INLINE.replace(
        "# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>",
        "# SPDX-FileCopyrightText: 2026 Other <other@example.org>",
    )
    assert comparable(BASE_INLINE) == comparable(variant_copyright)
    assert comparable(variant_copyright) == comparable(BASE_INLINE)

    variant_header = BASE_INLINE.replace(
        "---\n# Header comment line 1\n# Header comment line 2\n",
        "---\n# Single different header comment\n",
    )
    assert comparable(BASE_INLINE) == comparable(variant_header)
    assert comparable(variant_header) == comparable(BASE_INLINE)

    variant_uses = BASE_INLINE.replace(
        "    uses: ./.github/workflows/inline.yml",
        "    uses: owner/shared/.github/workflows/inline.yml@commit # v1.0.0",
    )
    assert comparable(BASE_INLINE) == comparable(variant_uses)
    assert comparable(variant_uses) == comparable(BASE_INLINE)


def test_comparable_unequal_planted_cases() -> None:
    """Planted cases where disallowed differences cause inequality in both directions."""
    variant_jobs_comment = BASE_INLINE.replace(
        "    # A comment inside jobs",
        "    # A different comment inside jobs",
    )
    assert comparable(BASE_INLINE) != comparable(variant_jobs_comment)
    assert comparable(variant_jobs_comment) != comparable(BASE_INLINE)

    variant_with = BASE_INLINE.replace(
        "      runner: ubuntu-26.04",
        "      runner: ubuntu-24.04",
    )
    assert comparable(BASE_INLINE) != comparable(variant_with)
    assert comparable(variant_with) != comparable(BASE_INLINE)

    second_uses_base = BASE_INLINE + "  other:\n    uses: ./workflow-a.yml\n"
    second_uses_variant = BASE_INLINE + "  other:\n    uses: ./workflow-b.yml\n"
    assert comparable(second_uses_base) != comparable(second_uses_variant)
    assert comparable(second_uses_variant) != comparable(second_uses_base)

    comment_after_first_non_comment = BASE_INLINE.replace(
        "name: Inline test\n",
        "name: Inline test\n# Comment right after first non-comment line\n",
    )
    assert comparable(BASE_INLINE) != comparable(comment_after_first_non_comment)
    assert comparable(comment_after_first_non_comment) != comparable(BASE_INLINE)
