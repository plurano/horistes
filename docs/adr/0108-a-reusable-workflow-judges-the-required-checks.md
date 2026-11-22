<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0108: A reusable workflow judges the required checks

- **Status**: Accepted
- **Date**: 2026-11-21 (amended 2026-11-22, decision **b**)
- **Authors**: Lusoris

## Context

Organisation rulesets list many required check contexts. Per-repository aggregators drift, and a single long-polling caller competes with gate workflows for `GITHUB_TOKEN` budget (1,000 requests/hour/repository). Earlier aggregators established verdict rules: owed contexts must report `success`; foreign event/branch suites are excluded; unexpected app slugs are ignored; transient forge errors and rate limits stay bounded.

Horistes ships reusable workflows as thin YAML over standard-library Python at `job.workflow_sha` ([ADR-0074](0074-the-commit-message-judge-runs-on-the-host-python.md)).

## Decision (b)

Two reusable workflows share [required_checks.py](../../tools/required_checks.py):

| Workflow | Trigger | Target | Draft stop |
| :--- | :--- | :--- | :--- |
| [required-checks.yml](../../.github/workflows/required-checks.yml) | gating caller tail after `result` | `sha` (head commit), one-shot | yes (second barrier behind caller skip) |
| [required-checks-sweep.yml](../../.github/workflows/required-checks-sweep.yml) | [required-checks-caller.yml](../../.github/workflows/required-checks-caller.yml) `schedule` `17,47 * * * *` + `workflow_dispatch` | `open-pulls` + default branch head | no |

Gating callers ([licence](../../templates/callers/licence.yml.tmpl), [secret-scan](../../templates/callers/secret-scan.yml.tmpl), [sign-off](../../templates/callers/sign-off.yml.tmpl), [workflow-check](../../templates/callers/workflow-check.yml.tmpl)) add job `required-checks` after `result` with `needs: result`, `if: always()` plus same-repository and draft guards, concurrency `required-checks-${{ github.workflow }}-${{ github.event.pull_request.head.sha || github.sha }}` with `cancel-in-progress: false`, and call the tail workflow. Fork heads skip the tail (`::notice::`); the sweep posts for them.

### Permission grants

| Caller piece | Scopes | Serves endpoint |
| :--- | :--- | :--- |
| Gate reusable job | as declared by each gate workflow | gate's own reads |
| Tail job | `contents: read`, `checks: read`, `actions: read`, `statuses: write` | checkout judge; list check runs; list workflow runs; read/post commit statuses |
| Sweep caller job | same reads + `pull-requests: read` | open pulls list |

Only `statuses` is write scope in tail and sweep jobs.

### Judge rules ([tools/required_checks.py](../../tools/required_checks.py))

- **Self exclusion** by workflow path: `open-pulls` only (`GITHUB_WORKFLOW_REF`); `sha` tail counts the gate's own `result` check run.
- **Fork tail stop** on `pull_request` when head repo ≠ `GITHUB_REPOSITORY` (zero API calls).
- **Pending post** only when the current `required-checks` status is `success` (rerun).
- **Freshness**: post within `POST_FRESH_SECONDS` (10) of snapshot read end; one re-read; otherwise `::warning::` and exit 0.
- **Sweep cap**: `SWEEP_MAX_CALLS` (250) per session.

Render context `[RequiredChecks]` (Required, PullRequestOnly, NotOwed, Apps) is the single source for tail and sweep template inputs ([tools/render.py](../../tools/render.py)).

## Budget (measured in tests, one page per read)

| Scenario | API calls |
| :--- | ---: |
| Tail one sha | 3 reads + 0–1 POST ≈ 4 |
| Sweep 14 open heads + default | 78 |
| Sweep `OPEN_PULLS_MAX` (100) + default | 228 |
| Page-bound sweep (uncapped theory) | 3015 |
| Page-bound sweep (cap enforced) | 250 |

Calls per hour at a 30-minute schedule: \(2 \times 228 = 456\) at 100 open pulls (under 1,000/h).

## Alternatives rejected

- **a — `workflow_run` caller**: needs a dated dangerous-triggers exception against the ADR-0098 rendered-caller zizmor contract.
- **c — sweep only**: ≈1,368 calls/hour at 10-minute interval with 100 open pulls (exceeds 1,000 budget).

## Revisit when

- Pinned actionlint accepts `concurrency.queue`.
- Measured evidence that a shared concurrency group leaves a PR `CLEAN` with a lone cancelled tail check run.
- Measured token use near the repository budget cap.

## Consequences

Consumers render tails from gating templates and the sweep from [required-checks.yml.tmpl](../../templates/callers/required-checks.yml.tmpl) into `.github/workflows/required-checks-caller.yml`. Interface stays at `interface_minor` **0.3** in [interface.lock.json](../../interface.lock.json).