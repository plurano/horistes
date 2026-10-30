# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Merge train sweep: land or update one pull request in every repository of the app.

The step "Land or update one pull request in every repository of the app" of
.github/workflows/merge-train.yml runs this file from a checkout of the
workflow's own commit; the step before it writes train_rules.py into
RUNNER_TEMP. The header of the workflow describes what a run does.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.environ["RUNNER_TEMP"])
from train_rules import (
    ARM,
    COMMIT_MUTATION,
    COMMITS,
    DISARM,
    DRAFT,
    ENQUEUE,
    MERGE,
    MERGED,
    QUERY,
    READY,
    REFRESH_LABEL,
    SECURITY_HOLD_LABEL,
    NoSignOffError,
    check_release_off,
    check_release_parallel,
    drifted,
    head_since,
    in_flight,
    issues_to_close,
    lease_until,
    refresh,
    security_hold,
    serving_order,
    squash_message,
    superseded,
    updatable,
    verdict,
    yield_to_lander,
)

TIMEOUT = 30
SETTLE_ROUNDS = 3
SETTLE_SECONDS = 5
PULL_PAGES = 4
READ_ATTEMPTS = 3
READ_BACKOFF_SECONDS = 5
SWEEP_SECONDS = 1500
MAX_PAGES = 20
JWT_SECONDS = 540
MAX_RELEASES = 256
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
WRITE_RIGHTS = {
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "checks": "read",
}
READ_RIGHTS = {"contents": "read", "pull_requests": "read", "checks": "read"}
ISSUE_RIGHTS = {"issues": "write"}
CLOSE_DAYS = 7
LIST_RIGHTS = {"metadata": "read"}
METHODS = ("SQUASH", "MERGE")
ACCEPTABLE = ("UNSTABLE", "HAS_HOOKS")
NO_NEW_COMMITS = "There are no new commits on the base branch."
CONFLICT = "merge conflict between base and head"
MERGE_IN_PROGRESS = "Merge already in progress"
UNGRANTED = "The permissions requested are not granted"
SKIP_UNSIGNED = "skipped: dependency update without Signed-off-by, a person lands it"


class RefusedError(Exception):
    """A forge call that failed or was refused."""


class UnsignedBotError(RefusedError):
    """A dependency update whose commits carry no sign-off; the train skips it."""


class MergeInProgressError(RefusedError):
    """The forge answered that it is already merging the pull request."""


class UngrantedError(RefusedError):
    """The installation has not accepted the rights the train asks for; none of it can run."""


class InvalidSettingError(Exception):
    """A setting outside its allowed values."""


