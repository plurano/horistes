#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Bounded Git input and surviving decision-record diff coordinates."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import ciwf
from ciwf import NULL_IDS

RECORD = re.compile(r"docs/adr/\d{4}-[^/]+\.md")
DEADLINE_SECONDS = 30
# Records held 88 files and the tree 640 tracked files on 2026-11-16.
MAX_FILES = 20_000
MAX_LINES = 20_000
MAX_SOURCE_BYTES = 1_000_000
# Old + new bytes, hunk/line headers, and a 1024-byte allowance per tracked path.
MAX_GIT_BYTES = 2 * MAX_SOURCE_BYTES + MAX_LINES * 128 + MAX_FILES * 1024
GIT_CHUNK_BYTES = 64 * 1024
ATTRIBUTES = ("filter", "working-tree-encoding")

# Against the work tree, so a local run judges uncommitted records too.
DIFF = (
    "diff",
    "--unified=0",
    "--inter-hunk-context=0",
    "--no-renames",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--text",
    "--default-prefix",
)
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class GitOutput:
    """One bounded reader's bytes and error, inspected only after joining it."""

    data: bytearray = field(default_factory=bytearray)
    exceeded: bool = False
    error: OSError | None = None


def _read_git(stream: BinaryIO, output: GitOutput) -> None:
    """One worker, at most budget/chunk + 1 fixed-size reads, including the EOF probe."""
    try:
        with stream:
            for _ in range(MAX_GIT_BYTES // GIT_CHUNK_BYTES + 1):
                chunk = stream.read(GIT_CHUNK_BYTES)
                if not chunk:
                    break
                output.data.extend(chunk)
                if len(output.data) > MAX_GIT_BYTES:
                    output.exceeded = True
                    break
    except OSError as err:
        output.error = err


def _git_output(process: subprocess.Popen, command: str) -> bytes:
    """Join under one deadline; every exit kills a live child and reaps it with a timeout."""
    output = GitOutput()
    worker = threading.Thread(target=_read_git, args=(process.stdout, output), daemon=True)
    deadline = time.monotonic() + DEADLINE_SECONDS
    worker.start()
    try:
        worker.join(timeout=DEADLINE_SECONDS)
        if worker.is_alive():
            raise subprocess.TimeoutExpired(command, DEADLINE_SECONDS)
        if output.error:
            raise output.error
        if output.exceeded:
            raise ciwf.ReadError(f"{command}: byte budget {MAX_GIT_BYTES} exceeded")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        return bytes(output.data)
    except subprocess.TimeoutExpired as err:
        raise ciwf.ReadError(
            f"{command}: deadline {DEADLINE_SECONDS}s exceeded (byte budget {MAX_GIT_BYTES})"
        ) from err
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=DEADLINE_SECONDS)
        worker.join(timeout=DEADLINE_SECONDS)


def git(root: Path, *args: str) -> str:
    """Bound Git stdout before decoding, and stderr before replacement-decoded diagnostics."""
    command = f"git {' '.join(args)}"
    try:
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                ["git", "-C", str(root), *args],
                stdout=subprocess.PIPE,
                stderr=errors,
                env=ciwf.git_env(),
            )
            data = _git_output(process, command)
            errors.seek(0)
            stderr = errors.read(MAX_GIT_BYTES + 1)
            if len(stderr) > MAX_GIT_BYTES:
                raise ciwf.ReadError(f"{command}: stderr byte budget {MAX_GIT_BYTES} exceeded")
            if process.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip() or process.returncode
                raise ciwf.ReadError(f"{command}: {message}")
            return data.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as err:
        raise ciwf.ReadError(f"{command}: {err} (byte budget {MAX_GIT_BYTES})") from err


def merge_base(root: Path, ref: str) -> str:
    """The commit HEAD and ref share; without one the check cannot tell what changed."""
    if ref in NULL_IDS:
        raise ciwf.ReadError(f"base {ref} is the null commit: no merge base to compare with")
    try:
        return git(root, "merge-base", ref, "HEAD").strip()
    except ciwf.ReadError as err:
        raise ciwf.ReadError(f"no merge base of {ref} and HEAD: {err}") from err


@dataclass
class Deletion:
    """New-side boundary and whether the removed run began and ended with text."""

    point: int
    first: bool
    last: bool


@dataclass
class Changes:
    """Added lines and contiguous removed runs from one bounded diff walk."""

    added: set[int] = field(default_factory=set)
    deletions: list[Deletion] = field(default_factory=list)


def git_lines(text: str, *, keepends: bool = False) -> list[str]:
    """Split only at LF, stripping CR only before LF unless endings must stay exact."""
    parts = text.split("\n")
    if keepends:
        lines = [line + "\n" for line in parts[:-1]]
    else:
        lines = [line.removesuffix("\r") for line in parts[:-1]]
    return lines + ([parts[-1]] if parts[-1] else [])


def diff_changes(diff: str) -> Changes:
    """Walk new-side coordinates once, including zero-length hunks and removed runs."""
    changes = Changes()
    number, deleting = None, False
    for line in limited(git_lines(diff), MAX_LINES * 10, "diff line limit"):
        hunk = HUNK.match(line)
        if hunk:
            number = int(hunk.group(1)) + (hunk.group(2) == "0")
        elif number is None or line.startswith("\\"):
            continue
        elif line.startswith("-"):
            nonblank = bool(line[1:].strip())
            if deleting:
                changes.deletions[-1].last = nonblank
            else:
                changes.deletions.append(Deletion(number - 1, nonblank, nonblank))
            deleting = True
            continue
        elif line.startswith("+"):
            changes.added.add(number)
            number += 1
        elif line.startswith(" "):
            number += 1
        deleting = False
    return changes


