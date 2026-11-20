<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0098: The release fan-out runs in its own workflow run

- **Status**: Accepted (amends [ADR-0031](0031-the-merge-train-app-runs-release-please.md), [ADR-0045](0045-the-release-train-is-opt-out-and-bounded.md) and [ADR-0096](0096-the-release-labels-module-leaves-the-release-step.md))
- **Date**: 2026-11-20
- **Authors**: Lusoris

## Context

The train's caller holds a workflow concurrency group until every job in its run ends. A release matrix in that run therefore holds the group while waiting for runners. In the measured fleet incident on 2026-11-20, the train job in run 44701216583 ended at 08:24:35Z; three release legs still waited at 08:33Z because their runner pods lacked CPU and memory. The scheduled 08:28Z train run 44702786399 waited behind that group. Earlier runs held it for 41–42 minutes. The regression reproduces this topology and the stalled train in [test_train_release_run.py](../../tests/test_train_release_run.py), using the bounded scheduler in [train_timeline.py](../../tests/train_timeline.py).

The scheduler reads rendered callers and reusable jobs, dependencies, runner expressions and concurrency groups. It models case-insensitive groups, one running and one pending entrant, replacement of the older pending entrant, running cancellation when requested, and workflow completion after all jobs end. Its six scheduled cycles use the template cron, eight-minute train jobs and unavailable release runners; its race scenarios use release runs ten minutes apart with fifteen-minute legs. These are explicit scenario bounds, not predictions of forge timing ([train_timeline.py](../../tests/train_timeline.py), [test_train_release_run.py](../../tests/test_train_release_run.py)).

## Decision

[merge-train.yml](../../.github/workflows/merge-train.yml) ends with the train job. Its sweep writes the same bounded release list to `GITHUB_OUTPUT` and, when `TRAIN_RELEASE_FILE` is set, a JSON file containing exactly `format: 1`, the original `TRAIN_HOLD_LABELS` string and `releases`. There is one list computation and one artifact writer; an unset path writes no file ([merge_train.py](../../tools/merge_train.py), `test_summary_release_file_matches_output` in [test_merge_train.py](../../tests/test_merge_train.py)). The sweep retains `release_off`. The train job keeps empty permissions and drops its job outputs. An upload step publishes `merge-train-releases` for one day, with missing files treated as errors, only when the run is not cancelled, the switch is on and the sweep produced a release output ([test_train_release_run.py](../../tests/test_train_release_run.py)).

The new [release caller](../../templates/callers/merge-train-release.yml.tmpl) starts on completion of the workflow named `Merge train`. It deliberately has no workflow concurrency group. It grants only `actions: read` to its reusable call, so the plan can download the completed train run's artifact. The new [merge-train-release.yml](../../.github/workflows/merge-train-release.yml) declares required `runner` and `app_client_id` inputs and `app_private_key` secret, with numeric `release_max_parallel` defaulting to 3. It accepts no switch or hold-label input; the artifact carries the train's hold labels. The five-minute plan job checks out its own commit's modules without persisted credentials, downloads the named artifact pattern from the triggering run into a fixed runner-temporary path and publishes `releases` and `hold_labels` outputs ([test_train_release_run.py](../../tests/test_train_release_run.py)). A missing artifact pattern succeeds, allowing the validator to report the absent file.

The `plan` command in [release_labels.py](../../tools/release_labels.py) validates before any release leg can mint an app token. [test_release_labels.py](../../tests/test_release_labels.py) covers these rules:

- A conclusion outside `success` and `failure` produces a notice naming it, empty releases and empty hold labels, with exit 0. Failed sweeps may still supply a release list.
- Only `schedule` and `workflow_dispatch` events qualify. The triggering head branch must equal the default branch, and the triggering repository must equal the caller repository ignoring case. Each refusal emits an error and exits 1.
- Parallelism must be a whole number from 1 through 3. Invalid values emit an error and exit 3. The validation has moved out of the train's settings.
- An absent file produces the notice “train run uploaded no release list (switch off, or the sweep ended before its summary)”, empty releases and hold labels, and exit 0.
- A present file must be a JSON object with exactly `format`, `hold_labels` and `releases`. The format is integer 1. Hold labels are a string of at most 4096 characters without a newline or carriage return.
- Releases are a list of at most 256 objects, each containing exactly `owner` and `name`. The shared `MAX_RELEASES` definition lives in [release_workflows.py](../../tools/release_workflows.py) and also bounds the sweep writer. Owners start with an ASCII letter or digit and contain at most 39 letters, digits or hyphens. Names contain 1–100 ASCII letters, digits, dots, underscores or hyphens and cannot be `.` or `..`. Duplicate owner/name pairs are refused ignoring case.
- Invalid shape, malformed JSON and read failures emit an error and exit 1. Reading is bounded to one MiB of characters. An empty valid list produces a notice that no repository is named and exits 0. Valid plans write compact JSON `releases` and the original single-line `hold_labels` to `GITHUB_OUTPUT`.

The release job needs the plan and runs only when not cancelled and its release output is neither empty nor `[]`. It retains the `merge-train` environment, empty permissions, fifteen-minute timeout, fail-fast disabled, bounded matrix parallelism and the existing release steps. The settings step receives the plan's hold labels. Every leg's concurrency group includes the app client id and repository owner/name, independent of run id ([merge-train-release.yml](../../.github/workflows/merge-train-release.yml), [test_merge_train.py](../../tests/test_merge_train.py)).

## Consequences and migration

Queued release runners no longer hold the train caller's workflow group. Different repositories may release concurrently; the same repository has one leg at a time across runs. A newer pending leg replaces the older pending leg, which ends cancelled. The running leg is never cancelled by this policy. Release-please reads the default branch afresh, so the newer pending leg covers the replaced one. Case-insensitive group and pending-replacement scenarios, the old-layout stall and five planted concurrency defects are replayed in [test_train_release_run.py](../../tests/test_train_release_run.py).

Consumers must add the [release caller](../../templates/callers/merge-train-release.yml.tmpl) on their default branch and drop `release_runner` and `release_max_parallel` from the train call. Configure the release caller's `runner` and optional `release_max_parallel` there instead. The train and release callers may use different runner sets ([test_runner_labels.py](../../tests/test_runner_labels.py)). Custom train workflow names require a matching `workflow_run.workflows` entry. Without the release caller, the train still sweeps and uploads its artifact but no release fan-out starts. The input removals declare an interface break at version 0.3 in [interface.lock.json](../../interface.lock.json); [test_interface.py](../../tests/test_interface.py) verifies the version rule.

Artifact retention is one day. A release plan that starts after its artifact expires reports no list and schedules no releases; a later train sweep discovers eligible repositories again. The separate run has its own status and cancellation lifecycle. Train switch-off prevents upload rather than cancelling already-started release runs ([merge-train.yml](../../.github/workflows/merge-train.yml), [test_train_release_run.py](../../tests/test_train_release_run.py)).
