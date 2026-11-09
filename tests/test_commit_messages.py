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
differ because the forge writes the account name on squash commits. A merge
commit without a sign-off passes only when its tree is the clean merge of its
two parents (it adds no content of its own: the forge's test merge of a pull
request, an update-branch merge); a merge that carries a sign-off is checked
like any commit. A bot author passes with its own sign-off, as the update
bot's `:gitSignOff` preset writes it, or with a person's; every sign-off value
must read `Name <email>`. A forge bot address is recognised in any case, local
part and domain alike, as the author's email is compared.

Two credit rules, on one message each:

- Trailers are read by git (`git interpret-trailers --parse`), and their keys
  are held against `TRAILERS`. A key that is not listed is refused, so a new
  kind of credit needs no new rule here.
- A footer is no trailer, so two shapes of line are refused wherever they
  stand: a co-author line outside the trailer block, and a line that opens with
  "generated with".

The base is `BASE_REF` as the gate receives it, or `origin/main`. A base that
does not name a commit fails the test; it is never skipped.

The judge functions and constants live in `tools/commit_message.py` (ADR-0074),
which provides the standalone stdlib-only judge and CLI used by `make check-message`
and the commit-msg hook. This module runs the range test on gate runs and tests
the judge rules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import commit_message
import pytest
from commit_message import (
    ALONE,
    MAX_MESSAGE,
    MERGE_HEAD_BYTES,
    MESSAGE_BYTES,
    JudgeError,
    author_email,
    credited,
    foreign_sign_off,
    git,
    judged,
    message_text,
    oversize,
    pending,
    refused,
    run_git,
    unsigned,
)
from conftest import ROOT

SIGN_OFF = "Signed-off-by: A Person <person@example.org>"
GOOD = f"feat(x): add a thing\n\nThe header is generated with its own command.\n\n{SIGN_OFF}\n"
BOT = {
    **ALONE,
    "GIT_AUTHOR_NAME": "renovate[bot]",
    "GIT_AUTHOR_EMAIL": "29139614+renovate[bot]@users.noreply.github.com",
}


def test_commits_of_this_change_credit_no_tool():
    base = os.environ.get("BASE_REF", "origin/main")
    assert refused(ROOT, base) == {}


CO_AUTHOR = "Co-authored-by: renovate[bot] <29139614+renovate[bot]@users.noreply.github.com>"


def test_judged_clean_message_has_no_finding():
    """A clean commit message signed off by its author produces no findings."""
    assert judged(ROOT, "", "person@example.org", "", GOOD) == []


def test_judged_planted_bot_co_author_is_refused():
    """A commit message with a planted bot Co-authored-by trailer is refused."""
    planted = GOOD + CO_AUTHOR + "\n"
    assert judged(ROOT, "", "person@example.org", "", planted) == ["trailer Co-authored-by"]


def test_judged_message_of_another_author_is_refused():
    """A commit message signed off by a different author is refused."""
    found = judged(ROOT, "", "other@example.org", "", GOOD)
    assert found == ["sign-off person@example.org is not the author other@example.org"]


def test_judged_unsigned_message_is_refused():
    """A commit message without a Signed-off-by trailer is refused."""
    assert judged(ROOT, "", "person@example.org", "", "feat(x): add a thing\n") == [
        "no trailer Signed-off-by"
    ]


@pytest.mark.parametrize(
    ("length", "found"),
    [
        (MAX_MESSAGE, ["trailer Co-authored-by"]),
        (MAX_MESSAGE + 1, [f"message of {MAX_MESSAGE + 1} characters, over {MAX_MESSAGE}"]),
    ],
    ids=["exact", "one over"],
)
def test_judged_length_comes_first(length, found):
    """At the bound a message is judged on its trailers; one over gets the length finding alone."""
    head, tail = "feat(x): add a thing\n\n", f"\n\n{SIGN_OFF}\n{CO_AUTHOR}\n"
    message = head + "x" * (length - len(head) - len(tail)) + tail
    assert len(message) == length
    assert judged(ROOT, "", "person@example.org", "", message) == found


