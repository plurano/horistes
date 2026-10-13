<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0029: A forge app drives the merge train, with no bypass

- **Status**: Accepted
- **Date**: 2026-10-13
- **Authors**: Lusoris

## Context

Pull requests in the fleet's repositories wait at CLEAN or BEHIND until a
session lands or updates them. When the sessions are idle or held by a usage
limit, the trains stop. The cluster repository lusoris/k8s has a train of its
own (`.github/workflows/merge-train.yml` with `tools/merge-train/warden.py`).
It writes with a personal access token, and it is switched off. The fleet
decided on 2026-10-11 to replace that token with a forge app whose token is
minted per run.

A workflow cannot do this with GITHUB_TOKEN: events that token causes start no
workflow run. A head branch it updated would wait for checks that never
start, and a merge it made would start no push workflow on the default branch.
Release pull requests opened with GITHUB_TOKEN stay BLOCKED for the same
reason; one in cordanaLLM/Aegis-OS has waited since 2026-09-27.

The k8s warden measured how the forge behaves:

- It offers auto-merge only while a pull request cannot merge at once, so a
  CLEAN pull request cannot be armed.
- An update-branch call answers 422 with "There are no new commits on the base
  branch." when there is nothing to do, and with a merge conflict message when
  the update conflicts.
- Merge states read UNKNOWN for a short time after a push to the default
  branch.

## Decision

- **Reusable workflow.** `.github/workflows/merge-train.yml` is a reusable
  workflow. Each run writes at most once:
  - It lands the oldest pull request the forge reports CLEAN (or a state the
    caller accepts in `accept_states`). It uses the merge mutation with the
    head commit it read, so a newer push cannot be merged unseen.
  - If none is CLEAN and no armed pull request is still in flight, it arms
    auto-merge on the oldest BEHIND pull request whose checks are not red. It
    then merges the default branch into that pull request through update-branch.
    It never rebases: a forge rebase re-commits the head unsigned.
  - An armed pull request that is red does not hold the train.
- **Arming at BEHIND, not at CLEAN.** The order asked for arming at CLEAN. The
  forge refuses that, so a CLEAN pull request is landed directly instead.
  Safety rests on the forge's own CLEAN verdict and on an app with no bypass:
  the merge call fails for a red or blocked pull request.
- **Left alone:**
  - drafts;
  - pull requests from forks;
  - bases other than the default branch;
  - the labels in `hold_labels` (by default `hold`, `do-not-merge` and
    `planted`);
  - heads in `person_branches` (by default `release-please--`, because a release
    is a person's act);
  - dependency updates whose own commit is younger than `min_age_days`. The
    default is 3 days, the fleet's minimum age. The own commit is the oldest
    commit that is no merge, so a repin or a merge pushed on top does not
    restart the wait. A security label or "[SECURITY]" in the title skips it.
- **Settings.** Every input is checked against an allow-list before any call.
  A bad value stops the run with exit code 3. A forge refusal stops it with
  exit code 1 and the forge's message. The input `switch: off` turns a run
  into a report.
- **The app.**
  - The rights are in `apps/merge-train.json`, and a test holds them equal to
    what the token step asks for: `contents: write` (merge),
    `pull_requests: write` (update-branch, auto-merge, reading pull requests),
    `workflows: write`, `checks: read` (check suite conclusions) and
    `metadata: read`. The forge refuses a merge or an update-branch that
    changes a file under `.github/workflows` to a token without
    `workflows: write`. Pin updates change those files, and so does an update
    of a BEHIND pull request after the default branch changed a workflow.
  - The app is one public app owned by plurano and installed on every account
    of the fleet. A private app can be installed only on the account that owns
    it, and the fleet's pull requests are spread over nine accounts. Public
    means that anyone can install it on their own repositories; it gets no
    access to the fleet's.
  - The app has no webhook, subscribes to no events and asks no user to
    authorise it.
  - The token is minted for the calling repository only and revoked when the
    job ends.
  - GITHUB_TOKEN gets no scope.
- **The caller.** `templates/callers/merge-train.yml.tmpl` runs the train on a
  push to the default branch, every hour at minute 41, and by hand. It passes
  the app's client id from the variable `MERGE_TRAIN_APP_CLIENT_ID` and its key
  from the secret `MERGE_TRAIN_APP_KEY`.
- **Secrets in callers.** `tools/render.py` accepts a secret in a caller only
  when the called workflow declares it, passed by name as one secret of the
  repository. `secrets: inherit` and a secret written out are refused.

## Consequences

- **Positive**:
  - Trains move while no session runs, and each landing starts the checks and
    push workflows a person's merge would start.
  - Nothing lands that the forge does not call mergeable, and no write
    bypasses a rule set.
  - Release pull requests and pushed repins can use the same app with the same
    rights, so their checks start too.
- **Negative**:
  - Registering the app, installing it on each account and storing its key
    are operator acts.
  - `workflows: write` lets the token change workflow files. The train itself
    only merges and updates pull requests the forge reports CLEAN or BEHIND,
    and the token lives for one job in one repository.
  - The hourly schedule costs runner minutes in private repositories.
- **Neutral**: only the 100 oldest open pull requests are read. The summary
  says when more are open.

## References

- Fleet order merge-train-fleet-app (G65), 2026-10-13 (paraphrased): prepare
  the app manifest with computed rights, a hand-over for the one operator act,
  and a reusable train templated on the k8s train. It keeps trains running
  while sessions idle, updates BEHIND by merge, respects the minimum age and
  merges only at CLEAN, with no bypass and a planted red pull request it must
  not merge.
- Fleet answers k8s-merge-train-credential (2026-10-11), fleet-update-min-age,
  fleet-merge-only-clean, g65-release-please-consumer, g65-repin-consumer and
  g65-app-owner (2026-10-13), all paraphrased above. g65-app-owner chose one
  public app for all nine accounts and added `workflows: write` to the rights.
- Forge documentation read 2026-10-13: "Permissions required for GitHub Apps"
  (labels and comments under Issues or Pull requests, update-branch under Pull
  requests, merge under Contents, releases under Contents and Workflows);
  "Registering a GitHub App from a manifest"; "Making a GitHub App public or
  private".
- `actions/create-github-app-token` v3.2.0 README: without `owner` and
  `repositories`, the token covers the current repository only. v3.3.0 came
  out on 2026-10-12; it reaches the workflow through the update bot once it is
  past the fleet's minimum age.
- `tests/test_merge_train.py` runs the step's script against a stand-in forge.
  The planted red, blocked pull request is not landed, and a mutant that lands
  whatever it reads lands it. Ten more mutants are each caught: red checks
  ignored, no in-flight hold, no minimum age, the newest commit as the age,
  release branches landed, forks landed, drafts landed, two landings per run,
  a conflict stopping the run, no second read of UNKNOWN.
- `tests/test_render.py`: the planted secret cases fail against `tools/render.py`
  of main.
