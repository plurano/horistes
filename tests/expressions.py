# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""A reader of the workflow expressions the shared workflows use.

It reads inputs and explicit matrix, github and vars context mappings, quoted
literals, `!`, `==`, `&&`, `||` and `format` with one input. Interpolation reads
several expression parts; `==` compares strings without case. Any other form
is an error, so a test never reads an expression it cannot read.
"""

from __future__ import annotations

import re

BRACES = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)
FORMAT = re.compile(r"^format\(\s*'([^']*)'\s*,\s*inputs\.(\w+)\s*\)$")
INPUT = re.compile(r"^(inputs|matrix|github|vars)\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)$")


def _operand(text: str, inputs: dict, contexts: dict) -> object:
    """An input, a quoted literal or a `format` of one input."""
    text = text.strip()
    found = FORMAT.match(text)
    if found:
        return found.group(1).replace("{0}", str(inputs[found.group(2)]))
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]
    found = INPUT.match(text)
    if found:
        value = inputs if found.group(1) == "inputs" else contexts.get(found.group(1), {})
        for key in found.group(2).split("."):
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"expression part not read here: {text!r}")
            value = value[key]
        return value
    raise ValueError(f"expression part not read here: {text!r}")


def _atom(text: str, inputs: dict, contexts: dict) -> object:
    """One operand or one comparison, with any leading `!`."""
    text = text.strip()
    negations = len(text) - len(text.lstrip("!"))
    text = text.lstrip("!").strip()
    if "==" in text:
        left, right = text.split("==", 1)
        sides = [str(_operand(side, inputs, contexts)).casefold() for side in (left, right)]
        value: object = sides[0] == sides[1]
    else:
        value = _operand(text, inputs, contexts)
    for _ in range(negations):
        value = not value
    return value


def evaluate(text: str, inputs: dict, **contexts: dict) -> object:
    """The value of one expression, with or without its braces."""
    match = BRACES.match(text.strip())
    value: object = False
    for alternative in (match.group(1) if match else text).split("||"):
        for part in alternative.split("&&"):
            value = _atom(part, inputs, contexts)
            if not value:
                break
        if value:
            return value
    return value


def interpolate(text: str, inputs: dict, **contexts: dict) -> str:
    """Evaluate every expression in a literal string; reject unmatched braces."""
    pattern = re.compile(r"\$\{\{([^{}]*)\}\}")
    result = pattern.sub(lambda match: str(evaluate(match.group(1), inputs, **contexts)), text)
    if "${{" in result or "}}" in result:
        raise ValueError(f"expression part not read here: {text!r}")
    return result
