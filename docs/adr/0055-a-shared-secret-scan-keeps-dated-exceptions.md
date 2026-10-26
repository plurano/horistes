<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0055: A shared secret scan keeps dated exceptions

- **Status**: Accepted
- **Date**: 2026-10-26
- **Authors**: Lusoris

## Context

Two consumer repositories carry the same secret scan: `jellysin/plugin-lastfm`
keeps `tools/secret_scan.py`, and `tekmora/diallak` keeps a copy of it in
`tools/ci/secret_scan.py`, taken at plugin-lastfm commit `e5b0c49`. A
deduplication scan across the fleet (`praetorctl dedupe scan --across`) found
the pair. diallak's `.gitleaksignore` holds one dated exception, for a test fixture
that Gitleaks reports as a key; the scan lets it through until its expiry. Horistes' own
gate scans every commit without exceptions or ignore files
([ADR-0028](0028-every-commit-is-scanned-for-secrets.md)) and stays that way.

## Decision

- A shared reusable workflow `.github/workflows/secret-scan.yml` runs Gitleaks
  over the caller's full history with expiring exceptions recorded in
  `.gitleaksignore`.
- The scanner script lives in `tools/secret_scan.py`, taken from
  `jellysin/plugin-lastfm` `tools/secret_scan.py` at commit `b2e289d160c9`
  (EUPL-1.2, same author); only type hints, formatting and the planted seed
  string changed. It is checked out from the
  workflow's own commit (`job.workflow_repository` at `job.workflow_sha`),
  ensuring callers execute the version of the script matching their pinned
  workflow commit ([ADR-0053](0053-the-train-sweep-is-a-checked-out-file.md)).
- The scan step is preceded by a mandatory `--self-test` step that verifies
  scanner detection against temporary repositories with planted keys and
  expired/unexpired exceptions before the caller's real history is trusted.
- A caller template `templates/callers/secret-scan.yml.tmpl` provides thin
  caller wiring with a result job reporting the required check name `Secret scan`
  ([ADR-0030](0030-a-result-job-reports-the-calling-jobs-name.md)).
- `actionlint` 1.7.12 does not know `job.workflow_*`; the finding is accepted
  in `.github/actionlint.yaml` with an expiry bounded under
  [ADR-0010](0010-one-longest-time-for-every-exception.md).

## Consequences

- Consumer repositories drop their local scanner script copies and wire to the
  shared workflow.
- Exceptions remain strictly bounded: exactly one full `commit:path:rule:line`
  fingerprint per `# expires YYYY-MM-DD: reason` line.
- Horistes' own gate remains untouched and exception-free (ADR-0028).
