# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Judge commit messages against sign-off and tool-credit rules (ADR-0074).

This module uses Python standard library modules only and runs under
`python3 -I -B` so that a fresh checkout without `.venv` or installed tools
can judge commit messages for hooks and workflows.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

DEADLINE = 20  # seconds for one git call
MAX_COMMITS = 500
MAX_MESSAGE = 65536
MESSAGE_BYTES = 4 * MAX_MESSAGE  # UTF-8 takes at most 4 bytes per character
MERGE_HEAD_BYTES = 65 * MAX_COMMITS  # one SHA-256 name and a newline per head
TRAILERS = {"signed-off-by", "migration"}
FOOTER = re.compile(r"^\W*(generated with\b|co-authored-by\s*:)", re.IGNORECASE)
FORGE_BOT = re.compile(r"\d+\+[a-z0-9-]+\[bot\]@users\.noreply\.github\.com", re.IGNORECASE)
SIGN_OFF_VALUE = re.compile(r"[^<>\n]*[^<>\s] <([^<>\s@]+@[^<>\s@]+)>")
ALONE = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "A Person",
    "GIT_AUTHOR_EMAIL": "person@example.org",
    "GIT_COMMITTER_NAME": "A Person",
    "GIT_COMMITTER_EMAIL": "person@example.org",
}


class JudgeError(Exception):
    """Refusal or failure in commit message judging."""


def run_git(
    repo: Path, *arguments: str, text: str | None = None, env: dict | None = None
) -> subprocess.CompletedProcess[str]:
    """One git call in repo; the caller judges its exit status."""
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=text,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )


def git(repo: Path, *arguments: str, text: str | None = None, env: dict | None = None) -> str:
    """Run one git command in repo and return its output or raise JudgeError."""
    done = run_git(repo, *arguments, text=text, env=env)
    if done.returncode != 0:
        raise JudgeError(f"git {' '.join(arguments)}: {done.stderr.strip()}")
    return done.stdout


def merged_tree(repo: Path, ours: str, theirs: str) -> str | None:
    """Tree of the clean merge of ours and theirs, or None when they conflict."""
    done = run_git(repo, "merge-tree", "--write-tree", ours, theirs, env=ALONE)
    if done.returncode == 0 and done.stdout:
        return done.stdout.splitlines()[0].strip()
    return None


def trailers(message: str, repo: Path | None = None) -> list[tuple[str, str]]:
    """Trailers of one message as (key, value) pairs, as git reads them in repo.

    Without a repo git reads them in the working directory.
    """
    target = Path.cwd() if repo is None else repo
    parsed = git(target, "interpret-trailers", "--parse", text=message, env=ALONE)
    return [
        (line.split(":", 1)[0].strip(), line.split(":", 1)[1].strip())
        for line in parsed.splitlines()
        if ":" in line
    ]


def trailer_keys(message: str, repo: Path | None = None) -> list[str]:
    """Keys of the trailer block of one message, as git reads it."""
    return [key for key, _ in trailers(message, repo=repo)]


def foreign_sign_off(author: str, message: str, repo: Path | None = None) -> list[str]:
    """Findings on malformed sign-offs, and when none names the author (a bot's: or a person)."""
    values = [v for k, v in trailers(message, repo=repo) if k.lower() == "signed-off-by"]
    if not values:
        return []
    findings: list[str] = []
    emails: list[str] = []
    for value in values:
        match = SIGN_OFF_VALUE.fullmatch(value)
        if match:
            emails.append(match.group(1))
        else:
            findings.append(f"malformed sign-off {value!r}")
    if not emails:
        return findings
    if any(email.lower() == author.lower() for email in emails):
        return findings
    if FORGE_BOT.fullmatch(author) and any(not FORGE_BOT.fullmatch(e) for e in emails):
        return findings
    findings.append(f"sign-off {', '.join(emails)} is not the author {author}")
    return findings


def credited(message: str, repo: Path | None = None) -> list[str]:
    """What one message holds that credits a tool: trailer keys, then footer lines."""
    keys = trailer_keys(message, repo=repo)
    lines = [line.strip() for line in message.splitlines() if FOOTER.match(line)]
    loose = [line for line in lines if line.split(":", 1)[0] not in keys]
    return [f"trailer {key}" for key in keys if key.lower() not in TRAILERS] + [
        f"line {line}" for line in loose
    ]


def unsigned(message: str, repo: Path | None = None) -> list[str]:
    """The finding of a message that has no `Signed-off-by` trailer."""
    keys = {key.lower() for key in trailer_keys(message, repo=repo)}
    return [] if "signed-off-by" in keys else ["no trailer Signed-off-by"]


def oversize(message: str) -> list[str]:
    """Finding when a message is longer than MAX_MESSAGE characters."""
    if len(message) > MAX_MESSAGE:
        return [f"message of {len(message)} characters, over {MAX_MESSAGE}"]
    return []


