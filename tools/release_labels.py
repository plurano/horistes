# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Release settings and label handling for release-please."""

import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_workflows import MAX_RELEASES, local_release_workflows, workflow_runs_release

PENDING_LABEL = "autorelease: pending"
TAGGED_LABEL = "autorelease: tagged"
RELEASE_HEAD = "release-please--"
RELEASE_LABEL = "autorelease:"
TIMEOUT = 30
MAX_BYTES = 1024 * 1024
READ_ATTEMPTS = 3
READ_BACKOFF = 1
# Only the fields the release checks read: the REST pull list carries two full repository
# objects per pull, about 480 kB for 20 pulls, and the forge cut that body short in 3 of 10
# reads. The order is that of the REST list: newest created first.
PULLS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    closed: pullRequests(
      states: [CLOSED, MERGED], first: 20, orderBy: {field: CREATED_AT, direction: DESC}
    ) { nodes { ...release } }
    open: pullRequests(
      states: [OPEN], first: 100, orderBy: {field: CREATED_AT, direction: DESC}
    ) { nodes { ...release } }
  }
}
fragment release on PullRequest {
  number state title mergedAt baseRefName headRefName
  mergeCommit { oid }
  labels(first: 100) { nodes { name } }
}
"""


class CallError(Exception):
    """A forge call failed or returned an error status; `status` is the HTTP code, if any."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class URLRefusedError(CallError):
    """A request or redirect URL is outside the transport allow-list."""


