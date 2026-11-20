# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The expression reader the tests of the shared workflows play calls with."""

from __future__ import annotations

import pytest
from expressions import evaluate, interpolate


@pytest.mark.parametrize(
    ("text", "inputs", "value"),
    [
        ("inputs.outside_hosts", {"outside_hosts": True}, True),
        ("${{ !inputs.outside_hosts }}", {"outside_hosts": True}, False),
        ("${{ inputs.a && 'A' || 'B' }}", {"a": False}, "B"),
        ("${{ inputs.a && 'A' || 'B' }}", {"a": True}, "A"),
        ("inputs.a || inputs.b", {"a": False, "b": False}, False),
        ("inputs.root == '.'", {"root": "."}, True),
        ("inputs.root == '.'", {"root": "./"}, False),
        ("inputs.root == 'Sub'", {"root": "sub"}, True),
        ("!inputs.root == '.'", {"root": "."}, False),
        ("format('lint ({0})', inputs.root)", {"root": "a/b"}, "lint (a/b)"),
        ("${{ inputs.r == '.' && 'X' || format('X ({0})', inputs.r) }}", {"r": "."}, "X"),
        ("${{ inputs.r == '.' && 'X' || format('X ({0})', inputs.r) }}", {"r": ""}, "X ()"),
    ],
)
def test_expression_reader(text, inputs, value):
    assert evaluate(text, inputs) == value


@pytest.mark.parametrize(
    "text",
    ["github.event_name == 'push'", "format('{0}{1}', inputs.a, inputs.b)", "inputs.a != 'b'"],
)
def test_expression_reader_refuses_what_it_does_not_read(text):
    with pytest.raises(ValueError, match="not read here"):
        evaluate(text, {"a": "x", "b": "y"})


@pytest.mark.parametrize("owner,name", [("A", "B"), ("a" * 39, "b" * 100), ("", "")])
def test_matrix_and_multiple_expression_parts(owner, name):
    text = "release-${{ inputs.app }}-${{ matrix.repository.owner }}-${{ matrix.repository.name }}"
    contexts = {"matrix": {"repository": {"owner": owner, "name": name}}}
    assert interpolate(text, {"app": "123"}, **contexts) == f"release-123-{owner}-{name}"
    assert evaluate("matrix.repository.owner", {}, **contexts) == owner
    assert interpolate("literal", {}) == "literal"
    assert interpolate("${{ github.run_id }}", {}, github={"run_id": 42}) == "42"


@pytest.mark.parametrize(
    "text",
    [
        "matrix.missing",
        "matrix.repository[0]",
        "matrix.",
        "matrix.repository.owner()",
        "${{ matrix.repository.owner",
        "${{ matrix.repository.owner }}}}",
    ],
)
def test_interpolation_refuses_unknown_forms(text):
    with pytest.raises(ValueError, match="not read here"):
        if text.startswith("matrix"):
            evaluate(text, {}, matrix={"repository": {"owner": "a"}})
        else:
            interpolate(text, {}, matrix={"repository": {"owner": "a"}})