def signed_off(repo: Path, tree: str, author: str, parents: str, message: str) -> list[str]:
    """Sign-off findings of one commit; a clean 2-parent merge without a trailer is exempt.

    `tree` names the commit's tree: the commit itself, or the tree written from the
    index git is about to commit. Only a 2-parent message without a sign-off reads it.
    """
    parent_list = parents.split()
    if len(parent_list) == 2 and unsigned(message, repo=repo):
        p1, p2 = parent_list
        merged = merged_tree(repo, p1, p2)
        target = git(repo, "rev-parse", "--verify", f"{tree}^{{tree}}", env=ALONE).strip()
        if merged is not None and merged == target:
            return []
    return unsigned(message, repo=repo) + foreign_sign_off(author, message, repo=repo)


def judged(repo: Path, tree: str, author: str, parents: str, message: str) -> list[str]:
    """Findings on one message: its length first, then sign-off and credit."""
    return oversize(message) or (
        signed_off(repo, tree, author, parents, message) + credited(message, repo=repo)
    )


def author_email(repo: Path) -> str:
    """The email of git's author identity, as git hands it to a commit hook."""
    ident = git(repo, "var", "GIT_AUTHOR_IDENT")
    match = re.search(r"<([^<>\s]+)>", ident)
    if not match:
        raise JudgeError(f"git var GIT_AUTHOR_IDENT names no email: {ident.strip()!r}")
    return match.group(1)


def pending(repo: Path) -> tuple[str, str]:
    """Parents and tree of the commit git is about to make in repo.

    The parents are `HEAD` and, while a merge is in progress, each head in
    `MERGE_HEAD`. The tree is written from the index git commits, which git names
    to its hooks in `GIT_INDEX_FILE`; it is read only for a merge.
    """
    head = run_git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", env=ALONE)
    if head.returncode != 0:
        return "", ""
    merging = run_git(repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", env=ALONE)
    if merging.returncode != 0:
        return head.stdout.strip(), ""
    merge_path = git(repo, "rev-parse", "--git-path", "MERGE_HEAD", env=ALONE).strip()
    heads = bounded_text(Path(repo, merge_path), MERGE_HEAD_BYTES, "ascii").split()
    tree = git(repo, "write-tree", env=ALONE).strip()
    return " ".join([head.stdout.strip(), *heads]), tree


def bounded_text(path: str | Path, bound: int, encoding: str) -> str:
    """A regular file as text, refused when it holds more than `bound` bytes.

    It is opened without blocking, so a FIFO with no writer is refused at once
    instead of holding the commit, and at most `bound` + 1 bytes are read.
    """
    name = str(path)
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as err:
        raise JudgeError(f"{name}: {err.strerror}") from err
    try:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError as err:
        os.close(descriptor)
        raise JudgeError(f"{name}: {err.strerror}") from err
    if not regular:
        os.close(descriptor)
        raise JudgeError(f"{name}: not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(bound + 1)
    if len(data) > bound:
        raise JudgeError(f"{name}: over {bound} bytes")
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as err:
        raise JudgeError(f"{name}: not {encoding}: {err}") from err


def message_text(path: str | Path) -> str:
    """The message file as text: a regular UTF-8 file of at most MESSAGE_BYTES bytes."""
    return bounded_text(path, MESSAGE_BYTES, "utf-8")


def refused(repo: Path, base: str) -> dict[str, list[str]]:
    """Commits between the base and HEAD, merges included, that break a sign-off or credit rule."""
    git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    listed = git(repo, "rev-list", f"--max-count={MAX_COMMITS + 1}", f"{base}..HEAD")
    commits = listed.split()
    if len(commits) > MAX_COMMITS:
        raise JudgeError(f"more than {MAX_COMMITS} commits since {base}")
    found: dict[str, list[str]] = {}
    for sha in commits:
        read = git(repo, "log", "-1", "--format=format:%ae%x00%P%x00%B", sha)
        author, parents, message = read.split("\x00", 2)
        lines = judged(repo, sha, author, parents, message)
        if lines:
            found[sha] = lines
    return found


def _message_findings(message_file: str) -> list[str]:
    """Findings on one message file, judged as the commit git is about to make here."""
    top = run_git(Path.cwd(), "rev-parse", "--show-toplevel", env=ALONE)
    if top.returncode != 0:
        raise JudgeError(f"{Path.cwd()}: not a git repository")
    repo = Path(top.stdout.strip())
    message = message_text(message_file)
    parents, tree = pending(repo)
    return judged(repo, tree, author_email(repo), parents, message)


def _check_message(message_file: str) -> int:
    """Judge one commit message file: rc 0 clean, 1 findings, 2 when it cannot judge."""
    try:
        findings = _message_findings(message_file)
    except JudgeError as err:
        sys.stderr.write(f"{err}\n")
        return 2
    except (OSError, subprocess.TimeoutExpired) as err:
        sys.stderr.write(f"{message_file}: the judge could not run: {err}\n")
        return 2
    if findings:
        for finding in findings:
            sys.stderr.write(f"{finding}\n")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for commit message judging."""
    parser = argparse.ArgumentParser(
        description="Judge commit messages by sign-off and tool-credit rules (ADR-0074)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check", help="Judge a single message file")
    check_parser.add_argument("message_file", help="Path to commit message file")

    args = parser.parse_args(argv)
    if args.command == "check":
        return _check_message(args.message_file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
