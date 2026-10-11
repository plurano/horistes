# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Commit messages of a change: no line that credits a tool or a model.

The engine's `forge check-message` reads the subject and the migration footer
and passes a message that names a tool as co-author. Until the engine refuses
such a message itself, this test reads every commit between the base of the
change and HEAD. It retires with that engine release.

Two rules, on one message each:

- Trailers are read by git (`git interpret-trailers --parse`), and their keys
  are held against `TRAILERS`. A key that is not listed is refused, so a new
  kind of credit needs no new rule here.
- A footer is no trailer, so two shapes of line are refused wherever they
  stand: a co-author line outside the trailer block, and a line that opens with
  "generated with".

The base is `BASE_REF` as the gate receives it, or `origin/main`. A base that
does not name a commit fails the test; it is never skipped. The message of a
squash merge is written on the forge and is read here only after it landed, on
the run of the default branch.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT

DEADLINE = 20  # seconds for one git call
MAX_COMMITS = 500
TRAILERS = {"signed-off-by", "migration"}
FOOTER = re.compile(r"^\W*(generated with\b|co-authored-by\s*:)", re.IGNORECASE)
SIGN_OFF = "Signed-off-by: A Person <person@example.org>"
GOOD = f"feat(x): add a thing\n\nThe header is generated with its own command.\n\n{SIGN_OFF}\n"
# Outside this repository git reads no configuration of the machine it runs on.
ALONE = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "A Person",
    "GIT_AUTHOR_EMAIL": "person@example.org",
    "GIT_COMMITTER_NAME": "A Person",
    "GIT_COMMITTER_EMAIL": "person@example.org",
}


def git(repo: Path, *arguments: str, text: str | None = None, env: dict | None = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        input=text,
        capture_output=True,
        text=True,
        timeout=DEADLINE,
        check=False,
        env=None if env is None else {**os.environ, **env},
    )
    assert done.returncode == 0, f"git {' '.join(arguments)}: {done.stderr.strip()}"
    return done.stdout


def credited(message: str) -> list[str]:
    """What one message holds that credits a tool: trailer keys, then footer lines."""
    parsed = git(ROOT, "interpret-trailers", "--parse", text=message, env=ALONE)
    keys = [line.split(":", 1)[0].strip() for line in parsed.splitlines() if ":" in line]
    lines = [line.strip() for line in message.splitlines() if FOOTER.match(line)]
    loose = [line for line in lines if line.split(":", 1)[0] not in keys]
    return [f"trailer {key}" for key in keys if key.lower() not in TRAILERS] + [
        f"line {line}" for line in loose
    ]


def refused(repo: Path, base: str) -> dict[str, list[str]]:
    """Commits between the base and HEAD whose message credits a tool."""
    git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    listed = git(repo, "rev-list", "--no-merges", f"--max-count={MAX_COMMITS + 1}", f"{base}..HEAD")
    commits = listed.split()
    assert len(commits) <= MAX_COMMITS, f"more than {MAX_COMMITS} commits since {base}"
    found = {sha: credited(git(repo, "log", "-1", "--format=%B", sha)) for sha in commits}
    return {sha: lines for sha, lines in found.items() if lines}


def test_commits_of_this_change_credit_no_tool():
    base = os.environ.get("BASE_REF", "origin/main")
    assert refused(ROOT, base) == {}


@pytest.mark.parametrize(
    "message",
    [
        GOOD,
        GOOD.replace(SIGN_OFF, f"Migration: rename the input.\n{SIGN_OFF}"),
        GOOD.replace("feat(x): add", "feat(x)!: add"),
        "feat(x): add a thing\n",
    ],
    ids=["sign-off", "migration footer", "breaking subject", "subject alone"],
)
def test_message_without_credit_passes(message):
    assert credited(message) == []


@pytest.mark.parametrize(
    ("planted", "found"),
    [
        (f"Co-Authored-By: Some Model <model@example.org>\n{SIGN_OFF}", ["trailer Co-Authored-By"]),
        (f"co-authored-by: Some Model <model@example.org>\n{SIGN_OFF}", ["trailer co-authored-by"]),
        (f"Assisted-by: Some Tool\n{SIGN_OFF}", ["trailer Assisted-by"]),
        (f"Generated with Some Tool\n\n{SIGN_OFF}", ["line Generated with Some Tool"]),
        (
            f"\N{ROBOT FACE} Generated with [Some Tool](https://example.org)\n\n{SIGN_OFF}",
            ["line \N{ROBOT FACE} Generated with [Some Tool](https://example.org)"],
        ),
    ],
    ids=["co-author", "co-author in small letters", "unlisted key", "footer", "footer with a sign"],
)
def test_planted_credit_is_refused(planted, found):
    assert credited(GOOD.replace(SIGN_OFF, planted)) == found


def test_planted_co_author_outside_the_trailer_block_is_refused():
    line = "Co-authored-by: Some Model <m@example.org>"
    assert credited(GOOD.replace("\n\nThe header", f"\n\n{line}\n\nThe header")) == [f"line {line}"]


def commit(repo: Path, message: str) -> str:
    options = ["--quiet", "--allow-empty", "--no-gpg-sign", "--file=-"]
    git(repo, "commit", *options, text=message, env=ALONE)
    return git(repo, "rev-parse", "HEAD").strip()


def test_planted_commit_is_refused_and_the_commits_around_it_pass(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    commit(tmp_path, GOOD.replace("add a thing", "add another"))
    bad = commit(
        tmp_path, GOOD.replace(SIGN_OFF, f"Co-Authored-By: A Model <m@example.org>\n{SIGN_OFF}")
    )
    commit(tmp_path, GOOD.replace("add a thing", "add a third"))
    assert refused(tmp_path, base) == {bad: ["trailer Co-Authored-By"]}
    assert refused(tmp_path, "HEAD") == {}


def test_base_that_names_no_commit_fails(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    commit(tmp_path, GOOD)
    with pytest.raises(AssertionError, match="rev-parse"):
        refused(tmp_path, "0000000000000000000000000000000000000000")
    with pytest.raises(AssertionError, match="rev-parse"):
        refused(tmp_path, "origin/absent")
