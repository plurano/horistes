<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0035: A weekly job bounds the licence tool's image age

- **Status**: Accepted
- **Date**: 2026-10-16
- **Authors**: Lusoris

## Context

[ADR-0008](0008-licence-tool-image-by-digest.md) pinned the licence tool's image
by digest on `docker.io`, noting as a negative consequence that nothing updated
the digest automatically. Renovate
([ADR-0019](0019-update-proposals-from-renovate-configured-here.md)) took ownership
of proposing updates for the image digest alongside the release comment in
`.github/workflows/licence.yml`, governed by a lower bound of three days after
a release (`minimumReleaseAge` in `default.json`).

The upper bound remained open (issue #2): nothing failed when the pin stayed old
while a newer release existed, or when the registry read failed.

## Decision

- A weekly scheduled workflow `.github/workflows/image-age.yml` runs
  `tools/image_age.py check`.
- `tools/image_age.py` reads the pinned release and digest from
  `.github/workflows/licence.yml` using the same regular expression configured in
  `renovate.json`.
- The tool queries release tags from Docker Hub's public API with an explicit
  deadline of 20 seconds. The workflow passes the URL with `--tags-url`, so the
  host scan (`tools/hosts.py`) sees the host the tool asks.
- The upper bound is 14 days (three days release age waiting period plus review
  margin). When the newest release is older than 14 days (strictly greater than 14
  days) and differs from the pinned version, the check fails (red, exit code 1).
- When a newer release exists but is 14 days old or younger, the check passes
  (green, exit code 0) and prints a note that Renovate proposes it after three
  days.
- When the pinned release tag's digest on Docker Hub differs from the pinned
  digest (the tag moved), the check reports the discrepancy and fails (red, exit
  code 1).
- When the registry cannot be read (request timeout, HTTP error, malformed JSON,
  no release tags found, or pinned release missing on Docker Hub), the check
  reports the cause and fails (red, exit code 3). Each failed read is red: a
  weekly job keeps no memory between runs, so it enforces a stricter bound than
  waiting across multiple failures.
- `hub.docker.com` is declared in `hosts.toml` for
  `.github/workflows/image-age.yml` with its reason. The workflow is not
  reusable, so the host needs neither a switch nor an end date
  ([ADR-0003](0003-declared-hosts.md)); it leaves together with the `docker.io`
  exception of `licence.yml` ([ADR-0005](0005-host-exception-with-an-end.md)),
  when a pinned image of the licence tool is on the forge's registry.

## Consequences

- **Positive**: an outdated pin or moved tag fails the weekly run and alerts
  maintainers; the upper bound is enforced automatically without human tracking.
- **Negative**: an external outage or failure of Docker Hub's public API fails
  the weekly run without a local defect in the repository.
- **Neutral**: a release difference between a caller's local pre-commit hook and
  the workflow check is not reported by this job. The job is no required check
  and not part of `make verify-all`, since it asks an outside host; its offline
  tests run in the normal test suite.

## References

- [ADR-0003](0003-declared-hosts.md)
- [ADR-0005](0005-host-exception-with-an-end.md)
- [ADR-0008](0008-licence-tool-image-by-digest.md)
- [ADR-0019](0019-update-proposals-from-renovate-configured-here.md)
- Issue plurano/horistes#2
- Fleet answer ledger, id horistes-2-upper-bound, 2026-10-16 (paraphrased): a
  a weekly scheduled job reads the registry's tags with a deadline; it is red
  when the newest release is older than 14 days and differs from the pin, or
  when the registry cannot be read; the host gets a `hosts.toml` entry, and a
  decision record names ADR-0008. The moved-tag rule is added here.
- `tools/image_age.py`, `tests/test_image_age.py`, `.github/workflows/image-age.yml`.
