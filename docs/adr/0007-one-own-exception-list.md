<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0007: Exceptions the engine's list cannot carry live in one own list

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

An exception here is one rule in one file, with a reason and a day it ends;
once that day has passed it fails like a missing exception. Three places held
such entries: the engine's list in `.standards.yaml`, the accepted findings in
`.github/zizmor.yml`, and the dated hosts in `hosts.toml`
([ADR-0005](0005-host-exception-with-an-end.md)).

The switch of this repository's own licence check needs a fourth kind. The
engine declares the required check of a job that calls a reusable workflow
under the calling job's own name, which no run reports
(cordanaLLM/praetor issue 1212). The engine's audit compares
`.github/rulesets/main.json` with what it declares, so the file has to carry
that name. The engine's list refuses an entry for it: `praetorctl audit`
accepts only the rule names its own gates read.

## Decision

- `exceptions.yaml` at the root holds the exceptions no other list can carry.
  An entry has the fields of the engine's list (`rule`, `path`, `reason`,
  `expires`) and `issue`, the issue that tracks the cause.
- `tools/exceptions.py check`, already the reader of the accepted findings, is
  the only reader. It fails on an entry with a missing, empty or unknown field,
  on a path that is no file of the repository, on a rule and path named twice,
  on a day that has passed and on a day more than 90 days ahead. The cap of 90
  days holds for the accepted findings in `.github/zizmor.yml` as well.
- The list has no tiers and no patterns: one entry names one file.
- First entry: the rule-set file and issue 1212, until 2026-12-31. No rule set
  is live on this repository while it is private. Before it goes public or a
  rule set goes live, the issue is fixed or no `praetorctl sync --remote` runs.

## Consequences

- **Positive**: a known defect the repository has to live with is named,
  counted by the gate and ends by itself; renewing it is a reviewed change.
- **Negative**: a second list in the engine's shape exists beside the engine's
  own until the engine accepts rules a repository declares
  (cordanaLLM/praetor issue 1206); then the entries move there and this file
  and its reader go.
- **Neutral**: the dated hosts stay in `hosts.toml`, where the host scan reads
  them together with the hosts they belong to.

## References

- Fleet answer ledger, id shared-ci-own-switch-required-context, 2026-10-11
  (paraphrased): the repository switches its own licence check now, on the
  condition that the known engine defect is carried as a dated entry of at
  most 90 days that names the issue, and that no remote sync runs before the
  issue is fixed once the repository is public or a rule set is live.
- Fleet answer ledger, id shared-ci-1212-entry-place, 2026-10-11 (paraphrased):
  the entry lives in the repository's own list in the engine's shape; the
  existing exception tool is extended and stays the only reader; it fails on
  an expired entry, a missing field and a longer expiry, with planted cases.