@pytest.mark.parametrize("email", ["person@example.org", "other@example.net"])
def test_author_email_reads_the_author_git_hands_the_hook(monkeypatch, email):
    """author_email returns the email git var names for the GIT_AUTHOR_EMAIL the hook gets."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "A Person")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", email)
    assert author_email(ROOT) == email


@pytest.mark.parametrize(
    ("size", "passes"),
    [(MESSAGE_BYTES, True), (MESSAGE_BYTES + 1, False)],
    ids=["at the bound", "one byte over"],
)
def test_message_text_reads_up_to_the_byte_bound(tmp_path, size, passes):
    """A message file up to the byte bound is read, while one byte over is refused."""
    path = tmp_path / "message.txt"
    path.write_bytes(b"x" * size)
    if passes:
        assert message_text(str(path)) == "x" * size
        return
    with pytest.raises(JudgeError, match=f"over {MESSAGE_BYTES} bytes"):
        message_text(str(path))


def test_message_text_holds_at_most_the_bound_of_a_large_file(tmp_path):
    """A 64-times oversized file is refused after reading one byte past the bound.

    The peak counts the bytes read and pytest's text of them for the failed assert,
    about four characters a byte: some five times the bound, against 64 times or more
    for a read of the whole file.
    """
    path = tmp_path / "large.txt"
    with path.open("wb") as handle:
        handle.truncate(64 * MESSAGE_BYTES)
    tracemalloc.start()
    try:
        with pytest.raises(JudgeError, match=f"over {MESSAGE_BYTES} bytes"):
            message_text(str(path))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 16 * MESSAGE_BYTES, peak


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this system")
def test_message_text_refuses_a_fifo_without_waiting(tmp_path):
    """A FIFO with no writer would block a plain open; the non-blocking open refuses it.

    Should the open block, a helper opens a writer after 5 seconds so the test
    ends, and the elapsed time fails it.
    """
    path = tmp_path / "message.fifo"
    os.mkfifo(path)
    done = threading.Event()

    def release() -> None:
        """Open the FIFO as a writer after five seconds if the read open blocked."""
        if done.wait(5):
            return
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # no reader: the open under test did not block
            return
        os.close(descriptor)

    helper = threading.Thread(target=release, daemon=True)
    helper.start()
    started = time.monotonic()
    try:
        with pytest.raises(JudgeError, match="not a regular file"):
            message_text(str(path))
    finally:
        done.set()
        helper.join(10)
    assert time.monotonic() - started < 2


@pytest.mark.skipif(sys.platform == "win32", reason="Windows refuses to open a directory")
def test_message_text_refuses_a_directory(tmp_path):
    """A directory passed to message_text is refused as not a regular file."""
    with pytest.raises(JudgeError, match="not a regular file"):
        message_text(str(tmp_path))


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


def commit_file(repo: Path, name: str, content: str, message: str, env: dict | None = None) -> str:
    """Write one file, stage it and commit it with this message; returns the commit."""
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", name, env=ALONE)
    return commit(repo, message, env)


def branches(repo: Path) -> str:
    """Planted repository: main and feature one commit apart, each adding a file; the base."""
    git(repo, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit_file(repo, "base.txt", "base\n", GOOD)
    git(repo, "checkout", "--quiet", "-b", "feature", env=ALONE)
    commit_file(repo, "feature.txt", "feature work\n", GOOD.replace("add a thing", "feature work"))
    git(repo, "checkout", "--quiet", "main", env=ALONE)
    commit_file(repo, "main.txt", "main work\n", GOOD.replace("add a thing", "main work"))
    return base


def merge(repo: Path, message: str, branch: str = "feature", env: dict | None = None) -> str:
    """Merge a branch with this message; returns the merge commit."""
    options = ["--no-ff", "--no-edit", "--no-gpg-sign", "--quiet", "-m", message]
    git(repo, "merge", *options, branch, env=env or ALONE)
    return git(repo, "rev-parse", "HEAD").strip()


def merge_with_own_content(
    repo: Path, message: str, branch: str = "feature", env: dict | None = None
) -> str:
    """Merge a branch and add a file of the merge's own; returns the merge commit."""
    options = ["--no-ff", "--no-commit", "--no-gpg-sign", "--quiet"]
    git(repo, "merge", *options, branch, env=env or ALONE)
    return commit_file(repo, "own.txt", "own content\n", message, env)


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
    with pytest.raises(JudgeError, match="rev-parse"):
        refused(tmp_path, "0000000000000000000000000000000000000000")
    with pytest.raises(JudgeError, match="rev-parse"):
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


