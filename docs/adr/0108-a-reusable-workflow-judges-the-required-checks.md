<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0108: A reusable workflow judges the required checks

- **Status**: Accepted
- **Date**: 2026-11-21 (amended 2026-11-22 and 2026-11-23, decision **b**)
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
- **Proposal checks** remain owed on `pull_request`, `pull_request_target` and `merge_group` (`exempt_reason`). The [licence tail](../../.github/workflows/licence-check.yml) and [sweep caller](../../.github/workflows/required-checks-caller.yml) mark `Sign-off` proposal-only because the [Sign-off caller](../../.github/workflows/sign-off-check.yml) runs on pull requests and merge groups.
- **Check-run admission** requests `filter=all` and selects the newest eligible run per name. Actions checks require a suite in the workflow-run listing after event, branch, and applicable self-workflow exclusions. External checks use the configured app slug. Ten full workflow-run pages exhaust the listing budget and stop judgment (`read_snapshot`, `_read_workflow_runs`).
- **Waiting and failure** use the earliest eligible non-self workflow timestamp, with the commit date as fallback. A terminal failure in either record takes precedence over a pending peer (`_workflow_anchor`, `decide`).
- **Status writes** require a changed verdict. A pending verdict can replace only a success (`_should_post`).
- **Freshness** requires a write within `POST_FRESH_SECONDS` (10) of the snapshot read starting. Before each write, `_status_preflight` reads the newest commit-status page. A self status created or updated after the snapshot began, a change in the observed status evidence, or a full page without the self status triggers a fresh snapshot. A second stale read emits `::warning::` and exits 0. After a successful POST, the judge verifies the created status ID against page 1. A different status in the judge context created at or after the second in which the snapshot began, or a full page missing the created status ID, triggers one fresh judgment. A second contested write warns with the SHA and exits 0.
- **Write recovery** makes one POST attempt per evaluation. A rate limit or 5xx allows one additional read-decide-post cycle after the bounded wait. Other write errors and a second failed POST emit `::error::` and exit 1 (`ApiSession.request_json`, `_evaluate_one`).
- **Sweep ordering** skips success heads during triage. The candidate list contains never-judged heads in pull order, previously judged waiting heads oldest first, and previous failures oldest first. When the list exceeds `max_targets`, each sweep takes a block of that size starting at `((run_number - 1) * max_targets) % len(candidates)`, wrapping at the end. With a stable list and consecutive caller workflow run numbers, every head is selected within `ceil(len(candidates) / max_targets)` sweeps. Each evaluation reads statuses afresh and deduplicates an unchanged failure. The default head is evaluated last using its branch ref (`_sweep_evaluation_order`, `_sweep_default_branch`).
- **Sweep cap** counts every transport attempt against the 250-call session budget, including retries and failures. A POST requires one remaining call for verification (`ApiSession._request_once`).

Render context `[RequiredChecks]` (Required, PullRequestOnly, NotOwed, Apps) is the single source for tail and sweep template inputs. When `PullRequestOnly` is omitted, the renderer includes `Sign-off` if it is required ([tools/render.py](../../tools/render.py)).

## Budget

The steady-state rows use one page per listing and no retries or freshness refresh. A complete snapshot reads statuses, checks and workflow runs, plus the commit if no eligible timestamp is available. A write adds the status preflight, one POST, and the verification read. Sweep triage adds the open-pull listing, one status listing per head, and two default-branch requests. The sweep fixtures exercise commit-date fallback. See `test_sweep_call_count_at_fourteen_open_pulls`, `test_sweep_capless_call_counts` and `test_sweep_transport_call_counts_measured_at_defaults` in [tests/test_required_checks.py](../../tests/test_required_checks.py).

| Scenario | API calls |
| :--- | ---: |
| Tail, eligible workflow timestamp | 3 without a write, 6 with a write |
| Tail, commit-date fallback | 4 without a write, 7 with a write |
| One-page sweep, 14 open heads + default | 122 |
| One-page sweep, 100 open heads + default, cap disabled in test | 320 |
| One-page sweep, 100 open heads, cap enforced | 250 |
| Page-bound sweep, cap disabled in test | 3,677 |
| Page-bound sweep, cap enforced | 250 |

At 100 open heads, the capped one-page fixture posts for 21 pull heads before the session stops. Two scheduled sweeps per hour can spend up to 500 calls in total.

## Alternatives rejected

- **a — `workflow_run` caller**: needs a dated dangerous-triggers exception against the ADR-0098 rendered-caller zizmor contract.
- **c — sweep only**: ≈1,368 calls/hour at 10-minute interval with 100 open pulls (exceeds 1,000 budget).

## Revisit when

- Pinned actionlint accepts `concurrency.queue`.
- Measured evidence that a shared concurrency group leaves a PR `CLEAN` with a lone cancelled tail check run.
- Measured token use near the repository budget cap.

## Consequences

Consumers render tails from gating templates and the sweep from [required-checks.yml.tmpl](../../templates/callers/required-checks.yml.tmpl) into `.github/workflows/required-checks-caller.yml`. Interface stays at `interface_minor` **0.3** in [interface.lock.json](../../interface.lock.json).
