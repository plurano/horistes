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

PENDING_LABEL = "autorelease: pending"
TAGGED_LABEL = "autorelease: tagged"
RELEASE_HEAD = "release-please--"
RELEASE_LABEL = "autorelease:"
TIMEOUT = 30
MAX_BYTES = 1024 * 1024
READ_ATTEMPTS = 3
READ_BACKOFF = 1
KNOWN_RELEASE_WORKFLOWS = (
    "jellysin/release-helper/.github/workflows/release-please.yml@",
    "jellysin/release-helper/.github/workflows/release-please.yaml@",
    "plurano/horistes/.github/workflows/release-please.yml@",
    "plurano/horistes/.github/workflows/release-please.yaml@",
)
ALLOWED_TRIGGERS = ("push", "workflow_dispatch", "schedule", "workflow_run")
LOCAL_WORKFLOW_PREFIX = "./.github/workflows/"
ACTION_STEP_RE = re.compile(r"^(?:-\s+)?uses:\s*[\"']?googleapis/release-please-action@")
BLOCK_SCALAR_RE = re.compile(r":\s*[|>][-+0-9]*(?:\s*#.*)?$")
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


def _read_once(req, method, url, status_only):
    """Execute one attempt of an HTTP request, decoding JSON or returning raw body."""
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
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


def is_literal_false(val):
    """Whether an if: condition is a literal false (quoted, expression, or raw)."""
    val = re.sub(r"\s+#.*$", "", val).strip()
    for _ in range(2):
        if (val.startswith("'") and val.endswith("'")) or (
            val.startswith('"') and val.endswith('"')
        ):
            val = val[1:-1].strip()
    if val.startswith("$" + "{{") and val.endswith("}}"):
        val = val[3:-2].strip()
        for _ in range(2):
            if (val.startswith("'") and val.endswith("'")) or (
                val.startswith('"') and val.endswith('"')
            ):
                val = val[1:-1].strip()
    return val.lower() == "false"


def is_custom_config_val(val, default):
    """Whether val is a non-default config path, resolving literal expressions."""
    expr_open = "$" + "{{"
    if expr_open not in val:
        return val != default
    stripped = val.strip()
    if not (stripped.startswith(expr_open) and stripped.endswith("}}")):
        return True
    inner = stripped[len(expr_open) : -2].strip()
    lit = re.match(r"^['\"](.+)['\"]$", inner)
    if lit:
        return lit.group(1) != default
    return False


def has_custom_config(lines):
    """Whether with: sets config or manifest path away from default."""
    defaults = {
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }
    pending_key = None
    for line in lines[:100]:
        clean = re.sub(r"\s+#.*$", "", line).strip()
        if pending_key is not None:
            key, pending_key = pending_key, None
            if is_custom_config_val(clean.strip("'\""), defaults[key]):
                return True
            continue
        items = (
            clean.strip("{}").split(",")
            if (clean.startswith("{") and clean.endswith("}"))
            else [clean]
        )
        for item in items[:10]:
            match = re.match(r"^([a-zA-Z0-9_-]+)\s*:\s*(.*)$", item.strip())
            if not match:
                continue
            key = match.group(1).replace("_", "-")
            val = match.group(2).strip().strip("'\"")
            if key not in defaults:
                continue
            if val in (">", ">-", "|", "|-"):
                pending_key = key
                continue
            if is_custom_config_val(val, defaults[key]):
                return True
    return False


def inline_triggers(rest):
    """Event names of an inline on: value: a name, a [list] or a {mapping}."""
    if not (rest[:1] in "[{" and rest[-1:] in "]}"):
        return [rest.strip("'\"")]
    ends = ":" if rest.startswith("{") else ",]"
    names = []
    depth = 0
    token = ""
    for char in rest[:2000]:
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        if depth == 1 and char not in "[{]},:":
            token += char
            continue
        if depth <= 1 and char in ends and token.strip():
            names.append(token.strip().strip("'\""))
        if depth <= 1 and char in ",:]}[{":
            token = ""
    return names


def push_lines(events, push_indent, truncated):
    """Push children; None when the window cuts them."""
    for position, (indent, _) in enumerate(events[:5000]):
        if indent <= push_indent:
            return events[:position]
    return None if truncated else events


def literal_pattern(value, key):
    """A literal name or star pattern, else None."""
    value = value.strip()
    if value.startswith(("&", "*", "!!")):
        return None
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        value = value[1:-1]
    pattern = value.removeprefix("!") if key == "branches" else value
    if pattern.startswith("refs/") or not re.fullmatch(r"[A-Za-z0-9._/*-]+", pattern):
        return None
    return value


