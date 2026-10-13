# Horistes

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/plurano/horistes/blob/HEAD/AGENTS.md
<!-- praetor:readme-governance:end -->

Reusable workflows and templates for the repositories of the Plurano framework
family: the jobs
every repository runs (build, test, lint, security scans, release, landing) are
kept once here and called from thin callers, instead of each repository keeping
its own copy.

## Name

Greek *horistes*, the boundary commissioner who fixes limits. Pronounced
ho-RIS-tees.

The repository is `plurano/horistes`. The name is final with the first
release: callers and preset references carry it, and the forge does not
redirect a call after a rename
([ADR-0023](docs/adr/0023-the-name-is-horistes.md)). Records written before
2026-10-12 keep the working name `shared-ci`.

## Status

Private; nothing is released yet. The repository has its own gate and the
checks every workflow added here must pass. The first reusable workflow is the
licence check; this repository is its first caller and no longer keeps a
licence job of its own. The second is the scorecard run; it has no caller
yet, because a repository can call it only once this one is public. The caller
a consumer keeps exists as a template for both.

Consumers call this repository directly; no copy per owner is built. Its name
is chosen. It turns public now that its history is shown to hold no private
content; the first release follows later. Until then only private repositories
of the same owner can call it
([ADR-0022](docs/adr/0022-public-consumers-call-this-repository-directly.md),
[ADR-0024](docs/adr/0024-public-before-the-first-release.md)).

## Workflows

| File | Purpose | Inputs | Check | Hosts beyond the forge |
| :--- | :--- | :--- | :--- | :--- |
| `.github/workflows/licence.yml` | every file carries its licence information (`reuse lint`) | `runner` (label, default `ubuntu-26.04`, the newest working image); `root` (directory checked as the project root, default `.`) | `<calling job> / REUSE lint`; with another `root`, `<calling job> / REUSE lint (<root>)` ([ADR-0027](docs/adr/0027-a-narrowed-licence-run-is-named-for-its-folder.md)) | `docker.io` until 2026-12-31 ([ADR-0005](docs/adr/0005-host-exception-with-an-end.md)); the image is named by digest ([ADR-0008](docs/adr/0008-licence-tool-image-by-digest.md)) |
| `.github/workflows/scorecard.yml` | the supply-chain checks of the OpenSSF Scorecard action, reported to code scanning and kept as the artifact `scorecard-result` | `outside_hosts` (boolean, default false: the run fails, [ADR-0026](docs/adr/0026-a-call-that-analyses-nothing-fails.md)); `publish` (boolean, default false) | `<calling job> / Public repository`, `<calling job> / Analysis`; with `outside_hosts` false, `Analysis` is the failed refusal and the analysis job reports as `Analysis (not started)` | with `outside_hosts`: `www.bestpractices.dev`, `api.deps.dev`, `api.osv.dev`, `oss-fuzz-build-logs.storage.googleapis.com`; with `publish`: `api.scorecard.dev`, `fulcio.sigstore.dev`, `rekor.sigstore.dev`, `tuf-repo-cdn.sigstore.dev` ([ADR-0013](docs/adr/0013-scorecard-behind-a-switch.md)) |
| `.github/workflows/merge-train.yml` | in every repository the merge-train app is installed on, lands one clean pull request or updates the next one by merge ([ADR-0029](docs/adr/0029-a-forge-app-drives-the-merge-train.md)) | `app_client_id` (required); `runner` (required, needs python3 and openssl); `switch` (`on` or `off`); `merge_method` (`SQUASH` or `MERGE`); `accept_states` (`UNSTABLE`, `HAS_HOOKS`; default none); `min_age_days` (default 3); `hold_labels`, `dependency_branches`, `person_branches`, `security_labels`; secret `app_private_key` | `<calling job> / Merge train`, never a check of a pull request | none |

The licence job reads the contents and takes no secret; the caller grants
`contents: read` and nothing else. On a draft pull request the job stops with
an error instead of running. `root` must be a plain relative path (letters,
digits, dot, underscore, hyphen, slash; no `..`); anything else stops the job
before the tool runs. A run with another `root` than `.` checks that folder
only: its check carries the folder in its name, so it never satisfies a
required `REUSE lint`, and its summary says that files outside it were not
checked ([ADR-0027](docs/adr/0027-a-narrowed-licence-run-is-named-for-its-folder.md)).

