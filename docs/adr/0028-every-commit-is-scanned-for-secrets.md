<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0028: Every commit is scanned for secrets in the gate

- **Status**: Accepted
- **Date**: 2026-10-13
- **Authors**: Lusoris

## Context

The repository is public
([ADR-0024](0024-public-before-the-first-release.md)), and no workflow
scanned it for credentials. The gap audit of 2026-10-12 listed this as row
A2-01. The engine at commit 3bbde07 audits the linters a repository declares
and fails when no workflow runs one. `.standards.yaml` declares gitleaks
through the facet `security:high`, together with semgrep and trivy, and the
adoption wrote a dated exception for each of the three.

## Decision

- The gate workflow `.github/workflows/ci.yml` installs gitleaks 8.30.1 the way
  it installs actionlint: the release archive from the forge, checked against
  the SHA-256 of the release's checksum file. It then scans every commit of the
  checkout before `make verify-all` runs. The job already checks out the full
  history, and its check is the required `Verification gate`.
- The scan uses the tool's own rules only. The step refuses a `.gitleaks.toml`
  or `.gitleaksignore` in the tree, clears the two variables that name a rule
  file, and ignores `gitleaks:allow` comments. Otherwise the change that adds
  a secret could also add the rule that hides it. A finding fails the step,
  and its secret is redacted in the log.
- The gitleaks exception in `.standards.yaml` is removed. semgrep and trivy keep
  theirs until 2027-01-11: semgrep's rules come from its registry, an outside
  host, and trivy downloads its vulnerability database from one. Both need a
  host decision under [ADR-0010](0010-one-longest-time-for-every-exception.md)
  before they can run.
- `make test` passes `GITLEAKS` to the tests, like the other outside tools.
  `tests/test_secret_scan.py` runs the step as written on planted repositories,
  and checks that the tool is the release the workflow pins.

## Consequences

- **Positive**:
  - A credential committed anywhere in the history fails the required check,
    even after a later commit removes it.
  - A finding cannot be silenced from inside the change: the planted rule
    file and the allow comment both stay red.
- **Negative**:
  - A false positive cannot be allowed in the tree. It needs a change to this
    record and to the step.
  - A contributor needs gitleaks 8.30.1 on the path to run the gate locally.
  - The update bot does not read the gitleaks version or its checksum, as it
    does not read actionlint's; both are raised by hand.
- **Neutral**: the scan reads the commits of the checkout. A scan of the
  forge's other refs is not part of the gate.

## References

- Fleet order fleet-engine-pin-sweep-20261013 (paraphrased): pin the engine
  commit that audits declared linters, and for each declared scanner that never
  runs, either add a pinned, fail-closed job shown failing on a planted case or
  keep the dated exception with its reason.
- `tests/test_secret_scan.py`: a clean history passes. A planted token fails,
  also when a later commit removes it or an allow comment marks it, and its
  value is not printed. A rule file or an ignore file in the tree is refused.
  With the refusal taken out of the step, a rule file without rules turns the
  planted token green.
- Mutants of the step, each caught by a test: without `--ignore-gitleaks-allow`,
  without `--redact`, and with the exit status ignored.
- gitleaks 8.30.1 `git --help`: a rule file is read from `--config`, from
  `GITLEAKS_CONFIG`, from `GITLEAKS_CONFIG_TOML` or from `.gitleaks.toml` in the
  scanned path, in that order. Without one, the tool's own rules apply.
- The scan of this repository at the time of writing: 42 commits, no leaks.