def sequence_values(following, indent):
    """Block sequence items up to the next filter."""
    values = []
    for level, clean in following[:5000]:
        if level < indent or (level == indent and not clean.startswith("- ")):
            break
        if not clean.startswith("- "):
            return None
        values.append(clean[2:])
    return values or None


def filter_patterns(value, following, indent, key):
    """Scalar, flow or block patterns, at most 200."""
    if not value:
        values = sequence_values(following, indent)
    elif value == "[]":
        values = []
    elif value.startswith("[") and value.endswith("]"):
        values = value[1:-1].split(",")
    else:
        values = [value]
    if values is None or len(values) > 200:
        return None
    patterns = [literal_pattern(v, key) for v in values[:200]]
    return None if None in patterns else patterns


def push_filters(events, indent, truncated):
    """Allow-listed push filters, else None."""
    block = push_lines(events, indent, truncated)
    if block is None:
        return None
    filters = {}
    child_indent = block[0][0] if block else 0
    sequence = False
    for index, (level, clean) in enumerate(block[:5000]):
        if level != child_indent or (sequence and clean.startswith("- ")):
            continue
        match = re.fullmatch(r"""(['"]?)([A-Za-z0-9_-]+)\1\s*:\s*(.*)""", clean)
        if not match:
            return None
        key, value = match[2], match[3]
        sequence = not value
        if key in ("paths", "paths-ignore"):
            continue
        if key not in ("branches", "branches-ignore", "tags", "tags-ignore"):
            return None
        patterns = filter_patterns(value, block[index + 1 :], child_indent, key)
        if patterns is None or key in filters:
            return None
        filters[key] = patterns
    return filters


def branch_matches(pattern, default_branch):
    """Anchored glob: * stops at a slash, ** crosses it."""
    regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, default_branch) is not None


def push_runs_on_branch(events, indent, default_branch, truncated):
    """Whether push admits the default branch; unknown filters count."""
    filters = push_filters(events, indent, truncated)
    if filters is None or {"branches", "branches-ignore"} <= filters.keys():
        return True
    if "branches" in filters:
        included = False
        for pattern in filters["branches"][:200]:
            if branch_matches(pattern.removeprefix("!"), default_branch):
                included = not pattern.startswith("!")
        return included
    if "branches-ignore" in filters:
        return not any(branch_matches(p, default_branch) for p in filters["branches-ignore"][:200])
    return not ({"tags", "tags-ignore"} & filters.keys())


def allowed_event(clean, following, indent, default_branch, truncated):
    """Whether one event entry admits the default branch."""
    key_m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)", clean)
    if key_m:
        name, value = key_m.groups()
        if name == "push" and not value:
            return push_runs_on_branch(following, indent, default_branch, truncated)
        return name in ALLOWED_TRIGGERS
    seq_m = re.match(r"^-\s*([a-zA-Z0-9_-]+)", clean)
    return bool(seq_m and seq_m.group(1) in ALLOWED_TRIGGERS)


def trigger_lines(lines):
    """Event candidates outside comments and block scalars."""
    block_indent = None
    for line in lines[:5000]:
        clean = re.sub(r"\s+#.*$", "", line.strip()).strip()
        if not clean:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None and indent > block_indent:
            continue
        block_indent = None
        if clean.startswith("#"):
            continue
        if BLOCK_SCALAR_RE.search(clean):
            block_indent = indent
        yield indent, clean


def block_has_allowed_trigger(events, default_branch, truncated):
    """Direct event keys until the next top-level key."""
    on_indent = events[0][0] if events else 0
    for position, (indent, clean) in enumerate(events[:5000]):
        if indent == 0:
            return False
        if indent == on_indent and allowed_event(
            clean, events[position + 1 :], indent, default_branch, truncated
        ):
            return True
    return truncated


def workflow_has_allowed_trigger(content, default_branch):
    """Whether top-level on: admits the default branch."""
    lines = content.splitlines()
    events = list(trigger_lines(lines))
    for position, (indent, clean) in enumerate(events[:5000]):
        if indent != 0 or not re.match(r"^on:\s*(.*)$", clean):
            continue
        rest = clean.split(":", 1)[1].strip()
        if rest:
            return any(t in ALLOWED_TRIGGERS for t in inline_triggers(rest))
        return block_has_allowed_trigger(events[position + 1 :], default_branch, len(lines) > 5000)
    return len(lines) > 5000


