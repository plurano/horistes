<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0108: A reusable workflow judges the required checks

- **Status**: Accepted
- **Date**: 2026-11-21 (amended 2026-11-22, 2026-11-23 and 2026-11-24, decision **b**)
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
- **Proposal checks** remain owed on `pull_request`, `pull_request_target` and `merge_group` (`exempt_reason`). The [licence tail](../../.github/workflows/licence-check.yml) and [sweep caller](../../.github/workflows/required-checks-caller.yml) mark `Sign-off` proposal-only because the [Sign-off caller](../../.github/workflows/sign-off-check.yml) runs on pull requests and merge groups. Rulesets require `required-checks`, the proposal policy. Push-class judgments use `required-checks (push)`. Both judge contexts are excluded from the evidence.
- **Check-run admission** requests `filter=all` and selects the newest eligible run per exact name. Runs follow one total order, ranking stamped runs by timestamp then ID and unstamped runs above stamped ones by ID because GitHub leaves `started_at` empty only before a run begins (`test_check_run_total_order_selects_unstamped_across_permutations`, `test_check_run_equal_stamps_compare_by_id`). When the selected run has no timestamp, it counts as pending regardless of its `status` or `conclusion` fields so a completed run missing timestamps fails closed (`test_unstamped_completed_check_run_counts_as_pending`). Commit-status contexts are case-insensitive. Actions checks require a suite in the workflow-run listing after event, branch, and applicable self-workflow exclusions. External checks use the configured app slug. Ten full workflow-run pages exhaust the listing budget and stop judgment (`read_snapshot`, `_read_workflow_runs`).
- **Waiting and failure** use the earliest eligible non-self workflow timestamp, with the commit date as fallback. A terminal failure in either record takes precedence over a pending peer (`_workflow_anchor`, `decide`).
- **Status writes** require a changed verdict. A pending verdict can replace a success or failure (`_should_post`). Checks without a tail are settled by the sweep within one cron interval when their head fits in the sweep budget.
- **Freshness** requires a write within `POST_FRESH_SECONDS` (10) of the snapshot read starting. Before a write, `_status_preflight` reads up to 20 status pages in order, stopping at the first page with a judge status or a short page. A judge status that has moved, a change in observed required status evidence, or reaching the page bound without finding the judge status triggers a fresh snapshot. A second stale read emits `::warning::` and exits 0. After an accepted `success` POST, the judge reserves a withdrawal before decoding the response and verifies the created status ID against page 1 (`test_success_post_non_json_body_withdraws_and_reports_decode_error`). A different status in the judge context created at or after the second in which the snapshot began, or a full page missing the created status ID, triggers one fresh judgment. If the judge cannot confirm a contested success, it spends a reserved call to replace it with pending and reports the warning or error that stopped judgment. If the withdrawal fails, the judge emits `::error::` with the SHA and exits 1. Because commit statuses offer no compare-and-set and external locking would require `contents: write`, which the permission grants omit, the judge bounds rather than removes the window in which a contested `success` is visible between its POST and the verification read plus withdrawal POST.
- **Write recovery** makes one POST attempt per evaluation. A rate limit or 5xx allows one additional read-decide-post cycle after the bounded wait. Other write errors and a second failed POST emit `::error::` and exit 1 (`ApiSession.request_json`, `_evaluate_one`). Sessions enforce an 8-minute deadline (`SESSION_DEADLINE_SECONDS`), stopping ordinary reads, verdict writes, retries, waits, and sweep target starts 30 s before it (`WITHDRAWAL_RESERVE_SECONDS`) while keeping one bounded withdrawal attempt up to the session deadline (`test_withdrawal_uses_reserved_deadline_window_after_work_cutoff`, `test_withdrawal_refused_past_session_deadline_reports_risk`, `test_work_cutoff_boundary_allows_one_second_before_and_refuses_at_cutoff`).
- **Sweep ordering** starts with never-judged and unknown pull heads in pull order, then waiting heads, failures, and successes, oldest first within each group. When the list exceeds `max_targets`, `candidates` is split into `blocks = ceil(len(candidates) / max_targets)` balanced blocks of size `s <= max_targets`, selecting block `(run_number - 1) % blocks` and rotating its start by `((run_number - 1) // blocks) % s`. When `s < max_targets`, the rotated block comes first and the sweep fills the remaining `max_targets - s` slots with the candidates following that block, wrapping around, so any cap or deadline cuts fill heads before block members (`test_sweep_fills_over_budget_blocks_to_max_targets`, `test_sweep_cap_cuts_fill_before_block_members`). With a stable list and consecutive caller workflow run numbers, every head is judged within `blocks` sweeps when no cap or deadline cuts a sweep, and within `(s - k + 1) * blocks` sweeps when each sweep judges at least `k` heads of its block of size `s`. After triage, the judge evaluates the default head with its branch ref before evaluating the selected pull heads. After an evaluation error, the sweep continues judging the remaining heads within its deadline and API budget, then exits 1. Each evaluation reads statuses afresh and deduplicates an unchanged verdict (`_sweep_evaluation_order`, `_sweep_default_branch`).
- **Sweep cap** counts every transport attempt against the 250-call session budget, including retries and failures. Triage reads the first status page for up to 100 pull heads and stops at 120 attempts. Heads with no judge status on page one or no completed triage read are treated as never judged. A normal POST reserves two more calls, one for verification and one for a possible withdrawal (`ApiSession.request_json`). Before each pull head, reaching the cap stops the sweep and warns with the remaining selected heads, counting a head whose evaluation the cap interrupted as not judged while excluding a head that finished on the cap (`test_sweep_cap_counts_unjudged_when_exhausted_by_default_branch`, `test_sweep_cap_counts_interrupted_head_as_unjudged`, `test_sweep_cap_excludes_head_finishing_at_exact_cap`).

