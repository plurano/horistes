# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""The strict reader: `on` stays a string, a duplicate key is refused."""

from __future__ import annotations

import sys

import pytest
from conftest import REUSABLE, ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf


def test_on_key_is_a_string_and_booleans_survive(tree):
    tree.write({".github/workflows/licence.yml": REUSABLE})
    ((path, kind, doc),) = ciwf.files(tree.root)
    assert (path, kind) == (".github/workflows/licence.yml", "workflow")
    assert "workflow_call" in ciwf.triggers(doc)
    assert doc["on"]["workflow_call"]["inputs"]["publish"]["default"] is False


def test_duplicate_key_is_refused(tree):
    doubled = REUSABLE.replace(
        "permissions:\n  contents: read\n",
        "permissions:\n  contents: read\npermissions:\n  contents: write\n",
    )
    assert doubled != REUSABLE
    tree.write({".github/workflows/licence.yml": doubled})
    with pytest.raises(ciwf.ReadError, match="duplicate key 'permissions'"):
        ciwf.files(tree.root)


def test_write_scopes_names_an_absent_block():
    assert ciwf.write_scopes(None) == ["<unset>"]
    assert ciwf.write_scopes("write-all") == ["<write-all>"]
    assert ciwf.write_scopes({"contents": "read", "id-token": "write"}) == ["id-token"]
