<!--
SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
SPDX-License-Identifier: EUPL-1.2
-->

# ADR-0032: The merge train commits generated files for dependency updates

- **Status**: Accepted
- **Date**: 2026-10-15
- **Authors**: Lusoris

## Context

Dependency-update pull requests (such as `renovate/*`) in fleet repositories
with committed generated files (for example `plurano/amussis`, where a WebAssembly
module and third-party notices are compiled and generated from `Cargo.lock`)
consistently go red until a person manually regenerates and commits them.

The hosted Mend Renovate app cannot run arbitrary build commands: Mend's hosted
plan refuses `postUpgradeTasks` execution for security and isolation reasons.
A workflow in the repository running under `GITHUB_TOKEN` could generate the files,
but commits pushed with `GITHUB_TOKEN` trigger no GitHub Actions workflows, so
required checks would never run on the updated head.

ADR-0029 established a single central key store in `lusoris/k8s` for a GitHub App
driving the merge train, minting short-lived scoped tokens per repository whose
events do start check runs. The merge train already sweeps every repository
served by the app.

## Decision

- **Move before landing.** The merge train (`.github/workflows/merge-train.yml`,
  job `train`, step "Land or update one pull request in every repository of the app")
  gains one generic move: `regenerate`. It executes only when `switch` is `on`,
  prior to landing.
- **Candidates.** Open pull requests whose verdict is not draft, cross-repository,
  held, not for the default branch, or release/reserved branch ("release or reserved
  branch: a person lands it"), and whose `headRefName` starts with one of
  the configured `dependency_branches` prefixes (by default `renovate/`, `dependabot/`).
- **Artifact contract (fleet-generic):**
  - Name: `regenerated-<40-hex head sha>`.
  - Content: A zip holding `manifest.json` and `files/<path>` for each entry,
    and nothing else.
  - Manifest format:
    ```json
    {
      "schema": "horistes.generated-files.v1",
      "head": "<40-hex head sha>",
      "files": [
        {
          "path": "path/to/file",
          "sha256": "<64-hex>",
          "bytes": 1234
        }
      ],
      "commands": [
        ["argv0", "argv1"]
      ]
    }
    ```
- **Repository allowlist.** The repository declares which paths may be committed
  in `.github/generated-files` on its default branch (one repo-relative path per
  line, `#` comments, blank lines ignored). If this file is missing (HTTP 404),
  the move is skipped for that repository without error. The artifact never
  widens this allowlist.
- **Artifact lookup & verification:**
  - Artifacts are listed via `GET /repos/{r}/actions/artifacts?name=regenerated-<headRefOid>&per_page=10`.
    The newest unexpired artifact whose `workflow_run.head_sha == headRefOid` and
    `workflow_run.head_branch == headRefName` is selected. If none matches, the train
    evaluates the next candidate.
  - The download URL (`archive_download_url`) responds with an HTTP 302 redirect
    to a storage URL. The train follows this without forwarding the `Authorization`
    header (using a redirect-disabled opener and performing a clean GET). The storage
    URL's scheme must match the scheme of `GITHUB_API_URL` (https in production, http in
    tests); an empty or scheme-mismatched redirect is refused.
  - Artifact download size is bounded at `MAX_ARTIFACT_BYTES = 16 MiB` (16,777,216 bytes).
    Before reading any member content, each member's declared `file_size` and the total
    uncompressed size sum across all members must not exceed `MAX_ARTIFACT_BYTES`. Duplicate
    member names are refused. Directory entries (names ending with `/` and `file_size == 0`)
    are ignored; entries ending with `/` and `file_size > 0` are refused.
  - Manifest `files` entries must be strictly typed objects with `str` path, `int` bytes,
    and `str` sha256; duplicate paths in manifest files are refused. Validation requires:
    exactly `manifest.json` and `files/<p>` per manifest entry; `schema == "horistes.generated-files.v1"`;
    `head == headRefOid`; every path in the allowlist, relative, without `..`, leading `/`,
    or backslash; file sizes and SHA-256 digests matching the manifest.
  - Any `RefusedError` during artifact discovery, download, validation, or file-diff
    filtering is caught per pull request and noted in the train summary
    (`notes[number] = f"generated files refused: {err}"[:200]`), allowing the sweep to
    continue evaluating subsequent candidates and proceed to the normal landing/update
    loop without aborting the repository run. A `RefusedError` during `createCommitOnBranch`
    (the write path) still propagates and fails the run.
- **Skipping current files.** Each file is compared against the head commit
  (`GET /repos/{r}/contents/{path}?ref=headRefOid`, comparing the Git blob SHA:
  `sha1(b"blob %d\0" % len + data)`). If all files already match the head's content,
  the train leaves the pull request's verdict in the report unchanged and proceeds to the
  next candidate.
- **Commit creation.** When changed files remain, the train invokes the GraphQL
  `createCommitOnBranch` mutation on the pull request head branch:
  - Branch: `{repositoryNameWithOwner, branchName: headRefName}`.
  - `expectedHeadOid`: `headRefOid`.
  - `fileChanges.additions`: `[{path, contents (base64)}]`.
  - Commit message: headline `"chore(deps): regenerate generated files"`, body
    `"Regenerated by the dependency update's own workflow run <workflow_run html_url or id>, artifact regenerated-<sha>.\n\nSigned-off-by: <bot login> <<bot id>+<bot login>@users.noreply.github.com>"`.
  - Bot identity is discovered once per sweep via `GET /app` with the JWT to get the
    slug and unauthenticated `GET /users/<slug>[bot]` to obtain the bot ID (the forge
    refuses JWT authentication on non-`/app` endpoints).
  - Commits made through GraphQL are signed by GitHub. The app commit triggers new
    check runs on the updated head.
- **Rate of change.** In each repository, at most one write is performed per run.
  If generated files are committed, the sweep notes "generated files committed",
  reports outcome "committed generated files to #N", counts it under "updated",
  and does not land any pull request in that run.
- **Rights.** `WRITE_RIGHTS` in `.github/workflows/merge-train.yml` and
  `default_permissions` in `apps/merge-train.json` gain `actions: read`.
  `READ_RIGHTS` remains unchanged.

## Consequences

- **Positive:**
  - Dependency update pull requests for repositories with generated assets
    automatically receive the required generated files and turn green.
  - GitHub signs the generated commits via GraphQL `createCommitOnBranch`, and the
    push starts repository checks.
  - The repository maintains strict boundary control over what can be committed via
    the default-branch `.github/generated-files` allowlist.
- **Negative:**
  - The new permission `actions: read` on the merge-train GitHub App must be
    accepted on every installation across the fleet's accounts (an operator act).
- **Neutral:**
  - Renovate treats a branch that holds another author's commit as edited and
    stops rebasing it. The fleet preset `default.json` therefore lists the bot's
    commit address in `gitIgnoredAuthors` (Renovate compares an entry for
    equality before it reads it as a pattern). Renovate keeps rebasing the
    branch; a rebase drops the generated-file commit, the repository workflow
    uploads a new artifact for the new head, and the next sweep commits it again.

## References

- Fleet order merge-train-generated-files, 2026-10-15.
- Decision record 0029 (`docs/adr/0029-a-forge-app-drives-the-merge-train.md`).
- Decision record 0031 (`docs/adr/0031-the-merge-train-app-runs-release-please.md`).
- GitHub GraphQL API documentation: `createCommitOnBranch`.
- GitHub REST API documentation: Actions Artifacts and Repository Contents.
