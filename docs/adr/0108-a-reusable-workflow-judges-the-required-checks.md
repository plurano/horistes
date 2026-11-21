<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0108: A reusable workflow judges the required checks

- **Status**: Accepted
- **Date**: 2026-11-21
- **Authors**: Lusoris

## Context

Branch protection and repository rulesets across an organisation otherwise list every required check context directly or maintain a separate aggregator workflow in each repository. Direct ruleset lists deadlock whenever a path-filtered workflow does not run or when pull-request-only gates do not exist on default-branch pushes and merge-group commits. Per-repository copies drift as bug fixes land in one repository and not in others.

An earlier single-repository aggregator established six hard invariants from production incidents: every owed context must report `success` rather than passing when absent, `skipped`, `neutral` or `cancelled`; each check run is admitted only from its expected GitHub App slug (`github-actions` by default) so another app cannot satisfy or mask the same name; check suites created by workflow runs from another event or another branch on the same commit SHA (such as a scheduled run or a cancelled pull-request run after a fast-forward push) are excluded; commit statuses on the SHA count alongside check runs by their newest state per context; transient HTTP 5xx forge outages are waited out for up to 15 minutes before failing as `Transient GitHub API failure:`; and primary or secondary HTTP 403/429 rate limits are waited out using `retry-after`, `x-ratelimit-reset` plus 5 seconds, or exponential backoff while polling adapts from 30 seconds up to 240 seconds.

`plurano/horistes` keeps reusable workflows as small YAML shells over standard-library Python modules checked out from the workflow's own commit and self-tested before judging the caller ([ADR-0074](0074-the-commit-message-judge-runs-on-the-host-python.md), [ADR-0095](0095-pull-requests-check-the-full-sign-off-ident.md)). Its forge transport already lives in [forge.py](../../tools/forge.py), where rate-limit detection previously inspected `gh api` error text without HTTP headers.

## Decision

[required-checks.yml](../../.github/workflows/required-checks.yml) provides a single reusable `workflow_call` job named `Required checks` (reported by the forge as `<calling job> / Required checks`), paired with the consumer caller template [required-checks.yml.tmpl](../../templates/callers/required-checks.yml.tmpl) whose `result` job reports `Required checks` ([ADR-0030](0030-a-result-job-reports-the-calling-jobs-name.md)). Top-level permissions are `{}`; the job asks for `contents: read`, `checks: read`, `statuses: read` and `actions: read` and nothing else. On a draft pull request the job stops before checkout. Otherwise it sparse-checks out [required_checks.py](../../tools/required_checks.py), [forge.py](../../tools/forge.py) and [results.py](../../tools/results.py) from `job.workflow_repository` at `job.workflow_sha` without persisted credentials, runs `python3 -I -B required-checks-source/tools/required_checks.py self-test`, and then runs `judge` ([test_required_checks.py](../../tests/test_required_checks.py)).

The workflow declares seven inputs: `runner` (string, default `ubuntu-26.04`), `required` (required string, non-empty JSON list of unique context names), `pull-request-only` (string, default `"[]"`), `not-owed` (string, default `"[]"`), `apps` (string, default `"{}"`, mapping context name to expected GitHub App slug), `wait-minutes` (number, default `60`, whole minutes `1`–`240`), and `rate-limit-wait-minutes` (number, default `30`, whole minutes `0`–`90`). Because GitHub Actions expressions provide no arithmetic `+` operator, `timeout-minutes` maps `inputs.wait-minutes` (`1`–`240`) to `wait-minutes + 10` (`11`–`250`) through a `fromJSON` lookup table with fallback `10` for out-of-range inputs, which `parse_config` then refuses before polling. Malformed JSON, `NaN` or `Infinity` constants, an empty or duplicate `required` list, any name in `pull-request-only`, `not-owed` or `apps` absent from `required`, an invalid app slug, or an out-of-range minute value exits with code 2 and a named error before any API call ([required_checks.py](../../tools/required_checks.py), [test_required_checks.py](../../tests/test_required_checks.py)).

[forge.py](../../tools/forge.py) gains `ResponseError`, `request` (invoking `gh api --include` with the existing 60-second deadline and parsing HTTP status lines and lowercase response headers), `is_rate_limit`, `rate_limit_delay`, and optional `max_pages` and `fetch` parameters on `paged` while keeping `download`, `api`, `paged` defaults and `collect` unchanged for existing callers ([test_forge.py](../../tests/test_forge.py), [test_required_checks.py](../../tests/test_required_checks.py)). One `gh api` runner and one pagination loop serve both readers.

[required_checks.py](../../tools/required_checks.py) enforces the base aggregator semantics with injectable clock, sleep and transport functions:

