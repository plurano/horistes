<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0016: Action pins are listed, written and checked by zizmor behind three guards, and `with` keys are read here

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0002](0002-versioning-and-pinning.md) has every outside action named by
a commit with its release in a comment. Three things can be wrong with such a
line, and nothing in the gate reads two of them:

- **The use is on a tag or a branch.** zizmor's audit `unpinned-uses` reports
  it offline, and `make lint-workflows` runs it.
- **The comment names another release than the commit.** A reviewer reads the
  comment, the runner reads the commit. Only a request to the action's
  repository can tell, and the gate asks no host.
- **A `with:` key the action does not declare.** The runner ignores it with a
  warning, so a misspelled key leaves the default in force. actionlint 1.7.12
  and zizmor 1.30.1 with every audit on both accept `sign-commit` on
  `peter-evans/create-pull-request` at v7.0.8, where the input is
  `sign-commits` (measured 2026-10-11, exit 0 each). actionlint reports an
  unknown key of an action inside the repository; on an outside action named
  by a commit it reported none, `actions/checkout` included.

The family needed one tool for this that every repository can run. Updates
are not its job: Renovate proposes them and holds the waiting time (see
References).

zizmor 1.30.1 already does the first two and writes the fix. It was measured
on 2026-10-11 on the workflows of 24 repositories of the family (243 files,
1433 uses), with `git ls-remote` of every action's repository as the
reference:

| Measured | Result |
| :--- | :--- |
| uses on a tag or a branch | 100 of 100 reported; `--fix` wrote the commit the reference names for all 100, with the full release as the comment (`@v7` becomes `@3d3c42e... # v7.0.1`) |
| commits under a comment | 36 reported with the persona `pedantic`; none of the 1220 lines whose comment the reference confirms was reported |
| comments `--fix` corrects | 27 of the 36, the commit kept; 9 stay findings: 4 name a release the repository does not have, 4 name a branch that has moved on, 1 has words after the release |
| other lines changed | 0 |

It also has three ways to pass without having checked, each measured:

- With no token, with `ZIZMOR_OFFLINE=true` or with
  `ZIZMOR_NO_ONLINE_AUDITS=true` in the environment it leaves the comment
  audit out and exits 0 on a tree with a wrong comment. Without a token it
  prints a warning; with either variable it prints none.
- It has no option that runs only the named audits. The others are switched
  off by name in a configuration file, and a name it does not know is
  ignored. `--fix=all` applies the fixes of every audit that ran: without the
  configuration it rewrote lines that have nothing to do with pins.
- A comment that holds more than the release (`# v4.2.2 kept by policy`) is
  not compared at all by the default persona, so a wrong release followed by
  words passes there. Only `pedantic` reports it.

## Decision

