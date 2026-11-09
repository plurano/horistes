# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The merge train's script, run as written against a stand-in forge (decision record 0029).

The script of the step "Land or update one pull request in every repository of
the app" is taken out of the reusable workflow and run with the environment the
step sets. A small HTTP server stands in for the forge: it checks the app's JWT
against a key made for the test run, lists installations and their
repositories, mints and revokes tokens, answers the GraphQL and REST calls of
the train and records every write. Only the wait between two reads of an
UNKNOWN merge state and between read retries are shortened.
"""

from __future__ import annotations

import base64
import email.message
import errno
import functools
import hashlib
import http.client
import importlib.util
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import zipfile
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from conftest import ROOT, measured

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

try:
    import fcntl
except ImportError:  # Windows has no fcntl; the pipe-capacity tests skip there.
    fcntl = None
# Setting a pipe's capacity is Linux only (F_SETPIPE_SZ); macOS and Windows skip those tests.
PIPE_SIZE = getattr(fcntl, "F_SETPIPE_SZ", None)
NO_PIPE_SIZE = "setting a pipe's capacity needs F_SETPIPE_SZ, which only Linux has"
# Open descriptors are listed in /proc/self/fd on Linux and /dev/fd on macOS; Windows has
# neither, so the descriptor-leak tests skip there.
FD_DIR = next((d for d in ("/proc/self/fd", "/dev/fd") if os.path.isdir(d)), None)
NO_FD_DIR = "counting open descriptors needs /proc/self/fd or /dev/fd"

TRAIN = ROOT / ".github/workflows/merge-train.yml"
STEP = "Land or update one pull request in every repository of the app"
NOW = datetime.now(UTC)
OLD = (NOW - timedelta(days=10)).isoformat()
YOUNG = (NOW - timedelta(hours=5)).isoformat()
CONFLICT = "merge conflict between base and head"
NO_NEW = "There are no new commits on the base branch."
QUEUE_ONLY = "Repository rule violations found\n\nChanges must be made through the merge queue"
# The forge's answer while it is still merging (fleet run 39131740663, 2026-10-18).
MERGE_IN_PROGRESS = {
    "type": "UNPROCESSABLE",
    "path": ["mergePullRequest"],
    "locations": [{"line": 4, "column": 3}],
    "message": "Merge already in progress",
}
CLIENT_ID = "Iv23planted"
WRITE = {
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "checks": "read",
}
READ = {"contents": "read", "pull_requests": "read", "checks": "read"}
ISSUES = {"issues": "write"}
BOT = "merge-train-app"


SCRIPT = ROOT / "tools/merge_train.py"
RUN = 'python3 "$GITHUB_WORKSPACE/merge-train-source/tools/merge_train.py"'
# Starts the genuine tools/merge_train.py, so coverage counts that file, with both waits at 0.
LAUNCH = """\
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("merge_train", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules["merge_train"] = module
spec.loader.exec_module(module)
module.SETTLE_SECONDS = 0
module.READ_BACKOFF_SECONDS = 0
sys.exit(module.main(os.environ))
"""


def script() -> str:
    """The sweep script the train step runs from the checkout of the workflow's commit."""
    steps = [s for s in ciwf.load(TRAIN)["jobs"]["train"]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1, f"one step named {STEP!r}"
    assert steps[0]["run"].strip() == RUN, steps[0]["run"]
    return SCRIPT.read_text(encoding="utf-8")


MODULE_STEP = "Write the train rules module"


def module() -> str:
    """The rules module the step before the train writes."""
    steps = [s for s in ciwf.load(TRAIN)["jobs"]["train"]["steps"] if s.get("name") == MODULE_STEP]
    assert len(steps) == 1, f"one step named {MODULE_STEP!r}"
    lines = steps[0]["run"].splitlines()
    assert lines[0] == """cat > "$RUNNER_TEMP/train_rules.py" <<'PY'""", lines[0]
    assert lines[-1] == "PY", lines[-1]
    return "\n".join(lines[1:-1]) + "\n"


def openssl(*args: str, data: bytes = b"") -> bytes:
    done = subprocess.run(
        ["openssl", *args], input=data, capture_output=True, check=True, timeout=60
    )
    return done.stdout


@functools.cache
def app_key(bits: int = 2048, which: str = "app") -> tuple[str, bytes]:
    """A key made for this test run: the private PEM text and the public PEM."""
    del which
    private = openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{bits}")
    return private.decode(), openssl("pkey", "-pubout", data=private)


@functools.cache
def non_rsa_app_key(kind: str) -> str:
    """A non-RSA private key PEM text generated for test refusal."""
    if kind == "DSA":
        params = openssl(
            "genpkey", "-genparam", "-algorithm", "DSA", "-pkeyopt", "dsa_paramgen_bits:2048"
        )
        return openssl("genpkey", "-paramfile", "/dev/stdin", data=params).decode()
    if kind == "EC":
        return openssl(
            "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"
        ).decode()
    if kind == "Ed25519":
        return openssl("genpkey", "-algorithm", "ED25519").decode()
    if kind == "RSA-PSS":
        return openssl(
            "genpkey", "-algorithm", "RSA-PSS", "-pkeyopt", "rsa_keygen_bits:2048"
        ).decode()
    raise ValueError(f"unsupported key kind: {kind}")


_ABSENT = object()


def build_commit_node(number: int, suites: list, **fields) -> dict:
    """The latest commit node of one pull request."""
    author = fields.pop("author", f"author-{number}@example.com")
    headline = fields.pop("messageHeadline", f"commit {number}")
    created_at = fields.pop("createdAt", OLD)
    commit_date = fields.pop("committedDate", created_at)
    signature = fields.pop("signature", _ABSENT)
    user = fields.pop("user", _ABSENT)
    suite_nodes = []
    base_time = datetime(2026, 10, 18, 0, 0, 0, tzinfo=UTC)
    for i, item in enumerate(suites):
        if isinstance(item, dict):
            suite_nodes.append(item)
        else:
            created_at_suite = (base_time + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            suite_nodes.append(
                {
                    "databaseId": 1000 + i,
                    "conclusion": item,
                    "createdAt": created_at_suite,
                    "app": {"slug": "github-actions"},
                    "workflowRun": {"workflow": {"databaseId": 100 + i}},
                }
            )
    author_node = dict(author) if isinstance(author, dict) else {"email": author}
    is_regenerate = headline == "chore(deps): regenerate generated files"
    if user is not _ABSENT:
        author_node["user"] = {"login": user} if isinstance(user, str) else user
    elif "user" not in author_node and is_regenerate:
        author_node["user"] = {"login": f"{BOT}[bot]"}

    if signature is not _ABSENT:
        sig_node = signature
    elif is_regenerate:
        sig_node = {"wasSignedByGitHub": True}
    else:
        sig_node = None

    return {
        "committedDate": commit_date,
        "messageHeadline": headline,
        "author": author_node,
        "signature": sig_node,
        "checkSuites": {"nodes": suite_nodes},
    }


def pull(number: int, state: str, **fields) -> dict:
    """One open pull request as the query reads it."""
    commits = fields.pop("commits", None)
    suites = fields.pop("suites", ["SUCCESS"])
    labels = fields.pop("labels", ())
    created_at = fields.get("createdAt", OLD)
    raw_author = fields.pop("author", _ABSENT)
    if raw_author is _ABSENT:
        pr_author = {"login": f"author-{number}"}
    elif isinstance(raw_author, dict) and "login" in raw_author and "email" not in raw_author:
        pr_author = raw_author
    else:
        fields["author"] = raw_author
        pr_author = {"login": f"author-{number}"}
    reviews = fields.pop("reviews", [])
    review_nodes = reviews if isinstance(reviews, dict) else {"nodes": reviews}
    commit_node = build_commit_node(number, suites, **fields)
    node = {
        "number": number,
        "id": f"PR_{number}",
        "title": fields.pop("title", f"change {number}"),
        "body": fields.pop("body", ""),
        "createdAt": created_at,
        "author": pr_author,
        "isDraft": False,
        "isCrossRepository": False,
        "baseRefName": "main",
        "headRefName": f"feat/change-{number}",
        "headRefOid": f"{number:040x}",
        "mergeStateStatus": state,
        "autoMergeRequest": None,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "reviews": review_nodes,
        "refreshes": {"nodes": []},
        "latest": {"nodes": [{"commit": commit_node}]},
    }
    if commits is not None:
        node["history"] = {
            "nodes": [
                {"commit": {"committedDate": when, "parents": {"totalCount": parents}}}
                for when, parents in commits
            ]
        }
    node.update(fields)
    return node


def merged_pull(number: int, *issues: tuple, by: str = BOT, at: str = YOUNG, **fields) -> dict:
    """One merged pull request as the close step reads it; issues: (number, state, reopened)."""
    nodes = []
    for issue in issues:
        issue_number, state, reopened = (*issue, ())[:3]
        nodes.append(
            {
                "number": issue_number,
                "state": state,
                "repository": {"nameWithOwner": fields.get("issue_repo", "fleet/app")},
                "timelineItems": {"nodes": [{"createdAt": when} for when in reopened]},
            }
        )
    return {
        "number": number,
        "mergedAt": at,
        "mergedBy": {"login": by} if by else None,
        "mergeCommit": {"oid": f"{number:x}" * 40},
        "closingIssuesReferences": {"nodes": nodes},
    }


def repository(
    *pulls: dict,
    auto_merge: bool = True,
    release: tuple = (False, False),
    queue: bool = False,
    rules: tuple | None = (),
    lease: object = _ABSENT,
) -> dict:
    """One repository as the query reads it; `release` says which release-please files it has.

    `rules` are the rule types of the default branch; None stands for rules the
    token could not read.
    """
    config, manifest = release
    read = None if rules is None else {"nodes": [{"type": kind} for kind in rules]}
    return {
        "defaultBranchRef": {"name": "main", "rules": read},
        "autoMergeAllowed": auto_merge,
        "mergeQueue": {"id": "MQ_planted"} if queue else None,
        "landerLease": None if lease is _ABSENT else {"description": lease},
        "releaseConfig": {"oid": "c" * 40} if config else None,
        "releaseManifest": {"oid": "d" * 40} if manifest else None,
        "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": list(pulls)},
    }


class Forge:
    """What the stand-in forge serves, and every write, token and revocation it saw.

    `Forge(*reads)` installs the app on the account fleet with the one repository
    fleet/app, read as `reads` in turn (the last read repeats). `install` adds
    accounts and repositories. Writes of fleet/app are kept in `writes` as before;
    `where` keeps every write with its repository.
    """

    def __init__(self, *reads: dict) -> None:
        self.public = app_key()[1]
        self.installations: list[dict] = []
        self.reads: dict[str, list[dict]] = {}
        self.active_reads: dict[str, dict] = {}
        self.read_calls: list[tuple[str, dict]] = []
        self.read_statuses: dict[str, list[int]] = {}
        self.mutation_statuses: dict[str, list[int]] = {}
        self.archived: set[str] = set()
        self.tokens: dict[str, tuple] = {}
        self.minted: list[tuple[int, dict]] = []
        self.revoked: list[str] = []
        self.writes: list[tuple] = []
        self.where: list[tuple[str, tuple]] = []
        self.merged = True
        self.refuse_merge = ""
        self.merge_errors: list[dict] = []
        self.refuse_merge_in: set[str] = set()
        self.update_answers: dict[int, tuple[int, str]] = {}
        self.workdir = Path()
        self.contents: dict[tuple[str, str, str], tuple[int, dict]] = {}
        self.artifacts: dict[tuple[str, str], list[dict]] = {}
        self.storage: dict[int, bytes] = {}
        self.storage_cut: dict[int, int] = {}
        self.storage_auth_seen: list[str] = []
        self.users_auth_seen: list[str] = []
        self.artifact_redirect_locations: dict[int, str | None] = {}
        self.refuse_commit = False
        self.messages: list[tuple[str, str, str | None, str | None]] = []
        self.commits: dict[int, list[tuple[str, int]]] = {}
        self.merge_states: dict[int, str] = {}
        self.commit_counts: dict[int, int] = {}
        # One detail per number, or a list read in turn (the last repeats); None reads as
        # a pull request the forge no longer returns.
        self.review_details: dict[int, dict | list[dict | None] | None] = {}
        self.review_refusals: dict[int, str] = {}
        self.review_body_calls: list[int] = []
        self.merged_pulls: dict[str, list[dict]] = {}
        self.issue_right = True
        self.ungranted: set[int] = set()
        self.jobs: dict[int, list[dict]] = {}
        self.jobs_calls: list[int] = []
        self.label_missing = False
        self.install(7, "fleet", app=list(reads))

    def set_allowlist(self, repo: str, default_branch: str, paths: list[str]) -> None:
        text = "\n".join(paths)
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self.contents[(repo, ".github/generated-files", default_branch)] = (
            200,
            {"type": "file", "content": encoded, "encoding": "base64"},
        )

    def set_head_content(self, repo: str, path: str, head_sha: str, data: bytes) -> None:
        blob_sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        self.contents[(repo, path, head_sha)] = (
            200,
            {"type": "file", "sha": blob_sha, "size": len(data)},
        )

    def add_artifact(
        self,
        repo: str,
        artifact_id: int,
        name: str,
        zip_bytes: bytes,
        head_sha: str,
        head_branch: str,
        expired: bool = False,
        created_at: str | None = None,
        workflow_run_id: int = 12345,
        html_url: str | None = None,
    ) -> None:
        self.storage[artifact_id] = zip_bytes
        art = {
            "id": artifact_id,
            "name": name,
            "size_in_bytes": len(zip_bytes),
            "archive_download_url": f"http://127.0.0.1/repos/{repo}/actions/artifacts/{artifact_id}/zip",
            "expired": expired,
            "created_at": created_at or NOW.isoformat(),
            "workflow_run": {
                "id": workflow_run_id,
                "head_sha": head_sha,
                "head_branch": head_branch,
                "html_url": html_url or f"https://github.com/{repo}/actions/runs/{workflow_run_id}",
            },
        }
        self.artifacts.setdefault((repo, name), []).append(art)

    def install(self, number: int, account: str, suspended: bool = False, **repos) -> None:
        self.installations.append(
            {"id": number, "account": account, "suspended": suspended, "repos": list(repos)}
        )
        for name, reads in repos.items():
            self.reads[f"{account}/{name}"] = list(reads)

    def jwt_refusal(self, header: str) -> str:
        """Why the Authorization header is no valid JWT of the app, or ''."""
        head, payload, signature = header.removeprefix("Bearer ").split(".")
        (self.workdir / "jwt.pub").write_bytes(self.public)
        (self.workdir / "jwt.sig").write_bytes(base64.urlsafe_b64decode(signature + "=="))
        checked = subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(self.workdir / "jwt.pub"),
                "-signature",
                str(self.workdir / "jwt.sig"),
            ],
            input=f"{head}.{payload}".encode(),
            capture_output=True,
            check=False,
            timeout=60,
        )
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        now = time.time()
        if checked.returncode != 0:
            return "signature does not match the app's key"
        if json.loads(base64.urlsafe_b64decode(head + "=="))["alg"] != "RS256":
            return "not RS256"
        if claims["iss"] != CLIENT_ID or not claims["iat"] <= now < claims["exp"]:
            return f"claims {claims}"
        if claims["exp"] - claims["iat"] > 600:
            return "lives longer than ten minutes"
        return ""

    def mint(self, number: int, body: dict) -> tuple[int, dict]:
        self.minted.append((number, body))
        (inst,) = [i for i in self.installations if i["id"] == number]
        names = body.get("repositories")
        if names is None and body["permissions"] == {"metadata": "read"}:
            token = f"list-token-{number}"
            self.tokens[token] = ("list", number, None)
        elif names is not None and number in self.ungranted:
            return 422, {
                "message": "The permissions requested are not granted to this installation."
            }
        elif "issues" in body["permissions"] and not self.issue_right:
            return 422, {"message": "The permissions requested are not granted."}
        elif names and len(names) == 1 and names[0] in inst["repos"]:
            full = f"{inst['account']}/{names[0]}"
            token = "planted-token" if full == "fleet/app" else f"token-{full}"
            self.tokens[token] = ("repo", number, full, body["permissions"])
        else:
            return 422, {"message": f"stand-in forge: token request {body} not expected"}
        return 201, {"token": token}

    def repo_of(self, header: str) -> tuple:
        token = header.removeprefix("Bearer ")
        kind = self.tokens.get(token, ("unknown",))
        assert kind[0] == "repo" and token not in self.revoked, (token, kind, self.revoked)
        return kind[2], kind[3]

    def _handle_commit_mutation(self, full: str, variables: dict) -> dict:
        if self.refuse_commit:
            return {"errors": [{"message": "createCommitOnBranch refused by stand-in"}]}
        inp = variables["input"]
        branch = inp["branch"]["branchName"]
        expected = inp["expectedHeadOid"]
        paths = [a["path"] for a in inp.get("fileChanges", {}).get("additions", [])]
        headline = inp["message"]["headline"]
        body = inp["message"]["body"]
        self.write(full, ("commit", branch, expected, paths, headline, body))
        return {"data": {"createCommitOnBranch": {"commit": {"oid": "c" * 40}}}}

    def _record_message(self, kind: str, query: str, variables: dict) -> None:
        headline = variables.get("headline") if "commitHeadline: $headline" in query else None
        body = variables.get("body") if "commitBody: $body" in query else None
        self.messages.append((kind, variables["id"], headline, body))

    def graphql(self, header: str, query: str, variables: dict) -> dict:
        full, rights = self.repo_of(header)
        mutations = (
            "mergePullRequest",
            "enqueuePullRequest",
            "enablePullRequestAutoMerge",
            "disablePullRequestAutoMerge",
            "createCommitOnBranch",
            "convertPullRequestToDraft",
            "markPullRequestReadyForReview",
        )
        if any(name in query for name in mutations):
            assert rights == WRITE, rights
        if "convertPullRequestToDraft" in query:
            self.write(full, ("draft", variables["id"]))
            return {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}}
        if "markPullRequestReadyForReview" in query:
            self.write(full, ("ready", variables["id"]))
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}}
        if "disablePullRequestAutoMerge" in query:
            self.write(full, ("disarm", variables["id"]))
            return {
                "data": {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}
            }
        if "enqueuePullRequest" in query:
            self.write(full, ("enqueue", variables["id"], variables["oid"]))
            entry = {"state": "QUEUED"} if self.merged else None
            return {"data": {"enqueuePullRequest": {"mergeQueueEntry": entry}}}
        if "mergePullRequest" in query:
            self._record_message("merge", query, variables)
            self.write(full, ("merge", variables["id"], variables["oid"], variables["method"]))
            if (self.active_reads.get(full) or {}).get("mergeQueue"):
                return {"errors": [{"type": "UNPROCESSABLE", "message": QUEUE_ONLY}]}
            if self.merge_errors:
                return {"errors": self.merge_errors}
            if self.refuse_merge or full in self.refuse_merge_in:
                return {"errors": [{"message": self.refuse_merge or "refused here"}]}
            return {"data": {"mergePullRequest": {"pullRequest": {"merged": self.merged}}}}
        if "enablePullRequestAutoMerge" in query:
            self._record_message("arm", query, variables)
            self.write(full, ("arm", variables["id"], variables["oid"], variables["method"]))
            armed = {"autoMergeRequest": {"enabledAt": NOW.isoformat()}}
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": armed}}}
        if "createCommitOnBranch" in query:
            return self._handle_commit_mutation(full, variables)
        return self._read_graphql(full, query, variables)

    def _read_graphql(self, full: str, query: str, variables: dict) -> dict:
        if "mergeStateStatus" in query and "pullRequest(" in query:
            return self.pull_merge_state(full, variables)
        if "reviews(first: 100)" in query:
            return self.pull_review_bodies(full, variables)
        if "pullRequest(" in query:
            return self.pull_commits(full, variables)
        if "states: MERGED" in query:
            nodes = self.merged_pulls.get(full, [])
            return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}
        return self.pull_page(full, query, variables)

    def pull_review_bodies(self, full: str, variables: dict) -> dict:
        owner, name = full.split("/")
        number = variables["number"]
        assert variables == {"owner": owner, "name": name, "number": number}, variables
        self.review_body_calls.append(number)
        if number in self.review_refusals:
            return {"errors": [{"message": self.review_refusals[number]}]}
        empty = {"totalCount": 0, "nodes": []}
        detail = self.review_details.get(number, {})
        if isinstance(detail, list):
            detail = detail.pop(0) if len(detail) > 1 else detail[0]
        if detail is None:
            return {"data": {"repository": {"pullRequest": None}}}
        pr = {
            "reviews": detail.get("reviews", empty),
            "comments": detail.get("comments", empty),
            "commits": detail.get("commits", empty),
        }
        return {"data": {"repository": {"pullRequest": pr}}}

    def pull_commits(self, full: str, variables: dict) -> dict:
        owner, name = full.split("/")
        number = variables["number"]
        assert variables == {"owner": owner, "name": name, "number": number}, variables
        default = [(f"change {number}\n\nbody {number}\n\nSigned-off-by: Dev <dev@example.org>", 1)]
        items = self.commits.get(number, default)
        total = self.commit_counts.get(number, len(items))
        nodes = [
            {"commit": {"message": message, "parents": {"totalCount": parents}}}
            for message, parents in items
        ]
        return {
            "data": {
                "repository": {"pullRequest": {"commits": {"totalCount": total, "nodes": nodes}}}
            }
        }

    def pull_merge_state(self, full: str, variables: dict) -> dict:
        """Return the mock mergeStateStatus GraphQL response for a pull request."""
        owner, name = full.split("/")
        number = variables["number"]
        assert variables == {"owner": owner, "name": name, "number": number}, variables
        if number in self.merge_states:
            state = self.merge_states[number]
        else:
            nodes = self.active_reads.get(full, {}).get("pullRequests", {}).get("nodes", [])
            matches = [p["mergeStateStatus"] for p in nodes if p["number"] == number]
            state = matches[0] if matches else "CLEAN"
        return {"data": {"repository": {"pullRequest": {"mergeStateStatus": state}}}}

    def pull_page(self, full: str, query: str, variables: dict) -> dict:
        owner, name = full.split("/")
        after = variables.get("after")
        expected = {"owner": owner, "name": name}
        if "after" in variables:
            expected["after"] = after
        assert variables == expected, variables
        size = int(re.search(r"pullRequests\(states: OPEN, first: (\d+)", query)[1])
        if after is None:
            reads = self.reads[full]
            self.active_reads[full] = reads.pop(0) if len(reads) > 1 else reads[0]
        read = self.active_reads[full]
        start = int(after.removeprefix("cursor-")) if after else 0
        nodes = read["pullRequests"]["nodes"]
        end = min(start + size, len(nodes))
        page = {
            "nodes": nodes[start:end],
            "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": f"cursor-{end}"},
        }
        return {"data": {"repository": {**read, "pullRequests": page}}}

    def graphql_response(self, header: str, query: str, variables: dict) -> tuple[int, dict]:
        full, _ = self.repo_of(header)
        if query.lstrip().startswith("mutation"):
            body = self.graphql(header, query, variables)
            operation = re.search(r"\b(\w+)\(\s*input:", query)[1]
            statuses = self.mutation_statuses.get(operation, [])
        elif "pullRequests(" in query and "states: MERGED" not in query:
            self.read_calls.append((full, dict(variables)))
            size = int(re.search(r"pullRequests\(states: OPEN, first: (\d+)", query)[1])
            if size > 25:
                return 502, {"message": "stand-in forge: read page size exceeds 25"}
            statuses = self.read_statuses.get(full, [])
            body = None
        else:
            statuses = []
            body = None
        status = statuses.pop(0) if statuses else 200
        if status != 200:
            return status, {"message": f"stand-in forge: queued HTTP {status}"}
        return status, body or self.graphql(header, query, variables)

    def write(self, full: str, write: tuple) -> None:
        self.where.append((full, write))
        if full == "fleet/app":
            self.writes.append(write)

    def update(self, header: str, full: str, number: int, body: dict) -> tuple[int, dict]:
        token_repo, rights = self.repo_of(header)
        assert (token_repo, rights) == (full, WRITE), (token_repo, rights, full)
        self.write(full, ("update", number, body["expected_head_sha"]))
        code, message = self.update_answers.get(number, (202, "Updating pull request branch."))
        return code, {"message": message}

    def run_jobs(self, header: str, full: str, run_id: int) -> tuple[int, dict]:
        token_repo, rights = self.repo_of(header)
        assert (token_repo, rights) == (full, WRITE), (token_repo, rights, full)
        self.jobs_calls.append(run_id)
        return 200, {"jobs": self.jobs.get(run_id, [])}

    def label_write(self, header: str, full: str, write: tuple) -> tuple[int, dict]:
        token_repo, rights = self.repo_of(header)
        assert (token_repo, rights) == (full, WRITE), (token_repo, rights, full)
        if write[0] == "unlabel" and self.label_missing:
            return 404, {"message": "Label does not exist"}
        self.write(full, write)
        return 200, {}

    def issue_write(self, header: str, full: str, write: tuple) -> tuple[int, dict]:
        token_repo, rights = self.repo_of(header)
        assert (token_repo, rights) == (full, ISSUES), (token_repo, rights, full)
        self.write(full, write)
        return 200, {}

    def installation_list(self) -> list[dict]:
        return [
            {
                "id": i["id"],
                "account": {"login": i["account"]},
                "suspended_at": OLD if i["suspended"] else None,
            }
            for i in self.installations
        ]

    def repositories(self, header: str) -> dict:
        kind = self.tokens[header.removeprefix("Bearer ")]
        assert kind[0] == "list", kind
        (inst,) = [i for i in self.installations if i["id"] == kind[1]]
        repos = [
            {
                "name": name,
                "full_name": f"{inst['account']}/{name}",
                "archived": f"{inst['account']}/{name}" in self.archived,
                "disabled": False,
            }
            for name in inst["repos"]
        ]
        return {"total_count": len(repos), "repositories": repos}