@pytest.mark.parametrize(
    "other_cased",
    [
        "49699333+DEPENDABOT[BOT]@USERS.NOREPLY.GITHUB.COM",
        "49699333+dependabot[bot]@USERS.NOREPLY.GITHUB.COM",
        "49699333+Dependabot[BOT]@users.noreply.github.com",
    ],
    ids=["all-upper", "upper-domain", "mixed-local"],
)
def test_foreign_sign_off_refuses_other_forge_bot_in_various_casings(other_cased):
    """Negative: a bot commit signed off by another forge bot in various casings is refused."""
    bot = BOT["GIT_AUTHOR_EMAIL"]
    message = f"chore(deps): update\n\nSigned-off-by: dependabot[bot] <{other_cased}>\n"
    assert foreign_sign_off(bot, message) == [f"sign-off {other_cased} is not the author {bot}"]


@pytest.mark.parametrize(
    "bot_author",
    [
        "29139614+RENOVATE[BOT]@USERS.NOREPLY.GITHUB.COM",
        "29139614+renovate[bot]@USERS.NOREPLY.GITHUB.COM",
        "29139614+Renovate[BOT]@users.noreply.github.com",
    ],
    ids=["all-upper", "upper-domain", "mixed-local"],
)
def test_forge_bot_author_in_various_casings_passes_with_person_sign_off(bot_author):
    """Positive: a forge bot author in various casings passes with a person's sign-off."""
    message = f"chore(deps): update\n\n{SIGN_OFF}\n"
    assert foreign_sign_off(bot_author, message) == []


def test_bot_author_with_uppercase_longer_address_is_refused():
    """Boundary: an upper-case address that runs past the forge form is no bot address."""
    author = "29139614+RENOVATE[BOT]@USERS.NOREPLY.GITHUB.COM.example.org"
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


def test_planted_forge_test_merge_passes(tmp_path):
    """A clean merge representing a forge test merge without a trailer passes."""
    base = branches(tmp_path)
    feature_sha = git(tmp_path, "rev-parse", "feature", env=ALONE).strip()
    main_sha = git(tmp_path, "rev-parse", "main", env=ALONE).strip()
    merge(tmp_path, f"Merge {feature_sha} into {main_sha}\n", env=ALONE)
    assert refused(tmp_path, base) == {}


def test_planted_update_branch_merge_for_a_bot_passes(tmp_path):
    """A bot update-branch merge (clean merge of main into feature) without a trailer passes."""
    base = branches(tmp_path)
    git(tmp_path, "checkout", "--quiet", "feature", env=ALONE)
    merge(tmp_path, "Merge branch 'main' into feature\n", branch="main", env=BOT)
    assert refused(tmp_path, base) == {}


def test_planted_merge_commit_with_own_content_is_refused(tmp_path):
    """A merge commit adding content without a sign-off trailer is refused."""
    base = branches(tmp_path)
    merged = merge_with_own_content(
        tmp_path, "Merge branch 'feature' with own content\n", env=ALONE
    )
    assert refused(tmp_path, base) == {merged: ["no trailer Signed-off-by"]}


