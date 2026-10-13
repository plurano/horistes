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

- **One sweep.** `.github/workflows/merge-train.yml` is a reusable workflow
  that one caller in the fleet's operations repository runs. Each run sweeps
  every repository the app is installed on, except archived ones and
  suspended installations. In each repository it writes at most once:
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
  A bad value, an empty client id or a key that is no private key stops the
  run with exit code 3. A forge refusal in one repository is reported with the
  forge's message and the sweep goes on; the run ends with exit code 1 when
  any repository refused. The input `switch: off` turns a run into a report
  and its tokens carry read rights only. Repositories not reached within 25
  minutes are named in the summary.
- **The app.**
  - The rights are in `apps/merge-train.json`, and a test holds them equal to
    what the train asks the forge for: `contents: write` (merge),
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
  - The key signs a JWT of the app with openssl; it reaches openssl through a
    pipe and is never written to a file. With the JWT the job lists the
    installations, mints a metadata-read token per installation to list its
    repositories, and mints one token per repository, limited to that
    repository. Each token is revoked when its repository is done.
  - GITHUB_TOKEN gets no scope.
- **Where the key lives.** The key is stored once, as the secret
  `MERGE_TRAIN_APP_KEY` of the environment `merge-train` in the operations
  repository lusoris/k8s. The environment lets only the branch main deploy, so
  a workflow changed on another branch never reads the key, and the key is no
  repository secret. The client id is the repository variable
  `MERGE_TRAIN_APP_CLIENT_ID`. The repositories the train serves hold no
  secret and keep no caller.
- **The caller.** `templates/callers/merge-train.yml.tmpl` is the one caller,
  kept in the operations repository. It runs the sweep every 20 minutes and
  by hand, on one of the fleet's own runners (`arc-cauda` in lusoris/k8s); the
  fleet spends no hosted runner minutes on it. The called job names the
  environment; the caller passes the key by name, because a called workflow
  receives an environment secret only when the caller passes it and gets an
  empty string otherwise. The script refuses an empty key.
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
  - A repository joins the train when the app is installed on it, with no
    file and no secret of its own.
- **Negative**:
  - Registering the app, installing it on each account and storing its key
    are operator acts.
  - `workflows: write` lets the token change workflow files. The train itself
    only merges and updates pull requests the forge reports CLEAN or BEHIND,
    and each token covers one repository and is revoked when it is done.
  - The key is one secret for every account of the fleet. It sits in one
    environment that only main may use, and the cluster session keeps the
    caller; whoever can push to main of lusoris/k8s can run the train.
  - A run moves one pull request per repository, so a pull request that is
    BEHIND lands in the second run after its checks pass, about 40 minutes.
- **Neutral**: only the 100 oldest open pull requests of a repository are
  read. The summary says when more are open. The old train of lusoris/k8s is
  retired after the first sweep summary lists all nine accounts.

## References

- Fleet order merge-train-fleet-app (G65), 2026-10-13 (paraphrased): prepare
  the app manifest with computed rights, a hand-over for the one operator act,
  and a reusable train templated on the k8s train. It keeps trains running
  while sessions idle, updates BEHIND by merge, respects the minimum age and
  merges only at CLEAN, with no bypass and a planted red pull request it must
  not merge.
- Fleet answers k8s-merge-train-credential (2026-10-11), fleet-update-min-age,
  fleet-merge-only-clean, g65-release-please-consumer, g65-repin-consumer and
  g65-app-owner, g65-key-store, g65-app-name and g65-ops-repo (2026-10-13),
  all paraphrased above. g65-app-owner chose one public app for all nine
  accounts and added `workflows: write` to the rights. g65-key-store chose one
  stored key and one scheduled sweep on a cluster runner, with no secret and
  no caller in the consumers, because hosted runner minutes are not paid for.
  g65-app-name named the app Plurano Merge Train. g65-ops-repo placed the key
  in the environment `merge-train` of lusoris/k8s, deployable from main only.
- Forge documentation read 2026-10-13: "Permissions required for GitHub Apps"
  (labels and comments under Issues or Pull requests, update-branch under Pull
  requests, merge under Contents, releases under Contents and Workflows);
  "Registering a GitHub App from a manifest"; "Making a GitHub App public or
  private".
- Forge documentation read 2026-10-13: "Reuse workflows", section on inputs
  and secrets: the caller must pass an environment secret by name; the job
  that sets `environment` receives the environment's value; an unpassed
  secret is an empty string with no error. REST endpoints "Create an
  installation access token for an app" (`permissions`, `repositories`),
  "List installations for the authenticated app", "List repositories
  accessible to the app installation" and "Revoke an installation access
  token".
- `tests/test_merge_train.py` runs the step's script against a stand-in forge.
  The planted red, blocked pull request is not landed, and a mutant that lands
  whatever it reads lands it. Ten more mutants are each caught: red checks
  ignored, no in-flight hold, no minimum age, the newest commit as the age,
  release branches landed, forks landed, drafts landed, two landings per run,
  a conflict stopping the run, no second read of UNKNOWN. The stand-in forge
  checks the app's JWT against a key made for the test run and refuses a
  token that is not limited to one repository. Three sweep mutants are
  caught: no revocation, no limit to one repository, and a refusal stopping
  the sweep. A key of another app is refused, an empty key stops the run, and
  a key written to a file is found by the script check.
- `tests/test_render.py`: the planted secret cases fail against `tools/render.py`
  of main.
