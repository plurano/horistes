# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Host scan: every host a workflow or action names is declared in hosts.toml.

    hosts.py check [--root DIR]
    hosts.py list [--root DIR]

A file may reach the forge's own hosts without declaration. Every other
host is declared for that file with a reason; in a reusable workflow it
also names the boolean input, off by default, that switches it on. An
outside action must be in the list of allowed actions, which records the
hosts the action reaches by itself. A declared host that is no longer
found fails too, so the list cannot go stale.

What the scan reads: URLs and registry paths in every string of the file,
container and service images, and the registries that package commands
contact without naming them (IMPLIED below). What it cannot see: a host a
tool contacts on its own. It is a review aid, not an egress filter.

Exit codes: 0 pass, 1 undeclared or stale host, 3 input error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

DECLARATION = "hosts.toml"
DYNAMIC = "<dynamic>"

EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)
URL = re.compile(r"\b(?:https?|git|ssh|oci|docker)://([^\s/'\"`)<>]*)")
REGISTRY_PATH = re.compile(r"(?<![\w./@:-])((?:[a-z0-9-]+\.)+(?:io|com|org|dev|net|sh))/[\w$.{-]")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")

# Package commands and the registries they contact without naming them. A
# line that carries --offline is taken at its word.
IMPLIED = (
    (
        re.compile(r"\b(?:pip3?|python3? -m pip)\s+install\b|\bpipx\s+(?:run|install)\b|\buvx?\s"),
        ("pypi.org", "files.pythonhosted.org"),
    ),
    (
        re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:ci|install|i|add|dlx|exec)\b|\bnpx\s"),
        ("registry.npmjs.org",),
    ),
    (
        re.compile(r"\bcargo\s+(?:fetch|install|build|test|check|clippy|update|doc|run|bench)\b"),
        ("crates.io", "index.crates.io", "static.crates.io"),
    ),
    (
        re.compile(r"\bgo\s+(?:install|get|mod\s+download|mod\s+verify|build|test|vet|run)\b"),
        ("proxy.golang.org", "sum.golang.org"),
    ),
    (re.compile(r"\bapt(?:-get)?\s+(?:install|update)\b"), ("<os-package-archive>",)),
    (re.compile(r"\bdocker\s+(?:pull|run|build|buildx)\b"), ("<container-images>",)),
)

# A step that runs make: the Makefile at the root is then part of what the file reaches.
MAKE = re.compile(r"(?:^|[\s;&|(])make\s+[\w-]", re.M)


def _image_host(image: str) -> str:
    image = EXPRESSION.sub("", image)
    if not image or "$" in image:
        return DYNAMIC
    first = image.split("/", 1)[0]
    return (
        first.split(":", 1)[0].lower()
        if "/" in image and ("." in first or ":" in first)
        else "docker.io"
    )


def _strings(node: object):
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


def _uses(doc: dict):
    """Every `uses:` of a workflow or composite action: job-level calls and steps."""
    jobs = doc.get("jobs") or {}
    holders = [job for job in jobs.values() if isinstance(job, dict)]
    holders.append(doc.get("runs") or {})
    for holder in holders:
        if isinstance(holder.get("uses"), str):
            yield holder["uses"]
        for step in holder.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                yield step["uses"]


def _text_hosts(text: str, origin: str, hosts: dict[str, str]) -> None:
    """Add the hosts one text names: URLs, registry paths and package commands."""
    plain = EXPRESSION.sub("", text)
    for authority in URL.findall(plain):
        host = authority.rsplit("@", 1)[-1].split(":", 1)[0].lower()
        known = host and "$" not in host and HOSTNAME.match(host)
        hosts.setdefault(host if known else DYNAMIC, f"{origin}URL")
    for host in REGISTRY_PATH.findall(plain):
        hosts.setdefault(host, f"{origin}registry path")
    for line in plain.splitlines():
        if "--offline" in line:
            continue
        for pattern, implied in IMPLIED:
            if pattern.search(line):
                command = pattern.search(line).group(0).strip()
                for host in implied:
                    hosts.setdefault(host, f"{origin}command `{command}`")


