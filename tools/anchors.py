#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Check or move best-practices line anchors using pinned source text.

    anchors.py [check|fix] [--root DIR] [--base REV]

Exit codes: 0 pass or nothing left to move, 1 drift or unmappable anchor,
3 file or git error. Fix maps old ranges against --base (default HEAD).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import ciwf

ANCHOR_BOUNDARY = r"(?=$|\s|\)|[.,](?:\s|$))"
ANCHOR = re.compile(r"blob/main/([^\s#)]+)#L(\d+)(?:-L(\d+))?" + ANCHOR_BOUNDARY)
ANCHOR_FRAGMENT = re.compile(r"blob/main/[^\s#)]+#L\d[^\s)]*")
JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')


@dataclass(frozen=True)
class Pin:
    """Consecutive source substrings delimiting one evidence range."""

    path: str
    first: tuple[str, ...]
    last: tuple[str, ...]
    keys: tuple[str, ...]


_CRYPTO_KEYS = (
    "crypto_published_justification",
    "crypto_call_justification",
    "crypto_floss_justification",
    "crypto_working_justification",
)

PINS: tuple[Pin, ...] = (
    Pin(
        "tools/merge_train.py",
        ("def app_jwt(",),
        ('return f"{signing_input}.{b64url(done.stdout)}"',),
        (*_CRYPTO_KEYS, "crypto_weaknesses_justification", "crypto_random_justification"),
    ),
    Pin(
        "tools/merge_train.py",
        ("if hashlib.sha256(content).hexdigest() != sha:",),
        ("if hashlib.sha256(content).hexdigest() != sha:",),
        _CRYPTO_KEYS,
    ),
    Pin(
        "tools/merge_train.py",
        ("blob_sha = hashlib.sha1(",),
        ("blob_sha = hashlib.sha1(",),
        ("crypto_weaknesses_justification",),
    ),
    Pin(
        "tools/merge_train.py",
        ("def key_bits(",),
        ("return int(match.group(1)) if match else None",),
        ("crypto_call_justification",),
    ),
    Pin(
        "tools/merge_train.py",
        ("def key_bits(",),
        ('f"the app key has {bits} bits; at least {MIN_KEY_BITS} are required"',),
        ("crypto_keylength_justification",),
    ),
    Pin(
        "tests/test_merge_train.py",
        ("@pytest.mark.parametrize(", '"bits",', "[", 'pytest.param(1024, id="1024-bits"),'),
        (
            "done, _ = train(tmp_path, forge, app_key=padded)",
            "assert done.returncode == 0, done.stdout + done.stderr",
            'assert forge.writes == [("merge", "PR_1", f"{1:040x}", "SQUASH")]',
        ),
        ("crypto_keylength_justification",),
    ),
    Pin(
        "tools/results.py",
        ("found = hashlib.sha256(data).hexdigest()",),
        ('raise ResultError(f"archive is sha256:{found[:12]}, the forge lists {digest[:19]}")',),
        _CRYPTO_KEYS,
    ),
    Pin(
        "tools/hook_copy.py",
        ("def _sha256(path: Path) -> str:",),
        ('return hashlib.file_digest(source, "sha256").hexdigest()',),
        _CRYPTO_KEYS,
    ),
    Pin(
        "tools/stranger.py",
        ("digest, size = hashlib.sha256(), 0",),
        ("digest, size = hashlib.sha256(), 0",),
        _CRYPTO_KEYS,
    ),
)


def say(line: str) -> None:
    sys.stdout.write(line + "\n")


def complain(line: str) -> None:
    sys.stderr.write(line + "\n")


def _block_at(lines: list[str], block: tuple[str, ...], offset: int) -> bool:
    """Match a complete block at a zero-based offset, without wrapping at EOF."""
    return 0 <= offset <= len(lines) - len(block) and all(
        text in lines[offset + index] for index, text in enumerate(block)
    )


def derive(pin: Pin, lines: list[str]) -> tuple[int, int] | str:
    """Derive the unique start and earliest complete ending block after it."""
    if not pin.first or not pin.last:
        raise ValueError(f"pin {pin.path} needs nonempty first and last blocks")
    starts = [index + 1 for index in range(len(lines)) if _block_at(lines, pin.first, index)]
    if len(starts) != 1:
        return f"pin {pin.path} first text {pin.first[0]!r} matches {len(starts)} places"
    start = starts[0]
    for end in range(start + max(len(pin.first), len(pin.last)) - 1, len(lines) + 1):
        if _block_at(lines, pin.last, end - len(pin.last)):
            return start, end
    return f"pin {pin.path} last text {pin.last[-1]!r} matches no place at or after line {start}"