class Handler(BaseHTTPRequestHandler):
    """The stand-in forge's HTTP side; what it serves is the Forge on its server."""

    def log_message(self, *args) -> None:
        pass

    def answer(self, code: int, body: dict | list | None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def app_call(self) -> bool:
        """Refuse a call of the app whose JWT does not hold; True when refused."""
        refusal = self.server.forge.jwt_refusal(self.headers["Authorization"])
        if refusal:
            self.answer(401, {"message": f"A JSON web token could not be decoded: {refusal}"})
        return bool(refusal)

    def _get_contents(self, path: str) -> None:
        full_repo, _, file_path = path.removeprefix("/repos/").partition("/contents/")
        query = parse_qs(urlsplit(self.path).query)
        ref = query.get("ref", [""])[0]
        target = self.server.forge.contents.get((full_repo, file_path, ref))
        if target is not None:
            code, body = target
            self.answer(code, body)
        else:
            self.answer(404, {"message": "Not Found"})

    def _get_artifacts(self, path: str) -> None:
        full_repo = path.removeprefix("/repos/").removesuffix("/actions/artifacts")
        query = parse_qs(urlsplit(self.path).query)
        name = query.get("name", [""])[0]
        arts = self.server.forge.artifacts.get((full_repo, name), [])
        host = self.headers.get("Host", "127.0.0.1")
        arts_copy = []
        for art in arts:
            item = dict(art)
            item["archive_download_url"] = (
                f"http://{host}/repos/{full_repo}/actions/artifacts/{art['id']}/zip"
            )
            arts_copy.append(item)
        self.answer(200, {"total_count": len(arts_copy), "artifacts": arts_copy})

    def _get_artifact_zip(self, path: str) -> None:
        _, _, tail = path.removeprefix("/repos/").partition("/actions/artifacts/")
        art_id = int(tail.removesuffix("/zip"))
        self.server.forge.repo_of(self.headers["Authorization"])
        loc = self.server.forge.artifact_redirect_locations.get(art_id, f"/storage/{art_id}")
        self.send_response(302)
        if loc is not None:
            self.send_header("Location", loc)
        self.end_headers()

    def _get_storage(self, path: str) -> None:
        if self.headers.get("Authorization"):
            self.server.forge.storage_auth_seen.append(self.headers["Authorization"])
            self.answer(400, {"message": "storage URL refuses Authorization header"})
            return
        art_id = int(path.removeprefix("/storage/"))
        data = self.server.forge.storage.get(art_id)
        if data is None:
            self.answer(404, {"message": "Not Found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        cut = self.server.forge.storage_cut.get(art_id)
        if cut is not None:
            self.wfile.write(data[:cut])
            self.close_connection = True
            return
        self.wfile.write(data)

    def _dispatch_app(self, path: str) -> bool:
        if path == "/app":
            if not self.app_call():
                self.answer(200, {"slug": "merge-train-app"})
            return True
        if path.startswith("/users/"):
            header = self.headers.get("Authorization", "")
            self.server.forge.users_auth_seen.append(header)
            token = header.removeprefix("Bearer ")
            if not header:
                # the runner address shares 60 unauthenticated calls an hour with every job on it
                self.answer(403, {"message": "API rate limit exceeded for 192.0.2.1."})
            elif token not in self.server.forge.tokens or token in self.server.forge.revoked:
                self.answer(401, {"message": "Bad credentials"})
            else:
                self.answer(200, {"id": 123456, "login": path.removeprefix("/users/")})
            return True
        if path == "/app/installations":
            if not self.app_call():
                self.answer(200, self.server.forge.installation_list())
            return True
        if path == "/installation/repositories":
            self.answer(200, self.server.forge.repositories(self.headers["Authorization"]))
            return True
        return False

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if self._dispatch_app(path):
            return
        if "/contents/" in path and path.startswith("/repos/"):
            self._get_contents(path)
        elif path.startswith("/repos/") and path.endswith("/actions/artifacts"):
            self._get_artifacts(path)
        elif "/actions/artifacts/" in path and path.endswith("/zip"):
            self._get_artifact_zip(path)
        elif path.startswith("/storage/"):
            self._get_storage(path)
        elif re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs/\d+/jobs", path):
            full, _, tail = path.removeprefix("/repos/").partition("/actions/runs/")
            run_id = int(tail.removesuffix("/jobs"))
            self.answer(*self.server.forge.run_jobs(self.headers["Authorization"], full, run_id))
        else:
            raise AssertionError(self.path)

    def do_POST(self) -> None:
        if self.path.startswith("/repos/") and self.path.endswith("/comments"):
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            number = int(tail.removesuffix("/comments"))
            write = ("comment", number, self.body()["body"])
            self.answer(*self.server.forge.issue_write(self.headers["Authorization"], full, write))
            return
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/labels", self.path):
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            write = ("label", int(tail.removesuffix("/labels")), *self.body()["labels"])
            self.answer(*self.server.forge.label_write(self.headers["Authorization"], full, write))
            return
        if self.path == "/graphql":
            request = self.body()
            header = self.headers["Authorization"]
            self.answer(
                *self.server.forge.graphql_response(header, request["query"], request["variables"])
            )
            return
        prefix, _, tail = self.path.partition("/app/installations/")
        number, _, action = tail.partition("/")
        assert (prefix, action) == ("", "access_tokens"), self.path
        if not self.app_call():
            self.answer(*self.server.forge.mint(int(number), self.body()))

    def do_PUT(self) -> None:
        prefix, _, tail = self.path.rpartition("/pulls/")
        number, _, action = tail.partition("/")
        assert prefix.startswith("/repos/") and action == "update-branch", self.path
        full = prefix.removeprefix("/repos/")
        self.answer(
            *self.server.forge.update(self.headers["Authorization"], full, int(number), self.body())
        )

    def do_PATCH(self) -> None:
        full, _, number = self.path.removeprefix("/repos/").partition("/issues/")
        body = self.body()
        write = ("close", int(number), body["state"], body["state_reason"])
        self.answer(*self.server.forge.issue_write(self.headers["Authorization"], full, write))

    def do_DELETE(self) -> None:
        if "/labels/" in self.path:
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            number, _, name = tail.partition("/labels/")
            write = ("unlabel", int(number), unquote(name))
            self.answer(*self.server.forge.label_write(self.headers["Authorization"], full, write))
            return
        assert self.path == "/installation/token", self.path
        self.server.forge.revoked.append(self.headers["Authorization"].removeprefix("Bearer "))
        self.answer(204, None)


def serve(forge: Forge) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.forge = forge
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def train(
    tmp_path: Path,
    forge: Forge,
    text: str | None = None,
    helper: str | None = None,
    **settings: str,
):
    """Run the step's script against the stand-in forge; the result and the summary written."""
    (tmp_path / "train_rules.py").write_text(helper or module(), encoding="utf-8")
    if text is None:
        # The genuine script runs from tools/, so coverage counts it; LAUNCH zeroes the waits.
        command = [sys.executable, "-I", "-B", "-c", LAUNCH, str(SCRIPT)]
    else:
        source = text.replace("SETTLE_SECONDS = 5", "SETTLE_SECONDS = 0")
        source = source.replace("READ_BACKOFF_SECONDS = 5", "READ_BACKOFF_SECONDS = 0")
        path = tmp_path / "train.py"
        path.write_text(source, encoding="utf-8")
        command = [sys.executable, "-I", "-B", str(path)]
    summary = tmp_path / "summary.md"
    output = tmp_path / "output"
    forge.workdir = tmp_path
    server = serve(forge)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    env = measured(
        {
            "PATH": "/usr/bin:/bin",
            "GITHUB_API_URL": url,
            "GITHUB_GRAPHQL_URL": f"{url}/graphql",
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path),
            "TRAIN_APP_CLIENT_ID": CLIENT_ID,
            "TRAIN_APP_KEY": app_key()[0],
            "TRAIN_SWITCH": "on",
            "TRAIN_MERGE_METHOD": "SQUASH",
            "TRAIN_ACCEPT_STATES": "",
            "TRAIN_MIN_AGE_DAYS": "3",
            "TRAIN_HOLD_LABELS": "hold,do-not-merge,planted",
            "TRAIN_DEPENDENCY_BRANCHES": "renovate/ dependabot/",
            "TRAIN_PERSON_BRANCHES": "release-please--",
            "TRAIN_RELEASE_OFF": "",
            "TRAIN_RELEASE_MAX_PARALLEL": "3",
            "TRAIN_SECURITY_LABELS": "security",
            **{f"TRAIN_{key.upper()}": value for key, value in settings.items()},
        }
    )
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=120,
        )
    finally:
        server.shutdown()
        server.server_close()
    written = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return done, written


def releases_of(tmp_path: Path) -> list[dict] | None:
    """The `releases` output the step wrote for the release job, or None."""
    output = tmp_path / "output"
    lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
    values = [line.removeprefix("releases=") for line in lines if line.startswith("releases=")]
    assert len(values) <= 1, lines
    return json.loads(values[0]) if values else None


def landed(number: int) -> tuple:
    return ("merge", f"PR_{number}", f"{number:040x}", "SQUASH")


def armed(number: int) -> tuple:
    return ("arm", f"PR_{number}", f"{number:040x}", "SQUASH")


def disarmed(number: int) -> tuple:
    return ("disarm", f"PR_{number}")


def queued(number: int) -> tuple:
    return ("enqueue", f"PR_{number}", f"{number:040x}")


def updated(number: int) -> tuple:
    return ("update", number, f"{number:040x}")


CHECKOUT = {
    "repository": "${{ job.workflow_repository }}",
    "ref": "${{ job.workflow_sha }}",
    "path": "merge-train-source",
    "persist-credentials": False,
    "sparse-checkout": "tools/merge_train.py",
}


def is_checkout(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@")


def checkout_problems(doc: dict) -> list[str]:
    """What is wrong with the checkout of the workflow's own commit before the train step."""
    steps = doc["jobs"]["train"]["steps"]
    train = next(i for i, s in enumerate(steps) if s.get("name") == STEP)
    found = [s for s in steps[:train] if is_checkout(s)]
    if len(found) != 1:
        return [f"{len(found)} checkouts before the train step"]
    given = found[0].get("with", {})
    return [
        f"{key}: {given.get(key)!r}" for key, want in CHECKOUT.items() if given.get(key) != want
    ]


def _raising_opener(err: urllib.error.HTTPError):
    """Stand-in opener raising an HTTPError."""

    class StubOpener:
        """Stand-in opener raising an HTTPError."""

        def open(self, request, timeout=None):
            """Raise the designated HTTPError."""
            raise err

    return StubOpener()


def test_call_http_error_message_closed(tmp_path, monkeypatch):
    """call closes HTTPError on the error message path."""
    merge_train = load_train(tmp_path, monkeypatch)

    err = urllib.error.HTTPError(
        "https://api.github.test/call", 403, "Forbidden", {}, io.BytesIO(b"refused message")
    )
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    with pytest.raises(merge_train.RefusedError) as caught:
        merge_train.call("https://api.github.test/call")
    assert caught.value.__cause__ is err
    assert "refused message" in str(caught.value)
    assert err.closed


class _UnreadableBody(io.BytesIO):
    """A response body whose read raises the error it is given."""

    def __init__(self, failure: Exception):
        super().__init__(b"")
        self.failure = failure

    def read(self, *args, **kwargs):
        """Raise the given error instead of reading."""
        raise self.failure


class _RecordingBody(io.BytesIO):
    """A response body that records how many bytes each read asked for."""

    def __init__(self, initial: bytes):
        super().__init__(initial)
        self.asked: list[int | None] = []

    def read(self, amt=None):
        """Record the size asked for, then read."""
        self.asked.append(amt)
        return super().read(amt)


@pytest.mark.parametrize(
    "failure",
    [OSError("disk read error"), http.client.IncompleteRead(b"")],
    ids=["oserror", "incomplete-read"],
)
def test_call_http_error_unreadable_body(tmp_path, monkeypatch, failure):
    """Negative: a body that cannot be read still gives a RefusedError with the status.

    The message says the body was unreadable, the HTTPError is its cause, and it is closed.
    """
    merge_train = load_train(tmp_path, monkeypatch)

    err = urllib.error.HTTPError(
        "https://api.github.test/call", 502, "Bad Gateway", {}, _UnreadableBody(failure)
    )
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    with pytest.raises(merge_train.RefusedError) as caught:
        merge_train.call("https://api.github.test/call")
    assert caught.value.__cause__ is err
    assert "HTTP 502" in str(caught.value)
    assert "body unreadable" in str(caught.value)
    assert err.closed


def test_call_http_error_invalid_utf8_body(tmp_path, monkeypatch):
    """Negative: a body that is not UTF-8 is decoded with replacement characters, then closed."""
    merge_train = load_train(tmp_path, monkeypatch)

    err = urllib.error.HTTPError(
        "https://api.github.test/call", 500, "Internal Error", {}, io.BytesIO(b"\xffbad")
    )
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    with pytest.raises(merge_train.RefusedError) as caught:
        merge_train.call("https://api.github.test/call")
    assert caught.value.__cause__ is err
    assert str(caught.value).endswith("HTTP 500 �bad")
    assert err.closed


@pytest.mark.parametrize("size", [499, 500, 501])
def test_call_http_error_body_boundary_truncation(tmp_path, monkeypatch, size):
    """Boundary: the message keeps at most ERROR_TEXT characters of the body."""
    merge_train = load_train(tmp_path, monkeypatch)
    assert merge_train.ERROR_TEXT == 500

    err = urllib.error.HTTPError(
        "https://api.github.test/call", 500, "Error", {}, io.BytesIO(b"a" * size)
    )
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    with pytest.raises(merge_train.RefusedError) as caught:
        merge_train.call("https://api.github.test/call")
    assert str(caught.value).endswith("HTTP 500 " + "a" * min(size, merge_train.ERROR_TEXT))
    assert err.closed


def test_call_http_error_read_bounded(tmp_path, monkeypatch):
    """Boundary: the body is read once, at most 4 * ERROR_TEXT bytes, never to its end."""
    merge_train = load_train(tmp_path, monkeypatch)

    body = _RecordingBody(b"short response")
    err = urllib.error.HTTPError("https://api.github.test/call", 500, "Error", {}, body)
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    with pytest.raises(merge_train.RefusedError):
        merge_train.call("https://api.github.test/call")
    assert body.asked == [4 * merge_train.ERROR_TEXT]
    assert err.closed


def test_call_http_error_redirect_closed(tmp_path, monkeypatch):
    """call closes HTTPError on the redirect path when redirect is False."""
    merge_train = load_train(tmp_path, monkeypatch)

    message = email.message.Message()
    message["Location"] = "https://api.github.test/redirected"
    err = urllib.error.HTTPError(
        "https://api.github.test/call", 302, "Found", message, io.BytesIO(b"")
    )
    monkeypatch.setattr(merge_train.urllib.request, "build_opener", lambda *h: _raising_opener(err))
    res = merge_train.call("https://api.github.test/call", redirect=False)
    assert res == {"location": "https://api.github.test/redirected"}
    assert err.closed


def test_train_script_comes_from_the_workflow_commit():
    assert checkout_problems(ciwf.load(TRAIN)) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("repository", "${{ github.repository }}"),
        ("ref", "${{ github.sha }}"),
        ("persist-credentials", True),
        ("sparse-checkout", "tools/"),
    ],
)
def test_a_checkout_of_another_commit_is_refused(key, value):
    doc = ciwf.load(TRAIN)
    step = next(s for s in doc["jobs"]["train"]["steps"] if is_checkout(s))
    step["with"][key] = value
    assert checkout_problems(doc) == [f"{key}: {value!r}"]


def test_a_checkout_after_the_train_step_is_refused():
    doc = ciwf.load(TRAIN)
    steps = doc["jobs"]["train"]["steps"]
    checkout = next(s for s in steps if is_checkout(s))
    steps.remove(checkout)
    steps.append(checkout)
    assert checkout_problems(doc) == ["0 checkouts before the train step"]


def test_clean_pull_request_lands(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_clean_pull_request_is_queued_where_the_branch_has_a_merge_queue(tmp_path):
    forge = Forge(repository(pull(1, "DIRTY"), pull(2, "CLEAN"), queue=True))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [queued(2)]
    assert "### fleet/app: queued #2" in summary
    assert "| #2 | CLEAN | queued |" in summary


def test_without_the_queue_check_the_merge_is_refused(tmp_path):
    """The planted case: the forge refuses a direct merge where a queue is required."""
    flow = script().replace("    if queue:", "    if False:")
    assert flow != script(), "the queue check is where the test expects it"
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    done, _ = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "Changes must be made through the merge queue" in done.stdout
    assert forge.writes == [landed(1)]


def test_queued_pull_request_is_left_to_the_queue(tmp_path):
    waiting = pull(1, "CLEAN", isInMergeQueue=True)
    forge = Forge(repository(waiting, pull(2, "CLEAN"), pull(3, "BEHIND"), queue=True))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [queued(2)]
    assert "| #1 | CLEAN | in the merge queue |" in summary


def test_missing_queue_entry_fails(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    forge.merged = False
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no merge queue entry" in done.stdout


def test_clean_pull_request_on_second_page_lands(tmp_path):
    forge = Forge(repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31))))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    assert [v.get("after") for _, v in forge.read_calls] == [None, "cursor-25"]


@pytest.mark.parametrize("status", [502, 504])
def test_transient_read_error_is_retried(tmp_path, status):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_statuses["fleet/app"] = [status]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert len(forge.read_calls) == 2


@pytest.mark.parametrize("status", [502, 504])
def test_exhausted_read_retries_fail_closed(tmp_path, status):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_statuses["fleet/app"] = [status] * 4
    done, _ = train(tmp_path, forge)
    assert done.returncode != 0
    assert f"HTTP {status}" in done.stdout + done.stderr
    assert forge.writes == []
    assert [full for full, _ in forge.read_calls] == ["fleet/app"] * 3
    assert forge.read_statuses["fleet/app"] == [status]


def test_read_500_is_not_retried(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_statuses["fleet/app"] = [500]
    done, _ = train(tmp_path, forge)
    assert done.returncode != 0
    assert "HTTP 500" in done.stdout + done.stderr
    assert forge.writes == []
    assert len(forge.read_calls) == 1


@pytest.mark.parametrize(
    "state, operation, write",
    [("CLEAN", "mergePullRequest", landed(1)), ("BEHIND", "enablePullRequestAutoMerge", armed(1))],
)
def test_mutation_502_is_not_retried(tmp_path, state, operation, write):
    forge = Forge(repository(pull(1, state)))
    forge.mutation_statuses[operation] = [502, 502]
    done, _ = train(tmp_path, forge)
    assert done.returncode != 0
    assert "HTTP 502" in done.stdout + done.stderr
    assert forge.writes == [write]
    assert forge.mutation_statuses[operation] == [502]


@pytest.mark.parametrize(
    "count, pages, truncated", [(25, 1, False), (100, 4, False), (120, 4, True)]
)
def test_pull_pages_stop_at_cap(tmp_path, count, pages, truncated):
    forge = Forge(
        repository(*(pull(n, "CLEAN" if n == 101 else "DIRTY") for n in range(1, count + 1)))
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert len(forge.read_calls) == pages
    assert ("Only the 100 oldest open pull requests were read." in summary) == truncated
    assert f"| #{min(count, 100)} |" in summary
    assert "| #101 |" not in summary


def test_page_size_100_is_refused_by_forge(tmp_path):
    source = module()
    mutant = source.replace("first: 25, after:", "first: 100, after:")
    assert mutant != source
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, helper=mutant)
    assert done.returncode != 0
    assert "HTTP 502" in done.stdout + done.stderr
    assert "read page size exceeds 25" in done.stdout + done.stderr
    assert forge.writes == []


def test_unknown_state_rereads_all_pull_pages(tmp_path):
    first = repository(*(pull(n, "UNKNOWN" if n == 30 else "DIRTY") for n in range(1, 31)))
    second = repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31)))
    forge = Forge(first, second)
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    assert [v.get("after") for _, v in forge.read_calls] == [None, "cursor-25"] * 2


def test_second_page_read_retry_keeps_cursor_and_earlier_nodes(tmp_path):
    forge = Forge(repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31))))
    forge.read_statuses["fleet/app"] = [200, 502, 200]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    assert [v.get("after") for _, v in forge.read_calls] == [None, "cursor-25", "cursor-25"]
    assert "| #1 | DIRTY |" in summary


def test_planted_red_pull_request_is_not_landed(tmp_path):
    """The planted case: required checks failed, the forge reports BLOCKED, nothing lands."""
    red = pull(1, "BLOCKED", suites=["SUCCESS", "FAILURE"], labels=())
    forge = Forge(repository(red))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BLOCKED | merge state BLOCKED |" in summary


def test_without_the_state_check_the_red_pull_request_would_land(tmp_path):
    """A mutant that lands whatever it reads is caught by the planted case."""
    mutant = module().replace('if state in conf["accept"]:', "if True:")
    assert mutant != module()
    forge = Forge(repository(pull(1, "BLOCKED", suites=["FAILURE"])))
    done, _ = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"mergeStateStatus": "DIRTY"}, "merge state DIRTY"),
        ({"isDraft": True}, "draft"),
        ({"isCrossRepository": True}, "from another repository: a person lands it"),
        ({"baseRefName": "next"}, "not for the default branch"),
        ({"labels": ("planted",)}, "labelled planted"),
        ({"labels": ("do-not-merge", "hold")}, "labelled do-not-merge"),
        ({"labels": ("superseded",)}, "superseded: a person closes it"),
        ({"body": "Superseded by #9."}, "superseded: a person closes it"),
        ({"headRefName": "release-please--branches--main"}, "a person lands it"),
        ({"mergeStateStatus": "UNSTABLE"}, "merge state UNSTABLE"),
        ({"mergeStateStatus": "HAS_HOOKS"}, "merge state HAS_HOOKS"),
    ],
)
def test_left_alone(tmp_path, fields, reason):
    state = fields.pop("mergeStateStatus", "CLEAN")
    forge = Forge(repository(pull(1, state, **fields)))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert reason in summary


