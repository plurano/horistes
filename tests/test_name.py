# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The repository's former address is written in no tracked file but two records.

Until 2026-10-12 the repository carried a working name
(docs/adr/0023-the-name-is-horistes.md). A caller line or a preset reference
that still names the former address is not redirected by the forge, so a file
that holds it fails here.

Two accepted records keep their text and with it the former preset reference;
they are named in `KEPT`. The bare working name is not refused: ids of the
fleet answer ledger begin with it and stay as they are.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

DEADLINE = 20  # seconds for one git call
OWNER = "plurano"
# Written in two parts, so that this file does not hold what it refuses.
FORMER = f"{OWNER}/" + "shared-ci"
KEPT = {
    "docs/adr/0020-shared-update-bot-values-as-a-preset.md",
    "docs/adr/0021-settings-sync-preset-switches-the-toolchain-manager-off.md",
}


def tracked(root: Path) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=True,
    )
    return [name for name in listed.stdout.split("\0") if name]


def holders(root: Path, names: list[str], kept: set[str]) -> list[str]:
    """The files among `names` that hold the former address and are not kept records."""
    found = []
    for name in names:
        path = root / name
        if name in kept or path.is_symlink() or not path.is_file():
            continue
        if FORMER.encode() in path.read_bytes():
            found.append(name)
    return found


def test_no_tracked_file_holds_the_former_address():
    names = tracked(ROOT)
    assert names, "git lists no tracked file"
    assert holders(ROOT, names, KEPT) == []


def test_each_kept_record_still_holds_it():
    # A record that no longer holds the address needs no place in the list.
    for name in sorted(KEPT):
        assert holders(ROOT, [name], set()) == [name], name


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        pytest.param(f'{{"extends": ["github>{FORMER}"]}}\n', True, id="preset-reference"),
        pytest.param(f"    uses: {FORMER}/.github/workflows/licence.yml@0123\n", True, id="caller"),
        pytest.param(f'{{"extends": ["github>{OWNER}/horistes"]}}\n', False, id="new-address"),
        pytest.param("id shared-ci-name of the answer ledger\n", False, id="ledger-id"),
        pytest.param("", False, id="empty-file"),
    ],
)
def test_a_planted_file_is_judged_by_the_address_alone(tmp_path: Path, text: str, refused: bool):
    (tmp_path / "planted.json").write_text(text, encoding="utf-8")
    assert holders(tmp_path, ["planted.json"], set()) == (["planted.json"] if refused else [])


def test_a_kept_name_is_passed_over_and_a_missing_file_is_no_holder(tmp_path: Path):
    (tmp_path / "record.md").write_text(f"`github>{FORMER}`\n", encoding="utf-8")
    assert holders(tmp_path, ["record.md", "gone.md"], {"record.md"}) == []
    assert holders(tmp_path, ["record.md", "gone.md"], set()) == ["record.md"]