def settings(env):
    """The inputs of the workflow, each checked against an allow-list."""
    switch = env["TRAIN_SWITCH"]
    method = env["TRAIN_MERGE_METHOD"].upper()
    accept = env["TRAIN_ACCEPT_STATES"].upper().split()
    age = env["TRAIN_MIN_AGE_DAYS"]
    if switch not in ("on", "off"):
        raise InvalidSettingError(f"switch is {switch!r}, not on or off")
    if method not in METHODS:
        raise InvalidSettingError(f"merge_method is {method!r}, not one of {', '.join(METHODS)}")
    if any(state not in ACCEPTABLE for state in accept):
        raise InvalidSettingError(f"accept_states holds {accept}, allowed: {', '.join(ACCEPTABLE)}")
    if not age.isdigit() or int(age) > 30:
        raise InvalidSettingError(f"min_age_days is {age!r}, not a whole number from 0 to 30")
    if not env.get("TRAIN_APP_CLIENT_ID", "").strip():
        raise InvalidSettingError("app_client_id is empty")
    if "PRIVATE KEY-----" not in env.get("TRAIN_APP_KEY", ""):
        raise InvalidSettingError(
            "the app key is empty or no PEM key: the caller passes no secret, "
            "or the job runs outside the environment that holds it"
        )
    release_off = check_release_off(env["TRAIN_RELEASE_OFF"], InvalidSettingError)
    parallel = check_release_parallel(env["TRAIN_RELEASE_MAX_PARALLEL"], InvalidSettingError)
    return {
        "switch": switch,
        "method": method,
        "accept": frozenset(("CLEAN", *accept)),
        "min_age": timedelta(days=int(age)),
        "hold": frozenset(name for name in env["TRAIN_HOLD_LABELS"].split(",") if name),
        "dependency": tuple(env["TRAIN_DEPENDENCY_BRANCHES"].split()),
        "person": tuple(env["TRAIN_PERSON_BRANCHES"].split()),
        "release_off": release_off,
        "release_max_parallel": parallel,
        "security": frozenset(name for name in env["TRAIN_SECURITY_LABELS"].split(",") if name),
        "client_id": env["TRAIN_APP_CLIENT_ID"].strip(),
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """An HTTP redirect handler that does not follow redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(url, token=None, method="POST", body=None, raw=False, redirect=True):
    """One request to the forge; the parsed answer, or RefusedError with what it said."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    opener = urllib.request.build_opener() if redirect else urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            content = answer.read(MAX_ARTIFACT_BYTES + 1)
            if len(content) > MAX_ARTIFACT_BYTES:
                raise RefusedError(f"{method} {url}: response exceeds {MAX_ARTIFACT_BYTES} bytes")
            # read(amt) returns a short body without error if the peer closes early.
            length = answer.headers.get("Content-Length", "")
            if length.isdigit() and len(content) != int(length):
                raise RefusedError(f"{method} {url}: body cut: {len(content)} of {length} bytes")
            if raw:
                return content
            text = content.decode()
    except urllib.error.HTTPError as err:
        if not redirect and err.code in (301, 302, 303, 307, 308):
            loc = err.headers.get("Location")
            return {"location": urllib.parse.urljoin(url, loc) if loc else ""}
        raise RefusedError(f"{method} {url}: HTTP {err.code} {err.read().decode()[:500]}") from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise RefusedError(f"{method} {url}: {err}") from err
    return json.loads(text) if text else {}


def graphql(env, query, variables):
    answer = call(
        env["GITHUB_GRAPHQL_URL"],
        env["TRAIN_TOKEN"],
        body={
            "query": query,
            "variables": variables,
        },
    )
    errors = answer.get("errors")
    if errors and all(
        error.get("type") == "UNPROCESSABLE" and error.get("message") == MERGE_IN_PROGRESS
        for error in errors
    ):
        raise MergeInProgressError(f"GraphQL: {json.dumps(errors)[:500]}")
    if errors or "data" not in answer:
        raise RefusedError(f"GraphQL: {json.dumps(errors)[:500]}")
    return answer["data"]


def read_page(env, variables):
    """One page of the read; a forge gateway error (502, 504) is tried again."""
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return graphql(env, QUERY, variables)["repository"]
        except RefusedError as err:
            if getattr(err.__cause__, "code", 0) not in (502, 504) or attempt == READ_ATTEMPTS:
                raise
            time.sleep(READ_BACKOFF_SECONDS)


def pull_pages(env):
    """The oldest open pull requests, at most PULL_PAGES pages."""
    owner, name = env["GITHUB_REPOSITORY"].split("/", 1)
    variables, pulls, repo = {"owner": owner, "name": name, "after": None}, [], {}
    for _ in range(PULL_PAGES):
        repo = read_page(env, variables)
        pulls += repo["pullRequests"]["nodes"]
        page = repo["pullRequests"]["pageInfo"]
        if not page["hasNextPage"]:
            break
        variables["after"] = page["endCursor"]
    repo["pullRequests"]["nodes"] = pulls
    return repo


def fetch(env):
    """The repository and its open pull requests, re-read while a merge state is UNKNOWN."""
    repo = {}
    for attempt in range(SETTLE_ROUNDS):
        repo = pull_pages(env)
        pulls = repo["pullRequests"]["nodes"]
        if all(pull["mergeStateStatus"] != "UNKNOWN" or pull["isDraft"] for pull in pulls):
            break
        if attempt + 1 < SETTLE_ROUNDS:
            time.sleep(SETTLE_SECONDS)
    return repo


def pull_commits(env, number):
    """The non-merge commit messages of one pull request, oldest first."""
    owner, name = env["GITHUB_REPOSITORY"].split("/", 1)
    data = graphql(env, COMMITS, {"owner": owner, "name": name, "number": number})
    commits = data["repository"]["pullRequest"]["commits"]
    if commits["totalCount"] > 100:
        raise RefusedError(f"#{number}: more than 100 commits, cannot read every sign-off")
    return [
        node["commit"]["message"]
        for node in commits["nodes"]
        if node["commit"]["parents"]["totalCount"] == 1
    ]


def commit_variables(env, conf, pull):
    """Variables for MERGE and ARM; headline and body when squashing."""
    base = {"id": pull["id"], "oid": pull["headRefOid"], "method": conf["method"]}
    if conf["method"] != "SQUASH":
        return base
    messages = pull_commits(env, pull["number"])
    try:
        headline, body = squash_message(pull["title"], pull["number"], messages)
    except NoSignOffError as err:
        if pull["headRefName"].startswith(conf["dependency"]):
            raise UnsignedBotError(f"#{pull['number']}: {err}") from err
        raise RefusedError(f"#{pull['number']}: {err}") from err
    except ValueError as err:
        raise RefusedError(f"#{pull['number']}: {err}") from err
    return {**base, "headline": headline, "body": body}


def land(env, conf, pull, queue):
    if queue:
        data = graphql(env, ENQUEUE, {"id": pull["id"], "oid": pull["headRefOid"]})
        if not data["enqueuePullRequest"]["mergeQueueEntry"]:
            raise RefusedError(f"#{pull['number']}: no merge queue entry")
        return "queued"
    try:
        data = graphql(env, MERGE, commit_variables(env, conf, pull))
    except MergeInProgressError:
        # The forge is already merging it; the next run reads the result.
        return "merge in flight"
    if not data["mergePullRequest"]["pullRequest"]["merged"]:
        raise RefusedError(f"#{pull['number']}: the forge did not report it merged")
    return "landed"


def arm(env, conf, pull):
    data = graphql(env, ARM, commit_variables(env, conf, pull))
    if not data["enablePullRequestAutoMerge"]["pullRequest"]["autoMergeRequest"]:
        raise RefusedError(f"#{pull['number']}: the forge returned no auto-merge request")


def update(env, pull):
    """Merge the default branch into the head branch; False when it conflicts."""
    url = f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}/pulls/{pull['number']}"
    try:
        call(
            f"{url}/update-branch",
            env["TRAIN_TOKEN"],
            "PUT",
            {
                "expected_head_sha": pull["headRefOid"],
            },
        )
    except RefusedError as err:
        if NO_NEW_COMMITS in str(err):
            return True
        if CONFLICT in str(err):
            return False
        raise
    return True


