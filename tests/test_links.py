# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Relative Markdown links: every relative link points to an existing file and heading anchor."""

from __future__ import annotations

import subprocess
import sys

from conftest import ROOT, Tree


def check(tree: Tree, files: dict[str, str], *args: str) -> subprocess.CompletedProcess:
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


def test_root_absolute_link_reads_from_the_repository_root(tree):
    """A link starting with `/` names a file from the repository root, not the linking folder."""
    good = {"docs/guide.md": "See [readme](/README.md).\n", "README.md": "# Readme\n"}
    assert check(tree, good).returncode == 0
    planted = {"docs/guide.md": "See [readme](/docs/README.md).\n", "README.md": "# Readme\n"}
    done = check(tree, planted)
    assert done.returncode == 1, done.stdout
    assert "docs/guide.md: target `/docs/README.md` does not exist" in done.stderr
