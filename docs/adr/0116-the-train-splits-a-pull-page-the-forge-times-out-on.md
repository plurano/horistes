<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0116: The train splits a pull page the forge times out on

- **Status**: Accepted (amends [ADR-0080](0080-the-train-outlasts-transient-forge-errors.md))
- **Date**: 2026-11-25
- **Authors**: Lusoris

## Context

Fleet sweep 45654277865 refused `vidiko/vmafx` with HTTP 502 on a GraphQL POST.
Earlier sweeps reported HTTP 504. The refusal identified the repository and
endpoint, but every train query and mutation used that endpoint.

On 2026-11-25 at 18:14 CET, the open pull request query on that repository took
10.4 seconds for 25 pulls (881,697 bytes) and 4.5 seconds for 10 (356,331
bytes). Retrying the same 25 pulls could repeatedly hit the forge's time limit.

## Decision

Give every GraphQL document a unique operation name and prefix its refusals
with that name. Preserve the exception type and transport cause used by the
retry and merge-in-progress handlers. An anonymous document stops the sweep
with a programming error.

Request 25 open pulls initially. On a transient refusal, keep the cursor and
halve the requested size, down to six. Keep the reduced size for later pages
and settle reads of this repository. A page of six or fewer uses the three
read attempts from ADR-0080. A permanent refusal still ends the read immediately.
The last request asks for at most the remainder of the 100-pull allowance.
The forge's final `hasNextPage` determines whether the summary reports truncation.

Report a recovered split once in the repository section with the reduced size
and the first line of the original refusal. If the repository is later refused,
include the split in its refusal line. If the smallest request fails, report
its size and cursor and fail the sweep. Mutations still run once.

Write each report record as one physical line to stdout and the step summary.
Escape pipes in table cells. Write workflow annotations on one line and keep
the release outputs as JSON.

Start the 1,500-second sweep budget after settings validation, including the
app-key check. Check the budget before every pull-page attempt, including
splits and retries after a backoff. A budget refusal names the document,
page size and cursor. When the budget stops a retry, it also names the last
refusal. A request already in flight can finish after the budget is spent.

## Consequences

- A repository with 100 or more open pulls normally takes four page calls per
  pass. A persistent outage takes five calls at the first cursor (25, 12, 6,
  6, 6), with two five-second backoffs.
- With full pages and both splits, each pass needs at most 17 pages at the
  floor, including the final four pulls. If each succeeds on its third
  attempt, three settle rounds cost at most `2 + 3 × 17 × 3 = 155` pull-page
  calls per repository. The two larger failed calls occur only once.
- At 30 seconds per call plus retry and settle sleeps, that scenario would
  cost 5,170 seconds, and a single pass 1,760 seconds. The budget stops it:
  the sweep checks it before each repository and before every pull-page
  attempt. Only one pull-page request can be in flight when the budget is
  spent. Settings validation uses time from the 30-minute job limit. A fleet
  run spent 5 seconds before the train step and 4 seconds after it (run
  45675358659). Later repositories are listed as unreached.
- If the forge returns short nonfinal pages, each must advance the cursor and
  contribute at least one pull. The absolute bound is 100 pages per pass, or
  902 pull-page calls across three rounds including both splits. The same
  timeout-and-sleep calculation gives 30,070 seconds before the budget ends
  the read. These figures cover pull pagination only. The socket timeout
  applies to individual I/O operations.

## References

- Query and status variant in [.github/workflows/merge-train.yml](../../.github/workflows/merge-train.yml).
- Page sizing, settle rounds and sweep budget in [tools/merge_train.py](../../tools/merge_train.py).
- The oversized-page 504 fixture and its split-disabled control in [tests/test_merge_train.py](../../tests/test_merge_train.py).