def found_hosts(doc: dict, actions: dict, makefile: str = "") -> tuple[dict[str, str], list[str]]:
    """Hosts a document names, each with what named it, and the outside actions not allowed.

    A document that runs `make` reaches what the repository's Makefile reaches,
    so the Makefile's text is read as part of it.
    """
    hosts: dict[str, str] = {}
    unknown = []
    runs_make = False
    for text in _strings(doc):
        _text_hosts(text, "", hosts)
        runs_make = runs_make or bool(MAKE.search(EXPRESSION.sub("", text)))
    if runs_make and makefile:
        _text_hosts(makefile, "Makefile, ", hosts)
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        images = [job.get("container")] + list((job.get("services") or {}).values())
        for image in images:
            image = image.get("image") if isinstance(image, dict) else image
            if isinstance(image, str):
                hosts.setdefault(_image_host(image), "container image")
    for uses in _uses(doc):
        if uses.startswith("./"):
            continue
        if uses.startswith("docker://"):
            hosts.setdefault(_image_host(uses[len("docker://") :]), "docker action")
            continue
        name = uses.split("@", 1)[0]
        if name not in actions:
            if name not in unknown:
                unknown.append(name)
            continue
        for host in actions[name]:
            hosts.setdefault(host, f"action {name}")
    return hosts, unknown


def _declaration(root: Path) -> dict:
    path = root / DECLARATION
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    if not isinstance(data.get("forge"), list) or not data["forge"]:
        raise ciwf.ReadError(f"{path}: `forge` must list the forge's hosts")
    return data


def _declared_problems(path: str, doc: dict, reusable: bool, entry: dict, outside: dict) -> list:
    """What is wrong with the hosts declared for one file."""
    problems = []
    inputs = (ciwf.triggers(doc).get("workflow_call") or {}).get("inputs") or {} if reusable else {}
    for host, spec in sorted(entry.items()):
        if host not in outside:
            problems.append(f"{path}: declared host {host} is not found in the file any more")
        if not isinstance(spec, dict) or not str(spec.get("reason", "")).strip():
            problems.append(f"{path}: host {host} is declared without a reason")
            continue
        if reusable:
            switch = inputs.get(spec.get("input")) or {}
            if switch.get("type") != "boolean" or switch.get("default") is not False:
                problems.append(
                    f"{path}: host {host} needs `input` naming a boolean input "
                    "that defaults to false"
                )
    return problems


def _file_problems(path: str, kind: str, doc: dict, declared: dict, show: bool) -> list:
    """What is wrong with one workflow or action file."""
    hosts, unknown = found_hosts(doc, declared.get("actions", {}), declared.get("<makefile>", ""))
    entry = declared.get("files", {}).get(path, {})
    forge = set(declared["forge"])
    outside = {host: why for host, why in hosts.items() if host not in forge}
    if show:
        named = ", ".join(f"{h} ({w})" for h, w in sorted(outside.items()))
        ciwf.say(f"{path}: {named or 'forge only'}")
    problems = [
        f"{path}: action {name} is not in the allowed list ([actions] in {DECLARATION})"
        for name in unknown
    ]
    for host, why in sorted(outside.items()):
        if host not in entry:
            problems.append(f"{path}: host {host} ({why}) is not declared")
    reusable = kind == "workflow" and "workflow_call" in ciwf.triggers(doc)
    return problems + _declared_problems(path, doc, reusable, entry, outside)


def check(root: Path, show: bool) -> int:
    try:
        declared = _declaration(root)
        documents = ciwf.files(root)
        makefile = root / "Makefile"
        declared["<makefile>"] = makefile.read_text(encoding="utf-8") if makefile.exists() else ""
    except ciwf.ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    problems = []
    used_actions = set()
    for path, kind, doc in documents:
        used_actions.update(
            u.split("@", 1)[0] for u in _uses(doc) if not u.startswith(("./", "docker://"))
        )
        problems += _file_problems(path, kind, doc, declared, show)
    known_paths = {path for path, _, _ in documents}
    for path in sorted(set(declared.get("files", {})) - known_paths):
        problems.append(f"{DECLARATION}: [files] names {path}, which does not exist")
    for name in sorted(set(declared.get("actions", {})) - used_actions):
        problems.append(f"{DECLARATION}: [actions] allows {name}, which no file uses")
    if show:
        return 0
    for line in problems:
        ciwf.complain(f"FAIL: {line}")
    if problems:
        return 1
    ciwf.say(
        f"PASS: {len(documents)} files, {len(used_actions)} outside actions, every host declared"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check", "list"))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    return check(args.root, show=args.command == "list")


if __name__ == "__main__":
    sys.exit(main())
