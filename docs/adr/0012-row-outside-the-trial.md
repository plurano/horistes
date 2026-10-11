<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0012: A push whose commit does not hold a job's workflow file lies outside the trial

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0004](0004-same-verdict-before-a-switch.md) makes an absent job an error:
when one job of a pair ran and the other did not report, a check may have been
lost, and the trial ends with exit code 3. A trial that reads a long-lived
branch meets a second cause. On every push before the caller was added, the
old job ran alone, because the new workflow file was not yet in that commit.
The first trial of a consumer on its default branch ended with exit code 3 for
nine such pushes and no lost check. Leaving the branch out of the map would
hide them, and with them every later push on which a check is really lost.

## Decision

- A row where one job ran and the other is absent is `outside` when the commit
  does not hold the workflow file of the absent job. It counts for nothing: it
  is no equal row, no difference and no error. The check reports how many rows
  lie outside.
- An absent job whose workflow file the commit holds stays an error, as
  before.
- Whether the commit holds the file is read, never taken from a date or a
  commit named in the map. `tools/forge.py` asks the forge for the listing of
  `.github/workflows` at the commit and records the watched files it does not
  hold under `not_in_tree` of the subject, so a recorded trial is judged again
  without the forge.
- A file is recorded only on a push. The forge starts a run from the workflow
  files of the commit the event names, and for a push that is the commit the
  run names. A pull request runs the files of its merge commit, which a run
  does not name; an absent job there stays an error.
- The listing is evidence only when it came, holds fewer entries than the
  forge lists at most (1,000), and holds every watched workflow that did run
  on that push. Anything else is a forge error and ends the check with exit
  code 3. A failed request is never read as a missing file.
- A record that names a file as not in the tree while a check of that file is
  recorded on the same subject is an error.
- This adds to ADR-0004. Its other rules, the side conditions and the exit
  codes are unchanged.

## Consequences

- **Positive**: a trial can read the branch the switch is made on from its
  start, so a check lost on a later push still ends the trial; the pushes
  before the caller no longer end it.
- **Negative**: a branch on which the caller's file was removed again gives
  rows outside the trial, not errors. Three things still show it: the count
  of such rows in the report, the ten equal rows the pair needs, and the side
  conditions, which read the caller in the tree that is judged and fail when
  it is not there. One more request per push on which a watched workflow did
  not run.
- **Neutral**: the same holds for the old job after the switch: a push whose
  commit no longer holds the old workflow file is outside the trial.

## References

- GitHub Docs, "Workflows", section "Workflow triggers" (read 2026-10-11):
  a run uses the version of the workflow that is present in the commit or
  ref of the event. "Events that trigger workflows": for `push` that commit
  is the tip commit pushed; for `pull_request` it is the merge commit.
- GitHub REST reference, "Get repository content" (read 2026-10-11): a folder
  is answered as a list of at most 1,000 entries.
- `tests/test_forge.py`: the listing and the five answers that are refused;
  `tests/test_verdict.py`: cases O1 to O9.