def test_planted_spoofed_bot_merge_with_own_content_is_refused(tmp_path):
    """A merge commit adding content under a bot author without a sign-off is refused."""
    base = branches(tmp_path)
    merged = merge_with_own_content(tmp_path, "Merge branch 'feature' with own content\n", env=BOT)
    assert refused(tmp_path, base) == {merged: ["no trailer Signed-off-by"]}


def test_conflict_merge_resolved_without_sign_off_is_refused(tmp_path):
    """A merge whose conflict is resolved by hand, without a sign-off, is refused."""
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit_file(tmp_path, "file.txt", "base line\n", GOOD)
    git(tmp_path, "checkout", "--quiet", "-b", "feature", env=ALONE)
    commit_file(tmp_path, "file.txt", "feature edit\n", GOOD.replace("add a thing", "feature work"))
    git(tmp_path, "checkout", "--quiet", "main", env=ALONE)
    commit_file(tmp_path, "file.txt", "main edit\n", GOOD.replace("add a thing", "main work"))
    conflict = run_git(
        tmp_path, "merge", "--no-ff", "--no-gpg-sign", "--quiet", "feature", env=ALONE
    )
    assert conflict.returncode == 1, conflict.stdout + conflict.stderr
    merged = commit_file(tmp_path, "file.txt", "resolved line\n", "Merge branch 'feature'\n")
    assert len(git(tmp_path, "log", "-1", "--format=%P", merged).split()) == 2
    assert refused(tmp_path, base) == {merged: ["no trailer Signed-off-by"]}


def test_bot_plain_commit_sign_off_requirements(tmp_path):
    """A bot commit passes with its own or a person's sign-off; none or another bot's fails."""
    bot = BOT["GIT_AUTHOR_EMAIL"]
    other = "49699333+dependabot[bot]@users.noreply.github.com"
    no_trailer_msg = "chore(deps): update\n"
    assert unsigned(no_trailer_msg) + foreign_sign_off(bot, no_trailer_msg) == [
        "no trailer Signed-off-by"
    ]
    own_msg = f"chore(deps): update\n\nSigned-off-by: renovate[bot] <{bot}>\n"
    assert unsigned(own_msg) + foreign_sign_off(bot, own_msg) == []
    other_msg = f"chore(deps): update\n\nSigned-off-by: dependabot[bot] <{other}>\n"
    assert foreign_sign_off(bot, other_msg) == [f"sign-off {other} is not the author {bot}"]
    person_signed_msg = f"chore(deps): update\n\n{SIGN_OFF}\n"
    assert unsigned(person_signed_msg) + foreign_sign_off(bot, person_signed_msg) == []

    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    bad_plain = commit(tmp_path, no_trailer_msg, env=BOT)
    assert refused(tmp_path, base) == {bad_plain: ["no trailer Signed-off-by"]}
    bad_other = commit(tmp_path, other_msg, env=BOT)
    not_author = f"sign-off {other} is not the author {bot}"
    assert refused(tmp_path, bad_plain) == {bad_other: [not_author]}
    commit(tmp_path, own_msg, env=BOT)
    commit(tmp_path, person_signed_msg, env=BOT)
    assert refused(tmp_path, bad_other) == {}


@pytest.mark.parametrize(
    ("trailer", "finding"),
    [
        (
            "Signed-off-by: A Person <person@example.org> junk",
            "malformed sign-off 'A Person <person@example.org> junk'",
        ),
        (
            "Signed-off-by: <person@example.org>",
            "malformed sign-off '<person@example.org>'",
        ),
        (
            "Signed-off-by: A Person person@example.org",
            "malformed sign-off 'A Person person@example.org'",
        ),
    ],
    ids=["trailing junk", "missing name", "missing angle brackets"],
)
def test_malformed_sign_off_is_refused(trailer, finding):
    """Negative: a sign-off that is not `Name <email>` is refused, even with the author's email."""
    message = f"feat(x): add\n\n{trailer}\n"
    assert foreign_sign_off("person@example.org", message) == [finding]