def advance(env, conf, repo, rows, notes):
    """Update the first pull request `updatable` names, arming it first where it says."""
    for pull, arming in updatable(repo, rows, notes, conf):
        if arming:
            try:
                arm(env, conf, pull)
            except UnsignedBotError:
                notes[pull["number"]] = SKIP_UNSIGNED
                continue
            notes[pull["number"]] = "armed, updated from the default branch"
        else:
            notes[pull["number"]] = "updated from the default branch"
        if update(env, pull):
            return
        notes[pull["number"]] = "conflicts with the default branch"


def run_jobs(env, run_id):
    """The jobs of one workflow run with their steps, at most 100."""
    api = f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}"
    url = f"{api}/actions/runs/{run_id}/jobs?per_page=100"
    return call(url, env["TRAIN_TOKEN"], "GET")["jobs"]


def toggle_refresh(env, pull):
    """Add REFRESH_LABEL and remove it: two events that carry the pull request as it is now."""
    api = f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}"
    url = f"{api}/issues/{pull['number']}/labels"
    call(url, env["TRAIN_TOKEN"], "POST", {"labels": [REFRESH_LABEL]})
    name = urllib.parse.quote(REFRESH_LABEL, safe="")
    try:
        call(f"{url}/{name}", env["TRAIN_TOKEN"], "DELETE")
    except RefusedError as err:
        if "HTTP 404" not in str(err):
            raise


def hold_for_security(env, repo, rows, notes, conf):
    """Turn dependency updates into drafts while a security update is open; release them after.

    Returns the numbers held in this run: the rest of the run leaves them alone.
    """
    if repo["defaultBranchRef"] is None:
        return frozenset()
    fix, hold, release = security_hold(rows, repo["defaultBranchRef"]["name"], conf)
    api = f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}"
    for pull in hold:
        url = f"{api}/issues/{pull['number']}/labels"
        call(url, env["TRAIN_TOKEN"], "POST", {"labels": [SECURITY_HOLD_LABEL]})
        graphql(env, DRAFT, {"id": pull["id"]})
        notes[pull["number"]] = f"held as a draft: security update #{fix['number']} is open"
    for pull in release:
        if pull["isDraft"]:
            graphql(env, READY, {"id": pull["id"]})
        name = urllib.parse.quote(SECURITY_HOLD_LABEL, safe="")
        try:
            call(f"{api}/issues/{pull['number']}/labels/{name}", env["TRAIN_TOKEN"], "DELETE")
        except RefusedError as err:
            if "HTTP 404" not in str(err):
                raise
        notes[pull["number"]] = "released from the security hold"
    return frozenset(pull["number"] for pull in hold)


def bot_identity(env, conf, token):
    """The app's bot login and numeric id; the user lookup carries an installation token.

    Unauthenticated, it would share the runner address's 60 calls an hour.
    """
    app = call(f"{env['GITHUB_API_URL']}/app", app_jwt(env, conf), "GET")
    login = f"{app['slug']}[bot]"
    user = call(f"{env['GITHUB_API_URL']}/users/{login}", token, "GET")
    return login, str(user["id"])


