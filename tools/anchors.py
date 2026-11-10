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
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import ciwf

ANCHOR = re.compile(r"blob/main/([^\s#)]+)#L(\d+)(?:-L(\d+))?")


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
    for end in range(start + len(pin.last) - 1, len(lines) + 1):
        if _block_at(lines, pin.last, end - len(pin.last)):
            return start, end
    return f"pin {pin.path} last text {pin.last[-1]!r} matches no place at or after line {start}"


def anchor_text(path: str, start: int, end: int) -> str:
    """Format a single-line or inclusive multi-line source anchor."""
    return f"{path}#L{start}" if start == end else f"{path}#L{start}-L{end}"


def _ranges(root: Path, pins: tuple[Pin, ...]) -> dict[Pin, tuple[int, int] | str]:
    """Derive pins for files present in this tree."""
    return {
        pin: derive(pin, (root / pin.path).read_text(encoding="utf-8").splitlines())
        for pin in pins
        if (root / pin.path).exists()
    }


def _derived_anchors(ranges: dict[Pin, tuple[int, int] | str]) -> set[str]:
    """Collect only ranges whose source blocks identify a valid anchor."""
    return {
        anchor_text(pin.path, *span) for pin, span in ranges.items() if not isinstance(span, str)
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


def findings(answers: dict[str, str], root: Path, pins: tuple[Pin, ...] = PINS) -> list[str]:
    """Find stale, unknown and missing anchors for each pin's declared answers."""
    ranges = _ranges(root, pins)
    found = [span for span in ranges.values() if isinstance(span, str)]
    valid = {
        (key, anchor_text(pin.path, *span))
        for pin, span in ranges.items()
        if not isinstance(span, str)
        for key in pin.keys
    }
    for key, text in answers.items():
        for match in ANCHOR.finditer(text):
            anchor = match.group(0).removeprefix("blob/main/")
            if not (root / match.group(1)).exists():
                found.append(f"{key}: anchor {anchor} links missing {match.group(1)}")
            elif (key, anchor) not in valid:
                found.append(
                    f"{key}: anchor {anchor} matches no pin for this answer; "
                    "run tools/anchors.py fix"
                )
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
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env=ciwf.git_env(),
    )


def _maps_to(pin: Pin, lines: list[str], start: int, end: int) -> bool:
    """Require both blocks exactly at the historical range's boundaries."""
    return (
        1 <= start <= end <= len(lines)
        and max(len(pin.first), len(pin.last)) <= end - start + 1
        and _block_at(lines, pin.first, start - 1)
        and _block_at(lines, pin.last, end - len(pin.last))
    )


def _moves(
    answers: dict[str, str], root: Path, base: str, pins: tuple[Pin, ...]
) -> tuple[dict[str, str], list[str]]:
    """Resolve every old anchor against the base before allowing any write."""
    references = _references(answers)
    problems = [
        f"anchor {old} cannot be mapped: {path} is missing"
        for old, (path, _, _) in references.items()
        if not (root / path).exists()
    ]
    if problems or not findings(answers, root, pins):
        return {}, problems
    if _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        raise OSError(f"base revision {base} not found")
    ranges = _ranges(root, pins)
    valid = _derived_anchors(ranges)
    moves = {}
    for old, (path, start, end) in references.items():
        done = _git(root, "show", f"{base}:{path}")
        lines = done.stdout.splitlines() if done.returncode == 0 else []
        matches = [pin for pin in pins if pin.path == path and _maps_to(pin, lines, start, end)]
        if not matches and old in valid:
            continue
        if len(matches) != 1:
            problems.append(f"anchor {old} cannot be mapped: matches {len(matches)} pins at {base}")
            continue
        span = ranges[matches[0]]
        if isinstance(span, str):
            problems.append(f"anchor {old} cannot be mapped: {span}")
            continue
        new = anchor_text(path, *span)
        if new != old:
            moves[old] = new
    return moves, problems


def run(command: str, root: Path, base: str, pins: tuple[Pin, ...] = PINS) -> int:
    """Check answers or apply a complete set of unambiguous anchor moves."""
    path = root / ".bestpractices.json"
    raw = path.read_bytes().decode("utf-8")
    answers = json.loads(raw)
    if command == "check":
        problems = findings(answers, root, pins)
        for problem in problems:
            complain(f"FAIL: {problem}")
        if problems:
            return 1
        say(f"PASS: {len(_references(answers))} anchors match their pins")
        return 0
    moves, problems = _moves(answers, root, base, pins)
    for problem in problems:
        complain(f"FAIL: {problem}")
    if problems:
        return 1
    if not moves:
        say("PASS: no anchor to move")
        return 0
    alternatives = "|".join(re.escape(old) for old in moves)
    pattern = re.compile(r"blob/main/(" + alternatives + r")(?![\d-])")
    updated = pattern.sub(lambda match: "blob/main/" + moves[match.group(1)], raw)
    path.write_bytes(updated.encode("utf-8"))
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