The scorecard job passes whatever it finds and takes no secret. The caller
grants `contents: read`, `security-events: write` and `id-token: write`. Two
of its hosts are asked with the repository's name, so a caller switches the
job on by the repository's visibility and never with a plain `true`, and the
caller is kept in a public repository only. A run that analyses nothing
fails: with `outside_hosts` false, `<calling job> / Analysis` is a failed job
with the reason in its text, because a skipped analysis would read as a pass
([ADR-0026](docs/adr/0026-a-call-that-analyses-nothing-fails.md)). A caller that passes `true` for
either input in a repository that is not public gets a failed check,
`<calling job> / Public repository`, with the reason in its text, and the
analysis does not start
([ADR-0015](docs/adr/0015-refusal-outside-a-public-repository-is-a-failed-job.md)).
The runner is no input: the Scorecard
service takes a published result only from a job whose label is written out,
and `make check-scorecard` holds the file to that and the service's other
rules.

The merge train runs from one caller in the fleet's operations repository
and sweeps every repository the merge-train app is installed on; the
repositories it serves keep no caller and no secret. The app's key is the
secret `MERGE_TRAIN_APP_KEY` of the environment `merge-train` there, which
only the branch main may use. For each repository the job mints a token
limited to that repository, with `contents: write`, `pull_requests: write`,
`workflows: write` and `checks: read`, and revokes it when the repository is
done; GITHUB_TOKEN gets no scope. A run lands at most one pull request per
repository: the oldest the forge reports CLEAN, through the merge call with
the head commit it read, so a red or blocked pull request is refused by the
forge itself and the app has no bypass. With none CLEAN and no armed one in
flight, it arms auto-merge on the oldest BEHIND pull request whose checks are
not red and merges the default branch into it. It leaves alone drafts, forks,
other bases, the hold labels, release branches (`release-please--`, a release
is a person's act), dependency updates younger than the minimum age and
archived repositories. The app is one public app installed on every account
of the fleet; its rights are in `apps/merge-train.json`, and a test holds them
equal to what the train asks the forge for.

## Layout

| Path | Content |
| :--- | :--- |
| `.github/workflows/` | reusable workflows, this repository's own gate (`ci.yml`) and its callers (`licence-check.yml`) |
| `actions/<name>/` | composite steps |
| `templates/callers/` | the caller a consumer keeps, one template per shared workflow ([ADR-0009](docs/adr/0009-caller-templates-in-the-engines-syntax.md)) |
| `tools/` | the checks of the gate, the same-verdict check for a switch, the renderer of the caller templates, the release read-back, and the check of action pins and runner labels |
| `tests/` | tests of those checks; each rule has a planted defect that must fail |
| `tests/fixtures/` | planted cases a workflow is run on: `unlicensed` must fail the licence check, `licensed` must pass it ([ADR-0006](docs/adr/0006-planted-case-as-a-fixture.md)); `readback` holds the parameter files of three read releases |
| `apps/` | manifests of the forge apps the workflows use, with their computed rights ([ADR-0029](docs/adr/0029-a-forge-app-drives-the-merge-train.md)) |
| `interface.lock.json` | recorded interface of every workflow and action |
| `hosts.toml` | hosts and outside actions the workflows may reach; a host without a switch has a day it ends, at most 90 days ahead ([ADR-0010](docs/adr/0010-one-longest-time-for-every-exception.md)); a host for public repositories only is marked ([ADR-0013](docs/adr/0013-scorecard-behind-a-switch.md)), and its workflow refuses another repository in a job of its own ([ADR-0015](docs/adr/0015-refusal-outside-a-public-repository-is-a-failed-job.md)) that runs on every call and is given the visibility and every such input ([ADR-0026](docs/adr/0026-a-call-that-analyses-nothing-fails.md)) |
| `exceptions.yaml` | exceptions to rules no other list can carry, each with its issue and a day it ends ([ADR-0007](docs/adr/0007-one-own-exception-list.md)) |
| `docs/adr/` | decision records |

## Gate

`make verify-all` is the one gate, locally and in CI. It needs Python 3.12 or
newer, actionlint 1.7.12, gitleaks 8.30.1 and `praetorctl` on the path; the
other tools are installed into `.venv` from `requirements/tools.txt` with their
hashes.

- `make verify` runs this repository's own checks without the engine: YAML and
  Python lint, actionlint and zizmor, the interface record, the host scan, the
  accepted findings and exceptions, the caller templates, the Scorecard
  service's rules, the runner labels, the tests and the licence headers.
- The tests read the message of every commit between the base of the change
  and `HEAD` (`tests/test_commit_messages.py`), on every run of the gate. A
  message fails without a `Signed-off-by` trailer, and when it credits
  a tool or a model: a trailer whose key is not `Signed-off-by` or `Migration`,
  a co-author line, or a line that opens with "generated with". The engine's
  own message check passes such a message; this test retires when the pinned
  engine refuses it. The message of a squash merge is written on the forge, so
  it is read only afterwards, on the run of `main`.
- No tracked file holds the address this repository had under its working name
  (`tests/test_name.py`): the forge does not redirect a call to it. Two
  accepted records keep it and are named in the test
  ([ADR-0023](docs/adr/0023-the-name-is-horistes.md)).
- Before `make verify-all`, the gate workflow scans every commit of the
  checkout for secrets with gitleaks and its own rules. It refuses a
  `.gitleaks.toml` or `.gitleaksignore` in the tree and ignores allow comments;
  a finding is printed redacted ([ADR-0028](docs/adr/0028-every-commit-is-scanned-for-secrets.md)).
  `tests/test_secret_scan.py` runs the step on planted repositories.
- `make interface-snapshot` rewrites `interface.lock.json` after an interface
  change. A change that breaks a caller also raises the recorded version:
  `interface_minor` while `interface_major` is 0, `interface_major` from 1 on
  ([ADR-0026](docs/adr/0026-a-call-that-analyses-nothing-fails.md)).

## Caller templates

A consumer's caller is rendered from `templates/callers/<name>.yml.tmpl`:

```sh
python3 tools/render.py render --template templates/callers/licence.yml.tmpl --context context.toml \
  > .github/workflows/licence-check.yml
```

`context.toml` holds the values of the template's fields:

```toml
RunnerTag = "ubuntu-26.04"

[Shared]
Repository = "owner/name"
Commit = "<full id of the commit of this repository the caller names>"
Version = "<the version that commit carries, written as a comment>"
```

The templates are written for the engine (Go `text/template` with the
delimiters `<%` and `%>`). The engine does not render them yet
(cordanaLLM/praetor issue 1211), so `tools/render.py` reads the part a caller
needs: a field, a maintainer note and the trim markers. It refuses anything
else, and a field the context does not hold; exit code 3.

`make check-templates`, part of the gate, renders every template with sample
values and fails unless the result calls a workflow file of this repository by
commit, with inputs that workflow declares. A secret is passed only when that
workflow declares it, by name, as `${{ secrets.NAME }}`; `secrets: inherit`
and a secret written out are refused. The job must grant
exactly the scopes the called workflow's jobs ask for, and an input that
switches on a host for public repositories only is passed as
`${{ github.event.repository.visibility == 'public' }}` or as false
([ADR-0013](docs/adr/0013-scorecard-behind-a-switch.md)). The scorecard
template is rendered in public repositories only: elsewhere, and with false,
its `Analysis` check fails ([ADR-0026](docs/adr/0026-a-call-that-analyses-nothing-fails.md)). It takes no
`RunnerTag`.

A caller that runs on pull requests or in a merge queue also holds the job
`result`. It carries the calling job's own name, which the engine declares as
the required check, and fails unless the call passed; the forge reports the
calling job itself only as "<name> / <called job>"
([ADR-0030](docs/adr/0030-a-result-job-reports-the-calling-jobs-name.md)).

## Switching a consumer

A repository replaces its own job by a shared workflow only after both ran
side by side and decided the same
([ADR-0004](docs/adr/0004-same-verdict-before-a-switch.md)):

```sh
python3 tools/verdict.py --map trial.toml --live --record runs.json --markdown verdict.md
```

`trial.toml` names the repository, the trial's branches, each pair of old and
new check with their workflow files, and the planted commits:

```toml
repository = "owner/name"
branches = ["trial/licence"]

[[pair]]
family = "licence check"
class = "R"
old = "REUSE lint"
new = "Licence / REUSE lint"
old_workflow = ".github/workflows/reuse.yml"
new_workflow = ".github/workflows/licence-check.yml"
called_workflow = ".github/workflows/licence.yml"
draft_stop = "Stop on a draft pull request"

[[planted]]
family = "licence check"
commit = "<full id of a commit both jobs must fail>"
expect = "FAIL"
marker = "reuse lint"

[[planted]]
family = "licence check"
commit = "<full id of a commit both jobs must pass>"
expect = "PASS"
```

`branches` may name the branch the switch is made on from its start. A push
whose commit does not yet hold the caller's workflow file lies outside the
trial and counts for nothing; a job that is absent although the commit holds
its workflow file ends the check with exit code 3
([ADR-0012](docs/adr/0012-row-outside-the-trial.md)).

A planted case can also be a fixture: a folder of the repository both forms
of the job are run on, in jobs of their own that exist only on the trial's
branch. Such a pair must reach the named verdict on every subject and counts as
a planted case of its family
([ADR-0006](docs/adr/0006-planted-case-as-a-fixture.md)):

```toml
[[fixture]]
family = "licence check"
class = "R"
old = "Old form, unlicensed fixture"
new = "New form, unlicensed fixture / REUSE lint (tests/fixtures/unlicensed)"
old_workflow = ".github/workflows/trial-old.yml"
new_workflow = ".github/workflows/trial-new.yml"
called_workflow = ".github/workflows/licence.yml"
draft_stop = "Stop on a draft pull request"
expect = "FAIL"
marker = "reuse lint"
```

A job that passes whatever it finds, such as a scorecard run, is a pair of
class U. Where both jobs pass, the checks and findings of the SARIF result
each uploaded are compared, and the planted commits name a finding that must
show in both results and a commit where it must show in neither
([ADR-0011](docs/adr/0011-uploaded-result-of-a-job-that-always-passes.md)):

```toml
[[pair]]
family = "scorecard"
class = "U"
old = "Scorecard analysis"
new = "Scorecard / Analysis"
old_workflow = ".github/workflows/scorecard-old.yml"
new_workflow = ".github/workflows/scorecard-check.yml"
called_workflow = ".github/workflows/scorecard.yml"
old_artifact = "scorecard-old"
new_artifact = "scorecard-result"

[[planted]]
family = "scorecard"
commit = "<full id of a commit with the planted finding>"
expect = "FINDING"
marker = '^PinnedDependenciesID \| \.github/workflows/planted\.yml:7 '

[[planted]]
family = "scorecard"
commit = "<full id of a commit without it>"
expect = "CLEAN"
marker = '^PinnedDependenciesID \| \.github/workflows/planted\.yml:7 '
```

A finding is one line: its rule, its places, the first line of its message
and a digest of the other lines. `--record` keeps the results in the recorded
file, so `--runs` judges them again after the artifacts expired.

`--root` is the consumer's checkout and `--called-root` the checkout of this
repository when the two differ. `--runs FILE` judges a recorded file instead of
the forge. Exit code 0 allows the switch; 1, 2 and 3 do not.

## Reading a release back

A published release is verified the way a stranger does it: with no login,
no token and an empty home folder
([ADR-0014](docs/adr/0014-release-read-back-without-credentials.md)):

```sh
python3 tools/readback.py verify --params release.toml --tag v1.2.3
```

The tool needs Python 3.12, `git` and `cosign` (read with v2.6.3) and
nothing else from this repository than `tools/readback.py` and
`tools/stranger.py`. A repository writes one parameter file and no script:

```toml
repository = "owner/name"
signer = ".github/workflows/release.yml"      # the workflow that signs
other_workflows = [".github/workflows/ci.yml"]  # must not pass in its place
unverified = ["notes-{version}.txt"]           # assets nothing here verifies
images = ["ghcr.io/owner/name:{version}"]

[blobs]
checksums = "checksums.txt"
signature = "checksums.txt.sigstore.json"
```

A file names `blobs`, `images` or both; `{tag}` is the tag and `{version}`
the tag without a leading `v`. `python3 tools/readback.py check --params
release.toml` reads the file and makes no request.

- **Blobs.** The release holds exactly the checksums, their signature, the
  listed files and the files declared `unverified`. A file that is declared
  `unverified` and listed in the checksums fails: the declaration is out of
  date and has to go. Each listed file matches
  its digest, the signature verifies the checksums, and for each listed file
  the forge serves a provenance attestation that verifies it.
- **Images.** The signature verifies the reference, and the provenance
  attestation verifies the image by the digest the signature named.
- **Identity.** Every signature and attestation must come from the `signer`
  workflow of the repository, run on the tag, at the commit `git ls-remote`
  gives for the tag.
- **Controls.** Each accepted check is asked again with one expectation
  changed (another tag, the branch `main`, another repository, another
  commit, another issuer, another workflow, an altered file), and each of
  those calls must be refused. A control that gets no answer fails.

The lines read `PASS accept`, `PASS refuse`, `FAIL ...` and `NOTE ...`. Exit
code 0 means every line passed, 1 that a check failed or a control was
accepted, 3 that the parameters, the tag or an answer of the forge could not
be read. The forge's API answers 60 requests an hour per address without
credentials; a release costs one request and one more for each attestation
the listed files need. The parameter files of the releases the tool was
accepted on are kept in `tests/fixtures/readback/`.

## Action pins

Every outside action and called workflow is named by a commit with its
release in a comment. One tool lists, writes and checks those lines for any
checkout
([ADR-0016](docs/adr/0016-action-pins-checked-with-zizmor-behind-three-guards.md)):

```sh
make list-pins  PINS_ROOT=../other   # uses on a tag or a branch; asks no host
make check-pins PINS_ROOT=../other   # fails on any line below
make pin        PINS_ROOT=../other   # writes what can be fixed, then checks
```

`PINS_ROOT` defaults to this repository. The tool reads
`.github/workflows/*.yml` and `actions/*/action.yml` of the checkout and runs
with the tools of this repository (`.venv`), never with a zizmor of the
checkout.

| `FAIL` line | Meaning | `pin` |
| :--- | :--- | :--- |
| `action is not pinned to a hash` | the use is on a tag or a branch | writes the commit the name stands for today and its release as the comment |
| `is pointed to by tag vX` | the comment names another release than the commit | corrects the comment, keeps the commit |
| `missing version comment` | a commit without a comment | adds the release |
| `comment does not contain a version`, `points to unknown ref`, `branch points to commit ...` | the comment is no release of that repository, or holds more than the release | left for a person |
| ``input `x` is not declared at that ref`` | a `with:` key the action or the called workflow does not have; the runner would ignore it | left for a person |
| `no action.yml or action.yaml at that ref, or not readable` | the forge does not serve the metadata to this token | left for a person |

`pin` moves no pin to a newer release; updates come from Renovate. The
comment has to be the release alone (`# v4.2.2`). A commit that no release
names is not judged.

`check-pins` and `pin` need a token in `GH_TOKEN`, `GITHUB_TOKEN` or
`ZIZMOR_GITHUB_TOKEN`, or a logged-in `gh`. Exit code 0 means every line
passed, 1 a finding, 3 that the check could not be made: no token, a zizmor
that did not report the planted case of the run, a finding of an audit the
tool does not switch off, or an answer of the forge that cannot be read. The
last line of a pass names the zizmor version and the numbers of files, uses
and keys read. Neither target is part of `make verify-all`, which asks no
host.

For this repository the check runs by itself: `.github/workflows/pins.yml`
starts it on a pull request that changes a workflow, the tool or the pinned
tools, once a week, and by hand
([ADR-0017](docs/adr/0017-pin-check-as-its-own-job-outside-the-gate.md)). It
is no required check. The summary of each run names the forge requests the
check took. Other repositories run the three targets from a checkout of this
one until a reusable workflow exists.

### Runner labels

The same tool refuses a floating label of a hosted machine
([ADR-0018](docs/adr/0018-floating-runner-labels-refused-by-the-pin-tool.md)):

```sh
make check-runners PINS_ROOT=../other   # asks no host, needs no token
```

```text
FAIL: .github/workflows/build.yml:46: `runs-on` of job `build`: `ubuntu-latest` is a floating runner label; name the release it stands for
```

The labels are `ubuntu-latest`, `windows-latest`, `macos-latest`,
`macos-latest-large` and `macos-latest-xlarge`, as a whole word and without
regard to case: `ubuntu-latest-xl` is a label of its own and passes.

| Read | Not read |
| :--- | :--- |
| `runs-on` of a job: a text, a list, `group` with `labels`, the text of an expression | a name, a description, a command, a variable, a comment |
| every value under `strategy` of a job | a repository variable (`vars`) or an output of an earlier job (`needs`) |
| every value a job hands to a called workflow under `with` | a label put together from parts (`ubuntu-${{ inputs.release }}`) |
| `default` and `options` of an input of `workflow_call` and `workflow_dispatch` | an action file |

The last line of a pass counts the jobs that take their label from an
expression. Exit code 0 is a pass, 1 a finding, 3 a workflow that cannot be
read. `check-pins` and `pin` run the check as well; `pin` rewrites no label.
The check is part of `make verify-all`. It has no exception list yet: a
finding is fixed.

## Updates

Newer releases are proposed by Renovate (the update bot; the English verb)
from `renovate.json` and the preset it extends, `default.json`
([ADR-0019](docs/adr/0019-update-proposals-from-renovate-configured-here.md),
[ADR-0020](docs/adr/0020-shared-update-bot-values-as-a-preset.md)).
A proposal waits three days after the release, is never merged by the bot and
has to pass the gate like any other pull request.

| Read by the bot | Where | How it finds the release |
| :--- | :--- | :--- |
| outside actions | `.github/workflows/` | the commit and the release in the comment behind it |
| tools of the gate | `requirements/tools.txt` | the compile command in the header of the file, which it reruns |
| the licence tool's image | `.github/workflows/licence.yml` | the comment `# release X.Y.Z` on the line above `uses:` and the digest in it |

Not read: the files the Praetor engine writes (`.devcontainer/` among them),
the machine of a job, and the actionlint and gitleaks versions with their
checksums in the `Makefile` and the gate workflow.

`make requirements` recompiles the tool pins by hand with the same command.
Keep the header and the comment above the image line: `tests/test_update_bot.py`
fails when the bot could no longer read one of them.

### The preset other repositories extend

`default.json` holds what is the same in every repository
([ADR-0020](docs/adr/0020-shared-update-bot-values-as-a-preset.md)):

| Value | Setting |
| :--- | :--- |
| base | `config:recommended` |
| commits of the bot | carry the sign-off line (`:gitSignOff`) |
| waiting time after a release | three days |
| merge by the bot | never |
| the machine a job runs on | not proposed |

A repository extends it by reference and keeps only what is its own (which
managers run, which files they read):

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github><owner>/<this repository>"]
}
```

Without a file name Renovate reads `default.json` of the named repository;
in this family the reference is `github>plurano/horistes`. This
repository's own `renovate.json` extends the preset the same way and repeats
none of its values.

- While this repository is private, only private repositories of the same
  owner can extend the preset. A public repository waits until this one is
  public.
- From the first release on, a consumer names the release
  (`github><owner>/<this repository>#1.0.0`) and adds `renovate-config` to
  its `enabledManagers`: Renovate then proposes the next release of the
  preset like any other update. A reference without a release follows the
  default branch.
