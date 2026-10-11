<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0010: A dated host runs no longer than any other exception

- **Status**: Accepted
- **Date**: 2026-10-11
- **Authors**: Lusoris

## Context

The repository has three places that hold an exception with a day it ends: the
accepted linter findings in `.github/zizmor.yml`, the entries of
`exceptions.yaml`, and a host of a reusable workflow declared in `hosts.toml`
with `expires` ([ADR-0005](0005-host-exception-with-an-end.md)).
[ADR-0007](0007-one-own-exception-list.md) caps the first two at 90 days ahead.
The third had no cap: `tools/hosts.py check` refused a day that had passed and
accepted any later one, so an entry dated years ahead was an exception without
an end in all but name.

## Decision

- `tools/hosts.py check` refuses a dated host whose day is more than 90 days
  ahead, with the same wording as the other two places.
- The rule is one function, `day_problem` in `tools/exceptions.py`, and the
  longest time is one constant there. `tools/hosts.py` calls it; it keeps
  reading `hosts.toml` itself, and `tools/exceptions.py` stays the only reader
  of the other two places.
- The one entry that exists, `docker.io` for `.github/workflows/licence.yml`
  until 2026-12-31, is 81 days ahead on the day of this record and stays as it
  is.

## Consequences

- **Positive**: every dated exception of the repository is looked at again
  within 90 days, whichever list holds it; a change of the longest time is one
  line.
- **Negative**: a host that has to stay longer is renewed by a change of
  `hosts.toml` at least every 90 days.
- **Neutral**: no entry that exists today changes its verdict; the cap decides
  a new or renewed day.

## References

- Fleet answer ledger, id shared-ci-m1-done, 2026-10-11 (paraphrased): the
  host exception list had no 90-day cap; the cap is the first step of the next
  unit.
- `tests/test_hosts.py`: a day exactly 90 days ahead passes, a day 91 days
  ahead fails with the message.
