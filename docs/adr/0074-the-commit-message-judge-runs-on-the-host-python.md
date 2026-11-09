<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0074: The commit-message judge runs on the host Python

- **Status**: Accepted
- **Date**: 2026-11-09
- **Authors**: Lusoris

## Context

ADR-0072 introduced commit message judging via the `commit-msg` hook and `make check-message`,
running pytest on `tests/test_commit_messages.py` within `.venv`. It required `.venv` with a
valid `.stamp` matching `requirements/tools.txt` (`check-tools-stamp`).

In a fresh clone without `.venv`, `make check-message` failed on every commit with:
`$(VENV)/.stamp does not match requirements/tools.txt; run make tools`. Clean messages and
planted defects alike were blocked until `make tools` ran. Similarly, the squash helper's
clone of the default branch required a virtual environment merely to judge a proposed squash
message.

## Decision

- Commit message judging moves into a standalone standard-library-only module
  `tools/commit_message.py`, executed under `python3 -I -B`. It requires host Python
  (>= 3.12) and git alone, with zero third-party dependencies, no `.venv`, and no stamp.
  `tests/test_commit_message_tool.py` refuses an import outside `sys.stdlib_module_names` and
  runs the command line under `-I -S -B`; the CI floor job runs it on Python 3.12.
  An older `python3` gets exit status 2 and a message naming the 3.12 floor before any
  judging, so the hook never judges on an interpreter the floor job does not run.
- The gate's range test (`tests/test_commit_messages.py`) imports the same module, so the hook
  and the gate judge by one implementation.
- The `check-message` target in `Makefile` drops the `check-tools-stamp` prerequisite and
  runs `python3 -I -B tools/commit_message.py check "$$MESSAGE_FILE"`. It uses the literal
  word `python3` to prevent an inherited `PYTHON` environment variable from substituting
  another interpreter. The requirement for a non-empty `MESSAGE_FILE` remains.
- The `commit-msg` hook in `lefthook.yml` continues to run `make check-message` unchanged,
  passing the message file via a quoted here-document. A fresh clone without `.venv` can commit
  and judge messages immediately.
- The judge uses explicit exceptions (`JudgeError`) and finding lists rather than assertions,
  ensuring `-O` (PYTHONOPTIMIZE) cannot bypass refusals or strip error handling.
- `check-tools-stamp` remains in place for `pre-push`, `make check`, and CI workflows.
- This decision supersedes the virtual environment and stamp requirements of
  [ADR-0072](0072-the-commit-msg-hook-judges-each-message.md).

## Consequences

- **Positive**:
  - A fresh clone without `make tools` can commit and judge messages immediately.
  - The squash helper's default-branch checkout can judge messages without provisioning a `.venv`.
  - `python3 -I` isolates execution from inherited environment settings (`PYTHONPATH`,
    `PYTHONOPTIMIZE`).
- **Negative**:
  - The host system must have `python3` (>= 3.12) and `git` available on `PATH`.
