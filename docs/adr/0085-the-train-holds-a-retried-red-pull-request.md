<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0085: The train holds a retried red pull request

- **Status**: Accepted
- **Date**: 2026-11-15
- **Authors**: Lusoris

## Context

A refreshed pull request can regain the front of the train while repeating
checks that already failed for the same change. Issue 188 records the
2026-09-20 observation: pull request 5650 returned to the front immediately
after its 05:26:54Z refresh. It also records the 2026-11-14 23:29Z landing of
pull request 7845 while the intended first pull request, 7840, was still
running checks, forcing another CI cycle of about 35 minutes.

At `cc4167b`, `is_red` in
[.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml)
judges the current head only, using the newest suite per workflow or app
([ADR-0042](0042-the-merge-train-judges-each-workflow-by-its-newest-run.md)).
`run` in [tools/merge_train.py](../../tools/merge_train.py) lets an armed
BLOCKED head hold followers and updates the first eligible BEHIND pull
request. A refreshed red head with pending checks therefore recovers its
priority. The planted `test_retry_hold_refreshed_red_*` cases in
[tests/test_merge_train.py](../../tests/test_merge_train.py) exercise both paths.

The existing lander lease yields all ordinary pull requests to an external
landing queue; its expiry does not describe a planned sequence
([ADR-0065](0065-the-train-yields-to-a-lander-lease.md)). The
`test_ordered_lease_out_of_order_session_does_not_land` fixture in
[tests/test_merge_train.py](../../tests/test_merge_train.py) reproduces the
clean session pull request waiting behind an earlier planned entry.

## Decision

The sweep implements retry fairness in
[tools/merge_train.py](../../tools/merge_train.py), keeping the rules heredoc
within its field budget
([ADR-0082](0082-the-train-graphql-documents-leave-the-rules-step.md)). After
trying to land a clean pull request, `hold_retries` examines in-flight or
BEHIND candidates in serving order. It reads at most `RETRY_READ_LIMIT = 5`
candidates, stopping at the first unheld one. Security updates and the label
`merge-train: urgent` bypass history reads.

`RETRY_HISTORY` reads the last 20 commits and 20 force-push events. Each
commit, including an event's previous head, carries up to 30 check suites.
`completed_attempt` applies `newest_suites` and `RED` from the rules module:
a failure completes an attempt; otherwise a nonempty set with every Actions
suite concluded completes it as green. Other attempts are ignored. The
latest completed attempt is selected by the newest failed suite's creation
time, or the newest suite's creation time for green attempts.

`retry_hold` holds a latest failed attempt for `RETRY_HOLD_DAYS = 7` only
while its own diff is unchanged. `own_diff` uses the JSON three-dot compare
against the history read's base oid, normalizing file names, previous names,
status and added/deleted patch lines. Files without patches use their blob
sha. Hunk offsets and context do not change this identity. A full 30-suite
page, 300 or more compare files, a moved head or an unreadable response holds
with an explanation. `test_retry_hold_*` in
[tests/test_merge_train.py](../../tests/test_merge_train.py) covers these
boundaries, green reruns and diff changes.

Held rows before an unheld candidate lose their in-flight/update eligibility
for that sweep. If all inspected candidates are held, the original serving
order stays and the oldest receives a release note, permitting retries even
when alone. Notes show the failed commit, workflows and UTC expiry, or the
unreadable-history cause. `hold_retries` never writes a label, disarms a pull
request or stores retry state. A live lease and a sweep that lands perform no
retry-history reads; `test_retry_hold_live_lease_does_not_read_history` and
`test_retry_hold_landing_run_does_not_read_history` pin those paths.

The optional ordered lease form is
`until=YYYY-MM-DDTHH:MM:SSZ; order=7840,7842`. `LEASE_DESC` accepts 1–20
entries of 1–7 decimal digits, each positive and without a leading zero;
`lease_until` retains its expiry return value. `next_planned` and
`serve_lander_order` in [tools/merge_train.py](../../tools/merge_train.py)
keep security rows and the first order entry that is open. That entry
retains its normal eligibility checks. An entry missing from the pull
requests read counts as closed only when every page was read; past an
unread page it may still be open, so the order is unknown and ordinary pull
requests yield as under a plain lease. The summary names the next entry,
says none is open, or shows the plain lease note when the order is unknown.
Invalid descriptions refuse the repository before any landing or update;
`disarm_red_scanning` still runs first, because taking auto-merge off a red
pull request lands nothing
([ADR-0068](0068-the-train-holds-on-failed-code-scanning.md)). Expired and
plain leases retain their previous behavior. `test_ordered_lease_*` and the
existing `test_*lease*` cases in
[tests/test_merge_train.py](../../tests/test_merge_train.py) cover the grammar,
closed entries, security bypass, unread pages and oldest-first controls.

## Consequences

This extends [ADR-0065](0065-the-train-yields-to-a-lander-lease.md), rather
than replacing it. The lander still owns lease renewal and expiry; existing
auto-merge requests remain outside the lease's power to stop the forge.
The sweep's retry memory is bounded forge history, so an older attempt can
age out of the queried window. The all-held release permits progress when
history is unavailable; its summary explicitly records that choice.

The additional logical API cost is bounded by 5 × (1 GraphQL + 2 REST) reads
per repository per sweep, as enforced by `RETRY_READ_LIMIT` and
`test_retry_hold_all_held_releases_oldest_with_bounded_reads` in
[tests/test_merge_train.py](../../tests/test_merge_train.py). Existing
transport retry limits still apply to each read. No permissions or local
state are added. Plain leases need no migration; landers opt into ordering
by adding the bounded order suffix to their existing description.
