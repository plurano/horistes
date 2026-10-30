<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0065: The train yields to a lander lease

- **Status**: Accepted
- **Date**: 2026-10-30
- **Authors**: Lusoris

## Context

The merge train periodically sweeps every repository the app is installed on, landing
the oldest clean pull request ([ADR-0029](0029-a-forge-app-drives-the-merge-train.md)).
Some repositories also run a lander (a landing queue outside the forge). When both run,
the train can land pull requests ahead of the lander, invalidating the lander's prepared
head commit and forcing it to re-test.

Coordination must happen through the forge without extra app permissions, webhooks, or
local state access.

## Decision

- A repository label named `merge-train: lander lease` serves as the coordination lease.
  Its description holds an unexpired UTC timestamp in the format `until=YYYY-MM-DDTHH:MM:SSZ`.
- The lander writes and renews the lease label; the merge train only reads it. A lander
  sets an expiry longer than the train's sweep interval and renews it while active.
- When an unexpired lease is present, the train yields landing order to the lander and
  lands only security updates. Security fixes bypass the lease so critical patches never
  wait behind an external landing queue.
- If the lease expires, the train automatically resumes landing all clean pull requests
  without manual intervention.
- A malformed lease description causes the train to refuse the repository and exit red
  without performing any writes, signaling that a person must fix the description.
- Disarming an already armed pull request is out of scope; once armed, the forge merges it.
- Rights remain unchanged; existing permissions suffice.
- The lease label is intentionally omitted from the repository label taxonomy file: labels
  outside the taxonomy file remain untouched on the forge, whereas a taxonomy sync would
  overwrite the description.

## Consequences

The train yields its landing order to an active lander while keeping security updates
unblocked. A short window stays open: a lease written after a sweep read the repository takes
effect on the next sweep, and a lander handles that overtake as any other moved base.

Tests in `tests/test_merge_train.py` (`test_*lease*`): verify the GraphQL query, yielding
clean and behind pull requests, permitting security updates, keeping security holds under a
lease, refusing malformed or null descriptions before writes occur, unleased and expired
landing, boundary timestamp handling, and maintaining switched-off behavior.
