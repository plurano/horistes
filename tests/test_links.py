# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Relative Markdown links: every relative link points to an existing file and heading anchor."""

from __future__ import annotations

import subprocess
import sys
import time

import links
import pytest
from conftest import ROOT, Tree


def check(tree: Tree, files: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    """Check links in a tracked fixture tree populated with the supplied files."""
    tree.write(files)
    tree.git("init", "-q", "-b", "main")
    tree.git("add", "-A")
    tree.git("commit", "-q", "-m", "fixture")
    return tree.tool("links.py", *args)


def test_current_repository_links_pass():
    """Every relative link and anchor in the repository Markdown files exists."""
    done = subprocess.run(
        [sys.executable, "-B", str(ROOT / "tools" / "links.py"), "--root", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout


def test_planted_missing_file_fails(tree):
    """A relative link pointing to a missing file fails with its message."""
    good = {"README.md": "See [target](doc.md) for details.\n", "doc.md": "# Title\n"}
    assert check(tree, good).returncode == 0
    planted = {"README.md": "See [target](missing.md) for details.\n"}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "README.md: target `missing.md` does not exist" in done.stderr


def test_planted_missing_anchor_fails(tree):
    """A link to a Markdown file with an absent heading anchor fails with its message."""
    good = {"README.md": "See [section](doc.md#title).\n", "doc.md": "# Title\n"}
    assert check(tree, good).returncode == 0
    planted = {"README.md": "See [section](doc.md#missing).\n", "doc.md": "# Title\n"}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "README.md: anchor `#missing` not found in `doc.md`" in done.stderr


def test_external_url_and_pure_anchor_and_fenced_link_ignored(tree):
    """External URLs, pure anchors, and links inside fenced code blocks are ignored."""
    content = (
        "# Heading\n\n[ext](https://example.com/missing.md) [anchor](#heading)\n"
        "```markdown\n[fenced](missing.md)\n```\n"
        "[ref]: https://example.com/ref.md\n[valid](doc.md#intro)\n"
    )
    files = {"README.md": content, "doc.md": "# Intro\n"}
    done = check(tree, files)
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout
    broken = content.replace("```markdown\n", "").replace("```\n", "")
    assert check(tree, {"README.md": broken, "doc.md": "# Intro\n"}).returncode == 1


def test_planted_inline_code_links_ignored(tree):
    """Planted links in code pass; removing the first span's backticks exposes its defect."""
    content = (
        "`[a](missing.md)`\n"
        "`^[A-Za-z0-9](?:[A-Za-z0-9._/: -]{0,98}[A-Za-z0-9._/:-])?$`\n"
        "`` [b](gone.md) ``\n"
    )
    done = check(tree, {"README.md": content})
    assert done.returncode == 0, done.stderr
    assert "PASS:" in done.stdout
    planted = content.replace("`[a](missing.md)`", "[a](missing.md)")
    done = check(tree, {"README.md": planted})
    assert done.returncode == 1, done.stdout
    assert "README.md: target `missing.md` does not exist" in done.stderr


@pytest.mark.parametrize(
    "content",
    [
        "[`code`](missing.md)\n",
        "`x` [a](missing.md)\n",
        "a ` b [a](missing.md)\n",
        "``x` [a](missing.md)\n",
    ],
)
def test_boundary_links_outside_code_spans_still_read(tree, content):
    """Boundary links outside closed spans or after unmatched backticks still fail."""
    done = check(tree, {"README.md": content})
    assert done.returncode == 1, done.stdout
    assert "README.md: target `missing.md` does not exist" in done.stderr


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", ""),
        ("plain text", "plain text"),
        ("`[a](missing.md)`", " " * 17),
        ("`` [b](gone.md) ``", " " * 18),
        ("[`code`](doc.md)", "[" + " " * 6 + "](doc.md)"),
        ("`x` [a](missing.md)", " " * 3 + " [a](missing.md)"),
        ("a ` b [a](missing.md)", "a ` b [a](missing.md)"),
        ("``x` [a](missing.md)", "``x` [a](missing.md)"),
        ("``x`y``", " " * 7),
        ("`x``y`", " " * 6),
        ("`x` a `y`", " " * 3 + " a " + " " * 3),
        ("``x`y`", "``x" + " " * 3),
        ("`x\ny`", "`x\ny`"),
        ("[a](x`y`.md)", "[a](x`y`.md)"),
        ("[not a `link](/foo`)", "[not a " + " " * 12 + ")"),
        (r"\`[a](missing.md) `", r"\`[a](missing.md) `"),
        (r"\``[a](m.md)`", "\\`" + " " * 11),
        (r"\\`[a](m.md)`", "\\\\" + " " * 11),
        (r"`a\`[b](m.md)`", " " * 4 + "[b](m.md)`"),
        ("[a](x`y` z", "[a](x" + " " * 3 + " z"),
        ("[a [b] c](d`e`.md)", "[a [b] c](d`e`.md)"),
        ("![i](p`q`.png)", "![i](p`q`.png)"),
        ("[a](<x`y`.md>)", "[a](<x`y`.md>)"),
    ],
)
def test_boundary_without_code_spans_preserves_spacing(line, expected):
    """Boundary masking preserves length and requires complete matching backtick runs."""
    assert links.without_code_spans(line) == expected


@pytest.mark.parametrize(
    ("content", "target"),
    [
        ("[a](x`y`.md)\n", "x`y`.md"),
        ("[r]: a`b`.md\n", "a`b`.md"),
    ],
)
@pytest.mark.parametrize("present", [True, False])
def test_backticks_in_destinations_remain_literal(tree, content, target, present):
    """Literal destination backticks pass with a file and fail when that file is absent."""
    files = {"README.md": content}
    if present:
        files[target] = "# Target\n"
    done = check(tree, files)
    if present:
        assert done.returncode == 0, done.stderr
    else:
        assert done.returncode == 1, done.stdout
        assert f"README.md: target `{target}` does not exist" in done.stderr


def test_escaped_backtick_opener_keeps_link_visible(tree):
    """An escaped opener leaves a missing relative link visible to the checker."""
    done = check(tree, {"README.md": "\\`[a](missing.md) `\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md: target `missing.md` does not exist" in done.stderr


def test_reference_definition_read_from_raw_line(tree):
    """A code span near a reference use does not hide its raw missing-file definition."""
    done = check(tree, {"README.md": "`x` [r]\n\n[r]: missing.md\n"})
    assert done.returncode == 1, done.stdout
    assert "README.md: target `missing.md` does not exist" in done.stderr


def test_without_code_spans_growth_is_bounded():
    """Bound 64x input growth below 200x time; old cubic growth predicts about 512x."""
    timings = []
    for size in (100, 800):
        line = "a".join("`" * length for length in range(1, size + 1)) + " [x](missing.md)"
        samples = []
        for _ in range(5):
            start = time.perf_counter()
            masked = links.without_code_spans(line)
            samples.append(time.perf_counter() - start)
            assert masked.endswith(" [x](missing.md)")
        timings.append(min(samples))
    assert timings[1] / max(timings[0], 1e-4) < 200


def test_root_absolute_link_reads_from_the_repository_root(tree):
    """A link starting with `/` names a file from the repository root, not the linking folder."""
    good = {"docs/guide.md": "See [readme](/README.md).\n", "README.md": "# Readme\n"}
    assert check(tree, good).returncode == 0
    planted = {"docs/guide.md": "See [readme](/docs/README.md).\n", "README.md": "# Readme\n"}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "docs/guide.md: target `/docs/README.md` does not exist" in done.stderr