Render context `[RequiredChecks]` (Required, PullRequestOnly, NotOwed, Apps) is the single source for tail and sweep template inputs. When `PullRequestOnly` is omitted, the renderer includes `Sign-off` if it is required ([tools/render.py](../../tools/render.py)).

## Budget

The steady-state rows use one page per listing and no retries or freshness refresh. A complete snapshot reads statuses, checks and workflow runs, plus the commit if no eligible timestamp is available. A write adds the status preflight, one POST, and the verification read. A contested success may require one withdrawal POST. Sweep setup adds the open-pull listing, one status page per triaged head, and two default-branch requests. The sweep fixtures exercise commit-date fallback. See `test_sweep_call_count_at_fourteen_open_pulls`, `test_sweep_capless_call_counts` and `test_sweep_transport_call_counts_measured_at_defaults` in [tests/test_required_checks.py](../../tests/test_required_checks.py).

| Scenario | API calls |
| :--- | ---: |
| Tail, eligible workflow timestamp | 3 without a write, 6 with a write |
| Tail, commit-date fallback | 4 without a write, 7 with a write |
| One-page sweep, 14 open heads + default | 122 |
| One-page sweep, 100 open heads + default, cap disabled in test | 320 |
| One-page sweep, 100 open heads, cap enforced | 250 |
| Page-bound sweep, cap disabled in test | 1,777 |
| Page-bound sweep, cap enforced | 250 |

At 100 open heads, the capped one-page fixture posts for the default head and 19 pull heads. The capped page-bound fixture posts for the default head and one pull head. Two scheduled sweeps per hour can spend up to 500 calls in total.

## Alternatives rejected

- **a — `workflow_run` caller**: needs a dated dangerous-triggers exception against the ADR-0098 rendered-caller zizmor contract.
- **c — sweep only**: ≈1,368 calls/hour at 10-minute interval with 100 open pulls (exceeds 1,000 budget).

## Revisit when

- Pinned actionlint accepts `concurrency.queue`.
- Measured evidence that a shared concurrency group leaves a PR `CLEAN` with a lone cancelled tail check run.
- Measured token use near the repository budget cap.

## Consequences

Consumers render tails from gating templates and the sweep from [required-checks.yml.tmpl](../../templates/callers/required-checks.yml.tmpl) into `.github/workflows/required-checks-caller.yml`. Interface stays at `interface_minor` **0.3** in [interface.lock.json](../../interface.lock.json).