- Every owed context (in `required`, not in `not-owed`, and not in `pull-request-only` outside `pull_request` and `pull_request_target` events) must settle with `success`: a completed check run from its expected app with conclusion `success`, or—for contexts not pinned in `apps`—a newest commit status in state `success` (and when both exist, neither failing nor pending). Absent, `skipped`, `neutral`, `cancelled`, `timed_out`, `action_required`, `failure`, `stale`, `startup_failure`, active check runs at the deadline, and `pending`, `failure` or `error` statuses all fail. Exempt contexts (`not-owed`, or `pull-request-only` outside `pull_request` and `pull_request_target`) pass when absent, `skipped`, pending or `success`, and still fail if a completed check run or settled commit status reported a failure.
- Check runs are filtered to the caller's expected app slug (`apps[name]`, defaulting to `github-actions`); runs from any other app or missing `app.slug` do not count and are logged. Check runs whose `check_suite.id` belongs to a workflow run on the same SHA from a foreign event (such as `schedule`, `pull_request` during a `push` or `merge_group` run, or `push` on another branch) are excluded via `/repos/{owner}/{repo}/actions/runs?head_sha={sha}`. Within the remaining runs, the newest check run per name wins by timestamp and numeric id; commit statuses select the newest status per context by timestamp and numeric id.
- API HTTP 5xx responses retry with delays `30, 60, 120, 240, 240, 210` seconds (15 minutes total) and then fail with `Transient GitHub API failure:` without judging checks. HTTP 403 or 429 responses carrying `retry-after`, `x-ratelimit-remaining: 0`, or a `rate limit` message wait `retry-after` seconds, `max(0, reset - floor(now)) + 5` seconds, or `60` seconds doubling up to `960` seconds; at most 6 waits per call are permitted, cumulative waits stay within `rate-limit-wait-minutes`, and no wait may cross the `wait-minutes` deadline (`<=` allowed, `>` refused). Exceeding any bound fails with `GitHub API rate limit:` without judging checks. HTTP 403 without a rate-limit signal fails immediately. When a poll pass waits on a rate limit after its initial check-run read, check runs are listed once more before judging so checks registered during the wait are included.
- Polling starts at 30 seconds, doubles up to 240 seconds while the judged set's statuses and conclusions are unchanged, resets to 30 seconds on any change, and clamps sleeps to the remaining deadline. Every loop carries a scalar upper bound.

## Alternatives

- Embedding JavaScript inside `actions/github-script` as in the earlier single-repository aggregator: rejected because large inline scripts hit GitHub Actions expression and field limits, duplicate transport logic outside [forge.py](../../tools/forge.py), and cannot be unit-tested and coverage-gated alongside the Python tools in `tools/`.
- Adding a preliminary setup job to compute `wait-minutes + 10` as a job output: rejected because it consumes a second runner queue slot on every invocation and adds a second check-run name to the reusable workflow's recorded interface in [interface.lock.json](../../interface.lock.json), whereas `fromJSON` array indexing computes `wait-minutes + 10` directly in `timeout-minutes`.
- Enforcing a fixed two-minute registration cutoff on absent owed checks: rejected because every Horistes consumer caller reports its required context from a `result` job with `needs:` on the reusable call ([ADR-0030](0030-a-result-job-reports-the-calling-jobs-name.md)), which GitHub registers only after the needed job finishes. Polling up to `wait-minutes` until all owed contexts register and complete handles `needs:` chains without per-consumer dependency tables.

## Consequences and migration

Consumers render [required-checks.yml.tmpl](../../templates/callers/required-checks.yml.tmpl) into `.github/workflows/required-checks.yml`, list every merge-gating context in `required`, configure `pull-request-only`, `not-owed` and `apps` as needed, and switch their ruleset to `Required checks` only after a side-by-side verdict trial ([ADR-0004](0004-same-verdict-before-a-switch.md)). Adding this new workflow and template is backwards-compatible and leaves `interface_major` and `interface_minor` at `0.3` in [interface.lock.json](../../interface.lock.json).

Five concerns from the earlier single-repository aggregator remain outside this workflow as caller duties or later work:

- **Tier computation**: callers that vary owed checks by event or pull-request tier compute that decision in the caller and pass excluded names in `not-owed`.
- **Hardware lane switches**: callers with optional self-hosted hardware lanes evaluate their repository variables or runner probes in the caller and pass disabled lanes in `not-owed`.
- **Edited pull-request window anchoring**: this workflow does not inspect timeline `ready_for_review` timestamps or edit-stamped `display_title` prefixes; callers that re-run partial gates on `edited` pull-request events scope those triggers in the caller or leave edit-stamp correlation to later work.
- **Delayed registration dependencies**: no `needs:` dependency map is passed; polling waits up to `wait-minutes` for absent owed contexts to register, so a broken workflow that never creates an owed context fails at `wait-minutes` rather than after a short grace window unless dependency declarations are added later.
- **External upload jobs**: no SARIF upload-job propagation map is declared; where an external code-scanning app already posted an older check run on an unchanged SHA before a new upload finishes, coordinating propagation remains a caller duty or later work.

## References

- Credit: semantics adapted from an earlier single-repository aggregator (owed contexts must succeed, producer app pinning, foreign-event suite exclusion, 15-minute 5xx outage wait, bounded primary/secondary rate-limit wait, check-run re-read after a wait, and adaptive 30–240 s polling).
- [ADR-0004: A consumer switches only after the same verdict is shown](0004-same-verdict-before-a-switch.md)
- [ADR-0030: A result job reports the calling job's name](0030-a-result-job-reports-the-calling-jobs-name.md)
- [ADR-0074: The commit-message judge runs on the host Python](0074-the-commit-message-judge-runs-on-the-host-python.md)
- [ADR-0095: Pull requests check the full sign-off ident](0095-pull-requests-check-the-full-sign-off-ident.md)
- [ADR-0098: The release fan-out runs in its own workflow run](0098-the-release-fan-out-runs-in-its-own-workflow-run.md)
- GitHub Docs, "REST API endpoints for check runs", "REST API endpoints for commit statuses", "REST API endpoints for workflow runs", and "Rate limits for the REST API" (`X-GitHub-Api-Version: 2022-11-28`).