def anchor_text(path: str, start: int, end: int) -> str:
    """Format a single-line or inclusive multi-line source anchor."""
    return f"{path}#L{start}" if start == end else f"{path}#L{start}-L{end}"


def _source_text(path: Path) -> str | None:
    """Read UTF-8 source once, distinguishing absence from other read failures."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as err:
        raise OSError(f"cannot read source {path}: {err}") from err


def _snapshot(root: Path, pins: tuple[Pin, ...]) -> dict[str, str | None]:
    """Capture each distinct pinned source, including missing files."""
    return {path: _source_text(root / path) for path in dict.fromkeys(pin.path for pin in pins)}


def _ranges(
    root: Path, pins: tuple[Pin, ...], snapshot: dict[str, str | None] | None = None
) -> dict[Pin, tuple[int, int] | str]:
    """Derive every pin from one snapshot, reporting absent source files."""
    if snapshot is None:
        snapshot = _snapshot(root, pins)
    return {
        pin: (
            f"pin {pin.path} source file is missing"
            if snapshot[pin.path] is None
            else derive(pin, snapshot[pin.path].splitlines())
        )
        for pin in pins
    }


def _derived_anchors(ranges: dict[Pin, tuple[int, int] | str]) -> set[tuple[str, str]]:
    """Collect valid anchors together with the answer keys they support."""
    return {
        (key, anchor_text(pin.path, *span))
        for pin, span in ranges.items()
        if not isinstance(span, str)
        for key in pin.keys
    }


def _pin_findings(
    answers: dict[str, str], pins: tuple[Pin, ...], ranges: dict[Pin, tuple[int, int] | str]
) -> list[str]:
    """Require every declared answer to link its pin's complete derived anchor."""
    found = []
    for pin in pins:
        if not pin.keys:
            found.append(f"pin {pin.path} lists no answer")
        span = ranges.get(pin)
        if span is None or isinstance(span, str):
            continue
        anchor = anchor_text(pin.path, *span)
        for key in pin.keys:
            linked = {match.group(0) for match in ANCHOR.finditer(answers.get(key, ""))}
            if f"blob/main/{anchor}" not in linked:
                found.append(f"pin {anchor} is not linked by {key}")
    return found


def _source_exists(root: Path, path: str, snapshot: dict[str, str | None]) -> bool:
    """Use the pinned snapshot for presence checks, or stat an unpinned link."""
    return snapshot[path] is not None if path in snapshot else (root / path).exists()


def _answer_findings(
    key: str, text: str, root: Path, valid: set[tuple[str, str]], snapshot: dict[str, str | None]
) -> list[str]:
    """Check one answer's complete anchor tokens and malformed fragments."""
    matches = list(ANCHOR.finditer(text))
    starts = {match.start() for match in matches}
    found = [
        f"{key}: malformed anchor {match.group(0)}"
        for match in ANCHOR_FRAGMENT.finditer(text)
        if match.start() not in starts
    ]
    for match in matches:
        anchor = match.group(0).removeprefix("blob/main/")
        if not _source_exists(root, match.group(1), snapshot):
            found.append(f"{key}: anchor {anchor} links missing {match.group(1)}")
        elif (key, anchor) not in valid:
            found.append(
                f"{key}: anchor {anchor} matches no pin for this answer; run tools/anchors.py fix"
            )
    return found


def findings(
    answers: dict[str, str],
    root: Path,
    pins: tuple[Pin, ...] = PINS,
    *,
    snapshot: dict[str, str | None] | None = None,
) -> list[str]:
    """Find stale, unknown and missing anchors for each pin's declared answers."""
    if snapshot is None:
        snapshot = _snapshot(root, pins)
    ranges = _ranges(root, pins, snapshot)
    found = [span for span in ranges.values() if isinstance(span, str)]
    valid = _derived_anchors(ranges)
    for key, text in answers.items():
        found += _answer_findings(key, text, root, valid, snapshot)
    return found + _pin_findings(answers, pins, ranges)


