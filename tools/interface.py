# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Interface lock of the reusable workflows and composite actions.

    interface.py snapshot [--root DIR]
    interface.py check [--root DIR] [--base-ref REF]

The lock (interface.lock.json) records what a caller depends on: inputs,
secrets, outputs, the names of the jobs (the forge reports a check under
them) and the scopes a workflow asks to write. `check` fails when the lock
does not describe the tree, and, with --base-ref, when the tree breaks the
interface recorded at that ref without a higher `interface_major`.

Exit codes: 0 pass, 1 the lock is out of date or a break is undeclared,
3 input error (unreadable file, unknown base ref).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

LOCK = "interface.lock.json"
# The forge sends the null commit as the state before a push that creates the
# branch, as on the first push to a new repository; SHA-1 and SHA-256 forms.
NULL_IDS = frozenset({"0" * 40, "0" * 64})


def _inputs(block: object) -> dict:
    out = {}
    for name, spec in (block or {}).items():
        spec = spec or {}
        out[str(name)] = {
            "type": str(spec.get("type", "string")),
            "required": bool(spec.get("required", False)),
            "default": spec.get("default"),
        }
    return out


def describe(root: Path) -> dict:
    workflows, actions = {}, {}
    for path, kind, doc in ciwf.files(root):
        if kind == "action":
            actions[path] = {
                "inputs": {
                    n: {k: v for k, v in s.items() if k != "type"}
                    for n, s in _inputs(doc.get("inputs")).items()
                },
                "outputs": sorted(str(n) for n in (doc.get("outputs") or {})),
            }
            continue
        events = ciwf.triggers(doc)
        if "workflow_call" not in events:
            continue
        call = events["workflow_call"]
        jobs = doc.get("jobs") or {}
        scopes = set()
        for job in jobs.values():
            block = (
                job.get("permissions", doc.get("permissions")) if isinstance(job, dict) else None
            )
            scopes.update(ciwf.write_scopes(block))
        workflows[path] = {
            "inputs": _inputs(call.get("inputs")),
            "secrets": {
                str(n): {"required": bool((s or {}).get("required", False))}
                for n, s in (call.get("secrets") or {}).items()
            },
            "outputs": sorted(str(n) for n in (call.get("outputs") or {})),
            "jobs": {
                str(i): str(j.get("name", i)) if isinstance(j, dict) else str(i)
                for i, j in jobs.items()
            },
            "write_scopes": sorted(scopes),
        }
    return {"workflows": workflows, "actions": actions}


def breaks(old: dict, new: dict) -> list[str]:
    """Changes from old to new that a caller pinned to old cannot absorb."""
    found = []
    for group in ("workflows", "actions"):
        for path, was in old.get(group, {}).items():
            now = new.get(group, {}).get(path)
            if now is None:
                found.append(f"{path}: removed")
                continue
            for name, spec in was.get("inputs", {}).items():
                cur = now["inputs"].get(name)
                if cur is None:
                    found.append(f"{path}: input {name} removed")
                elif cur.get("type") != spec.get("type"):
                    found.append(
                        f"{path}: input {name} changed type {spec.get('type')} -> {cur.get('type')}"
                    )
                elif cur["required"] and not spec["required"]:
                    found.append(f"{path}: input {name} became required")
            for name, spec in now["inputs"].items():
                if name not in was.get("inputs", {}) and spec["required"]:
                    found.append(f"{path}: new required input {name}")
            for name in was.get("outputs", []):
                if name not in now["outputs"]:
                    found.append(f"{path}: output {name} removed")
            if group == "actions":
                continue
            for name, spec in was.get("secrets", {}).items():
                cur = now["secrets"].get(name)
                if cur is None:
                    found.append(f"{path}: secret {name} removed")
                elif cur["required"] and not spec["required"]:
                    found.append(f"{path}: secret {name} became required")
            for name, spec in now["secrets"].items():
                if name not in was.get("secrets", {}) and spec["required"]:
                    found.append(f"{path}: new required secret {name}")
            for job, name in was.get("jobs", {}).items():
                cur = now["jobs"].get(job)
                if cur is None:
                    found.append(f"{path}: job {job} removed (check {name!r} disappears)")
                elif cur != name:
                    found.append(f"{path}: check name of job {job} changed {name!r} -> {cur!r}")
            for scope in now["write_scopes"]:
                if scope not in was.get("write_scopes", []):
                    found.append(f"{path}: new write scope {scope}")
    return found


def _read_lock(root: Path) -> dict:
    path = root / LOCK
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    if not isinstance(lock, dict) or not isinstance(lock.get("interface_major"), int):
        raise ciwf.ReadError(f"{path}: no integer interface_major")
    return lock


def _base_lock(root: Path, ref: str) -> dict:
    """The lock at ref; a ref that exists but holds no lock has an empty interface."""

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
        )

    if ref in NULL_IDS:
        ciwf.say(
            f"base {ref} is the null commit (a push that creates the branch):"
            " every entry counts as new"
        )
        return {"interface_major": -1, "workflows": {}, "actions": {}}
    if git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        raise ciwf.ReadError(f"base ref {ref} is not a commit here")
    prefix = git("rev-parse", "--show-prefix").stdout.strip()
    if git("cat-file", "-e", f"{ref}:{prefix}{LOCK}").returncode != 0:
        ciwf.say(f"base {ref} holds no {LOCK}: every entry counts as new")
        return {"interface_major": -1, "workflows": {}, "actions": {}}
    shown = git("show", f"{ref}:{prefix}{LOCK}")
    if shown.returncode != 0:
        raise ciwf.ReadError(f"cannot read {LOCK} at {ref}: {shown.stderr.strip()}")
    try:
        return json.loads(shown.stdout)
    except ValueError as err:
        raise ciwf.ReadError(f"{LOCK} at {ref}: {err}") from err


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("snapshot", "check"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base-ref")
    args = parser.parse_args()
    try:
        current = describe(args.root)
        if args.command == "snapshot":
            major = _read_lock(args.root)["interface_major"] if (args.root / LOCK).exists() else 0
            text = (
                json.dumps({"interface_major": major, **current}, indent=2, sort_keys=True) + "\n"
            )
            (args.root / LOCK).write_text(text, encoding="utf-8")
            ciwf.say(
                f"wrote {LOCK}: {len(current['workflows'])} reusable workflows, "
                f"{len(current['actions'])} actions"
            )
            return 0
        lock = _read_lock(args.root)
        base = _base_lock(args.root, args.base_ref) if args.base_ref else None
    except ciwf.ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    counts = f"{len(current['workflows'])} reusable workflows, {len(current['actions'])} actions"
    if {k: lock.get(k) for k in ("workflows", "actions")} != current:
        ciwf.complain(
            f"FAIL: {LOCK} does not describe the tree ({counts}); "
            "run `make interface-snapshot` and review the diff"
        )
        return 1
    if base is None:
        ciwf.say(
            f"PASS: {LOCK} describes the tree ({counts}); no base ref given, breaks not checked"
        )
        return 0
    broken = breaks(base, lock)
    if broken and lock["interface_major"] <= base.get("interface_major", -1):
        for line in broken:
            ciwf.complain(f"BREAK: {line}")
        ciwf.complain(
            f"FAIL: {len(broken)} break(s) against {args.base_ref} "
            f"with interface_major still {lock['interface_major']}; raise it"
        )
        return 1
    ciwf.say(
        f"PASS: {LOCK} describes the tree ({counts}); {len(broken)} break(s) "
        f"against {args.base_ref}, interface_major {lock['interface_major']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