- A repository that lints with REUSE names the configuration in its
  `REUSE.toml`, because JSON carries no comment for a header:

  ```toml
  [[annotations]]
  path = ["renovate.json"]
  precedence = "aggregate"
  SPDX-FileCopyrightText = "<year> <holder>"
  SPDX-License-Identifier = "<licence of the repository>"
  ```

  If the repository already has an `aggregate` table for files without a
  header, add `"renovate.json"` to its `path` list instead.

### The preset for a repository under settings sync

`settings-sync.json` is for a repository that holds its Rust toolchain file
equal to a source in another repository and compares it in its gate
([ADR-0021](docs/adr/0021-settings-sync-preset-switches-the-toolchain-manager-off.md)).
There a toolchain proposal of the bot changes the copy and can never pass, so
the preset switches the manager `rust-toolchain` off and does nothing else.
Such a repository names both presets:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": [
    "github><owner>/<this repository>",
    "github><owner>/<this repository>:settings-sync"
  ]
}
```

- A repository that cannot extend the preset yet (see above) writes the one
  key into its own file, in the same form as the preset holds it:
  `"rust-toolchain": { "enabled": false }`.
- A repository that names its managers in `enabledManagers` and leaves
  `rust-toolchain` out needs neither.
- A repository that owns its toolchain file does not extend this preset: it
  would receive no toolchain proposal, and nothing reports that.

Check a changed preset before it is merged, with the validator that ships in
the Renovate image:

```sh
docker run --rm --network=none -v "$PWD:/data:ro" -w /data \
  --entrypoint renovate-config-validator renovate/renovate:<release> \
  --strict --no-global default.json settings-sync.json renovate.json
```

`--no-global` makes it read the files as repository configuration. The
planted files `tests/fixtures/update-bot/planted-preset.json` and
`tests/fixtures/update-bot/planted-settings-sync.json` must each fail the
same command.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