- `tools/pins.py` is the tool, with three commands. `list` prints every
  outside use on a tag or a branch and asks no host. `check` fails on such a
  use, on a commit whose comment is another release, no release or missing,
  and on an undeclared `with:` key. `pin` lets zizmor rewrite what it can fix
  and then checks. Exit code 0 is a pass, 1 a finding, 3 a check that could
  not be made. It is one implementation for the family: a repository runs it
  from a checkout of this one with this repository's tools (`make check-pins
  PINS_ROOT=<checkout>`), never with a zizmor of its own. On 2026-10-11 one
  other repository of the family had zizmor in its tree at all.
- zizmor does the reading of pins and the rewriting. The tool runs it with a
  configuration that switches off the 39 other audits of 1.30.1, with the
  persona `pedantic` and with `--no-ignores`, so a `zizmor: ignore` comment
  hides nothing. The tool has no exception list: a finding is fixed.
- Three guards turn zizmor's silent passes into exit code 3:
  1. **A planted workflow in every run.** It holds one use on a tag and one
     commit under a wrong release. A run that does not report both did not
     check, whatever the reason: no network audit, a changed audit name, a
     changed output.
  2. **A finding of any other audit ends the run.** A newer zizmor with an
     audit the configuration does not name is refused before `--fix` is
     started, so `pin` cannot apply a fix nobody asked for.
  3. **An exit code outside 0 and 11 to 14 ends the run.** Those are the codes
     zizmor documents for "no finding" and "findings".
- A token is required for `check` and `pin`: `GH_TOKEN`, `GITHUB_TOKEN` or
  `ZIZMOR_GITHUB_TOKEN`, else the one the forge's client holds. Without one
  the tool stops before zizmor starts.
- The `with:` keys are read by the tool itself. For a step it fetches
  `action.yml`, else `action.yaml`, of the action at the written ref; for a
  job that calls a workflow, the called file and its `on.workflow_call.inputs`.
  Requests go through `tools/forge.py`. Names are compared without case, as
  the runner does. An action that runs an image also takes `args` and
  `entrypoint`. Metadata the forge does not hold is a finding; a request that
  fails is exit code 3.
- The check is not part of `make verify`. The gate asks no host, and this
  check asks the forge and every action's repository.

## Consequences

- **Positive**:
  - A wrong comment, a use on a tag and a misspelled input are each one
    `FAIL` line with file and line, from one command, for any repository.
  - A run that did not check cannot print `PASS`.
  - No resolver of our own: tags, annotated tags and branches are resolved by
    zizmor over the git protocol, which cost no request of the forge's API
    budget in the measurement.
- **Negative**:
  - Nothing runs the check on its own. It is run by hand or by a session
    until a decision puts it into CI.
  - A commit that no release names is not judged. zizmor reports nothing for
    it whatever the comment says (measured on the head of a `main` branch
    under four comments); a pin on such a commit is a decision the reviewer
    has to see.
  - The comment has to be the release and nothing else, `# v4.2.2` or
    `# tag=v4.2.2`. A second remark on the same line fails.
  - A use inside the repository (`./`) and an image (`docker://`) are not
    read. A required input that is missing is not reported either.
  - An action in a private repository needs a token that reads it; without
    one its metadata is reported as not readable and the check fails.
  - `check` on the 24 repositories cost about 475 requests of the forge's
    budget, one or two for each distinct use.
  - The tests cannot ask a host. What the tool does with zizmor's answers is
    tested with a stand-in that answers as 1.30.1 was measured to; the real
    zizmor runs in the tests only where it needs no network. After an update
    of zizmor the planted workflow is what proves, on each run, that both
    audits still report.
  - A workflow with a YAML merge key (`<<`) is refused with exit code 3:
    `tools/ciwf.py` does not read it.
- **Neutral**: `make lint-workflows` keeps its own offline run of zizmor with
  every audit; this tool adds no audit to the gate and removes none.

## References

- Fleet answer ledger, 2026-10-11 (paraphrased). shared-ci-pin-helper-vs-renovate:
  the helper lists, pins and checks and fails closed; it moves no pin to a
  newer release, because Renovate and its waiting time own updates.
  shared-ci-pinning-verdict-2: zizmor is the engine, in place of a resolver
  of our own. shared-ci-zizmor-in-fleet-gates: zizmor is in the tree of this
  repository and one other, so the helper runs from this repository's tools.
- zizmor documentation, read 2026-10-11: the list of audits
  (<https://docs.zizmor.sh/audits/>), the exit codes and the configuration
  file (<https://docs.zizmor.sh/usage/>, <https://docs.zizmor.sh/configuration/>).
  Version 1.30.1 as installed from `requirements/tools.txt`.
- `tests/test_pins.py`: each kind of finding, each guard and each way the
  metadata can be unreadable, with the real zizmor for the listing, the
  switched-off audits, the ignore comment and the run told to stay offline.
- Each check was seen to refuse on 2026-10-11: 17 defects planted one at a
  time in a copy of the tool, among them each guard removed, the key
  comparison removed and the persona changed; the tests failed on each. The
  rewritten copy of the 24 repositories was compared line by line with the
  reference. The logs are not part of the tree.
