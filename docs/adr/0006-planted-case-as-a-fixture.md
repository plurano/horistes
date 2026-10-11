<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0006: A planted case may be a fixture in the tree

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0004](0004-same-verdict-before-a-switch.md) asks every switch for a
planted case both jobs must fail, and names it as a commit. For the licence
check that commit would hold a file without licence information. This
repository's own hook and gate refuse such a commit, and it would live only on
a trial branch that is deleted after the trial, so nobody could replay the
failure later.

## Decision

- The licence workflow takes an input `root` (default `.`), passed to the tool
  as its project root. At the default the tool is called exactly as before. A
  root that is no plain relative path stops the job before the tool runs.
- Two fixtures are committed like any other file:
  `tests/fixtures/unlicensed` holds one file without licence information;
  `tests/fixtures/licensed` holds the same file with it, and its own licence
  text. `REUSE.toml` names the one unlicensed file, so the check at the
  repository root passes over it. The entry names that file, never the fixture
  tree.
- The gate runs the tool on both fixtures on every run: the unlicensed one
  must be refused with that file named, the licensed one must pass, and a copy
  of the tree without the entry must fail on exactly that file
  (`tests/test_fixtures.py`).
- `tools/verdict.py` reads `[[fixture]]` tables: a pair of jobs that run the
  old and the new form on a fixture folder. Such a pair must reach the verdict
  it names on every subject, a failure with the reason marker on both sides.
  It needs one equal row with that verdict and none against it, and it counts
  as a planted case of its family. The ten rows and the ordinary pass stay a
  rule of the family's main pair.
- The jobs that run the fixtures exist only on the trial's branch. A job that
  fails by design on the main branch would turn its runs red for good.
- This adds to ADR-0004: a planted case is a commit or a fixture.

## Consequences

- **Positive**: the failing case is in the tree and replayed by the gate on
  every run; no commit has to get past the hook that guards the rule under
  test; a consumer can point the licence check at a subtree.
- **Negative**: the tree holds one file that carries no licence information
  itself; the job the workflow replaces takes no root, so on the fixtures the
  trial compares the old form of the job (the tool called directly, with the
  same root) with the new one, not the old file itself.
- **Neutral**: the workflow's interface grows by one optional input.

## References

- Fleet answer ledger, id shared-ci-planted-commit-path, 2026-10-11
  (paraphrased): the planted licence case is a fixture, not a commit that gets
  around the hook; the workflow takes a root input, the fixtures are committed
  normally, the entry in `REUSE.toml` names only the planted folder, and the
  failing case stays replayable on the main branch.