def allowlist_paths(env, default_branch):
    """The paths declared in .github/generated-files on the default branch, or None."""
    url = (
        f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}/"
        f"contents/.github/generated-files?ref={default_branch}"
    )
    try:
        data = call(url, env["TRAIN_TOKEN"], "GET")
    except RefusedError as err:
        if "HTTP 404" in str(err):
            return None
        raise
    if not isinstance(data, dict) or "content" not in data:
        return None
    raw = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    lines = [line.split("#", 1)[0].strip() for line in raw.splitlines()]
    return frozenset(p for p in lines if p)


def find_artifact(env, pull):
    """The newest unexpired regenerated artifact matching the head commit and branch, or None."""
    head_sha = pull["headRefOid"]
    head_branch = pull["headRefName"]
    url = (
        f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}/"
        f"actions/artifacts?name=regenerated-{head_sha}&per_page=10"
    )
    answer = call(url, env["TRAIN_TOKEN"], "GET")
    artifacts = answer.get("artifacts", [])
    matching = []
    for art in artifacts:
        if art.get("expired"):
            continue
        run = art.get("workflow_run") or {}
        if run.get("head_sha") == head_sha and run.get("head_branch") == head_branch:
            matching.append(art)
    if not matching:
        return None
    matching.sort(key=lambda a: (a.get("created_at") or "", a.get("id") or 0), reverse=True)
    return matching[0]


def download_artifact(env, art):
    """Download the artifact zip without sending Authorization to the storage URL."""
    url = art["archive_download_url"]
    redirect = call(url, env["TRAIN_TOKEN"], "GET", redirect=False)
    storage_url = redirect.get("location")
    if not storage_url:
        raise RefusedError(f"GET {url}: expected 302 redirect to storage URL")
    expected_scheme = urllib.parse.urlsplit(env["GITHUB_API_URL"]).scheme
    storage_scheme = urllib.parse.urlsplit(storage_url).scheme
    if storage_scheme != expected_scheme:
        raise RefusedError(
            f"GET {url}: storage URL scheme {storage_scheme!r} != {expected_scheme!r}"
        )
    return call(storage_url, None, "GET", raw=True)


def check_zip_members(repo, num, zf):
    """Validate zip members against zip bombs and duplicates; return file member names."""
    namelist = zf.namelist()
    if len(namelist) != len(set(namelist)):
        raise RefusedError(f"{repo} #{num}: duplicate zip member names")
    file_members = set()
    total_size = 0
    for info in zf.infolist():
        if info.file_size < 0 or info.file_size > MAX_ARTIFACT_BYTES:
            raise RefusedError(
                f"{repo} #{num}: zip member {info.filename} exceeds {MAX_ARTIFACT_BYTES} bytes"
            )
        total_size += info.file_size
        if total_size > MAX_ARTIFACT_BYTES:
            raise RefusedError(f"{repo} #{num}: zip total size exceeds {MAX_ARTIFACT_BYTES} bytes")
        if info.filename.endswith("/"):
            if info.file_size > 0:
                raise RefusedError(f"{repo} #{num}: directory entry {info.filename!r} has size > 0")
        else:
            file_members.add(info.filename)
    return file_members


def check_safe_path(repo, num, p, allowlist):
    """Ensure path has no parent traversal, leading slash, backslash, and is allowlisted."""
    if "\\" in p or p.startswith("/") or ".." in p or p not in allowlist:
        raise RefusedError(f"{repo} #{num}: path {p!r} not allowed")


def check_manifest_entry(repo, num, entry, allowlist, seen_paths):
    """Validate one manifest file entry schema and allowlist path."""
    if not isinstance(entry, dict):
        raise RefusedError(f"{repo} #{num}: manifest files entry is not a dict")
    p, size, sha = entry.get("path"), entry.get("bytes"), entry.get("sha256")
    if not isinstance(p, str) or not isinstance(sha, str):
        raise RefusedError(f"{repo} #{num}: invalid manifest entry types")
    if type(size) is not int or size < 0:
        raise RefusedError(f"{repo} #{num}: invalid manifest entry types")
    if p in seen_paths:
        raise RefusedError(f"{repo} #{num}: duplicate path {p!r} in manifest files")
    seen_paths.add(p)
    check_safe_path(repo, num, p, allowlist)
    return p, size, sha