@pytest.mark.parametrize(
    "body",
    ["Supersedes #1.", "Folds three changes; supersedes #4, #1 and #6.", "(supersedes #1)"],
)
def test_planted_superseded_pull_request_stays_unmerged(tmp_path, body):
    """The planted case: #1 is CLEAN, but the open #2 says it replaces #1."""
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "BLOCKED", body=body)))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | CLEAN | superseded: a person closes it |" in summary


def test_without_the_superseded_check_the_replaced_pull_request_would_land(tmp_path):
    mutant = module().replace(
        'if "superseded" in labels or pull["number"] in replaced:', "if False:"
    )
    assert mutant != module()
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "BLOCKED", body="Supersedes #1")))
    done, _ = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


@pytest.mark.parametrize(
    "body", ["Follows up #1.", "Supersedes the old approach; see #1.", "Supersedes #11"]
)
def test_a_mention_that_replaces_nothing_lands(tmp_path, body):
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "BLOCKED", body=body)))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_accepted_state_lands_only_when_declared(tmp_path):
    forge = Forge(repository(pull(1, "UNSTABLE")))
    done, _ = train(tmp_path, forge, accept_states="unstable")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


@pytest.mark.parametrize(
    ("created", "fields", "lands"),
    [
        (YOUNG, {}, False),
        (OLD, {}, True),
        (YOUNG, {"labels": ("security",)}, True),
        (YOUNG, {"title": "fix(deps): update x [SECURITY]"}, True),
        (YOUNG, {"title": "fix(deps): update x to v0.62.0 [security]"}, True),
    ],
    ids=["young", "old", "label", "title", "title-lowercase"],
)
def test_dependency_update_waits_for_the_minimum_age(tmp_path, created, fields, lands):
    update = pull(1, "CLEAN", headRefName="renovate/tool-1.x", createdAt=created, **fields)
    forge = Forge(repository(update))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([landed(1)] if lands else [])


def test_minimum_age_zero_lands_a_young_update(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN", headRefName="dependabot/x", createdAt=YOUNG)))
    done, _ = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_rebased_dependency_update_lands_when_pull_request_is_old_enough(tmp_path):
    four_days_ago = (NOW - timedelta(days=4)).isoformat()
    one_hour_ago = (NOW - timedelta(hours=1)).isoformat()
    update = pull(
        1,
        "CLEAN",
        headRefName="renovate/tool-1.x",
        createdAt=four_days_ago,
        commits=[(one_hour_ago, 1)],
    )
    forge = Forge(repository(update))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_dependency_update_younger_than_minimum_age_does_not_land(tmp_path):
    one_day_ago = (NOW - timedelta(days=1)).isoformat()
    update = pull(
        1,
        "CLEAN",
        headRefName="renovate/tool-1.x",
        createdAt=one_day_ago,
    )
    forge = Forge(repository(update))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    created_dt = datetime.fromisoformat(one_day_ago)
    expected = (
        f"dependency update opened {created_dt:%Y-%m-%d %H:%M}Z, younger than the minimum age"
    )
    assert expected in summary


def test_without_pull_request_age_the_rebased_update_does_not_land(tmp_path):
    read_from_commit = (
        'created = datetime.fromisoformat(pull["history"]["nodes"][0]["commit"]["committedDate"])'
    )
    mutant = module().replace(
        'created = datetime.fromisoformat(pull["createdAt"])',
        read_from_commit,
    )
    assert mutant != module()
    four_days_ago = (NOW - timedelta(days=4)).isoformat()
    one_hour_ago = (NOW - timedelta(hours=1)).isoformat()
    update = pull(
        1,
        "CLEAN",
        headRefName="renovate/tool-1.x",
        createdAt=four_days_ago,
        commits=[(one_hour_ago, 1)],
    )
    forge = Forge(repository(update))
    done, summary = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    commit_dt = datetime.fromisoformat(one_hour_ago)
    expected = f"dependency update opened {commit_dt:%Y-%m-%d %H:%M}Z, younger than the minimum age"
    assert expected in summary


def test_one_landing_per_run(tmp_path):
    forge = Forge(repository(pull(1, "DIRTY"), pull(2, "CLEAN"), pull(3, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]


def test_behind_pull_request_is_armed_and_updated(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), pull(2, "BEHIND")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(1), updated(1)]
    assert "| #1 | BEHIND | armed, updated from the default branch |" in summary


def test_unarmed_behind_is_not_updated_without_auto_merge(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), auto_merge=False))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BEHIND | behind: auto-merge off, not armed, not updated |" in summary


def test_armed_behind_is_updated_without_auto_merge(tmp_path):
    waiting = pull(2, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(pull(1, "BEHIND"), waiting, auto_merge=False))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]


@pytest.mark.parametrize(
    "waiting",
    [{}, {"autoMergeRequest": {"enabledAt": OLD}}],
    ids=["unarmed", "armed"],
)
@pytest.mark.parametrize("rules", [("REQUIRED_LINEAR_HISTORY",), None], ids=["linear", "unread"])
def test_linear_history_branch_is_never_updated(tmp_path, waiting, rules):
    """A merge commit from the train would break the rule (decision record 0044)."""
    kinds = rules and ("REQUIRED_SIGNATURES", *rules)
    forge = Forge(repository(pull(1, "BEHIND", **waiting), rules=kinds))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BEHIND | behind: linear history, a person rebases |" in summary


def test_signed_branch_without_linear_history_is_armed_and_updated(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), rules=("REQUIRED_SIGNATURES",)))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(1), updated(1)]


def test_red_behind_pull_request_is_skipped(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND", suites=["TIMED_OUT"]), pull(2, "BEHIND")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(2), updated(2)]


def test_armed_pull_request_in_flight_holds_the_train(tmp_path):
    flying = pull(1, "BLOCKED", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(flying, pull(2, "BEHIND")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "#1 is armed and in flight" in summary


def test_red_armed_pull_request_does_not_hold_the_train(tmp_path):
    stuck = pull(1, "BLOCKED", autoMergeRequest={"enabledAt": OLD}, suites=["FAILURE"])
    forge = Forge(repository(stuck, pull(2, "BEHIND")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(2), updated(2)]


def test_armed_behind_pull_request_is_updated(tmp_path):
    """An armed BEHIND pull request cannot land before the update, so it holds nothing."""
    waiting = pull(1, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(waiting, pull(2, "BEHIND")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert "armed and in flight" not in summary
    assert "| #1 | BEHIND | updated from the default branch |" in summary


def test_armed_behind_goes_before_an_older_unarmed_one(tmp_path):
    waiting = pull(2, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(pull(1, "BEHIND"), waiting))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]


def test_conflict_moves_to_the_next_pull_request(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), pull(2, "BEHIND")))
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(1), updated(1), armed(2), updated(2)]
    assert "| #1 | BEHIND | conflicts with the default branch |" in summary


def test_no_new_commits_is_no_error(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), pull(2, "BEHIND")))
    forge.update_answers[1] = (422, NO_NEW)
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(1), updated(1)]


def test_other_update_refusal_fails(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND")))
    forge.update_answers[1] = (403, "Resource not accessible by integration")
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "::error title=Merge train refused::" in done.stdout


def test_refused_merge_fails(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.refuse_merge = "Required status check is failing"
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "Required status check is failing" in done.stdout


def test_merge_already_in_progress_is_in_flight(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = [MERGE_IN_PROGRESS]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "merge in flight #1" in summary
    assert "Refused:" not in summary


@pytest.mark.parametrize(
    "errors",
    [
        [{**MERGE_IN_PROGRESS, "message": "Pull request is not mergeable"}],
        [{**MERGE_IN_PROGRESS, "type": "FORBIDDEN"}],
        [MERGE_IN_PROGRESS, {"type": "UNPROCESSABLE", "message": "Head branch was modified"}],
    ],
)
def test_other_merge_errors_stay_refused(tmp_path, errors):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = errors
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr


def test_merge_not_reported_fails(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merged = False
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr


def test_unknown_state_is_read_again(tmp_path):
    forge = Forge(repository(pull(1, "UNKNOWN")), repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_switched_off_writes_nothing(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "BEHIND")))
    done, summary = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "switched off" in summary


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("switch", "yes"),
        ("merge_method", "REBASE"),
        ("accept_states", "BLOCKED"),
        ("accept_states", "DIRTY"),
        ("min_age_days", "-1"),
        ("min_age_days", "31"),
        ("min_age_days", "two"),
        ("release_off", "justname"),
        ("release_max_parallel", "0"),
        ("release_max_parallel", "4"),
        ("release_max_parallel", "two"),
    ],
)
def test_invalid_setting_stops_before_any_call(tmp_path, key, value):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, **{key: value})
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert (forge.minted, forge.writes) == ([], [])


@pytest.mark.parametrize(
    ("status", "settings"),
    [
        pytest.param(3, {"merge_method": "REBASE"}, id="invalid_setting"),
        pytest.param(0, {}, id="clean_pull_lands"),
    ],
)
def test_entrypoint_exit_status(tmp_path, status, settings):
    """The script run as a program exits with the status main() returns.

    LAUNCH calls main() directly, so only this test holds the exit status of the entrypoint.
    """
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, text=script(), **settings)
    assert done.returncode == status, done.stdout + done.stderr
    if status == 3:
        assert done.stdout.startswith("::error title=Merge train setting::")
        assert (forge.minted, forge.writes) == ([], [])
    else:
        assert forge.writes == [landed(1)]


@pytest.mark.parametrize("parallel", ["1", "3"])
def test_release_max_parallel_boundaries_accepted(tmp_path, parallel):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, release_max_parallel=parallel)
    assert done.returncode == 0, done.stdout + done.stderr


def test_merge_method_merge_is_passed(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("merge", "PR_1", f"{1:040x}", "MERGE")]


def test_token_and_key_are_never_printed(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.refuse_merge = "refused"
    done, summary = train(tmp_path, forge)
    out = done.stdout + done.stderr + summary + (tmp_path / "output").read_text(encoding="utf-8")
    assert "planted-token" not in out and "list-token" not in out
    body = "".join(app_key()[0].splitlines()[1:-1])
    assert body[:40] not in out.replace("\n", "")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "jwt.pub",
        "jwt.sig",
        "output",
        "summary.md",
        "train_rules.py",
    ]


@pytest.mark.parametrize("name", ["train", "train_rules"])
def test_script_passes_the_python_linter(tmp_path, name):
    path = tmp_path / f"{name}.py"
    path.write_text(script() if name == "train" else module(), encoding="utf-8")
    ruff = ROOT / ".venv/bin/ruff"
    which_ruff = shutil.which("ruff")
    if ruff.exists():
        command = [str(ruff)]
    elif which_ruff:
        command = [which_ruff]
    else:
        command = [sys.executable, "-m", "ruff"]
    done = subprocess.run(
        [*command, "check", "--config", str(ROOT / "ruff.toml"), str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


MANIFEST = ROOT / "apps/merge-train.json"


def test_app_rights_are_the_rights_the_train_asks_for(tmp_path):
    """The app holds what the train's tokens ask for, metadata read (always granted), no more."""
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.minted == [
        (7, {"permissions": {"metadata": "read"}}),
        (7, {"permissions": WRITE, "repositories": ["app"]}),
    ]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["default_permissions"] == {**WRITE, **ISSUES, "metadata": "read"}


def test_switched_off_asks_for_read_rights_only(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.minted[1] == (7, {"permissions": READ, "repositories": ["app"]})


def test_app_has_no_webhook_no_events_and_no_user_authorisation():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["default_events"] == []
    assert manifest["request_oauth_on_install"] is False
    assert "name" not in manifest, "the handover names the app"


def test_app_is_public_so_every_fleet_account_can_install_it():
    """One app owned by plurano; a private app installs on its own account only."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["public"] is True


def test_github_token_gets_no_permission():
    doc = ciwf.load(TRAIN)
    assert doc["permissions"] == {}
    assert doc["jobs"]["train"]["permissions"] == {}


def sweep_forge() -> Forge:
    """fleet/app lands #1, other/b updates #2, other/old is archived, gone is suspended."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.install(9, "other", b=[repository(pull(2, "BEHIND"))], old=[repository(pull(3, "CLEAN"))])
    forge.install(11, "gone", True, c=[repository(pull(4, "CLEAN"))])
    forge.archived.add("other/old")
    return forge


def test_sweep_runs_the_train_in_every_repository_of_the_app(tmp_path):
    forge = sweep_forge()
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.where == [
        ("fleet/app", landed(1)),
        ("other/b", armed(2)),
        ("other/b", updated(2)),
    ]
    assert "| fleet | 1 | 1 | 0 | 0 |" in summary
    assert "| other | 1 | 0 | 1 | 0 |" in summary
    assert "| gone | suspended | | | |" in summary
    assert "### fleet/app: landed #1" in summary
    assert "### other/b:" in summary


def test_empty_repository_does_not_stop_the_sweep(tmp_path):
    empty = repository()
    empty["defaultBranchRef"] = None
    forge = Forge(empty)
    forge.install(9, "other", b=[repository(pull(2, "CLEAN"))])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.where == [("other/b", landed(2))]
    assert "### other/b: landed #2" in summary


def test_sweep_mints_one_token_per_repository_and_revokes_each(tmp_path):
    forge = sweep_forge()
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.minted == [
        (7, {"permissions": {"metadata": "read"}}),
        (7, {"permissions": WRITE, "repositories": ["app"]}),
        (9, {"permissions": {"metadata": "read"}}),
        (9, {"permissions": WRITE, "repositories": ["b"]}),
    ]
    assert sorted(forge.revoked) == sorted(forge.tokens)


def test_without_revocation_tokens_outlive_the_sweep(tmp_path):
    mutant = script().replace("def revoke(env, token):\n", "def revoke(env, token):\n    return\n")
    assert mutant != script()
    forge = sweep_forge()
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.revoked == []


def test_without_the_repository_limit_the_forge_refuses_the_token(tmp_path):
    mutant = script().replace('rights, [target["name"]])', "rights)")
    assert mutant != script()
    forge = sweep_forge()
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.where == []


def test_refusal_in_one_repository_does_not_stop_the_others(tmp_path):
    forge = sweep_forge()
    forge.refuse_merge_in.add("fleet/app")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert ("other/b", updated(2)) in forge.where
    assert "::error title=Merge train refused::fleet/app: " in done.stdout
    assert "| fleet | 1 | 0 | 0 | 1 |" in summary
    assert sorted(forge.revoked) == sorted(forge.tokens)


def ungranted_forge() -> Forge:
    """sweep_forge plus the account denied, whose installation 13 has not accepted the rights."""
    forge = sweep_forge()
    forge.install(13, "denied", x=[repository(pull(5, "CLEAN"))], y=[repository(pull(6, "CLEAN"))])
    forge.ungranted.add(13)
    return forge


def test_installation_without_the_rights_is_skipped_and_the_others_run(tmp_path):
    forge = ungranted_forge()
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert ("fleet/app", landed(1)) in forge.where
    assert ("other/b", updated(2)) in forge.where
    assert [m for m in forge.minted if m[0] == 13 and "repositories" in m[1]] == [
        (13, {"permissions": WRITE, "repositories": ["x"]})
    ]
    refusal = (
        "::error title=Merge train refused::denied: installation 13 has not accepted "
        "the rights the train asks for; 2 repositories skipped: "
    )
    assert refusal in done.stdout
    assert "| denied | 2 | 0 | 0 | 2 |" in summary
    assert sorted(forge.revoked) == sorted(forge.tokens)


def test_without_the_installation_skip_each_repository_asks_again(tmp_path):
    mutant = script().replace("except UngrantedError as err:", "except KeyError as err:")
    assert mutant != script()
    forge = ungranted_forge()
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 1, done.stdout + done.stderr
    assert len([m for m in forge.minted if m[0] == 13 and "repositories" in m[1]]) == 2
    assert "installation 13 has not accepted" not in done.stdout


def test_without_collecting_refusals_one_repository_stops_the_sweep(tmp_path):
    mutant = script().replace(
        '            except RefusedError as err:\n                tally["refused"] += 1\n',
        '            except KeyError as err:\n                tally["refused"] += 1\n',
    )
    assert mutant != script()
    forge = sweep_forge()
    forge.refuse_merge_in.add("fleet/app")
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 1, done.stdout + done.stderr
    assert ("other/b", updated(2)) not in forge.where


def test_key_of_another_app_is_refused(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.public = openssl("pkey", "-pubout", data=openssl("genpkey", "-algorithm", "RSA"))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "::error title=Merge train refused::" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


@pytest.mark.parametrize(
    ("key", "value"),
    [("app_client_id", ""), ("app_key", ""), ("app_key", "not a key")],
)
def test_missing_app_credential_stops_before_any_call(tmp_path, key, value):
    """An environment secret the caller did not pass arrives empty; the run says so."""
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, **{key: value})
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert (forge.minted, forge.where) == ([], [])


def load_train(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The merge train script, imported with its rules module written to RUNNER_TEMP."""
    (tmp_path / "train_rules.py").write_text(module(), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    spec = importlib.util.spec_from_file_location("merge_train", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "bits",
    [
        pytest.param(1024, id="1024-bits"),
        pytest.param(2047, id="2047-bits"),
    ],
)
def test_short_app_key_is_refused(tmp_path, bits):
    """An RSA app key shorter than 2048 bits is refused before any forge call."""
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, app_key=app_key(bits)[0])
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert str(bits) in done.stdout
    assert "2048" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


@pytest.mark.parametrize(
    "bits",
    [
        pytest.param(2048, id="2048-bits"),
        pytest.param(4096, id="4096-bits"),
    ],
)
def test_sufficient_app_key_is_accepted(tmp_path, bits):
    """An RSA app key of at least 2048 bits is accepted and proceeds to the forge."""
    priv, pub = app_key(bits)
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.public = pub
    done, _ = train(tmp_path, forge, app_key=priv)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("merge", "PR_1", f"{1:040x}", "SQUASH")]


@pytest.mark.parametrize(
    ("bits", "expected"),
    [
        pytest.param(2048, 2048, id="2048-bits"),
        pytest.param(1024, 1024, id="1024-bits"),
    ],
)
def test_key_bits_reads_length_directly(tmp_path, monkeypatch, bits, expected):
    """The key_bits helper measures the key's bit length through a pipe."""
    train_mod = load_train(tmp_path, monkeypatch)
    assert train_mod.key_bits(app_key(bits)[0]) == expected


def test_key_bits_timeout_raises_invalid_setting_error(tmp_path, monkeypatch):
    """A subprocess timeout in key_bits raises InvalidSettingError."""
    train_mod = load_train(tmp_path, monkeypatch)
    key = app_key(2048)[0]

    def mock_run(*args, **kwargs):
        """Stand in for subprocess.run: every openssl call times out."""
        raise subprocess.TimeoutExpired(cmd=["openssl"], timeout=train_mod.TIMEOUT)

    monkeypatch.setattr(train_mod.subprocess, "run", mock_run)
    with pytest.raises(train_mod.InvalidSettingError) as exc_info:
        train_mod.key_bits(key)
    assert f"timed out after {train_mod.TIMEOUT}" in str(exc_info.value)
    assert str(train_mod.MIN_KEY_BITS) in str(exc_info.value)


def test_app_jwt_timeout_raises_refused_error(tmp_path, monkeypatch):
    """A subprocess timeout in app_jwt raises RefusedError."""
    train_mod = load_train(tmp_path, monkeypatch)
    env = {"TRAIN_APP_KEY": app_key(2048)[0]}

    def mock_run(*args, **kwargs):
        """Stand in for subprocess.run: every openssl call times out."""
        raise subprocess.TimeoutExpired(cmd=["openssl"], timeout=train_mod.TIMEOUT)

    monkeypatch.setattr(train_mod.subprocess, "run", mock_run)
    conf = {"client_id": "test-client-id"}
    with pytest.raises(train_mod.RefusedError) as exc_info:
        train_mod.app_jwt(env, conf)
    assert f"timed out after {train_mod.TIMEOUT}" in str(exc_info.value)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("DSA", id="dsa-2048"),
        pytest.param("EC", id="ec-p256"),
        pytest.param("Ed25519", id="ed25519"),
        pytest.param("RSA-PSS", id="rsa-pss-2048"),
    ],
)
def test_non_rsa_app_key_is_refused(tmp_path, kind):
    """A non-RSA app key is refused before any forge call."""
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, app_key=non_rsa_app_key(kind))
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert "RSA" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


def leaked(body: str, out: str) -> list[str]:
    """Return every 12-character window of body found in out."""
    if len(body) < 12:
        return []
    return [body[i : i + 12] for i in range(len(body) - 12 + 1) if body[i : i + 12] in out]


def test_leaked_finds_sixteen_character_fragment():
    """A 16-character fragment of the key body embedded in other text is detected."""
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = f"prefix_{body[4:20]}_suffix"
    found = leaked(body, out)
    assert len(found) == 5
    assert found[0] == body[4:16]


def test_leaked_finds_twelve_character_fragment():
    """A 12-character fragment of the key body embedded in other text is detected."""
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = f"prefix_{body[10:22]}_suffix"
    assert leaked(body, out) == [body[10:22]]


def test_leaked_returns_empty_when_no_fragment_present():
    """Text containing no fragment of the key body yields an empty list."""
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = "completely_different_text_without_matching_chunks"
    assert leaked(body, out) == []


def test_leaked_returns_empty_for_eleven_character_boundary_fragment():
    """An 11-character fragment is below the 12-character window threshold and returns empty."""
    body = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    out = f"prefix_{body[4:15]}_suffix"
    assert leaked(body, out) == []


def test_refused_app_key_is_never_printed(tmp_path):
    """The private key material never appears in the output of a refused run."""
    forge = Forge(repository(pull(1, "CLEAN")))
    key_pem = app_key(1024)[0]
    done, summary = train(tmp_path, forge, app_key=key_pem)
    assert done.returncode == 3, done.stdout + done.stderr
    out = (done.stdout + done.stderr + summary).replace("\n", "")
    body = "".join(key_pem.splitlines()[1:-1])
    assert leaked(body, out) == []
    assert "PRIVATE KEY" not in out


def test_unreadable_app_key_is_refused(tmp_path):
    """An unreadable app key is refused before any forge call."""
    lines = app_key(2048)[0].splitlines()
    corrupted = f"{lines[0]}\ncorrupted\n{lines[-1]}\n"
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, app_key=corrupted)
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert "2048" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


def test_oversized_app_key_is_refused(tmp_path):
    """An app key longer than MAX_KEY_CHARS is refused before any forge call."""
    lines = app_key(2048)[0].splitlines()
    header = f"{lines[0]}\n"
    footer = f"\n{lines[-1]}\n"
    filler = "A" * (16384 + 1 - len(header) - len(footer))
    oversized = header + filler + footer
    assert len(oversized) == 16384 + 1
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, app_key=oversized)
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert "16384" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


def test_app_key_at_maximum_length_boundary_is_accepted(tmp_path):
    """An RSA app key padded to exactly MAX_KEY_CHARS is accepted and proceeds."""
    priv, pub = app_key(2048)
    padded = priv + "\n" * (16384 - len(priv))
    assert len(padded) == 16384
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.public = pub
    done, _ = train(tmp_path, forge, app_key=padded)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("merge", "PR_1", f"{1:040x}", "SQUASH")]