def _references(answers: dict[str, str]) -> dict[str, tuple[str, int, int]]:
    """Collect each distinct literal anchor and its inclusive coordinates."""
    return {
        match.group(0).removeprefix("blob/main/"): (
            match.group(1),
            int(match.group(2)),
            int(match.group(3) or match.group(2)),
        )
        for text in answers.values()
        for match in ANCHOR.finditer(text)
    }


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded git read with the shared git environment."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
            env=ciwf.git_env(),
        )
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as err:
        raise OSError(f"git {' '.join(args)} in {root} failed: {err}") from err


def _maps_to(pin: Pin, lines: list[str], start: int, end: int) -> bool:
    """Require both blocks exactly at the historical range's boundaries."""
    return (
        1 <= start <= end <= len(lines)
        and max(len(pin.first), len(pin.last)) <= end - start + 1
        and _block_at(lines, pin.first, start - 1)
        and _block_at(lines, pin.last, end - len(pin.last))
    )


def _base_oid(root: Path, base: str) -> str:
    """Resolve the user's base once so subsequent reads use an immutable commit."""
    done = _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if done.returncode != 0:
        raise OSError(f"base revision {base} not found: {done.stderr.strip()}")
    return done.stdout.strip()


def _historical_lines(root: Path, oid: str, path: str, base: str) -> list[str]:
    """Read a present historical file, treating only an empty tree listing as absence."""
    listed = _git(root, "ls-tree", "--name-only", oid, "--", path)
    if listed.returncode != 0:
        raise OSError(f"cannot list {path} at {base}: {listed.stderr.strip()}")
    if not listed.stdout:
        return []
    shown = _git(root, "show", f"{oid}:{path}")
    if shown.returncode != 0:
        raise OSError(f"cannot read {path} at {base}: {shown.stderr.strip()}")
    return shown.stdout.splitlines()


def _mapped_reference(
    key: str,
    old: str,
    coordinates: tuple[str, int, int],
    lines: list[str],
    ranges: dict[Pin, tuple[int, int] | str],
    pins: tuple[Pin, ...],
    base: str,
) -> tuple[str | None, list[str]]:
    """Map one answer's anchor only through pins assigned to that answer."""
    path, start, end = coordinates
    matches = [
        pin
        for pin in pins
        if key in pin.keys and pin.path == path and _maps_to(pin, lines, start, end)
    ]
    if not matches and (key, old) in _derived_anchors(ranges):
        return old, []
    if len(matches) != 1:
        return None, [
            f"anchor {old} for {key} cannot be mapped: matches {len(matches)} pins at {base}"
        ]
    span = ranges[matches[0]]
    if isinstance(span, str):
        return None, [f"anchor {old} cannot be mapped: {span}"]
    return anchor_text(path, *span), []


def _collect_moves(
    answers: dict[str, str],
    pins: tuple[Pin, ...],
    ranges: dict[Pin, tuple[int, int] | str],
    history: dict[str, list[str]],
    base: str,
) -> tuple[dict[str, str], list[str]]:
    """Combine per-answer mappings only when each literal has one destination."""
    mapped, problems = {}, []
    references = {
        (key, old): coordinates
        for key, text in answers.items()
        for old, coordinates in _references({key: text}).items()
    }
    for (key, old), coordinates in references.items():
        new, errors = _mapped_reference(
            key, old, coordinates, history[coordinates[0]], ranges, pins, base
        )
        problems += errors
        if new is None:
            continue
        if old in mapped and mapped[old] != new:
            problems.append(f"anchor {old} maps to both {mapped[old]} and {new}")
        mapped[old] = new
    return {old: new for old, new in mapped.items() if old != new}, problems


def _replace_anchors(text: str, moves: dict[str, str]) -> str:
    """Replace complete anchor tokens once, preserving longer anchors and avoiding cascades."""
    return ANCHOR.sub(
        lambda match: (
            "blob/main/"
            + moves.get(
                match.group(0).removeprefix("blob/main/"), match.group(0).removeprefix("blob/main/")
            )
        ),
        text,
    )


def _moved_answers(answers: dict[str, str], moves: dict[str, str]) -> dict[str, str]:
    """Apply simultaneous literal moves to each decoded answer."""
    return {key: _replace_anchors(text, moves) for key, text in answers.items()}