def job_direct_indent(job_lines):
    """Indent of a job's own keys: its first line that is neither blank nor a comment."""
    for line in job_lines[:1000]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return len(line) - len(line.lstrip(" "))
    return None


def job_key(clean, own_level, flags):
    """Record what one job line sets in flags; True when the line opens a with: block."""
    if own_level and clean.startswith("if:"):
        if is_literal_false(clean.split(":", 1)[1]):
            flags["dormant"] = True
    elif own_level and clean.startswith("uses:"):
        target = clean.split(":", 1)[1].strip().strip("'\"")
        if any(target.startswith(w) for w in KNOWN_RELEASE_WORKFLOWS):
            flags["reusable"] = True
        if target.removeprefix(LOCAL_WORKFLOW_PREFIX) in flags["local"]:
            flags["reusable"] = True
    elif own_level and clean.startswith("with:"):
        rest = clean.split(":", 1)[1].strip()
        if not rest:
            return True
        flags["custom"] = flags["custom"] or has_custom_config([rest])
    if ACTION_STEP_RE.search(clean):
        flags["action"] = True
    return False


def job_runs_release(job_lines, local):
    """Whether a single job runs release-please action or reusable workflow."""
    direct_indent = job_direct_indent(job_lines)
    if direct_indent is None:
        return False
    flags = {"dormant": False, "reusable": False, "action": False, "custom": False}
    flags["local"] = local
    block_indent = None
    in_with = False
    with_lines = []
    for line in job_lines[:1000]:
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not stripped:
            continue
        if block_indent is not None and indent > block_indent:
            if in_with:
                with_lines.append(line)
            continue
        block_indent = None
        if stripped.startswith("#"):
            continue
        if BLOCK_SCALAR_RE.search(line):
            block_indent = indent
        if in_with and indent > direct_indent:
            with_lines.append(line)
            continue
        in_with = False
        clean = re.sub(r"\s+#.*$", "", stripped)
        if job_key(clean, indent == direct_indent, flags):
            in_with = True
    custom_config = flags["custom"] or has_custom_config(with_lines)
    reusable_call = flags["reusable"]
    if flags["dormant"]:
        return False
    if flags["action"]:
        return True
    return bool(reusable_call and not custom_config)


def workflow_runs_release(content, local, default_branch):
    """Whether workflow triggers and jobs structure run release-please."""
    if not workflow_has_allowed_trigger(content, default_branch):
        return False
    return jobs_run_release(content, local)


def jobs_run_release(content, local):
    """Whether a job of the workflow runs release-please, triggers aside.

    local names the workflow files of this repository that run it themselves,
    so a job-level call of ./.github/workflows/<name> counts too.
    """
    in_jobs = False
    job_indent = None
    current_job = []
    lines = content.splitlines()[:5000]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_job:
                current_job.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if in_jobs:
                if current_job and job_runs_release(current_job, local):
                    return True
                current_job = []
                in_jobs = False
            if re.match(r"^jobs:\s*(?:#.*)?$", stripped):
                in_jobs = True
                job_indent = None
            continue
        if not in_jobs:
            continue
        if stripped.startswith("#"):
            if current_job:
                current_job.append(line)
            continue
        if job_indent is None:
            job_indent = indent
        if indent == job_indent and re.match(r"^[a-zA-Z0-9_-]+:\s*(?:#.*)?$", stripped):
            if current_job and job_runs_release(current_job, local):
                return True
            current_job = []
        elif current_job is not None:
            current_job.append(line)
    return bool(in_jobs and current_job and job_runs_release(current_job, local))


def local_release_workflows(contents):
    """Local workflow files that run release-please, directly or through local calls.

    Grows the set level by level; GitHub connects at most ten levels of workflows
    and permits no loops, so ten rounds reach every chain and a cycle cannot spin.
    """
    local = frozenset()
    for _ in range(10):
        grown = frozenset(n for n, c in contents.items() if jobs_run_release(c, local))
        if grown == local:
            break
        local = grown
    return local


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


def main(argv, env):
    """Dispatch settings or after command."""
    if len(argv) < 2:
        sys.stderr.write("usage: release_labels.py <settings|after>\n")
        return 2
    commands = {"settings": cmd_settings, "after": cmd_after}
    if argv[1] not in commands:
        sys.stderr.write(f"unknown command: {argv[1]}\n")
        return 2
    try:
        return commands[argv[1]](env)
    except (CallError, KeyError, json.JSONDecodeError) as err:
        sys.stdout.write(f"::error title=Release labels::{argv[1]}: {err}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv, os.environ))