def read_manifest(repo, num, head, zf):
    """Read and validate top-level manifest.json from the zip archive."""
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception as err:
        raise RefusedError(f"{repo} #{num}: invalid manifest.json: {err}") from err
    if not isinstance(manifest, dict):
        raise RefusedError(f"{repo} #{num}: manifest.json root is not a dict")
    if manifest.get("schema") != "horistes.generated-files.v1":
        raise RefusedError(f"{repo} #{num}: unexpected manifest schema {manifest.get('schema')!r}")
    if manifest.get("head") != head:
        raise RefusedError(f"{repo} #{num}: manifest head {manifest.get('head')} != {head}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise RefusedError(f"{repo} #{num}: manifest files is not a list")
    return manifest, files


def extract_manifest_files(repo, num, zf, files, names, allowlist):
    """Validate and extract file members declared in manifest.json."""
    expected = {"manifest.json"}
    file_bytes = {}
    seen_paths = set()
    for entry in files:
        p, size, sha = check_manifest_entry(repo, num, entry, allowlist, seen_paths)
        member = f"files/{p}"
        if member not in names:
            raise RefusedError(f"{repo} #{num}: member {member!r} missing from artifact")
        expected.add(member)
        content = zf.read(member)
        if len(content) != size:
            raise RefusedError(f"{repo} #{num}: {p} size {len(content)} != {size}")
        if hashlib.sha256(content).hexdigest() != sha:
            raise RefusedError(f"{repo} #{num}: {p} sha256 mismatch")
        file_bytes[p] = content
    if names != expected:
        extra = sorted(names - expected)
        raise RefusedError(f"{repo} #{num}: artifact contains unexpected members: {extra}")
    return file_bytes


def validate_artifact(env, pull, data, allowlist):
    """Validate zip content and manifest, returning (manifest, file_bytes_by_path)."""
    repo = env["GITHUB_REPOSITORY"]
    num = pull["number"]
    head = pull["headRefOid"]
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as err:
        raise RefusedError(f"{repo} #{num}: bad zip archive: {err}") from err
    names = check_zip_members(repo, num, zf)
    if "manifest.json" not in names:
        raise RefusedError(f"{repo} #{num}: manifest.json missing from artifact")
    manifest, files = read_manifest(repo, num, head, zf)
    file_bytes = extract_manifest_files(repo, num, zf, files, names, allowlist)
    return manifest, file_bytes


def filter_changed_files(env, pull, file_bytes):
    """Files from the artifact whose content differs from headRefOid."""
    changed = {}
    for path, data in file_bytes.items():
        url = (
            f"{env['GITHUB_API_URL']}/repos/{env['GITHUB_REPOSITORY']}/"
            f"contents/{path}?ref={pull['headRefOid']}"
        )
        try:
            head_file = call(url, env["TRAIN_TOKEN"], "GET")
        except RefusedError as err:
            if "HTTP 404" in str(err):
                head_file = None
            else:
                raise
        blob_sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        if head_file and head_file.get("sha") == blob_sha:
            continue
        changed[path] = data
    return changed


def commit_generated_files(env, conf, pull, art, changed):
    """Commit changed generated files to the pull request branch via createCommitOnBranch."""
    run = art.get("workflow_run") or {}
    run_ref = run.get("html_url") or run.get("id") or "unknown"
    bot_login = conf["bot_login"]
    bot_id = conf["bot_id"]
    trailer = f"Signed-off-by: {bot_login} <{bot_id}+{bot_login}@users.noreply.github.com>"
    body = (
        f"Regenerated by the dependency update's own workflow run {run_ref}, "
        f"artifact regenerated-{pull['headRefOid']}.\n\n{trailer}"
    )
    additions = [
        {"path": path, "contents": base64.b64encode(data).decode("ascii")}
        for path, data in sorted(changed.items())
    ]
    variables = {
        "input": {
            "branch": {
                "repositoryNameWithOwner": env["GITHUB_REPOSITORY"],
                "branchName": pull["headRefName"],
            },
            "expectedHeadOid": pull["headRefOid"],
            "fileChanges": {"additions": additions},
            "message": {
                "headline": "chore(deps): regenerate generated files",
                "body": body,
            },
        }
    }
    data = graphql(env, COMMIT_MUTATION, variables)
    if not data.get("createCommitOnBranch", {}).get("commit", {}).get("oid"):
        raise RefusedError(f"#{pull['number']}: createCommitOnBranch returned no commit")


def is_candidate(pull, reason, default_branch, conf):
    """Whether a pull request is a candidate for generated file regeneration."""
    if not pull["headRefName"].startswith(conf["dependency"]):
        return False
    if pull["isDraft"] or pull["isCrossRepository"]:
        return False
    if pull["baseRefName"] != default_branch:
        return False
    if reason.startswith("labelled "):
        return False
    return reason != "release or reserved branch: a person lands it"


def regenerate(env, conf, repo, rows, notes):
    """Commit regenerated files for one candidate dependency update, or None.

    Security updates go first, then the head that has waited longest: a head the
    update tool or the train pushed since is served after one never served.
    """
    if repo["defaultBranchRef"] is None:
        # An empty repository has no default branch and no pull requests.
        return None
    default_branch = repo["defaultBranchRef"]["name"]
    candidates = [pull for pull, reason in rows if is_candidate(pull, reason, default_branch, conf)]
    candidates.sort(key=head_since)
    candidates = [pull for pull, _ in serving_order([(pull, None) for pull in candidates], conf)]
    if not candidates:
        return None
    allowlist = allowlist_paths(env, default_branch)
    if allowlist is None:
        return None
    for pull in candidates:
        try:
            art = find_artifact(env, pull)
            if not art:
                continue
            data = download_artifact(env, art)
            _, file_bytes = validate_artifact(env, pull, data, allowlist)
            changed = filter_changed_files(env, pull, file_bytes)
        except RefusedError as err:
            notes[pull["number"]] = f"generated files refused: {err}"[:200]
            continue
        if not changed:
            continue
        commit_generated_files(env, conf, pull, art, changed)
        notes[pull["number"]] = "generated files committed"
        return f"committed generated files to #{pull['number']}"
    return None


def re_arm_drifted(env, conf, served, held, notes):
    """Re-arm an armed squash pull request whose headline drifted from its title."""
    for pull, _ in served:
        if pull["number"] not in held and drifted(pull, conf):
            graphql(env, DISARM, {"id": pull["id"]})
            arm(env, conf, pull)
            notes[pull["number"]] = "re-armed: title changed after arming"
            return f"re-armed #{pull['number']}"
    return None


def land_clean(env, conf, repo, served, notes):
    """Land or enqueue the oldest clean pull request, or None."""
    for pull, reason in served:
        if reason == "land":
            try:
                done = notes[pull["number"]] = land(env, conf, pull, repo.get("mergeQueue"))
            except UnsignedBotError:
                notes[pull["number"]] = SKIP_UNSIGNED
                continue
            return f"{done} #{pull['number']}"
    return None


def lander_lease(repo, now):
    """The live lander lease expiry stamp or None; raise RefusedError on invalid lease."""
    try:
        lease = lease_until(repo, now)
        return lease
    except ValueError as err:
        raise RefusedError(f"{err}") from err


def run(env, conf, repo, now):
    pulls = repo["pullRequests"]["nodes"]
    replaced = superseded(pulls)
    rows = [(pull, verdict(pull, repo, conf, now, replaced)) for pull in pulls]
    notes = {pull["number"]: reason for pull, reason in rows}
    if conf["switch"] == "off":
        return rows, notes, "switched off: nothing landed or updated"
    lease = lander_lease(repo, now)
    served = serving_order(rows, conf)
    held = hold_for_security(env, repo, served, notes, conf)
    served = [
        (pull, f"labelled {SECURITY_HOLD_LABEL}" if pull["number"] in held else reason)
        for pull, reason in served
    ]
    if lease:
        served = yield_to_lander(served, notes, conf, lease)
    outcome = regenerate(env, conf, repo, served, notes)
    if outcome:
        return rows, notes, outcome
    re_armed = re_arm_drifted(env, conf, served, held, notes)
    if re_armed:
        return rows, notes, re_armed
    landed_pr = land_clean(env, conf, repo, served, notes)
    if landed_pr:
        return rows, notes, landed_pr
    holding = [pull["number"] for pull, reason in served if in_flight(pull, reason)]
    if holding:
        return rows, notes, f"#{holding[0]} is armed and in flight"
    outcome = refresh(
        served, notes, lambda run: run_jobs(env, run), lambda pull: toggle_refresh(env, pull)
    )
    if outcome:
        return rows, notes, outcome
    advance(env, conf, repo, served, notes)
    outcome = (
        f"the lander holds the landing order until {lease}"
        if lease
        else "no pull request at a mergeable state"
    )
    return rows, notes, outcome


def report(name, repo, rows, notes, outcome):
    """The section of one repository with open pull requests."""
    lines = [
        f"### {name}: {outcome}",
        "",
        "| Pull request | Merge state | Train |",
        "|---|---|---|",
    ]
    lines += [
        f"| #{pull['number']} | {pull['mergeStateStatus']} | {notes[pull['number']]} |"
        for pull, _ in rows
    ]
    if repo["pullRequests"]["pageInfo"]["hasNextPage"]:
        lines.append("\nOnly the 100 oldest open pull requests were read.")
    return "\n".join(lines) + "\n"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(env, conf):
    """A JWT of the app, signed RS256 by openssl; the key goes through a pipe, never a file."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise RefusedError("openssl is not on the runner: the app's JWT cannot be signed")
    now = int(time.time())
    claims = {"iat": now - 60, "exp": now + JWT_SECONDS, "iss": conf["client_id"]}
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    signing_input = f"{head}.{b64url(json.dumps(claims).encode())}"
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "w") as pipe:
        pipe.write(env["TRAIN_APP_KEY"])
    try:
        done = subprocess.run(
            [openssl, "dgst", "-sha256", "-sign", f"/dev/fd/{read_end}"],
            input=signing_input.encode(),
            capture_output=True,
            pass_fds=(read_end,),
            timeout=TIMEOUT,
            check=False,
        )
    finally:
        os.close(read_end)
    if done.returncode != 0 or not done.stdout:
        raise RefusedError(f"openssl could not sign the app's JWT (exit {done.returncode})")
    return f"{signing_input}.{b64url(done.stdout)}"


def pages(env, token, path, key=None):
    """Every item of a paginated list, at most MAX_PAGES pages of 100."""
    items = []
    for page in range(1, MAX_PAGES + 1):
        answer = call(f"{env['GITHUB_API_URL']}{path}?per_page=100&page={page}", token, "GET")
        batch = answer[key] if key else answer
        items += batch
        if len(batch) < 100:
            return items
    raise RefusedError(f"GET {path}: more than {MAX_PAGES * 100} entries")


def mint(env, conf, installation, rights, repositories=None):
    """An installation token with exactly these rights, for these repositories only."""
    body = {"permissions": rights}
    if repositories is not None:
        body["repositories"] = repositories
    url = f"{env['GITHUB_API_URL']}/app/installations/{installation['id']}/access_tokens"
    try:
        return call(url, app_jwt(env, conf), "POST", body)["token"]
    except RefusedError as err:
        if "HTTP 422 " in str(err) and UNGRANTED in str(err):
            raise UngrantedError(str(err)) from err
        raise


def revoke(env, token):
    call(f"{env['GITHUB_API_URL']}/installation/token", token, "DELETE")


def repositories(env, conf, installation, identity):
    """The installation's repositories the train serves, read with a read-only token.

    The first call fills identity with the bot login and id, using that token.
    """
    token = mint(env, conf, installation, LIST_RIGHTS)
    try:
        if not identity:
            identity["bot_login"], identity["bot_id"] = bot_identity(env, conf, token)
        listed = pages(env, token, "/installation/repositories", "repositories")
    finally:
        revoke(env, token)
    return [r for r in listed if not r.get("archived") and not r.get("disabled")]


def close_issues(env, conf, installation, target, now):
    """Close the open issues that pull requests the app merged name as closed; the lines to report.

    The token for it carries issues write and nothing else, and is minted only when
    an issue is to be closed.
    """
    owner, name = target["full_name"].split("/", 1)
    data = graphql(env, MERGED, {"owner": owner, "name": name})
    merged = data["repository"]["pullRequests"]["nodes"]
    bot = conf["bot_login"].removesuffix("[bot]")
    since = now - timedelta(days=CLOSE_DAYS)
    found = issues_to_close(merged, target["full_name"], bot, since)
    if not found:
        return []
    try:
        token = mint(env, conf, installation, ISSUE_RIGHTS, [target["name"]])
    except RefusedError as err:
        numbers = ", ".join(f"#{issue}" for _, _, issue in found)
        raise RefusedError(f"issues {numbers} stay open, no issues write: {err}") from err
    url = f"{env['GITHUB_API_URL']}/repos/{target['full_name']}/issues"
    lines = []
    try:
        for number, oid, issue in found:
            text = f"Closed by #{number}, which the merge train merged as {oid}."
            call(f"{url}/{issue}/comments", token, "POST", {"body": text})
            call(f"{url}/{issue}", token, "PATCH", {"state": "closed", "state_reason": "completed"})
            lines.append(f"Closed issue #{issue}: #{number} merged as {oid[:7]}.")
    finally:
        revoke(env, token)
    return lines


def one(env, conf, installation, target, now):
    """The train in one repository, with a token for that repository alone."""
    rights = WRITE_RIGHTS if conf["switch"] == "on" else READ_RIGHTS
    token = mint(env, conf, installation, rights, [target["name"]])
    local = {**env, "GITHUB_REPOSITORY": target["full_name"], "TRAIN_TOKEN": token}
    closed = []
    try:
        repo = fetch(local)
        rows, notes, outcome = run(local, conf, repo, now)
        if conf["switch"] == "on":
            closed = close_issues(local, conf, installation, target, now)
    finally:
        revoke(env, token)
    moved = any(
        note.startswith(("armed", "re-armed", "updated", "queued", "refreshed", "held", "released"))
        or note == "generated files committed"
        for note in notes.values()
    )
    section = report(target["full_name"], repo, rows, notes, outcome) if rows else ""
    if closed:
        section = section or f"### {target['full_name']}: {outcome}\n"
        section += "\n" + "\n".join(closed) + "\n"
    return outcome, section, moved, bool(repo["releaseConfig"] and repo["releaseManifest"])


def sweep(env, conf, started):
    """Run the train in every repository; the per-account tally, the sections, the refusals."""
    accounts, sections, refusals, skipped, releases, off, identity = {}, [], [], [], [], [], {}
    for installation in pages(env, app_jwt(env, conf), "/app/installations"):
        account = installation["account"]["login"]
        if installation.get("suspended_at"):
            accounts[account] = "suspended"
            continue
        try:
            targets = repositories(env, conf, installation, identity)
        except RefusedError as err:
            refusals.append(f"{account}: {err}")
            accounts[account] = "refused"
            continue
        tally = {"repositories": 0, "landed": 0, "updated": 0, "refused": 0}
        accounts[account] = tally
        for index, target in enumerate(targets):
            if time.monotonic() - started > SWEEP_SECONDS:
                skipped.append(target["full_name"])
                continue
            tally["repositories"] += 1
            try:
                now = datetime.now(UTC)
                outcome, section, moved, release = one(
                    env, {**conf, **identity}, installation, target, now
                )
            except UngrantedError as err:
                rest = len(targets) - index
                tally["repositories"] += rest - 1
                tally["refused"] += rest
                refusals.append(
                    f"{account}: installation {installation['id']} has not accepted the rights "
                    f"the train asks for; {rest} repositories skipped: {err}"
                )
                break
            except RefusedError as err:
                tally["refused"] += 1
                refusals.append(f"{target['full_name']}: {err}")
                continue
            tally["landed"] += outcome.startswith("landed")
            tally["updated"] += moved
            sections.append(section)
            if release:
                if target["full_name"].lower() in conf["release_off"]:
                    off.append(target["full_name"])
                else:
                    releases.append({"owner": account, "name": target["name"]})
    return accounts, [s for s in sections if s], refusals, skipped, releases, off


def append(path, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def summary(env, accounts, sections, refusals, skipped, releases, off):
    lines = [
        "## Merge train sweep",
        "",
        "| Account | Repositories | Landed | Updated | Refused |",
        "|---|---|---|---|---|",
    ]
    for account, tally in sorted(accounts.items(), key=lambda item: item[0].lower()):
        if isinstance(tally, str):
            lines.append(f"| {account} | {tally} | | | |")
        else:
            lines.append(
                f"| {account} | {tally['repositories']} | {tally['landed']} "
                f"| {tally['updated']} | {tally['refused']} |"
            )
    if refusals:
        lines += ["", "Refused:", ""] + [f"- {line}" for line in refusals]
    if skipped:
        lines += ["", f"Not reached within {SWEEP_SECONDS} s: {', '.join(skipped)}"]
    if len(releases) > MAX_RELEASES:
        late = ", ".join(f"{r['owner']}/{r['name']}" for r in releases[MAX_RELEASES:])
        lines += ["", f"No release run, the job matrix holds {MAX_RELEASES}: {late}"]
        del releases[MAX_RELEASES:]
    if off:
        lines += ["", f"Release train off: {', '.join(off)}"]
    named = ", ".join(f"{r['owner']}/{r['name']}" for r in releases) or "none"
    lines += ["", f"Release pull requests and releases next: {named}"]
    text = "\n".join([*lines, "", *sections]) + "\n"
    sys.stdout.write(text)
    append(env["GITHUB_STEP_SUMMARY"], text)
    append(env["GITHUB_OUTPUT"], f"releases={json.dumps(releases, separators=(',', ':'))}\n")


def main(env):
    try:
        conf = settings(env)
    except InvalidSettingError as err:
        sys.stdout.write(f"::error title=Merge train setting::{err}\n")
        return 3
    try:
        accounts, sections, refusals, skipped, releases, off = sweep(env, conf, time.monotonic())
    except RefusedError as err:
        sys.stdout.write(f"::error title=Merge train refused::{err}\n")
        return 1
    summary(env, accounts, sections, refusals, skipped, releases, off)
    for line in refusals:
        sys.stdout.write(f"::error title=Merge train refused::{line}\n")
    if skipped:
        sys.stdout.write(f"::warning title=Merge train::{len(skipped)} repositories not reached\n")
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main(os.environ))