def test_message_length_boundary():
    """Boundary: MAX_MESSAGE characters pass; one more is refused before any parsing."""
    exact = "a" * MAX_MESSAGE
    assert oversize(exact) == []
    over = "a" * (MAX_MESSAGE + 1)
    assert oversize(over) == [f"message of {MAX_MESSAGE + 1} characters, over {MAX_MESSAGE}"]


def plant_message(repo: Path, length: int) -> str:
    """Commit a message git stores as exactly length characters, read back; the commit."""
    header = "feat(x): add\n\n"
    trailer = f"\n{SIGN_OFF}\n"
    message = header + "x" * (length - len(header) - len(trailer)) + trailer
    sha = commit(repo, message)
    stored = git(repo, "cat-file", "commit", sha).split("\n\n", 1)[1]
    assert (len(message), stored) == (length, message)
    return sha


@pytest.mark.parametrize("length", [MAX_MESSAGE - 1, MAX_MESSAGE], ids=["one under", "exact"])
def test_planted_message_up_to_the_limit_passes(tmp_path, length):
    """Boundary: a commit whose stored message is MAX_MESSAGE characters or fewer passes."""
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    plant_message(tmp_path, length)
    assert refused(tmp_path, base) == {}


def test_planted_oversize_message_is_refused(tmp_path):
    """Boundary: a commit whose stored message is one character over MAX_MESSAGE is refused."""
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    bad = plant_message(tmp_path, MAX_MESSAGE + 1)
    assert refused(tmp_path, base) == {
        bad: [f"message of {MAX_MESSAGE + 1} characters, over {MAX_MESSAGE}"]
    }


def test_boundary_octopus_merge_without_trailer_is_refused(tmp_path):
    """Boundary: a clean three-parent merge without a trailer is refused; exemption needs two."""
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit_file(tmp_path, "base.txt", "base\n", GOOD)
    for name in ("branch1", "branch2"):
        git(tmp_path, "checkout", "--quiet", "-b", name, "main", env=ALONE)
        commit_file(tmp_path, f"{name}.txt", f"{name}\n", GOOD.replace("add a thing", name))
    git(tmp_path, "checkout", "--quiet", "main", env=ALONE)
    commit_file(tmp_path, "main.txt", "main\n", GOOD.replace("add a thing", "main work"))
    options = [
        "--no-ff",
        "--no-gpg-sign",
        "--quiet",
        "-m",
        "Merge branches 'branch1' and 'branch2'\n",
    ]
    git(tmp_path, "merge", *options, "branch1", "branch2", env=ALONE)
    merged = git(tmp_path, "rev-parse", "HEAD").strip()
    assert len(git(tmp_path, "log", "-1", "--format=%P", merged).split()) == 3
    assert refused(tmp_path, base) == {merged: ["no trailer Signed-off-by"]}


@pytest.mark.parametrize(("extra", "passes"), [(0, True), (1, False)], ids=["at-bound", "over"])
def test_pending_reads_merge_head_up_to_the_byte_bound(tmp_path, extra, passes):
    """Boundary: a MERGE_HEAD of MERGE_HEAD_BYTES bytes is read; one byte more is refused."""
    branches(tmp_path)
    git(tmp_path, "merge", "--no-ff", "--no-commit", "--quiet", "feature", env=ALONE)
    path = Path(tmp_path, git(tmp_path, "rev-parse", "--git-path", "MERGE_HEAD").strip())
    head = path.read_text(encoding="ascii")
    path.write_text(head + " " * (MERGE_HEAD_BYTES + extra - len(head)), encoding="ascii")
    if passes:
        parents, tree = pending(tmp_path)
        assert parents.split()[1] == head.strip()
        assert tree
        return
    with pytest.raises(JudgeError, match=f"over {MERGE_HEAD_BYTES} bytes"):
        pending(tmp_path)


