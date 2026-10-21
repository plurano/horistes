# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Reads the result file a job uploaded, and compares the results of two jobs.

A job such as a scorecard run passes whatever it finds: what it says is in
the SARIF file it uploads. verdict.py compares that file for the old and the
new job (class U). Of one file two sets are kept:

    checks    every rule the tool lists, and the rule of every result
    findings  one line per result:
              <rule> | <file>:<line> | <first line of the message> | <12 hex>

The hex digits are the SHA-256 over the other lines of the message in sorted
order: the order of the detail lines does not count, their text does. What
names the run itself (time, identifier of the analysis) is left out.

Record of one result, as verdict.py reads it and forge.py writes it:

    {"state": "read", "artifact": ..., "sha256": ..., "tools": [...],
     "checks": [...], "findings": [...]}
    {"state": "none" | "expired", "artifact": ...}
    {"state": "unreadable", "artifact": ..., "detail": ...}
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

SARIF_VERSION = "2.1.0"
MAX_BYTES = 20 * 1024 * 1024
MAX_RESULTS = 10_000
MAX_NAMED = 3
STATES = ("read", "none", "expired", "unreadable")


class ResultError(Exception):
    """A result file that cannot be read as what it must be."""


def _location(entry: object) -> str:
    physical = entry.get("physicalLocation") if isinstance(entry, dict) else None
    if not isinstance(physical, dict):
        return "-"
    artifact = physical.get("artifactLocation")
    region = physical.get("region")
    uri = artifact.get("uri", "-") if isinstance(artifact, dict) else "-"
    line = region.get("startLine", "") if isinstance(region, dict) else ""
    return f"{uri}:{line}"


def _finding(result: object) -> tuple[str, str]:
    """The rule of one result and the line that identifies the finding."""
    rule = result.get("ruleId") if isinstance(result, dict) else None
    if not isinstance(rule, str) or not rule:
        raise ResultError("a result names no rule")
    locations = result.get("locations") if isinstance(result, dict) else None
    if locations is not None and not isinstance(locations, list):
        raise ResultError("locations of a result are no list")
    places = sorted(_location(entry) for entry in locations or []) or ["-"]
    message = result.get("message")
    text = message.get("text") if isinstance(message, dict) else None
    lines = text.splitlines() if isinstance(text, str) else []
    rest = hashlib.sha256("\n".join(sorted(lines[1:])).encode()).hexdigest()[:12]
    return rule, f"{rule} | {', '.join(places)} | {lines[0] if lines else ''} | {rest}"


def _run(run: object) -> tuple[str, set[str], set[str]]:
    """The tool, the checks and the findings of one run of a SARIF log."""
    tool = run.get("tool") if isinstance(run, dict) else None
    driver = tool.get("driver") if isinstance(tool, dict) else None
    if not isinstance(driver, dict):
        raise ResultError("a run names no tool")
    results = run.get("results")
    rules = driver.get("rules")
    results = [] if results is None else results
    rules = [] if rules is None else rules
    if not isinstance(results, list) or not isinstance(rules, list):
        raise ResultError("rules or results of a run are no list")
    if len(results) > MAX_RESULTS:
        raise ResultError(f"{len(results)} results in one run, at most {MAX_RESULTS} are read")
    checks = {
        rule["id"] for rule in rules if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    }
    findings = set()
    for result in results:
        rule, finding = _finding(result)
        checks.add(rule)
        findings.add(finding)
    version = driver.get("semanticVersion") or driver.get("version") or ""
    return f"{driver.get('name')} {version}".strip(), checks, findings


def from_sarif(text: bytes | str) -> dict:
    """The checks and findings of a SARIF log."""
    try:
        doc = json.loads(text)
    except RecursionError as err:
        raise ResultError("result file is not JSON (nested too deep)") from err
    except ValueError as err:
        raise ResultError(f"result file is not JSON ({err})") from err
    runs = doc.get("runs") if isinstance(doc, dict) else None
    if not isinstance(runs, list) or doc.get("version") != SARIF_VERSION:
        raise ResultError(f"result file is no SARIF {SARIF_VERSION} log")
    tools, checks, findings = set(), set(), set()
    for run in runs:
        tool, found_checks, found = _run(run)
        tools.add(tool)
        checks |= found_checks
        findings |= found
    return {"tools": sorted(tools), "checks": sorted(checks), "findings": sorted(findings)}


def from_archive(data: bytes, digest: str) -> dict:
    """The result in an artifact's archive; `digest` is what the forge lists for it, or empty."""
    found = hashlib.sha256(data).hexdigest()
    if digest and digest != f"sha256:{found}":
        raise ResultError(f"archive is sha256:{found[:12]}, the forge lists {digest[:19]}")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [name for name in archive.namelist() if name.endswith(".sarif")]
            if len(members) != 1:
                raise ResultError(f"{len(members)} files *.sarif in the archive, one expected")
            with archive.open(members[0]) as member:
                text = member.read(MAX_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as err:
        raise ResultError(f"archive cannot be read ({err})") from err
    if len(text) > MAX_BYTES:
        raise ResultError(f"{members[0]} is larger than {MAX_BYTES} bytes")
    return {"sha256": found, **from_sarif(text)}


def problem(result: object) -> str:
    """Why a recorded result cannot be judged; empty when it can."""
    if not isinstance(result, dict) or result.get("state") not in STATES:
        return "no result is recorded"
    if result["state"] == "unreadable":
        return f"result is unreadable: {result.get('detail') or 'no reason recorded'}"
    if result["state"] != "read":
        return ""
    for key in ("checks", "findings"):
        entries = result.get(key)
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            return f"result record has no list `{key}`"
    return ""


def _only(old: dict, new: dict, key: str) -> list[str]:
    """What one result holds under `key` and the other does not, a few of them by name."""
    lines = []
    for side, mine, other in (("old", old, new), ("new", new, old)):
        alone = sorted(set(mine[key]) - set(other[key]))
        if alone:
            more = f" and {len(alone) - MAX_NAMED} more" if len(alone) > MAX_NAMED else ""
            lines.append(f"{key} only in the {side} result: {'; '.join(alone[:MAX_NAMED])}{more}")
    return lines


def compare(old: dict, new: dict, marker: re.Pattern | None) -> tuple[str, str, str]:
    """Row state for two judgeable results, whether the marker shows, and what differs."""
    states = (old["state"], new["state"])
    if "expired" in states:
        return "invalid", "-", "a result has expired on the forge"
    if states == ("none", "none"):
        return "no-evidence", "-", "neither job uploaded a result"
    if "none" in states:
        return (
            "differ",
            "-",
            f"the {'old' if states[0] == 'none' else 'new'} job uploaded no result",
        )
    if not old["checks"] and not new["checks"]:
        return "no-evidence", "-", "both results hold no check"
    differences = _only(old, new, "checks") + _only(old, new, "findings")
    if differences:
        tools = [", ".join(side.get("tools") or ["?"]) for side in (old, new)]
        told = f" (tools: old {tools[0]}, new {tools[1]})" if tools[0] != tools[1] else ""
        return "differ", "-", "; ".join(differences) + told
    if marker is None:
        return "equal", "-", ""
    return "equal", "yes" if any(marker.search(line) for line in old["findings"]) else "no", ""
