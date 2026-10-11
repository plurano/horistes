# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Finds the workflow and action files of a tree and reads them strictly.

The reader refuses a mapping with two keys of the same name, and reads only
true and false as booleans, so the `on` key of a workflow stays the string
"on" instead of becoming True as YAML 1.1 has it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

BOOL_TAG = "tag:yaml.org,2002:bool"


class StrictLoader(yaml.SafeLoader):
    """SafeLoader without the yes/no/on/off booleans and without duplicate keys."""


StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
StrictLoader.add_implicit_resolver(
    BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def _mapping(loader: StrictLoader, node: yaml.MappingNode) -> dict:
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


class ReadError(Exception):
    """A workflow or action file that cannot be read as a mapping."""


def say(line: str) -> None:
    """Write one line of a tool's result to standard output."""
    sys.stdout.write(line + "\n")


def complain(line: str) -> None:
    """Write one line of a finding or an error to standard error."""
    sys.stderr.write(line + "\n")


def load(path: Path) -> dict:
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise ReadError(f"{path}: {err}") from err
    if not isinstance(doc, dict):
        raise ReadError(f"{path}: not a mapping")
    return doc


def files(root: Path) -> list[tuple[str, str, dict]]:
    """Every workflow and composite action below root as (path, kind, document).

    Workflows live in .github/workflows (the forge reads no other directory),
    actions in actions/<name>/action.yml.
    """
    found = []
    for pattern, kind in (
        (".github/workflows/*.y*ml", "workflow"),
        ("actions/*/action.y*ml", "action"),
    ):
        for path in sorted(root.glob(pattern)):
            if path.suffix in (".yml", ".yaml"):
                found.append((path.relative_to(root).as_posix(), kind, load(path)))
    return found


def strings(node: object):
    """Every string below a node, without recursion: the depth of a document is not bounded."""
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def triggers(doc: dict) -> dict:
    """The `on` block as a mapping from event name to its settings."""
    on = doc.get("on")
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(event): {} for event in on}
    if isinstance(on, dict):
        return {str(event): (settings or {}) for event, settings in on.items()}
    return {}


def write_scopes(permissions: object) -> list[str]:
    """Scopes a permissions block asks to write; `write-all` and an absent block are named."""
    if permissions is None:
        return ["<unset>"]
    if isinstance(permissions, str):
        return ["<write-all>"] if permissions == "write-all" else []
    if isinstance(permissions, dict):
        return sorted(str(scope) for scope, level in permissions.items() if level == "write")
    return ["<unreadable>"]