def allowed_url(url):
    """Allow lower-case HTTPS, or HTTP to the loopback stand-in only."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    scheme = url.partition(":")[0]
    return bool(host) and (
        scheme == "https" or (scheme == "http" and host in ("127.0.0.1", "localhost", "::1"))
    )


class AllowedRedirect(urllib.request.HTTPRedirectHandler):
    """Check every redirect target before urllib can open it."""

    def http_error_302(self, req, fp, code, msg, headers):
        target = headers.get("location", headers.get("uri"))
        if target is not None and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
            target = urllib.parse.urljoin(req.full_url, target)
        if target is not None and not allowed_url(target):
            fp.close()
            raise URLRefusedError("redirect URL refused: HTTPS or loopback HTTP required")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _read_once(req, method, url, status_only):
    """Execute one attempt of an HTTP request, decoding JSON or returning raw body."""
    try:
        opener = urllib.request.build_opener(AllowedRedirect)
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise CallError(f"{method} {url}: body exceeds {MAX_BYTES} bytes")
            # read(amt) returns a body the peer closed early without an error; the length names it.
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and len(raw) != int(length):
                raise http.client.IncompleteRead(raw, int(length) - len(raw))
            if status_only:
                return resp.status, raw
            return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as err:
        if status_only:
            return err.code, b""
        raw = err.read(MAX_BYTES + 1)
        detail = raw.decode("utf-8", errors="replace")[:200]
        raise CallError(f"{method} {url}: HTTP {err.code}: {detail}", err.code) from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise CallError(f"{method} {url}: connection error: {err}") from err


def call(url, token, method="GET", body=None, status_only=False, read_only=False):
    """Execute one HTTP request to the forge, with bounded read and timeout."""
    if not allowed_url(url):
        raise URLRefusedError("call URL refused: HTTPS or loopback HTTP required")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    can_retry = (method == "GET" or read_only) and not status_only
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return _read_once(req, method, url, status_only)
        except (json.JSONDecodeError, UnicodeDecodeError, http.client.IncompleteRead) as err:
            what, detail = "invalid JSON response", str(err)
            if isinstance(err, http.client.IncompleteRead):
                whole = len(err.partial) + (err.expected or 0)
                what, detail = "body cut", f"{len(err.partial)} of {whole} bytes read"
            if not can_retry:
                raise CallError(f"{method} {url}: {what}: {detail}") from err
            if attempt < READ_ATTEMPTS:
                time.sleep(READ_BACKOFF * attempt)
                continue
            raise CallError(
                f"{method} {url}: {what} after {READ_ATTEMPTS} attempts: {detail}"
            ) from err


def read_config(api_url, repo, token):
    """Read and parse release-please-config.json via the contents API."""
    url = f"{api_url}/repos/{repo}/contents/release-please-config.json"
    _, data = call(url, token, method="GET")
    raw = data.get("content", "")
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except ValueError as err:
        raise CallError(f"parse release-please-config.json: {err}") from err


def read_default_branch(api_url, repo, token):
    """The default branch; missing data is a call error."""
    _, repository = call(f"{api_url}/repos/{repo}", token, method="GET")
    default_branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(default_branch, str) or not default_branch:
        raise CallError("repository default_branch must be a non-empty string")
    return default_branch


def reads_own_release(api_url, repo, token):
    """The workflow file name running release-please in this repository, or False."""
    url = f"{api_url}/repos/{repo}/contents/.github/workflows"
    try:
        _, entries = call(url, token, method="GET")
    except CallError as err:
        if err.status == 404:
            return False
        raise
    if not isinstance(entries, list):
        return False
    workflows = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "file"
        and isinstance(entry.get("name"), str)
        and (entry["name"].endswith(".yml") or entry["name"].endswith(".yaml"))
    ]
    if len(workflows) > 100:
        raise CallError(f"workflow count {len(workflows)} exceeds bound of 100")
    if not workflows:
        return False
    default_branch = read_default_branch(api_url, repo, token)
    contents = {}
    for entry in workflows[:100]:
        name = entry["name"]
        _, data = call(entry.get("url") or f"{url}/{name}", token, method="GET")
        raw = data.get("content", "") if isinstance(data, dict) else ""
        try:
            contents[name] = base64.b64decode(raw).decode("utf-8")
        except ValueError as err:
            raise CallError(f"decode {name}: {err}") from err
    local = local_release_workflows(contents)
    for name, content in contents.items():
        if workflow_runs_release(content, local, default_branch):
            return name
    return False


def evaluate_skip(config):
    """Determine effective skip-github-release across packages."""
    top = config.get("skip-github-release", False)
    packages = config.get("packages")
    if not packages or not isinstance(packages, dict):
        return bool(top)
    pkg_skips = {}
    for name, pkg in sorted(packages.items())[:100]:
        val = pkg.get("skip-github-release", top) if isinstance(pkg, dict) else top
        pkg_skips[name] = bool(val)
    values = set(pkg_skips.values())
    if len(values) > 1:
        details = ", ".join(f"{k}={v}" for k, v in pkg_skips.items())
        raise ValueError(f"mixed skip-github-release across packages: {details}")
    return next(iter(values))


def package_settings(config, component):
    """The settings of the package a release branch names, over the top-level ones."""
    packages = config.get("packages") if isinstance(config.get("packages"), dict) else {}
    entries = [p for p in list(packages.values())[:100] if isinstance(p, dict)]
    named = [
        p for p in entries if component and component in (p.get("component"), p.get("package-name"))
    ]
    package = named[0] if named else entries[0] if len(entries) == 1 else {}
    keys = (
        "include-component-in-tag",
        "include-v-in-tag",
        "tag-separator",
        "pull-request-title-pattern",
    )
    return {key: package.get(key, config.get(key)) for key in keys}


def release_tag(pull, config):
    """The tag release-please gives a merged release pull request, or None for any other pull."""
    base_ref = (pull.get("base") or {}).get("ref") or ""
    head_ref = (pull.get("head") or {}).get("ref") or ""
    # release-please's branch names: release-please--branches--<base>[--components--<component>]
    branch = re.fullmatch(
        rf"release-please--branches--{re.escape(base_ref)}(?:--components--(.+))?", head_ref
    )
    if not base_ref or not branch:
        return None
    component = branch.group(1) or ""
    settings = package_settings(config, component)
    if settings["pull-request-title-pattern"]:
        return None
    # release-please's default title pattern, chore${scope}: release${component} ${version}.
    title = re.fullmatch(
        r"chore(?:\((?P<branch>[\w./-]+)\))?: release ?(?:@?[\w./-]*)? "
        r"v?(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)",
        pull.get("title") or "",
    )
    if not title or title.group("branch") not in (None, base_ref):
        return None
    prefix = "v" if settings["include-v-in-tag"] is not False else ""
    if component and settings["include-component-in-tag"] is not False:
        separator = settings["tag-separator"] if isinstance(settings["tag-separator"], str) else "-"
        prefix = f"{component}{separator}{prefix}"
    return prefix + title.group("version")


def parse_release_pull(pull, config):
    """Validate a merged release pull request and return item dict or None."""
    labels = {lbl.get("name") for lbl in pull.get("labels", []) if isinstance(lbl, dict)}
    if PENDING_LABEL not in labels:
        return None
    num = pull.get("number")
    sha = pull.get("merge_commit_sha") or ""
    tag = release_tag(pull, config)
    if not (pull.get("merged_at") and tag and re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
        sys.stdout.write(f"::warning::pull #{num} labelled {PENDING_LABEL} fails release checks\n")
        return None
    return {"number": num, "sha": sha, "tag": tag}


def as_rest_pull(node):
    """A GraphQL pull request node in the shape of the REST pulls API the checks read."""
    return {
        "number": node.get("number"),
        "state": str(node.get("state") or "").lower(),
        "title": node.get("title"),
        "merged_at": node.get("mergedAt"),
        "merge_commit_sha": (node.get("mergeCommit") or {}).get("oid"),
        "base": {"ref": node.get("baseRefName") or ""},
        "head": {"ref": node.get("headRefName") or ""},
        "labels": list(((node.get("labels") or {}).get("nodes") or [])[:100]),
    }


def read_pulls(graphql_url, repo, token):
    """The 20 newest closed and the 100 newest open pull requests, needed fields only."""
    owner, _, name = repo.partition("/")
    body = {"query": PULLS_QUERY, "variables": {"owner": owner, "name": name}}
    _, answer = call(graphql_url, token, method="POST", body=body, read_only=True)
    data = answer.get("data") if isinstance(answer, dict) else None
    found = (data or {}).get("repository")
    if not isinstance(found, dict) or answer.get("errors"):
        errors = answer.get("errors") if isinstance(answer, dict) else answer
        raise CallError(f"POST {graphql_url}: pull request read failed: {json.dumps(errors)[:500]}")
    lists = {}
    for key, limit in (("closed", 20), ("open", 100)):
        nodes = (found.get(key) or {}).get("nodes")
        if not isinstance(nodes, list):
            raise CallError(f"POST {graphql_url}: expected list of {key} pulls")
        lists[key] = [as_rest_pull(node) for node in nodes[:limit] if isinstance(node, dict)]
    return lists["closed"], lists["open"]


def read_pending(pulls, config):
    """List merged release pull requests still carrying autorelease: pending."""
    pending = []
    for pull in pulls[:20]:
        item = parse_release_pull(pull, config)
        if item is not None:
            pending.append(item)
    return pending


def read_frozen(pulls, hold):
    """Open release pull requests a person froze: a hold label or a release label past pending."""
    frozen = []
    for pull in pulls[:100]:
        if pull.get("state") != "open":
            continue
        if not pull.get("head", {}).get("ref", "").startswith(RELEASE_HEAD):
            continue
        labels = {lbl.get("name", "") for lbl in pull.get("labels", []) if isinstance(lbl, dict)}
        release = {n for n in labels if n.startswith(RELEASE_LABEL) and n != PENDING_LABEL}
        named = sorted((labels & hold) | release)
        if named:
            frozen.append(f"#{pull['number']} ({', '.join(named)})")
    return frozen


def cmd_settings(env):
    """Read release settings and emit skip and pending outputs."""
    api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = env["REPO"]
    token = env["TOKEN"]
    config = read_config(api_url, repo, token)
    try:
        skip = evaluate_skip(config) or reads_own_release(api_url, repo, token)
    except ValueError as err:
        sys.stdout.write(f"::error::{err}\n")
        return 1
    if isinstance(skip, str):
        sys.stdout.write(
            f"::notice::{repo} runs release-please in its own workflow {skip}; "
            "the train creates no release\n"
        )
    closed, opened = read_pulls(env.get("GITHUB_GRAPHQL_URL", f"{api_url}/graphql"), repo, token)
    pending = read_pending(closed, config)
    hold = frozenset(name for name in env.get("HOLD_LABELS", "").split(",") if name)
    frozen = read_frozen(opened, hold)
    for item in frozen:
        sys.stdout.write(f"::notice::release pull request {item} is frozen; not updated\n")
    out_file = env.get("GITHUB_OUTPUT")
    if out_file:
        skip_str = "true" if skip else "false"
        pending_str = json.dumps(pending, separators=(",", ":"))
        frozen_str = "true" if frozen else "false"
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(f"skip={skip_str}\npending={pending_str}\nfrozen={frozen_str}\n")
    return 0


def get_tag_commit(api_url, repo, token, tag):
    """Resolve the commit SHA for a tag ref, unwrapping annotated tags once."""
    tag_quoted = urllib.parse.quote(tag, safe="")
    url = f"{api_url}/repos/{repo}/git/matching-refs/tags/{tag_quoted}"
    try:
        _, entries = call(url, token, method="GET")
    except CallError as err:
        if err.status == 404:
            return None
        raise
    if not isinstance(entries, list):
        return None
    if len(entries) >= 30:
        raise CallError(f"matching-refs for {tag}: {len(entries)} entries (>= 30)")
    exact = f"refs/tags/{tag}"
    entry = None
    for item in entries[:30]:
        if item.get("ref") == exact:
            entry = item
            break
    if not entry:
        return None
    obj = entry.get("object", {})
    obj_type = obj.get("type")
    sha = obj.get("sha")
    if obj_type == "commit":
        return sha
    if obj_type == "tag" and sha:
        tag_url = f"{api_url}/repos/{repo}/git/tags/{sha}"
        _, tag_data = call(tag_url, token, method="GET")
        target_obj = tag_data.get("object", {})
        if target_obj.get("type") == "commit":
            return target_obj.get("sha")
    return None


def move_pending_labels(api_url, repo, token, pending):
    """For each pending item with tag on commit: add tagged label, then delete pending."""
    for item in pending[:100]:
        commit = get_tag_commit(api_url, repo, token, item["tag"])
        if commit == item["sha"]:
            num = item["number"]
            post_url = f"{api_url}/repos/{repo}/issues/{num}/labels"
            call(post_url, token, method="POST", body={"labels": [TAGGED_LABEL]})
            del_quoted = urllib.parse.quote(PENDING_LABEL, safe="")
            del_url = f"{api_url}/repos/{repo}/issues/{num}/labels/{del_quoted}"
            call(del_url, token, method="DELETE")
        else:
            sys.stdout.write(
                f"::notice::pull #{item['number']} stays pending (tag not yet on merge commit)\n"
            )
    return 0


def judge_failed_release(api_url, repo, token, pending):
    """Judge a failed release-please run when releases were not skipped."""
    if not pending:
        sys.stdout.write("::error::release step failed and pending is empty\n")
        return 1
    for item in pending[:100]:
        pull_url = f"{api_url}/repos/{repo}/pulls/{item['number']}"
        _, pull = call(pull_url, token, method="GET")
        labels = {lbl.get("name") for lbl in pull.get("labels", []) if isinstance(lbl, dict)}
        if PENDING_LABEL in labels:
            sys.stdout.write(f"::error::pull #{item['number']} still carries {PENDING_LABEL}\n")
            return 1
        commit = get_tag_commit(api_url, repo, token, item["tag"])
        if commit != item["sha"]:
            sys.stdout.write(f"::error::tag {item['tag']} commit {commit} != {item['sha']}\n")
            return 1
        tag_quoted = urllib.parse.quote(item["tag"], safe="")
        rel_url = f"{api_url}/repos/{repo}/releases/tags/{tag_quoted}"
        status, _ = call(rel_url, token, method="GET", status_only=True)
        if status != 200:
            sys.stdout.write(f"::error::release {item['tag']} returned HTTP {status}\n")
            return 1
    for item in pending[:100]:
        sys.stdout.write(f"::notice::release {item['tag']} existed on the release commit; done\n")
    return 0


def cmd_after(env):
    """Move release labels or judge a failed release run."""
    api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    repo = env["REPO"]
    token = env["TOKEN"]
    skip = env.get("SKIP", "false").lower() == "true"
    outcome = env.get("OUTCOME", "")
    raw_pending = env.get("PENDING", "[]")
    pending = json.loads(raw_pending) if raw_pending else []
    if skip:
        return move_pending_labels(api_url, repo, token, pending)
    if outcome in ("success", "skipped"):
        return 0
    if outcome == "failure":
        return judge_failed_release(api_url, repo, token, pending)
    sys.stdout.write(f"::error::unexpected release outcome: {outcome}\n")
    return 1


MAX_RELEASE_PARALLEL = 3
PLAN_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
PLAN_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


def check_release_parallel(raw):
    """Release jobs at once, a whole number from 1 to 3 (HISS-06)."""
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= MAX_RELEASE_PARALLEL:
        raise ValueError(
            f"release_max_parallel is {raw!r}, not a whole number from 1 to {MAX_RELEASE_PARALLEL}"
        )
    return int(raw)


def check_plan_repository(item):
    """Allow only repository identifiers safe for a matrix and a token scope."""
    if not isinstance(item, dict) or set(item) != {"owner", "name"}:
        raise CallError("release repository must contain exactly owner and name")
    owner, name = item["owner"], item["name"]
    if not isinstance(owner, str) or not PLAN_OWNER.fullmatch(owner):
        raise CallError("release repository owner is invalid (1..39 letters, digits or hyphens)")
    if not isinstance(name, str) or not PLAN_NAME.fullmatch(name) or name in (".", ".."):
        raise CallError("release repository name is invalid (1..100 letters, digits, dot, _ or -)")
    return (owner.casefold(), name.casefold())


def check_plan_hold(hold):
    """A bounded single-line output value, preserved exactly."""
    if not isinstance(hold, str) or len(hold) > 4096 or "\n" in hold or "\r" in hold:
        raise CallError("release hold_labels must be a single line of at most 4096 characters")
    return hold


def check_release_plan(data):
    """Validate the versioned artifact with exact keys and scalar bounds."""
    if not isinstance(data, dict) or set(data) != {"format", "hold_labels", "releases"}:
        raise CallError("release list must contain exactly format, hold_labels and releases")
    if type(data["format"]) is not int or data["format"] != 1:
        raise CallError("release list format must be 1")
    hold = check_plan_hold(data["hold_labels"])
    releases = data["releases"]
    if not isinstance(releases, list) or len(releases) > MAX_RELEASES:
        raise CallError(f"release list must be a list of at most {MAX_RELEASES} repositories")
    seen = set()
    for item in releases[:MAX_RELEASES]:
        key = check_plan_repository(item)
        if key in seen:
            raise CallError("release list contains a duplicate repository (case-insensitive)")
        seen.add(key)
    return releases, hold


def write_plan_output(env, releases, hold):
    """One compact output line per value; validation disallows line injection."""
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(
            f"releases={json.dumps(releases, separators=(',', ':'))}\nhold_labels={hold}\n"
        )
    return 0


def read_release_plan(path):
    """Read a bounded local artifact; decode and I/O errors retain their context."""
    try:
        with open(path, encoding="utf-8") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CallError(f"release list exceeds {MAX_BYTES} characters")
        return check_release_plan(json.loads(raw))
    except (OSError, UnicodeError, ValueError) as err:
        raise CallError(f"read release list: {err}") from err


def cmd_plan(env):
    """Accept only completed default-branch train runs before consuming their artifact."""
    conclusion = env["RUN_CONCLUSION"]
    if conclusion not in {"success", "failure"}:
        sys.stdout.write(f"::notice::train run conclusion {conclusion!r}; no release legs\n")
        return write_plan_output(env, [], "")
    if env["RUN_EVENT"] not in {"schedule", "workflow_dispatch"}:
        raise CallError("train run event must be schedule or workflow_dispatch")
    if env["RUN_HEAD_BRANCH"] != env["DEFAULT_BRANCH"]:
        raise CallError("train run head branch must be the default branch")
    if env["RUN_REPOSITORY"].casefold() != env["REPOSITORY"].casefold():
        raise CallError("train run repository must be the caller repository")
    try:
        check_release_parallel(env["MAX_PARALLEL"])
    except ValueError as err:
        sys.stdout.write(f"::error title=Release plan setting::{err}\n")
        return 3
    if not Path(env["FILE"]).exists():
        sys.stdout.write(
            "::notice::train run uploaded no release list "
            "(switch off, or the sweep ended before its summary)\n"
        )
        return write_plan_output(env, [], "")
    releases, hold = read_release_plan(env["FILE"])
    if not releases:
        sys.stdout.write("::notice::no repository named in the release list\n")
    return write_plan_output(env, releases, hold)


def main(argv, env):
    """Dispatch plan, settings or after command."""
    if len(argv) < 2:
        sys.stderr.write("usage: release_labels.py <plan|settings|after>\n")
        return 2
    commands = {"plan": cmd_plan, "settings": cmd_settings, "after": cmd_after}
    if argv[1] not in commands:
        sys.stderr.write(f"unknown command: {argv[1]}\n")
        return 2
    try:
        return commands[argv[1]](env)
    except URLRefusedError as err:
        sys.stderr.write(f"::error title=Release labels::{argv[1]}: {err}\n")
        return 1
    except (CallError, KeyError, OSError, json.JSONDecodeError) as err:
        sys.stdout.write(f"::error title=Release labels::{argv[1]}: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
