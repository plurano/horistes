<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0072: The commit-msg hook judges each message

- **Status**: Accepted
- **Date**: 2026-11-09
- **Authors**: Lusoris

## Context

`tests/test_commit_messages.py` refuses a commit message without a `Signed-off-by`
trailer, one whose sign-off names another author, and one that credits a tool or a
model: a trailer key other than `Signed-off-by` or `Migration`, a co-author line, or a
line that opens with "generated with". It reads the commits between the base of a change
and `HEAD`, so it judges a pull request's own commits before they land.

The message of a squash merge is different: the forge writes it when the pull request
lands, so the gate reads it only afterwards, on the run of `main`. Main's head 99a8785
landed with the forge's default squash message, which appends a `Co-authored-by` trailer,
and `test_commits_of_this_change_credit_no_tool` failed on the push run of `main`.

The engine's squash helper (`squash_merge.sh`) writes the subject and body of a squash
merge itself. Before it sends them, it can run the repository's own `commit-msg` hook on
that text: `lefthook run commit-msg <file>` in a checkout of the default branch. A
non-zero exit refuses the merge. A repository without such a hook gets a skip line. This
repository had no `commit-msg` job.

Lefthook 2.2.2 pastes the hook's arguments (`{1}`) into the shell line as they are, for
`run` and `scripts` jobs alike, and runs that line with `sh -c`
(`internal/run/controller/command/replacer/replacer.go`, `AddGitArgs`;
`internal/run/controller/exec/exec_unix.go`). Only file templates are quoted. A path
with a space splits into several words, and one holding a single quote ends quotes put
around `{1}`, so `$(...)` in such a path runs. A job's `env` values take no templates.

## Decision

- `tests/test_commit_messages.py` gains a one-message mode. With `MESSAGE_FILE` set,
  `test_message_file_passes_the_judge` reads that file and judges it with `judged()`,
  the same function the range test applies to each commit, as the commit git is about
  to make in the working directory's repository. Its author is git's author identity
  (`git var GIT_AUTHOR_IDENT`): `git commit` sets it for its hooks to the effective
  author, `--author` and `--amend` included. Its parents are `HEAD` and, while a merge
  is in progress, the heads in `MERGE_HEAD`; a clean merge without a sign-off is read
  against the tree git writes from the index it commits. The file is opened without
  blocking and must be a regular file, so a FIFO is refused at once, and at most
  `MESSAGE_BYTES` (four bytes for each of the 65,536 characters) are read. Without
  `MESSAGE_FILE` the test is skipped.
- `make check-message` runs only that test. It refuses to run without `MESSAGE_FILE`,
  since the test would then be skipped and pass, and it needs a `.venv` whose stamp
  matches `requirements/tools.txt` (`check-tools-stamp`), as pre-push does. It installs
  nothing. It clears `PYTEST_ADDOPTS`, since `--collect-only` there passes any message,
  and `PYTHONOPTIMIZE`: pytest rewrites the asserts of test modules and those run under
  `-O` too, but with rewriting turned off (`--assert=plain`) `-O` drops the assert that
  refuses the message.
- `lefthook.yml` adds the job `commit-msg: message-judge`, which sets `MESSAGE_FILE`
  from `{1}` read out of a here-document with a quoted delimiter
  (`$(cat <<'GIT_ARGUMENT'`) and runs `make --no-print-directory check-message`. The
  shell expands nothing in that here-document, so a space, quote, `$` or backquote stays
  part of the path; only a line equal to `GIT_ARGUMENT` or trailing newlines would
  change it, and the changed path then names no file.
- The file is judged as git hands it to the hook, before git strips comment lines or
  the diff of `git commit -v`. A line there that breaks a rule is refused too, and a
  diff long enough to pass the 65,536-character limit refuses the message. Cutting at
  the scissors line instead would let a message given with `-F` hide a trailer below a
  scissors line that git keeps.
- `tests/test_message_hook.py` runs the job in a planted repository with lefthook:
  through `lefthook run commit-msg <file>` and through `git commit`. A clean message
  passes and a planted `Co-authored-by: renovate[bot]` trailer is refused, as are an
  unquoted `{1}`, a missing file, no `MESSAGE_FILE`, a stale stamp, a sign-off of
  another author, an inherited `PYTEST_ADDOPTS=--collect-only`, and an inherited
  `PYTHONOPTIMIZE=2` in a tree whose `pyproject.toml` turns assertion rewriting off. A
  file named `a'$(touch planted)'b c.txt` is one path to the job and creates nothing,
  while the planted single-quoted `'{1}'` runs it. `git commit --author` and
  `--amend` are judged against the author git keeps; `git merge` of a clean merge
  without a sign-off lands, and a merge commit that adds its own content without one
  is refused.
- `tests/test_commit_messages.py` refuses a FIFO without waiting, a directory, and a
  file one byte over `MESSAGE_BYTES`, and reads no more than that bound of a larger
  file.
- `tests/test_dev_mode.py` finds the coverage step of `make test` by its two cleared
  settings and the assignment after them, since `check-message` clears the same two.

## Consequences

- **Positive**:
  - A squash message the helper writes is judged by the same rules before it lands,
    not only on the run of `main`.
  - A local commit with a co-author trailer or without a sign-off is refused when it is
    made, before pre-push.
- **Negative**:
  - Each commit starts a pytest run; the hook took about 0.2 seconds in local runs.
  - A checkout without `make tools` cannot commit until it runs it; the squash helper's
    default-branch checkout needs a `.venv` as well.
  - `git commit -v` with a long diff, or with a diff line that reads like a co-author
    trailer, is refused; commit without `-v` then.
- **Neutral**:
  - The range test on every gate run stays, so a message that passes the hook in another
    clone is still judged in CI.
  - Revisit when the pinned engine refuses these messages itself; the credit rules of
    `tests/test_commit_messages.py` then retire, and this hook with them.