def test_pending_refuses_an_index_that_moves_while_it_reads(tmp_path, monkeypatch):
    """A merge whose index changes between the two reads of pending() is refused, not judged."""
    branches(tmp_path)
    git(tmp_path, "merge", "--no-ff", "--no-commit", "--quiet", "feature", env=ALONE)
    real = commit_message.git
    moved: list[str] = []

    def racing_git(repo, *arguments, **options):
        out = real(repo, *arguments, **options)
        if arguments[:1] == ("write-tree",) and not moved:
            moved.append(out)
            Path(repo, "late.txt").write_text("late\n", encoding="utf-8")
            real(repo, "add", "late.txt", env=ALONE)
        return out

    monkeypatch.setattr(commit_message, "git", racing_git)
    with pytest.raises(JudgeError, match="changed while the judge read them"):
        pending(tmp_path)
    assert moved


def test_pending_takes_a_merge_that_holds_still(tmp_path):
    """A merge in progress that nothing changes gives HEAD, the merged head and its tree."""
    branches(tmp_path)
    git(tmp_path, "merge", "--no-ff", "--no-commit", "--quiet", "feature", env=ALONE)
    parents, tree = pending(tmp_path)
    assert parents.split() == [
        git(tmp_path, "rev-parse", "HEAD").strip(),
        git(tmp_path, "rev-parse", "feature").strip(),
    ]
    assert tree == git(tmp_path, "write-tree").strip()


def test_range_with_a_message_not_in_utf8_is_a_judge_error(tmp_path):
    """A commit message git emits in bytes that are not UTF-8 is a JudgeError, not a traceback."""
    git(tmp_path, "init", "--quiet", "--initial-branch=main", env=ALONE)
    base = commit(tmp_path, GOOD)
    tree = git(tmp_path, "write-tree").strip()
    person = "A Person <person@example.org> 1700000000 +0000"
    header = f"tree {tree}\nparent {base}\nauthor {person}\ncommitter {person}\n\n"
    # git commit-tree rewrites bytes that are not UTF-8; hash-object stores them as given.
    raw = header.encode("ascii") + f"fix: caf\xe9 bytes\n\n{SIGN_OFF}\n".encode("latin-1")
    # nosemgrep: dangerous-subprocess-use-audit expires 2027-03-09 category D: runs git hash-object
    made = subprocess.run(
        ["git", "-C", str(tmp_path), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=raw,
        capture_output=True,
        timeout=20,
        check=True,
        env={**os.environ, **ALONE},
    )
    git(tmp_path, "update-ref", "HEAD", made.stdout.decode("ascii").strip())
    with pytest.raises(JudgeError, match="output is not UTF-8"):
        refused(tmp_path, base)


def test_a_read_that_stalls_is_refused_at_the_deadline(tmp_path, monkeypatch):
    """A file read that does not return within DEADLINE seconds is refused; the judge goes on."""
    release = threading.Event()

    def stalled(name, bound):
        release.wait(30)
        return b""

    monkeypatch.setattr(commit_message, "bounded_bytes", stalled)
    monkeypatch.setattr(commit_message, "DEADLINE", 0.2)
    started = time.monotonic()
    try:
        with pytest.raises(JudgeError, match=r"not read within 0\.2 seconds"):
            message_text(tmp_path / "message.txt")
    finally:
        release.set()
    assert time.monotonic() - started < 5


def test_a_read_error_in_the_worker_reaches_the_caller(tmp_path, monkeypatch):
    """An OSError the reading thread meets reaches the caller; the CLI turns it into rc 2."""

    def broken(name, bound):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(commit_message, "bounded_bytes", broken)
    with pytest.raises(OSError, match="Input/output error"):
        message_text(tmp_path / "message.txt")
