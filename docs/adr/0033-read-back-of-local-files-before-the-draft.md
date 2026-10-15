<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0033: The read-back reads a release's files from a local folder before the draft

- **Status**: Accepted
- **Date**: 2026-10-15
- **Authors**: Lusoris

## Context

The read-back of [ADR-0014](0014-release-read-back-without-credentials.md)
verifies a published release. `plurano/amussis` reads its release files back
in the release workflow before the draft release exists, so that files that
fail never reach a release page. It could not use the shared tool there and
kept a copy of its own with four planted controls, against 25 of the shared
tool. A rule fixed in one copy was not fixed in the other
(plurano/amussis#5).

## Decision

`tools/readback.py verify` takes `--local DIR`. Only the source of the assets
changes:

- The assets are the files directly in `DIR`. A symbolic link, a
  subdirectory, a name that is not a plain file name or more than 200
  entries stops the run with exit code 3.
- No request goes to the forge's release list or its download addresses.
  The control for an asset that does not exist asks the folder instead.
- The commit of the tag, the signature, the provenance from the forge's
  attestation API, the images and every control stay as they are. The tag
  must already be pushed and the files attested, which the build job of a
  release workflow does before the read-back runs.
- The first `NOTE` line names the local folder, so a log shows which source
  was read.

## Consequences

- A release workflow calls the shared tool on its build files before the
  draft, and its own copy of the checks goes.
- In local mode the forge has recorded no digest yet; the digests are
  checked against the checksums and the provenance only.
- The published mode is unchanged: the existing tests pass with the same
  line counts and requests.
- Tested in `tests/test_readback.py`: a good folder passes with the same
  counts as a published release, every blob defect of the published tests
  fails in local mode as well, and each folder refusal has its own case.
