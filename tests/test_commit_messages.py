# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Commit messages of a change: a sign-off, and no line that credits a tool or a model.

The engine's `forge check-message` reads the subject and the migration footer
and passes a message that names a tool as co-author. Until the engine refuses
such a message itself, this test reads every commit between the base of the
change and HEAD, including merge commits. The credit rules retire with that
engine release.

The sign-off is read here too, so it is checked on every run of the gate:
pull request, merge queue and push alike. A message without a `Signed-off-by`
trailer is refused. The sign-off must name the author's email; names may
differ because the forge writes the account name on squash commits. A bot
author needs a sign-off of the person who lands it. A merge commit the forge
writes for a bot (the merge train's update-branch) is exempt: it carries no
trailer and brings in only commits of the base.

Two credit rules, on one message each:

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
FORGE_BOT = re.compile(r"\d+\+[a-z0-9-]+\[bot\]@users\.noreply\.github\.com")
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
BOT = {
    **ALONE,
    "GIT_AUTHOR_NAME": "renovate[bot]",
    "GIT_AUTHOR_EMAIL": "29139614+renovate[bot]@users.noreply.github.com",
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


def trailers(message: str) -> list[tuple[str, str]]:
    """Trailers of one message as (key, value) pairs, as git reads them."""
    parsed = git(ROOT, "interpret-trailers", "--parse", text=message, env=ALONE)
    return [
        (line.split(":", 1)[0].strip(), line.split(":", 1)[1].strip())
        for line in parsed.splitlines()
        if ":" in line
    ]


def trailer_keys(message: str) -> list[str]:
    """Keys of the trailer block of one message, as git reads it."""
    return [key for key, _ in trailers(message)]


def sign_off_email(value: str) -> str:
    """Email between the last '<' and '>' of a trailer value."""
    start = value.rfind("<")
    end = value.rfind(">")
    if start != -1 and end != -1 and start < end:
        return value[start + 1 : end]
    return value


def foreign_sign_off(author: str, message: str) -> list[str]:
    """Findings when Signed-off-by trailers do not match the author email."""
    if FORGE_BOT.fullmatch(author):
        return []
    values = [v for k, v in trailers(message) if k.lower() == "signed-off-by"]
    emails = [sign_off_email(v) for v in values]
    if not emails:
        return []
    if any(email.lower() == author.lower() for email in emails):
        return []
    return [f"sign-off {', '.join(emails)} is not the author {author}"]


def credited(message: str) -> list[str]:
    """What one message holds that credits a tool: trailer keys, then footer lines."""
    keys = trailer_keys(message)
    lines = [line.strip() for line in message.splitlines() if FOOTER.match(line)]
    loose = [line for line in lines if line.split(":", 1)[0] not in keys]
    return [f"trailer {key}" for key in keys if key.lower() not in TRAILERS] + [
        f"line {line}" for line in loose
    ]


def unsigned(message: str) -> list[str]:
    """The finding of a message that has no `Signed-off-by` trailer."""
    keys = {key.lower() for key in trailer_keys(message)}
    return [] if "signed-off-by" in keys else ["no trailer Signed-off-by"]


def signed_off(author: str, parents: str, message: str) -> list[str]:
    """Sign-off findings of one commit; a merge the forge writes for a bot is exempt."""
    if len(parents.split()) > 1 and FORGE_BOT.fullmatch(author):
        return []
    return unsigned(message) + foreign_sign_off(author, message)


def refused(repo: Path, base: str) -> dict[str, list[str]]:
    """Commits between the base and HEAD, merges included, that break a sign-off or credit rule."""
    git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    listed = git(repo, "rev-list", f"--max-count={MAX_COMMITS + 1}", f"{base}..HEAD")
    commits = listed.split()
    assert len(commits) <= MAX_COMMITS, f"more than {MAX_COMMITS} commits since {base}"
    found: dict[str, list[str]] = {}
    for sha in commits:
        read = git(repo, "log", "-1", "--format=%ae%x00%P%x00%B", sha)
        author, parents, message = read.split("\x00", 2)
        lines = signed_off(author, parents, message) + credited(message)
        if lines:
            found[sha] = lines
    return found


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


@pytest.mark.parametrize(
    "message",
    [GOOD, GOOD.replace(SIGN_OFF, f"Migration: rename the input.\n{SIGN_OFF}")],
    ids=["sign-off alone", "sign-off after a footer"],
)
def test_message_with_sign_off_passes(message):
    assert unsigned(message) == []


@pytest.mark.parametrize(
    "message",
    [
        "feat(x): add a thing\n",
        GOOD.replace(SIGN_OFF, "Migration: rename the input."),
        f"feat(x): add a thing\n\n{SIGN_OFF}\n\nThe body comes after it.\n",
        GOOD.replace("Signed-off-by:", "Signed-off-by"),
    ],
    ids=["subject alone", "other trailer only", "sign-off above the body", "no colon"],
)
def test_planted_message_without_sign_off_is_refused(message):
    assert unsigned(message) == ["no trailer Signed-off-by"]


def test_planted_co_author_outside_the_trailer_block_is_refused():
    line = "Co-authored-by: Some Model <m@example.org>"
    assert credited(GOOD.replace("\n\nThe header", f"\n\n{line}\n\nThe header")) == [f"line {line}"]


def commit(repo: Path, message: str, env: dict | None = None) -> str:
    options = ["--quiet", "--allow-empty", "--no-gpg-sign", "--file=-"]
    git(repo, "commit", *options, text=message, env=env or ALONE)
    return git(repo, "rev-parse", "HEAD").strip()


def branches(repo: Path) -> str:
    """Planted repository: main and feature one commit apart each; returns the base."""
    git(repo, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(repo, GOOD)
    git(repo, "checkout", "--quiet", "-b", "feature", env=ALONE)
    commit(repo, GOOD.replace("add a thing", "feature work"))
    git(repo, "checkout", "--quiet", "main", env=ALONE)
    commit(repo, GOOD.replace("add a thing", "main work"))
    return base


def merge(repo: Path, message: str, env: dict | None = None) -> str:
    """Merge feature into main with this message; returns the merge commit."""
    options = ["--no-ff", "--no-edit", "--no-gpg-sign", "--quiet", "-m", message]
    git(repo, "merge", *options, "feature", env=env or ALONE)
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


def test_planted_commit_without_sign_off_is_refused(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    bad = commit(tmp_path, "fix(x): change a thing\n\nNo sign-off here.\n")
    commit(tmp_path, GOOD.replace("add a thing", "add another"))
    assert refused(tmp_path, base) == {bad: ["no trailer Signed-off-by"]}


def test_base_that_names_no_commit_fails(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    commit(tmp_path, GOOD)
    with pytest.raises(AssertionError, match="rev-parse"):
        refused(tmp_path, "0000000000000000000000000000000000000000")
    with pytest.raises(AssertionError, match="rev-parse"):
        refused(tmp_path, "origin/absent")


def test_foreign_sign_off_passes_when_email_matches_case_insensitively():
    """A sign-off with different name and uppercase email passes."""
    message = "feat(x): add\n\nSigned-off-by: Another Name <PERSON@example.org>\n"
    assert foreign_sign_off("person@example.org", message) == []


def test_foreign_sign_off_refuses_different_email():
    """A sign-off with a different email is refused naming both emails."""
    message = "feat(x): add\n\nSigned-off-by: A Person <other@example.org>\n"
    findings = foreign_sign_off("person@example.org", message)
    assert findings == ["sign-off other@example.org is not the author person@example.org"]


def test_foreign_sign_off_passes_when_one_of_multiple_sign_offs_matches():
    """Two sign-offs where one is foreign and one matches the author passes."""
    message = (
        "feat(x): add\n\n"
        "Signed-off-by: Foreign Person <other@example.org>\n"
        "Signed-off-by: A Person <person@example.org>\n"
    )
    assert foreign_sign_off("person@example.org", message) == []


def test_bot_author_forge_format_distinction():
    """A forge bot author passes with a person's sign-off; non-forge is refused."""
    bot = "29139614+renovate[bot]@users.noreply.github.com"
    message = f"chore(deps): update\n\n{SIGN_OFF}\n"
    assert foreign_sign_off(bot, message) == []
    non_forge = "renovate[bot]@example.org"
    assert foreign_sign_off(non_forge, message) == [
        f"sign-off person@example.org is not the author {non_forge}"
    ]


@pytest.mark.parametrize(
    "author",
    [
        "29139614+renovate[bot]@users.noreply.github.com.example.org",
        "other-29139614+renovate[bot]@users.noreply.github.com",
    ],
)
def test_bot_author_with_a_longer_address_is_not_the_forge_form(author):
    """Negative: the forge form must span the whole address, neither open nor close it."""
    message = f"chore(deps): update\n\n{SIGN_OFF}\n"
    assert foreign_sign_off(author, message) == [
        f"sign-off person@example.org is not the author {author}"
    ]


def test_planted_merge_commit_with_foreign_sign_off_is_refused(tmp_path):
    """A merge commit with a foreign sign-off is refused; with the author's sign-off it passes."""
    base = branches(tmp_path)
    foreign = merge(
        tmp_path, "Merge branch 'feature'\n\nSigned-off-by: Foreign <other@example.org>\n"
    )
    assert refused(tmp_path, base) == {
        foreign: ["sign-off other@example.org is not the author person@example.org"]
    }
    git(tmp_path, "reset", "--quiet", "--hard", "HEAD~1", env=ALONE)
    merge(tmp_path, f"Merge branch 'feature'\n\n{SIGN_OFF}\n")
    assert refused(tmp_path, base) == {}


def test_planted_merge_commit_without_sign_off_is_refused(tmp_path):
    """A merge commit without a sign-off trailer is refused by unsigned."""
    base = branches(tmp_path)
    merged = merge(tmp_path, "Merge branch 'feature' without sign-off\n")
    assert refused(tmp_path, base) == {merged: ["no trailer Signed-off-by"]}


def test_planted_forge_merge_for_a_bot_passes_and_its_plain_commit_does_not(tmp_path):
    """Boundary: a bot's merge without a sign-off is exempt; a bot's plain commit is not."""
    base = branches(tmp_path)
    merge(tmp_path, "Merge branch 'main' into feature\n", env=BOT)
    assert refused(tmp_path, base) == {}
    plain = commit(tmp_path, "chore(deps): update a pin\n", env=BOT)
    assert refused(tmp_path, base) == {plain: ["no trailer Signed-off-by"]}