def _moves(
    answers: dict[str, str],
    root: Path,
    base: str,
    pins: tuple[Pin, ...],
    snapshot: dict[str, str | None] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Resolve moves against one base and reject any findings left after applying them."""
    if snapshot is None:
        snapshot = _snapshot(root, pins)
    references = _references(answers)
    problems = [
        f"anchor {old} cannot be mapped: {path} is missing"
        for old, (path, _, _) in references.items()
        if not _source_exists(root, path, snapshot)
    ]
    if problems or not findings(answers, root, pins, snapshot=snapshot):
        return {}, problems
    moves = {}
    if references:
        oid = _base_oid(root, base)
        paths = dict.fromkeys(path for path, _, _ in references.values())
        history = {path: _historical_lines(root, oid, path, base) for path in paths}
        moves, problems = _collect_moves(
            answers, pins, _ranges(root, pins, snapshot), history, base
        )
    problems += [
        f"after moves: {finding}"
        for finding in findings(_moved_answers(answers, moves), root, pins, snapshot=snapshot)
    ]
    return moves, problems


def _updated_raw(raw: str, answers: dict[str, str], moves: dict[str, str]) -> str | None:
    """Preserve JSON spelling and reject moves hidden behind escaped raw anchors."""
    updated = JSON_STRING.sub(
        lambda match: '"' + _replace_anchors(match.group(0)[1:-1], moves) + '"', raw
    )
    if json.loads(updated) != _moved_answers(answers, moves):
        return None
    return updated


def _changed_inputs(path: Path, raw: bytes, snapshot: dict[str, str | None]) -> list[str]:
    """Best-effort recheck: report answers or pinned sources changed since capture.

    This narrows, but does not close, the window for concurrent edits. Between
    recheck and replacement, edits to .bestpractices.json are overwritten; edits
    to pinned sources stay on disk but may leave written anchors pointing at
    moved lines, so tests/test_bestpractices.py fails until make fix-anchors runs again.
    """
    problems = []
    if path.read_bytes() != raw:
        problems.append(".bestpractices.json changed while fixing")
    problems += [
        f"{source} changed while fixing"
        for source, text in snapshot.items()
        if _source_text(path.parent / source) != text
    ]
    return problems


def _write_atomic(
    path: Path, raw: bytes, updated: bytes, snapshot: dict[str, str | None]
) -> list[str]:
    """Flush a same-directory temporary file and recheck inputs before replacement.

    The recheck is best effort. Between recheck and replacement, edits to
    .bestpractices.json are overwritten; edits to pinned sources stay on disk but
    may leave written anchors pointing at moved lines, so tests/test_bestpractices.py
    fails until make fix-anchors runs again.
    """
    temporary = None
    failure: BaseException | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        problems = _changed_inputs(path, raw, snapshot)
        if problems:
            return problems
        os.replace(temporary, path)
        return []
    except OSError as err:
        failure = err
        raise OSError(f"cannot atomically update {path}: {err}") from err
    except BaseException as err:
        failure = err
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as err:
                message = f"cannot remove temporary file {temporary} for {path}: {err}"
                if failure is not None:
                    complain(f"ERROR: {message}")
                else:
                    raise OSError(message) from err


def _report(problems: list[str]) -> bool:
    """Report every refusal and indicate whether any problem blocks the operation."""
    for problem in problems:
        complain(f"FAIL: {problem}")
    return bool(problems)


def run(command: str, root: Path, base: str, pins: tuple[Pin, ...] = PINS) -> int:
    """Check answers or apply a complete set of unambiguous anchor moves."""
    path = root / ".bestpractices.json"
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    answers = json.loads(text)
    if not isinstance(answers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in answers.items()
    ):
        raise ValueError(".bestpractices.json must map answer names to text")
    snapshot = _snapshot(root, pins)
    if command == "check":
        if _report(findings(answers, root, pins, snapshot=snapshot)):
            return 1
        say(f"PASS: {len(_references(answers))} anchors match their pins")
        return 0
    moves, problems = _moves(answers, root, base, pins, snapshot)
    if _report(problems):
        return 1
    if not moves:
        say("PASS: no anchor to move")
        return 0
    updated = _updated_raw(text, answers, moves)
    if updated is None:
        _report(["anchor moves do not apply to the raw file (escaped spelling?)"])
        return 1
    if _report(_write_atomic(path, raw, updated.encode("utf-8"), snapshot)):
        return 1
    for old, new in moves.items():
        say(f"moved {old} -> {new}")
    return 0


def main(argv: list[str] | None = None, *, pins: tuple[Pin, ...] = PINS) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", nargs="?", default="check", choices=("check", "fix"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--base", default="HEAD")
    args = parser.parse_args(argv)
    try:
        return run(args.command, args.root, args.base, pins)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
        ValueError,
        ciwf.ReadError,
    ) as err:
        complain(f"ERROR: {err}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