def shrink_pipes(train_mod, monkeypatch):
    """Set the capacity of both ends of train_mod.os.pipe to 4096 bytes."""
    real_pipe = train_mod.os.pipe

    def small_pipe():
        """Create a pipe with 4096-byte capacity on each end."""
        r, w = real_pipe()
        fcntl.fcntl(r, PIPE_SIZE, 4096)
        fcntl.fcntl(w, PIPE_SIZE, 4096)
        return r, w

    monkeypatch.setattr(train_mod.os, "pipe", small_pipe)


@pytest.mark.skipif(PIPE_SIZE is None, reason=NO_PIPE_SIZE)
def test_key_longer_than_pipe_capacity_key_bits(tmp_path, monkeypatch):
    """key_bits reads an RSA key longer than the 4096-byte pipe buffer without hanging."""
    train_mod = load_train(tmp_path, monkeypatch)
    shrink_pipes(train_mod, monkeypatch)
    key = ("x" * 63 + "\n") * 100 + app_key(2048)[0]
    result = {}

    def worker():
        """Read key_bits in a background thread."""
        try:
            result["bits"] = train_mod.key_bits(key)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive()
    assert result == {"bits": 2048}


@pytest.mark.skipif(PIPE_SIZE is None, reason=NO_PIPE_SIZE)
def test_key_longer_than_pipe_capacity_app_jwt(tmp_path, monkeypatch):
    """app_jwt signs a JWT using a key longer than the 4096-byte pipe buffer without hanging."""
    train_mod = load_train(tmp_path, monkeypatch)
    shrink_pipes(train_mod, monkeypatch)
    key = ("x" * 63 + "\n") * 100 + app_key(2048)[0]
    env = {"TRAIN_APP_KEY": key}
    conf = {"client_id": "test-client-id"}
    result = {}

    def worker():
        """Sign app_jwt in a background thread."""
        try:
            result["jwt"] = train_mod.app_jwt(env, conf)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(20)
    assert not thread.is_alive()
    assert "error" not in result, result
    token = result.get("jwt")
    assert isinstance(token, str)
    assert len(token.split(".")) == 3


def test_key_bits_pipe_oserror_raises_invalid_setting_error(tmp_path, monkeypatch):
    """An OSError from os.pipe in key_bits raises InvalidSettingError."""
    train_mod = load_train(tmp_path, monkeypatch)

    def mock_pipe():
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(train_mod.os, "pipe", mock_pipe)
    with pytest.raises(train_mod.InvalidSettingError) as exc_info:
        train_mod.key_bits(app_key(2048)[0])
    msg = str(exc_info.value)
    assert "Too many open files" in msg or "EMFILE" in msg
    assert f"at least {train_mod.MIN_KEY_BITS} bits are required" in msg


def test_app_jwt_pipe_oserror_raises_refused_error(tmp_path, monkeypatch):
    """An OSError from os.pipe in app_jwt raises RefusedError."""
    train_mod = load_train(tmp_path, monkeypatch)

    def mock_pipe():
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(train_mod.os, "pipe", mock_pipe)
    env = {"TRAIN_APP_KEY": app_key(2048)[0]}
    conf = {"client_id": "test-client-id"}
    with pytest.raises(train_mod.RefusedError) as exc_info:
        train_mod.app_jwt(env, conf)
    msg = str(exc_info.value)
    assert "Too many open files" in msg or "EMFILE" in msg


def test_os_failure_names_the_type_and_description_only(tmp_path, monkeypatch):
    """os_failure gives the type and strerror without errno or file name, else the plain text."""
    train_mod = load_train(tmp_path, monkeypatch)
    missing = FileNotFoundError(errno.ENOENT, "No such file or directory", "key.pem")
    assert train_mod.os_failure(missing) == "FileNotFoundError: No such file or directory"
    assert train_mod.os_failure(OSError("plain text")) == "OSError: plain text"


@pytest.mark.skipif(FD_DIR is None, reason=NO_FD_DIR)
def test_key_bits_thread_runtime_error_raises_invalid_setting_error_and_leaks_no_fds(
    tmp_path, monkeypatch
):
    """A RuntimeError in thread start in key_bits raises InvalidSettingError and leaks no fds."""
    train_mod = load_train(tmp_path, monkeypatch)

    class FailingThread(train_mod.threading.Thread):
        def start(self):
            raise RuntimeError("thread creation failed")

    monkeypatch.setattr(train_mod.threading, "Thread", FailingThread)
    fds_before = len(os.listdir(FD_DIR))
    with pytest.raises(train_mod.InvalidSettingError) as exc_info:
        train_mod.key_bits(app_key(2048)[0])
    fds_after = len(os.listdir(FD_DIR))
    assert fds_after == fds_before
    msg = str(exc_info.value)
    assert "thread creation failed" in msg or "RuntimeError" in msg
    assert f"at least {train_mod.MIN_KEY_BITS} bits are required" in msg


@pytest.mark.skipif(FD_DIR is None, reason=NO_FD_DIR)
def test_app_jwt_thread_runtime_error_raises_refused_error_and_leaks_no_fds(tmp_path, monkeypatch):
    """A RuntimeError in thread start in app_jwt raises RefusedError and leaks no fds."""
    train_mod = load_train(tmp_path, monkeypatch)

    class FailingThread(train_mod.threading.Thread):
        def start(self):
            raise RuntimeError("thread creation failed")

    monkeypatch.setattr(train_mod.threading, "Thread", FailingThread)
    env = {"TRAIN_APP_KEY": app_key(2048)[0]}
    conf = {"client_id": "test-client-id"}
    fds_before = len(os.listdir(FD_DIR))
    with pytest.raises(train_mod.RefusedError) as exc_info:
        train_mod.app_jwt(env, conf)
    fds_after = len(os.listdir(FD_DIR))
    assert fds_after == fds_before
    msg = str(exc_info.value)
    assert "thread creation failed" in msg or "RuntimeError" in msg


def patch_openssl_which(tmp_path, monkeypatch, train_mod, marker):
    """Point shutil.which at a wrapper that touches marker and runs openssl; record each run.

    Returns the wrapper path and the list of (argument list, executable) pairs.
    """
    wrapper = tmp_path / "openssl_wrapper.sh"
    wrapper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexec openssl "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setattr(
        train_mod.shutil,
        "which",
        lambda cmd: str(wrapper) if cmd == "openssl" else None,
    )
    calls = []
    real_run = train_mod.subprocess.run

    def recording_run(cmd, *args, **kwargs):
        """Record the argument list and the executable, then make the real call."""
        calls.append((cmd, kwargs.get("executable")))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(train_mod.subprocess, "run", recording_run)
    return str(wrapper), calls


def run_app_jwt(train_mod):
    """Sign a token with a fresh 2048-bit key and return it."""
    env = {"TRAIN_APP_KEY": app_key(2048)[0]}
    conf = {"client_id": "test-client-id"}
    return train_mod.app_jwt(env, conf)


def test_key_bits_uses_openssl_path_from_shutil_which(tmp_path, monkeypatch):
    """key_bits runs the openssl that shutil.which found."""
    train_mod = load_train(tmp_path, monkeypatch)
    marker = tmp_path / "marker_key_bits"
    patch_openssl_which(tmp_path, monkeypatch, train_mod, marker)
    assert train_mod.key_bits(app_key(2048)[0]) == 2048
    assert marker.is_file()


def test_app_jwt_uses_openssl_path_from_shutil_which(tmp_path, monkeypatch):
    """app_jwt runs the openssl that shutil.which found."""
    train_mod = load_train(tmp_path, monkeypatch)
    marker = tmp_path / "marker_app_jwt"
    patch_openssl_which(tmp_path, monkeypatch, train_mod, marker)
    assert len(run_app_jwt(train_mod).split(".")) == 3
    assert marker.is_file()


def test_key_bits_passes_literal_openssl_and_executable_path(tmp_path, monkeypatch):
    """key_bits passes the literal "openssl" first and the which path as executable."""
    train_mod = load_train(tmp_path, monkeypatch)
    which_path, calls = patch_openssl_which(tmp_path, monkeypatch, train_mod, tmp_path / "m")
    assert train_mod.key_bits(app_key(2048)[0]) == 2048
    assert [(cmd[0], executable) for cmd, executable in calls] == [("openssl", which_path)]


def test_app_jwt_passes_literal_openssl_and_executable_path(tmp_path, monkeypatch):
    """app_jwt passes the literal "openssl" first and the which path as executable."""
    train_mod = load_train(tmp_path, monkeypatch)
    which_path, calls = patch_openssl_which(tmp_path, monkeypatch, train_mod, tmp_path / "m")
    assert len(run_app_jwt(train_mod).split(".")) == 3
    assert [(cmd[0], executable) for cmd, executable in calls] == [("openssl", which_path)]


def test_key_pipe_joins_the_writer_before_returning(tmp_path, monkeypatch):
    """key_pipe waits for the background writer thread to terminate before returning."""
    train_mod = load_train(tmp_path, monkeypatch)
    script = tmp_path / "failing_openssl.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    monkeypatch.setattr(
        train_mod.shutil,
        "which",
        lambda cmd: str(script) if cmd == "openssl" else None,
    )

    real_write_key = train_mod.write_key
    event = threading.Event()

    def wrapped_write_key(write_end, key):
        """Sleep two seconds before calling real write_key and signaling completion."""
        time.sleep(2)
        real_write_key(write_end, key)
        event.set()

    monkeypatch.setattr(train_mod, "write_key", wrapped_write_key)
    assert train_mod.key_bits(app_key(2048)[0]) is None
    assert event.is_set()


def key_on_disk(text: str) -> list[str]:
    """Ways the script could put the key in a file; empty when the key goes through a pipe only."""
    found = [word for word in ("tempfile", "write_text", "write_bytes") if word in text]
    if len(re.findall(r"(?<![.\w])open\(", text)) != 1:
        found.append("open() other than the summary file")
    if "pass_fds=(read_end,)" not in text:
        found.append("no pipe to openssl")
    return found


def test_key_reaches_openssl_through_a_pipe_only():
    assert key_on_disk(script()) == []


def test_key_written_to_a_file_is_found():
    mutant = script().replace(
        "    read_end, write_end = os.pipe()\n",
        "    read_end, write_end = os.pipe()\n"
        "    with open('key.pem', 'w') as kept:\n        kept.write(env['TRAIN_APP_KEY'])\n",
    )
    assert mutant != script()
    assert key_on_disk(mutant) == ["open() other than the summary file"]


def test_job_reads_the_key_from_the_environment_merge_train():
    job = ciwf.load(TRAIN)["jobs"]["train"]
    assert job["environment"] == "merge-train"
    env = [s for s in job["steps"] if s.get("name") == STEP][0]["env"]
    assert env["TRAIN_APP_KEY"] == "${{ secrets.app_private_key }}"
    assert env["TRAIN_APP_CLIENT_ID"] == "${{ inputs.app_client_id }}"


RELEASE_PLEASE = "googleapis/release-please-action@"
APP_TOKEN = "actions/create-github-app-token@"
TOKEN_RIGHTS = {
    "permission-contents": "write",
    "permission-pull-requests": "write",
    "permission-workflows": "write",
}


def release_forge() -> Forge:
    """fleet/app has both release-please files, other/b only the config, other/c none."""
    forge = Forge(repository(pull(1, "CLEAN"), release=(True, True)))
    forge.install(
        9,
        "other",
        b=[repository(release=(True, False))],
        c=[repository()],
        d=[repository(release=(True, True))],
        old=[repository(release=(True, True))],
    )
    forge.archived.add("other/old")
    return forge


def test_sweep_names_the_repositories_with_release_please(tmp_path):
    forge = release_forge()
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [
        {"owner": "fleet", "name": "app"},
        {"owner": "other", "name": "d"},
    ]
    assert "Release pull requests and releases next: fleet/app, other/d" in summary


def test_release_off_skips_listed_repository_and_reports_it(tmp_path):
    forge = release_forge()
    done, summary = train(tmp_path, forge, release_off="other/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release pull requests and releases next: fleet/app" in summary
    assert "Release train off: other/d" in summary


def test_release_off_is_case_insensitive(tmp_path):
    forge = release_forge()
    done, summary = train(tmp_path, forge, release_off="Other/D")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release pull requests and releases next: fleet/app" in summary
    assert "Release train off: other/d" in summary


def test_without_the_release_off_check_a_listed_repository_is_named(tmp_path):
    mutant = script().replace('target["full_name"].lower() in conf["release_off"]', "False")
    assert mutant != script()
    done, _ = train(tmp_path, release_forge(), mutant, release_off="other/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert {"owner": "other", "name": "d"} in releases_of(tmp_path)


def test_without_the_manifest_check_a_config_alone_is_named(tmp_path):
    mutant = script().replace(
        'bool(repo["releaseConfig"] and repo["releaseManifest"])', 'bool(repo["releaseConfig"])'
    )
    assert mutant != script()
    done, _ = train(tmp_path, release_forge(), mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert {"owner": "other", "name": "b"} in releases_of(tmp_path)


def test_release_list_is_written_when_a_repository_refused(tmp_path):
    """A refusal in fleet/app keeps it off the list; the other repositories still get their run."""
    forge = release_forge()
    forge.refuse_merge_in.add("fleet/app")
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "other", "name": "d"}]


def test_no_release_list_without_a_sweep(tmp_path):
    forge = release_forge()
    forge.public = openssl("pkey", "-pubout", data=openssl("genpkey", "-algorithm", "RSA"))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert releases_of(tmp_path) is None


def test_release_list_is_bounded_by_the_matrix(tmp_path):
    mutant = script().replace("MAX_RELEASES = 256", "MAX_RELEASES = 1")
    assert mutant != script()
    done, summary = train(tmp_path, release_forge(), mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "No release run, the job matrix holds 1: other/d" in summary


def frozen_findings(steps: list, pr_step: dict) -> list[str]:
    """What lets release-please update a release pull request a person froze."""
    found = []
    if pr_step.get("if") != "steps.settings.outputs.frozen == 'false'":
        found.append("pull-request step updates a frozen release pull request")
    reading = [s for s in steps if str(s.get("run", "")).endswith('release_labels.py" settings')]
    if len(reading) != 1 or reading[0].get("env", {}).get("HOLD_LABELS") != (
        "${{ inputs.hold_labels }}"
    ):
        found.append("settings step does not read the hold labels")
    return found


def release_job_findings(doc: dict) -> list[str]:
    """What keeps the release job from running release-please with the app's own token."""
    found = []
    job = doc["jobs"].get("release", {})
    steps = job.get("steps", [])
    minting = [s for s in steps if str(s.get("uses", "")).startswith(APP_TOKEN)]
    running = [s for s in steps if str(s.get("uses", "")).startswith(RELEASE_PLEASE)]
    if len(minting) != 1 or len(running) != 2:
        return [f"{len(minting)} token steps and {len(running)} release-please steps"]
    mint = minting[0].get("with", {})
    if mint.get("owner") != "${{ matrix.repository.owner }}":
        found.append("token not minted for the repository's owner")
    if mint.get("repositories") != "${{ matrix.repository.name }}":
        found.append("token not limited to the repository")
    rights = {key: value for key, value in mint.items() if key.startswith("permission-")}
    if rights != TOKEN_RIGHTS:
        found.append(f"token rights {rights}")
    expected_token = f"${{{{ steps.{minting[0].get('id')}.outputs.token }}}}"
    expected_repo = "${{ matrix.repository.owner }}/${{ matrix.repository.name }}"
    create_step, pr_step = running[0], running[1]
    create_with = create_step.get("with", {})
    pr_with = pr_step.get("with", {})
    if create_with.get("token") != expected_token:
        found.append(f"release step token is {create_with.get('token')!r}, not the app's")
    if pr_with.get("token") != expected_token:
        found.append(f"pull-request step token is {pr_with.get('token')!r}, not the app's")
    if create_with.get("repo-url") != expected_repo:
        found.append("release step runs on another repository")
    if pr_with.get("repo-url") != expected_repo:
        found.append("pull-request step runs on another repository")
    if create_with.get("skip-github-pull-request") not in (True, "true"):
        found.append("release step does not skip pull requests")
    if create_step.get("continue-on-error") not in (True, "true"):
        found.append("release step does not continue on error")
    if "steps.settings.outputs.skip == 'false'" not in str(create_step.get("if", "")):
        found.append("release step does not check skip setting")
    if pr_with.get("skip-github-release") not in (True, "true"):
        found.append("pull-request step does not skip releases")
    found += frozen_findings(steps, pr_step)
    judging = [s for s in steps if str(s.get("run", "")).endswith('release_labels.py" after')]
    judge_env = judging[0].get("env", {}) if len(judging) == 1 else {}
    outcome = f"${{{{ steps.{create_step.get('id')}.outcome }}}}"
    if (judge_env.get("OUTCOME"), judge_env.get("SKIP")) != (
        outcome,
        "${{ steps.settings.outputs.skip }}",
    ):
        found.append("no step judges the release step that continues on error")
    if job.get("permissions") != {} or job.get("environment") != "merge-train":
        found.append("GITHUB_TOKEN has rights, or the key's environment is missing")
    if "inputs.switch == 'on'" not in job.get("if", ""):
        found.append("runs while the train is switched off")
    if job.get("strategy", {}).get("matrix", {}).get("repository") != (
        "${{ fromJSON(needs.train.outputs.releases) }}"
    ):
        found.append("matrix is not the sweep's list")
    return found


def release_bound_findings(doc: dict) -> list[str]:
    """What lets more release jobs run at once than the bound allows (HISS-06)."""
    found = []
    strategy = doc["jobs"].get("release", {}).get("strategy", {})
    if strategy.get("max-parallel") != "${{ inputs.release_max_parallel }}":
        found.append("strategy max-parallel is not '${{ inputs.release_max_parallel }}'")
    inputs = doc.get("on", {}).get("workflow_call", {}).get("inputs", {})
    bound = inputs.get("release_max_parallel") or {}
    default = bound.get("default")
    if bound.get("type") != "number" or type(default) is not int or default not in (1, 2, 3):
        found.append(
            "workflow_call input release_max_parallel is missing, "
            "not type number, or default not int 1..3"
        )
    return found


def test_release_job_runs_release_please_with_the_app_token():
    assert release_job_findings(ciwf.load(TRAIN)) == []
    assert release_bound_findings(ciwf.load(TRAIN)) == []


@pytest.mark.parametrize(
    ("step_index", "change", "finding"),
    [
        (
            0,
            lambda w: w.update(token="${{ github.token }}"),
            "release step token is '${{ github.token }}', not the app's",
        ),
        (0, lambda w: w.pop("token"), "release step token is None, not the app's"),
        (
            1,
            lambda w: w.update(token="${{ github.token }}"),
            "pull-request step token is '${{ github.token }}', not the app's",
        ),
        (1, lambda w: w.pop("token"), "pull-request step token is None, not the app's"),
    ],
)
def test_github_token_for_release_please_is_found(step_index, change, finding):
    """GITHUB_TOKEN opens release pull requests that start no checks (cordanaLLM/imago#58)."""
    doc = ciwf.load(TRAIN)
    steps = [
        s for s in doc["jobs"]["release"]["steps"] if s.get("uses", "").startswith(RELEASE_PLEASE)
    ]
    change(steps[step_index]["with"])
    assert release_job_findings(doc) == [finding]


@pytest.mark.parametrize(
    ("step_index", "finding"),
    [
        (0, "release step runs on another repository"),
        (1, "pull-request step runs on another repository"),
    ],
)
def test_release_please_repo_url_is_found(step_index, finding):
    doc = ciwf.load(TRAIN)
    steps = [
        s for s in doc["jobs"]["release"]["steps"] if s.get("uses", "").startswith(RELEASE_PLEASE)
    ]
    steps[step_index]["with"]["repo-url"] = "other/repo"
    assert release_job_findings(doc) == [finding]


@pytest.mark.parametrize(
    ("mutator", "finding"),
    [
        (
            lambda steps: steps[0]["with"].pop("skip-github-pull-request"),
            "release step does not skip pull requests",
        ),
        (
            lambda steps: steps[0].pop("continue-on-error"),
            "release step does not continue on error",
        ),
        (
            lambda steps: steps[0].pop("if"),
            "release step does not check skip setting",
        ),
        (
            lambda steps: steps[1]["with"].pop("skip-github-release"),
            "pull-request step does not skip releases",
        ),
    ],
)
def test_release_step_pieces_are_found(mutator, finding):
    doc = ciwf.load(TRAIN)
    steps = [
        s for s in doc["jobs"]["release"]["steps"] if s.get("uses", "").startswith(RELEASE_PLEASE)
    ]
    mutator(steps)
    assert release_job_findings(doc) == [finding]


@pytest.mark.parametrize(
    ("mutator", "finding"),
    [
        (
            lambda doc: doc["jobs"]["release"]["strategy"].pop("max-parallel"),
            "strategy max-parallel is not '${{ inputs.release_max_parallel }}'",
        ),
        (
            lambda doc: doc["jobs"]["release"]["strategy"].update({"max-parallel": 4}),
            "strategy max-parallel is not '${{ inputs.release_max_parallel }}'",
        ),
        (
            lambda doc: doc["on"]["workflow_call"]["inputs"]["release_max_parallel"].update(
                {"default": 4}
            ),
            (
                "workflow_call input release_max_parallel is missing, "
                "not type number, or default not int 1..3"
            ),
        ),
    ],
)
def test_release_max_parallel_mutants_are_found(mutator, finding):
    doc = ciwf.load(TRAIN)
    mutator(doc)
    assert release_bound_findings(doc) == [finding]


def test_release_job_without_the_judge_is_found():
    """continue-on-error would turn every failed release green without the judge step."""
    doc = ciwf.load(TRAIN)
    steps = doc["jobs"]["release"]["steps"]
    judge = [s for s in steps if str(s.get("run", "")).endswith('release_labels.py" after')]
    assert len(judge) == 1
    judge[0]["env"]["OUTCOME"] = "success"
    assert release_job_findings(doc) == ["no step judges the release step that continues on error"]
    steps.remove(judge[0])
    assert release_job_findings(doc) == ["no step judges the release step that continues on error"]


def test_release_job_mutants_are_found():
    """Release job findings flag unconstrained tokens, excess rights, and bypassed switches."""
    doc = ciwf.load(TRAIN)
    job = doc["jobs"]["release"]
    mint = [s for s in job["steps"] if s.get("uses", "").startswith(APP_TOKEN)][0]["with"]
    mint.pop("repositories")
    mint["permission-issues"] = "write"
    job["if"] = job["if"].replace("inputs.switch == 'on'", "true")
    assert release_job_findings(doc) == [
        "token not limited to the repository",
        (
            "token rights {'permission-contents': 'write', 'permission-pull-requests': 'write', "
            "'permission-workflows': 'write', 'permission-issues': 'write'}"
        ),
        "runs while the train is switched off",
    ]


def test_release_token_rights_are_within_the_app():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["default_permissions"]
    for key, value in TOKEN_RIGHTS.items():
        assert manifest[key.removeprefix("permission-").replace("-", "_")] == value


def _write_manifest_files(zf, files, corrupt_sha, extra_members=None):
    if isinstance(files, dict):
        manifest_files = []
        for path, data in files.items():
            digest = "0" * 64 if corrupt_sha else hashlib.sha256(data).hexdigest()
            manifest_files.append({"path": path, "sha256": digest, "bytes": len(data)})
            zf.writestr(f"files/{path}", data)
        return manifest_files
    extra = extra_members or {}
    written = set()
    for entry in files:
        if not (isinstance(entry, dict) and isinstance(entry.get("path"), str)):
            continue
        p = entry["path"]
        if p in written or f"files/{p}" in extra:
            continue
        written.add(p)
        zf.writestr(f"files/{p}", b"x" * entry.get("bytes", 8))
    return files


def make_artifact_zip(
    head: str,
    files: dict[str, bytes] | list,
    schema: str = "horistes.generated-files.v1",
    manifest_head: str | None = None,
    extra_members: dict[str, bytes] | None = None,
    corrupt_sha: bool = False,
    duplicate_member: str | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        manifest_files = _write_manifest_files(zf, files, corrupt_sha, extra_members)
        manifest = {
            "schema": schema,
            "head": manifest_head or head,
            "files": manifest_files,
            "commands": [["cargo", "build"]],
        }
        zf.writestr("manifest.json", json.dumps(manifest))
        if extra_members:
            for member_name, member_data in extra_members.items():
                zf.writestr(member_name, member_data)
        if duplicate_member:
            with pytest.warns(UserWarning, match="Duplicate name"):
                zf.writestr(duplicate_member, b"duplicate-content")
    return buf.getvalue()


def make_oversize_member_zip(
    head: str, path: str = "notices.txt", declared_size: int = 17 * 1024 * 1024
) -> bytes:
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "schema": "horistes.generated-files.v1",
                    "head": head,
                    "files": [
                        {
                            "path": path,
                            "sha256": hashlib.sha256(b"short").hexdigest(),
                            "bytes": 5,
                        }
                    ],
                    "commands": [],
                }
            ),
        )
        zf.writestr(f"files/{path}", b"short")
    data = bytearray(buf.getvalue())
    target_name = f"files/{path}".encode()
    # Central directory headers; the archive is a few hundred bytes, read once.
    for header in re.finditer(re.escape(b"PK\x01\x02"), bytes(data)):
        pos = header.start()
        fname_len = struct.unpack("<H", data[pos + 28 : pos + 30])[0]
        fname = data[pos + 46 : pos + 46 + fname_len]
        if fname == target_name:
            struct.pack_into("<I", data, pos + 24, declared_size)
    return bytes(data)


