<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0079: Dependency updates wait while a session pull request lands

- **Status**: Accepted
- **Date**: 2026-11-12
- **Authors**: Lusoris

## Context

On 2026-11-12, session pull requests in golusoris/golusoris waited for checks or
stood BEHIND while Renovate merged non-security updates. Each merge moved the
default branch, put the session branch behind again, and restarted its checks.
The session author was a person account, so bot-author matching did not identify
these pull requests.

[ADR-0062](0062-dependency-updates-wait-while-a-security-update-is-open.md) already
uses a label and a draft conversion to keep dependency updates from moving the
default branch while a security update waits. Its draft mechanism closes both
native auto-merge and Renovate's own merge path. Session holds need the same
mechanism, with a bound so a stuck session cannot hold an update indefinitely.

## Decision

Extend the existing hold rules and share the label, draft, ready and summary
writes in `tools/merge_train.py`. The session rules live there too, not in the
inline rules module: that step is close to the 4096-field limit the standards
engine's provenance audit sets for one run script, and the session rules would
pass it. The reusable workflow adds `session_authors`,
a comma-separated list of logins, and `session_hold_hours`, a whole number from
1 to 48, default 4. An empty author list disables new session holds.

An open session pull request waits when its author is listed, it is ready for
review, comes from the same repository, targets the default branch, uses no
dependency branch prefix, carries no configured hold label, and has no red
head check suite. Its merge state must be accepted by the train, BEHIND, or
BLOCKED while the newest run of a workflow on the head commit has not finished. A
BLOCKED pull request whose workflow runs have all finished does not trigger the
hold: it may be blocked by review requirements or unresolved threads. Only check
suites of workflow runs count, because the train already reads them with its
checks read right; an app suite without a workflow run can stay open forever, and
the status-check rollup would need the statuses read right on every repository.
A commit status or an app check still running therefore does not hold updates.

While a session waits, label eligible non-security dependency updates
`merge-train: session hold` and convert them to drafts. Forks, other bases,
already labelled updates and drafts no managed hold keeps are left alone. Security updates
remain eligible to land. A person marking a held update ready does not cause
another draft conversion while its session label remains.

Recompute holds each run. If no qualifying session remains, remove every session
hold label and mark a held draft ready when no other managed hold remains.
Merging, closing, red checks, conversion to draft, or a configured hold label
can end a session's wait. Also release an update when its earliest session-label
event is older than `session_hold_hours`; retain that event as evidence to skip
holding it again on later runs. The query reads the first 20 labelled timeline
events. No separate persisted state is introduced.

The security and session holds share one writer. Apply new labels before
releasing holds, draft an update at most once, and mark it ready only after
both managed holds are gone. Either hold can label a draft the other keeps, so
an update the security hold releases while a session waits passes straight to
the session hold without a ready and a second draft. An author's draft without a managed hold label is never marked
ready. Held and released notes reach the step summary; session hold notes name
the waiting pull request and the configured bound.

## Consequences

Adopters see dependency updates as drafts during a session hold. The feature is
off by default. A stuck session delays an update for at most the configured
bound, plus the interval until the next train run, before that update is
released from the session hold permanently. Security holds can independently
keep it in draft; security updates themselves remain unaffected.

The existing pull-request and label write paths supply the required rights.
`tests/test_merge_train.py` covers pending and behind sessions, exclusions,
release, the earliest-event bound, manual readiness, both holds together and
invalid settings. `test_planted_session_hold_that_finds_no_session_lands_update`
removes the session predicate and demonstrates the dependency update landing
before the waiting session.

## Alternatives

Renovate `schedule` and `prHourlyLimit` do not observe waiting session pull
requests and therefore cannot coordinate this landing order. A forge merge
queue could coordinate landing, but is unavailable for the affected private,
person-owned repositories; repositories with a queue retain the existing train
queue path from [ADR-0034](0034-the-merge-train-queues-where-the-branch-has-a-merge-queue.md).
