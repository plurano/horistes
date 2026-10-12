# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""A reader of the workflow expressions the shared workflows use.

It reads inputs, quoted literals, `!`, `==`, `&&`, `||` and `format` with one
input, the way the forge reads them; `==` compares strings without case. Any
other form is an error, so a test never reads an expression it cannot read.
"""

from __future__ import annotations

import re

BRACES = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)
FORMAT = re.compile(r"^format\(\s*'([^']*)'\s*,\s*inputs\.(\w+)\s*\)$")
INPUT = re.compile(r"^inputs\.(\w+)$")


def _operand(text: str, inputs: dict) -> object:
    """An input, a quoted literal or a `format` of one input."""
    text = text.strip()
    found = FORMAT.match(text)
    if found:
        return found.group(1).replace("{0}", str(inputs[found.group(2)]))
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    found = INPUT.match(text)
    if found:
        return inputs[found.group(1)]
    raise ValueError(f"expression part not read here: {text!r}")


def _atom(text: str, inputs: dict) -> object:
    """One operand or one comparison, with any leading `!`."""
    text = text.strip()
    negations = len(text) - len(text.lstrip("!"))
    text = text.lstrip("!").strip()
    if "==" in text:
        left, right = text.split("==", 1)
        sides = [str(_operand(side, inputs)).casefold() for side in (left, right)]
        value: object = sides[0] == sides[1]
    else:
        value = _operand(text, inputs)
    for _ in range(negations):
        value = not value
    return value


def evaluate(text: str, inputs: dict) -> object:
    """The value of one expression, with or without its braces."""
    match = BRACES.match(text.strip())
    value: object = False
    for alternative in (match.group(1) if match else text).split("||"):
        for part in alternative.split("&&"):
            value = _atom(part, inputs)
            if not value:
                break
        if value:
            return value
    return value