def test_dependency_pull_with_valid_artifact_commits_generated_files(tmp_path):
    """A candidate dependency pull request with a valid artifact commits generated files."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt", "wasm/module.wasm"])
    zip_bytes = make_artifact_zip(
        head, {"notices.txt": b"licenses", "wasm/module.wasm": b"\x00asm\x01\x00\x00\x00"}
    )
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
        workflow_run_id=456,
        html_url="https://github.com/fleet/app/actions/runs/456",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(forge.writes) == 1
    kind, branch, exp_head, paths, headline, body = forge.writes[0]
    assert kind == "commit"
    assert branch == "renovate/demo-dep"
    assert exp_head == head
    assert paths == ["notices.txt", "wasm/module.wasm"]
    assert headline == "chore(deps): regenerate generated files"
    assert (
        "Regenerated by the dependency update's own workflow run "
        "https://github.com/fleet/app/actions/runs/456" in body
    )
    assert body.endswith(
        "Signed-off-by: merge-train-app[bot] <123456+merge-train-app[bot]@users.noreply.github.com>"
    )
    assert not any(w[0] == "merge" for w in forge.writes)
    assert "### fleet/app: committed generated files to #1" in summary
    assert "| #1 | BLOCKED | generated files committed |" in summary
    assert "| fleet | 1 | 0 | 1 | 0 |" in summary


def test_negative_no_allowlist_file(tmp_path):
    """Regeneration is refused when no allowlist file exists on the default branch."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)