def added_lines(diff: str) -> set[int]:
    """Added line numbers from the shared diff walk, independent of path headers."""
    return diff_changes(diff).added


def _bounded_text(path: Path, *, source: bool = False) -> str:
    """Read regular UTF-8 files under a byte limit; only sources may hold no text."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            if source:
                return ""
            raise ciwf.ReadError(f"{path}: record is not a regular file")
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except FileNotFoundError as err:
        if source:
            return ""
        raise ciwf.ReadError(f"{path}: {err}") from err
    except OSError as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    kind = "source" if source else "record"
    if len(data) > MAX_SOURCE_BYTES:
        raise ciwf.ReadError(f"{path}: {kind} byte limit {MAX_SOURCE_BYTES} exceeded")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as err:
        if source:
            return ""
        raise ciwf.ReadError(f"{path}: {err}") from err


def limited(items: list[str], bound: int, label: str) -> list[str]:
    """Refuse a partial judgment instead of silently truncating the input."""
    if len(items) > bound:
        raise ciwf.ReadError(f"{label} {bound} exceeded")
    return items


def record_text(path: Path) -> str:
    """Read one regular record under byte and line limits, with contextual input errors."""
    text = _bounded_text(path)
    limited(git_lines(text), MAX_LINES, "record line limit")
    return text


def _deleted_patch(lines: list[str]) -> bool:
    """Only the header before this patch's first hunk can mark its new side absent."""
    for line in lines:
        if HUNK.match(line):
            break
        if line == "+++ /dev/null":
            return True
    return False


def _patches(lines: list[str]) -> list[list[str]]:
    """Split an already line-bounded diff at Git patch headers, never hunk body text."""
    patches: list[list[str]] = [[]]
    for line in lines:
        if line.startswith("diff --git "):
            patches.append([])
        patches[-1].append(line)
    return patches


def _record_changes(root: Path, name: str, diff: str) -> Changes | None:
    """Bind surviving patches; type-change deletion hunks never reach the diff walk."""
    lines = limited(git_lines(diff), MAX_LINES * 10, "diff line limit")
    if any(line.startswith(("Binary files ", "GIT binary patch")) for line in lines):
        raise ciwf.ReadError(f"{name}: binary record diff")
    if not os.path.lexists(root / name):
        return None
    try:
        if not stat.S_ISREG(os.lstat(root / name).st_mode):
            raise ciwf.ReadError(f"{name}: record is not a regular file")
    except OSError as err:
        raise ciwf.ReadError(f"{name}: {err}") from err
    changes = Changes()
    for patch in _patches(lines):
        if _deleted_patch(patch):
            continue
        surviving = diff_changes("\n".join(patch))
        changes.added.update(surviving.added)
        changes.deletions.extend(surviving.deletions)
    return changes if changes.added or changes.deletions else None


def _check_attributes(root: Path, names: list[str]) -> None:
    """Refuse conversions before diffing; check-attr -z emits path/attribute/value triples."""
    if not names:
        return
    output = git(root, "check-attr", "-z", *ATTRIBUTES, "--", *names).split("\0")
    expected = {(name, attribute) for name in names for attribute in ATTRIBUTES}
    if output[-1] or len(output) != len(expected) * 3 + 1:
        raise ciwf.ReadError("git check-attr: malformed attribute triples")
    for index in range(0, len(output) - 1, 3):
        name, attribute, value = output[index : index + 3]
        if (name, attribute) not in expected:
            raise ciwf.ReadError(f"git check-attr: unexpected {name} {attribute}")
        expected.remove((name, attribute))
        if value not in ("unspecified", "unset"):
            raise ciwf.ReadError(
                f"{name}: {attribute} attribute {value!r} changes record coordinates"
            )


def _record_names(output: str, label: str) -> list[str]:
    """Bound the full name list before retaining the record subset."""
    names = limited(output.rstrip("\0").split("\0"), MAX_FILES, label)
    return [name for name in names if RECORD.fullmatch(name)]


def changed_records(root: Path, base: str) -> dict[str, Changes]:
    """Added lines and deletion boundaries plus local new records absent from git diff."""
    names = _record_names(
        git(root, "diff", "-z", "--name-only", "--no-renames", base, "--", "docs/adr"),
        "changed file limit",
    )
    untracked = _record_names(
        git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "docs/adr"),
        "new file limit",
    )
    all_names = limited(list(dict.fromkeys([*names, *untracked])), MAX_FILES, "changed file limit")
    _check_attributes(root, all_names)
    changed: dict[str, Changes] = {}
    diff_lines = 0
    for name in names:
        diff = git(root, *DIFF, base, "--", f":(literal){name}")
        diff_lines += len(git_lines(diff))
        if diff_lines > MAX_LINES * 10:
            raise ciwf.ReadError(f"diff line limit {MAX_LINES * 10} exceeded")
        changes = _record_changes(root, name, diff)
        if changes is not None:
            changed[name] = changes
    for name in untracked:
        count = len(git_lines(record_text(root / name)))
        changed[name] = Changes(set(range(1, count + 1)))
    return changed
