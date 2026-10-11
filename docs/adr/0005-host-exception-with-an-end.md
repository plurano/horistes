<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0005: A host without a switch is an exception with a day it ends

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

[ADR-0003](0003-declared-hosts.md) lets a reusable workflow reach a host
outside the forge only behind a boolean input that is off by default. The
licence check cannot follow that rule yet. The job the engine writes into every
repository runs the licence tool through `fsfe/reuse-action`, a container
action whose image the runner pulls from a public registry. A switch for that
host would switch off the whole check, and no pinned image of the tool exists
on the forge's registry.

The shared workflow has to decide exactly what the job it replaces decides
([ADR-0004](0004-same-verdict-before-a-switch.md)), so the first version runs
the same tool the same way.

## Decision

- A host of a reusable workflow may be declared in `hosts.toml` with
  `expires = YYYY-MM-DD` in place of `input`. Such an entry is an exception: one
  host in one file, with a reason and a day it ends.
- `tools/hosts.py check` accepts the entry up to that day and fails from the
  day after. It also fails on an entry that names both `input` and `expires`,
  on an `expires` that is no date, and on `expires` in a file that is no
  reusable workflow (there a reason is enough and nothing would end).
- The gate prints the number of such exceptions with its result.
- First entry: `docker.io` for `.github/workflows/licence.yml`, until
  2026-12-31. The action stays pinned by commit. The exception ends when a
  pinned image of the licence tool is on the forge's registry and the workflow
  takes it from there. If the image tag behind the action moves before that,
  the fallback is to install the tool with recorded hashes.

## Consequences

- **Positive**: the shared licence check and the job it replaces run the same
  tool, so the switch can be shown to change no verdict; the gap in the host
  rule is named, counted and ends by itself.
- **Negative**: until the entry ends, every caller of the licence workflow
  pulls an image from a public registry without a login, which shares the
  registry's rate limit per address on self-hosted runners.
- **Neutral**: renewing an exception is a change of `hosts.toml` and is
  reviewed like any other host.

## References

- Fleet answer ledger, id shared-ci-licence-tool-source, 2026-10-11
  (paraphrased): the shared licence workflow uses the same tool path as the
  engine-written job, as a dated exception to the forge-only default that ends
  when a pinned tool image exists on the forge's registry; installation with
  hashes is the fallback.
- Fleet answer ledger, id ci-config-19 (paraphrased): tools come from the
  pinned development image or the forge's registry.
