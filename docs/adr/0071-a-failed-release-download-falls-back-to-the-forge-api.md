<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0071: A failed release download falls back to the forge API

- **Status**: Accepted (amends [ADR-0058](0058-shared-installer-actions.md))
- **Date**: 2026-11-09
- **Authors**: Lusoris

## Context

The verification gate, the shared workflow check and the shared secret scan install
pinned release binaries (actionlint, gitleaks) from the forge's release-download
address, `https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>`, and check
each archive against a pinned SHA-256 before unpacking it
([ADR-0028](0028-every-commit-is-scanned-for-secrets.md),
[ADR-0036](0036-the-update-bot-moves-pinned-release-tools-and-checksums.md),
[ADR-0058](0058-shared-installer-actions.md)).

That address was the only path. On 2026-11-09, between 05:20 and 05:31 UTC, it
answered HTTP 504 to hosted runners on and off: main's gate on c98f8d3 (run
42566562278) and the gates of pull requests 48 and 114 failed at "Install the pinned
actionlint" in six attempts, each after three `curl` tries answered 504, and passed
only on reruns. `curl --retry` spreads its retries over seconds, so a longer outage of
that one address turns every gate red, with no second path to the same pinned asset.

Two of the three install steps (`.github/workflows/ci.yml`,
`.github/workflows/workflow-check.yml`) carried their own copy of the download instead
of calling `fetch_verified` from `actions/verified-download/verified-download.sh`.

## Decision

- `fetch_verified` keeps its `curl` call unchanged. When that call fails and the URL is
  a forge release-download address whose owner, repository, tag and asset match an
  allow-list of plain names, `fetch_release_fallback` fetches the same asset with
  `gh release download <tag> --repo <owner/repo> --pattern <asset> --output -`, which
  reads the release through `api.github.com`. It prints a
  `::warning title=Release download fallback::` annotation naming the failed address.
- `gh` writes to a pipe that `head -c` cuts one byte past the caller's byte bound, so
  an oversized asset stops at that size instead of filling the disk; a fetch over the
  bound, or one where `gh` or `head` fails, is refused and leaves no file.
- `run_with_deadline`, a bash function built on `sleep` and `kill`, stops `curl` (with
  its retries) and `gh` each after the caller's timeout; GNU `timeout` is absent on
  macOS runners. One call therefore takes at most twice `timeout-seconds`. Stopping
  the watcher at any point also stops its `sleep`, so no process outlives the call.
- `download_bounds_valid` refuses a byte bound or a timeout that is not a plain
  positive decimal before any arithmetic runs: bash arithmetic evaluates other text,
  command substitutions in array subscripts included. Both `fetch_verified` and
  `fetch_release_fallback` call it.
- The SHA-256 check after either path is unchanged, so the fallback installs only the
  pinned bytes.
- Any other URL, a name outside the allow-list, or a runner without `gh` fails as
  before, with no second request.
- `gh` reads `GH_TOKEN`; the `verified-download` and `install-gitleaks` actions and the
  actionlint steps set it to `${{ github.token }}`. A caller that sources the script
  through the `lib` output sets it in its own step.
- The actionlint steps of `ci.yml` and `workflow-check.yml` source
  `verified-download.sh` and call `fetch_verified`; the gitleaks step of `ci.yml` uses
  `./actions/install-gitleaks`. No workflow step downloads a release asset on its own;
  `tests/test_release_downloads.py` holds that.
- The shared workflow check adds `actions/verified-download/verified-download.sh` to
  its sparse checkout; `tests/test_workflow_check_gate.py` refuses a run step that
  reads a `workflow-check-source/` file the sparse checkout leaves out, and a
  reference to that directory whose path it cannot resolve (a variable, a glob).
- `ci.yml` joins the zizmor `self-repository` entries in `.github/zizmor.yml` with the
  same expiry and reason as the other callers of a local action.
- `tests/test_installer_actions.py` runs the fallback against stub `curl` and `gh`:
  outage, healthy path, other host, lookalike host, plain HTTP, missing `gh`, wrong
  bytes, an endless asset cut at the bound, an asset exactly at the bound and one byte
  over, failed `gh`, a `curl` and a `gh` that hang past the timeout, bounds that are
  not plain numbers (command substitutions among them) at both entry points, and 100
  deadline calls that leave no `sleep` behind. The `gh` stubs take the
  `--output -` shape only.

## Consequences

- **Positive**:
  - An outage of the release-download address alone no longer turns the gates red,
    and the warning shows which path ran.
  - One fallback serves this repository and every caller of the shared actions.
- **Negative**:
  - A caller runner without `gh` (some self-hosted runners) keeps the single path.
  - An outage of the whole forge still fails the install; no cache holds the archives.
- **Neutral**:
  - The pinned versions and checksums in the workflow `env` blocks are unchanged, so
    the update bot reads them as before
    ([ADR-0036](0036-the-update-bot-moves-pinned-release-tools-and-checksums.md)).
  - Revisit if the fallback warning appears on most runs, or if `gh release download`
    changes the asset path it reads.