def test_negative_path_not_in_allowlist(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["allowed.txt"])
    zip_bytes = make_artifact_zip(head, {"allowed.txt": b"ok", "secret.env": b"TOKEN=1"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "path 'secret.env' not allowed" in summary


def test_negative_manifest_head_mismatch(tmp_path):
    """Regeneration is refused when the artifact manifest head does not match the pull request."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    bad_head = f"{2:040x}"
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"}, manifest_head=bad_head)
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "manifest head" in summary


def test_negative_artifact_from_another_head_branch(tmp_path):
    """Artifacts produced for a different head branch are refused."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/other-dep",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)


def test_negative_sha256_mismatch(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"}, corrupt_sha=True)
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "sha256 mismatch" in summary


def test_negative_extra_zip_member(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(
        head, {"notices.txt": b"licenses"}, extra_members={"extra.txt": b"surprise"}
    )
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "unexpected members" in summary


def test_negative_path_with_parent_traversal(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["../evil.txt"])
    zip_bytes = make_artifact_zip(head, {"../evil.txt": b"surprise"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "not allowed" in summary


def test_negative_artifact_size_above_bound(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    large_zip = b"x" * (16 * 1024 * 1024 + 1)
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        large_zip,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "response exceeds" in summary


def cut_artifact_forge():
    """A dependency pull whose artifact body the storage cuts in half under the full length."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    forge.storage_cut[101] = len(zip_bytes) // 2
    return forge


def test_cut_artifact_body_fails_by_name(tmp_path):
    forge = cut_artifact_forge()
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "body cut:" in summary


def test_without_the_length_check_the_cut_is_not_named(tmp_path):
    """The planted mutant: without the length check the cut surfaces only as a bad zip."""
    check = "if length.isdigit() and len(content) != int(length):"
    mutant = script().replace(check, "if False:")
    assert mutant != script()
    forge = cut_artifact_forge()
    done, summary = train(tmp_path, forge, mutant)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "body cut" not in summary
    assert "bad zip archive" in summary


def test_negative_switch_off(tmp_path):
    """When switch is off, no candidate is regenerated or landed."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge, switch="off")
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)


def test_negative_non_dependency_branch(tmp_path):
    """Regeneration ignores pull requests whose branch does not start with the dependency prefix."""
    head = f"{1:040x}"
    pull_req = pull(1, "BEHIND", headRefName="feat/demo-feature")
    forge = Forge(repository(pull_req))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="feat/demo-feature",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)


def test_negative_held_label(tmp_path):
    """A pull request with a hold label is skipped by regeneration."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep", labels=["hold"])
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)


def test_negative_content_already_equal(tmp_path):
    """Regeneration is skipped when the regenerated file contents already match the branch."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    content = b"already current content"
    forge.set_head_content("fleet/app", "notices.txt", head, content)
    zip_bytes = make_artifact_zip(head, {"notices.txt": content})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "| #1 | BLOCKED | merge state BLOCKED |" in summary


def test_without_the_allowlist_check_the_unallowed_path_would_be_committed(tmp_path):
    """The planted case: without the allowlist check, unallowed paths are committed."""
    mutant = script().replace("or p not in allowlist", "or False")
    assert mutant != script()
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["allowed.txt"])
    zip_bytes = make_artifact_zip(head, {"planted.txt": b"unallowed content"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 0
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert forge.writes[0][3] == ["planted.txt"]


def test_rights_token_carries_actions_read_and_storage_url_never_receives_authorization(tmp_path):
    """Artifact downloads pass authorization only to GitHub API, not storage URLs."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"content"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    repo_token_mint = [m for m in forge.minted if m[1].get("repositories") == ["app"]]
    assert len(repo_token_mint) == 1
    assert repo_token_mint[0][1]["permissions"]["actions"] == "read"
    assert len(forge.storage_auth_seen) == 0


def test_bot_identity_lookup_carries_an_installation_token(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.users_auth_seen == ["Bearer list-token-7"]


def test_negative_unauthenticated_bot_identity_lookup_is_rate_limited(tmp_path):
    mutant = script().replace(
        'call(f"{env[\'GITHUB_API_URL\']}/users/{login}", token, "GET")',
        'call(f"{env[\'GITHUB_API_URL\']}/users/{login}", None, "GET")',
    )
    assert mutant != script()
    forge = Forge(repository(pull(1, "CLEAN")))
    done, summary = train(tmp_path, forge, mutant)
    assert forge.users_auth_seen == [""]
    assert "API rate limit exceeded" in summary
    assert not forge.writes


@pytest.mark.parametrize(
    ("redirect", "expected_msg"),
    [
        pytest.param(
            "ftp://storage.example.com/evil.zip",
            "storage URL scheme 'ftp' != 'http'",
            id="scheme_mismatch",
        ),
        pytest.param("", "expected 302 redirect to storage URL", id="location_empty"),
    ],
)
def test_negative_artifact_storage_location(tmp_path, redirect, expected_msg):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"content"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    forge.artifact_redirect_locations[101] = redirect
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert expected_msg in summary


def test_negative_zip_oversize_declared_file_size(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_oversize_member_zip(head, "notices.txt", 17 * 1024 * 1024)
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "exceeds 16777216 bytes" in summary


def test_negative_zip_duplicate_member_names(tmp_path):
    """Regeneration refuses an artifact zip containing duplicate member filenames."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(
        head, {"notices.txt": b"content"}, duplicate_member="files/notices.txt"
    )
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "duplicate zip member names" in summary


def test_positive_zip_directory_entry_accepted(tmp_path):
    """Artifact zip archives containing directory entries are accepted."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"content"}, extra_members={"files/": b""})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert forge.writes[0][3] == ["notices.txt"]
    assert "generated files committed" in summary


def test_negative_zip_directory_entry_nonzero_size_refused(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(
        head, {"notices.txt": b"content"}, extra_members={"files/": b"has-size"}
    )
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "directory entry 'files/' has size > 0" in summary


def test_negative_manifest_non_dict_file_entry(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, ["not_a_dict"])
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "manifest files entry is not a dict" in summary


def test_negative_manifest_duplicate_path(tmp_path):
    """Regeneration refuses an artifact manifest with duplicate file paths."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    digest = hashlib.sha256(b"content").hexdigest()
    entries = [
        {"path": "notices.txt", "bytes": 7, "sha256": digest},
        {"path": "notices.txt", "bytes": 7, "sha256": digest},
    ]
    zip_bytes = make_artifact_zip(head, entries, extra_members={"files/notices.txt": b"content"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "duplicate path 'notices.txt' in manifest files" in summary


def test_first_candidate_bad_second_good_commits_second(tmp_path):
    """A refused artifact on the first candidate falls through to regenerate the second."""
    head1 = f"{1:040x}"
    head2 = f"{2:040x}"
    p1 = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    p2 = pull(2, "BLOCKED", headRefName="renovate/demo-2", headRefOid=head2)
    forge = Forge(repository(p1, p2))
    forge.set_allowlist("fleet/app", "main", ["allowed.txt"])
    zip1 = make_artifact_zip(head1, {"forbidden.txt": b"bad"})
    zip2 = make_artifact_zip(head2, {"allowed.txt": b"good"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.add_artifact(
        "fleet/app",
        102,
        f"regenerated-{head2}",
        zip2,
        head_sha=head2,
        head_branch="renovate/demo-2",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert forge.writes[0][1] == "renovate/demo-2"
    assert (
        "| #1 | BLOCKED | generated files refused: fleet/app #1: path 'forbidden.txt' not allowed |"
        in summary
    )
    assert "| #2 | BLOCKED | generated files committed |" in summary
    assert "### fleet/app: committed generated files to #2" in summary


def test_refused_artifact_falls_through_to_land_clean_pull(tmp_path):
    """A refused artifact on a candidate falls through to land an eligible clean pull request."""
    head1 = f"{1:040x}"
    p1 = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    p2 = pull(2, "CLEAN", headRefName="feat/other-change")
    forge = Forge(repository(p1, p2))
    forge.set_allowlist("fleet/app", "main", ["allowed.txt"])
    zip1 = make_artifact_zip(head1, {"forbidden.txt": b"bad"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert forge.writes == [landed(2)]
    assert (
        "| #1 | BLOCKED | generated files refused: fleet/app #1: path 'forbidden.txt' not allowed |"
        in summary
    )
    assert "| #2 | CLEAN | landed |" in summary
    assert "### fleet/app: landed #2" in summary


def test_commit_mutation_refusal_fails_repository_run(tmp_path):
    """A GraphQL error on createCommitOnBranch fails the train run for the repository."""
    head = f"{1:040x}"
    dep_pull = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep_pull))
    forge.refuse_commit = True
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="renovate/demo-dep",
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1
    assert "createCommitOnBranch refused" in done.stdout


def test_regenerate_excludes_person_branch(tmp_path):
    """A release or person branch is excluded from regeneration candidate selection."""
    head = f"{1:040x}"
    rel_pull = pull(1, "BLOCKED", headRefName="release-please--branches--main")
    forge = Forge(repository(rel_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip_bytes = make_artifact_zip(head, {"notices.txt": b"licenses"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head}",
        zip_bytes,
        head_sha=head,
        head_branch="release-please--branches--main",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "a person lands it" in summary


PLANTED_COMMITS = [
    ("feat: first step\n\nFirst body.\n\nSigned-off-by: Dev <dev@example.org>", 1),
    (
        (
            "Merge branch 'main' into feat/change-1\n\n"
            "Signed-off-by: plurano-merge-train[bot] "
            "<1+plurano-merge-train[bot]@users.noreply.github.com>"
        ),
        2,
    ),
    (
        (
            "fix: follow up\n\nSecond body.\n\n"
            "Co-authored-by: plurano-merge-train[bot] "
            "<1+plurano-merge-train[bot]@users.noreply.github.com>\n"
            "Signed-off-by: Dev <dev@example.org>"
        ),
        1,
    ),
]
PLANTED_BODY = (
    "* feat: first step\n\nFirst body.\n\n"
    "* fix: follow up\n\nSecond body.\n\n"
    "Signed-off-by: Dev <dev@example.org>"
)


@pytest.mark.parametrize(("state", "kind"), [("CLEAN", "merge"), ("BEHIND", "arm")])
def test_planted_squash_message_drops_bot_merge_and_co_author(tmp_path, state, kind):
    forge = Forge(repository(pull(1, state)))
    forge.commits[1] = list(PLANTED_COMMITS)
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.messages == [(kind, "PR_1", "change 1 (#1)", PLANTED_BODY)]
    body = forge.messages[0][3]
    assert body is not None
    assert "co-authored-by" not in body.lower()
    assert body.count("Signed-off-by:") == 1


def test_single_commit_squash_message_uses_body_and_distinct_sign_offs(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.messages == [
        ("merge", "PR_1", "change 1 (#1)", "body 1\n\nSigned-off-by: Dev <dev@example.org>")
    ]


def test_multiple_distinct_sign_offs_kept_in_first_seen_order(tmp_path):
    """Multiple distinct Signed-off-by trailers are preserved in first-seen order."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commits[1] = [
        ("feat: a\n\nSigned-off-by: First <first@example.org>", 1),
        (
            (
                "feat: b\n\n"
                "Signed-off-by: Second <second@example.org>\n"
                "Signed-off-by: First <first@example.org>"
            ),
            1,
        ),
    ]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.messages == [
        (
            "merge",
            "PR_1",
            "change 1 (#1)",
            (
                "* feat: a\n\n* feat: b\n\n"
                "Signed-off-by: First <first@example.org>\n"
                "Signed-off-by: Second <second@example.org>"
            ),
        )
    ]


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_pull_without_sign_off_is_refused_before_any_write(tmp_path, state):
    forge = Forge(repository(pull(1, state)))
    forge.commits[1] = [("feat: unsigned\n\nBody without trailer.", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1
    assert forge.writes == []
    assert forge.messages == []
    assert "fleet/app: #1: no Signed-off-by on its commits" in summary


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_unsigned_dependency_update_is_skipped_and_the_run_stays_green(tmp_path, state):
    forge = Forge(repository(pull(1, state, headRefName="renovate/configure")))
    forge.commits[1] = [("chore: configure renovate", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        f"| #1 | {state} | skipped: dependency update without Signed-off-by, a person lands it |"
        in summary
    )


def test_after_an_unsigned_dependency_update_the_next_clean_pull_lands(tmp_path):
    forge = Forge(
        repository(
            pull(1, "CLEAN", headRefName="renovate/configure"),
            pull(2, "CLEAN"),
        )
    )
    forge.commits[1] = [("chore: configure renovate", 1)]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_unsigned_bot_pull_request_is_skipped_and_listed(tmp_path, state):
    """An unsigned bot PR is skipped on non-dependency branches and listed in summary."""
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(repository(pull(1, state, author=bot, headRefName="fix/x")))
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        f"| #1 | {state} | skipped: bot pull request without Signed-off-by, its producer signs it |"
        in summary
    )
    assert "Bot pull requests without Signed-off-by, their producers sign them:" in summary
    assert "- fleet/app #1" in summary.splitlines()


def test_unsigned_person_pull_request_on_non_dependency_head_is_refused(tmp_path):
    """An unsigned PR authored by a person on a non-dependency head fails the run."""
    user = {"login": "someone", "__typename": "User"}
    forge = Forge(repository(pull(1, "CLEAN", author=user, headRefName="fix/x")))
    forge.commits[1] = [("fix: manual commit without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "no Signed-off-by on its commits" in summary


def test_unsigned_bot_on_dependency_head_yields_dependency_skip_note(tmp_path):
    """On dependency branches, the dependency skip note takes precedence for bot authors."""
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(repository(pull(1, "CLEAN", author=bot, headRefName="renovate/configure")))
    forge.commits[1] = [("chore: configure renovate", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | skipped: dependency update without Signed-off-by, a person lands it |"
        in summary
    )
    assert "Bot pull requests without Signed-off-by, their producers sign them:" not in summary


def test_unsigned_author_without_typename_is_refused(tmp_path):
    """An author without __typename on a non-dependency head is treated as a person and refused."""
    author = {"login": "someone"}
    forge = Forge(repository(pull(1, "CLEAN", author=author, headRefName="fix/x")))
    forge.commits[1] = [("fix: missing typename commit", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "no Signed-off-by on its commits" in summary


def test_after_an_unsigned_bot_pull_request_the_next_clean_pull_lands(tmp_path):
    """A clean pull request lands even when preceded by an unsigned bot pull request."""
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(
        repository(
            pull(1, "CLEAN", author=bot, headRefName="fix/x"),
            pull(2, "CLEAN"),
        )
    )
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]


def test_unsigned_bot_listed_in_summary_when_later_pull_is_refused(tmp_path):
    """An unsigned bot skipped before a later refusal is retained in the sweep summary."""
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(
        repository(
            pull(1, "CLEAN", author=bot, headRefName="fix/x"),
            pull(2, "CLEAN"),
        )
    )
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    forge.refuse_merge = "refused here"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "Refused:" in summary
    assert "Bot pull requests without Signed-off-by, their producers sign them:" in summary
    assert "- fleet/app #1" in summary.splitlines()


def test_planted_sweep_without_unsigned_bots_extend_drops_skipped_bot(tmp_path):
    """Planted mutant: without unsigned_bots.extend on RefusedError, skipped bots are lost."""
    target = "                unsigned_bots.extend(err.unsigned_bots)"
    assert script().count(target) == 1
    flow = script().replace(target, "                pass")
    assert flow != script()
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(
        repository(
            pull(1, "CLEAN", author=bot, headRefName="fix/x"),
            pull(2, "CLEAN"),
        )
    )
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    forge.refuse_merge = "refused here"
    done, summary = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "- fleet/app #1" not in summary.splitlines()


def test_without_the_bot_pr_skip_the_unsigned_bot_fails_the_run(tmp_path):
    """Planted mutant: disabling the bot check causes unsigned bot PRs to fail the run."""
    flow = script().replace(
        "if is_bot(pull):",
        "if False:",
    )
    assert flow != script(), "the bot skip condition is where the test expects it"
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(repository(pull(1, "CLEAN", author=bot, headRefName="fix/x")))
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no Signed-off-by on its commits" in summary + done.stdout


def test_no_summary_list_block_when_no_bot_pull_request_is_skipped(tmp_path):
    """When no bot pull request without sign-off is skipped, the summary list block is absent."""
    forge = Forge(repository(pull(1, "CLEAN")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Bot pull requests without Signed-off-by, their producers sign them:" not in summary


def test_without_the_bot_skip_the_unsigned_update_fails_the_run(tmp_path):
    """The planted case: without the skip, an unsigned bot update fails the run."""
    flow = script().replace(
        'if pull["headRefName"].startswith(conf["dependency"]):',
        "if False:",
    )
    assert flow != script(), "the dependency skip condition is where the test expects it"
    forge = Forge(repository(pull(1, "CLEAN", headRefName="renovate/configure")))
    forge.commits[1] = [("chore: configure renovate", 1)]
    done, summary = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "no Signed-off-by on its commits" in summary + done.stdout


def test_queue_unsigned_user_pull_request_is_refused(tmp_path):
    user = {"login": "someone", "__typename": "User"}
    forge = Forge(repository(pull(1, "CLEAN", author=user, headRefName="fix/x"), queue=True))
    forge.commits[1] = [("fix: manual commit without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "no Signed-off-by on its commits" in summary


def test_queue_unsigned_bot_pull_request_is_skipped_and_listed(tmp_path):
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(repository(pull(1, "CLEAN", author=bot, headRefName="fix/x"), queue=True))
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | skipped: bot pull request without Signed-off-by, its producer signs it |"
        in summary
    )
    assert "Bot pull requests without Signed-off-by, their producers sign them:" in summary
    assert "- fleet/app #1" in summary.splitlines()


def test_queue_signed_pull_request_is_queued(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [queued(1)]


def test_queue_unsigned_dependency_head_is_skipped(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN", headRefName="renovate/x"), queue=True))
    forge.commits[1] = [("chore: configure renovate", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | skipped: dependency update without Signed-off-by, a person lands it |"
        in summary
    )


def test_queue_unsigned_bot_followed_by_signed_pull_request_queues_second(tmp_path):
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(
        repository(
            pull(1, "CLEAN", author=bot, headRefName="fix/x"),
            pull(2, "CLEAN"),
            queue=True,
        )
    )
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [queued(2)]
    assert (
        "| #1 | CLEAN | skipped: bot pull request without Signed-off-by, its producer signs it |"
        in summary
    )


def test_merge_method_merge_unsigned_user_is_refused(tmp_path):
    user = {"login": "someone", "__typename": "User"}
    forge = Forge(repository(pull(1, "CLEAN", author=user, headRefName="fix/x")))
    forge.commits[1] = [("fix: manual commit without trailer", 1)]
    done, summary = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "no Signed-off-by on its commits" in summary


def test_merge_method_merge_unsigned_bot_is_skipped(tmp_path):
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    forge = Forge(repository(pull(1, "CLEAN", author=bot, headRefName="fix/x")))
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | skipped: bot pull request without Signed-off-by, its producer signs it |"
        in summary
    )


def test_queue_co_authored_by_commits_refused(tmp_path):
    mutant = module().replace('elif not low.startswith("co-authored-by:"):', "else:")
    assert mutant != module()
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    forge.commits[1] = list(PLANTED_COMMITS)
    done, summary = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "fleet/app: #1: squash message holds a Co-authored-by line" in summary


def test_pull_with_more_than_100_commits_is_refused(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commit_counts[1] = 101
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1
    assert forge.writes == []
    assert forge.messages == []
    assert "fleet/app: #1: more than 100 commits" in summary


@pytest.mark.parametrize(("state", "kind"), [("CLEAN", "merge"), ("BEHIND", "arm")])
def test_merge_method_merge_sends_no_headline_or_body(tmp_path, state, kind):
    forge = Forge(repository(pull(1, state)))
    forge.commits[1] = [("feat: signed\n\nSigned-off-by: Dev <dev@example.org>", 1)]
    done, _ = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.messages == [(kind, "PR_1", None, None)]


def test_without_commit_body_in_merge_mutation_the_planted_test_fails(tmp_path):
    mutant = module().replace(", commitBody: $body", "")
    assert mutant != module()
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commits[1] = list(PLANTED_COMMITS)
    done, _ = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.messages == [("merge", "PR_1", "change 1 (#1)", None)]
    assert forge.messages[0][3] != PLANTED_BODY


def test_without_trailer_stripping_the_guard_refuses_co_authored_by(tmp_path):
    mutant = module().replace('elif not low.startswith("co-authored-by:"):', "else:")
    assert mutant != module()
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commits[1] = list(PLANTED_COMMITS)
    done, summary = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 1
    assert forge.writes == []
    assert "fleet/app: #1: squash message holds a Co-authored-by line" in summary


def test_co_authored_by_named_in_prose_still_lands(tmp_path):
    prose = "fix: drop the trailer\n\nThe forge added a Co-authored-by: line for the bot.\n\n"
    forge = Forge(repository(pull(1, "CLEAN", title="fix: no Co-authored-by: trailer")))
    forge.commits[1] = [(prose + "Signed-off-by: Dev <dev@example.org>", 1)]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.messages[0][3] == (
        "The forge added a Co-authored-by: line for the bot.\n\n"
        "Signed-off-by: Dev <dev@example.org>"
    )


def closed(issue: int, pull: int) -> list[tuple]:
    oid = f"{pull:x}" * 40
    text = f"Closed by #{pull}, which the merge train merged as {oid}."
    return [("comment", issue, text), ("close", issue, "closed", "completed")]


def test_issue_a_train_merge_names_is_closed(tmp_path):
    """Planted: an app merge whose body says Closes #3 left #3 open; the sweep closes it."""
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    done, written = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == closed(3, 5)
    assert (7, {"permissions": ISSUES, "repositories": ["app"]}) in forge.minted
    assert len(forge.revoked) == len(forge.minted)
    assert "Closed issue #3: #5 merged as 5555555." in written


def test_without_the_close_step_the_issue_stays_open(tmp_path):
    """Mutation: the sweep without the close step leaves the planted issue open."""
    call = "closed = close_issues(local, conf, installation, target, now)"
    text = script()
    assert text.count(call) == 1
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    done, _ = train(tmp_path, forge, text.replace(call, "closed = []"))
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []


def test_landing_and_closing_in_one_run(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"), (4, "OPEN"))]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1), *closed(3, 5), *closed(4, 5)]


@pytest.mark.parametrize(
    ("merged", "why"),
    [
        (merged_pull(5, (3, "OPEN"), by="lusoris"), "a person merged it"),
        (merged_pull(5, (3, "OPEN"), by=""), "no merger recorded"),
        (merged_pull(5, (3, "CLOSED")), "already closed"),
        (merged_pull(5, (3, "OPEN"), issue_repo="fleet/other"), "another repository"),
        (merged_pull(5, (3, "OPEN"), at=OLD), "merged before the window"),
        (merged_pull(5, (3, "OPEN", [NOW.isoformat()])), "reopened after the merge"),
    ],
)
def test_issue_left_open(tmp_path, merged, why):
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, why + done.stdout + done.stderr
    assert forge.writes == [], why
    assert all(body["permissions"] != ISSUES for _, body in forge.minted), why


def test_reopened_before_the_merge_is_closed(tmp_path):
    reopened = (NOW - timedelta(days=1)).isoformat()
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN", [reopened]))]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == closed(3, 5)


def test_without_issues_write_the_open_issue_is_reported(tmp_path):
    """No silent skip: an installation without issues write refuses, naming the issue."""
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.issue_right = False
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    done, written = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "issues #3 stay open, no issues write" in written
    assert len(forge.revoked) == len(forge.minted) - 1


def test_switched_off_closes_nothing(tmp_path):
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    done, _ = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []


def load_rules(tmp_path: Path, text: str | None = None):
    """The train rules module, written from the step text and imported from a file."""
    path = tmp_path / "train_rules_under_test.py"
    path.write_text(text or module(), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("train_rules_under_test", path)
    rules = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rules)
    return rules


def suite(
    number: int, conclusion: str | None, minute: int, workflow: int | None = 1, app="github-actions"
):
    """One check suite node; workflow None stands for a suite of another app."""
    return {
        "databaseId": number,
        "conclusion": conclusion,
        "createdAt": f"2026-10-18T01:{minute:02d}:00Z",
        "app": {"slug": app},
        "workflowRun": {"databaseId": number, "workflow": {"databaseId": workflow}}
        if workflow
        else None,
    }


# cordanaLLM/nucleus pull 207 (bfe6923): pr-project-gate cancelled three times by its
# concurrency group, then green; another workflow green; a Renovate suite with no run.
NUCLEUS_207 = [
    suite(105911160001, "CANCELLED", 10, 355134910),
    suite(105911161630, "CANCELLED", 10, 355134910),
    suite(105911346572, "CANCELLED", 11, 355134910),
    suite(105917123100, "SUCCESS", 47, 355134910),
    suite(105911160081, "SUCCESS", 10, 355116106),
    suite(105911149923, None, 10, None, "renovate"),
]
OLD_RED = 'return any(suite["conclusion"] in RED for suite in suites)'
NEW_RED = 'return any(suite["conclusion"] in RED for suite in newest_suites(suites).values())'


def test_superseded_cancelled_runs_leave_the_pull_request_behind(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND", suites=NUCLEUS_207)))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [armed(1), updated(1)]
    assert "| #1 | BEHIND | armed, updated from the default branch |" in summary


def test_with_every_suite_judged_the_superseded_runs_hold_it_red(tmp_path):
    mutant = module().replace(NEW_RED, OLD_RED)
    assert mutant != module()
    forge = Forge(repository(pull(1, "BEHIND", suites=NUCLEUS_207)))
    done, summary = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BEHIND | red |" in summary


@pytest.mark.parametrize(
    ("suites", "red"),
    [
        (NUCLEUS_207, False),
        ([suite(1, "SUCCESS", 0), suite(2, "CANCELLED", 5)], True),
        ([suite(1, "SUCCESS", 0), suite(2, "FAILURE", 5)], True),
        ([suite(1, "TIMED_OUT", 0), suite(2, "SUCCESS", 5), suite(3, None, 9)], False),
        ([suite(1, "SUCCESS", 0, 1), suite(2, "FAILURE", 5, 2)], True),
        ([suite(100, "FAILURE", 0), suite(200, "SUCCESS", 0)], False),
        ([suite(100, "SUCCESS", 0), suite(200, "FAILURE", 0)], True),
        ([suite(1, "FAILURE", 0, None, "scanner")], True),
        ([suite(1, "FAILURE", 0, None, "scanner"), suite(2, "SUCCESS", 5, None, "scanner")], False),
        ([suite(1, "FAILURE", 0, None, "scanner"), suite(2, "SUCCESS", 5, None, "other")], True),
        ([], False),
    ],
    ids=[
        "nucleus-207",
        "newest-cancelled",
        "newest-failure",
        "newest-in-progress",
        "two-workflows",
        "tie-higher-id-green",
        "tie-higher-id-red",
        "app-failure",
        "app-superseded",
        "apps-apart",
        "no-suites",
    ],
)
def test_each_workflow_is_judged_by_its_newest_run(tmp_path, suites, red):
    assert load_rules(tmp_path).is_red(pull(1, "BEHIND", suites=suites)) is red


def test_a_head_without_a_commit_node_is_not_red(tmp_path):
    assert load_rules(tmp_path).is_red(pull(1, "BEHIND", latest={"nodes": []})) is False


DRAFT_STEP = "Stop on a draft pull request"
REFRESH = "merge-train: refresh"
# cordanaLLM/nucleus pull 205: the ready run (201) cancelled by the synchronize run
# with the draft payload (202), which failed at the draft step; another workflow green.
STOPPED = [
    suite(201, "CANCELLED", 20, 7),
    suite(202, "FAILURE", 21, 7),
    suite(203, "SUCCESS", 20, 8),
]
REFRESH_CALL = (
    "outcome = refresh(\n"
    "        served, notes, lambda run: run_jobs(env, run),"
    " lambda pull: toggle_refresh(env, pull)\n"
    "    )"
)
GUARD = "if refreshed_since(pull, max(created for _, created in runs)):"


def steps(*failed: str) -> list[dict]:
    """The jobs of one run whose steps named in `failed` failed."""
    done = [{"name": "Set up job", "conclusion": "success"}]
    return [
        {
            "id": 1,
            "name": "Validate",
            "steps": done + [{"name": n, "conclusion": "failure"} for n in failed],
        }
    ]


def refreshed(number: int) -> list[tuple]:
    return [("label", number, REFRESH), ("unlabel", number, REFRESH)]


def unlabelled(minute: int, second: int = 0) -> dict:
    """The refreshes timeline with one removal of the refresh label."""
    when = f"2026-10-18T01:{minute:02d}:{second:02d}Z"
    return {"nodes": [{"createdAt": when, "label": {"name": REFRESH}}]}


def stopped_forge(state: str = "BLOCKED", suites=STOPPED, jobs=(DRAFT_STEP,), **fields) -> Forge:
    forge = Forge(repository(pull(1, state, suites=suites, **fields)))
    forge.jobs[202] = steps(*jobs)
    return forge


@pytest.mark.parametrize("state", ["BLOCKED", "BEHIND", "UNSTABLE"])
def test_a_pull_request_stopped_at_the_draft_step_is_refreshed(tmp_path, state):
    done, summary = train(tmp_path, stopped_forge(state))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "### fleet/app: refreshed #1" in summary
    assert f"| #1 | {state} | refreshed: its red runs stopped at the draft step |" in summary


def test_the_refresh_adds_the_label_then_removes_it_and_reads_only_the_red_run(tmp_path):
    forge = stopped_forge()
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == refreshed(1)
    assert forge.jobs_calls == [202]


def test_the_refreshed_pull_request_lands_on_the_next_run(tmp_path):
    forge = Forge(repository(pull(1, "BLOCKED", suites=STOPPED)), repository(pull(1, "CLEAN")))
    forge.jobs[202] = steps(DRAFT_STEP)
    first, _ = train(tmp_path, forge)
    forge.revoked.clear()  # the stand-in names its tokens the same on every run
    second, summary = train(tmp_path, forge)
    assert first.returncode == second.returncode == 0, second.stdout + second.stderr
    assert forge.writes == [*refreshed(1), landed(1)]
    assert "### fleet/app: landed #1" in summary


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"jobs": ("Run the tests",)}, "another step failed"),
        ({"jobs": (DRAFT_STEP, "Run the tests")}, "the draft step and another failed"),
        ({"jobs": ()}, "no failed step"),
        (
            {"suites": [*STOPPED, suite(204, "FAILURE", 21, None, "other")]},
            "a red suite of another app",
        ),
        (
            {"suites": [*STOPPED, suite(205, "SUCCESS", 22, 7)]},
            "a newer run of the workflow is green",
        ),
        ({"isDraft": True}, "a draft"),
        ({"labels": ("hold",)}, "a held label"),
        ({"state": "DIRTY"}, "a conflict: a person pushes anyway"),
        ({"refreshes": unlabelled(21)}, "refreshed when the run was created"),
        ({"refreshes": unlabelled(30)}, "refreshed after the run, which started no run"),
    ],
)
def test_a_pull_request_the_refresh_cannot_help_is_left_alone(tmp_path, fields, why):
    forge = stopped_forge(**fields)
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, why + done.stdout + done.stderr
    assert forge.writes == [], why


def test_a_refresh_that_started_no_run_is_left_to_a_person(tmp_path):
    done, summary = train(tmp_path, stopped_forge(refreshes=unlabelled(30)))
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "| #1 | BLOCKED | stopped at the draft step, a refresh started no run: a person pushes |"
        in summary
    )


def test_a_refresh_before_the_failed_run_does_not_hold_the_next(tmp_path):
    forge = stopped_forge(refreshes=unlabelled(20, 59))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == refreshed(1)


def test_a_clean_pull_request_lands_before_any_refresh(tmp_path):
    forge = Forge(repository(pull(1, "BLOCKED", suites=STOPPED), pull(2, "CLEAN")))
    forge.jobs[202] = steps(DRAFT_STEP)
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert forge.jobs_calls == []


def test_switched_off_the_train_refreshes_nothing(tmp_path):
    forge = stopped_forge()
    done, _ = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (forge.writes, forge.jobs_calls) == ([], [])


def test_at_most_three_red_pull_requests_are_read(tmp_path):
    pulls = [pull(n, "BLOCKED", suites=[suite(n, "FAILURE", 21, 7)]) for n in range(1, 6)]
    forge = Forge(repository(*pulls))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (forge.writes, forge.jobs_calls) == ([], [1, 2, 3])


def test_a_label_already_gone_still_counts_as_refreshed(tmp_path):
    forge = stopped_forge()
    forge.label_missing = True
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == refreshed(1)[:1]
    assert "### fleet/app: refreshed #1" in summary


@pytest.mark.parametrize(
    ("where", "original", "mutant", "fields"),
    [
        ("script", REFRESH_CALL, "outcome = None", {}),
        ("module", GUARD, "if False:", {"refreshes": unlabelled(30)}),
        (
            "module",
            "return failed == {DRAFT_STEP}",
            "return DRAFT_STEP in failed",
            {"jobs": (DRAFT_STEP, "x")},
        ),
        (
            "module",
            '("red", "merge state BLOCKED",',
            '("merge state BLOCKED",',
            {"state": "BEHIND"},
        ),
    ],
)
def test_each_refresh_rule_is_needed(tmp_path, where, original, mutant, fields):
    """Each mutant flips the outcome of one case: the rule it removes decides that case."""
    text = script() if where == "script" else module()
    assert original in text
    changed = text.replace(original, mutant)
    kept = stopped_forge(**fields)
    mutated = stopped_forge(**fields)
    first, _ = train(tmp_path, kept)
    if where == "script":
        second, _ = train(tmp_path, mutated, changed)
    else:
        second, _ = train(tmp_path, mutated, helper=changed)
    assert first.returncode == second.returncode == 0, second.stdout + second.stderr
    assert bool(kept.writes) != bool(mutated.writes), (kept.writes, mutated.writes)


def test_check_release_off_parses_and_lowercases(tmp_path):
    rules = load_rules(tmp_path)
    assert rules.check_release_off("Other/D cordanaLLM/Aegis-OS") == frozenset(
        {"other/d", "cordanallm/aegis-os"}
    )
    assert rules.check_release_off("") == frozenset()


@pytest.mark.parametrize(
    "invalid",
    ["justname", "a/b/c", "/name", "owner/", "/"],
)
def test_check_release_off_refuses_invalid_entry(tmp_path, invalid):
    rules = load_rules(tmp_path)
    with pytest.raises(ValueError, match="not owner/name"):
        rules.check_release_off(invalid)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job: job["steps"][-1].pop("if"),
        lambda job: [s.get("env", {}).pop("HOLD_LABELS", None) for s in job["steps"]],
    ],
)
def test_release_job_without_the_frozen_check_is_refused(mutate):
    doc = ciwf.load(TRAIN)
    mutate(doc["jobs"]["release"])
    assert release_job_findings(doc) != []


FROZEN_RELEASE = [
    {
        "headRefName": "release-please--branches--master--components--app",
        "labels": ("autorelease: cut",),
    },
    {"headRefName": "release-please--branches--main", "labels": ("autorelease: pending",)},
    {"headRefName": "feat/change-1", "labels": ("autorelease: pending",)},
]


@pytest.mark.parametrize("fields", FROZEN_RELEASE)
def test_planted_release_pull_request_behind_main_is_not_updated(tmp_path, fields):
    """The planted case: a labelled release pull request behind main, person_branches emptied."""
    forge = Forge(repository(pull(1, "BEHIND", **fields)))
    done, summary = train(tmp_path, forge, person_branches="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "a person lands it" in summary


@pytest.mark.parametrize("fields", FROZEN_RELEASE)
def test_without_the_release_check_the_sweep_updates_it(tmp_path, fields):
    mutant = module().replace("release = release or any(", "release = False and any(")
    mutant = mutant.replace(
        'release = pull["headRefName"].startswith(RELEASE_HEAD)', "release = False"
    )
    assert mutant != module()
    forge = Forge(repository(pull(1, "BEHIND", **fields)))
    done, _ = train(tmp_path, forge, helper=mutant, person_branches="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert updated(1) in forge.writes


def head_at(node: dict, when: str) -> dict:
    """The pull request with its head commit committed at `when`."""
    node["latest"]["nodes"][0]["commit"]["committedDate"] = when
    return node


def regenerable(forge: Forge, *nodes: dict) -> None:
    """A changed generated file in an artifact of each pull request's head."""
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    for node in nodes:
        head = node["headRefOid"]
        forge.add_artifact(
            "fleet/app",
            100 + node["number"],
            f"regenerated-{head}",
            make_artifact_zip(head, {"notices.txt": b"licenses"}),
            head_sha=head,
            head_branch=node["headRefName"],
            workflow_run_id=400 + node["number"],
            html_url=f"https://github.com/fleet/app/actions/runs/{400 + node['number']}",
        )


def test_security_update_gets_the_generated_files_write_before_an_older_digest(tmp_path):
    """A security update takes precedence for generated files write over an older digest."""
    # Golusoris, 2026-10-28: a digest update re-pushed on every main push took every write.
    digest = head_at(pull(1, "BLOCKED", headRefName="renovate/self-digest"), YOUNG)
    fix = head_at(
        pull(2, "BLOCKED", headRefName="renovate/x-net", labels=("security",), createdAt=YOUNG),
        OLD,
    )
    forge = Forge(repository(digest, fix))
    regenerable(forge, digest, fix)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert [w[1] for w in forge.writes if w[0] == "commit"] == ["renovate/x-net"]
    assert "### fleet/app: committed generated files to #2" in summary


def test_security_title_counts_like_the_label_for_the_generated_files_write(tmp_path):
    """A [SECURITY] title prefix prioritizes generated files write like the label."""
    digest = pull(1, "BLOCKED", headRefName="renovate/self-digest")
    fix = pull(2, "BLOCKED", headRefName="renovate/x-net", title="fix(deps): x/net [SECURITY]")
    forge = Forge(repository(digest, fix))
    regenerable(forge, digest, fix)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "### fleet/app: committed generated files to #2" in summary


def test_generated_files_write_serves_the_longest_waiting_head_first(tmp_path):
    """Candidates are regenerated in longest-waiting order based on head commit timestamp."""
    # The older update's head moved last: the newer one has waited longer and goes first.
    moved = head_at(pull(1, "BLOCKED", headRefName="renovate/self-digest"), YOUNG)
    waiting = head_at(pull(2, "BLOCKED", headRefName="renovate/other-dep"), OLD)
    forge = Forge(repository(moved, waiting))
    regenerable(forge, moved, waiting)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "### fleet/app: committed generated files to #2" in summary


def test_security_pull_request_lands_before_an_older_clean_one(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "CLEAN", labels=("security",))))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes[0] == landed(2), forge.writes


def test_lowercase_security_title_lands_young_before_an_older_clean_one(tmp_path):
    # The golusoris x/net update: Renovate lowercases the marker, no label.
    older = pull(1, "CLEAN")
    fix = pull(
        2,
        "CLEAN",
        headRefName="renovate/x-net",
        createdAt=YOUNG,
        title="fix(deps): x/net [security]",
    )
    forge = Forge(repository(older, fix))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes[0] == landed(2), forge.writes


def test_security_pull_request_is_updated_before_an_older_armed_one(tmp_path):
    older = pull(1, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    fix = pull(2, "BEHIND", labels=("security",))
    forge = Forge(repository(older, fix))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert armed(2) in forge.writes and updated(2) in forge.writes, forge.writes
    assert updated(1) not in forge.writes, forge.writes


HOLD = "merge-train: security hold"
LINEAR = ("REQUIRED_SIGNATURES", "REQUIRED_LINEAR_HISTORY")


def held_pair() -> tuple[dict, dict]:
    """A dependency update armed and CLEAN, and a security fix one commit behind it."""
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps", autoMergeRequest={"enabledAt": OLD})
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", title="fix(deps): x/net [security]")
    return update, fix


def test_dependency_update_is_held_as_a_draft_while_a_security_update_is_open(tmp_path):
    """Neither auto-merge nor the update bot merges a draft (decision record 0062)."""
    forge = Forge(repository(*held_pair(), rules=LINEAR))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1")], forge.writes
    assert "held as a draft: security update #2 is open" in summary, summary


def test_planted_hold_that_holds_nothing_lands_the_update_ahead_of_the_fix(tmp_path):
    helper = module().replace("return fixes[0], hold, []", "return fixes[0], [], []")
    forge = Forge(repository(*held_pair(), rules=LINEAR))
    done, _ = train(tmp_path, forge, helper=helper)
    assert done.returncode == 0, done.stdout + done.stderr
    assert landed(1) in forge.writes, forge.writes
    assert ("draft", "PR_1") not in forge.writes, forge.writes


def test_held_update_is_released_when_no_security_update_is_open(tmp_path):
    held = pull(1, "CLEAN", headRefName="renovate/go-snaps", isDraft=True, labels=(HOLD,))
    forge = Forge(repository(held))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("ready", "PR_1"), ("unlabel", 1, HOLD)], forge.writes
    assert "released from the security hold" in summary, summary


def test_held_update_a_person_marked_ready_is_not_held_again(tmp_path):
    ready = pull(1, "BLOCKED", headRefName="renovate/go-snaps", labels=(HOLD,))
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(ready, fix, rules=LINEAR))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [w for w in forge.writes if w[0] in ("label", "draft", "unlabel")], forge.writes


def test_release_of_an_update_already_ready_only_removes_the_label(tmp_path):
    ready = pull(1, "BLOCKED", headRefName="renovate/go-snaps", labels=(HOLD,))
    forge = Forge(repository(ready))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("unlabel", 1, HOLD)], forge.writes


@pytest.mark.parametrize(
    "other",
    [
        {"headRefName": "feat/change-1"},
        {"headRefName": "renovate/go-snaps", "isDraft": True},
        {"headRefName": "renovate/go-snaps", "isCrossRepository": True},
        {"headRefName": "renovate/go-snaps", "baseRefName": "next"},
        {"headRefName": "renovate/y-crypto", "labels": ("security",)},
    ],
    ids=["person", "draft", "fork", "other-base", "security"],
)
def test_security_hold_leaves_other_pull_requests_alone(tmp_path, other):
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(pull(1, "BLOCKED", **other), fix, rules=LINEAR))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [w for w in forge.writes if w[0] in ("label", "draft")], forge.writes


@pytest.mark.parametrize(
    "fix",
    [
        {"isDraft": True, "labels": ("security",)},
        {"labels": ("security", "hold")},
    ],
    ids=["draft-fix", "held-fix"],
)
def test_a_security_update_the_train_does_not_serve_holds_nothing(tmp_path, fix):
    update = pull(1, "BLOCKED", headRefName="renovate/go-snaps")
    forge = Forge(repository(update, pull(2, "BEHIND", headRefName="renovate/x-net", **fix)))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [w for w in forge.writes if w[0] in ("label", "draft")], forge.writes


def armed_squash(headline: str = "old title (#1)", method: str = "SQUASH") -> dict:
    return {"enabledAt": OLD, "commitHeadline": headline, "mergeMethod": method}


def test_retitled_armed_pull_request_is_re_armed_with_new_headline(tmp_path):
    """An armed pull request retitled after arming is disarmed and armed again (ADR-0064)."""
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1), armed(1)], forge.writes
    assert forge.messages == [
        (
            "arm",
            "PR_1",
            "chore(deps): update tool to v2 (#1)",
            "body 1\n\nSigned-off-by: Dev <dev@example.org>",
        )
    ]
    assert "### fleet/app: re-armed #1" in summary, summary
    assert "| #1 | BLOCKED | re-armed: title changed after arming |" in summary, summary


def test_unsigned_bot_retitled_armed_pull_request_is_disarmed_and_skipped(tmp_path):
    """An armed bot pull request retitled without sign-off is disarmed and skipped."""
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    retitled = pull(
        1,
        "BLOCKED",
        author=bot,
        headRefName="fix/x",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    forge.commits[1] = [("chore: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)], forge.writes
    assert (
        "| #1 | BLOCKED | skipped: bot pull request without Signed-off-by, its producer signs it |"
        in summary
    )
    assert "### fleet/app: disarmed #1: its commits carry no Signed-off-by" in summary
    assert "- fleet/app #1" in summary.splitlines()


def test_unsigned_person_retitled_armed_pull_request_is_disarmed_and_refused(tmp_path):
    """An armed person's pull request retitled without sign-off is disarmed and refused."""
    user = {"login": "someone", "__typename": "User"}
    retitled = pull(
        1,
        "BLOCKED",
        author=user,
        headRefName="fix/x",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    forge.commits[1] = [("chore: manual update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)], forge.writes
    assert "no Signed-off-by on its commits" in summary


def test_planted_re_arm_drifted_without_unsigned_bot_error_refuses_bot(tmp_path):
    """Planted mutant: unhandled UnsignedBotError in re_arm_drifted refuses the run."""
    target = (
        "            try:\n"
        "                arm(env, conf, pull)\n"
        "            except UnsignedBotError as err:\n"
        '                notes[pull["number"]] = err.note\n'
        "                return f\"disarmed #{pull['number']}: its commits carry no Signed-off-by\""
    )
    mutant = (
        "            try:\n"
        "                arm(env, conf, pull)\n"
        "            except KeyError as err:\n"
        '                notes[pull["number"]] = err.note\n'
        "                return f\"disarmed #{pull['number']}: its commits carry no Signed-off-by\""
    )
    assert script().count(target) == 1
    flow = script().replace(target, mutant)
    assert flow != script()
    bot = {"login": "praetor-ops", "__typename": "Bot"}
    retitled = pull(
        1,
        "BLOCKED",
        author=bot,
        headRefName="fix/x",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    forge.commits[1] = [("chore: automated update without trailer", 1)]
    done, _ = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr


def test_planted_drifted_that_never_fires_leaves_stale_headline_armed(tmp_path):
    helper = module().replace(
        "return armed.get(\"commitHeadline\") != f\"{pull['title']} (#{pull['number']})\"",
        "return False",
    )
    assert helper != module()
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    done, _ = train(tmp_path, forge, helper=helper)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [], forge.writes
    assert forge.messages == [], forge.messages


def test_armed_pull_request_with_matching_headline_is_not_re_armed(tmp_path):
    matching = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v2 (#1)"),
    )
    forge = Forge(repository(matching))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [], forge.writes
    assert forge.messages == [], forge.messages
    assert "#1 is armed and in flight" in summary, summary


def test_merge_method_merge_does_not_re_arm_retitled_pull_request(tmp_path):
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    done, _ = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [], forge.writes
    assert forge.messages == [], forge.messages


def test_switched_off_does_not_re_arm_retitled_pull_request(tmp_path):
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    done, summary = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [], forge.writes
    assert forge.messages == [], forge.messages
    assert "switched off" in summary, summary


def test_retitled_armed_pull_request_held_for_security_is_not_re_armed(tmp_path):
    """A retitled update held as a draft for security is not disarmed and re-armed (ADR-0064)."""
    update = pull(
        1,
        "CLEAN",
        headRefName="renovate/go-snaps",
        title="chore(deps): update go-snaps to v2",
        autoMergeRequest=armed_squash("chore(deps): update go-snaps to v1 (#1)"),
    )
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", title="fix(deps): x/net [security]")
    forge = Forge(repository(update, fix, rules=LINEAR))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert ("label", 1, HOLD) in forge.writes and ("draft", "PR_1") in forge.writes, forge.writes
    assert disarmed(1) not in forge.writes and armed(1) not in forge.writes, forge.writes
    assert "held as a draft: security update #2 is open" in summary, summary


@pytest.mark.parametrize(
    ("conf_method", "auto_merge", "expected"),
    [
        ("SQUASH", armed_squash("old (#1)", "SQUASH"), True),
        ("SQUASH", armed_squash("change 1 (#1)", "SQUASH"), False),
        ("MERGE", armed_squash("old (#1)", "SQUASH"), False),
        ("SQUASH", armed_squash("old (#1)", "MERGE"), False),
        ("SQUASH", None, False),
    ],
    ids=["drifted", "matching", "conf-merge", "armed-merge", "unarmed"],
)
def test_drifted_rule(tmp_path, conf_method, auto_merge, expected):
    rules = load_rules(tmp_path)
    node = pull(1, "BLOCKED", title="change 1", autoMergeRequest=auto_merge)
    assert rules.drifted(node, {"method": conf_method}) is expected


def test_query_includes_lander_lease():
    assert 'landerLease: label(name: "merge-train: lander lease") { description }' in module()


def test_planted_overtake_yields_to_live_lander_lease(tmp_path):
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    forge = Forge(repository(pull(1, "CLEAN"), lease=f"until={future}"))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "the lander holds the landing order until" in summary
    assert f"the lander holds the landing order until {future}" in summary


def test_without_the_lease_call_the_overtake_lands(tmp_path):
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    call_text = "lease = lease_until(repo, now)"
    mutant = script().replace(call_text, "lease = None")
    assert mutant != script()
    forge = Forge(repository(pull(1, "CLEAN"), lease=f"until={future}"))
    done, _ = train(tmp_path, forge, text=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_expired_lease_lands(tmp_path):
    past = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    forge = Forge(repository(pull(1, "CLEAN"), lease=f"until={past}"))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_absent_lease_label_lands(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


@pytest.mark.parametrize(
    "bad_desc",
    [
        "",
        "until=tomorrow",
        "until=2026-10-30T23:00:00Z extra",
        "hold",
        "until=2026-13-45T00:00:00Z",
        None,
    ],
)
def test_bad_lease_description_refuses_repo_and_serves_next(tmp_path, bad_desc):
    forge = Forge(repository(pull(1, "CLEAN"), lease=bad_desc))
    forge.install(9, "other", b=[repository(pull(2, "CLEAN"))])
    done, summary = train(tmp_path, forge)
    assert done.returncode != 0
    assert forge.writes == []
    assert forge.where == [("other/b", landed(2))]
    assert "fleet/app" in done.stdout
    assert "### other/b: landed #2" in summary


def test_security_update_lands_under_live_lease(tmp_path):
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    plain = pull(1, "CLEAN")
    sec = pull(2, "CLEAN", title="fix(deps): update [SECURITY]")
    forge = Forge(repository(plain, sec, lease=f"until={future}"))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert f"| #1 | CLEAN | the lander holds the landing order until {future} |" in summary
    assert "### fleet/app: landed #2" in summary


def test_behind_pull_on_non_linear_repo_under_live_lease_is_not_armed_or_updated(tmp_path):
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    behind_pull = pull(1, "BEHIND")
    forge = Forge(repository(behind_pull, rules=(), lease=f"until={future}"))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | BEHIND | the lander holds the landing order until {future} |" in summary
    assert f"### fleet/app: the lander holds the landing order until {future}" in summary


def test_bad_lease_description_with_security_hold_writes_nothing(tmp_path):
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps", autoMergeRequest={"enabledAt": OLD})
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", title="fix(deps): x/net [security]")
    forge = Forge(repository(update, fix, rules=LINEAR, lease="hold"))
    done, _ = train(tmp_path, forge)
    assert done.returncode != 0
    assert forge.writes == []


def test_security_hold_under_live_lease_still_drafts_dependency_updates(tmp_path):
    """A live lander lease with an open security update drafts other dependency updates."""
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps", autoMergeRequest={"enabledAt": OLD})
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", title="fix(deps): x/net [security]")
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    forge = Forge(repository(update, fix, rules=LINEAR, lease=f"until={future}"))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1")]
    assert "held as a draft: security update #2 is open" in summary


def test_switched_off_with_live_lease_stays_switched_off(tmp_path):
    """When switch is off, an active lander lease does not trigger landing or updates."""
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    forge = Forge(repository(pull(1, "CLEAN"), lease=f"until={future}"))
    done, summary = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "switched off: nothing landed or updated" in summary


def test_lease_until_boundary_equal_stamp_returns_none(tmp_path):
    """A lander lease expiring at exactly the boundary time returns None."""
    rules = load_rules(tmp_path)
    now = datetime(2026, 10, 30, 23, 0, 0, tzinfo=UTC)
    stamp = "2026-10-30T23:00:00Z"
    repo = {"landerLease": {"description": f"until={stamp}"}}
    assert rules.lease_until(repo, now) is None
    before = datetime(2026, 10, 30, 22, 59, 59, tzinfo=UTC)
    assert rules.lease_until(repo, before) == stamp
    after = datetime(2026, 10, 30, 23, 0, 1, tzinfo=UTC)
    assert rules.lease_until(repo, after) is None


def test_a_behind_head_is_not_regenerated(tmp_path):
    """A behind pull request is never regenerated because Renovate rebases it next."""
    head1 = f"{1:040x}"
    head2 = f"{2:040x}"
    behind_pull = pull(1, "BEHIND", headRefName="renovate/demo-1", headRefOid=head1)
    up_to_date_pull = pull(2, "BLOCKED", headRefName="renovate/demo-2", headRefOid=head2)
    forge = Forge(repository(behind_pull, up_to_date_pull))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    zip2 = make_artifact_zip(head2, {"notices.txt": b"two"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.add_artifact(
        "fleet/app",
        102,
        f"regenerated-{head2}",
        zip2,
        head_sha=head2,
        head_branch="renovate/demo-2",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert forge.writes[0][1] == "renovate/demo-2"
    assert "| #1 | BEHIND | generated files" not in summary
    assert "| #2 | BLOCKED | generated files committed |" in summary
    assert "### fleet/app: committed generated files to #2" in summary


def test_a_fresh_train_write_holds_other_landings(tmp_path):
    """A fresh train regenerate commit holds other pull requests from landing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = (datetime.fromisoformat(recent) + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #2 | CLEAN | held for the generated files of #1 until {until} |" in summary


def test_the_written_head_itself_lands_under_the_hold(tmp_path):
    """The written pull request itself remains eligible to land while holding others."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "CLEAN",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary
    assert "held for the generated files of #1 until" in summary


def test_a_security_update_lands_first_while_a_train_write_is_open(tmp_path):
    """A security update bypasses an active train write hold and lands immediately."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    sec = pull(2, "CLEAN", headRefName="renovate/patch", title="fix(deps): security fix [SECURITY]")
    other = pull(3, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, sec, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1"), landed(2)]
    assert "### fleet/app: landed #2" in summary
    assert landed(2) in forge.writes
    assert landed(3) not in forge.writes
    assert "held for the generated files" not in summary


def test_the_write_hold_ends_after_its_timeout(tmp_path):
    """A train write hold expires after WRITE_HOLD_MINUTES elapse."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    past = (datetime.now(UTC) - timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=past,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    exact_60 = (now - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    boundary_pull = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=exact_60,
    )
    conf = {"bot_id": "123456", "bot_login": f"{BOT}[bot]"}
    assert rules.write_hold([(boundary_pull, "merge state BLOCKED")], conf, now) is None
    just_before = now - timedelta(minutes=59, seconds=59)
    assert rules.write_hold([(boundary_pull, "merge state BLOCKED")], conf, just_before) is not None


def test_a_red_written_head_releases_the_hold(tmp_path):
    """A red check on a BLOCKED written head releases the hold immediately."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    red_written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        suites=["FAILURE"],
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(red_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


@pytest.mark.parametrize("state", ["UNSTABLE", "UNKNOWN"])
def test_a_train_write_with_a_red_check_still_holds(tmp_path, state):
    """A train write in a non-terminal state with a red check stays held."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        state,
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        suites=["FAILURE"],
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert landed(2) not in forge.writes
    assert "held for the generated files of #1 until" in summary


@pytest.mark.parametrize(
    "author_email",
    [
        pytest.param("bot@renovateapp.com", id="rebased_head"),
        pytest.param("attacker@example.com", id="other_author"),
    ],
)
def test_non_bot_author_with_regenerate_headline_does_not_hold(tmp_path, author_email):
    """A regenerate headline from a non-bot author does not hold the train."""
    rules = load_rules(tmp_path)
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pr = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author={"email": author_email},
        committedDate=recent,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(pr, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_the_query_reads_the_head_commit_headline_and_author(tmp_path):
    """The GraphQL pull query requests the head commit headline, author, and signature."""
    rules = load_rules(tmp_path)
    assert "messageHeadline" in rules.QUERY
    assert "author { email user { login } }" in rules.QUERY
    assert "signature { wasSignedByGitHub }" in rules.QUERY


def test_a_young_train_written_head_holds_nothing(tmp_path):
    """A train write on a pull request younger than min_age_days holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    young_written = pull(
        1,
        "CLEAN",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        createdAt=YOUNG,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(young_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_draft_train_written_head_holds_nothing(tmp_path):
    """A train write on a draft pull request holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    draft_written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        isDraft=True,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(draft_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_labelled_train_written_head_holds_nothing(tmp_path):
    """A train write on a pull request with a hold label holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    labelled_written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        labels=("hold",),
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(labelled_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_cross_repository_train_written_head_holds_nothing(tmp_path):
    """A train write on a cross-repository fork pull request holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cross_written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        isCrossRepository=True,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(cross_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_behind_train_written_head_holds_nothing(tmp_path):
    """A train write on a behind pull request holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    behind_written = pull(
        1,
        "BEHIND",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(behind_written, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_an_unsigned_regenerate_commit_holds_nothing(tmp_path):
    """A regenerate commit without a GitHub signature holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    unsigned = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
        signature=None,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(unsigned, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_regenerate_commit_of_another_login_holds_nothing(tmp_path):
    """A regenerate commit authored by another GitHub login holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    imposter = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        user="someone-else",
        committedDate=recent,
        signature={"wasSignedByGitHub": True},
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(imposter, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_a_future_dated_regenerate_commit_skew_bounds(tmp_path):
    """A regenerate commit dated beyond allowed clock skew holds nothing."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    far_future = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_pull = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=far_future,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(future_pull, other))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary

    near_future = (datetime.now(UTC) + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    skew_pull = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=near_future,
    )
    forge_skew = Forge(repository(skew_pull, other))
    done_skew, summary_skew = train(tmp_path, forge_skew)
    assert done_skew.returncode == 0, done_skew.stdout + done_skew.stderr
    assert forge_skew.writes == []
    assert "held for the generated files of #1 until" in summary_skew


def test_regenerate_waits_when_another_pr_is_armed_blocked(tmp_path):
    """Regeneration waits when another non-behind pull request is armed for auto-merge."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    armed_other = pull(
        2,
        "BLOCKED",
        headRefName="feat/armed-feature",
        autoMergeRequest={
            "enabledAt": NOW.isoformat(),
            "commitHeadline": "feat",
            "mergeMethod": "SQUASH",
        },
    )
    forge = Forge(repository(candidate, armed_other))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(write[0] == "commit" for write in forge.writes)
    assert "| #1 | BLOCKED | regenerate waits: #2 is armed |" in summary


def test_regenerate_proceeds_when_another_pr_is_armed_behind(tmp_path):
    """Regeneration proceeds when an armed pull request is behind default branch."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    armed_behind = pull(
        2,
        "BEHIND",
        headRefName="feat/armed-behind",
        autoMergeRequest={
            "enabledAt": NOW.isoformat(),
            "commitHeadline": "feat",
            "mergeMethod": "SQUASH",
        },
    )
    forge = Forge(repository(candidate, armed_behind))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert "### fleet/app: committed generated files to #1" in summary


def test_regenerate_proceeds_when_another_pr_is_armed_red(tmp_path):
    """Regeneration proceeds when an armed pull request is red and BLOCKED."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    armed_other = pull(
        2,
        "BLOCKED",
        headRefName="feat/armed-feature",
        autoMergeRequest={
            "enabledAt": NOW.isoformat(),
            "commitHeadline": "feat",
            "mergeMethod": "SQUASH",
        },
        suites=["FAILURE"],
    )
    forge = Forge(repository(candidate, armed_other))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert sum(1 for write in forge.writes if write[0] == "commit") == 1
    assert "regenerate waits" not in summary


def test_regenerate_waits_when_another_pr_is_armed_red_but_unstable(tmp_path):
    """Regeneration waits when an armed pull request is red but UNSTABLE."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    armed_other = pull(
        2,
        "UNSTABLE",
        headRefName="feat/armed-feature",
        autoMergeRequest={
            "enabledAt": NOW.isoformat(),
            "commitHeadline": "feat",
            "mergeMethod": "SQUASH",
        },
        suites=["FAILURE"],
    )
    forge = Forge(repository(candidate, armed_other))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(write[0] == "commit" for write in forge.writes)
    assert "| #1 | BLOCKED | regenerate waits: #2 is armed |" in summary


def test_regenerate_proceeds_when_another_pr_is_armed_security(tmp_path):
    """Regeneration proceeds when an armed pull request is a security update."""
    head1 = f"{1:040x}"
    candidate = pull(
        1,
        "BLOCKED",
        headRefName="renovate/patch-1",
        title="fix(deps): security fix 1 [SECURITY]",
        headRefOid=head1,
    )
    armed_sec = pull(
        2,
        "BLOCKED",
        headRefName="renovate/patch-2",
        title="fix(deps): security fix 2 [SECURITY]",
        autoMergeRequest={
            "enabledAt": NOW.isoformat(),
            "commitHeadline": "sec",
            "mergeMethod": "SQUASH",
        },
    )
    forge = Forge(repository(candidate, armed_sec))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/patch-1",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert "### fleet/app: committed generated files to #1" in summary


def test_another_blocked_candidate_gets_no_write_during_hold(tmp_path):
    """Another blocked candidate does not receive a regenerate write during a hold."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/held-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    head2 = f"{2:040x}"
    other_candidate = pull(2, "BLOCKED", headRefName="renovate/other-dep", headRefOid=head2)
    forge = Forge(repository(written, other_candidate))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip2 = make_artifact_zip(head2, {"notices.txt": b"two"})
    forge.add_artifact(
        "fleet/app",
        102,
        f"regenerated-{head2}",
        zip2,
        head_sha=head2,
        head_branch="renovate/other-dep",
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #2 | BLOCKED | merge state BLOCKED |" in summary


def test_newly_behind_between_read_and_write_skips_commit(tmp_path):
    """Regeneration skips committing when the head becomes behind between read and write."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    forge = Forge(repository(candidate))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.merge_states[1] = "BEHIND"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BLOCKED | behind since the read: not regenerated |" in summary


def test_regenerate_skips_when_the_live_merge_state_is_unknown(tmp_path):
    """Regeneration skips when the live merge state re-read returns UNKNOWN."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    forge = Forge(repository(candidate))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.merge_states[1] = "UNKNOWN"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(write[0] == "commit" for write in forge.writes)
    assert "| #1 | BLOCKED | merge state UNKNOWN: not regenerated |" in summary
    assert "### fleet/app: #1 merge state UNKNOWN: regenerate waits" in summary


def test_regenerate_skips_when_the_live_merge_state_is_missing(tmp_path):
    """Regeneration skips when the live merge state re-read returns None."""
    head1 = f"{1:040x}"
    candidate = pull(1, "BLOCKED", headRefName="renovate/demo-1", headRefOid=head1)
    forge = Forge(repository(candidate))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.merge_states[1] = None
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(write[0] == "commit" for write in forge.writes)
    assert "| #1 | BLOCKED | merge state missing: not regenerated |" in summary
    assert "### fleet/app: #1 merge state missing: regenerate waits" in summary


def test_an_unknown_live_state_lands_nothing_in_the_same_sweep(tmp_path):
    """An UNKNOWN live merge state halts the sweep without landing any pull request."""
    head1 = f"{1:040x}"
    candidate = pull(1, "CLEAN", headRefName="renovate/demo-1", headRefOid=head1)
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(candidate, other))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    forge.merge_states[1] = "UNKNOWN"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not any(write[0] in ("commit", "merge", "enqueue") for write in forge.writes)
    assert forge.writes == []
    assert "regenerate waits" in summary
    assert "### fleet/app: #1 merge state UNKNOWN: regenerate waits" in summary


def test_without_the_behind_check_the_behind_head_is_regenerated(tmp_path):
    """The planted case: without the behind check, the behind head commits generated files."""
    check = 'if pull["mergeStateStatus"] == "BEHIND":'
    mutant = script().replace(check, "if False:")
    assert mutant != script()
    head1 = f"{1:040x}"
    behind_pull = pull(1, "BEHIND", headRefName="renovate/demo-1", headRefOid=head1)
    forge = Forge(repository(behind_pull))
    forge.merge_states[1] = "CLEAN"
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    zip1 = make_artifact_zip(head1, {"notices.txt": b"one"})
    forge.add_artifact(
        "fleet/app",
        101,
        f"regenerated-{head1}",
        zip1,
        head_sha=head1,
        head_branch="renovate/demo-1",
    )
    done, summary = train(tmp_path, forge, text=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    commits = [write for write in forge.writes if write[0] == "commit"]
    assert len(commits) == 1
    assert "### fleet/app: committed generated files to #1" in summary


def test_without_the_write_hold_other_pull_request_lands(tmp_path):
    """The planted case: without the write hold, another clean pull request lands."""
    mutant = script().replace("hold = write_hold(served, conf, now)", "hold = None")
    assert mutant != script()
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BLOCKED",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    other = pull(2, "CLEAN", headRefName="feat/clean-feature")
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge, text=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


def test_planted_review_settle_holds_unaddressed_review(tmp_path):
    """A person-authored PR with an unaddressed review does not land."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    repo = repository(p)
    forge = Forge(repo)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "review settle: review at" in summary
    assert "| #1 | CLEAN | review settle:" in summary


def test_planted_review_settle_allows_after_reply(tmp_path):
    """A person-authored PR lands after the author replies to the review."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=5)).isoformat()
    reply_time = (NOW - timedelta(minutes=2)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            },
            {
                "submittedAt": reply_time,
                "state": "COMMENTED",
                "author": {"login": "person"},
                "commit": {"oid": f"{1:040x}"},
            },
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_planted_review_settle_skips_bot_pr(tmp_path):
    """A bot PR (dependency prefix + bot author) lands despite unaddressed reviews."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        headRefName="renovate/something",
        author={"login": "renovate[bot]"},
        createdAt=OLD,
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_review_on_previous_head_does_not_hold(tmp_path):
    """A review on an older commit does not hold the current head."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": "0" * 39 + "2"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_push_after_review_allows_landing(tmp_path):
    """A commit pushed after the review unblocks landing."""
    review_time = (NOW - timedelta(minutes=10)).isoformat()
    head_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_person_on_dependency_branch_is_held(tmp_path):
    """A person-authored PR on a dependency-prefix branch is not exempt."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        headRefName="renovate/something",
        author={"login": "person"},
        createdAt=OLD,
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "review settle" in summary


def test_approved_review_does_not_hold(tmp_path):
    """An APPROVED review on the current head does not hold landing."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "APPROVED",
                "author": {"login": "reviewer"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_pending_review_does_not_crash(tmp_path):
    """A review without submittedAt (PENDING state) does not crash or hold."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": None,
                "state": "PENDING",
                "author": {"login": "reviewer"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_the_query_reads_author_and_reviews(tmp_path):
    """The GraphQL pull query requests the PR author and recent reviews."""
    rules = load_rules(tmp_path)
    assert "author { login __typename }" in rules.QUERY
    assert "author { login }" in rules.QUERY
    assert "reviews(last: 20)" in rules.QUERY


def _detail(
    reviews: list[dict] = (),
    comments: list[dict] = (),
    commits: list[str] = (),
    review_total: int | None = None,
) -> dict:
    """Build a REVIEW_BODIES pullRequest payload for Forge.review_details."""
    rev_list = list(reviews)
    com_list = list(comments)
    msg_list = [{"commit": {"message": m}} for m in commits]
    return {
        "reviews": {
            "totalCount": len(rev_list) if review_total is None else review_total,
            "nodes": rev_list,
        },
        "comments": {"totalCount": len(com_list), "nodes": com_list},
        "commits": {"totalCount": len(msg_list), "nodes": msg_list},
    }


def _bot_review(
    review_id: int,
    login: str,
    body: str,
    state: str = "COMMENTED",
    typename: str = "Bot",
) -> dict:
    """One review node for REVIEW_BODIES."""
    return {
        "databaseId": review_id,
        "state": state,
        "body": body,
        "author": {"login": login, "__typename": typename},
    }


def test_planted_review_settle_skips_live_bot_pr(tmp_path):
    """A live-shaped bot dependency PR (__typename Bot, no [bot] suffix) skips review settle."""
    rules = load_rules(tmp_path)
    no_author = {"headRefName": "renovate/x", "author": None}
    assert not rules.is_bot_dependency(no_author, {"dependency": ("renovate/",)})
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        headRefName="renovate/x",
        author={"login": "renovate", "__typename": "Bot"},
        createdAt=OLD,
        committedDate=head_time,
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbitai"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_planted_unanswered_coderabbit_nitpick_body_holds(tmp_path):
    """An unanswered CodeRabbit nitpick review body on an older commit holds the PR."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[
            _bot_review(
                101,
                "coderabbitai",
                "<!-- coderabbit-ai-generated:v1 -->\n<summary>Nitpick comments: 1</summary>",
            )
        ]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    expected = (
        "review body: coderabbitai review 101 has findings "
        "without a reply naming pullrequestreview-101"
    )
    assert expected in summary
    assert f"| #1 | CLEAN | {expected} |" in summary


def test_planted_multiple_unanswered_review_bodies_report_more_count(tmp_path):
    """Multiple unanswered bot review bodies append (and N more) to the hold reason."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[
            _bot_review(101, "coderabbitai", "**Outside diff range comments (1)**"),
            _bot_review(102, "sourcery-ai", "Hey - I've found 1 issue\n**Security issues**:\n- x"),
        ]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "without a reply naming pullrequestreview-101 (and 1 more)" in summary


def test_planted_generic_author_comment_without_id_still_held(tmp_path):
    """An author comment without the review id or a non-author comment with the id stays held."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[_bot_review(202, "coderabbitai", "Nitpick comments: 1")],
        comments=[
            {"body": "Fixed everything, thanks!", "author": {"login": "person"}},
            {"body": "Addressed pullrequestreview-202", "author": {"login": "bystander"}},
        ],
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "pullrequestreview-202" in summary


def test_author_comment_or_review_naming_id_allows_landing(tmp_path):
    """An author issue comment naming pullrequestreview-<id> or review naming review <id> lands."""
    for comments, extra_reviews in (
        ([{"body": "Addressed in pullrequestreview-303.", "author": {"login": "person"}}], []),
        (
            [],
            [
                {
                    "databaseId": 304,
                    "state": "COMMENTED",
                    "body": "Skipped review 303 as intentional.",
                    "author": {"login": "person", "__typename": "User"},
                }
            ],
        ),
    ):
        p = pull(1, "CLEAN", author={"login": "person"})
        forge = Forge(repository(p))
        forge.review_details[1] = _detail(
            reviews=[
                _bot_review(303, "coderabbitai", "Outside diff range comments (1)"),
                *extra_reviews,
            ],
            comments=comments,
        )
        done, summary = train(tmp_path, forge)
        assert done.returncode == 0, done.stdout + done.stderr
        assert forge.writes == [landed(1)]
        assert "### fleet/app: landed #1" in summary


def test_commit_message_naming_review_id_allows_landing(tmp_path):
    """A commit message on the PR naming pullrequestreview-<id> unblocks landing."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[_bot_review(404, "coderabbitai", "Nitpick comments: 1")],
        commits=["fix: handle terminator (pullrequestreview-404)\n\nSigned-off-by: P <p@e.org>"],
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_clean_coderabbit_codacy_and_sourcery_bodies_land(tmp_path):
    """Clean CodeRabbit (empty or actionable-only), clean Codacy, and clean Sourcery land."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[
            _bot_review(501, "coderabbitai", "<!-- coderabbit-ai-generated:v1 -->"),
            _bot_review(
                502,
                "coderabbitai",
                "**Actionable comments posted: 1**\n- [ ] <!-- id --> Fix comments: Fix on PR",
            ),
            _bot_review(
                503,
                "codacy-production",
                "### Pull Request Overview\n**No blocking issues noticed**\n- [x] Covered",
            ),
            _bot_review(
                504,
                "sourcery-ai",
                "Hey - I've reviewed your changes and they look great!\n**Approved.**",
                state="APPROVED",
            ),
        ]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


@pytest.mark.parametrize(
    ("login", "body"),
    [
        (
            "codacy-production",
            "### Pull Request Overview\n**No blocking issues noticed**\n- [ ] Add test",
        ),
        (
            "codacy-production",
            "### Pull Request Overview\n:warning: **Resolve issues before merging**",
        ),
        (
            "sourcery-ai",
            "Hey - I've reviewed your changes!\n**Needs a human reviewer.**",
        ),
        (
            "sourcery-ai",
            "Hey - I've found 1 issue\n## Overall Comments\n- Check edge case",
        ),
    ],
)
def test_planted_codacy_and_sourcery_findings_hold(tmp_path, login, body):
    """Codacy unchecked boxes/blocking overview and Sourcery human-review/overall comments hold."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(reviews=[_bot_review(601, login, body)])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"review body: {login} review 601 has findings" in summary


@pytest.mark.parametrize(
    ("login", "body"),
    [
        ("other-review-bot", "Found 1 potential bug in handler."),
        ("coderabbitai", "Unexpected summary format without known markers."),
        ("sourcery-ai", "Unrecognized Sourcery review body format."),
    ],
)
def test_planted_unknown_bot_and_unknown_shapes_hold(tmp_path, login, body):
    """An unknown bot non-empty body or unknown CodeRabbit/Sourcery shape fails closed."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(reviews=[_bot_review(701, login, body)])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"review body: {login} review 701 has findings" in summary


@pytest.mark.parametrize(
    ("login", "body", "expected"),
    [
        (
            "sourcery-ai",
            "### Sourcery assessment\n\n**Approved.**",
            False,
        ),
        (
            "coderabbitai",
            (
                "<details><summary>Prompt to fix all issues</summary></details>\n"
                "<details><summary>Review info</summary></details>"
            ),
            False,
        ),
        (
            "coderabbitai",
            (
                "<summary>Prompt to fix all issues</summary>\n"
                "<h6><summary><picture></picture> Review info</summary></h6>"
            ),
            False,
        ),
        (
            "sourcery-ai",
            (
                "Hey - I've found 2 issues\n\n"
                "### Sourcery assessment\n\n"
                "**Approval pending.** 2 findings to address first.\n\n"
                "Blocking findings: `test.py:10`"
            ),
            True,
        ),
        (
            "sourcery-ai",
            (
                "Hey - I've reviewed your changes\n\n"
                "### Linked issues\n"
                "**[#2] Test issue** · 4 of 5 met\n"
                "**[#3] Second issue** · 2 of 2 met"
            ),
            True,
        ),
        (
            "sourcery-ai",
            "New security issues found",
            True,
        ),
        (
            "coderabbitai",
            (
                "Additional comments: 1\n"
                "Verification ran but could not confirm this finding\n"
                "<summary>Review info</summary>"
            ),
            True,
        ),
        (
            "codacy-production",
            (
                "### Pull Request Overview\n\n"
                ":eyes: **Worth a closer look**\n\n"
                "#### Test suggestions\n"
                "- [x] Validate coverage floor\n"
                "- [x] Verify stamps"
            ),
            True,
        ),
        (
            "coderabbitai",
            ("**Actionable comments posted: 1**\n\n- [ ] <!-- autofix --> Fix comments: Fix on PR"),
            False,
        ),
        (
            "sourcery-ai",
            (
                "Hey - I've found 3 issues\n\n"
                "<details><summary>Prompt for AI Agents</summary>\n"
                "## Individual Comments\n\n"
                "### Comment 1\n"
                "</details>\n\n"
                "### Sourcery assessment\n\n"
                "**Approval pending.** 3 findings to address first.\n\n"
                "Blocking findings: `Makefile:129`, `Makefile:128`"
            ),
            True,
        ),
        (
            "sourcery-ai",
            (
                "Hey - I've found 3 issues\n\n"
                "<details><summary>Prompt for AI Agents</summary>\n"
                "## Individual Comments\n\n"
                "### Comment 1\n"
                "</details>"
            ),
            False,
        ),
    ],
    ids=[
        "pr138-5819334919-sourcery-bare-approved",
        "pr139-5818723044-coderabbit-prompt-review-info",
        "pr150-5820146659-coderabbit-prompt-review-info-alt",
        "pr153-5820899825-sourcery-approval-pending",
        "pr150-5820192615-sourcery-linked-issues-partly-unmet",
        "pr139-5819112049-sourcery-new-security-issues",
        "pr140-5819221013-coderabbit-additional-comments-count",
        "pr153-5820891475-codacy-worth-closer-look-all-checked",
        "pr139-5818983092-coderabbit-actionable-comments-autofix-box",
        "pr140-5818710917-sourcery-found-issues-approval-pending",
        "synthetic-sourcery-found-issues-inline-only-no-assessment",
    ],
)
def test_bot_body_has_finding_table(tmp_path, monkeypatch, login, body, expected):
    """Classification over bot review body shapes; ids name the live review each row copies."""
    merge_train = load_train(tmp_path, monkeypatch)
    assert merge_train._bot_body_has_finding(login, body) == expected


def test_planted_sourcery_approval_pending_held_until_answered(tmp_path):
    """Sourcery Approval pending body holds until an author comment names the review id."""
    body = (
        "Hey - I've found 2 issues\n\n"
        "### Sourcery assessment\n\n"
        "**Approval pending.** 2 findings to address first.\n\n"
        "Blocking findings: `tests/test_foo.py:10`"
    )
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(reviews=[_bot_review(601, "sourcery-ai", body)])
    held_dir = tmp_path / "held"
    held_dir.mkdir()
    done, summary = train(held_dir, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    expected_hold = (
        "review body: sourcery-ai review 601 has findings "
        "without a reply naming pullrequestreview-601"
    )
    assert expected_hold in summary

    # Same with author comment naming pullrequestreview-601 -> lands.
    forge_answered = Forge(repository(p))
    forge_answered.review_details[1] = _detail(
        reviews=[_bot_review(601, "sourcery-ai", body)],
        comments=[
            {
                "body": "Addressed in pullrequestreview-601.",
                "author": {"login": "person"},
            }
        ],
    )
    landed_dir = tmp_path / "landed"
    landed_dir.mkdir()
    done, summary = train(landed_dir, forge_answered)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge_answered.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_dismissed_bot_review_and_person_review_body_land(tmp_path):
    """A DISMISSED bot review with a nitpick and a User review body do not hold."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[
            _bot_review(801, "coderabbitai", "Nitpick comments: 1", state="DISMISSED"),
            _bot_review(802, "reviewer", "Please check this", state="COMMENTED", typename="User"),
        ]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_bot_dependency_pr_skips_review_bodies_fetch_and_lands(tmp_path):
    """A live-shaped bot dependency PR lands without fetching REVIEW_BODIES."""
    p = pull(
        1,
        "CLEAN",
        headRefName="renovate/pkg-1.x",
        author={"login": "renovate", "__typename": "Bot"},
        createdAt=OLD,
    )
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[_bot_review(901, "coderabbitai", "Nitpick comments: 1")]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.review_body_calls == []
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_planted_review_bodies_over_100_holds(tmp_path):
    """More than 100 reviews holds the PR because not every answer can be read."""
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(reviews=[], review_total=101)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "review body: more than 100 reviews, cannot read every answer" in summary


def test_planted_armed_pr_held_by_review_body_is_disarmed(tmp_path):
    """An armed PR held by an unanswered bot review body is disarmed with a disarmed note."""
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        autoMergeRequest={"enabledAt": OLD},
    )
    forge = Forge(repository(p))
    forge.review_details[1] = _detail(
        reviews=[_bot_review(1101, "coderabbitai", "Nitpick comments: 1")]
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)]
    expected = (
        "disarmed: review body: coderabbitai review 1101 has findings "
        "without a reply naming pullrequestreview-1101"
    )
    assert expected in summary


def test_row_with_other_hold_reason_skips_review_bodies_fetch(tmp_path):
    """A PR already held by another rule (such as a hold label) does not fetch REVIEW_BODIES."""
    p = pull(1, "CLEAN", author={"login": "person"}, labels=("hold",))
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.review_body_calls == []
    assert "| #1 | CLEAN | labelled hold |" in summary


def test_review_bodies_query_fields(tmp_path, monkeypatch):
    """REVIEW_BODIES queries reviews, comments, and commits with totalCount on each."""
    merge_train = load_train(tmp_path, monkeypatch)
    q = merge_train.REVIEW_BODIES
    for token in (
        "reviews(first: 100)",
        "databaseId",
        "state",
        "body",
        "author { login __typename }",
        "comments(first: 100)",
        "author { login }",
        "commits(first: 100)",
        "commit { message }",
    ):
        assert token in q
    assert q.count("totalCount") == 3


def test_planted_code_scanning_failure_holds_clean_pull(tmp_path):
    """A pull request with a failed code-scanning results check does not land."""
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "code-scanning check failed" in summary
    assert "| #1 | CLEAN | code-scanning check failed |" in summary


def test_code_scanning_success_allows_landing(tmp_path):
    """A pull request with a successful code-scanning check lands normally."""
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "SUCCESS", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_pull_without_code_scanning_lands_unchanged(tmp_path):
    """A pull request without a code-scanning check lands unchanged."""
    p = pull(1, "CLEAN")
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_planted_github_app_code_scanning_failure_holds_clean_pull(tmp_path):
    """A pull request with a failed suite from the github app does not land."""
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "code-scanning check failed" in summary
    assert "| #1 | CLEAN | code-scanning check failed |" in summary


def test_superseded_code_scanning_failure_allows_landing(tmp_path):
    """A newer successful code-scanning suite supersedes an older failed one."""
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
            suite(1002, "SUCCESS", 15, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_verdict_direct_code_scanning_hold(tmp_path):
    """Direct test of verdict and has_red_code_scanning for code scanning hold."""
    rules = load_rules(tmp_path)
    p_fail = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    assert rules.has_red_code_scanning(p_fail) == "code-scanning check failed"
    conf = {
        "hold": frozenset(),
        "person": "",
        "accept": frozenset({"CLEAN"}),
        "dependency": "renovate/",
    }
    repo = {"defaultBranchRef": {"name": "main"}}
    assert rules.verdict(p_fail, repo, conf, NOW) == "code-scanning check failed"

    p_pass = pull(
        2,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "SUCCESS", 10, None, "github-code-scanning"),
        ],
    )
    assert rules.has_red_code_scanning(p_pass) is False
    assert rules.verdict(p_pass, repo, conf, NOW) == "land"


def test_planted_armed_pr_with_red_code_scanning_is_not_merged(tmp_path):
    """An armed PR whose CodeQL results check turns red is not merged (sveltesentio#512)."""
    armed_clean = pull(
        1,
        "CLEAN",
        autoMergeRequest={"enabledAt": OLD},
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(armed_clean))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)]
    assert "disarmed: code-scanning check failed" in summary


def test_behind_pr_not_armed_when_code_scanning_is_red(tmp_path):
    """A BEHIND PR with a failed code-scanning check is not armed or updated."""
    p = pull(
        1,
        "BEHIND",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p, pull(2, "BEHIND")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert armed(1) not in forge.writes
    assert "| #1 | BEHIND | code-scanning check failed |" in summary
    assert armed(2) in forge.writes


def test_retitled_armed_pr_not_re_armed_when_code_scanning_is_red(tmp_path):
    """A retitled armed PR with a failed code-scanning check is not re-armed."""
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(retitled))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert disarmed(1) in forge.writes
    assert armed(1) not in forge.writes
    assert "not re-armed: code-scanning check failed" in summary


def test_without_code_scanning_hold_the_red_scan_pr_would_land(tmp_path):
    """A mutant that disables has_red_code_scanning lets the planted case land."""
    mutant = module().replace(
        "    scanning = has_red_code_scanning(pull)\n    if scanning:\n        return scanning",
        "    pass",
    )
    assert mutant != module()
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p))
    done, _ = train(tmp_path, forge, helper=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_github_advanced_security_app_holds_clean_pull(tmp_path):
    """A failed suite from the github-advanced-security app holds a CLEAN pull."""
    p = pull(
        1,
        "CLEAN",
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-advanced-security"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "code-scanning check failed" in summary


def test_armed_pr_disarmed_when_review_settle_hides_red_scan(tmp_path):
    """An armed PR held by review settle is still disarmed when scanning is red."""
    head_time = (NOW - timedelta(minutes=10)).isoformat()
    review_time = (NOW - timedelta(minutes=3)).isoformat()
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        committedDate=head_time,
        autoMergeRequest={"enabledAt": OLD},
        reviews=[
            {
                "submittedAt": review_time,
                "state": "COMMENTED",
                "author": {"login": "coderabbit[bot]"},
                "commit": {"oid": f"{1:040x}"},
            }
        ],
        suites=[
            "SUCCESS",
            suite(1001, "FAILURE", 10, None, "github-code-scanning"),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert disarmed(1) in forge.writes
    assert "disarmed: code-scanning check failed" in summary


def _finding(review_id: int) -> dict:
    """A REVIEW_BODIES payload with one unanswered CodeRabbit nitpick body."""
    return _detail(reviews=[_bot_review(review_id, "coderabbitai", "Nitpick comments: 1")])


def test_security_update_held_by_review_body_still_holds_the_update(tmp_path):
    """A security fix a review body holds is still the fix the update waits for (record 0062)."""
    forge = Forge(repository(*held_pair(), rules=LINEAR))
    forge.review_details[2] = _finding(1201)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1")], forge.writes
    assert "held as a draft: security update #2 is open" in summary, summary
    assert "| #2 | BEHIND | review body: coderabbitai review 1201 has findings" in summary, summary


def test_retitled_armed_pull_request_held_by_review_body_stays_disarmed(tmp_path):
    """A drifted title does not re-arm a pull request a review body disarmed in this run."""
    retitled = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(retitled))
    forge.review_details[1] = _finding(1301)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)], forge.writes
    assert forge.messages == [], forge.messages
    assert "| #1 | BLOCKED | disarmed: review body: coderabbitai review 1301" in summary, summary


def test_switched_off_reports_a_review_body_hold_with_a_read_token(tmp_path):
    """Switched off, the train mints read rights only: it reports the hold and writes nothing."""
    p = pull(1, "CLEAN", author={"login": "person"}, autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(p))
    forge.review_details[1] = _finding(1401)
    done, summary = train(tmp_path, forge, switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.minted[1] == (7, {"permissions": READ, "repositories": ["app"]})
    assert forge.writes == [], forge.writes
    assert "| #1 | CLEAN | review body: coderabbitai review 1401 has findings" in summary, summary


def test_landing_reads_the_review_bodies_again(tmp_path):
    """A pull request the train lands has its review bodies read twice: verdict and landing."""
    forge = Forge(repository(pull(1, "CLEAN", author={"login": "person"})))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)], forge.writes
    assert forge.review_body_calls == [1, 1], forge.review_body_calls


@pytest.mark.parametrize(
    ("auto_merge", "writes", "note"),
    [
        (None, [], "| #1 | CLEAN | review body: coderabbitai review 1501"),
        ({"enabledAt": OLD}, [disarmed(1)], "| #1 | CLEAN | disarmed: review body: coderabbitai"),
    ],
    ids=["unarmed", "armed"],
)
def test_finding_posted_after_the_first_read_holds_at_landing(tmp_path, auto_merge, writes, note):
    """A bot body posted between the verdict read and the landing holds the pull request."""
    p = pull(1, "CLEAN", author={"login": "person"}, autoMergeRequest=auto_merge)
    forge = Forge(repository(p))
    forge.review_details[1] = [_detail(), _finding(1501)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == writes, forge.writes
    assert forge.review_body_calls == [1, 1], forge.review_body_calls
    assert note in summary, summary


def _token_mixes(tokens: tuple[str, ...], longest: int) -> list[str]:
    """Every string of up to longest tokens, in order."""
    return [
        "".join(mix)
        for size in range(longest + 1)
        for mix in itertools.product(tokens, repeat=size)
    ]


# The expressions the scans replaced; each scan must answer as its expression does.
HTML_COMMENT_ORACLE = re.compile(r"<!--.*?-->", re.S)
REVIEW_INFO_ORACLE = re.compile(
    r"<summary>(?:(?!</summary>).)*?Review info.*?</summary>", re.S | re.I
)


def test_html_comment_strip_matches_its_expression(tmp_path, monkeypatch):
    """Every mix of up to five comment fragments strips as re.sub with <!--.*?--> does."""
    merge_train = load_train(tmp_path, monkeypatch)
    mixes = _token_mixes(("<!--", "-->", "<!-", "-", "a", "\n"), 5)
    wrong = [
        m for m in mixes if merge_train._strip_html_comments(m) != HTML_COMMENT_ORACLE.sub("", m)
    ]
    assert len(mixes) == 9331
    assert wrong == []


def test_review_info_scan_matches_its_expression(tmp_path, monkeypatch):
    """Every mix of up to five summary fragments reads as the Review info expression reads it."""
    merge_train = load_train(tmp_path, monkeypatch)
    tokens = ("<summary>", "</summary>", "Review info", "REVIEW INFO", "<SUMMARY>", "x")
    mixes = _token_mixes(tokens, 5)
    wrong = [
        m for m in mixes if merge_train._has_review_info(m) != bool(REVIEW_INFO_ORACLE.search(m))
    ]
    assert len(mixes) == 9331
    assert wrong == []


@pytest.mark.parametrize(
    ("scan", "body"),
    [
        ("_strip_html_comments", "<!--" * 16384),
        ("_has_review_info", "<summary>" * 7281),
        ("_sourcery_finding", "1" * 65536),
    ],
    ids=["comment-openers", "summary-openers", "met-digits"],
)
def test_bot_body_scans_stay_linear(tmp_path, monkeypatch, scan, body):
    """Up to 64 KiB of unclosed openers or digits: the replaced expressions took 2 to 16 s."""
    merge_train = load_train(tmp_path, monkeypatch)
    start = time.perf_counter()
    getattr(merge_train, scan)(body)
    assert time.perf_counter() - start < 0.25


@pytest.mark.parametrize(
    ("unreadable", "reason"),
    [
        ("refused", "review body: cannot read: GraphQL: "),
        ("gone", "review body: cannot read: the forge returned no pull request"),
        ("oversize", "review body: cannot read: POST "),
    ],
    ids=["refused", "gone", "oversize"],
)
def test_unreadable_review_bodies_hold_that_pull_request_alone(tmp_path, unreadable, reason):
    """A refused, missing or oversize read holds its pull request; the run lands the next one."""
    forge = Forge(
        repository(
            pull(1, "CLEAN", author={"login": "person"}),
            pull(2, "CLEAN", author={"login": "person"}),
        )
    )
    if unreadable == "refused":
        forge.review_refusals[1] = "stand-in refuses the read"
    elif unreadable == "gone":
        forge.review_details[1] = None
    else:
        body = "x" * (16 * 1024 * 1024)  # MAX_ARTIFACT_BYTES of the whole answer
        forge.review_details[1] = _detail(reviews=[_bot_review(1601, "coderabbitai", body)])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)], forge.writes
    assert f"| #1 | CLEAN | {reason}" in summary, summary


def test_review_body_reads_stop_at_the_bound_security_update_first(tmp_path):
    """Ten reads a run, security update first; the pull request past the bound is held unread."""
    people = [pull(n, "CLEAN", author={"login": "person"}) for n in range(1, 11)]
    fix = pull(11, "CLEAN", headRefName="renovate/x-net", title="fix(deps): x/net [security]")
    forge = Forge(repository(*people, fix))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.review_body_calls == [11, *range(1, 10), 11], forge.review_body_calls
    assert forge.writes == [landed(11)], forge.writes
    expected = (
        "| #10 | CLEAN | review body: not read: more than 10 pull requests to read in one run |"
    )
    assert expected in summary, summary
