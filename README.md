# Plurano shared CI (working name)

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/plurano/shared-ci/blob/HEAD/AGENTS.md
<!-- praetor:readme-governance:end -->

Reusable workflows and templates for the repositories of the family: the jobs
every repository runs (build, test, lint, security scans, release, landing) are
kept once here and called from thin callers, instead of each repository keeping
its own copy.

## Name

`shared-ci` is a working name. The real name is chosen before the first
release.

## Status

Private; nothing is released yet. The repository has its own gate and the
checks every workflow added here must pass. The first reusable workflow is the
licence check; this repository is its first caller and no longer keeps a
licence job of its own.

## Workflows

| File | Purpose | Inputs | Check | Hosts beyond the forge |
| :--- | :--- | :--- | :--- | :--- |
| `.github/workflows/licence.yml` | every file carries its licence information (`reuse lint`) | `runner` (label, default `ubuntu-26.04`, the newest working image); `root` (directory checked as the project root, default `.`) | `<calling job> / REUSE lint` | `docker.io` until 2026-12-31 ([ADR-0005](docs/adr/0005-host-exception-with-an-end.md)) |

The job reads the contents and takes no secret; the caller grants
`contents: read` and nothing else. On a draft pull request the job stops with
an error instead of running. `root` must be a plain relative path (letters,
digits, dot, underscore, hyphen, slash; no `..`); anything else stops the job
before the tool runs.

## Layout

| Path | Content |
| :--- | :--- |
| `.github/workflows/` | reusable workflows, this repository's own gate (`ci.yml`) and its callers (`licence-check.yml`) |
| `actions/<name>/` | composite steps |
| `tools/` | the checks of the gate, and the same-verdict check for a switch |
| `tests/` | tests of those checks; each rule has a planted defect that must fail |
| `tests/fixtures/` | planted cases a workflow is run on: `unlicensed` must fail the licence check, `licensed` must pass it ([ADR-0006](docs/adr/0006-planted-case-as-a-fixture.md)) |
| `interface.lock.json` | recorded interface of every workflow and action |
| `hosts.toml` | hosts and outside actions the workflows may reach |
| `exceptions.yaml` | exceptions to rules no other list can carry, each with its issue and a day it ends ([ADR-0007](docs/adr/0007-one-own-exception-list.md)) |
| `docs/adr/` | decision records |

## Gate

`make verify-all` is the one gate, locally and in CI. It needs Python 3.12 or
newer, actionlint 1.7.12 and `praetorctl` on the path; the other tools are
installed into `.venv` from `requirements/tools.txt` with their hashes.

- `make verify` runs this repository's own checks without the engine: YAML and
  Python lint, actionlint and zizmor, the interface record, the host scan, the
  accepted findings and exceptions, the tests and the licence headers.
- `make interface-snapshot` rewrites `interface.lock.json` after an interface
  change. A change that breaks a caller also needs `interface_major` raised.

## Switching a consumer

A repository replaces its own job by a shared workflow only after both ran
side by side and decided the same
([ADR-0004](docs/adr/0004-same-verdict-before-a-switch.md)):

```sh
python3 tools/verdict.py --map trial.toml --live --record runs.json --markdown verdict.md
```

`trial.toml` names the repository, the trial's branches, each pair of old and
new check with their workflow files, and the planted commits:

```toml
repository = "owner/name"
branches = ["trial/licence"]

[[pair]]
family = "licence check"
class = "R"
old = "REUSE lint"
new = "Licence / REUSE lint"
old_workflow = ".github/workflows/reuse.yml"
new_workflow = ".github/workflows/licence-check.yml"
called_workflow = ".github/workflows/licence.yml"
draft_stop = "Stop on a draft pull request"

[[planted]]
family = "licence check"
commit = "<full id of a commit both jobs must fail>"
expect = "FAIL"
marker = "reuse lint"

[[planted]]
family = "licence check"
commit = "<full id of a commit both jobs must pass>"
expect = "PASS"
```

A planted case can also be a fixture: a folder of the repository both forms
of the job are run on, in jobs of their own that exist only on the trial's
branch. Such a pair must reach the named verdict on every subject and counts as
a planted case of its family
([ADR-0006](docs/adr/0006-planted-case-as-a-fixture.md)):

```toml
[[fixture]]
family = "licence check"
class = "R"
old = "Old form, unlicensed fixture"
new = "New form, unlicensed fixture / REUSE lint"
old_workflow = ".github/workflows/trial-old.yml"
new_workflow = ".github/workflows/trial-new.yml"
called_workflow = ".github/workflows/licence.yml"
draft_stop = "Stop on a draft pull request"
expect = "FAIL"
marker = "reuse lint"
```

`--root` is the consumer's checkout and `--called-root` the checkout of this
repository when the two differ. `--runs FILE` judges a recorded file instead of
the forge. Exit code 0 allows the switch; 1, 2 and 3 do not.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
