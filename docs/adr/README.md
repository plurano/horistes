# Architectural Decision Records (ADRs)

This directory documents key architectural decisions. Records are append-only
like public APIs under HISS-14 (Append-Only ABI & Migration Footers).
An accepted record keeps its number and text; a changed decision is a new
record that supersedes it.

| Number | Date | Title | Status |
| :--- | :--- | :--- | :--- |
| [0000](0000-template.md) | 2026-09-11 | ADR template | Accepted |
| [0001](0001-three-layers.md) | 2026-10-11 | One source, a copy per owner, thin callers | Accepted |
| [0002](0002-versioning-and-pinning.md) | 2026-10-11 | Versions, pins and the recorded interface | Accepted |
| [0003](0003-declared-hosts.md) | 2026-10-11 | Every host a workflow reaches is declared | Accepted |
| [0004](0004-same-verdict-before-a-switch.md) | 2026-10-11 | A consumer switches only after the same verdict is shown | Accepted |
| [0005](0005-host-exception-with-an-end.md) | 2026-10-11 | A host without a switch is an exception with a day it ends | Accepted |
| [0006](0006-planted-case-as-a-fixture.md) | 2026-10-11 | A planted case may be a fixture in the tree | Accepted |
| [0007](0007-one-own-exception-list.md) | 2026-10-11 | Exceptions the engine's list cannot carry live in one own list | Accepted |
| [0008](0008-licence-tool-image-by-digest.md) | 2026-10-11 | The licence tool's image is named by digest | Accepted |
| [0009](0009-caller-templates-in-the-engines-syntax.md) | 2026-10-11 | Caller templates are written in the engine's syntax and rendered here until the engine does | Accepted |
| [0010](0010-one-longest-time-for-every-exception.md) | 2026-10-11 | A dated host runs no longer than any other exception | Accepted |
| [0011](0011-uploaded-result-of-a-job-that-always-passes.md) | 2026-10-11 | A job that passes whatever it finds is compared by the result it uploads | Accepted |
| [0012](0012-row-outside-the-trial.md) | 2026-10-11 | A push whose commit does not hold a job's workflow file lies outside the trial | Accepted |
| [0013](0013-scorecard-behind-a-switch.md) | 2026-10-11 | The scorecard run sits behind a switch that a caller sets by the repository's visibility | Accepted |
| [0014](0014-release-read-back-without-credentials.md) | 2026-10-11 | A release is read back without credentials, by one tool and a parameter file per repository | Accepted |
| [0015](0015-refusal-outside-a-public-repository-is-a-failed-job.md) | 2026-10-11 | A workflow with an input for public repositories only refuses another repository with a failed job | Accepted |
| [0016](0016-action-pins-checked-with-zizmor-behind-three-guards.md) | 2026-10-11 | Action pins are listed, written and checked by zizmor behind three guards, and `with` keys are read here | Accepted |
| [0017](0017-pin-check-as-its-own-job-outside-the-gate.md) | 2026-10-11 | The pin check runs as its own job, on workflow changes, weekly and by hand, outside the gate | Accepted |
| [0018](0018-floating-runner-labels-refused-by-the-pin-tool.md) | 2026-10-11 | A floating label of a hosted machine is refused by the pin tool, without an exception list for now | Accepted |
| [0019](0019-update-proposals-from-renovate-configured-here.md) | 2026-10-11 | Updates are proposed by Renovate from a configuration in this repository | Accepted |
| [0020](0020-shared-update-bot-values-as-a-preset.md) | 2026-10-12 | The update bot's shared values are a preset file here, and this repository extends it | Accepted |
| [0021](0021-settings-sync-preset-switches-the-toolchain-manager-off.md) | 2026-10-12 | A second preset switches the toolchain manager off in a repository under settings sync | Accepted |
| [0022](0022-public-consumers-call-this-repository-directly.md) | 2026-10-12 | Public consumers call this repository directly; the copy per owner is not built | Accepted |
| [0023](0023-the-name-is-horistes.md) | 2026-10-12 | The repository is named Horistes, and the rename comes before the first release | Accepted |
| [0024](0024-public-before-the-first-release.md) | 2026-10-12 | The repository turns public before its first release | Accepted |
| [0025](0025-the-rule-set-goes-live-before-the-engine-fix.md) | 2026-10-12 | The rule set goes live before the engine names the licence check right | Accepted |
| [0026](0026-a-call-that-analyses-nothing-fails.md) | 2026-10-12 | A scorecard call that analyses nothing fails; a break at interface 0 raises the minor version | Accepted |
| [0027](0027-a-narrowed-licence-run-is-named-for-its-folder.md) | 2026-10-12 | A licence run narrowed to a folder is named for that folder | Accepted |
| [0028](0028-every-commit-is-scanned-for-secrets.md) | 2026-10-13 | Every commit is scanned for secrets in the gate, with the tool's own rules only | Accepted |
| [0029](0029-a-forge-app-drives-the-merge-train.md) | 2026-10-13 | A forge app drives the merge train, with no bypass | Accepted |
| [0030](0030-a-result-job-reports-the-calling-jobs-name.md) | 2026-10-14 | A result job reports the calling job's name | Accepted |
| [0031](0031-the-merge-train-app-runs-release-please.md) | 2026-10-14 | The merge-train app runs release-please, and a person still merges the release | Accepted |
| [0032](0032-the-merge-train-commits-generated-files.md) | 2026-10-15 | The merge train commits generated files for dependency updates | Accepted |
| [0033](0033-read-back-of-local-files-before-the-draft.md) | 2026-10-15 | The read-back reads a release's files from a local folder before the draft | Accepted |
| [0034](0034-the-merge-train-queues-where-the-branch-has-a-merge-queue.md) | 2026-10-16 | The merge train queues a pull request where the default branch has a merge queue | Accepted |
| [0035](0035-a-weekly-job-bounds-the-licence-tool-image-age.md) | 2026-10-16 | A weekly job bounds the licence tool's image age | Accepted |
| [0036](0036-the-update-bot-moves-pinned-release-tools-and-checksums.md) | 2026-10-16 | The update bot moves pinned release tools and their checksums | Accepted |
| [0037](0037-the-merge-train-writes-the-squash-message.md) | 2026-10-16 | The merge train writes the squash message | Accepted |
| [0038](0038-the-merge-train-skips-superseded-pull-requests.md) | 2026-10-16 | The merge train skips superseded pull requests | Accepted |
| [0039](0039-the-merge-train-closes-the-issues-its-merges-name.md) | 2026-10-17 | The merge train closes the issues its merges name | Accepted |
| [0040](0040-the-merge-train-leaves-a-release-to-its-writer.md) | 2026-10-18 | The merge train leaves a release to its writer | Accepted |
| [0041](0041-the-merge-train-refreshes-a-draft-stopped-pull-request.md) | 2026-10-18 | The merge train refreshes a draft-stopped pull request | Accepted |
| [0042](0042-the-merge-train-judges-each-workflow-by-its-newest-run.md) | 2026-10-18 | The merge train judges each workflow by its newest run | Accepted |
| [0043](0043-codeql-reports-to-code-scanning.md) | 2026-10-18 | CodeQL analyses the workflows and the Python tools and reports to code scanning | Accepted |
| [0044](0044-the-merge-train-updates-only-what-it-armed.md) | 2026-10-18 | The merge train updates only what it armed, and never a linear-history branch | Accepted |
| [0045](0045-the-merge-train-opens-no-release-pull-request-where-the-release-train-is-off.md) | 2026-10-19 | The merge train opens no release pull request where the release train is off | Accepted |
| [0046](0046-the-merge-train-counts-the-minimum-age-from-the-pull-request.md) | 2026-10-19 | The merge train counts the minimum age from the pull request | Accepted |
| [0047](0047-the-pin-check-is-required-on-every-pull-request.md) | 2026-10-19 | The pin check runs on every pull request and is a required check | Accepted |
| [0048](0048-the-python-floor-is-tested-in-ci.md) | 2026-10-19 | The Python floor is tested in CI | Accepted |
| [0049](0049-the-merge-train-leaves-a-frozen-release-pull-request-alone.md) | 2026-10-20 | The merge train leaves a frozen release pull request alone | Accepted |
| [0050](0050-a-vulnerability-fix-skips-the-waiting-time.md) | 2026-10-21 | A vulnerability fix skips the waiting time | Accepted |
