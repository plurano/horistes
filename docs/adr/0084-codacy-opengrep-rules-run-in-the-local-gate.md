<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0084: Codacy Opengrep rules run in the local gate

- **Status**: Accepted
- **Date**: 2026-11-14
- **Authors**: Lusoris

## Context

Issue 186 records findings that Codacy reported after the local gate passed.
The `753b7aa` / `fc53740` shape in `tests/test_examples.py:188`, passing
`argv` to `subprocess.CompletedProcess`, is one example. The local checks
had no mirror of Codacy's enabled Python rules. The outside-host question
for Semgrep remained open in [ADR-0028](0028-every-commit-is-scanned-for-secrets.md).

## Decision

Horistes (Greek for a boundary commissioner, pronounced ho-RIS-tees;
[ADR-0023](0023-the-name-is-horistes.md)) holds the scanner, rule configuration
and dependency lockfile. [tools/codacy_opengrep_scan.py](../../tools/codacy_opengrep_scan.py)
is adapted from `lusoris/SnatchArr`'s `tools/ci/codacy_opengrep_scan.py` at
`21f590a3535f`, under EUPL-1.2, with source credit in its docstring.
SnatchArr (spoken snatch-ar) asks the `*arr` media managers, such as Sonarr
and Radarr, to search for missing items and cutoff-unmet upgrades. Each
search is a snatch, and the `-arr` suffix follows that family's naming.
Moving SnatchArr onto this copy is a separate, later change.

[Makefile](../../Makefile) adds `make check-opengrep` to `verify`.
It checks the installation stamp, scans against the baseline, verifies the
canaries, and refuses baseline growth against `BASE_REF`; it never installs.
The pre-push hook in [lefthook.yml](../../lefthook.yml) checks the same stamp
before the remaining jobs; a missing or stale install names
`make opengrep-tools`. The gate workflow
[ci.yml](../../.github/workflows/ci.yml) runs `make opengrep-tools` as its
own step before `make verify-all`. The installed npm tree is git-ignored,
and [.yamllint.yaml](../../.yamllint.yaml) takes its ignore list from
[.gitignore](../../.gitignore), so `lint-yaml` does not read the YAML files
of the installed packages.

Only installation through `make opengrep-tools` may reach `registry.npmjs.org`
for the integrity-pinned CLI dependencies and the forge release asset for
the SHA-256-pinned Opengrep binary. The scan uses the cached binary and asks
no registry or Codacy endpoint. The gate workflow's pinned `setup-node`
uses the forge manifest and release assets, with `nodejs.org` as its fallback.
[hosts.toml](../../hosts.toml) declares those hosts. This settles the
outside-host note ADR-0028 left for Semgrep without adding a scan-time host.

The rule set is the checked-in read-only Codacy listing at
[enabled-python-ids.txt](../../tools/codacy_opengrep/testdata/enabled-python-ids.txt).
Its SHA-256 and count are recorded in the metadata header of
[opengrep-python.config.json](../../tools/codacy_opengrep/opengrep-python.config.json).
A listing refresh is manual, taken from the read-only listing and checked
offline with `--drift LISTING`. There is no Codacy fetch during a scan.

[baseline.json](../../tools/codacy_opengrep/baseline.json) is shrink-only.
Each entry records a content fingerprint, count, reason, issue, entry date,
expiry and MEL category. Changed or increased findings, stale entries,
expired entries, extended exceptions and growth against the base revision
fail the check. A base revision without a baseline permits its first adoption.

The Opengrep pin in [tool-versions.env](../../tools/codacy_opengrep/tool-versions.env)
must equal the installed CLI adapter's `preferredVersion`. The scanner
refuses a mismatch. [renovate.json](../../renovate.json) updates the locked
npm CLI; it has no separate Opengrep pin rule. An update that this scanner
cannot absorb requires a new decision.

Before invoking the CLI, the scanner verifies that its cached, wrapped engine
returns the pinned version. An execution failure stops before the adapter's
automatic repair can download a replacement. The engine's log stays in the
temporary scan directory.

The binary supports Linux x64 only. Other hosts skip scan, installation
and baseline checking with a line naming that reason and the host platform;
offline drift checking runs everywhere. The CLI receives
`--max-file-size 1MB`, and any `skippedFiles` or error entry fails the scan,
so a larger file cannot silently disappear from coverage.

## Consequences

The local gate and Codacy use the checked-in set of enabled Python rules.
[tests/test_codacy_opengrep_scan.py](../../tests/test_codacy_opengrep_scan.py)
replays recorded clean and planted reports in both directions and exercises
the scanner offline. The recorded reports quote scanned source. A quoted
ignore comment is JSON-escaped there, so `make check-exceptions` counts it
once, in its source file, and the decoded fingerprint stays unchanged. Every real scan also checks the `753b7aa` / `fc53740`
shape and the intrafile taint canary, so an unavailable engine or missing
taint behavior fails instead of producing a clean result.

Installation needs Node 24 and the declared hosts; an unchanged install is
a no-op. Scans need the verified cache and installed lockfile stamp. Linux
x64 CI enforces the scan; contributors on other platforms receive the stated
skip. Rule refreshes and justified initial baseline entries require review;
the baseline can subsequently shrink, and its dated entries must be resolved
before expiry.
