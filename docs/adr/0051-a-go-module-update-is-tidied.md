<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0051: A Go module update is tidied

- **Status**: Accepted
- **Date**: 2026-10-21
- **Authors**: Lusoris

## Context

The update bot proposed `github.com/ulikunitz/xz` v0.5.15 to
`plurano/pertica` (commit `c1d90d0`). The `go.sum` of `tools/go` in that
commit lacked the hash of a module the build then needed, and the secret-scan
job, which builds gitleaks from that module file, stopped with
`missing go.sum entry for module providing package github.com/ulikunitz/xz`
(run 39588424967). The hash was added by hand.

Renovate 44.175.0 updates a Go module with `go get` and runs `go mod tidy`
afterwards only when `postUpdateOptions` holds `gomodTidy` or one of its
variants (`lib/modules/manager/gomod/artifacts.ts`). The preset held no
`postUpdateOptions`, and no repository of the family sets it.

## Decision

- The preset `default.json` sets `"postUpdateOptions": ["gomodTidy"]`.
- `gomodTidy` is a value of the `postUpdateOptions` enum in the Renovate
  configuration schema (`renovate-schema.json`, read 2026-10-21) and in
  `lib/config/options/index.ts` of Renovate 44.143.0 and 44.175.0.

## Consequences

- An update of a Go module rewrites `go.mod` and `go.sum` to what
  `go mod tidy` leaves, so the module hashes the build needs are part of the
  proposal.
- Renovate still skips `go mod tidy` on a major update unless
  `gomodUpdateImportPaths` is set too; a major update keeps the review it gets
  today.
- The option has no effect on a repository without Go modules.
- `tests/test_update_bot.py` refuses a preset without the option, with an
  empty list, with another option only, or with the option as a plain string.
