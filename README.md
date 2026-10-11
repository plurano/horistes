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
checks every workflow added here must pass. No reusable workflow exists yet.

## Layout

| Path | Content |
| :--- | :--- |
| `.github/workflows/` | reusable workflows, and this repository's own gate (`ci.yml`) |
| `actions/<name>/` | composite steps |
| `tools/` | the checks of the gate |
| `tests/` | tests of those checks; each rule has a planted defect that must fail |
| `interface.lock.json` | recorded interface of every workflow and action |
| `hosts.toml` | hosts and outside actions the workflows may reach |
| `docs/adr/` | decision records |

## Gate

`make verify-all` is the one gate, locally and in CI. It needs Python 3.12 or
newer, actionlint 1.7.12 and `praetorctl` on the path; the other tools are
installed into `.venv` from `requirements/tools.txt` with their hashes.

- `make verify` runs this repository's own checks without the engine: YAML and
  Python lint, actionlint and zizmor, the interface record, the host scan, the
  accepted findings, the tests and the licence headers.
- `make interface-snapshot` rewrites `interface.lock.json` after an interface
  change. A change that breaks a caller also needs `interface_major` raised.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
