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

import ast
import base64
import contextlib
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
import types
import urllib.error
import urllib.request
import weakref
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlsplit

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
RELEASE = ROOT / ".github/workflows/merge-train-release.yml"
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
# The forge's answer after another merge left the pull request behind (k8s run 43245308767).
NOT_MERGEABLE = {
    "type": "UNPROCESSABLE",
    "path": ["mergePullRequest"],
    "locations": [{"line": 4, "column": 3}],
    "message": "Pull Request is not mergeable",
}
# A queued REST fault that closes the connection without an answer (RemoteDisconnected).
DROP = "drop"
CLIENT_ID = "Iv23planted"
WRITE = {
    "metadata": "read",
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "checks": "read",
}
READ = {"metadata": "read", "contents": "read", "pull_requests": "read", "checks": "read"}
STATUSES = {"statuses": "read"}
ISSUES = {"issues": "write"}
BOT = "merge-train-app"
APP = {"login": "plurano-merge-train", "__typename": "Bot"}


SCRIPT = ROOT / "tools/merge_train.py"
RUN = 'python3 "$GITHUB_WORKSPACE/merge-train-source/tools/merge_train.py"'
# Starts the genuine tools/merge_train.py, so coverage counts that file, with both waits at 0.
LAUNCH = """\
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
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


KEY_BUILD_SAFE_MODULES = frozenset({"urllib.request", __name__})


def key_build_safe_target(target: object) -> bool:
    """Return whether an attribute patch target is safe for test key builds (issue 260).

    Covers setattr and delattr; item and environment patches are not covered.
    """
    if isinstance(target, types.ModuleType):
        return (
            sys.modules.get(target.__name__) is not target
            or target.__name__ in KEY_BUILD_SAFE_MODULES
        )
    if isinstance(target, str):
        return target.rpartition(".")[0] in KEY_BUILD_SAFE_MODULES
    return False


KEY_BUILD_PATCHES: weakref.WeakSet[KeyBuildGuardPatch] = weakref.WeakSet()


class KeyBuildGuardPatch(pytest.MonkeyPatch):
    """Record shared-target attribute patches for the issue 260 key-build guard.

    Covers setattr and delattr; item and environment patches are not covered.
    """

    def __init__(self) -> None:
        super().__init__()
        self.key_build_hazards: list[str] = []
        KEY_BUILD_PATCHES.add(self)

    def _record_hazard(
        self, target: object, args: tuple[object, ...], kwargs: dict[str, object]
    ) -> None:
        if key_build_safe_target(target):
            return
        if isinstance(target, str):
            self.key_build_hazards.append(target)
            return
        name = args[0] if args else kwargs["name"]
        owner = getattr(target, "__name__", type(target).__name__)
        self.key_build_hazards.append(f"{owner}.{name}")

    def setattr(self, target: object, *args: object, **kwargs: object) -> None:
        super().setattr(target, *args, **kwargs)
        self._record_hazard(target, args, kwargs)

    def delattr(self, target: object, *args: object, **kwargs: object) -> None:
        super().delattr(target, *args, **kwargs)
        self._record_hazard(target, args, kwargs)

    def undo(self) -> None:
        self.key_build_hazards.clear()
        super().undo()


@pytest.fixture
def monkeypatch() -> Iterator[KeyBuildGuardPatch]:
    """Yield a KeyBuildGuardPatch for issue 260 (setattr and delattr; not item or env)."""
    patch = KeyBuildGuardPatch()
    yield patch
    patch.undo()


class KeyBuildUnderPatchError(AssertionError):
    """Raised when a key build runs under an active setattr or delattr patch (issue 260).

    Item and environment patches are not covered.
    """


def refuse_key_build_under_patch() -> None:
    """Raise KeyBuildUnderPatchError when any live patch holds hazards (issue 260).

    Covers setattr and delattr; item and environment patches are not covered.
    """
    hazards = [hazard for patch in KEY_BUILD_PATCHES for hazard in patch.key_build_hazards]
    if hazards:
        raise KeyBuildUnderPatchError(f"build the test key before patching: {', '.join(hazards)}")


def guarded_key_cache(build):
    """Wrap functools.cache(build) and refuse active patches on every call (issue 260).

    Covers setattr and delattr; item and environment patches are not covered.
    """
    cached = functools.cache(build)

    @functools.wraps(build)
    def wrapped(*args, **kwargs):
        refuse_key_build_under_patch()
        return cached(*args, **kwargs)

    wrapped.cache_clear = cached.cache_clear
    return wrapped


def openssl(*args: str, data: bytes = b"") -> bytes:
    refuse_key_build_under_patch()
    done = subprocess.run(
        ["openssl", *args], input=data, capture_output=True, check=True, timeout=60
    )
    return done.stdout


@guarded_key_cache
def app_key(bits: int = 2048, which: str = "app") -> tuple[str, bytes]:
    """A key made for this test run: the private PEM text and the public PEM."""
    del which
    private = openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{bits}")
    return private.decode(), openssl("pkey", "-pubout", data=private)


@guarded_key_cache
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
    status = fields.pop("status", None)
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
        "status": status,
        "checkSuites": {"nodes": suite_nodes},
    }


def build_commit_authors(commit_node, logins):
    """Use live commit-user shapes, defaulting to the latest commit's author."""
    users = (
        [commit_node["author"].get("user")]
        if logins is _ABSENT
        else [{"login": login} if login is not None else None for login in logins[:51]]
    )
    return {"totalCount": len(users), "nodes": [{"commit": {"author": {"user": u}}} for u in users]}


def pull(number: int, state: str, **fields) -> dict:
    """One open pull request as the query reads it."""
    commits = fields.pop("commits", None)
    commit_authors = fields.pop("commit_authors", _ABSENT)
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
    opinions = fields.pop("opinions", None)
    if opinions is None:
        opinion_nodes = [
            r
            for r in review_nodes.get("nodes") or []
            if r.get("state") in {"APPROVED", "CHANGES_REQUESTED"}
        ]
        opinions = {"totalCount": len(opinion_nodes), "nodes": opinion_nodes[-100:]}
    status = fields.pop("status", None)
    commit_node = build_commit_node(number, suites, status=status, **fields)
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
        "labels": {"totalCount": len(labels), "nodes": [{"name": name} for name in labels]},
        "reviews": review_nodes,
        "opinions": opinions,
        "refreshes": {"nodes": []},
        "holds": {"nodes": []},
        "latest": {"nodes": [{"commit": commit_node}]},
        "authors": build_commit_authors(commit_node, commit_authors),
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


# The train documents and their generated variants are checked against these tables below.
GRAPHQL_MUTATIONS = {
    "Arm": "enablePullRequestAutoMerge",
    "CreateCommit": "createCommitOnBranch",
    "Dequeue": "dequeuePullRequest",
    "Disarm": "disablePullRequestAutoMerge",
    "Draft": "convertPullRequestToDraft",
    "Enqueue": "enqueuePullRequest",
    "Merge": "mergePullRequest",
    "Ready": "markPullRequestReadyForReview",
}
GRAPHQL_STATUS_SELECTION = dict.fromkeys(
    (
        *GRAPHQL_MUTATIONS,
        "Commits",
        "FileModes",
        "LanderLease",
        "Merged",
        "MergeState",
        "PullPage",
        "RetryHistory",
        "ReviewBodies",
    ),
    False,
) | {"PullPageWithStatus": True}
PULL_PAGE_OPERATIONS = {"PullPage", "PullPageWithStatus"}


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
        self.queries: list[str] = []
        self.read_calls: list[tuple[str, dict]] = []
        self.pull_page_timeout_above: int | None = None
        self.read_statuses: dict[str, list[int]] = {}
        self.read_error_bodies: dict[int, bytes] = {}
        self.mutation_statuses: dict[str, list[int]] = {}
        self.merge_state_statuses: list[int] = []
        # GraphQL answers to cut under their full length, per kind ("read" or a mutation name).
        self.graphql_cuts: dict[str, int] = {}
        self.enqueue_errors: list[dict] = []
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
        # Downloads of an artifact still to cut; absent, every download is cut.
        self.storage_cuts_left: dict[int, int] = {}
        self.storage_reads: list[int] = []
        self.storage_auth_seen: list[str] = []
        self.users_auth_seen: list[str] = []
        self.artifact_redirect_locations: dict[int, str | None] = {}
        self.refuse_commit = False
        self.messages: list[tuple[str, str, str | None, str | None]] = []
        self._init_pull_reads()
        self.merged_pulls: dict[str, list[dict]] = {}
        self.issue_right = True
        self.ungranted: set[int] = set()
        self.jobs: dict[int, list[dict]] = {}
        self.jobs_calls: list[int] = []
        # Queued HTTP faults by REST route or GraphQL operation name.
        self.rest_faults: dict[str | tuple[str, str, str], list[int | str | tuple[int, bytes]]] = {}
        self.fault_requests: list[tuple[str, str]] = []
        self.request_ledger: set[tuple[str, str, str]] = set()
        self.request_refusals: list[str] = []
        self.redirect_location: tuple[str, str, str | None, str] | None = None
        self.request_lock, self.response_lock = threading.Lock(), threading.Lock()
        self.pending_response: object | None = None
        self.label_missing = False
        self.unlabel_status: int | None = None
        self.repository_moves: dict[str, str] = {}
        self.repository_statuses: dict[tuple[int, str], int] = {}
        self.repository_bodies: dict[str, object] = {}
        self.repository_calls: list[tuple[int, str]] = []
        self.install(7, "fleet", app=list(reads))

    def _init_pull_reads(self) -> None:
        self.commits: dict[int, list[tuple[str, int]]] = {}
        self.commit_heads: dict[int, str] = {}
        self.merge_states: dict[int, str] = {}
        self.commit_counts: dict[int, int] = {}
        # One detail per number, or a list read in turn (the last repeats); None reads as
        # a pull request the forge no longer returns.
        self.review_details: dict[int, dict | list[dict | None] | None] = {}
        self.review_refusals: dict[int, str] = {}
        self.review_body_calls: list[int] = []
        self.rights: dict[str, str] = {}
        self.right_refusals: dict[str, int] = {}
        self.right_calls: list[str] = []
        self.retry_histories: dict[int, dict] = {}
        self.retry_refusals: dict[int, str] = {}
        self.retry_calls: list[int] = []
        self.comparisons: dict[str, tuple[int, dict]] = {}
        self.compare_calls: list[str] = []
        self.lease_read: dict = {}
        self.lease_calls = 0
        self.modes: dict[tuple[str, str], int] = {}
        self.mode_error: str | None = None
        self.mode_calls: list[list[str]] = []

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
        """Record variables used as message arguments on the declared mutation field."""
        args = graphql_mutation_arguments(query, GRAPHQL_MUTATIONS[kind.capitalize()])
        headline = (
            variables.get("headline")
            if graphql_contains(args, "commitHeadline", ":", "$", "headline")
            else None
        )
        body = (
            variables.get("body")
            if graphql_contains(args, "commitBody", ":", "$", "body")
            else None
        )
        self.messages.append((kind, variables["id"], headline, body))

    def graphql(self, header: str, operation: str, query: str, variables: dict) -> dict:
        """Serve one GraphQL call: a mutation needs write rights and is recorded."""
        full, rights = self.repo_of(header)
        mutation = GRAPHQL_MUTATIONS.get(operation)
        if mutation is not None:
            assert rights in (WRITE, {**WRITE, **STATUSES}), rights
        if mutation == "convertPullRequestToDraft":
            self.write(full, ("draft", variables["id"]))
            return {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}}
        if mutation == "markPullRequestReadyForReview":
            self.write(full, ("ready", variables["id"]))
            return {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}}
        if mutation == "disablePullRequestAutoMerge":
            self.write(full, ("disarm", variables["id"]))
            return {
                "data": {"disablePullRequestAutoMerge": {"pullRequest": {"autoMergeRequest": None}}}
            }
        if mutation == "dequeuePullRequest":
            self.write(full, ("dequeue", variables["id"]))
            return {
                "data": {"dequeuePullRequest": {"mergeQueueEntry": {"id": f"MQ_{variables['id']}"}}}
            }
        if mutation == "enqueuePullRequest":
            self.write(full, ("enqueue", variables["id"], variables["oid"]))
            if self.enqueue_errors:
                return {"errors": self.enqueue_errors}
            entry = {"state": "QUEUED"} if self.merged else None
            return {"data": {"enqueuePullRequest": {"mergeQueueEntry": entry}}}
        if mutation == "mergePullRequest":
            self._record_message("merge", query, variables)
            self.write(full, ("merge", variables["id"], variables["oid"], variables["method"]))
            if (self.active_reads.get(full) or {}).get("mergeQueue"):
                return {"errors": [{"type": "UNPROCESSABLE", "message": QUEUE_ONLY}]}
            if self.merge_errors:
                return {"errors": self.merge_errors}
            if self.refuse_merge or full in self.refuse_merge_in:
                return {"errors": [{"message": self.refuse_merge or "refused here"}]}
            return {"data": {"mergePullRequest": {"pullRequest": {"merged": self.merged}}}}
        if mutation == "enablePullRequestAutoMerge":
            self._record_message("arm", query, variables)
            self.write(full, ("arm", variables["id"], variables["oid"], variables["method"]))
            armed = {"autoMergeRequest": {"enabledAt": NOW.isoformat()}}
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": armed}}}
        if mutation == "createCommitOnBranch":
            return self._handle_commit_mutation(full, variables)
        return self._read_graphql(full, operation, query, variables)

    def _read_graphql(self, full: str, operation: str, query: str, variables: dict) -> dict:
        if operation == "LanderLease":
            self.lease_calls += 1
            default = {
                "data": {"repository": {"landerLease": self.active_reads[full]["landerLease"]}}
            }
            return self.lease_read or default
        if operation == "FileModes":
            return self.pull_modes(query, variables)
        if operation == "RetryHistory":
            return self.pull_retry_history(full, variables)
        if operation == "MergeState":
            return self.pull_merge_state(full, variables)
        if operation == "ReviewBodies":
            return self.pull_review_bodies(full, variables)
        if operation == "Commits":
            return self.pull_commits(full, variables)
        if operation == "Merged":
            nodes = self.merged_pulls.get(full, [])
            return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}
        assert operation in PULL_PAGE_OPERATIONS, operation
        return self.pull_page(full, operation, query, variables)

    def pull_modes(self, query: str, variables: dict) -> dict:
        paths = [
            value
            for key, value in variables.items()
            if key.startswith("p") and ascii_digits(key[1:])
        ]
        self.mode_calls.append(paths)
        tokens = tuple(graphql_tokens(query))
        for index, path in enumerate(paths):
            variable = f"p{index}"
            assert graphql_contains(tokens, "$", variable, ":", "String", "!")
            assert graphql_contains(
                tokens,
                f"f{index}",
                ":",
                "file",
                "(",
                "path",
                ":",
                "$",
                variable,
                ")",
                "{",
                "mode",
                "}",
            )
            assert all(path not in token for token in tokens)
        if self.mode_error:
            return {"errors": [{"message": self.mode_error}]}
        return {"data": {"repository": retry_modes(variables, self.modes)}}

    def pull_retry_history(self, full: str, variables: dict) -> dict:
        """Read one pull's retry attempts, including discarded heads."""
        owner, name = full.split("/")
        number = variables["number"]
        assert variables == {"owner": owner, "name": name, "number": number}, variables
        self.retry_calls.append(number)
        if number in self.retry_refusals:
            return {"errors": [{"message": self.retry_refusals[number]}]}
        detail = self.retry_histories.get(number, retry_history(number))
        return {"data": {"repository": {"pullRequest": detail}}}

    def compare(self, header: str, full: str, refs: str) -> tuple[int, dict]:
        """The JSON compare endpoint, keyed by head oid with a checked three-dot base."""
        assert self.repo_of(header)[0] == full
        base, separator, head = refs.partition("...")
        assert (base, separator) == ("b" * 40, "..."), refs
        self.compare_calls.append(head)
        return self.comparisons.get(head, (200, {"files": [retry_file()]}))

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
            "headRefOid": detail.get("headRefOid", ""),
            "author": detail.get("author"),
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
        if number in self.commit_heads:
            head = self.commit_heads[number]
        else:
            read_nodes = self.active_reads.get(full, {}).get("pullRequests", {}).get("nodes", [])
            matches = [p["headRefOid"] for p in read_nodes if p["number"] == number]
            head = matches[0] if matches else f"{number:040x}"
        nodes = [
            {"commit": {"message": message, "parents": {"totalCount": parents}}}
            for message, parents in items
        ]
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "headRefOid": head,
                        "commits": {"totalCount": total, "nodes": nodes},
                    }
                }
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

    def _resolve_status(self, raw: dict | None, name: str | None) -> dict | None:
        """The head status as a lookup by context name returns it: a state or a null context."""
        if raw is None:
            return None
        if "context" in raw:
            return raw
        if "contexts" in raw:
            matching = [c for c in raw["contexts"] if c.get("context") == name]
            return {"context": {"state": matching[0]["state"]}} if matching else {"context": None}
        return None

    def _extract_page_nodes(
        self, nodes: list[dict], start: int, size: int, operation: str, variables: dict
    ) -> list[dict]:
        """One page of pull requests, the head status resolved by name where the query asks."""
        selects_status = GRAPHQL_STATUS_SELECTION[operation]
        status_name = variables.get("status")
        page_nodes = []
        for p in nodes[start : min(start + size, len(nodes))]:
            p_copy = dict(p)
            if "latest" in p and "nodes" in p["latest"]:
                latest_nodes = []
                for ln in p["latest"]["nodes"]:
                    ln_copy = dict(ln)
                    if "commit" in ln:
                        commit_copy = dict(ln["commit"])
                        if selects_status:
                            commit_copy["status"] = self._resolve_status(
                                commit_copy.get("status"), status_name
                            )
                        else:
                            commit_copy.pop("status", None)
                        ln_copy["commit"] = commit_copy
                    latest_nodes.append(ln_copy)
                p_copy["latest"] = {**p["latest"], "nodes": latest_nodes}
            page_nodes.append(p_copy)
        return page_nodes

    def pull_page(self, full: str, operation: str, query: str, variables: dict) -> dict:
        """Answer the open pull request query: check its variables, return the asked page."""
        owner, name = full.split("/")
        after, size = variables["after"], variables["first"]
        expected = {"owner": owner, "name": name, "after": after, "first": size}
        if "status" in variables:
            expected["status"] = variables["status"]
        assert variables == expected, variables
        tokens = tuple(graphql_tokens(query))
        assert graphql_contains(tokens, "$", "first", ":", "Int", "!")
        assert graphql_contains(
            tokens, "pullRequests", "(", "states", ":", "OPEN", "first", ":", "$", "first"
        )
        if after is None:
            reads = self.reads[full]
            self.active_reads[full] = reads.pop(0) if len(reads) > 1 else reads[0]
        read = self.active_reads[full]
        start = int(after.removeprefix("cursor-")) if after else 0
        nodes = read["pullRequests"]["nodes"]
        end = min(start + size, len(nodes))
        page_nodes = self._extract_page_nodes(nodes, start, size, operation, variables)
        page = {
            "nodes": page_nodes,
            "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": f"cursor-{end}"},
        }
        return {"data": {"repository": {**read, "pullRequests": page}}}

    def graphql_response(
        self, header: str, query: str, variables: dict
    ) -> tuple[int, dict | bytes]:
        """HTTP status and body of one GraphQL call; a status read without the right fails."""
        operation = graphql_operation(query)
        if operation is None:
            return 400, {"message": "stand-in forge: anonymous operation"}
        full, rights = self.repo_of(header)
        self.queries.append(query)
        if GRAPHQL_STATUS_SELECTION[operation] and rights.get("statuses") != "read":
            return 200, {
                "errors": [
                    {"type": "FORBIDDEN", "message": "Resource not accessible by integration"}
                ]
            }
        if mutation := GRAPHQL_MUTATIONS.get(operation):
            body = self.graphql(header, operation, query, variables)
            statuses = self.mutation_statuses.get(mutation, [])
        elif operation in PULL_PAGE_OPERATIONS:
            self.read_calls.append((full, dict(variables)))
            size = variables["first"]
            if size > 25:
                return 502, {"message": "stand-in forge: read page size exceeds 25"}
            if self.pull_page_timeout_above is not None and size > self.pull_page_timeout_above:
                return 504, self.read_error_bodies.get(
                    504, {"message": "stand-in forge: pull page timed out"}
                )
            statuses = self.read_statuses.get(full, [])
            body = None
        elif operation == "MergeState":
            statuses = self.merge_state_statuses
            body = None
        else:
            statuses = []
            body = None
        status = statuses.pop(0) if statuses else 200
        if status != 200:
            if statuses is self.read_statuses.get(full) and status in self.read_error_bodies:
                return status, self.read_error_bodies[status]
            return status, {"message": f"stand-in forge: queued HTTP {status}"}
        return status, body or self.graphql(header, operation, query, variables)

    def graphql_cut(self, query: str) -> bool:
        """Whether this GraphQL answer is cut: a cut of its kind is still queued."""
        kind = GRAPHQL_MUTATIONS.get(graphql_operation(query), "read")
        if self.graphql_cuts.get(kind, 0) <= 0:
            return False
        self.graphql_cuts[kind] -= 1
        return True

    def write(self, full: str, write: tuple) -> None:
        self.where.append((full, write))
        if full == "fleet/app":
            self.writes.append(write)

    def update(self, header: str, full: str, number: int, body: dict) -> tuple[int, dict]:
        """Record a branch update made with write rights; answer as queued, else accepted."""
        token_repo, rights = self.repo_of(header)
        assert token_repo == full and rights in (WRITE, {**WRITE, **STATUSES}), (
            token_repo,
            rights,
            full,
        )
        self.write(full, ("update", number, body["expected_head_sha"]))
        code, message = self.update_answers.get(number, (202, "Updating pull request branch."))
        return code, {"message": message}

    def run_jobs(self, header: str, full: str, run_id: int) -> tuple[int, dict]:
        """Record a jobs read made with write rights; return the jobs queued for that run."""
        token_repo, rights = self.repo_of(header)
        assert token_repo == full and rights in (WRITE, {**WRITE, **STATUSES}), (
            token_repo,
            rights,
            full,
        )
        self.jobs_calls.append(run_id)
        return 200, {"jobs": self.jobs.get(run_id, [])}

    def label_write(self, header: str, full: str, write: tuple) -> tuple[int, dict]:
        """Record a label write made with write rights; removing a missing label is a 404."""
        token_repo, rights = self.repo_of(header)
        assert token_repo == full and rights in (WRITE, {**WRITE, **STATUSES}), (
            token_repo,
            rights,
            full,
        )
        if write[0] == "unlabel" and self.label_missing:
            return 404, {"message": "Label does not exist"}
        if write[0] == "unlabel" and self.unlabel_status:
            return self.unlabel_status, {"message": "stand-in forge: label removal failed"}
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
                "id": inst["id"] * 1000 + index,
                "name": name,
                "full_name": f"{inst['account']}/{name}",
                "archived": f"{inst['account']}/{name}" in self.archived,
                "disabled": False,
            }
            for index, name in enumerate(inst["repos"])
        ]
        return {"total_count": len(repos), "repositories": repos}

    def repository_read(self, header: str, path: str) -> tuple[int, object]:
        """Metadata lookups use the live list token, including a moved repository's new path."""
        token = header.removeprefix("Bearer ")
        kind = self.tokens[token]
        assert kind[0] == "list" and token not in self.revoked
        self.repository_calls.append((kind[1], path))
        if status := self.repository_statuses.get((kind[1], path)):
            return status, {"message": "planted response body must stay private"}
        if path in self.repository_moves:
            return 301, self.repository_moves[path]
        if path in self.repository_bodies:
            return 200, self.repository_bodies[path]
        for inst in self.installations:
            for index, name in enumerate(inst["repos"]):
                full = f"{inst['account']}/{name}"
                repo_id = inst["id"] * 1000 + index
                if path.casefold() in (f"/repos/{full}".casefold(), f"/repositories/{repo_id}"):
                    return 200, {"id": repo_id, "full_name": full}
        return 404, {"message": "Not Found"}


ARTIFACTS_ROUTE = "GET /repos/{owner}/{repo}/actions/artifacts"
JOBS_ROUTE = "GET /repos/{owner}/{repo}/actions/runs/{id}/jobs"
INSTALLATION_REPOSITORIES_ROUTE = "GET /installation/repositories"
REVOKE_ROUTE = "DELETE /installation/token"


def ascii_digits(value):
    """Match decimal IDs and variable suffixes without parsing them as expressions."""
    return value.isascii() and value.isdecimal()


def repository_route(parts, field, number, *, source):
    """Normalize the repository suffix, preserving fixed source paths."""
    match parts:
        case ["contents", *tail] if (
            tail and tail != [".github", "generated-files"] and (not source or tail == ["~"])
        ):
            return ["contents", "{path}"]
        case ["compare", refs]:
            base, sep, head = refs.partition("...")
            if sep and field(base) and field(head):
                return ["compare", "{base}...{head}"]
        case ["collaborators", login, "permission"] if field(login):
            return ["collaborators", "{login}", "permission"]
        case ["issues", ident, "labels", label] if number(ident) and field(label):
            return ["issues", "{id}", "labels", "{label}"]
        case ["issues" | "pulls", ident, *tail] if number(ident):
            return [parts[0], "{id}", *tail]
        case ["actions", "artifacts", ident, "zip"] if number(ident):
            return ["actions", "artifacts", "{id}", "zip"]
        case ["actions", "runs", ident, "jobs"] if number(ident):
            return ["actions", "runs", "{id}", "jobs"]
    return parts


def rest_route(method, url, *, source=False):
    """Normalize route fields while keeping literal source paths distinct."""
    path = urlsplit(url).path
    assert path.startswith("/"), f"unresolved REST URL: {url}"
    parts = path[1:].split("/")

    def field(value):
        """Accept a source placeholder or a nonempty runtime field."""
        return value == "~" if source else bool(value)

    def number(value):
        """Accept a source placeholder or a decimal runtime identifier."""
        return field(value) if source else ascii_digits(value)

    match parts:
        case ["repos", owner, repo, *tail] if field(owner) and field(repo):
            parts = [
                "repos",
                "{owner}",
                "{repo}",
                *repository_route(tail, field, number, source=source),
            ]
        case ["users", login] if field(login.removesuffix("[bot]") if source else login):
            parts = ["users", "{login}"]
        case ["app", "installations", ident, "access_tokens"] if number(ident):
            parts = ["app", "installations", "{id}", "access_tokens"]
        case ["repositories" | "storage", ident] if number(ident):
            parts = [parts[0], "{id}"]
    path = "/" + "/".join(parts)
    assert not source or "~" not in path, f"unknown REST route shape: {method} {url}"
    return f"{method} {path}"


GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
GRAPHQL_LEXEME = re.compile(
    r"(?P<ignored>[ \t\r\n,\ufeff]+|#[^\r\n]*)"
    r'|(?P<token>"""(?:\\"""|(?!""")[\s\S])*"""|"(?:\\[^\r\n]|[^"\\\r\n])*"'
    r"|[_A-Za-z][_0-9A-Za-z]*|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    r"|\.\.\.|[!$():=@\[\]{|}&])"
    r"|(?P<invalid>.)"
)


def graphql_tokens(query):
    """Yield GraphQL tokens, skipping comments and keeping strings opaque."""
    for match in GRAPHQL_LEXEME.finditer(query):
        if token := match["token"]:
            yield token
        elif invalid := match["invalid"]:
            raise AssertionError(f"invalid stand-in GraphQL token: {invalid!r}")


def graphql_operation(query):
    """Read the declared operation name without matching the document body."""
    tokens = graphql_tokens(query)
    kind, name = next(tokens, ""), next(tokens, "")
    if kind in {"query", "mutation"} and GRAPHQL_NAME.fullmatch(name):
        return name
    return None


def graphql_level(tokens):
    """Yield one level of tokens, keeping nested values outside the sequence."""
    depth = 0
    for token in tokens:
        if token in (")", "]", "}"):
            if not depth:
                return
            depth -= 1
        elif not depth:
            yield token
        if token in ("(", "[", "{"):
            depth += 1


def graphql_mutation_arguments(query, mutation):
    """Read the arguments of the declared mutation's outer field."""
    tokens = graphql_tokens(query)
    assert next(tokens, None) == "mutation", mutation
    assert "{" in graphql_level(tokens), mutation
    field, following = next(tokens, ""), next(tokens, "")
    if following == ":":
        field, following = next(tokens, ""), next(tokens, "")
    assert field == mutation, (field, mutation)
    return graphql_input_fields(tokens) if following == "(" else ()


def graphql_input_fields(tokens):
    """Read only the immediate fields of the mutation's input argument."""
    recent: tuple[str, ...] = ()
    for token in graphql_level(tokens):
        recent = (*recent[-2:], token)
        if recent == ("input", ":", "{"):
            return tuple(graphql_level(tokens))
    return ()


def graphql_contains(tokens, *pattern):
    """Match a contiguous token sequence."""
    return any(tokens[index : index + len(pattern)] == pattern for index in range(len(tokens)))


class ForgeRequestRefusalError(AssertionError):
    """A named request refusal to report on the test thread."""


class Handler(BaseHTTPRequestHandler):
    """The stand-in forge's HTTP side; what it serves is the Forge on its server."""

    response_location: str | None = None

    def log_message(self, *args) -> None:
        pass

    def request_url(self) -> str:
        """Return the exact URL used to match this request against issued redirects."""
        return urldefrag(f"http://{self.headers['Host']}{self.path}").url

    def send_header(self, keyword, value) -> None:
        """Refuse a second Location before sending the response headers."""
        if keyword.lower() == "location":
            if self.response_location is not None:
                raise ForgeRequestRefusalError(
                    f"{self.command} {self.path}: second Location {value!r} "
                    f"after {self.response_location!r}"
                )
            self.response_location = value
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        """Stage the single Location once the response headers have been sent."""
        super().end_headers()
        value, self.response_location = self.response_location, None
        if value is not None:
            location, _ = urldefrag(urljoin(self.request_url(), value))
            self.server.forge.redirect_location = (
                self.command,
                location,
                self.headers.get("Authorization"),
                self.route,
            )

    def normalized_route(self, pending) -> str:
        """Name the pending redirect if the incoming route cannot be normalized."""
        try:
            return rest_route(self.command, self.path)
        except (AssertionError, ValueError) as error:
            if pending is None:
                raise
            _, location, _, issuer = pending
            reason = str(error).partition("\n")[0]
            raise ForgeRequestRefusalError(
                f"{self.command} {self.path}: pending redirect from {issuer} to {location} "
                f"refused: {reason}"
            ) from error

    def request_stage(self, pending) -> str:
        """Match the next request to its issuer's method, Location and authorization policy."""
        if pending is None:
            return "direct"
        method, location, authorization, issuer = pending
        storage = issuer == ARCHIVE_ROUTE and self.route == STORAGE_ROUTE
        followed = (self.command, self.request_url()) == (method, location) and (
            storage or self.headers.get("Authorization") == authorization
        )
        return "redirect" if followed else "direct"

    def send_final(self, write, *args) -> None:
        """Send the last bytes and publish completion before the next admission check."""
        forge = self.server.forge
        with forge.response_lock:
            write(*args)
            forge.pending_response = None

    def send_body(self, data: bytes) -> None:
        """Complete a length-delimited response at the last body byte or empty header block."""
        if data:
            self.end_headers()
            self.send_final(self.wfile.write, data)
        else:
            self.send_final(self.end_headers)

    def answer(self, code: int, body: dict | list | bytes | None) -> None:
        data = body
        if not isinstance(body, bytes):
            data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_body(data)

    def cut_answer(self, code: int, body: dict) -> None:
        """Answer under the full Content-Length with half the body, then close."""
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data[: len(data) // 2])
        self.close_connection = True

    def body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def fault(self, key, legacy_key) -> bool:
        """Serve the next queued fault, or close without an answer for DROP."""
        faults = self.server.forge.rest_faults
        queued = faults.get(key, faults.get(legacy_key, []) if key[2] == "direct" else [])
        if not queued:
            return False
        self.server.forge.fault_requests.append((self.command, self.path))
        fault = queued.pop(0)
        if fault == DROP:
            self.close_connection = True
        elif isinstance(fault, tuple):
            self.answer(*fault)
        else:
            self.answer(int(fault), {"message": f"stand-in forge: queued HTTP {fault}"})
        return True

    def dispatch(self, serve_rest) -> None:
        """Refuse a concurrent request only while another response is unsent."""
        forge = self.server.forge
        with forge.response_lock:
            if forge.pending_response is not None:
                raise ForgeRequestRefusalError(
                    f"concurrent stand-in forge request: {self.command} {self.path}"
                )
            forge.pending_response = getattr(self, "request", self)
        with forge.request_lock:
            pending, forge.redirect_location = forge.redirect_location, None
            self.route = self.normalized_route(pending)
            stage = self.request_stage(pending)
            request = self.body() if (self.command, self.path) == ("POST", "/graphql") else None
            operation = graphql_operation(request["query"]) if request is not None else None
            path = f"/graphql [{operation}]" if operation else self.route.split(" ", 1)[1]
            key = (self.command, path, stage)
            forge.request_ledger.add(key)
            if not self.fault(key, operation or self.route):
                if request is None:
                    serve_rest()
                else:
                    self._serve_graphql(request)

    def do_GET(self) -> None:
        """Dispatch a GET through the common fault check."""
        self.dispatch(self._serve_get)

    def do_POST(self) -> None:
        """Dispatch a POST through the common fault check."""
        self.dispatch(self._serve_post)

    def do_PUT(self) -> None:
        """Dispatch a PUT through the common fault check."""
        self.dispatch(self._serve_put)

    def do_PATCH(self) -> None:
        """Dispatch a PATCH through the common fault check."""
        self.dispatch(self._serve_patch)

    def do_DELETE(self) -> None:
        """Dispatch a DELETE through the common fault check."""
        self.dispatch(self._serve_delete)

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
        self.send_final(self.end_headers)

    def _get_storage(self, path: str) -> None:
        if self.headers.get("Authorization"):
            self.server.forge.storage_auth_seen.append(self.headers["Authorization"])
            self.answer(400, {"message": "storage URL refuses Authorization header"})
            return
        art_id = int(path.removeprefix("/storage/"))
        self.server.forge.storage_reads.append(art_id)
        data = self.server.forge.storage.get(art_id)
        if data is None:
            self.answer(404, {"message": "Not Found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(data)))
        cut = self.server.forge.storage_cut.get(art_id)
        left = self.server.forge.storage_cuts_left.get(art_id)
        if left is not None:
            self.server.forge.storage_cuts_left[art_id] = left - 1
        if cut is not None and (left is None or left > 0):
            self.end_headers()
            self.wfile.write(data[:cut])
            self.close_connection = True
            return
        self.send_body(data)

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
        if self.route in {RELEASE_NAME_ROUTE, RELEASE_ID_ROUTE}:
            self._get_repository(path)
            return True
        return False

    def _get_collaborator(self, path: str) -> None:
        """Serve a collaborator permission read independently of other REST routes."""
        forge = self.server.forge
        full, _, tail = path.removeprefix("/repos/").partition("/collaborators/")
        assert forge.repo_of(self.headers["Authorization"])[0] == full
        login = unquote(tail.removesuffix("/permission"))
        forge.right_calls.append(login)
        token = self.headers["Authorization"].removeprefix("Bearer ")
        if forge.tokens[token][3].get("metadata") != "read":
            self.answer(403, {"message": "Resource not accessible by integration"})
        elif login in forge.right_refusals:
            self.answer(forge.right_refusals[login], {"message": "Permission read refused"})
        elif login in forge.rights:
            permission = forge.rights[login]
            self.answer(200, {"permission": permission, "role_name": permission})
        else:
            self.answer(404, {"message": "Not Found"})

    def _get_repository(self, path: str) -> None:
        code, body = self.server.forge.repository_read(self.headers["Authorization"], path)
        if code == 301:
            host, port = self.server.server_address[:2]
            location = str(body).format(api=f"http://{host}:{port}", port=port)
            self.send_response(code)
            self.send_header("Location", location)
            self.send_final(self.end_headers)
        elif isinstance(body, bytes):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.send_body(body)
        else:
            self.answer(code, body)

    def _serve_get(self) -> None:
        """Dispatch a REST read after the common fault check."""
        path = urlsplit(self.path).path
        if self._dispatch_app(path):
            return
        if "/compare/" in path and path.startswith("/repos/"):
            full, _, refs = path.removeprefix("/repos/").partition("/compare/")
            assert self.headers["Accept"] == "application/vnd.github+json"
            self.answer(*self.server.forge.compare(self.headers["Authorization"], full, refs))
            return
        if self.route == PERMISSION_ROUTE:
            self._get_collaborator(path)
        elif "/contents/" in path and path.startswith("/repos/"):
            self._get_contents(path)
        elif path.startswith("/repos/") and path.endswith("/actions/artifacts"):
            self._get_artifacts(path)
        elif "/actions/artifacts/" in path and path.endswith("/zip"):
            self._get_artifact_zip(path)
        elif path.startswith("/storage/"):
            self._get_storage(path)
        elif self.route == JOBS_ROUTE:
            full, _, tail = path.removeprefix("/repos/").partition("/actions/runs/")
            run_id = int(tail.removesuffix("/jobs"))
            self.answer(*self.server.forge.run_jobs(self.headers["Authorization"], full, run_id))
        else:
            raise AssertionError(self.path)

    def _serve_post(self) -> None:
        """Serve REST POST requests after the common fault check."""
        if self.path.startswith("/repos/") and self.path.endswith("/comments"):
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            number = int(tail.removesuffix("/comments"))
            write = ("comment", number, self.body()["body"])
            self.answer(*self.server.forge.issue_write(self.headers["Authorization"], full, write))
            return
        if self.route == ADD_LABEL_ROUTE:
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            write = ("label", int(tail.removesuffix("/labels")), *self.body()["labels"])
            self.answer(*self.server.forge.label_write(self.headers["Authorization"], full, write))
            return
        prefix, _, tail = self.path.partition("/app/installations/")
        number, _, action = tail.partition("/")
        assert (prefix, action) == ("", "access_tokens"), self.path
        if not self.app_call():
            self.answer(*self.server.forge.mint(int(number), self.body()))

    def _serve_graphql(self, request) -> None:
        """Serve a GraphQL request whose operation fault queue was already checked."""
        forge = self.server.forge
        query = request["query"]
        header = self.headers["Authorization"]
        code, body = forge.graphql_response(header, query, request["variables"])
        if forge.graphql_cut(query):
            self.cut_answer(code, body)
        else:
            self.answer(code, body)

    def _serve_put(self) -> None:
        """Serve a branch update after the common fault check."""
        prefix, _, tail = self.path.rpartition("/pulls/")
        number, _, action = tail.partition("/")
        assert prefix.startswith("/repos/") and action == "update-branch", self.path
        full = prefix.removeprefix("/repos/")
        self.answer(
            *self.server.forge.update(self.headers["Authorization"], full, int(number), self.body())
        )

    def _serve_patch(self) -> None:
        """Serve an issue close after the common fault check."""
        full, _, number = self.path.removeprefix("/repos/").partition("/issues/")
        body = self.body()
        write = ("close", int(number), body["state"], body["state_reason"])
        self.answer(*self.server.forge.issue_write(self.headers["Authorization"], full, write))

    def _serve_delete(self) -> None:
        """Serve a label removal or revocation after the common fault check."""
        if "/labels/" in self.path:
            full, _, tail = self.path.removeprefix("/repos/").partition("/issues/")
            number, _, name = tail.partition("/labels/")
            write = ("unlabel", int(number), unquote(name))
            self.answer(*self.server.forge.label_write(self.headers["Authorization"], full, write))
            return
        assert self.path == "/installation/token", self.path
        self.server.forge.revoked.append(self.headers["Authorization"].removeprefix("Bearer "))
        self.answer(204, None)


class ForgeServer(ThreadingHTTPServer):
    """Report named HTTP worker refusals on the test thread."""

    def __init__(self, forge: Forge) -> None:
        """Attach the forge and bind to a loopback port."""
        self.forge = forge
        super().__init__(("127.0.0.1", 0), Handler)

    def handle_error(self, request, client_address) -> None:
        """Record named refusals on the HTTP worker error path."""
        if isinstance(error := sys.exception(), ForgeRequestRefusalError):
            self.forge.request_refusals.append(str(error))
        else:
            super().handle_error(request, client_address)

    def shutdown_request(self, request) -> None:
        """Complete cuts, drops and aborted answers when the socket signals EOF."""
        with self.forge.response_lock:
            super().shutdown_request(request)
            if self.forge.pending_response is request:
                self.forge.pending_response = None

    def server_close(self) -> None:
        """Close the server and fail the test with any named request refusals."""
        super().server_close()
        if self.forge.request_refusals:
            raise ForgeRequestRefusalError("\n".join(self.forge.request_refusals))


def serve(forge: Forge) -> ForgeServer:
    server = ForgeServer(forge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


REPORT_FILES = ("GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT", "TRAIN_RELEASE_FILE")
REPORT_HEADINGS = {
    "| Account | Repositories | Landed | Updated | Refused |",
    "| Pull request | Merge state | Train |",
}
REPORT_LINES = re.compile(
    r"## Merge train sweep|### \S+: .*|review bots: .*|Refused:|"
    r"Bot pull requests without Signed-off-by, their producers sign them:|"
    r"- .+|Not reached within .*|No release run, the job matrix holds .*|"
    r"Release train off: .*|Release pull requests and releases next: .*|"
    r"Only the 100 oldest open pull requests were read\.|"
    r"Split open pull request page to .*|Closed issue #.*|note: .*"
)
REPORT_ANNOTATION = re.compile(r"::(?:error|warning|notice) title=(?:(?!::).)*::.*")
TABLE_PIPE = re.compile(r"(?<!\\)\|")
RECORD_CONTROLS = frozenset("\0\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
# Leave headroom above measured fixture output while bounding each retained destination.
LINE_SHAPE_MAX_BYTES = 64 * 1024
LINE_SHAPE_PLANT_PYTEST = "line-shape plant pytest"
LINE_SHAPE_EXCEPTIONS = {
    (
        None,
        f"{LINE_SHAPE_PLANT_PYTEST} stdout",
    ): "Nested pytest reports the planted teardown error.",
    (
        None,
        f"{LINE_SHAPE_PLANT_PYTEST} stderr",
    ): "Nested pytest may report its own diagnostics on stderr.",
    (None, "ADR-92/93 pytest stdout"): "Nested pytest writes its own progress and failure report.",
    (None, "ADR-92/93 pytest stderr"): "Nested pytest may report its own diagnostics on stderr.",
    (
        "test_planted_dependency_unsigned_note_without_value_error_refusal",
        "train stderr",
    ): "The test asserts the deliberately uncaught refusal and its Python traceback.",
    (
        "test_anonymous_retry_document_stops_the_run[document-error-RetryHistory]",
        "train stderr",
    ): "The test asserts the traceback from refusing a planted anonymous RetryHistory document.",
    (
        "test_anonymous_retry_document_stops_the_run[document-error-FileModes]",
        "train stderr",
    ): "The test asserts the traceback from refusing a planted anonymous FileModes document.",
}


class LineShapeError(AssertionError):
    """A captured train destination contained a malformed record."""


def compact_json(text):
    """The release destinations use one compact JSON value."""
    try:
        return json.dumps(json.loads(text), separators=(",", ":"), allow_nan=False) == text
    except (ValueError, RecursionError):
        return False


def table_cells(line):
    """Count Markdown cells without treating escaped pipes as separators."""
    if not line.endswith("|"):
        return -1
    return len(TABLE_PIPE.findall(line)) - 1


def check_report_line(destination, number, line, columns):
    """Validate a report record and carry the surrounding table's column count."""
    where = f"{destination}:{number}"
    if line.startswith("|"):
        cells = table_cells(line)
        if line in REPORT_HEADINGS:
            return cells
        if columns is None or cells != columns:
            raise LineShapeError(f"{where}: expected {columns} table cells, got {cells}: {line!r}")
        return columns
    if line and not (REPORT_LINES.fullmatch(line) or REPORT_ANNOTATION.fullmatch(line)):
        raise LineShapeError(f"{where}: unrecognized report record: {line!r}")
    return None


def check_line_shape(destination, text, *, allow_external=False):
    """Check the physical separators and the record shapes for this destination.

    The net checks each line per destination and cannot attribute individually valid
    lines to one record. Record-level flattening is tested at write_records,
    annotate and note_cell. Further harness forms are tracked in issue 269.
    """
    columns = None
    release_records = 0
    for number, line in enumerate(text.split("\n"), 1):
        where = f"{destination}:{number}"
        if RECORD_CONTROLS.intersection(line):
            raise LineShapeError(f"{where}: forbidden record separator or NUL: {line!r}")
        if allow_external or destination.startswith("TRAIN_RELEASE_FILE"):
            continue
        if destination.startswith("GITHUB_OUTPUT"):
            if line and not (line.startswith("releases=") and compact_json(line[9:])):
                raise LineShapeError(f"{where}: expected compact releases= JSON: {line!r}")
            release_records += bool(line)
            if release_records > 1:
                raise LineShapeError(f"{where}: more than one releases= record")
        elif destination.endswith(" stderr"):
            if line:
                raise LineShapeError(f"{where}: unexpected stderr record: {line!r}")
        else:
            columns = check_report_line(destination, number, line, columns)
    if destination.startswith("TRAIN_RELEASE_FILE") and not compact_json(text):
        raise LineShapeError(f"{destination}:1: expected one compact JSON document")


class LineShapeCapture:
    """Keep the outputs that capfd.readouterr and subprocess PIPEs would otherwise drain."""

    def __init__(self, readouterr, caplog, test_name):
        """Set up capture buffers and per-destination byte accounting for one test."""
        self.readouterr = readouterr
        self.caplog = caplog
        self.test_name = test_name
        self.fd_streams = {"fd stdout": [], "fd stderr": []}
        self.process_streams = []
        self.files = set()
        self.sizes = Counter()
        self.overflow = None
        self.file_error = None

    def reserve(self, destination, size, *, replace=False):
        """Track current file sizes or accumulated stream bytes and defer overflow errors."""
        self.sizes[destination] = size if replace else self.sizes[destination] + size
        total = self.sizes[destination]
        if total <= LINE_SHAPE_MAX_BYTES:
            return True
        if self.overflow is None:
            self.overflow = LineShapeError(
                f"{destination}:1: {total} bytes exceeds the "
                f"{LINE_SHAPE_MAX_BYTES}-byte capture limit"
            )
        return False

    def read_fd(self):
        """Return captured descriptors to the caller and retain them for teardown."""
        captured = self.readouterr()
        for name, text in zip(self.fd_streams, captured, strict=True):
            if self.reserve(name, len(text.encode("utf-8"))):
                self.fd_streams[name].append(text)
        return captured

    def record_process(self, destination, raw):
        """Retain a child pipe only when the full destination fits its byte allowance."""
        text = raw.decode("utf-8")
        if self.reserve(destination, len(raw)):
            self.process_streams.append((destination, text))
        return text

    def watch_files(self, env):
        """Register destinations from an in-process or child train environment."""
        self.files.update((name, Path(env[name])) for name in REPORT_FILES if env.get(name))
        return env

    def snapshot_files(self, files):
        """Validate bounded binary snapshots without keeping copies or translating newlines."""
        for name, path in sorted(files):
            if not path.exists():
                continue
            destination = f"{name} ({path})"
            with path.open("rb") as handle:
                raw = handle.read(LINE_SHAPE_MAX_BYTES + 1)
                size = len(raw)
                if size > LINE_SHAPE_MAX_BYTES:
                    size = max(size, os.fstat(handle.fileno()).st_size)
            if self.reserve(destination, size, replace=True):
                try:
                    check_line_shape(destination, raw.decode("utf-8"))
                except LineShapeError as error:
                    if self.file_error is None:
                        self.file_error = LineShapeError(str(error))

    def check(self):
        """Check descriptors, process pipes, logs and registered files after the body.

        The net checks each line per destination and cannot attribute individually valid
        lines to one record. Record-level flattening is tested at write_records,
        annotate and note_cell. Further harness forms are tracked in issue 269.
        """
        self.read_fd()
        self.snapshot_files(self.files)
        self.files.clear()
        streams, self.process_streams = self.process_streams, []
        descriptors = self.fd_streams
        self.fd_streams = {"fd stdout": [], "fd stderr": []}
        logs = self.caplog.get_records("call")
        self.caplog.clear()
        self.reserve(
            "logging stderr", sum(len(record.getMessage().encode("utf-8")) for record in logs)
        )
        if self.overflow is not None:
            raise self.overflow
        for name, chunks in descriptors.items():
            check_line_shape(name, "".join(chunks))
        for name, text in streams:
            external = (None, name) in LINE_SHAPE_EXCEPTIONS or (
                self.test_name,
                name,
            ) in LINE_SHAPE_EXCEPTIONS
            check_line_shape(name, text, allow_external=external)
        if self.file_error is not None:
            raise self.file_error
        for number, record in enumerate(logs, 1):
            if (RECORD_CONTROLS | {"\n"}).intersection(record.getMessage()):
                raise LineShapeError(
                    f"logging stderr:{number}: multiline log: {record.getMessage()!r}"
                )


ACTIVE_LINE_SHAPE: ContextVar[LineShapeCapture] = ContextVar("merge_train_line_shape")


@pytest.fixture(autouse=True)
def line_shape(capfd, caplog, request):
    """Check every train test's fd streams, child pipes and named report destinations.

    The net checks each line per destination and cannot attribute individually valid
    lines to one record. Record-level flattening is tested at write_records,
    annotate and note_cell. Further harness forms are tracked in issue 269.
    """
    capture = LineShapeCapture(capfd.readouterr, caplog, request.node.name)
    token = ACTIVE_LINE_SHAPE.set(capture)
    # Fixture plumbing is separate from patches tracked by the app-key build guard.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(capfd, "readouterr", capture.read_fd)
        try:
            yield capture
        finally:
            try:
                capture.check()
            finally:
                ACTIVE_LINE_SHAPE.reset(token)


def watch_report_files(env):
    """Register the train's output paths before it can write to them."""
    return ACTIVE_LINE_SHAPE.get().watch_files(env)


def run_train_process(command, *, descriptor, env, timeout, cwd=None):
    """Capture child output byte-for-byte and retain both pipes for the line-shape check."""
    done = subprocess.run(
        command, env=env, capture_output=True, timeout=timeout, check=False, cwd=cwd
    )
    capture = ACTIVE_LINE_SHAPE.get()
    done.stdout = capture.record_process(f"{descriptor} stdout", done.stdout)
    done.stderr = capture.record_process(f"{descriptor} stderr", done.stderr)
    capture.snapshot_files((name, Path(env[name])) for name in REPORT_FILES if env.get(name))
    return done


# The step's environment apart from the forge address, the paths and the app key.
TRAIN_DEFAULTS = {
    "PATH": "/usr/bin:/bin",
    "TRAIN_APP_CLIENT_ID": CLIENT_ID,
    "TRAIN_SWITCH": "on",
    "TRAIN_MERGE_METHOD": "SQUASH",
    "TRAIN_ACCEPT_STATES": "",
    "TRAIN_MIN_AGE_DAYS": "3",
    "TRAIN_HOLD_LABELS": "hold,do-not-merge,planted",
    "TRAIN_DEPENDENCY_BRANCHES": "renovate/ dependabot/",
    "TRAIN_REGENERATE_LABEL": "rebase",
    "TRAIN_ONBOARDING_BRANCHES": "renovate/configure",
    "TRAIN_PERSON_BRANCHES": "release-please--",
    "TRAIN_RELEASE_OFF": "",
    "TRAIN_SECURITY_LABELS": "security",
    "TRAIN_REQUIRED_STATUS": "",
    "TRAIN_SESSION_AUTHORS": "",
    "TRAIN_SESSION_HOLD_HOURS": "4",
}


@contextlib.contextmanager
def train_environment(tmp_path, forge, settings):
    """Yield the train step's environment with the stand-in forge running."""
    forge.workdir = tmp_path
    private = app_key()[0]
    server = serve(forge)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        env = measured(
            TRAIN_DEFAULTS
            | {
                "GITHUB_API_URL": url,
                "GITHUB_GRAPHQL_URL": f"{url}/graphql",
                "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                "GITHUB_OUTPUT": str(tmp_path / "output"),
                "RUNNER_TEMP": str(tmp_path),
                "TRAIN_APP_KEY": private,
                **{f"TRAIN_{key.upper()}": value for key, value in settings.items()},
            }
        )
        yield watch_report_files(env)
    finally:
        server.shutdown()
        server.server_close()


def train(
    tmp_path: Path,
    forge: Forge,
    text: str | None = None,
    helper: str | None = None,
    *,
    optimize: bool = False,
    **settings: str,
):
    """Run the step's script against the stand-in forge; the result and the summary written."""
    (tmp_path / "train_rules.py").write_text(helper or module(), encoding="utf-8")
    flags = ["-I", "-B", *(["-O"] if optimize else [])]
    if text is None:
        # The genuine script runs from tools/, so coverage counts it; LAUNCH zeroes the waits.
        command = [sys.executable, *flags, "-c", LAUNCH, str(SCRIPT)]
    else:
        source = text.replace("SETTLE_SECONDS = 5", "SETTLE_SECONDS = 0")
        source = source.replace("READ_BACKOFF_SECONDS = 5", "READ_BACKOFF_SECONDS = 0")
        path = tmp_path / "train.py"
        path.write_text(source, encoding="utf-8")
        shutil.copyfile(ROOT / "tools/review_state.py", tmp_path / "review_state.py")
        shutil.copyfile(ROOT / "tools/release_workflows.py", tmp_path / "release_workflows.py")
        command = [sys.executable, *flags, str(path)]
    with train_environment(tmp_path, forge, settings) as env:
        Path(env["GITHUB_OUTPUT"]).unlink(missing_ok=True)  # Each sweep starts a new step.
        done = run_train_process(command, descriptor="train", env=env, timeout=120)
    summary = tmp_path / "summary.md"
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


def dequeued(number: int) -> tuple:
    return ("dequeue", f"PR_{number}")


def queued(number: int) -> tuple:
    return ("enqueue", f"PR_{number}", f"{number:040x}")


def updated(number: int) -> tuple:
    return ("update", number, f"{number:040x}")


def ctx(name, state):
    return {"contexts": [{"context": name, "state": state}]}


CHECKOUT = {
    "repository": "${{ job.workflow_repository }}",
    "ref": "${{ job.workflow_sha }}",
    "path": "merge-train-source",
    "persist-credentials": False,
    "sparse-checkout": (
        "tools/merge_train.py\ntools/review_state.py\ntools/release_workflows.py\n"
        "tools/release_labels.py\ntools/release_plan.py\n"
    ),
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


def file_modes_document(train_mod, monkeypatch):
    """Capture the document generated for a retry diff with one surviving file."""
    generated = []

    def read_file_modes(_env, query, _variables):
        """Capture the document and return equal file modes."""
        generated.append(query)
        commit = {"f0": {"mode": 33188}}
        return {"repository": {"before": commit, "after": commit}}

    with monkeypatch.context() as patch:
        patch.setattr(train_mod, "own_diff", lambda *_: [("path", "patch", "modified")])
        patch.setattr(train_mod, "read_graphql", read_file_modes)
        assert train_mod.same_retry_change({"GITHUB_REPOSITORY": "fleet/app"}, "base", "old", "new")
    (document,) = generated
    return document


def train_graphql_documents(train_mod, rules, generated):
    """Collect the train's static, rules-module and generated GraphQL documents."""
    constants = [
        doc
        for key, doc in vars(train_mod).items()
        if key.isupper() and isinstance(doc, str) and re.match(r"\s*(query\b|mutation\b|\{)", doc)
    ]
    return [*constants, rules.QUERY, rules.STATUS_QUERY, generated]


def test_train_graphql_documents_have_unique_names(tmp_path, monkeypatch):
    train_mod = load_train(tmp_path, monkeypatch)
    rules = load_rules(tmp_path)
    generated = file_modes_document(train_mod, monkeypatch)
    assert train_mod.graphql_name(generated) == "FileModes"
    documents = functools.partial(train_graphql_documents, train_mod, rules, generated)

    def check(docs):
        names = []
        for doc in docs:
            match = re.match(r"\s*(?:query|mutation)\s+([A-Z][A-Za-z0-9]*)\s*[(\{]", doc)
            assert match, "missing PascalCase operation name"
            assert graphql_operation(doc) == match[1]
            names.append(match[1])
        assert len(set(names)) == len(names), "duplicate operation name"

    check(documents())
    assert {
        graphql_operation(doc): bool(re.search(r"\bstatus\s*\{", doc)) for doc in documents()
    } == GRAPHQL_STATUS_SELECTION
    assert {
        graphql_operation(doc): doc.split(") {", 1)[1].split("(", 1)[0].strip()
        for doc in documents()
        if doc.lstrip().startswith("mutation")
    } == GRAPHQL_MUTATIONS
    with pytest.raises(AssertionError, match="missing"):
        check([*documents(), "query { viewer { login } }"])
    duplicate = train_mod.MERGE_STATE.replace("query MergeState(", "query FileModes(", 1)
    monkeypatch.setattr(train_mod, "MERGE_STATE", duplicate)
    with pytest.raises(AssertionError, match="duplicate"):
        check(documents())


def test_forge_operation_key_comes_from_tokens(monkeypatch):
    """Read declaration tokens with whitespace and delimiters."""

    def reject_match(*args, **kwargs):
        """Fail if operation dispatch matches a declaration pattern."""
        pytest.fail("operation dispatch attempted a declaration match")

    with monkeypatch.context() as patch:
        patch.setattr(re, "match", reject_match)
        assert graphql_operation(" \r\nquery   OpenPulls($limit: Int) { viewer { login } }") == (
            "OpenPulls"
        )
        assert graphql_operation("mutation Land{ mergePullRequest { clientMutationId } }") == (
            "Land"
        )
        assert graphql_operation("query { viewer { login } }") is None


def post_forge_graphql(forge, query, variables):
    """Send a GraphQL document through the real stand-in HTTP handler."""
    server = serve(forge)
    try:
        host, port = server.server_address
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as conn:
            conn.request(
                "POST",
                "/graphql",
                json.dumps({"query": query, "variables": variables}),
                {"Authorization": "Bearer test-token"},
            )
            response = conn.getresponse()
            body = json.load(response)
    finally:
        server.shutdown()
        server.server_close()

    return response.status, body


def test_forge_dispatch_uses_declared_operation_over_comment():
    """A real POST keys the fault ledger and serves the selection after a misleading comment."""
    forge = Forge(repository())
    forge.tokens["test-token"] = ("repo", 7, "fleet/app", READ)
    forge.merge_states[1] = "CLEAN"
    query = (
        "# query Other { status { state } }\nquery MergeState { pullRequest { mergeStateStatus } }"
    )
    variables = {"owner": "fleet", "name": "app", "number": 1}
    status, body = post_forge_graphql(forge, query, variables)

    assert forge.request_ledger == {("POST", "/graphql [MergeState]", "direct")}
    assert status == 200, body
    assert body == {"data": {"repository": {"pullRequest": {"mergeStateStatus": "CLEAN"}}}}


@pytest.mark.parametrize("operation", ["Arm", "Merge"])
@pytest.mark.parametrize("argument", ["body", "headline"])
@pytest.mark.parametrize("shape", ["comment", "string", "block-string", "nested", "present"])
def test_forge_records_only_mutation_message_arguments(operation, argument, shape):
    """A real mutation POST recognizes only variable arguments on the top-level field."""
    forge = Forge(repository())
    forge.tokens["test-token"] = ("repo", 7, "fleet/app", WRITE)
    name = f"commit{argument.capitalize()}"
    binding = f"{name}: ${argument}"
    planted = {
        "comment": f"# {binding}\n",
        "string": f'clientMutationId: "quoted \\" {binding}"',
        "block-string": f'clientMutationId: """block \\""" {binding}\nignored"""',
        "nested": f"extra: {{ {binding} }}",
        "present": binding,
    }[shape]
    variables = {"id": "PR_1", "oid": "head", "method": "SQUASH", argument: "planted message"}
    field = GRAPHQL_MUTATIONS[operation]
    declaration = f", ${argument}: String" if shape in {"nested", "present"} else ""
    query = (
        f"mutation {operation}($id: ID!, $oid: GitObjectID!, "
        f"$method: PullRequestMergeMethod!{declaration}) {{ "
        f"{field}(input: {{pullRequestId: $id, expectedHeadOid: $oid, "
        f"mergeMethod: $method, {planted}}}) "
        "{ clientMutationId } }"
    )
    status, result = post_forge_graphql(forge, query, variables)
    assert status == 200 and field in result["data"], result
    value = variables[argument] if shape == "present" else None
    headline, body = (value, None) if argument == "headline" else (None, value)
    assert forge.messages == [(operation.lower(), "PR_1", headline, body)]


def test_anonymous_graphql_document_raises_document_error(tmp_path, monkeypatch):
    train_mod = load_train(tmp_path, monkeypatch)
    assert train_mod.graphql_name("query PullPage($n: Int!) { viewer { login } }") == "PullPage"
    with pytest.raises(train_mod.GraphQLDocumentError, match="anonymous GraphQL operation"):
        train_mod.graphql_name("query { viewer { login } }")


def test_forge_refuses_anonymous_graphql():
    forge = Forge(repository())
    assert forge.graphql_response("", "query { viewer { login } }", {}) == (
        400,
        {"message": "stand-in forge: anonymous operation"},
    )


def test_clean_pull_request_on_second_page_lands(tmp_path):
    forge = Forge(repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31))))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    assert [v.get("after") for _, v in forge.read_calls] == [None, "cursor-25"]


@pytest.mark.parametrize("status", [502, 504])
def test_transient_read_error_is_retried(tmp_path, status):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_statuses["fleet/app"] = [status] * 3
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert [v["first"] for _, v in forge.read_calls] == [25, 12, 6, 6]


@pytest.mark.parametrize("split", [True, False], ids=["split", "split-removed"])
def test_oversized_pull_page_times_out(tmp_path, split):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.pull_page_timeout_above = 12
    flow = None
    if not split:
        source = script()
        split_guard = "if size <= PULL_PAGE_MIN or not transient(err):"
        assert source.count(split_guard) == 1
        flow = source.replace(split_guard, "if True:")
        assert flow != source
    done, summary = train(tmp_path, forge, flow)
    sizes = [v["first"] for _, v in forge.read_calls]
    if split:
        assert done.returncode == 0, done.stdout + done.stderr
        assert sizes == [25, 12]
        assert forge.writes == [landed(1)]
        section = summary.split("### fleet/app: landed #1")[1]
        assert re.search(
            r"Split open pull request page to 12 after graphql PullPage: .*HTTP 504", section
        )
        assert "stand-in forge: pull page timed out" in section
        assert "::error" not in done.stdout
    else:
        assert done.returncode == 1, done.stdout + done.stderr
        assert sizes == [25]
        assert forge.writes == []
        assert re.search(
            r"::error title=Merge train refused::fleet/app: graphql PullPage: .*HTTP 504",
            done.stdout,
        )


MULTILINE_READ_ERROR = b"stand-in forge: first error line\r\nstand-in forge: second error line"
FLATTENED_READ_ERROR = " ".join(MULTILINE_READ_ERROR.decode().splitlines())


def pull_refusal_line(stdout, summary):
    """Read the pull refusal and require its summary and annotation copies to match."""
    prefix = "::error title=Merge train refused::"
    annotations = [
        line.removeprefix(prefix) for line in stdout.splitlines() if line.startswith(prefix)
    ]
    assert len(annotations) == 1
    assert [line for line in summary.splitlines() if line.startswith("- fleet/app: ")] == [
        f"- {annotations[0]}"
    ]
    return annotations[0]


@pytest.mark.parametrize("status", [502, 504])
def test_exhausted_read_retries_fail_closed(tmp_path, status):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_error_bodies[status] = MULTILINE_READ_ERROR
    if status == 504:
        forge.pull_page_timeout_above = 0
    else:
        forge.read_statuses["fleet/app"] = [status] * 6
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    refusal = pull_refusal_line(done.stdout, summary)
    assert re.match(r"fleet/app: graphql PullPage: .*page size 6, cursor start:", refusal)
    assert refusal.endswith(f"HTTP {status} {FLATTENED_READ_ERROR}")
    assert forge.writes == []
    assert [v["first"] for _, v in forge.read_calls] == [25, 12, 6, 6, 6]
    if status == 502:
        assert forge.read_statuses["fleet/app"] == [status]


def test_short_final_page_refusal_names_cursor(tmp_path):
    forge = Forge(repository(*(pull(n, "DIRTY") for n in range(1, 101))))
    forge.pull_page_timeout_above = 12
    forge.read_statuses["fleet/app"] = [200] * 8 + [504] * 3
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 1, done.stdout + done.stderr
    assert re.search(
        r"fleet/app: graphql PullPageWithStatus: .*page size 4, cursor cursor-96: .*HTTP 504",
        summary,
    )
    assert [(v["after"], v["first"]) for _, v in forge.read_calls[-3:]] == [("cursor-96", 4)] * 3
    assert forge.writes == []


@pytest.mark.parametrize("empty", [True, False], ids=["empty-nodes", "missing-cursor"])
def test_nonadvancing_pull_page_refuses(tmp_path, monkeypatch, empty):
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []

    def broken_page(_env, _query, variables):
        calls.append(variables["first"])
        page = {
            "nodes": [] if empty else [pull(1, "CLEAN")],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1" if empty else None},
        }
        return {"repository": {"pullRequests": page}}

    monkeypatch.setattr(train_mod, "graphql", broken_page)
    with pytest.raises(
        train_mod.RefusedError, match="graphql PullPage: open pull request page did not advance"
    ):
        reader = train_mod.PullPageReader(train_mod.time.monotonic())
        train_mod.fetch({"GITHUB_REPOSITORY": "fleet/app"}, {}, reader)
    assert calls == [25]


def test_spent_sweep_budget_refuses_before_the_first_pull_page(tmp_path, monkeypatch):
    """No pull page request starts once the sweep budget is spent; the refusal names the read."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(train_mod, "graphql", lambda _env, _query, variables: calls.append(1))
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: float(train_mod.SWEEP_SECONDS + 1))
    with pytest.raises(
        train_mod.RefusedError,
        match=rf"graphql PullPage: sweep budget of {train_mod.SWEEP_SECONDS} s spent "
        r"before the read \(page size 25, cursor start\)",
    ):
        train_mod.fetch({"GITHUB_REPOSITORY": "fleet/app"}, {}, train_mod.PullPageReader(0.0))
    assert calls == []


def test_split_within_sweep_budget_finishes_and_next_page_refuses(tmp_path, monkeypatch):
    """A split begun inside the budget reads its page; the page after the budget is refused."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []
    tick = 0.0

    def page(_env, _query, variables):
        nonlocal tick
        calls.append((variables["first"], variables["after"]))
        if variables["first"] == 25:
            tick = train_mod.SWEEP_SECONDS - 1
            raise train_mod.RefusedError("graphql PullPage: timed out") from TimeoutError()
        tick = train_mod.SWEEP_SECONDS + 1
        connection = {
            "nodes": [pull(1, "CLEAN")],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }
        return {"repository": {"pullRequests": connection}}

    monkeypatch.setattr(train_mod, "graphql", page)
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: tick)
    with pytest.raises(
        train_mod.RefusedError,
        match=rf"graphql PullPage: sweep budget of {train_mod.SWEEP_SECONDS} s spent "
        r"before the read \(page size 12, cursor cursor-1\)",
    ):
        train_mod.fetch({"GITHUB_REPOSITORY": "fleet/app"}, {}, train_mod.PullPageReader(0.0))
    assert calls == [(25, None), (12, None)]


@pytest.mark.parametrize("guard", [True, False], ids=["budget-guard", "guard-removed"])
def test_minimum_pull_page_does_not_retry_past_sweep_budget(tmp_path, guard):
    forge = Forge(repository(*(pull(n, "CLEAN" if n == 26 else "DIRTY") for n in range(1, 27))))
    forge.read_statuses["fleet/app"] = [200, 504, 504, 504]
    forge.read_error_bodies[504] = MULTILINE_READ_ERROR
    setup = """
from types import SimpleNamespace

tick = 0.0
request = graphql
READ_BACKOFF_SECONDS = 2

def timed_graphql(env, query, variables):
    global tick
    try:
        return request(env, query, variables)
    finally:
        if variables.get("first") == PULL_PAGE // 2:
            tick = SWEEP_SECONDS - 1

def advance_backoff(seconds):
    global tick
    assert seconds == READ_BACKOFF_SECONDS
    assert tick == SWEEP_SECONDS - 1
    tick += seconds

time = SimpleNamespace(time=time.time, monotonic=lambda: tick, sleep=advance_backoff)
graphql = timed_graphql
"""
    entry = 'if __name__ == "__main__":'
    source = script().replace(entry, setup + "\n" + entry)
    if not guard:
        check = "if started is not None and over_budget(started):"
        assert source.count(check) == 1
        source = source.replace(check, "if False:")
    done, summary = train(tmp_path, forge, source)
    calls = [(v["first"], v["after"]) for _, v in forge.read_calls]
    before_retry = [(25, None), (25, "cursor-25"), (12, "cursor-25"), (6, "cursor-25")]
    if guard:
        assert done.returncode == 1, done.stdout + done.stderr
        assert calls == before_retry
        assert forge.writes == []
        refusal = pull_refusal_line(done.stdout, summary)
        assert re.match(
            r"fleet/app: graphql PullPage: .*page size 6, cursor cursor-25: "
            r"sweep budget of 1500 s spent before the read after graphql PullPage: .*HTTP 504",
            refusal,
        )
        assert refusal.endswith(f"HTTP 504 {FLATTENED_READ_ERROR}")
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        assert calls == [*before_retry, (6, "cursor-25")]
        assert forge.writes == [landed(26)]


@pytest.mark.parametrize("keep_split", [True, False], ids=["split-note", "note-removed"])
def test_recovered_pull_page_split_is_reported_on_later_refusal(tmp_path, keep_split):
    forge = Forge(repository(*(pull(n, "DIRTY") for n in range(1, 14))))
    forge.read_statuses["fleet/app"] = [504, 200, 500]
    forge.read_error_bodies[500] = MULTILINE_READ_ERROR
    source = None
    if not keep_split:
        source = script()
        note = "            add_refusal_context(err, split_note)"
        assert source.count(note) == 1
        source = source.replace(note, "            pass")
    done, summary = train(tmp_path, forge, text=source)
    assert done.returncode == 1, done.stdout + done.stderr
    assert [(v["first"], v["after"]) for _, v in forge.read_calls] == [
        (25, None),
        (12, None),
        (12, "cursor-12"),
    ]
    assert forge.writes == []
    refusal = pull_refusal_line(done.stdout, summary)
    assert refusal.startswith("fleet/app: graphql PullPage: ")
    assert "page size 12, cursor cursor-12:" in refusal
    assert refusal.endswith(f"HTTP 500 {FLATTENED_READ_ERROR}")
    split = r"Split open pull request page to 12 after graphql PullPage: .*HTTP 504"
    assert bool(re.search(split, refusal)) is keep_split


def test_read_500_is_not_retried(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.read_statuses["fleet/app"] = [500]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert re.search(r"fleet/app: graphql PullPage: .*HTTP 500", done.stdout)
    assert forge.writes == []
    assert [v["first"] for _, v in forge.read_calls] == [25]


@pytest.mark.parametrize(
    "state, operation, name, write",
    [
        ("CLEAN", "mergePullRequest", "Merge", landed(1)),
        ("BEHIND", "enablePullRequestAutoMerge", "Arm", armed(1)),
    ],
)
def test_mutation_502_is_not_retried(tmp_path, state, operation, name, write):
    forge = Forge(repository(pull(1, state)))
    forge.mutation_statuses[operation] = [502, 502]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert re.search(rf"fleet/app: graphql {name}: .*HTTP 502", done.stdout)
    assert forge.writes == [write]
    assert forge.mutation_statuses[operation] == [502]


@pytest.mark.parametrize(
    "count, timeout_above, sizes, truncated",
    [
        (25, None, [25], False),
        (100, None, [25] * 4, False),
        (120, None, [25] * 4, True),
        (120, 12, [25] + [12] * 8 + [4], True),
    ],
)
def test_pull_pages_stop_at_cap(tmp_path, count, timeout_above, sizes, truncated):
    forge = Forge(
        repository(*(pull(n, "CLEAN" if n == 101 else "DIRTY") for n in range(1, count + 1)))
    )
    forge.pull_page_timeout_above = timeout_above
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert [v["first"] for _, v in forge.read_calls] == sizes
    assert ("Only the 100 oldest open pull requests were read." in summary) == truncated
    assert [int(n) for n in re.findall(r"^\| #(\d+) \|", summary, re.M)] == list(
        range(1, min(count, 100) + 1)
    )


def test_page_size_100_is_refused_by_forge(tmp_path):
    source = script()
    mutant = source.replace("PULL_PAGE = 25", "PULL_PAGE = 100")
    assert mutant != source
    forge = Forge(repository(pull(1, "CLEAN")))
    done, summary = train(tmp_path, forge, mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert [v["first"] for _, v in forge.read_calls] == [100, 50, 25]
    assert "HTTP 502" in summary and "read page size exceeds 25" in summary
    assert forge.writes == [landed(1)]


def test_unknown_state_rereads_all_pull_pages(tmp_path):
    first = repository(*(pull(n, "UNKNOWN" if n == 30 else "DIRTY") for n in range(1, 31)))
    second = repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31)))
    forge = Forge(first, second)
    forge.read_statuses["fleet/app"] = [502]
    forge.install(9, "other", b=[repository()])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    pages = [(v["after"], v["first"]) for full, v in forge.read_calls if full == "fleet/app"]
    assert pages == [(None, 25)] + [(None, 12), ("cursor-12", 12), ("cursor-24", 12)] * 2
    assert [v["first"] for full, v in forge.read_calls if full == "other/b"] == [25]
    assert summary.count("Split open pull request page") == 1


def test_second_page_read_retry_keeps_cursor_and_earlier_nodes(tmp_path):
    forge = Forge(repository(*(pull(n, "CLEAN" if n == 30 else "DIRTY") for n in range(1, 31))))
    forge.read_statuses["fleet/app"] = [200, 502, 200]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(30)]
    assert [(v["after"], v["first"]) for _, v in forge.read_calls] == [
        (None, 25),
        ("cursor-25", 25),
        ("cursor-25", 12),
    ]
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
    assert "fleet/app: graphql Merge: GraphQL:" in done.stdout
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
        ("required_status", "pipe|inside"),
        ("required_status", "tab\tinside"),
        ("required_status", "-lead"),
        ("required_status", "x" * 101),
        ("regenerate_label", "a,b"),
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
    assert manifest["default_permissions"] == {**WRITE, **ISSUES, **STATUSES, "metadata": "read"}


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


def test_github_token_gets_only_dispatch_permission():
    doc = ciwf.load(TRAIN)
    assert doc["permissions"] == {}
    assert doc["jobs"]["train"]["permissions"] == {"actions": "write"}


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


def load_train(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, source: Path | None = None):
    """The merge train script, imported with its rules module written to RUNNER_TEMP."""
    (tmp_path / "train_rules.py").write_text(module(), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    spec = importlib.util.spec_from_file_location("merge_train", source or SCRIPT)
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
    key = ("x" * 63 + "\n") * 100 + app_key(2048)[0]
    shrink_pipes(train_mod, monkeypatch)
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
    key = ("x" * 63 + "\n") * 100 + app_key(2048)[0]
    shrink_pipes(train_mod, monkeypatch)
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

    key = app_key(2048)[0]
    monkeypatch.setattr(train_mod.os, "pipe", mock_pipe)
    with pytest.raises(train_mod.InvalidSettingError) as exc_info:
        train_mod.key_bits(key)
    msg = str(exc_info.value)
    assert "Too many open files" in msg or "EMFILE" in msg
    assert f"at least {train_mod.MIN_KEY_BITS} bits are required" in msg


def test_app_jwt_pipe_oserror_raises_refused_error(tmp_path, monkeypatch):
    """An OSError from os.pipe in app_jwt raises RefusedError."""
    train_mod = load_train(tmp_path, monkeypatch)

    def mock_pipe():
        raise OSError(errno.EMFILE, "Too many open files")

    env = {"TRAIN_APP_KEY": app_key(2048)[0]}
    monkeypatch.setattr(train_mod.os, "pipe", mock_pipe)
    conf = {"client_id": "test-client-id"}
    with pytest.raises(train_mod.RefusedError) as exc_info:
        train_mod.app_jwt(env, conf)
    msg = str(exc_info.value)
    assert "Too many open files" in msg or "EMFILE" in msg


def test_app_jwt_pipe_oserror_on_cold_key_cache(tmp_path, monkeypatch):
    """Exercise the pipe failure when this worker has no cached app keys."""
    app_key.cache_clear()
    test_app_jwt_pipe_oserror_raises_refused_error(tmp_path, monkeypatch)


def test_key_build_guard_refused_cold(monkeypatch):
    monkeypatch.setattr(os, "pipe", os.pipe)
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        app_key(2048, "guard-cold")
    assert str(exc_info.value) == "build the test key before patching: os.pipe"


def test_key_build_guard_refused_warm(monkeypatch):
    app_key(2048)
    monkeypatch.setattr(os, "pipe", os.pipe)
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        app_key(2048)
    assert str(exc_info.value) == "build the test key before patching: os.pipe"


def test_key_build_guard_refused_openssl(monkeypatch):
    monkeypatch.setattr(os, "pipe", os.pipe)
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        openssl("version")
    assert str(exc_info.value) == "build the test key before patching: os.pipe"


def test_key_build_guard_refused_non_rsa(monkeypatch):
    monkeypatch.setattr(os, "pipe", os.pipe)
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        non_rsa_app_key("EC")
    assert str(exc_info.value) == "build the test key before patching: os.pipe"


def test_key_build_guard_refused_string_target(monkeypatch):
    monkeypatch.setattr("os.pipe", os.pipe)
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        app_key(2048)
    assert str(exc_info.value) == "build the test key before patching: os.pipe"


def test_key_build_guard_refused_delattr(monkeypatch):
    monkeypatch.delattr(errno, "EMFILE")
    with pytest.raises(KeyBuildUnderPatchError) as exc_info:
        app_key(2048)
    assert str(exc_info.value) == "build the test key before patching: errno.EMFILE"


def test_key_build_guard_refused_argument_order(monkeypatch):
    with pytest.raises(KeyBuildUnderPatchError):
        app_key(2048, (monkeypatch.setattr(os, "pipe", os.pipe), "guard-argument")[1])


def test_key_build_guard_refused_loop_back_edge(monkeypatch):
    built = []
    with pytest.raises(KeyBuildUnderPatchError):
        for bits in (2048, 3072):
            built.append(app_key(bits))
            monkeypatch.setattr(os, "pipe", os.pipe)
    assert len(built) == 1


def test_key_build_guard_allowed_single_iteration(monkeypatch):
    built = []
    for bits in (2048,):
        built.append(app_key(bits))
        monkeypatch.setattr(os, "pipe", os.pipe)
    assert len(built) == 1


def test_key_build_guard_refused_redefinition(monkeypatch):
    def setup():
        monkeypatch.setattr(os, "pipe", os.pipe)

    setup()

    def noop():
        pass

    setup = noop
    assert setup is noop
    with pytest.raises(KeyBuildUnderPatchError):
        app_key(2048)


def test_key_build_guard_allowed_call_after_redefinition(monkeypatch):
    def setup():
        monkeypatch.setattr(os, "pipe", os.pipe)

    def noop():
        pass

    assert setup is not noop
    setup = noop
    setup()
    app_key(2048)


def test_key_build_guard_refused_parameter(monkeypatch):
    def inner(build):
        build()
        return app_key(2048)

    def patch():
        monkeypatch.setattr(os, "pipe", os.pipe)

    with pytest.raises(KeyBuildUnderPatchError):
        inner(patch)


def test_key_build_guard_allowed_parameter(monkeypatch):
    def inner(build):
        build()
        return app_key(2048)

    def noop():
        pass

    assert inner(noop) == app_key(2048)


def test_key_build_guard_allowed_build_before_patch(monkeypatch):
    app_key(2048)
    monkeypatch.setattr(os, "pipe", os.pipe)


def test_key_build_guard_allowed_module_copy(tmp_path, monkeypatch):
    train_mod = load_train(tmp_path, monkeypatch)
    monkeypatch.setattr(train_mod, "app_jwt", train_mod.app_jwt)
    app_key(2048)


def test_key_build_guard_allowed_urllib_request(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", urllib.request.urlopen)
    app_key(2048)


def test_key_build_guard_allowed_own_module(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "KEY_BUILD_SAFE_MODULES", KEY_BUILD_SAFE_MODULES)
    app_key(2048)
    monkeypatch.setattr(f"{__name__}.KEY_BUILD_SAFE_MODULES", KEY_BUILD_SAFE_MODULES)
    app_key(2048)


def test_key_build_guard_allowed_after_undo(monkeypatch):
    monkeypatch.setattr(os, "pipe", os.pipe)
    monkeypatch.undo()
    app_key(2048)


def test_key_build_guard_context(monkeypatch):
    with monkeypatch.context() as inner_patch:
        inner_patch.setattr(os, "pipe", os.pipe)
        with pytest.raises(KeyBuildUnderPatchError):
            app_key(2048)
    app_key(2048)


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

    key = app_key(2048)[0]
    monkeypatch.setattr(train_mod.threading, "Thread", FailingThread)
    fds_before = len(os.listdir(FD_DIR))
    with pytest.raises(train_mod.InvalidSettingError) as exc_info:
        train_mod.key_bits(key)
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

    env = {"TRAIN_APP_KEY": app_key(2048)[0]}
    monkeypatch.setattr(train_mod.threading, "Thread", FailingThread)
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


def run_app_jwt(train_mod, key: str):
    """Sign a token with the supplied key and return it."""
    env = {"TRAIN_APP_KEY": key}
    conf = {"client_id": "test-client-id"}
    return train_mod.app_jwt(env, conf)


def test_key_bits_uses_openssl_path_from_shutil_which(tmp_path, monkeypatch):
    """key_bits runs the openssl that shutil.which found."""
    train_mod = load_train(tmp_path, monkeypatch)
    marker = tmp_path / "marker_key_bits"
    key = app_key(2048)[0]
    patch_openssl_which(tmp_path, monkeypatch, train_mod, marker)
    assert train_mod.key_bits(key) == 2048
    assert marker.is_file()


def test_app_jwt_uses_openssl_path_from_shutil_which(tmp_path, monkeypatch):
    """app_jwt runs the openssl that shutil.which found."""
    train_mod = load_train(tmp_path, monkeypatch)
    marker = tmp_path / "marker_app_jwt"
    key = app_key(2048)[0]
    patch_openssl_which(tmp_path, monkeypatch, train_mod, marker)
    assert len(run_app_jwt(train_mod, key).split(".")) == 3
    assert marker.is_file()


def test_key_bits_passes_literal_openssl_and_executable_path(tmp_path, monkeypatch):
    """key_bits passes the literal "openssl" first and the which path as executable."""
    train_mod = load_train(tmp_path, monkeypatch)
    key = app_key(2048)[0]
    which_path, calls = patch_openssl_which(tmp_path, monkeypatch, train_mod, tmp_path / "m")
    assert train_mod.key_bits(key) == 2048
    assert [(cmd[0], executable) for cmd, executable in calls] == [("openssl", which_path)]


def test_app_jwt_passes_literal_openssl_and_executable_path(tmp_path, monkeypatch):
    """app_jwt passes the literal "openssl" first and the which path as executable."""
    train_mod = load_train(tmp_path, monkeypatch)
    key = app_key(2048)[0]
    which_path, calls = patch_openssl_which(tmp_path, monkeypatch, train_mod, tmp_path / "m")
    assert len(run_app_jwt(train_mod, key).split(".")) == 3
    assert [(cmd[0], executable) for cmd, executable in calls] == [("openssl", which_path)]


def test_key_pipe_joins_the_writer_before_returning(tmp_path, monkeypatch):
    """key_pipe waits for the background writer thread to terminate before returning."""
    train_mod = load_train(tmp_path, monkeypatch)
    script = tmp_path / "failing_openssl.sh"
    script.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    key = app_key(2048)[0]
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
    assert train_mod.key_bits(key) is None
    assert event.is_set()


def plain_open(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"


def allowed_report_opens(text):
    """Pin the only file open and its append-versus-replace modes to the report sink."""
    tree = ast.parse(text)
    sink = top_level_functions(tree).get("write_records")
    expected = ast.parse('open(path, mode, encoding="utf-8")', mode="eval").body
    sites = [(scope, ast.dump(node)) for scope, node in scoped_nodes(tree) if plain_open(node)]
    if sites != [(sink, ast.dump(expected))]:
        return False
    mode = ast.dump(ast.parse('mode = "w" if replace else "a"').body[0])
    return any(ast.dump(node) == mode for node in ast.walk(sink))


def key_on_disk(text: str) -> list[str]:
    """Ways the script could put the key in a file; empty when the key goes through a pipe only."""
    found = [word for word in ("tempfile", "write_text", "write_bytes") if word in text]
    if not allowed_report_opens(text):
        found.append("open() outside the summary and release file writers")
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
    assert key_on_disk(mutant) == ["open() outside the summary and release file writers"]


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
    mutant = script().replace('target["full_name"].casefold() in conf["release_off"]', "False")
    assert mutant != script()
    done, _ = train(tmp_path, release_forge(), mutant, release_off="other/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert {"owner": "other", "name": "d"} in releases_of(tmp_path)


@pytest.mark.parametrize("entry", ["former/d", "other/old-name"])
@pytest.mark.parametrize("location", ["/repositories/9002", "/repos/other/d"])
def test_release_off_follows_a_moved_repository(tmp_path, entry, location):
    forge = release_forge()
    forge.repository_moves[f"/repos/{entry}"] = location
    done, summary = train(tmp_path, forge, release_off=entry)
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release train off: other/d" in summary
    assert (9, f"/repos/{entry}") in forge.repository_calls
    assert (9, location) in forge.repository_calls
    assert len(forge.revoked) == len(forge.minted)


@pytest.mark.parametrize(
    ("location", "path"),
    [
        ("{api}/repositories/9002", "/repositories/9002"),
        ("{api}/repos/other/d", "/repos/other/d"),
    ],
)
def test_release_off_follows_an_absolute_same_origin_redirect(tmp_path, location, path):
    forge = release_forge()
    forge.repository_moves["/repos/former/d"] = location
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release train off: other/d" in summary
    assert (9, "/repos/former/d") in forge.repository_calls
    assert (9, path) in forge.repository_calls
    assert len(forge.revoked) == len(forge.minted)


def queue_faults_after_location(forge, key, faults):
    """Arm a direct-route queue after the old name has issued its Location."""
    repository_read = forge.repository_read

    def queue_direct_fault(header, path):
        """Queue a fault once the stand-in responds with a repository Location."""
        response = repository_read(header, path)
        if response[0] == 301:
            forge.rest_faults[key] = faults
        return response

    forge.repository_read = queue_direct_fault


def test_release_off_redirect_then_direct_lookup_at_same_url(tmp_path):
    """The issued Location stages one lookup, and the later entry uses the direct fault."""
    forge = release_fault_forge("/repos/other/app")
    redirected = [404]  # A missing moved repository lets release_off process the next entry.
    direct = [403]
    forge.rest_faults[fault_request_key(RELEASE_RENAMED_CASE)] = redirected
    queue_faults_after_location(forge, fault_request_key(RELEASE_NAME_ROUTE), direct)

    done, summary = train(tmp_path, forge, release_off="former/app other/app")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not redirected and not direct
    assert forge.fault_requests == [("GET", "/repos/other/app")] * 2
    assert "release_off entry other/app: HTTP 403" in summary
    assert "release_off entry former/app:" not in summary
    assert releases_of(tmp_path) == []


def test_release_off_rejected_redirect_cannot_stage_another_installation(tmp_path):
    """A rejected second Location cannot consume another installation's direct fault."""
    forge = release_forge()
    forge.repository_moves.update(
        {
            "/repos/former/d": "{api}/repos/other/old-name",
            "/repos/other/old-name": "{api}/repos/fleet/app",
        }
    )
    direct, redirected = [403], [418]
    repository_read = forge.repository_read

    def queue_faults(header, path):
        """Arm both staged queues only after installation 7 reaches the second Location."""
        response = repository_read(header, path)
        if (header, path) == ("Bearer list-token-7", "/repos/other/old-name"):
            forge.rest_faults[fault_request_key(RELEASE_NAME_ROUTE)] = direct
            forge.rest_faults[fault_request_key(RELEASE_RENAMED_CASE)] = redirected
        return response

    forge.repository_read = queue_faults
    done, summary = train(tmp_path, forge, release_off="fleet/app former/d")

    assert done.returncode == 0, done.stdout + done.stderr
    assert (direct, redirected) == ([], [418])
    assert forge.fault_requests == [("GET", "/repos/fleet/app")]
    assert (
        summary.splitlines().count(
            "::warning title=Merge train::release_off entry former/d: second repository redirect; "
            "release train off for installation 7"
        )
        == 1
    )
    assert (
        summary.splitlines().count(
            "::warning title=Merge train::release_off entry fleet/app: HTTP 403; "
            "release train off for installation 9"
        )
        == 1
    )
    assert "Release train off: fleet/app, other/d" in summary.splitlines()
    assert releases_of(tmp_path) == []


@pytest.mark.parametrize("stage", ["direct", "redirect"])
def test_release_off_rejected_redirect_cannot_stage_revoke(tmp_path, stage):
    """A rejected GET Location cannot move a DELETE into the redirect fault queue."""
    forge = release_fault_forge("/installation/token")
    forge.installations[0]["repos"].clear()
    direct_key = fault_request_key(REVOKE_ROUTE)
    faults = [403]
    queue_faults_after_location(forge, (*direct_key[:2], stage), faults)

    done, summary = train(tmp_path, forge, release_off="former/app")

    expect_fault = stage == "direct"
    assert done.returncode == 0, done.stdout + done.stderr
    assert faults == ([] if expect_fault else [403])
    assert forge.fault_requests == ([direct_key[:2]] if expect_fault else [])
    assert forge.revoked == ([] if expect_fault else ["list-token-7"])
    assert direct_key in forge.request_ledger
    assert (*direct_key[:2], "redirect") not in forge.request_ledger
    assert "release_off entry former/app: unsafe repository redirect" in summary
    warnings = [line for line in done.stdout.splitlines() if "token not revoked" in line]
    if expect_fault:
        (warning,) = warnings
        assert re.search(r"DELETE \S+/installation/token: HTTP 403\b", warning)
    else:
        assert warnings == []


@pytest.mark.parametrize(
    ("fragment", "next_token", "stage"),
    [
        pytest.param("#moved", "one", "redirect", id="fragment"),
        pytest.param("", "two", "direct", id="different-token"),
    ],
)
def test_forge_redirect_stage_matches_next_request(fragment, next_token, stage):
    """A same-origin follow-up drops its fragment and must retain the issuing Authorization."""
    forge = Forge(repository())
    forge.tokens.update(one=("list", 7, None), two=("list", 7, None))
    forge.repository_moves["/repos/former/app"] = f"/repos/fleet/app{fragment}"
    server = serve(forge)
    try:
        host, port = server.server_address
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as conn:
            conn.request("GET", "/repos/former/app", headers={"Authorization": "Bearer one"})
            response = conn.getresponse()
            assert response.status == 301
            assert response.getheader("Location") == f"/repos/fleet/app{fragment}"
            response.read()
            conn.request(
                "GET", "/repos/fleet/app", headers={"Authorization": f"Bearer {next_token}"}
            )
            assert conn.getresponse().read()
    finally:
        server.shutdown()
        server.server_close()

    method, route, _ = fault_request_key(RELEASE_NAME_ROUTE)
    assert forge.request_ledger == {
        fault_request_key(RELEASE_NAME_ROUTE),
        (method, route, stage),
    }


def test_forge_refuses_second_location(tmp_path, monkeypatch):
    """Reject an ambiguous repository redirect before either destination can be staged."""
    forge = release_fault_forge()
    queued = [418]
    forge.rest_faults[fault_request_key(RELEASE_ID_CASE)] = queued

    class DuplicateLocationHandler(Handler):
        """Send two Locations in the same answer to the old-name lookup."""

        def _get_repository(self, path):
            """Plant two redirect destinations on one repository lookup."""
            if path != "/repos/former/app":
                return super()._get_repository(path)
            self.send_response(301)
            self.send_header("Location", "/repositories/7000")
            self.send_header("location", "/repositories/8000")
            self.end_headers()

    monkeypatch.setattr(sys.modules[__name__], "Handler", DuplicateLocationHandler)
    refusal = (
        "GET /repos/former/app: second Location '/repositories/8000' after '/repositories/7000'"
    )
    with pytest.raises(AssertionError, match=re.escape(refusal)):
        done, _ = train(tmp_path, forge, release_off="former/app")
        pytest.fail(
            f"ambiguous response accepted: exit {done.returncode}, "
            f"lookups {forge.repository_calls}, redirect faults left {queued}"
        )
    assert forge.redirect_location is None
    assert queued == [418]
    assert all(path != "/repositories/7000" for _, path in forge.repository_calls)


def test_forge_raising_dispatch_discards_pending_redirect():
    """A request rejected by route normalization cannot leave a redirect for a later GET."""
    forge = release_fault_forge()
    forge.tokens["one"] = ("list", 7, None)
    key = fault_request_key(RELEASE_ID_CASE)
    queued = [418]
    forge.rest_faults[key] = queued
    server = serve(forge)
    host, port = server.server_address
    refusal = (
        f"GET *: pending redirect from {RELEASE_NAME_ROUTE} to http://{host}:{port}"
        "/repositories/7000 refused: unresolved REST URL: *"
    )

    reported = None
    try:
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as conn:
            headers = {"Authorization": "Bearer one"}
            conn.request("GET", "/repos/former/app", headers=headers)
            response = conn.getresponse()
            assert response.status == 301
            assert response.getheader("Location") == "/repositories/7000"
            response.read()
            conn.request("GET", "*", headers=headers)
            with pytest.raises(http.client.RemoteDisconnected):
                conn.getresponse()
            conn.request("GET", "/repositories/7000", headers=headers)
            response = conn.getresponse()
            response.read()
    finally:
        server.shutdown()
        try:
            server.server_close()
        except AssertionError as error:
            reported = str(error)

    assert (response.status, queued) == (200, [418]), (
        f"{key}: expected an unconsumed redirect fault after GET *, "
        f"got HTTP {response.status} and queue {queued}"
    )
    assert (*key[:2], "direct") in forge.request_ledger
    assert key not in forge.request_ledger
    assert reported == refusal
    with pytest.raises(
        AssertionError, match=re.escape(f"{RELEASE_ID_CASE}: consumed 0 of 1 faults")
    ):
        assert_faults_consumed(RELEASE_ID_CASE, queued, (), len(forge.fault_requests), 1)


def test_forge_rejects_overlapping_dispatch():
    """A second dispatch fails by route while the first request is still being served."""
    forge = Forge(repository())
    first, second = object.__new__(Handler), object.__new__(Handler)
    first.server = second.server = types.SimpleNamespace(forge=forge)
    first.command = second.command = "GET"
    first.path, second.path = "/app", "/app/installations"
    first.headers = second.headers = {"Host": "127.0.0.1"}

    def overlap():
        """Arrive during the first request's dispatch callback."""
        second.dispatch(serve_second)

    def serve_second():
        """Finish the nested request if the serial guard is missing."""

    with pytest.raises(
        AssertionError, match="concurrent stand-in forge request: GET /app/installations"
    ):
        first.dispatch(overlap)


def test_forge_accepts_request_after_response_sent(monkeypatch):
    """A new connection can follow a complete response while its handler is still serving."""
    forge = Forge(repository())
    second_sent, first_finished = threading.Event(), threading.Event()

    def serve_get(self):
        """Pause the first request after its wire response and check the serialized handoff."""
        if self.path == "/app/installations":
            assert first_finished.is_set()
        self.answer(200, {"path": self.path})
        if self.path == "/app":
            assert second_sent.wait(5)
            time.sleep(0.2)
            first_finished.set()

    monkeypatch.setattr(Handler, "_serve_get", serve_get)
    server = serve(forge)
    host, port = server.server_address
    try:
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as first:
            first.request("GET", "/app")
            assert first.getresponse().read() == b'{"path": "/app"}'
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as second:
            second.request("GET", "/app/installations")
            second_sent.set()
            assert second.getresponse().read() == b'{"path": "/app/installations"}'
    finally:
        second_sent.set()
        server.shutdown()
        server.server_close()


def test_forge_rejects_request_before_response_sent(monkeypatch):
    """A second connection is refused by name while the first answer is still pending."""
    forge = Forge(repository())
    awaiting_answer, release_answer = threading.Event(), threading.Event()

    def serve_get(self):
        """Keep the first response unsent until the client has checked the second connection."""
        if self.path == "/app":
            awaiting_answer.set()
            assert release_answer.wait(10)
        self.answer(200, {"path": self.path})

    monkeypatch.setattr(Handler, "_serve_get", serve_get)
    server = serve(forge)
    host, port = server.server_address
    refusal = "concurrent stand-in forge request: GET /app/installations"
    reported = None
    try:
        with contextlib.closing(http.client.HTTPConnection(host, port, timeout=5)) as first:
            first.request("GET", "/app")
            try:
                assert awaiting_answer.wait(5)
                with contextlib.closing(
                    http.client.HTTPConnection(host, port, timeout=5)
                ) as second:
                    second.request("GET", "/app/installations")
                    with pytest.raises(http.client.RemoteDisconnected):
                        second.getresponse()
            finally:
                release_answer.set()
                assert first.getresponse().read() == b'{"path": "/app"}'
    finally:
        server.shutdown()
        try:
            server.server_close()
        except ForgeRequestRefusalError as error:
            reported = str(error)
    assert reported == refusal


def test_release_off_exact_match_needs_no_repository_lookup(tmp_path):
    forge = Forge(repository(release=(True, True)))
    done, summary = train(tmp_path, forge, release_off="Fleet/App")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == []
    assert "Release train off: fleet/app" in summary
    assert forge.repository_calls == []


@pytest.mark.parametrize("redirect", [False, True])
def test_release_off_missing_repository_matches_nothing(tmp_path, redirect):
    forge = release_forge()
    if redirect:
        forge.repository_moves["/repos/former/d"] = "/repositories/99999"
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [
        {"owner": "fleet", "name": "app"},
        {"owner": "other", "name": "d"},
    ]
    assert "Release train off:" not in summary
    assert "::warning" not in summary


@pytest.mark.parametrize("status", [403, 500, 502, 503, 504])
@pytest.mark.parametrize("path", ["/repos/former/d", "/repositories/9002"])
def test_release_off_lookup_failure_closes_only_its_installation(tmp_path, status, path):
    forge = release_forge()
    forge.repository_moves["/repos/former/d"] = "/repositories/9002"
    forge.repository_statuses[(9, path)] = status
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release train off: other/d" in summary
    assert "::warning" in summary
    assert f"release_off entry former/d: HTTP {status}" in summary
    assert "installation 9" in summary
    assert "planted response body must stay private" not in done.stdout + done.stderr + summary
    assert forge.repository_calls.count((9, path)) == (3 if status in (502, 503, 504) else 1)
    assert len(forge.revoked) == len(forge.minted)


@pytest.mark.parametrize(
    "location",
    [
        "http://localhost:{port}/repositories/9002",
        "{api}/user",
        "{api}/repositories/9002?extra=1",
        "{api}/repos/other/d#fragment",
        "{api}/repos/other/d/extra",
        "{api}/repositories/not-a-number",
    ],
    ids=["other-host", "user", "query", "fragment", "extra-path", "non-numeric-id"],
)
def test_release_off_refuses_unsafe_repository_redirect(tmp_path, location):
    forge = release_forge()
    forge.repository_moves["/repos/former/d"] = location
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == []
    assert "Release train off: fleet/app, other/d" in summary
    assert "release_off entry former/d: unsafe repository redirect" in summary
    assert forge.repository_calls == [(7, "/repos/former/d"), (9, "/repos/former/d")]


def test_release_off_refuses_a_second_repository_redirect(tmp_path):
    forge = release_forge()
    forge.repository_moves["/repos/former/d"] = "/repos/other/old-name"
    forge.repository_moves["/repos/other/old-name"] = "/repositories/9002"
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == []
    assert "release_off entry former/d: second repository redirect" in summary
    assert forge.repository_calls == [
        (7, "/repos/former/d"),
        (7, "/repos/other/old-name"),
        (9, "/repos/former/d"),
        (9, "/repos/other/old-name"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id": "9002"},
        {"id": True},
        [],
        b"{bad JSON",
        pytest.param(None, id="empty"),
        pytest.param(b"null", id="null"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
@pytest.mark.parametrize("redirect", [False, True])
def test_release_off_refuses_invalid_repository_metadata(tmp_path, body, redirect):
    forge = release_forge()
    path = "/repositories/9002" if redirect else "/repos/former/d"
    if redirect:
        forge.repository_moves["/repos/former/d"] = path
    forge.repository_bodies[path] = body
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == []
    assert "release_off entry former/d:" in summary
    assert "invalid repository metadata" in summary
    assert "bad JSON" not in done.stdout + done.stderr + summary


def test_release_off_repository_id_does_not_match_another_owner(tmp_path):
    forge = release_forge()
    forge.install(11, "former", d=[repository(release=(True, True))])
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [
        {"owner": "fleet", "name": "app"},
        {"owner": "other", "name": "d"},
    ]
    assert "Release train off: former/d" in summary


def test_release_off_public_repository_outside_installations_matches_nothing(tmp_path):
    forge = release_forge()
    forge.repository_bodies["/repos/former/d"] = {"id": 99999, "full_name": "other/d"}
    done, summary = train(tmp_path, forge, release_off="former/d")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [
        {"owner": "fleet", "name": "app"},
        {"owner": "other", "name": "d"},
    ]
    assert "Release train off:" not in summary


def test_release_off_resolves_repository_metadata_without_a_redirect(tmp_path):
    forge = release_forge()
    forge.repository_bodies["/repos/former/d"] = {
        "id": 9002,
        "full_name": "other/d",
        "temp_clone_token": "planted response body must stay private",
    }
    done, summary = train(tmp_path, forge, release_off="Former/D")
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert "Release train off: other/d" in summary
    assert "planted response body must stay private" not in done.stdout + done.stderr + summary
    assert forge.repository_calls == [(7, "/repos/former/d"), (9, "/repos/former/d")]


def test_release_off_bounds_repository_lookups_per_installation(tmp_path):
    forge = Forge(repository(release=(True, True)))
    entries = [f"former/repo-{number}" for number in range(50)]
    done, summary = train(tmp_path, forge, release_off=" ".join(entries))
    assert done.returncode == 0, done.stdout + done.stderr
    assert releases_of(tmp_path) == [{"owner": "fleet", "name": "app"}]
    assert forge.repository_calls == [(7, f"/repos/{entry}") for entry in sorted(entries)]
    assert "::warning" not in summary


def test_release_off_ids_resolves_unmatched_entries_within_budget(tmp_path, monkeypatch):
    """When started is in budget, release_off_ids resolves each unmatched entry without warning."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []
    ids = {"former/a": 101, "former/b": 102, "former/c": 103}

    def fake_lookup(api, token, entry):
        calls.append((api, token, entry))
        return ids[entry]

    monkeypatch.setattr(train_mod, "release_off_repository_id", fake_lookup)
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: 100.0)
    env = {"GITHUB_API_URL": "https://api.example.test"}
    conf = {"release_off": frozenset(ids)}
    listed = [{"id": 1, "full_name": "fleet/app"}, {"id": 2, "full_name": "other/d"}]
    resolved, warnings = train_mod.release_off_ids(env, conf, {"id": 9}, "list-token", listed, 0.0)
    assert [entry for _, _, entry in calls] == ["former/a", "former/b", "former/c"]
    assert resolved == frozenset({101, 102, 103})
    assert warnings == []


def test_release_off_ids_closes_installation_when_budget_spent_before_first_lookup(
    tmp_path, monkeypatch
):
    """A spent sweep budget before the first lookup runs 0 lookups and opts out all listed IDs."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(
        train_mod, "release_off_repository_id", lambda api, token, entry: calls.append(entry)
    )
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: float(train_mod.SWEEP_SECONDS + 1))
    env = {"GITHUB_API_URL": "https://api.example.test"}
    conf = {"release_off": frozenset({"former/a", "former/b", "former/c"})}
    listed = [{"id": 9001, "full_name": "fleet/app"}, {"id": 9002, "full_name": "other/d"}]
    resolved, warnings = train_mod.release_off_ids(env, conf, {"id": 9}, "list-token", listed, 0.0)
    assert calls == []
    assert resolved == frozenset({9001, 9002})
    assert warnings == [
        (
            f"::warning title=Merge train::release_off entry former/a: "
            f"sweep budget of {train_mod.SWEEP_SECONDS} s spent; "
            f"release train off for installation 9"
        )
    ]


def test_release_off_ids_stops_after_first_lookup_when_budget_becomes_spent(tmp_path, monkeypatch):
    """When the sweep budget is spent after the first lookup, the second entry triggers fallback."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []

    def fake_lookup(api, token, entry):
        calls.append(entry)
        return 101

    ticks = itertools.chain(
        [float(train_mod.SWEEP_SECONDS - 1)],
        itertools.repeat(float(train_mod.SWEEP_SECONDS + 1)),
    )
    monkeypatch.setattr(train_mod, "release_off_repository_id", fake_lookup)
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: next(ticks))
    env = {"GITHUB_API_URL": "https://api.example.test"}
    conf = {"release_off": frozenset({"former/a", "former/b", "former/c"})}
    listed = [{"id": 9001, "full_name": "fleet/app"}, {"id": 9002, "full_name": "other/d"}]
    resolved, warnings = train_mod.release_off_ids(env, conf, {"id": 9}, "list-token", listed, 0.0)
    assert calls == ["former/a"]
    assert resolved == frozenset({9001, 9002})
    assert warnings == [
        (
            f"::warning title=Merge train::release_off entry former/b: "
            f"sweep budget of {train_mod.SWEEP_SECONDS} s spent; "
            f"release train off for installation 9"
        )
    ]


def test_release_off_ids_allows_lookups_at_exact_sweep_budget_boundary(tmp_path, monkeypatch):
    """Elapsed time equal to SWEEP_SECONDS is still within budget (> is strict)."""
    train_mod = load_train(tmp_path, monkeypatch)
    calls = []

    def fake_lookup(api, token, entry):
        calls.append(entry)
        return 9002

    monkeypatch.setattr(train_mod, "release_off_repository_id", fake_lookup)
    monkeypatch.setattr(train_mod.time, "monotonic", lambda: float(train_mod.SWEEP_SECONDS))
    env = {"GITHUB_API_URL": "https://api.example.test"}
    conf = {"release_off": frozenset({"former/a", "former/b"})}
    listed = [{"id": 9001, "full_name": "fleet/app"}, {"id": 9002, "full_name": "other/d"}]
    resolved, warnings = train_mod.release_off_ids(env, conf, {"id": 9}, "list-token", listed, 0.0)
    assert calls == ["former/a", "former/b"]
    assert resolved == frozenset({9002})
    assert warnings == []


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
    mutant = script().replace(
        "from release_workflows import MAX_RELEASES, check_hold_labels",
        "from release_workflows import check_hold_labels\nMAX_RELEASES = 1",
    )
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
        "${{ needs.plan.outputs.hold_labels }}"
    ):
        found.append("settings step does not read the hold labels")
    return found


def release_token_findings(mint):
    found = []
    if mint.get("owner") != "${{ matrix.repository.owner }}":
        found.append("token not minted for the repository's owner")
    if mint.get("repositories") != "${{ matrix.repository.name }}":
        found.append("token not limited to the repository")
    rights = {key: value for key, value in mint.items() if key.startswith("permission-")}
    if rights != TOKEN_RIGHTS:
        found.append(f"token rights {rights}")
    return found


def release_identity_findings(step, name, expected_token):
    found = []
    settings = step.get("with", {})
    if settings.get("token") != expected_token:
        found.append(f"{name} token is {settings.get('token')!r}, not the app's")
    if settings.get("repo-url") != "${{ matrix.repository.owner }}/${{ matrix.repository.name }}":
        found.append(f"{name} runs on another repository")
    return found


def release_action_findings(mint, running):
    expected_token = f"${{{{ steps.{mint.get('id')}.outputs.token }}}}"
    found = release_identity_findings(running[0], "release step", expected_token)
    for name, step in (("pull-request step", running[1]), ("second pull-request step", running[2])):
        found += release_identity_findings(step, name, expected_token)
        if step.get("with", {}).get("skip-github-release") not in (True, "true"):
            found.append(f"{name} does not skip releases")
    return found


def release_checks_findings(steps, running):
    found = []
    create_step, pr_step = running[:2]
    if create_step.get("with", {}).get("skip-github-pull-request") not in (True, "true"):
        found.append("release step does not skip pull requests")
    if create_step.get("continue-on-error") not in (True, "true"):
        found.append("release step does not continue on error")
    if "steps.settings.outputs.skip == 'false'" not in str(create_step.get("if", "")):
        found.append("release step does not check skip setting")
    if running[2].get("if") != f"steps.{pr_step.get('id')}.outcome == 'failure'":
        found.append("second pull-request step runs without a failed first attempt")
    found += frozen_findings(steps, pr_step)
    judging = [s for s in steps if str(s.get("run", "")).endswith('release_labels.py" after')]
    judge_env = judging[0].get("env", {}) if len(judging) == 1 else {}
    outcome = f"${{{{ steps.{create_step.get('id')}.outcome }}}}"
    if (judge_env.get("OUTCOME"), judge_env.get("SKIP")) != (
        outcome,
        "${{ steps.settings.outputs.skip }}",
    ):
        found.append("no step judges the release step that continues on error")
    return found


def release_job_scope_findings(job):
    found = []
    if job.get("permissions") != {} or job.get("environment") != "merge-train":
        found.append("GITHUB_TOKEN has rights, or the key's environment is missing")
    if "needs.plan.outputs.releases != '[]'" not in job.get("if", ""):
        found.append("runs without a non-empty release plan")
    if job.get("strategy", {}).get("matrix", {}).get("repository") != (
        "${{ fromJSON(needs.plan.outputs.releases) }}"
    ):
        found.append("matrix is not the sweep's list")
    return found


def release_job_findings(doc: dict) -> list[str]:
    """What keeps the release job from running release-please with the app's own token."""
    job = doc["jobs"].get("release", {})
    steps = job.get("steps", [])
    minting = [s for s in steps if str(s.get("uses", "")).startswith(APP_TOKEN)]
    running = [s for s in steps if str(s.get("uses", "")).startswith(RELEASE_PLEASE)]
    if len(minting) != 1 or len(running) != 3:
        return [f"{len(minting)} token steps and {len(running)} release-please steps"]
    return (
        release_token_findings(minting[0].get("with", {}))
        + release_action_findings(minting[0], running)
        + release_checks_findings(steps, running)
        + release_job_scope_findings(job)
    )


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
    assert release_job_findings(ciwf.load(RELEASE)) == []
    assert release_bound_findings(ciwf.load(RELEASE)) == []


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
        (
            2,
            lambda w: w.update(token="${{ github.token }}"),
            "second pull-request step token is '${{ github.token }}', not the app's",
        ),
    ],
)
def test_github_token_for_release_please_is_found(step_index, change, finding):
    """GITHUB_TOKEN opens release pull requests that start no checks (cordanaLLM/imago#58)."""
    doc = ciwf.load(RELEASE)
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
        (2, "second pull-request step runs on another repository"),
    ],
)
def test_release_please_repo_url_is_found(step_index, finding):
    doc = ciwf.load(RELEASE)
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
        (
            lambda steps: steps[2]["with"].pop("skip-github-release"),
            "second pull-request step does not skip releases",
        ),
    ],
)
def test_release_step_pieces_are_found(mutator, finding):
    doc = ciwf.load(RELEASE)
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
    doc = ciwf.load(RELEASE)
    mutator(doc)
    assert release_bound_findings(doc) == [finding]


def test_release_job_without_the_judge_is_found():
    """continue-on-error would turn every failed release green without the judge step."""
    doc = ciwf.load(RELEASE)
    steps = doc["jobs"]["release"]["steps"]
    judge = [s for s in steps if str(s.get("run", "")).endswith('release_labels.py" after')]
    assert len(judge) == 1
    judge[0]["env"]["OUTCOME"] = "success"
    assert release_job_findings(doc) == ["no step judges the release step that continues on error"]
    steps.remove(judge[0])
    assert release_job_findings(doc) == ["no step judges the release step that continues on error"]


def test_release_job_mutants_are_found():
    """Release job findings flag unconstrained tokens, excess rights, and bypassed switches."""
    doc = ciwf.load(RELEASE)
    job = doc["jobs"]["release"]
    mint = [s for s in job["steps"] if s.get("uses", "").startswith(APP_TOKEN)][0]["with"]
    mint.pop("repositories")
    mint["permission-issues"] = "write"
    job["if"] = job["if"].replace("needs.plan.outputs.releases != '[]'", "true")
    assert release_job_findings(doc) == [
        "token not limited to the repository",
        (
            "token rights {'permission-contents': 'write', 'permission-pull-requests': 'write', "
            "'permission-workflows': 'write', 'permission-issues': 'write'}"
        ),
        "runs without a non-empty release plan",
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


@pytest.mark.parametrize(
    "workflow_run",
    [
        pytest.param({"id": 43781873903}, id="run-id"),
        pytest.param(
            {
                "id": 43781873903,
                "html_url": "https://github.com/fleet/app/actions/runs/43781873903",
            },
            id="run-url",
        ),
        pytest.param(
            {"id": 456, "html_url": "https://github.com/fleet/app/actions/runs/456"},
            id="run-url-short",
        ),
        pytest.param(
            {
                "id": 43781873903,
                "html_url": (
                    "https://github.com/fleet-organisation/application-with-generated-files"
                    "/actions/runs/43781873903"
                ),
            },
            id="run-url-long",
        ),
    ],
)
def test_planted_regenerate_message_fits_commitlint(tmp_path, monkeypatch, workflow_run):
    """A long app identity and either run reference fit the commitlint line limits."""
    train_mod = load_train(tmp_path, monkeypatch)
    captured = {}

    def graphql(_env, _query, variables):
        captured.update(variables)
        return {"createCommitOnBranch": {"commit": {"oid": "a" * 40}}}

    monkeypatch.setattr(train_mod, "graphql", graphql)
    conf = {"bot_login": "plurano-merge-train[bot]", "bot_id": "342223998"}
    # a letter-leading head: textwrap splits a hyphenated word only between letters
    head = "deadbeef" * 5
    pr = {"number": 1, "headRefName": "renovate/demo-dep", "headRefOid": head}
    train_mod.commit_generated_files(
        {"GITHUB_REPOSITORY": "fleet/app"}, conf, pr, {"workflow_run": workflow_run}, {}
    )
    message = captured["input"]["message"]
    body = message["body"]
    assert all(len(line) <= 100 for line in [message["headline"], *body.splitlines()])
    assert all(len(line) <= 72 or len(line.split()) == 1 for line in body.splitlines()[:-1])
    trailer = (
        "Signed-off-by: plurano-merge-train "
        "<342223998+plurano-merge-train[bot]@users.noreply.github.com>"
    )
    assert body.splitlines()[-1] == trailer
    run_ref = workflow_run.get("html_url") or workflow_run["id"]
    lines = body.splitlines()
    assert any(f"{run_ref}," in line.split() for line in lines)
    assert any(f"regenerated-{head}." in line.split() for line in lines)
    assert " ".join(body.split()) == (
        f"Regenerated by the dependency update's own workflow run {run_ref}, "
        f"artifact regenerated-{head}. {trailer}"
    )


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
    assert body == (
        "Regenerated by the dependency update's own workflow run\n"
        "https://github.com/fleet/app/actions/runs/456, artifact\n"
        f"regenerated-{head}.\n\n"
        "Signed-off-by: merge-train-app <123456+merge-train-app[bot]@users.noreply.github.com>"
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


RENOVATE_BOT = {"login": "renovate", "__typename": "Bot"}
REGENERATION_LABEL = "merge-train: regeneration requested"
RENOVATE_SIGNOFF = (
    "chore(deps): update foo to v2\n\n"
    "Signed-off-by: renovate[bot] <29139614+renovate[bot]@users.noreply.github.com>"
)
ASKED_NOTE = "skipped: dependency update without Signed-off-by, asked its update bot to recreate it"
WAITING_NOTE = (
    "skipped: dependency update without Signed-off-by, waiting for its update bot to recreate it"
)
STILL_UNSIGNED_NOTE = (
    "skipped: dependency update still without Signed-off-by after its update bot "
    "recreated it; that bot's config adds none"
)
WOULD_ASK_NOTE = (
    "would ask: dependency update without Signed-off-by, would ask its update bot to recreate it"
)


def unsigned_dep_pull(number: int, state: str, **fields) -> dict:
    defaults = {
        "headRefName": "renovate/foo-2.x",
        "author": RENOVATE_BOT,
        "createdAt": OLD,
        "commit_authors": ["renovate[bot]"],
    }
    defaults.update(fields)
    return pull(number, state, **defaults)


LABELS_INCOMPLETE_NOTE = (
    "skipped: dependency update without Signed-off-by, labels not fully read, no recreate asked"
)
HEAD_MOVED_NOTE = (
    "skipped: dependency update without Signed-off-by, head moved since the sweep read, "
    "no recreate asked"
)
SKIP_UNSIGNED_NOTE = "skipped: dependency update without Signed-off-by, a person lands it"


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_unsigned_dependency_update_asks_the_update_bot_to_recreate(tmp_path, state):
    forge = Forge(repository(unsigned_dep_pull(1, state)))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL)]
    assert f"| #1 | {state} | {ASKED_NOTE} |" in summary


def test_unsigned_dependency_update_waiting_for_recreate_writes_nothing(tmp_path):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", labels=("rebase", REGENERATION_LABEL))))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {WAITING_NOTE} |" in summary


def test_unsigned_dependency_update_after_recreate_still_unsigned(tmp_path):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", labels=(REGENERATION_LABEL,))))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {STILL_UNSIGNED_NOTE} |" in summary


def test_unsigned_dependency_update_lands_after_the_update_bot_recreated_and_signed(tmp_path):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", labels=(REGENERATION_LABEL,))))
    forge.commits[1] = [(RENOVATE_SIGNOFF, 1)]
    done, _ = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_unsigned_dependency_regenerate_labels_once_when_signed_message_runs_twice(tmp_path):
    needle = (
        "            note = unsigned_update_note(env, conf, pull, read_head)\n"
        "            raise UnsignedBotError(f\"#{pull['number']}: {err}\", note) from err"
    )
    flow = script().replace(
        needle,
        "            note = unsigned_update_note(env, conf, pull, read_head)\n"
        "            unsigned_update_note(env, conf, pull, read_head)\n"
        "            raise UnsignedBotError(f\"#{pull['number']}: {err}\", note) from err",
        1,
    )
    assert flow != script()
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, _ = train(tmp_path, forge, flow, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL)]


def test_unsigned_dependency_update_by_a_person_still_skips_without_labels(tmp_path):
    user = {"login": "someone", "__typename": "User"}
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", author=user)))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {SKIP_UNSIGNED_NOTE} |" in summary


def test_planted_person_commit_on_bot_update_gets_no_recreate_ask(tmp_path):
    forge = Forge(
        repository(unsigned_dep_pull(1, "CLEAN", commit_authors=["renovate[bot]", "person-name"]))
    )
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {SKIP_UNSIGNED_NOTE} |" in summary


@pytest.mark.parametrize(
    ("logins", "expect_write"),
    [
        pytest.param(["renovate[bot]"] * 50, True, id="fifty"),
        pytest.param(["renovate[bot]"] * 51, False, id="fifty-one"),
    ],
)
def test_bot_commit_count_boundary_for_recreate_ask(tmp_path, logins, expect_write):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", commit_authors=logins)))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    if expect_write:
        assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL)]
        assert f"| #1 | CLEAN | {ASKED_NOTE} |" in summary
    else:
        assert forge.writes == []
        assert f"| #1 | CLEAN | {SKIP_UNSIGNED_NOTE} |" in summary


def test_planted_guard_beyond_label_page_gets_no_ask(tmp_path):
    labels = tuple(f"other-label-{i}" for i in range(50))
    pull_node = unsigned_dep_pull(1, "CLEAN", labels=labels)
    pull_node["labels"]["totalCount"] = 51
    forge = Forge(repository(pull_node))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {LABELS_INCOMPLETE_NOTE} |" in summary


def test_complete_label_read_of_fifty_asks_once(tmp_path):
    labels = tuple(f"other-label-{i}" for i in range(50))
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", labels=labels)))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL)]
    assert f"| #1 | CLEAN | {ASKED_NOTE} |" in summary


def test_unsigned_dependency_update_with_regenerate_label_off_skips_without_labels(tmp_path):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0", regenerate_label="")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | skipped: dependency update without Signed-off-by, a person lands it |"
        in summary
    )


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_unsigned_dependency_update_switch_off_reports_would_ask(tmp_path, state):
    forge = Forge(repository(unsigned_dep_pull(1, state)))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0", switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | {state} | {WOULD_ASK_NOTE} |" in summary


LINE_SEP = "\u2028"
DEP_SUBJECT_CO_AUTHOR_COMMITS = [
    (
        (
            f"chore(deps): update foo to v2{LINE_SEP}"
            "Co-authored-by: Bot <bot@example.org>\n\n"
            "Signed-off-by: Dev <dev@example.org>"
        ),
        1,
    ),
    ("chore(deps): follow up\n\nSigned-off-by: Dev <dev@example.org>", 1),
]
CO_AUTHOR_SQUASH_REFUSAL = "fleet/app: #1: squash message holds a Co-authored-by line"


@pytest.mark.parametrize("state", ["CLEAN", "BEHIND"])
def test_switch_off_signed_dependency_co_authored_subject_refused(tmp_path, state):
    forge = Forge(repository(unsigned_dep_pull(1, state)))
    forge.commits[1] = list(DEP_SUBJECT_CO_AUTHOR_COMMITS)
    done, summary = train(tmp_path, forge, min_age_days="0", switch="off")
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert CO_AUTHOR_SQUASH_REFUSAL in summary
    assert "Traceback" not in done.stdout + done.stderr


def test_signed_dependency_switch_off_without_co_author_subject_reports_no_note(tmp_path):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [(RENOVATE_SIGNOFF, 1)]
    done, summary = train(tmp_path, forge, min_age_days="0", switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert WOULD_ASK_NOTE not in summary
    assert CO_AUTHOR_SQUASH_REFUSAL not in summary


def test_planted_dependency_unsigned_note_without_value_error_refusal(tmp_path):
    kept = "        return unsigned_update_note(env, conf, pull, read_head)\n"
    needle = kept + (
        "    except ValueError as err:\n"
        "        raise RefusedError(f\"#{pull['number']}: {err}\") from err\n"
    )
    assert script().count(needle) == 1
    flow = script().replace(needle, kept)
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = list(DEP_SUBJECT_CO_AUTHOR_COMMITS)
    done, summary = train(tmp_path, forge, flow, min_age_days="0", switch="off")
    assert CO_AUTHOR_SQUASH_REFUSAL not in summary
    out = done.stdout + done.stderr
    assert "Traceback" in out
    assert "ValueError: squash message holds a Co-authored-by line" in out


def test_switch_off_linear_history_behind_unsigned_update_reports_no_ask(tmp_path):
    forge = Forge(
        repository(
            unsigned_dep_pull(1, "BEHIND"),
            rules=("REQUIRED_LINEAR_HISTORY",),
        )
    )
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, min_age_days="0", switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | BEHIND | behind: linear history, a person rebases |" in summary
    assert WOULD_ASK_NOTE not in summary


def test_after_an_asked_unsigned_dependency_update_the_next_clean_pull_lands(tmp_path):
    forge = Forge(
        repository(
            unsigned_dep_pull(1, "CLEAN"),
            pull(2, "CLEAN"),
        )
    )
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, _ = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL), landed(2)]


def test_planted_unsigned_regenerate_without_guard_check_relabels(tmp_path):
    target = "    if guard not in names:"
    assert script().count(target) == 1
    flow = script().replace(target, "    if True:")
    assert flow != script()
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN", labels=(REGENERATION_LABEL,))))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, _ = train(tmp_path, forge, flow, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert ("label", 1, "rebase", REGENERATION_LABEL) in forge.writes


@pytest.mark.parametrize("switch", ["on", "off"])
def test_unsigned_dependency_update_head_moved_skips_recreate_ask(tmp_path, switch):
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    forge.commit_heads[1] = "b" * 40
    done, summary = train(tmp_path, forge, min_age_days="0", switch=switch)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {HEAD_MOVED_NOTE} |" in summary
    assert ASKED_NOTE not in summary
    assert WOULD_ASK_NOTE not in summary


@pytest.mark.parametrize("moved", [False, True], ids=["same-head", "moved-head"])
def test_unsigned_dependency_update_incomplete_labels_precede_head_moved(tmp_path, moved):
    labels = tuple(f"other-label-{i}" for i in range(50))
    pull_node = unsigned_dep_pull(1, "CLEAN", labels=labels)
    pull_node["labels"]["totalCount"] = 51
    forge = Forge(repository(pull_node))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    if moved:
        forge.commit_heads[1] = "b" * 40
    done, summary = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | CLEAN | {LABELS_INCOMPLETE_NOTE} |" in summary
    assert HEAD_MOVED_NOTE not in summary


def test_behind_unsigned_dependency_non_arming_reports_no_would_ask_and_writes_no_label(tmp_path):
    dry_dir = tmp_path / "dry"
    dry_dir.mkdir()
    armed_pull = unsigned_dep_pull(1, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    dry_forge = Forge(repository(armed_pull))
    dry_forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    dry_done, dry_summary = train(dry_dir, dry_forge, min_age_days="0", switch="off")
    assert dry_done.returncode == 0, dry_done.stdout + dry_done.stderr
    assert dry_forge.writes == []
    assert "| #1 | BEHIND | behind |" in dry_summary
    assert WOULD_ASK_NOTE not in dry_summary

    live_dir = tmp_path / "live"
    live_dir.mkdir()
    live_forge = Forge(repository(armed_pull))
    live_forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    live_done, live_summary = train(live_dir, live_forge, min_age_days="0", switch="on")
    assert live_done.returncode == 0, live_done.stdout + live_done.stderr
    assert live_forge.writes == [updated(1)]
    assert "| #1 | BEHIND | updated from the default branch |" in live_summary
    assert ASKED_NOTE not in live_summary


def test_planted_unsigned_head_moved_without_head_check_labels(tmp_path):
    target = '        if read_head != pull["headRefOid"]:'
    assert script().count(target) == 1
    flow = script().replace(target, "        if False:")
    assert flow != script()
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    forge.commit_heads[1] = "b" * 40
    done, _ = train(tmp_path, forge, flow, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, "rebase", REGENERATION_LABEL)]


def test_planted_dry_unsigned_behind_without_arming_skip_reports_would_ask(tmp_path):
    target = "        if not arming:\n            continue\n"
    assert script().count(target) == 1
    flow = script().replace(target, "")
    assert flow != script()
    armed_pull = unsigned_dep_pull(1, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(armed_pull))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    done, summary = train(tmp_path, forge, flow, min_age_days="0", switch="off")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #1 | BEHIND | {WOULD_ASK_NOTE} |" in summary


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
    mutant = script().replace(", commitBody: $body", "")
    assert mutant != script()
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commits[1] = list(PLANTED_COMMITS)
    done, _ = train(tmp_path, forge, text=mutant)
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
    call = "close_issues(local, conf, installation, target, now)"
    text = script()
    assert text.count(call) == 1
    forge = Forge(repository(pull(1, "BLOCKED")))
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    done, _ = train(tmp_path, forge, text.replace(call, "[]"))
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


def load_review_state():
    """Load the matching reader for live or explicit offline rule sources."""
    from test_review_state import adr92_reader

    return adr92_reader()


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
    " lambda pull: toggle_refresh(env, pull), conf\n"
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


def test_stopped_pull_held_by_required_status_is_refreshed(tmp_path):
    """A pull request the required status holds still gets its fresh event at the draft step."""
    forge = stopped_forge("BLOCKED")
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "### fleet/app: refreshed #1" in summary
    assert forge.writes == refreshed(1)


def test_stopped_pull_with_other_failed_step_not_refreshed_under_required_status(tmp_path):
    """With required_status set, a pull request red at another step gets no fresh event."""
    forge = stopped_forge("BLOCKED", jobs=("x",))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "refreshed" not in summary


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


def test_release_off_setting_accepts_50_entries(tmp_path, monkeypatch):
    """Fifty entries, the bound, parse to the casefolded set."""
    train_mod = load_train(tmp_path, monkeypatch)
    entries = [f"fleet/repo-{number}" for number in range(50)]
    assert train_mod.MAX_RELEASE_OFF == 50
    assert train_mod.release_off_setting(" ".join(entries)) == frozenset(entries)


def test_release_off_setting_refuses_51_entries(tmp_path, monkeypatch):
    """Fifty-one entries are refused with the setting error."""
    train_mod = load_train(tmp_path, monkeypatch)
    raw = " ".join(f"fleet/repo-{number}" for number in range(51))
    with pytest.raises(train_mod.InvalidSettingError, match="release_off.*50"):
        train_mod.release_off_setting(raw)


def test_release_off_setting_counts_repeated_entries(tmp_path, monkeypatch):
    """The bound counts entries before deduplication, so 51 copies of one name are refused."""
    train_mod = load_train(tmp_path, monkeypatch)
    raw = " ".join(["fleet/app"] * 51)
    with pytest.raises(train_mod.InvalidSettingError, match="release_off.*50"):
        train_mod.release_off_setting(raw)


def test_release_off_over_the_bound_stops_before_any_forge_call(tmp_path):
    """A release_off over the bound is a setting error: exit 3, no token minted, no call."""
    forge = Forge(repository(pull(1, "CLEAN")))
    raw = " ".join(f"fleet/repo-{number}" for number in range(51))
    done, _ = train(tmp_path, forge, release_off=raw)
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert "release_off accepts at most 50 entries" in done.stdout
    assert (forge.minted, forge.where) == ([], [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job: job["steps"][-1].pop("if"),
        lambda job: next(s for s in job["steps"] if s.get("id") == "release-pr").pop("if"),
        lambda job: [s.get("env", {}).pop("HOLD_LABELS", None) for s in job["steps"]],
    ],
)
def test_release_job_without_the_frozen_check_is_refused(mutate):
    doc = ciwf.load(RELEASE)
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


def test_held_update_a_person_marked_ready_is_drafted_again_and_not_landed(tmp_path):
    ready = pull(1, "CLEAN", headRefName="renovate/go-snaps", labels=(HOLD,))
    fix = pull(2, "BEHIND", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(ready, fix, rules=LINEAR))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    holds = [w for w in forge.writes if w[0] in ("label", "draft", "unlabel")]
    assert holds == [("draft", "PR_1")], forge.writes
    assert landed(1) not in forge.writes, forge.writes


def test_release_of_an_update_already_ready_only_removes_the_label(tmp_path):
    ready = pull(1, "BLOCKED", headRefName="renovate/go-snaps", labels=(HOLD,))
    forge = Forge(repository(ready))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("unlabel", 1, HOLD)], forge.writes


def test_security_hold_held_by_required_status_holds_dependency_updates(tmp_path):
    """A security update held by required status still drafts other dependency updates."""
    update = pull(1, "BLOCKED", headRefName="renovate/go-snaps")
    fix = pull(2, "BLOCKED", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(update, fix, rules=LINEAR))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1")], forge.writes
    assert "held as a draft: security update #2 is open" in summary, summary


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


SESSION = "merge-train: session hold"
# An open suite of an app that starts no workflow run; it can stay open forever.
APP_SUITE = {
    "databaseId": 900,
    "conclusion": None,
    "createdAt": "2026-10-18T00:00:00Z",
    "app": {"slug": "review-app"},
    "workflowRun": None,
}
SESSION_PREDICATE = (
    "sessions = [pull for pull, _ in rows if session_waiting(pull, default_branch, conf, replaced)]"
)


def session_pair(state="BLOCKED", **fields):
    """A clean dependency update and a person's session PR whose head workflow run is running."""
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps")
    fields.setdefault("suites", [None])
    session = pull(2, state, author={"login": "lusoris"}, **fields)
    return update, session


@pytest.mark.parametrize("suites", [[None], ["SUCCESS", None]], ids=["running", "one-running"])
def test_session_pending_holds_dependency_update_as_a_draft(tmp_path, suites):
    update, session = session_pair(suites=suites)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, SESSION), ("draft", "PR_1")], forge.writes
    assert landed(1) not in forge.writes
    assert "held as a draft: session pull request #2 waits (bound 4 h)" in summary, summary


def test_session_behind_holds_dependency_update(tmp_path):
    forge = Forge(repository(*session_pair("BEHIND"), rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, SESSION), ("draft", "PR_1")], forge.writes
    assert "session pull request #2 waits (bound 4 h)" in summary


@pytest.mark.parametrize(
    "state,accept", [("CLEAN", ""), ("UNSTABLE", "UNSTABLE"), ("HAS_HOOKS", "HAS_HOOKS")]
)
def test_session_accepted_state_holds_dependency_update(tmp_path, state, accept):
    forge = Forge(repository(*session_pair(state), rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="other, lusoris ", accept_states=accept)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes[:2] == [("label", 1, SESSION), ("draft", "PR_1")], forge.writes
    assert landed(1) not in forge.writes
    assert landed(2) in forge.writes


@pytest.mark.parametrize(
    "fields,settings,include",
    [
        ({}, {"session_authors": "lusoris"}, False),
        ({}, {"session_authors": "someone-else"}, True),
        ({}, {"session_authors": ""}, True),
        ({}, {}, True),
        ({"suites": ["SUCCESS"]}, {"session_authors": "lusoris"}, True),
        ({"suites": ["SUCCESS", "SKIPPED"]}, {"session_authors": "lusoris"}, True),
        ({"suites": [None, "CANCELLED"]}, {"session_authors": "lusoris"}, True),
        ({"suites": []}, {"session_authors": "lusoris"}, True),
        ({"suites": ["FAILURE"]}, {"session_authors": "lusoris"}, True),
        ({"isDraft": True}, {"session_authors": "lusoris"}, True),
        ({"isCrossRepository": True}, {"session_authors": "lusoris"}, True),
        ({"baseRefName": "next"}, {"session_authors": "lusoris"}, True),
        ({"labels": ("hold",)}, {"session_authors": "lusoris"}, True),
        ({"headRefName": "renovate/other"}, {"session_authors": "lusoris"}, True),
        ({"author": None}, {"session_authors": "lusoris"}, True),
        ({"author": "author@example.com"}, {"session_authors": "lusoris"}, True),
        ({"suites": [APP_SUITE]}, {"session_authors": "lusoris"}, True),
        ({"latest": {"nodes": []}}, {"session_authors": "lusoris"}, True),
    ],
    ids=[
        "none",
        "unlisted",
        "disabled",
        "default",
        "success",
        "finished",
        "red-beside-running",
        "absent",
        "red",
        "draft",
        "fork",
        "base",
        "held",
        "dependency",
        "no-author",
        "string-author",
        "app-suite",
        "missing-head",
    ],
)
def test_session_nonwaiting_does_not_hold_dependency_update(tmp_path, fields, settings, include):
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps")
    defaults = {"author": {"login": "lusoris"}, "suites": [None]}
    session = pull(2, "BLOCKED", **(defaults | fields))
    forge = Forge(repository(update, *([session] if include else []), rules=LINEAR))
    done, _ = train(tmp_path, forge, **settings)
    assert done.returncode == 0, done.stdout + done.stderr
    assert landed(1) in forge.writes, forge.writes
    assert ("label", 1, SESSION) not in forge.writes


@pytest.mark.parametrize(
    "fields", [{"labels": ("security",)}, {"title": "fix(deps): [SECURITY] x/net"}]
)
def test_session_hold_never_holds_security_updates(tmp_path, fields):
    session = session_pair()[1]
    fix = pull(3, "CLEAN", headRefName="renovate/x-net", **fields)
    forge = Forge(repository(session, fix))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert landed(3) in forge.writes, forge.writes
    assert ("label", 3, SESSION) not in forge.writes


@pytest.mark.parametrize(
    "fields",
    [
        {"headRefName": "feat/update"},
        {"isDraft": True},
        {"isCrossRepository": True},
        {"baseRefName": "next"},
    ],
)
def test_session_hold_leaves_other_targets_alone(tmp_path, fields):
    update, session = session_pair()
    update.update(fields)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not [w for w in forge.writes if w[0] in ("label", "draft", "ready")], forge.writes


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("missing", [True, False])
def test_session_hold_release_when_session_is_gone(tmp_path, draft, missing):
    update = pull(1, "BLOCKED", headRefName="renovate/go-snaps", isDraft=draft, labels=(SESSION,))
    forge = Forge(repository(update))
    forge.label_missing = missing
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    expected = ([("ready", "PR_1")] if draft else []) + (
        [] if missing else [("unlabel", 1, SESSION)]
    )
    assert forge.writes == expected, forge.writes
    assert "released from the session hold: no session pull request waits" in summary


@pytest.mark.parametrize("change", ["red", "draft", "held"])
def test_session_hold_releases_when_session_stops_waiting(tmp_path, change):
    update, session = session_pair()
    update.update(isDraft=True, labels={"nodes": [{"name": SESSION}]})
    if change == "red":
        session = session_pair(suites=["FAILURE"])[1]
    elif change == "draft":
        session["isDraft"] = True
    else:
        session["labels"]["nodes"] = [{"name": "hold"}]
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("ready", "PR_1"), ("unlabel", 1, SESSION)], forge.writes
    assert "released from the session hold: no session pull request waits" in summary


def session_events(*hours):
    """Session label events, including unrelated events to exercise the earliest matching one."""
    return {
        "nodes": [
            {"createdAt": OLD, "label": {"name": "unrelated"}},
            *[
                {"createdAt": (NOW - timedelta(hours=age)).isoformat(), "label": {"name": SESSION}}
                for age in hours
            ],
        ]
    }


def test_session_hold_bound_releases_from_earliest_label_event(tmp_path):
    update, session = session_pair()
    update.update(isDraft=True, labels={"nodes": [{"name": SESSION}]}, holds=session_events(1, 5))
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("ready", "PR_1"), ("unlabel", 1, SESSION)], forge.writes
    assert "released from the session hold: held 4 h, the bound" in summary


def test_session_hold_expired_ready_update_is_never_held_again(tmp_path):
    update, session = session_pair()
    update["holds"] = session_events(5)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)], forge.writes


def test_session_hold_bound_is_inclusive_and_missing_events_keep_the_hold(tmp_path, monkeypatch):
    train_mod = load_train(tmp_path, monkeypatch)
    conf = {"session_hold": timedelta(hours=4)}
    update = pull(1, "CLEAN", holds=session_events(4))
    assert train_mod.session_hold_expired(update, conf, NOW)
    assert not train_mod.session_hold_expired(update, conf, NOW - timedelta(seconds=1))
    assert not train_mod.session_hold_expired(pull(1, "CLEAN"), conf, NOW)


def unrelated_events(count):
    """Label events of other labels only, oldest first."""
    return {"nodes": [{"createdAt": OLD, "label": {"name": "unrelated"}}] * count}


def test_session_hold_full_event_page_without_the_session_event_is_expired(tmp_path, monkeypatch):
    train_mod = load_train(tmp_path, monkeypatch)
    conf = {"session_hold": timedelta(hours=4)}
    full = train_mod.HOLD_EVENT_PAGE
    assert train_mod.session_hold_expired(pull(1, "CLEAN", holds=unrelated_events(full)), conf, NOW)
    short = pull(1, "CLEAN", holds=unrelated_events(full - 1))
    assert not train_mod.session_hold_expired(short, conf, NOW)
    paged = unrelated_events(full - 1)
    paged["nodes"].append(session_events(1)["nodes"][1])
    assert not train_mod.session_hold_expired(pull(1, "CLEAN", holds=paged), conf, NOW)


def test_session_hold_full_event_page_releases_the_update(tmp_path):
    update, session = session_pair()
    update.update(isDraft=True, labels={"nodes": [{"name": SESSION}]}, holds=unrelated_events(20))
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("ready", "PR_1"), ("unlabel", 1, SESSION)], forge.writes


def test_session_hold_a_person_marked_ready_is_drafted_again(tmp_path):
    update, session = session_pair()
    update["labels"] = {"nodes": [{"name": SESSION}]}
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("draft", "PR_1")], forge.writes
    assert "held as a draft: session pull request #2 waits (bound 4 h)" in summary


def test_session_hold_failed_draft_write_is_retried_and_never_lands(tmp_path):
    update, session = session_pair()
    forge = Forge(repository(update, session, rules=LINEAR))
    forge.mutation_statuses["convertPullRequestToDraft"] = [502]
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode != 0
    assert "HTTP 502" in done.stdout + done.stderr
    assert forge.writes == [("label", 1, SESSION), ("draft", "PR_1")], forge.writes
    update["labels"] = {"nodes": [{"name": SESSION}]}
    again = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, again, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert again.writes == [("draft", "PR_1")], again.writes


@pytest.mark.parametrize("label", [HOLD, SESSION])
def test_failed_label_removal_keeps_the_hold_and_is_retried(tmp_path, label):
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps", isDraft=True, labels=(label,))
    forge = Forge(repository(update))
    forge.unlabel_status = 500
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode != 0
    assert "HTTP 500" in done.stdout + done.stderr
    assert forge.writes == [("ready", "PR_1")], forge.writes
    update["isDraft"] = False
    again = Forge(repository(update))
    done, _ = train(tmp_path, again, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert again.writes == [("unlabel", 1, label)], again.writes


@pytest.mark.parametrize("label", [HOLD, SESSION])
def test_managed_hold_label_is_never_landable(tmp_path, label):
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps", labels=(label,))
    forge = Forge(repository(update))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("unlabel", 1, label)], forge.writes


@pytest.mark.parametrize("draft", [True, False])
def test_session_hold_releases_a_security_update_at_once(tmp_path, draft):
    session = session_pair()[1]
    fix = pull(
        3,
        "CLEAN",
        headRefName="renovate/x-net",
        isDraft=draft,
        labels=("security", SESSION),
        holds=session_events(1),
    )
    forge = Forge(repository(session, fix, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    expected = ([("ready", "PR_3")] if draft else []) + [("unlabel", 3, SESSION)]
    assert forge.writes[: len(expected)] == expected, forge.writes
    assert ("draft", "PR_3") not in forge.writes
    assert "released from the session hold: a security update is exempt" in summary


@pytest.mark.parametrize(
    "decision,held",
    [("REVIEW_REQUIRED", False), ("CHANGES_REQUESTED", False), ("APPROVED", True), (None, True)],
)
def test_session_hold_waits_only_when_checks_alone_block(tmp_path, decision, held):
    update, session = session_pair(reviewDecision=decision)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (("label", 1, SESSION) in forge.writes) is held, forge.writes
    assert (landed(1) in forge.writes) is not held, forge.writes


@pytest.mark.parametrize(
    "session_fields,other_body,held",
    [
        ({"labels": ("superseded",)}, "", False),
        ({"body": "Superseded by #9."}, "", False),
        ({}, "Supersedes #2.", False),
        ({}, "Follows up #2.", True),
    ],
    ids=["label", "own-body", "other-body", "control"],
)
def test_superseded_session_never_waits(tmp_path, session_fields, other_body, held):
    update, session = session_pair(**session_fields)
    other = pull(3, "BLOCKED", body=other_body)
    forge = Forge(repository(update, session, other, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (("label", 1, SESSION) in forge.writes) is held, forge.writes
    assert (landed(1) in forge.writes) is not held, forge.writes


@pytest.mark.parametrize("status,held", [(None, False), (ctx("lanes/booked", "SUCCESS"), True)])
def test_session_accepted_state_waits_only_when_required_status_passes(tmp_path, status, held):
    update, session = session_pair("CLEAN", suites=["SUCCESS"], status=status)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris", required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (("label", 1, SESSION) in forge.writes) is held, forge.writes


@pytest.mark.parametrize("count,held", [(29, False), (30, True)])
def test_session_full_suite_page_counts_as_pending(tmp_path, count, held):
    update, session = session_pair(suites=["SUCCESS"] * count)
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (("label", 1, SESSION) in forge.writes) is held, forge.writes


@pytest.mark.parametrize(
    "count,authors,held", [(101, "lusoris", True), (101, "", False), (100, "lusoris", False)]
)
def test_session_hold_with_unread_pages_holds_bounded(tmp_path, count, authors, held):
    update = pull(1, "CLEAN", headRefName="renovate/go-snaps")
    others = [pull(n, "DIRTY") for n in range(2, count + 1)]
    forge = Forge(repository(update, *others, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors=authors)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (("label", 1, SESSION) in forge.writes) is held, forge.writes
    assert (landed(1) in forge.writes) is not held, forge.writes
    note = "held as a draft: a session pull request past the pages read may wait (bound 4 h)"
    assert (note in summary) is held, summary


def test_train_job_runs_one_at_a_time_per_app():
    concurrency = ciwf.load(TRAIN)["jobs"]["train"]["concurrency"]
    assert "${{ inputs.app_client_id }}" in concurrency["group"], concurrency
    assert concurrency["cancel-in-progress"] is False, concurrency


@pytest.mark.parametrize("labels", [(HOLD, SESSION), (SESSION,)])
def test_session_release_preserves_security_hold_including_new_hold(tmp_path, labels):
    update = pull(1, "BLOCKED", headRefName="renovate/go-snaps", isDraft=True, labels=labels)
    fix = pull(3, "BEHIND", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(update, fix, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    expected = ([] if HOLD in labels else [("label", 1, HOLD)]) + [("unlabel", 1, SESSION)]
    assert forge.writes == expected, forge.writes
    assert "released from the session hold: no session pull request waits" in summary


def test_security_release_preserves_session_hold(tmp_path):
    update, session = session_pair()
    update.update(isDraft=True, labels={"nodes": [{"name": HOLD}, {"name": SESSION}]})
    forge = Forge(repository(update, session, rules=LINEAR))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("unlabel", 1, HOLD)], forge.writes


def test_security_release_preserves_session_hold_added_in_same_run(tmp_path):
    update, session = session_pair()
    update.update(mergeStateStatus="BLOCKED", labels={"nodes": [{"name": HOLD}]})
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, SESSION), ("draft", "PR_1"), ("unlabel", 1, HOLD)], (
        forge.writes
    )
    assert "held as a draft: session pull request #2 waits (bound 4 h)" in summary
    assert "released from the security hold" in summary


def test_security_released_draft_moves_to_session_hold_without_ready(tmp_path):
    """A security-held draft goes straight to the session hold: no ready, no second draft."""
    update, session = session_pair()
    update.update(isDraft=True, mergeStateStatus="BLOCKED", labels={"nodes": [{"name": HOLD}]})
    forge = Forge(repository(update, session, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, SESSION), ("unlabel", 1, HOLD)], forge.writes
    assert "held as a draft: session pull request #2 waits (bound 4 h)" in summary


def test_session_and_security_hold_additions_draft_only_once(tmp_path):
    update, session = session_pair()
    fix = pull(3, "BEHIND", headRefName="renovate/x-net", labels=("security",))
    forge = Forge(repository(update, session, fix, rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("label", 1, HOLD), ("draft", "PR_1"), ("label", 1, SESSION)], (
        forge.writes
    )
    assert "held as a draft: security update #3 is open" in summary
    assert "held as a draft: session pull request #2 waits (bound 4 h)" in summary


def test_session_and_security_hold_release_ready_only_once(tmp_path):
    update = pull(1, "BLOCKED", isDraft=True, labels=(HOLD, SESSION))
    forge = Forge(repository(update))
    done, _ = train(tmp_path, forge, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("unlabel", 1, HOLD), ("ready", "PR_1"), ("unlabel", 1, SESSION)], (
        forge.writes
    )


@pytest.mark.parametrize("raw", ["0", "49", "x", "1.5", "-1", ""])
def test_session_hold_hours_invalid_settings_fail(tmp_path, raw):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, session_hold_hours=raw)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "session_hold_hours" in done.stdout + done.stderr
    assert forge.writes == []


@pytest.mark.parametrize("hours", ["1", "48"])
def test_session_hold_hours_settings_boundaries(tmp_path, hours):
    forge = Forge(repository(*session_pair(), rules=LINEAR))
    done, summary = train(tmp_path, forge, session_authors="lusoris", session_hold_hours=hours)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"session pull request #2 waits (bound {hours} h)" in summary
    assert forge.writes == [("label", 1, SESSION), ("draft", "PR_1")]


def test_session_hold_query_keeps_required_status_selection_replaceable(tmp_path, monkeypatch):
    rules = load_rules(tmp_path)
    train_mod = load_train(tmp_path, monkeypatch)
    for query in (rules.QUERY, rules.STATUS_QUERY):
        assert "statusCheckRollup" not in query
        assert "workflowRun { databaseId workflow { databaseId } }" in query
        # The page sizes the full-page checks compare against are the sizes the query asks.
        assert f"checkSuites(first: {train_mod.SUITE_PAGE})" in query
        events = f"timelineItems(itemTypes: [LABELED_EVENT], first: {train_mod.HOLD_EVENT_PAGE})"
        assert f"holds: {events}" in query
        assert "mergeStateStatus isInMergeQueue reviewDecision" in query
        assert "... on LabeledEvent { createdAt label { name } }" in query
        assert "signature { wasSignedByGitHub }" in query
    assert "status { context(name: $status) { state } }" in rules.STATUS_QUERY
    assert "status { context(name: $status) { state } }" not in rules.QUERY


def test_planted_session_hold_that_finds_no_session_lands_update(tmp_path):
    mutant = script()
    assert mutant.count(SESSION_PREDICATE) == 1
    mutant = mutant.replace(SESSION_PREDICATE, "sessions = []")
    forge = Forge(repository(*session_pair(), rules=LINEAR))
    done, _ = train(tmp_path, forge, text=mutant, session_authors="lusoris")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)], forge.writes


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
    """Planted mutant: unhandled UnsignedBotError in re_arm_after_review refuses the run."""
    target = (
        "    try:\n"
        "        arm(env, conf, pull)\n"
        "    except UnsignedBotError as err:\n"
        '        notes[pull["number"]] = err.note\n'
        "        return f\"disarmed #{pull['number']}: its commits carry no Signed-off-by\""
    )
    mutant = (
        "    try:\n"
        "        arm(env, conf, pull)\n"
        "    except KeyError as err:\n"
        '        notes[pull["number"]] = err.note\n'
        "        return f\"disarmed #{pull['number']}: its commits carry no Signed-off-by\""
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
    clause = 'landerLease: label(name: "merge-train: lander lease") { description }'
    assert clause in module()
    query = script().split('LANDER_LEASE = """', 1)[1].split('"""', 1)[0]
    assert clause in query


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


def test_bad_lease_description_still_disarms_red_scanning(tmp_path):
    """A malformed lease still lets auto-merge come off a red-scanning pull (record 0068)."""
    armed = pull(
        1,
        "CLEAN",
        autoMergeRequest={"enabledAt": OLD},
        suites=["SUCCESS", suite(1001, "FAILURE", 10, None, "github-code-scanning")],
    )
    forge = Forge(repository(armed, lease="until=tomorrow"))
    done, summary = train(tmp_path, forge)
    assert done.returncode != 0
    assert forge.writes == [disarmed(1)]
    assert "lander lease description 'until=tomorrow' is invalid" in summary


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


def test_fresh_train_write_holds_other_landings_under_required_status(tmp_path):
    """A fresh regenerate head without the required status still holds other landings."""
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
    other = pull(
        2,
        "CLEAN",
        headRefName="feat/clean-feature",
        status=ctx("lanes/booked", "SUCCESS"),
    )
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert f"| #2 | CLEAN | held for the generated files of #1 until {until} |" in summary


def test_behind_regenerate_commit_does_not_write_hold_under_required_status(tmp_path):
    """A BEHIND regenerate head the status holds holds nothing: the update tool rebases it."""
    rules = load_rules(tmp_path)
    bot_email = f"123456+{BOT}[bot]@users.noreply.github.com"
    recent = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = pull(
        1,
        "BEHIND",
        headRefName="renovate/demo-dep",
        messageHeadline=rules.REGENERATE_HEADLINE,
        author=bot_email,
        committedDate=recent,
    )
    other = pull(
        2,
        "CLEAN",
        headRefName="feat/clean-feature",
        status=ctx("lanes/booked", "SUCCESS"),
    )
    forge = Forge(repository(written, other))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(2)]
    assert "### fleet/app: landed #2" in summary


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
        user="renovate[bot]",
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
    assert re.search(
        r"reviews\(last: 20\) \{\s*nodes \{\s*submittedAt\s*state\s*"
        r"author \{ login __typename \}",
        rules.QUERY,
    )
    assert "reviews(last: 20)" in rules.QUERY
    assert re.search(
        r"commit \{ oid \}\s*body\s*comments\(first: 10\) "
        r"\{ totalCount nodes \{ replyTo \{ id \} \} \}",
        rules.QUERY,
    )


def _head_review(login, typename, submitted_at, state="COMMENTED"):
    """One submitted review on pull request 1's current head."""
    return {
        "submittedAt": submitted_at,
        "state": state,
        "author": {"login": login, "__typename": typename},
        "commit": {"oid": f"{1:040x}"},
    }


def _thread_reply(login, submitted_at):
    """A person's review containing only a reply to an existing finding."""
    return {
        **_head_review(login, "User", submitted_at),
        "body": "",
        "comments": {"totalCount": 1, "nodes": [{"replyTo": {"id": "PRRC_finding"}}]},
    }


def test_planted_app_pr_person_reply_settles(tmp_path):
    """A person's commented review answers an earlier app review on an app PR."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _thread_reply("lusoris", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_planted_app_pr_without_reply_holds(tmp_path):
    """An unanswered app review still holds a non-dependency app PR."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[_head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00")],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:43:00Z on head, last response at 15:40:00Z"
    )


def test_planted_live_7747_person_reply_alone_settles(tmp_path):
    """The person's lone thread reply on live PR 7747 does not become a holding review."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:43:07+00:00",
        reviews=[_thread_reply("lusoris", "2026-11-13T15:47:50+00:00")],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_app_pr_person_approval_settles(tmp_path):
    """A person's approval answers earlier app reviews on an app PR."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _head_review("lusoris", "User", "2026-11-13T15:47:00+00:00", "APPROVED"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_app_pr_person_change_request_holds(tmp_path):
    """A person's request for changes holds an app PR rather than answering it."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _head_review("lusoris", "User", "2026-11-13T15:47:00+00:00", "CHANGES_REQUESTED"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_app_pr_app_reply_does_not_settle(tmp_path):
    """Another app's commented review cannot answer an app review."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _head_review("coderabbitai", "Bot", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_person_pr_other_person_review_still_holds(tmp_path):
    """A different person's review still holds a person-authored PR."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="feat/x",
        author={"login": "person", "__typename": "User"},
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[_head_review("other", "User", "2026-11-13T15:47:00+00:00")],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_app_pr_person_reply_before_bot_review_holds(tmp_path):
    """A person's reply cannot settle an app review submitted afterward."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _thread_reply("lusoris", "2026-11-13T15:42:00+00:00"),
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:45:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:45:00Z on head, last response at 15:42:00Z"
    )


def test_is_bot_author_shapes(tmp_path):
    """Bot detection accepts the GraphQL type and legacy suffix, including missing authors."""
    rules = load_review_state()
    assert rules._is_bot_author(None) is False
    assert rules._is_bot_author({"login": "x"}) is False
    assert rules._is_bot_author({"login": "renovate[bot]"}) is True
    assert rules._is_bot_author({"login": "renovate", "__typename": "Bot"}) is True
    assert rules._is_bot_author({"login": "person", "__typename": "User"}) is False


def test_planted_train_lands_app_pr_after_person_reply(tmp_path):
    """The step lands a non-dependency app PR after a person answers its app review."""
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        createdAt=OLD,
        committedDate=(NOW - timedelta(minutes=20)).isoformat(),
        reviews=[
            _head_review("sourcery-ai", "Bot", (NOW - timedelta(minutes=10)).isoformat()),
            _thread_reply("lusoris", (NOW - timedelta(minutes=6)).isoformat()),
        ],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_planted_train_holds_app_pr_without_reply(tmp_path):
    """The step keeps a non-dependency app PR held until its review is answered."""
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        createdAt=OLD,
        committedDate=(NOW - timedelta(minutes=20)).isoformat(),
        reviews=[_head_review("sourcery-ai", "Bot", (NOW - timedelta(minutes=10)).isoformat())],
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | CLEAN | review settle:" in summary


def test_planted_app_pr_own_app_review_does_not_settle(tmp_path):
    """The authoring app cannot answer another app's review."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _head_review("plurano-merge-train", "Bot", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_planted_app_pr_tie_holds(tmp_path):
    """An app review holds when its timestamp ties a person's reply."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:47:00+00:00"),
            _thread_reply("lusoris", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:47:00Z"
    )


def test_planted_person_pr_tie_holds(tmp_path):
    """A person's PR also holds when another person's review ties the reply."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="feat/x",
        author={"login": "person", "__typename": "User"},
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("other", "User", "2026-11-13T15:47:00+00:00"),
            _head_review("person", "User", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:47:00Z"
    )


def test_planted_app_pr_change_request_survives_other_reply(tmp_path):
    """A different person's thread reply cannot clear a change request."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED"),
            _thread_reply("bob", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: change request by alice at 15:45:00Z on head stands"
    )


def test_planted_app_pr_change_request_survives_other_approval(tmp_path):
    """A different person's approval cannot clear a change request."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED"),
            _head_review("bob", "User", "2026-11-13T15:47:00+00:00", "APPROVED"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: change request by alice at 15:45:00Z on head stands"
    )


def test_planted_app_pr_change_request_survives_review_window(tmp_path):
    """Twenty later thread replies cannot hide a standing change request."""
    rules = load_rules(tmp_path)
    request = _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED")
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _thread_reply("bob", f"2026-11-13T15:46:{second:02d}+00:00") for second in range(20)
        ],
        opinions={"totalCount": 1, "nodes": [request]},
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: change request by alice at 15:45:00Z on head stands"
    )


def test_planted_app_pr_truncated_opinions_hold(tmp_path):
    """An unread opinion may be a change request, so an app PR holds."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[_thread_reply("bob", "2026-11-13T15:47:00+00:00")],
        opinions={
            "totalCount": 101,
            "nodes": [
                _head_review("bob", "User", "2026-11-13T15:45:00+00:00", "APPROVED")
                for _ in range(100)
            ],
        },
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: 101 approvals or change requests, 100 read"
    )


def test_app_pr_complete_opinions_boundary_settles(tmp_path):
    """Reading all one hundred approvals leaves no unknown change request."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[_thread_reply("bob", "2026-11-13T15:47:00+00:00")],
        opinions={
            "totalCount": 100,
            "nodes": [
                _head_review("bob", "User", "2026-11-13T15:45:00+00:00", "APPROVED")
                for _ in range(100)
            ],
        },
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_app_pr_dismissed_change_request_settles(tmp_path):
    """A dismissed change request is absent from opinions and cannot hold."""
    rules = load_rules(tmp_path)
    request = _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED")
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            {**request, "state": "DISMISSED"},
            _thread_reply("bob", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert p["opinions"] == {"totalCount": 0, "nodes": []}
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_person_pr_truncated_opinions_do_not_hold(tmp_path):
    """The opinions completeness hold applies only to app pull requests."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        author={"login": "bob", "__typename": "User"},
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[_thread_reply("bob", "2026-11-13T15:47:00+00:00")],
        opinions={
            "totalCount": 101,
            "nodes": [
                _head_review("bob", "User", "2026-11-13T15:45:00+00:00", "APPROVED")
                for _ in range(100)
            ],
        },
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_opinions_query_reads_bounded_review_states(tmp_path):
    """The opinions window excludes replies and includes its total count."""
    rules = load_rules(tmp_path)
    assert re.search(
        r"opinions: reviews\(last: 100, states: \[APPROVED, CHANGES_REQUESTED\]\) "
        r"\{\s*totalCount nodes \{ submittedAt state author \{ login __typename \} "
        r"commit \{ oid \} \} \}",
        rules.QUERY,
    )


def test_app_pr_change_request_cleared_by_same_person_approval(tmp_path):
    """The requesting person's later approval clears the change request."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED"),
            _head_review("alice", "User", "2026-11-13T15:47:00+00:00", "APPROVED"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_app_pr_approval_before_change_request_holds(tmp_path):
    """An earlier approval cannot clear the same person's later change request."""
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("alice", "User", "2026-11-13T15:44:00+00:00", "APPROVED"),
            _head_review("alice", "User", "2026-11-13T15:45:00+00:00", "CHANGES_REQUESTED"),
            _thread_reply("bob", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: change request by alice at 15:45:00Z on head stands"
    )


def test_planted_app_pr_person_standalone_review_holds(tmp_path):
    """A new review body holds rather than answering the earlier app review."""
    rules = load_rules(tmp_path)
    person_review = {
        **_head_review("lusoris", "User", "2026-11-13T15:47:00+00:00"),
        "body": "Please also bump the chart.",
    }
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            person_review,
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_planted_app_pr_person_new_inline_comment_holds(tmp_path):
    """A new inline comment holds even with an empty review body."""
    rules = load_rules(tmp_path)
    person_review = {
        **_head_review("lusoris", "User", "2026-11-13T15:47:00+00:00"),
        "body": "",
        "comments": {"totalCount": 1, "nodes": [{"replyTo": None}]},
    }
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            person_review,
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_app_pr_reply_mixed_with_new_comment_holds(tmp_path):
    """A reply mixed with a new inline comment remains a holding review."""
    rules = load_rules(tmp_path)
    person_review = {
        **_thread_reply("lusoris", "2026-11-13T15:47:00+00:00"),
        "comments": {
            "totalCount": 2,
            "nodes": [{"replyTo": {"id": "PRRC_finding"}}, {"replyTo": None}],
        },
    }
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            person_review,
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_app_pr_truncated_comments_hold(tmp_path):
    """A truncated comment page cannot prove that every comment answers a thread."""
    rules = load_rules(tmp_path)
    person_review = {
        **_thread_reply("lusoris", "2026-11-13T15:47:00+00:00"),
        "comments": {
            "totalCount": 11,
            "nodes": [{"replyTo": {"id": "PRRC_finding"}} for _ in range(10)],
        },
    }
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            person_review,
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) == (
        "review settle: review at 15:47:00Z on head, last response at 15:40:00Z"
    )


def test_app_pr_standalone_review_answered_by_reply_settles(tmp_path):
    """A later thread reply answers a person's earlier standalone review."""
    rules = load_rules(tmp_path)
    person_review = {
        **_head_review("alice", "User", "2026-11-13T15:45:00+00:00"),
        "body": "Check the digest.",
    }
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
        committedDate="2026-11-13T15:40:00+00:00",
        reviews=[
            _head_review("sourcery-ai", "Bot", "2026-11-13T15:43:00+00:00"),
            person_review,
            _thread_reply("bob", "2026-11-13T15:47:00+00:00"),
        ],
    )
    assert rules.review_settle(p, {"dependency": ("renovate/",)}, NOW) is None


def test_person_reply_shapes(tmp_path):
    """Only a User approval or a review consisting entirely of thread replies answers."""
    rules = load_review_state()
    assert (
        rules._person_reply(_head_review("alice", "User", "2026-11-13T15:47:00+00:00", "APPROVED"))
        is True
    )
    assert rules._person_reply(_thread_reply("alice", "2026-11-13T15:47:00+00:00")) is True
    assert (
        rules._person_reply({**_thread_reply("app", "2026-11-13T15:47:00+00:00"), "author": APP})
        is False
    )
    assert (
        rules._person_reply(
            _head_review("alice", "User", "2026-11-13T15:47:00+00:00", "CHANGES_REQUESTED")
        )
        is False
    )
    assert rules._person_reply(_head_review("alice", "User", "2026-11-13T15:47:00+00:00")) is False


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
        user="renovate[bot]",
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
        user="renovate[bot]",
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
        (
            "nodes {\n"
            "          databaseId state body submittedAt updatedAt url authorAssociation\n"
            "          author { login __typename } commit { oid }\n"
            "        }"
        ),
        "nodes { body createdAt updatedAt url authorAssociation author { login __typename } }",
        "commits(first: 100)",
        "commit { message committedDate }",
    ):
        assert token in q
    assert "headChecks: commits(last: 1)" in q
    assert "filterBy: {appId: 347564}" in q
    assert q.count("totalCount") == 8


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
        ("refused", "review body: cannot read: graphql ReviewBodies: GraphQL: "),
        ("gone", "review body: cannot read: the forge returned no pull request"),
        ("oversize", "review body: cannot read: graphql ReviewBodies: POST "),
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


def _maintainer_answer(login="maintainer", typename="User", review_id=1701, association=None):
    """One comment or review body naming the review it answers."""
    node = {
        "body": f"Addressed pullrequestreview-{review_id}.",
        "author": {"login": login, "__typename": typename},
    }
    if association is not None:
        node["authorAssociation"] = association
    return node


def _maintainer_forge(comments=(), reviews=(), **fields):
    """A signed app-authored non-dependency pull request with a review-body finding."""
    fields.setdefault("author", {"login": "vmafx-release-bot", "__typename": "Bot"})
    p = pull(1, "CLEAN", headRefName="docs/render", **fields)
    forge = Forge(repository(p))
    forge.commits[1] = [("docs: render\n\nSigned-off-by: Producer <producer@example.org>", 1)]
    forge.review_details[1] = _detail(
        reviews=[_bot_review(1701, "coderabbitai", "Nitpick comments: 1"), *reviews],
        comments=comments,
    )
    return forge


def _assert_maintainer_hold(tmp_path, forge):
    """The original unanswered-review reason remains visible without a write."""
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    expected = (
        "review body: coderabbitai review 1701 has findings "
        "without a reply naming pullrequestreview-1701"
    )
    assert f"| #1 | CLEAN | {expected} |" in summary


def test_maintainer_write_answer_lands_bot_pull_request(tmp_path):
    """A human collaborator with write permission can answer an app's review-body finding."""
    forge = _maintainer_forge(comments=[_maintainer_answer()])
    forge.rights = {"maintainer": "write"}
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_planted_app_image_pin_owner_answer_lands(tmp_path):
    """An owner's named answer releases the live app image-pin review-body finding."""
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
    )
    forge = Forge(repository(p))
    forge.commits[1] = [
        ("chore(image): pin kb-mcp\n\nSigned-off-by: Producer <producer@example.org>", 1)
    ]
    forge.review_details[1] = _detail(
        reviews=[
            _bot_review(
                5869705498,
                "sourcery-ai",
                "Hey - I've found 1 issue\n\n### Sourcery assessment\n\n"
                "**Needs a human reviewer.** The pin changes the served image.",
            )
        ],
        comments=[
            {
                "body": "Answer to pullrequestreview-5869705498: each of its 1 findings is also "
                "an inline thread, answered and resolved there.",
                "authorAssociation": "OWNER",
                "author": {"login": "owner", "__typename": "User"},
            },
            {
                "body": "<!-- ai/pr-reviewer:v1 -->\nRisk review: low.",
                "authorAssociation": "OWNER",
                "author": {"login": "owner", "__typename": "User"},
            },
        ],
    )
    forge.rights = {"owner": "admin"}
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_app_image_pin_unrelated_owner_comment_holds(tmp_path, monkeypatch):
    """An unrelated owner comment leaves the Sourcery finding unanswered."""
    merge_train = load_train(tmp_path, monkeypatch)
    body = (
        "Hey - I've found 1 issue\n\n### Sourcery assessment\n\n"
        "**Needs a human reviewer.** The pin changes the served image."
    )
    assert merge_train._bot_body_has_finding("sourcery-ai", body) is True
    p = pull(
        1,
        "CLEAN",
        headRefName="image-pin/kb-mcp",
        author=APP,
    )
    forge = Forge(repository(p))
    forge.commits[1] = [
        ("chore(image): pin kb-mcp\n\nSigned-off-by: Producer <producer@example.org>", 1)
    ]
    forge.review_details[1] = _detail(
        reviews=[_bot_review(5869705498, "sourcery-ai", body)],
        comments=[
            {
                "body": "<!-- ai/pr-reviewer:v1 -->\nRisk review: low.",
                "authorAssociation": "OWNER",
                "author": {"login": "owner", "__typename": "User"},
            }
        ],
    )
    forge.rights = {"owner": "admin"}
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert (
        "| #1 | CLEAN | review body: sourcery-ai review 5869705498 has findings "
        "without a reply naming pullrequestreview-5869705498 |"
    ) in summary
    assert forge.right_calls == []


def test_maintainer_admin_answer_lands_bot_pull_request(tmp_path):
    """Admin permission also lets a human collaborator answer an app's review-body finding."""
    forge = _maintainer_forge(comments=[_maintainer_answer()])
    forge.rights = {"maintainer": "admin"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_maintainer_read_right_cannot_answer_bot_review(tmp_path):
    """A reader's comment naming the review cannot release an app-authored pull request."""
    forge = _maintainer_forge(comments=[_maintainer_answer("outsider")])
    forge.rights = {"outsider": "read"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == ["outsider"]


def test_maintainer_missing_right_cannot_answer_bot_review(tmp_path):
    """A permission lookup returning 404 leaves the review body unanswered."""
    forge = _maintainer_forge(comments=[_maintainer_answer("outsider")])
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == ["outsider"]


def test_maintainer_right_refusal_holds_bot_pull_request(tmp_path):
    """A refused permission read holds the pull request and names the candidate."""
    forge = _maintainer_forge(comments=[_maintainer_answer()])
    forge.right_refusals = {"maintainer": 500}
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "| #1 | CLEAN | review body: cannot read the right of maintainer:" in summary
    assert forge.right_calls == ["maintainer"]


def test_maintainer_answer_does_not_release_human_pull_request(tmp_path):
    """Human-authored pull requests still require the author's own review-body answer."""
    forge = _maintainer_forge(comments=[_maintainer_answer()], author={"login": "person"})
    forge.rights = {"maintainer": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == []


def test_bot_maintainer_answer_does_not_count(tmp_path):
    """Bot comments never qualify as maintainer answers even with write permission."""
    forge = _maintainer_forge(comments=[_maintainer_answer("coderabbitai", "Bot")])
    forge.rights = {"coderabbitai": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == []


def test_maintainer_right_lookups_stop_at_five(tmp_path):
    """A sixth candidate with write permission cannot exceed the five-login bound."""
    forge = _maintainer_forge(comments=[_maintainer_answer(f"candidate-{n}") for n in range(6)])
    forge.rights = {"candidate-5": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == [f"candidate-{n}" for n in range(5)]


def test_maintainer_review_answer_lands_bot_pull_request(tmp_path):
    """A human maintainer can answer in a submitted review body instead of a comment."""
    answer = _bot_review(1702, "maintainer", "Addressed review 1701.", typename="User")
    forge = _maintainer_forge(reviews=[answer])
    forge.rights = {"maintainer": "write"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_maintainer_answer_for_different_review_is_not_a_candidate(tmp_path):
    """A comment naming another review triggers no permission lookup."""
    forge = _maintainer_forge(comments=[_maintainer_answer(review_id=1702)])
    forge.rights = {"maintainer": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == []


def test_maintainer_answer_keeps_armed_bot_pull_request_armed(tmp_path):
    """An armed app-authored pull request with a maintainer answer lands without disarming."""
    forge = _maintainer_forge(comments=[_maintainer_answer()], autoMergeRequest={"enabledAt": OLD})
    forge.rights = {"maintainer": "write"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_bot_review_body_without_right_callback_keeps_old_behaviour(tmp_path, monkeypatch):
    """Without a permission callback the pure core still ignores maintainer answers."""
    merge_train = load_train(tmp_path, monkeypatch)
    forge = _maintainer_forge(comments=[_maintainer_answer()])
    p = pull(1, "CLEAN", author={"login": "vmafx-release-bot", "__typename": "Bot"})
    hold = merge_train.review_body_hold(p, forge.review_details[1])
    assert hold == (
        "review body: coderabbitai review 1701 has findings "
        "without a reply naming pullrequestreview-1701"
    )


def test_maintainer_right_cache_spans_reviews_and_reuses_fifth_login(tmp_path, monkeypatch):
    """The fifth candidate's cached write right answers several findings without another read."""
    merge_train = load_train(tmp_path, monkeypatch)
    comments = [_maintainer_answer(f"candidate-{n}") for n in range(5)]
    comments[-1]["body"] += " Also addressed review 1702."
    forge = _maintainer_forge(comments=comments)
    detail = forge.review_details[1]
    detail["reviews"]["nodes"].append(_bot_review(1702, "coderabbitai", "Nitpick comments: 1"))
    detail["reviews"]["totalCount"] = 2
    calls = []

    def right(login):
        calls.append(login)
        return login == "candidate-4"

    p = pull(1, "CLEAN", author={"login": "vmafx-release-bot", "__typename": "Bot"})
    assert merge_train.review_body_hold(p, detail, right=right) is None
    assert calls == [f"candidate-{n}" for n in range(5)]


def test_maintainer_false_right_is_cached_and_remaining_count_recomputed(tmp_path, monkeypatch):
    """Duplicate denied candidates are read once and only unanswered findings remain counted."""
    merge_train = load_train(tmp_path, monkeypatch)
    answer = _maintainer_answer("outsider")
    answer["body"] += " Also review 1702 and review 1703."
    forge = _maintainer_forge(comments=[answer, answer, _maintainer_answer()])
    detail = forge.review_details[1]
    detail["reviews"]["nodes"] += [
        _bot_review(n, "coderabbitai", "Nitpick comments: 1") for n in (1702, 1703)
    ]
    detail["reviews"]["totalCount"] = 3
    calls = []

    def right(login):
        calls.append(login)
        return login == "maintainer"

    p = pull(1, "CLEAN", author={"login": "vmafx-release-bot", "__typename": "Bot"})
    assert merge_train.review_body_hold(p, detail, right=right) == (
        "review body: coderabbitai review 1702 has findings "
        "without a reply naming pullrequestreview-1702 (and 1 more)"
    )
    assert calls == ["outsider", "maintainer"]


def test_bot_maintainer_review_answer_does_not_count(tmp_path):
    """A bot's submitted review is excluded from maintainer permission lookups."""
    answer = _bot_review(
        1702, "coderabbitai", "Actionable comments posted: 0\nAddressed review 1701."
    )
    forge = _maintainer_forge(reviews=[answer])
    forge.rights = {"coderabbitai": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == []


def test_maintainer_right_refusal_reason_stays_bounded(tmp_path, monkeypatch):
    """Long permission failures retain the candidate's name within the 200-character limit."""
    merge_train = load_train(tmp_path, monkeypatch)
    forge = _maintainer_forge(comments=[_maintainer_answer()])

    def right(login):
        raise merge_train.RefusedError("permission refused " + "x" * 300)

    p = pull(1, "CLEAN", author={"login": "vmafx-release-bot", "__typename": "Bot"})
    reason = merge_train.review_body_hold(p, forge.review_details[1], right=right)
    prefix = "review body: cannot read the right of maintainer: permission refused "
    assert reason.startswith(prefix)
    assert len(reason) == 200


def test_maintainer_right_read_needs_metadata_on_the_repository_token(tmp_path):
    """A repository token without metadata read cannot confirm a maintainer's right."""
    source = script()
    mutant = source.replace('WRITE_RIGHTS = {\n    "metadata": "read",', "WRITE_RIGHTS = {")
    assert mutant != source, "WRITE_RIGHTS no longer opens with metadata read; update the plant"
    forge = _maintainer_forge(comments=[_maintainer_answer()])
    forge.rights = {"maintainer": "write"}
    done, summary = train(tmp_path, forge, text=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "review body: cannot read the right of maintainer:" in summary
    assert "HTTP 403" in summary
    assert forge.right_calls == ["maintainer"]


@pytest.mark.parametrize("kind", ["comments", "reviews"])
@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_maintainer_collaborator_answer_precedes_five_none_commenters(tmp_path, kind, association):
    """A collaborator's answer takes priority over five NONE accounts naming the review."""
    comments = [_maintainer_answer(f"outsider-{n}", association="NONE") for n in range(5)]
    fields = {"comments": comments, "reviews": []}
    fields[kind].append(_maintainer_answer(association=association))
    forge = _maintainer_forge(**fields)
    forge.rights = {"maintainer": "write"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_maintainer_best_association_spans_answers(tmp_path):
    """A later review improves a login's rank without its earlier NONE answer hiding it."""
    comments = [_maintainer_answer(f"outsider-{n}", association="NONE") for n in range(5)]
    forge = _maintainer_forge(
        comments=[*comments, _maintainer_answer(association="NONE")],
        reviews=[_maintainer_answer(association="MEMBER")],
    )
    forge.rights = {"maintainer": "write"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_maintainer_private_member_contributor_answer_precedes_none(tmp_path):
    """A private member seen as CONTRIBUTOR still has its write permission checked first."""
    comments = [_maintainer_answer(f"outsider-{n}", association="NONE") for n in range(5)]
    forge = _maintainer_forge(comments=[*comments, _maintainer_answer(association="CONTRIBUTOR")])
    forge.rights = {"maintainer": "write"}
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert forge.right_calls == ["maintainer", "maintainer"]


def test_maintainer_none_writer_after_five_contributors_stays_held(tmp_path):
    """Five higher-ranked non-writers exhaust the budget before a NONE writer's answer."""
    comments = [_maintainer_answer(f"contributor-{n}", association="CONTRIBUTOR") for n in range(5)]
    forge = _maintainer_forge(comments=[*comments, _maintainer_answer(association="NONE")])
    forge.rights = {"maintainer": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    assert forge.right_calls == [f"contributor-{n}" for n in range(5)]


def test_maintainer_member_on_later_review_gets_global_priority(tmp_path):
    """A MEMBER answering a later finding is read first while the earlier finding stays held."""
    comments = [_maintainer_answer(f"outsider-{n}", association="NONE") for n in range(5)]
    forge = _maintainer_forge(
        comments=[*comments, _maintainer_answer(review_id=1702, association="MEMBER")],
        reviews=[_bot_review(1702, "sourcery-ai", "Needs a human reviewer")],
    )
    forge.rights = {"maintainer": "write"}
    _assert_maintainer_hold(tmp_path, forge)
    done_calls = ["maintainer", *[f"outsider-{n}" for n in range(4)]]
    assert forge.right_calls == done_calls


@pytest.mark.parametrize("late_refusal", [False, True])
def test_maintainer_right_refusal_disarms_then_next_run_lands(tmp_path, late_refusal):
    """A refused initial or late right read disarms; the next healthy run lands directly."""
    forge1 = _maintainer_forge(comments=[_maintainer_answer()], autoMergeRequest={"enabledAt": OLD})
    forge1.right_refusals = {"maintainer": 500}
    if late_refusal:
        forge1.review_details[1] = [_detail(), forge1.review_details[1]]
    done1, summary1 = train(tmp_path, forge1)
    assert done1.returncode == 0, done1.stdout + done1.stderr
    assert forge1.writes == [disarmed(1)]
    assert "disarmed: review body: cannot read the right of maintainer:" in summary1

    forge2 = _maintainer_forge(comments=[_maintainer_answer()], autoMergeRequest=None)
    forge2.rights = {"maintainer": "write"}
    done2, summary2 = train(tmp_path, forge2)
    assert done2.returncode == 0, done2.stdout + done2.stderr
    assert forge2.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary2
    assert forge2.right_calls == ["maintainer", "maintainer"]


def test_planted_required_status_absent_holds_clean_pull(tmp_path):
    """A clean pull request without the required commit status context does not land."""
    p = pull(1, "CLEAN")
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "status lanes/booked absent: held" in summary
    assert "| #1 | CLEAN | status lanes/booked absent: held |" in summary


def test_planted_required_status_other_context_holds_clean_pull(tmp_path):
    """A clean pull request with a different status context does not satisfy the required status."""
    p = pull(1, "CLEAN", status=ctx("ci/other", "SUCCESS"))
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "status lanes/booked absent: held" in summary
    assert "| #1 | CLEAN | status lanes/booked absent: held |" in summary


@pytest.mark.parametrize("state", ["PENDING", "FAILURE", "ERROR", "EXPECTED"])
def test_required_status_non_success_holds_clean_pull(tmp_path, state):
    """A required status context in any non-SUCCESS state holds the pull request."""
    p = pull(1, "CLEAN", status=ctx("lanes/booked", state))
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    expected = f"status lanes/booked {state.lower()}: held"
    assert expected in summary
    assert f"| #1 | CLEAN | {expected} |" in summary


def test_required_status_success_lands(tmp_path):
    """A pull request whose required commit status is SUCCESS lands."""
    p = pull(1, "CLEAN", status=ctx("lanes/booked", "SUCCESS"))
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


def test_unset_required_status_lands_unchanged(tmp_path):
    """When required_status is unset or empty, landing behavior is unchanged."""
    p1 = pull(1, "CLEAN")
    forge1 = Forge(repository(p1))
    done1, summary1 = train(tmp_path, forge1)
    assert done1.returncode == 0, done1.stdout + done1.stderr
    assert forge1.writes == [landed(1)]

    p2 = pull(1, "CLEAN", status=ctx("lanes/booked", "FAILURE"))
    forge2 = Forge(repository(p2))
    done2, summary2 = train(tmp_path, forge2)
    assert done2.returncode == 0, done2.stdout + done2.stderr
    assert forge2.writes == [landed(1)]


def test_planted_armed_pr_held_by_required_status_is_disarmed(tmp_path):
    """An armed PR held by an absent required status context is disarmed."""
    p = pull(
        1,
        "CLEAN",
        author={"login": "person"},
        autoMergeRequest={"enabledAt": OLD},
    )
    forge = Forge(repository(p))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)]
    assert "disarmed: status lanes/booked absent: held" in summary


def test_behind_pr_not_armed_or_updated_when_required_status_absent(tmp_path):
    """A BEHIND PR without the required commit status context is not armed or updated."""
    p = pull(1, "BEHIND")
    forge = Forge(repository(p, rules=()))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "status lanes/booked absent: held" in summary
    assert "| #1 | BEHIND | status lanes/booked absent: held |" in summary


def test_check_required_status_validates_and_strips(tmp_path):
    """The check_required_status helper strips whitespace and validates context names."""
    rules = load_rules(tmp_path)
    assert rules.check_required_status("") == ""
    assert rules.check_required_status("   ") == ""
    assert rules.check_required_status("lanes/booked") == "lanes/booked"
    assert rules.check_required_status("  lanes/booked  ") == "lanes/booked"
    assert rules.check_required_status("ci/circleci: build") == "ci/circleci: build"
    assert rules.check_required_status("has space") == "has space"
    assert rules.check_required_status("x" * 100) == "x" * 100
    with pytest.raises(ValueError, match="not a status context name"):
        rules.check_required_status("pipe|inside")
    with pytest.raises(ValueError, match="not a status context name"):
        rules.check_required_status("-lead")
    with pytest.raises(ValueError, match="not a status context name"):
        rules.check_required_status("x" * 101)


def test_required_status_with_spaces_end_to_end(tmp_path):
    """A status name with internal spaces holds while absent and lands on SUCCESS."""
    status_name = "ci/circleci: build"
    p_absent = pull(1, "CLEAN")
    forge_absent = Forge(repository(p_absent))
    done_absent, summary_absent = train(tmp_path, forge_absent, required_status=status_name)
    assert done_absent.returncode == 0, done_absent.stdout + done_absent.stderr
    assert forge_absent.writes == []
    assert f"status {status_name} absent: held" in summary_absent
    assert f"| #1 | CLEAN | status {status_name} absent: held |" in summary_absent

    p_success = pull(1, "CLEAN", status=ctx(status_name, "SUCCESS"))
    forge_success = Forge(repository(p_success))
    done_success, summary_success = train(tmp_path, forge_success, required_status=status_name)
    assert done_success.returncode == 0, done_success.stdout + done_success.stderr
    assert forge_success.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary_success


def test_required_status_hold_with_empty_nodes_reports_absent(tmp_path):
    """A pull request with no latest commit nodes reports absent."""
    rules = load_rules(tmp_path)
    p = pull(1, "CLEAN", latest={"nodes": []})
    conf = {"required_status": "lanes/booked"}
    assert rules.required_status_hold(p, conf) == "status lanes/booked absent: held"


def status_selection_paths(query: str) -> list[list[str]]:
    """Path of enclosing GraphQL fields for every 'status' selection."""
    tokens = re.findall(r"([A-Za-z0-9_]+(?:\s*:\s*[A-Za-z0-9_]+)?|\{|\}|\([^)]*\))", query)
    stack: list[str] = []
    last_word = ""
    paths = []
    for token in tokens[:2000]:
        if token.startswith("("):
            continue
        if token == "{":
            if last_word == "status":
                paths.append(stack[1:])
            stack.append(last_word)
            last_word = ""
        elif token == "}":
            if stack:
                stack.pop()
        else:
            last_word = token.split(":")[0].strip()
    return paths


def test_query_includes_status_contexts(tmp_path):
    """The GraphQL pull query requests commit status contexts only when required_status is set."""
    rules = load_rules(tmp_path)
    expected_path = ["repository", "pullRequests", "nodes", "latest", "nodes", "commit"]
    set_query = rules.pull_query({"required_status": "lanes/booked"})
    assert status_selection_paths(set_query) == [expected_path]
    assert "$status: String!" in set_query
    unset_query = rules.pull_query({})
    assert status_selection_paths(unset_query) == []
    assert status_selection_paths(rules.QUERY) == []
    assert "$status" not in rules.QUERY


def test_without_the_required_status_hold_a_held_pull_request_lands(tmp_path):
    """Planted: without the required status hold, a pull request lacking the status lands."""
    mutant = module().replace(
        "    status_hold = required_status_hold(pull, conf)\n"
        "    if status_hold:\n"
        "        return status_hold",
        "    pass",
    )
    assert mutant != module()
    p = pull(1, "CLEAN")
    forge = Forge(repository(p))
    done, _ = train(tmp_path, forge, helper=mutant, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


def test_required_status_set_drifted_armed_pull_disarmed_without_re_arming(tmp_path):
    """With required_status set, a retitled armed pull request is disarmed and not armed again."""
    drifted_pull = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
        status=ctx("lanes/booked", "SUCCESS"),
    )
    forge = Forge(repository(drifted_pull))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)], forge.writes
    assert not any(w[0] == "arm" for w in forge.writes)
    assert "re-armed" not in summary, summary
    assert "disarmed: title changed after arming, the train lands it directly" in summary, summary


def test_required_status_held_drifted_armed_pull_disarmed_once(tmp_path):
    """A retitled armed pull request the absent status holds is disarmed once, for the status."""
    drifted_pull = pull(
        1,
        "BLOCKED",
        title="chore(deps): update tool to v2",
        autoMergeRequest=armed_squash("chore(deps): update tool to v1 (#1)"),
    )
    forge = Forge(repository(drifted_pull))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)], forge.writes
    assert "disarmed: status lanes/booked absent: held" in summary, summary
    assert "title changed after arming" not in summary, summary


def test_required_status_token_rights(tmp_path):
    """Statuses read joins the minted rights only when required_status is set, switched off too."""
    forge1 = Forge(repository(pull(1, "CLEAN")))
    done1, _ = train(tmp_path, forge1)
    assert done1.returncode == 0, done1.stdout + done1.stderr
    assert forge1.minted[1] == (7, {"permissions": WRITE, "repositories": ["app"]})
    assert not any("status" in q for q in forge1.queries if "pullRequests" in q)

    forge2 = Forge(repository(pull(1, "CLEAN", status=ctx("lanes/booked", "SUCCESS"))))
    done2, _ = train(tmp_path, forge2, required_status="lanes/booked")
    assert done2.returncode == 0, done2.stdout + done2.stderr
    assert forge2.minted[1] == (
        7,
        {"permissions": {**WRITE, **STATUSES}, "repositories": ["app"]},
    )
    assert any("status {" in q for q in forge2.queries if "pullRequests" in q)

    forge3 = Forge(repository(pull(1, "CLEAN", status=ctx("lanes/booked", "SUCCESS"))))
    done3, _ = train(tmp_path, forge3, switch="off", required_status="lanes/booked")
    assert done3.returncode == 0, done3.stdout + done3.stderr
    assert forge3.minted[1] == (
        7,
        {"permissions": {**READ, **STATUSES}, "repositories": ["app"]},
    )


def test_forge_refuses_status_read_without_statuses_rights(tmp_path):
    """Forge refuses status read for a token without statuses rights, failing the run."""
    source = SCRIPT.read_text(encoding="utf-8")
    mutant = source.replace("rights = {**rights, **STATUS_RIGHTS}", "pass")
    assert mutant != source
    p = pull(1, "CLEAN", status=ctx("lanes/booked", "SUCCESS"))
    forge = Forge(repository(p))
    done, _ = train(tmp_path, forge, text=mutant, required_status="lanes/booked")
    assert done.returncode != 0
    assert "Resource not accessible by integration" in done.stdout + done.stderr


def test_required_status_set_behind_pull_updated_without_arming(tmp_path):
    """When required_status is set, a behind pull request is updated but never armed."""
    p_set = pull(
        1,
        "BEHIND",
        author={"login": "person"},
        status=ctx("lanes/booked", "SUCCESS"),
    )
    forge_set = Forge(repository(p_set, rules=()))
    done_set, summary_set = train(tmp_path, forge_set, required_status="lanes/booked")
    assert done_set.returncode == 0, done_set.stdout + done_set.stderr
    assert forge_set.writes == [updated(1)]
    assert not any(w[0] == "arm" for w in forge_set.writes)
    assert "updated from the default branch" in summary_set

    # Unset same pull -> armed + updated as before
    p_unset = pull(
        1,
        "BEHIND",
        author={"login": "person"},
        status=ctx("lanes/booked", "SUCCESS"),
    )
    forge_unset = Forge(repository(p_unset, rules=()))
    done_unset, summary_unset = train(tmp_path, forge_unset)
    assert done_unset.returncode == 0, done_unset.stdout + done_unset.stderr
    assert forge_unset.writes == [
        ("arm", "PR_1", f"{1:040x}", "SQUASH"),
        updated(1),
    ]
    assert "armed, updated from the default branch" in summary_unset


@pytest.mark.parametrize("auto_merge", [True, False], ids=["auto-merge-on", "auto-merge-off"])
def test_required_status_set_armed_behind_pull_disarmed_before_update(tmp_path, auto_merge):
    """While required_status is set an armed BEHIND pull request is disarmed, then updated.

    Auto-merge would otherwise land the new head before the train reads its status.
    Unset, the same pull request is updated and stays armed.
    """
    fields = {
        "author": {"login": "person"},
        "autoMergeRequest": {"enabledAt": OLD},
        "status": ctx("lanes/booked", "SUCCESS"),
    }
    forge = Forge(repository(pull(1, "BEHIND", **fields), rules=(), auto_merge=auto_merge))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1), updated(1)], forge.writes
    assert "| #1 | BEHIND | disarmed, updated from the default branch |" in summary, summary

    forge_unset = Forge(repository(pull(1, "BEHIND", **fields), rules=(), auto_merge=auto_merge))
    done_unset, summary_unset = train(tmp_path, forge_unset)
    assert done_unset.returncode == 0, done_unset.stdout + done_unset.stderr
    assert forge_unset.writes == [updated(1)], forge_unset.writes
    assert "| #1 | BEHIND | updated from the default branch |" in summary_unset, summary_unset


def test_required_status_set_keeps_unarmed_behind_pull_as_is_without_auto_merge(tmp_path):
    """Where auto-merge is off, an unarmed BEHIND pull request stays as it is (record 0044)."""
    behind = pull(1, "BEHIND", author={"login": "person"}, status=ctx("lanes/booked", "SUCCESS"))
    forge = Forge(repository(behind, auto_merge=False))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [], forge.writes
    assert "behind: auto-merge off, not armed, not updated" in summary, summary


def test_armed_pull_with_earlier_verdict_hold_and_absent_status_is_disarmed(tmp_path):
    """An armed pull request an earlier verdict holds is disarmed when its status is absent."""
    young_armed = pull(
        1,
        "CLEAN",
        headRefName="renovate/demo-dep",
        createdAt=NOW.isoformat(),
        autoMergeRequest={"enabledAt": OLD},
    )
    forge = Forge(repository(young_armed))
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)]
    assert "disarmed: status lanes/booked absent: held" in summary


def test_merge_queue_held_by_required_status(tmp_path):
    """A pull request in merge queue is dequeued if required status holds, untouched otherwise."""
    # queued, set, status absent -> DEQUEUE sent
    p1 = pull(1, "CLEAN", isInMergeQueue=True)
    forge1 = Forge(repository(p1, queue=True))
    done1, summary1 = train(tmp_path, forge1, required_status="lanes/booked")
    assert done1.returncode == 0, done1.stdout + done1.stderr
    assert forge1.writes == [dequeued(1)]
    assert "dequeued: status lanes/booked absent: held" in summary1

    # status SUCCESS -> none
    p2 = pull(1, "CLEAN", isInMergeQueue=True, status=ctx("lanes/booked", "SUCCESS"))
    forge2 = Forge(repository(p2, queue=True))
    done2, _ = train(tmp_path, forge2, required_status="lanes/booked")
    assert done2.returncode == 0, done2.stdout + done2.stderr
    assert forge2.writes == []

    # unset -> none
    p3 = pull(1, "CLEAN", isInMergeQueue=True)
    forge3 = Forge(repository(p3, queue=True))
    done3, _ = train(tmp_path, forge3)
    assert done3.returncode == 0, done3.stdout + done3.stderr
    assert forge3.writes == []


def test_dependency_pull_held_by_required_status_commits_generated_files(tmp_path):
    """A candidate dependency pull request held by required status commits regenerated files."""
    head = f"{1:040x}"
    dep_pull = pull(1, "CLEAN", headRefName="renovate/demo-dep")
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
    done, summary = train(tmp_path, forge, required_status="lanes/booked")
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(forge.writes) == 1
    assert forge.writes[0][0] == "commit"
    assert "### fleet/app: committed generated files to #1" in summary


# workflow_call inputs the job reads itself, not the train script. release_workflow feeds the
# "Start the release run" step, pinned by check_dispatch_wiring in tests/test_train_release_run.py.
JOB_RUNNER_INPUTS = {
    "train": "${{ inputs.runner }}",
}
INPUTS_OFF_TRAIN = frozenset({"runner", "release_workflow"})


def input_wiring_findings(doc: dict, source: str) -> list[str]:
    """Inputs missing from the train step env as TRAIN_<NAME>, or set there but never read."""
    inputs = doc["on"]["workflow_call"]["inputs"]
    steps = [s for s in doc["jobs"]["train"]["steps"] if s.get("name") == STEP]
    env = (steps[0].get("env") if steps else None) or {}
    pattern = r"""\benv(?:\s*\[\s*["']|\s*\.\s*get\s*\(\s*["'])(TRAIN_[A-Z0-9_]+)["']"""
    read = set(re.findall(pattern, source))
    found = []
    for job_name, expected_runs_on in JOB_RUNNER_INPUTS.items():
        actual = doc["jobs"].get(job_name, {}).get("runs-on")
        if actual != expected_runs_on:
            found.append(f"job {job_name} runs-on is {actual!r}, not {expected_runs_on!r}")
    for name in sorted(set(inputs) - INPUTS_OFF_TRAIN):
        key = f"TRAIN_{name.upper()}"
        if env.get(key) != f"${{{{ inputs.{name} }}}}":
            found.append(f"input {name} does not reach {key}")
        elif key not in read:
            found.append(f"{key} is set but the train never reads it")
    return found


def test_every_train_input_reaches_the_train_env():
    """Each workflow_call input but the runner inputs is passed to the train step and read."""
    doc = ciwf.load(TRAIN)
    assert INPUTS_OFF_TRAIN.issubset(doc["on"]["workflow_call"]["inputs"])
    assert input_wiring_findings(doc, SCRIPT.read_text(encoding="utf-8")) == []


def _train_env(doc: dict) -> dict:
    """The env mapping of the train step in doc."""
    return next(s for s in doc["jobs"]["train"]["steps"] if s.get("name") == STEP)["env"]


@pytest.mark.parametrize(
    ("mutator", "drop", "finding"),
    [
        (
            lambda doc: _train_env(doc).pop("TRAIN_REQUIRED_STATUS"),
            None,
            "input required_status does not reach TRAIN_REQUIRED_STATUS",
        ),
        (
            lambda doc: _train_env(doc).update(TRAIN_REQUIRED_STATUS="${{ inputs.switch }}"),
            None,
            "input required_status does not reach TRAIN_REQUIRED_STATUS",
        ),
        (
            lambda doc: doc["on"]["workflow_call"]["inputs"].update(extra={"type": "string"}),
            None,
            "input extra does not reach TRAIN_EXTRA",
        ),
        (
            lambda doc: doc["jobs"]["train"].update({"runs-on": "wrong"}),
            None,
            "job train runs-on is 'wrong', not '${{ inputs.runner }}'",
        ),
        (
            lambda doc: None,
            '"TRAIN_REQUIRED_STATUS"',
            "TRAIN_REQUIRED_STATUS is set but the train never reads it",
        ),
        (
            lambda doc: None,
            (
                'env.get("TRAIN_REQUIRED_STATUS", "")',
                '""  # comment only mentioning "TRAIN_REQUIRED_STATUS"',
            ),
            "TRAIN_REQUIRED_STATUS is set but the train never reads it",
        ),
    ],
)
def test_planted_input_wiring_mutants_are_found(mutator, drop, finding):
    """A dropped, rewired or new input, or an env name the train stops reading, is named."""
    doc = ciwf.load(TRAIN)
    mutator(doc)
    source = SCRIPT.read_text(encoding="utf-8")
    if isinstance(drop, tuple):
        source = source.replace(drop[0], drop[1])
    elif drop:
        source = source.replace(drop, '"GONE"')
    assert input_wiring_findings(doc, source) == [finding]


# Transient forge failures (decision record 0080): the k8s runs of 2026-11-12 went red on a
# dropped revocation (43203110717), a 502 on the jobs read (43251161246) and a merge refused
# after another merge moved the pull request (43245308767).


@pytest.mark.parametrize("fault", [502, 503, 504, DROP])
def test_transient_rest_read_is_tried_again(tmp_path, fault):
    """Planted: the jobs read fails once at the gateway or drops; the refresh still runs."""
    forge = stopped_forge()
    forge.rest_faults[JOBS_ROUTE] = [fault]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == refreshed(1)
    assert forge.rest_faults[JOBS_ROUTE] == []
    assert "### fleet/app: refreshed #1" in summary


@pytest.mark.parametrize("fault", [502, DROP])
def test_exhausted_rest_read_retries_refuse_the_repository(tmp_path, fault):
    forge = stopped_forge()
    forge.rest_faults[JOBS_ROUTE] = [fault] * 4
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "::error title=Merge train refused::fleet/app: GET " in done.stdout
    assert "Traceback" not in done.stderr
    assert forge.rest_faults[JOBS_ROUTE] == [fault]
    assert forge.writes == []


def test_exhausted_installation_read_refusal_stays_on_one_line(tmp_path):
    """Three gateway failures refuse the installation and keep both body lines in each report."""
    forge = Forge(repository(pull(1, "CLEAN")))
    failure = (502, MULTILINE_READ_ERROR)
    forge.rest_faults[INSTALLATION_REPOSITORIES_ROUTE] = [failure] * 4
    done, summary = train(tmp_path, forge)

    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.rest_faults[INSTALLATION_REPOSITORIES_ROUTE] == [failure]
    assert forge.writes == []
    prefix = "::error title=Merge train refused::"
    (annotation,) = [line for line in done.stdout.splitlines() if line.startswith(prefix)]
    refusal = annotation.removeprefix(prefix)
    assert re.fullmatch(
        r"fleet: GET \S+/installation/repositories\?\S+: HTTP 502 "
        + re.escape(FLATTENED_READ_ERROR),
        refusal,
    )
    assert [line for line in summary.splitlines() if line.startswith("- fleet: ")] == [
        f"- {refusal}"
    ]


def test_rest_read_500_is_not_tried_again(tmp_path):
    forge = stopped_forge()
    forge.rest_faults[JOBS_ROUTE] = [500, 500]
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "HTTP 500" in done.stdout
    assert forge.rest_faults[JOBS_ROUTE] == [500]


def test_without_the_read_retry_a_gateway_error_refuses(tmp_path):
    """Mutation: a GET that runs once turns the planted 502 on the jobs read red."""
    text = script()
    old = 'retries = READ_ATTEMPTS - 1 if method == "GET" else 0'
    assert text.count(old) == 1
    forge = stopped_forge()
    forge.rest_faults[JOBS_ROUTE] = [502]
    done, _ = train(tmp_path, forge, text.replace(old, "retries = 0"))
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []


@pytest.mark.parametrize(
    "fault", [DROP, 502, pytest.param((502, MULTILINE_READ_ERROR), id="multiline-502")]
)
def test_failed_revocation_warns_and_the_sweep_goes_on(tmp_path, fault):
    """Planted: the first revocation drops or fails; every repository is still served."""
    forge = sweep_forge()
    forge.rest_faults[REVOKE_ROUTE] = [fault]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.where == [
        ("fleet/app", landed(1)),
        ("other/b", armed(2)),
        ("other/b", updated(2)),
    ]
    warning = "::warning title=Merge train::token not revoked, it expires within the hour: DELETE "
    (line,) = [line for line in done.stdout.splitlines() if line.startswith(warning)]
    if isinstance(fault, tuple):
        assert line.endswith(f"HTTP 502 {FLATTENED_READ_ERROR}")
    assert "### fleet/app: landed #1" in summary
    # A revocation is a write: it runs once, so exactly one token stays unrevoked.
    assert len(forge.revoked) == len(forge.tokens) - 1


@pytest.mark.parametrize(("state", "code"), [("BEHIND", 0), ("UNKNOWN", 0), ("CLEAN", 1)])
def test_merge_refused_as_not_mergeable_reads_the_state_again(tmp_path, state, code):
    """Planted: the merge is refused as not mergeable; a moved pull request is a note."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = [NOT_MERGEABLE]
    forge.merge_states[1] = state
    done, summary = train(tmp_path, forge)
    assert done.returncode == code, done.stdout + done.stderr
    moved = f"### fleet/app: not landed: the merge state moved to {state} #1"
    assert (moved in summary) == (code == 0), summary
    assert ("Pull Request is not mergeable" in done.stdout) == (code == 1)


# Review of horistes PR 173: a cut body is transient, an enqueue refusal can be a move, and
# the moved-state read is tried again like every other GraphQL read.
ENQUEUE_NOT_MERGEABLE = {**NOT_MERGEABLE, "path": ["enqueuePullRequest"]}
CUT_CAUSE = "raise RefusedError(message) from cut"


def test_cut_read_answer_is_tried_again(tmp_path):
    """Planted: the page read answer stops halfway under its full length; the read runs again."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.graphql_cuts["read"] = 1
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert len(forge.read_calls) == 2
    assert forge.graphql_cuts["read"] == 0
    assert "### fleet/app: landed #1" in summary


def test_cut_mutation_answer_is_not_tried_again(tmp_path):
    """A merge whose answer is cut may have landed; it runs once and the repository is refused."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.graphql_cuts["mergePullRequest"] = 2
    done, _ = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "body cut:" in done.stdout
    assert forge.writes == [landed(1)]
    assert forge.graphql_cuts["mergePullRequest"] == 1


def test_cut_artifact_body_once_is_downloaded_again(tmp_path):
    """Planted: the first artifact download is cut; the second whole one is committed."""
    forge = cut_artifact_forge()
    forge.storage_cuts_left[101] = 1
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert any(w[0] == "commit" for w in forge.writes)
    assert "body cut" not in summary
    assert forge.storage_reads == [101, 101]
    assert fault_request_key(f"{STORAGE_ROUTE} [redirect]") in forge.request_ledger
    assert fault_request_key(STORAGE_ROUTE) in forge.request_ledger
    assert forge.storage_auth_seen == []


def test_without_the_cut_cause_a_cut_read_refuses(tmp_path):
    """Mutation: a cut refusal without its cause is not transient, so the planted read fails."""
    assert script().count(CUT_CAUSE) == 1
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.graphql_cuts["read"] = 1
    flow = script().replace(CUT_CAUSE, "raise RefusedError(message)")
    done, _ = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "body cut:" in done.stdout
    assert forge.writes == []


@pytest.mark.parametrize(("state", "code"), [("BEHIND", 0), ("UNKNOWN", 0), ("CLEAN", 1)])
def test_enqueue_refused_as_not_mergeable_reads_the_state_again(tmp_path, state, code):
    """Planted: the queue refuses the pull request as not mergeable; a moved one is a note."""
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    forge.enqueue_errors = [ENQUEUE_NOT_MERGEABLE]
    forge.merge_states[1] = state
    done, summary = train(tmp_path, forge)
    assert done.returncode == code, done.stdout + done.stderr
    assert forge.writes == [queued(1)]
    moved = f"### fleet/app: not landed: the merge state moved to {state} #1"
    assert (moved in summary) == (code == 0), summary
    assert ("Pull Request is not mergeable" in done.stdout) == (code == 1)


def test_without_the_enqueue_handler_a_moved_pull_request_refuses(tmp_path):
    """Mutation: an enqueue refusal that skips moved_state turns the BEHIND case red."""
    old = """        data = graphql(env, ENQUEUE, {"id": pull["id"], "oid": pull["headRefOid"]})
    except RefusedError as err:"""
    assert script().count(old) == 1
    flow = script().replace(old, old.replace("except RefusedError", "except ZeroDivisionError"))
    forge = Forge(repository(pull(1, "CLEAN"), queue=True))
    forge.enqueue_errors = [ENQUEUE_NOT_MERGEABLE]
    forge.merge_states[1] = "BEHIND"
    done, summary = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "not landed: the merge state moved" not in summary


@pytest.mark.parametrize("fault", [502, 503, 504])
def test_moved_state_read_is_tried_again(tmp_path, fault):
    """Planted: the merge is refused as not mergeable and the state read fails once."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = [NOT_MERGEABLE]
    forge.merge_states[1] = "BEHIND"
    forge.merge_state_statuses = [fault]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.merge_state_statuses == []
    assert "### fleet/app: not landed: the merge state moved to BEHIND #1" in summary


def test_exhausted_moved_state_reads_refuse_the_repository(tmp_path):
    """Boundary: three failed state reads refuse the repository; the fourth is never sent."""
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = [NOT_MERGEABLE]
    forge.merge_states[1] = "BEHIND"
    forge.merge_state_statuses = [502] * 4
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert re.search(r"fleet/app: graphql MergeState: .*HTTP 502", done.stdout)
    assert forge.merge_state_statuses == [502]
    assert "not landed: the merge state moved" not in summary


def test_without_the_read_wrapper_the_state_read_refuses(tmp_path):
    """Mutation: the moved-state read sent through graphql runs once and the 502 refuses."""
    old = "data = read_graphql(env, MERGE_STATE, {"
    assert script().count(old) == 1
    flow = script().replace(old, "data = graphql(env, MERGE_STATE, {")
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.merge_errors = [NOT_MERGEABLE]
    forge.merge_states[1] = "BEHIND"
    forge.merge_state_statuses = [502]
    done, _ = train(tmp_path, forge, flow)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "HTTP 502" in done.stdout


# Retry history is fresh relative to NOW, independent of the older head-suite fixtures.
def retry_suite(conclusion="FAILURE", *, when=None, workflow=1, number=1, app="github-actions"):
    node = suite(number, conclusion, 0, workflow=workflow, app=app)
    if app is None:
        node["app"] = None
    node["createdAt"] = (when or NOW - timedelta(hours=1)).isoformat()
    if workflow:
        node["workflowRun"]["workflow"]["name"] = f"workflow-{workflow}"
    return node


def retry_attempt(oid="a" * 40, suites=None):
    return {"oid": oid, "checkSuites": {"nodes": suites or []}}


def retry_history(number, *attempts, forced=()):
    return {
        "headRefOid": f"{number:040x}",
        "baseRefOid": "b" * 40,
        "commits": {"nodes": [{"commit": a} for a in attempts]},
        "timelineItems": {"nodes": [{"beforeCommit": a} for a in forced]},
    }


def retry_file(**changes):
    return {
        "filename": "app.py",
        "status": "modified",
        "patch": "@@ -1 +1 @@\n-old\n+new\n context",
        "sha": "c" * 40,
        **changes,
    }


def retry_modes(variables, modes=None):
    modes = modes or {}
    paths = [
        value for key, value in variables.items() if key.startswith("p") and ascii_digits(key[1:])
    ]
    return {
        side: {
            f"f{index}": {"mode": modes.get((variables[side], path), 33188)}
            for index, path in enumerate(paths)
        }
        for side in ("before", "after")
    }


def retry_forge(state="BLOCKED", *, follower=True, forced=False, **fields):
    first = pull(1, state, autoMergeRequest={"enabledAt": OLD}, suites=[None], **fields)
    followers = [pull(2, "BEHIND", autoMergeRequest={"enabledAt": OLD})] if follower else []
    forge = Forge(repository(first, *followers))
    attempt = retry_attempt(suites=[retry_suite()])
    forge.retry_histories[1] = (
        retry_history(1, forced=[attempt]) if forced else retry_history(1, attempt)
    )
    return forge


@pytest.mark.parametrize("forced", [False, True], ids=["commits", "force-push"])
def test_retry_hold_refreshed_red_in_flight_serves_green_follower(tmp_path, forced):
    """Without the fix the refreshed oldest head is armed and in flight, blocking #2."""
    forge = retry_forge(forced=forced)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    held = "held for retry: diff unchanged since aaaaaaaaaaaa failed workflow-1;"
    assert f"| #1 | BLOCKED | {held}" in summary
    assert forge.retry_calls == [1, 2]
    assert forge.compare_calls == ["a" * 40, f"{1:040x}"]


@pytest.mark.parametrize("forced", [False, True], ids=["commits", "force-push"])
def test_retry_hold_refreshed_red_behind_serves_green_follower(tmp_path, forced):
    """Without the fix the oldest BEHIND pull request is updated again."""
    forge = retry_forge("BEHIND", forced=forced)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    held = "held for retry: diff unchanged since aaaaaaaaaaaa failed workflow-1;"
    assert f"| #1 | BEHIND | {held}" in summary


def ordered_lease(*numbers, expired=False):
    until = NOW + timedelta(hours=-1 if expired else 1)
    stamp = until.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"until={stamp}; order={','.join(str(n) for n in numbers)}"


def test_ordered_lease_out_of_order_session_does_not_land(tmp_path):
    """At cc4167b the ordered description is refused; without a lease #7845 would land."""
    session = pull(7845, "CLEAN", author={"login": "session"})
    waiting = pull(7840, "BLOCKED", suites=[None])
    forge = Forge(repository(session, waiting, lease=ordered_lease(7840, 7845)))
    done, summary = train(tmp_path, forge, session_authors="session")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "#7840 is next" in summary
    assert "| #7845 | CLEAN | the lander holds the landing order until" in summary


@pytest.mark.parametrize(
    "change",
    [
        {"patch": "-old\n+different"},
        {"filename": "other.py"},
        {"previous_filename": "before.py"},
        {"status": "renamed"},
        {"patch": None, "sha": "d" * 40},
    ],
    ids=["content", "filename", "rename", "status", "binary"],
)
def test_retry_hold_changed_own_diff_serves_oldest(tmp_path, change):
    forge = retry_forge("BEHIND")
    forge.comparisons[f"{1:040x}"] = (200, {"files": [retry_file(**change)]})
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert "held for retry:" not in summary


def test_retry_hold_ignores_context_hunk_offsets_and_file_order(tmp_path):
    forge = retry_forge()
    a = retry_file(filename="a.py")
    b = retry_file(filename="b.py", patch=None)
    moved = {**a, "patch": "@@ -100 +200 @@\n other context\n-old\n+new"}
    forge.comparisons["a" * 40] = (200, {"files": [a, b]})
    forge.comparisons[f"{1:040x}"] = (200, {"files": [b, moved]})
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    assert "held for retry: diff unchanged" in summary


@pytest.mark.parametrize("age", [7, 8])
def test_retry_hold_old_failure_is_released(tmp_path, age):
    forge = retry_forge("BEHIND")
    attempt = retry_attempt(suites=[retry_suite(when=NOW - timedelta(days=age))])
    forge.retry_histories[1] = retry_history(1, attempt)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.compare_calls == []
    assert "held for retry:" not in summary


def retry_reader(tmp_path, monkeypatch, history):
    mod = load_train(tmp_path, monkeypatch)

    def read(env, query, variables):
        node = (
            retry_modes(variables) if "fragment M on Commit" in query else {"pullRequest": history}
        )
        return {"repository": node}

    monkeypatch.setattr(mod, "read_graphql", read)
    monkeypatch.setattr(mod, "call", lambda *args: {"files": [retry_file()]})
    conf = {"security": frozenset({"security"})}
    env = {"GITHUB_REPOSITORY": "fleet/app", "GITHUB_API_URL": "fixture", "TRAIN_TOKEN": "fixture"}
    return mod, conf, env


@pytest.mark.parametrize("seconds,held", [(-1, True), (0, False), (1, False)])
def test_retry_hold_exact_seven_day_boundary(tmp_path, monkeypatch, seconds, held):
    stamp = NOW - timedelta(days=7)
    history = retry_history(1, retry_attempt(suites=[retry_suite(when=stamp)]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    hold = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW + timedelta(seconds=seconds))
    assert bool(hold) is held
    if held:
        assert hold.endswith("until " + NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))


@pytest.mark.parametrize("exempt", ["urgent", "security"])
def test_retry_hold_exempt_pull_has_no_history_reads(tmp_path, exempt):
    labels = ["merge-train: urgent" if exempt == "urgent" else "security"]
    forge = retry_forge("BEHIND", labels=labels)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == []
    assert forge.compare_calls == []
    assert "held for retry:" not in summary


@pytest.mark.parametrize("state", ["BLOCKED", "BEHIND"])
def test_retry_hold_alone_retries_with_release_note(tmp_path, state):
    forge = retry_forge(state, follower=False)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([updated(1)] if state == "BEHIND" else [])
    assert "retry hold released: no other pull request to serve (held for retry:" in summary
    assert forge.retry_calls == [1]


@pytest.mark.parametrize("kind", ["green-head", "green-forced", "superseded-failure"])
def test_retry_hold_latest_completed_green_releases(tmp_path, kind):
    forge = retry_forge("BEHIND")
    failed = retry_attempt(suites=[retry_suite()])
    success = retry_suite("SUCCESS", when=NOW - timedelta(minutes=1), number=2)
    green = retry_attempt("d" * 40, [success])
    if kind == "green-forced":
        history = retry_history(1, failed, forced=[green])
    elif kind == "superseded-failure":
        history = retry_history(1, retry_attempt(suites=[retry_suite(), success]))
    else:
        history = retry_history(1, green, failed)  # Timestamps, not the list order, decide.
    forge.retry_histories[1] = history
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.compare_calls == []
    assert "held for retry:" not in summary


def test_retry_hold_incomplete_attempt_does_not_erase_failure(tmp_path):
    forge = retry_forge()
    failed = retry_attempt(suites=[retry_suite()])
    pending = retry_attempt("d" * 40, [retry_suite(None, when=NOW, number=2)])
    forge.retry_histories[1] = retry_history(1, failed, pending, forced=[None])
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    assert "failed workflow-1; until" in summary


def set_retry_fault(forge, kind):
    history = forge.retry_histories[1]
    if kind == "graphql":
        forge.retry_refusals[1] = "retry history refused"
    elif kind == "compare":
        forge.comparisons["a" * 40] = (403, {"message": "compare refused"})
    elif kind == "suites":
        history["commits"]["nodes"][0]["commit"]["checkSuites"]["nodes"] = [retry_suite()] * 30
    elif kind == "head":
        history["headRefOid"] = "d" * 40
    elif kind == "files":
        forge.comparisons["a" * 40] = (200, {"files": [retry_file()] * 300})


@pytest.mark.parametrize(
    "kind,cause",
    [
        ("graphql", "retry history refused"),
        ("compare", "HTTP 403"),
        ("suites", "30-suite page"),
        ("head", "head moved"),
        ("files", "300 files"),
    ],
)
@pytest.mark.parametrize("follower", [True, False], ids=["with-follower", "alone"])
def test_retry_hold_unreadable_holds_or_retries_alone(tmp_path, kind, cause, follower):
    forge = retry_forge("BEHIND", follower=follower)
    set_retry_fault(forge, kind)
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2 if follower else 1)]
    assert "held for retry: history unreadable:" in summary
    assert cause in summary
    assert ("retry hold released:" in summary) is not follower


def pull_note_cell(text, number, state):
    """Read the notes cell of one physical row with three Markdown cells."""
    (row,) = [line for line in text.splitlines() if line.startswith(f"| #{number} |")]
    cells = re.split(r"(?<!\\)\|", row)
    assert len(cells) == 5, row
    assert cells[:3] == ["", f" #{number} ", f" {state} "], row
    assert cells[-1] == "", row
    return cells[3].strip()


def test_artifact_list_refusal_stays_in_one_notes_cell(tmp_path):
    """All three artifact-list reads return a short two-line gateway failure."""
    dep = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep))
    forge.set_allowlist("fleet/app", "main", ["notices.txt"])
    forge.rest_faults[ARTIFACTS_ROUTE] = [(502, b"first line\r\nsecond line")] * 3
    done, summary = train(tmp_path, forge)

    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.rest_faults[ARTIFACTS_ROUTE] == []
    assert forge.writes == []
    for text in (summary, done.stdout):
        note = pull_note_cell(text, 1, "BLOCKED")
        assert note.startswith("generated files refused: GET ")
        assert note.endswith("HTTP 502 first line second line")


def test_review_read_refusal_stays_in_one_notes_cell(tmp_path):
    """A multiline gateway failure holds the reviewed pull and serves its follower."""
    first = pull(1, "CLEAN", author={"login": "person"})
    second = pull(2, "CLEAN", author={"login": "person"})
    forge = Forge(repository(first, second))
    forge.rest_faults["ReviewBodies"] = [(502, MULTILINE_READ_ERROR)] * 3
    done, summary = train(tmp_path, forge)

    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.rest_faults["ReviewBodies"] == []
    assert forge.writes == [landed(2)]
    for text in (summary, done.stdout):
        note = pull_note_cell(text, 1, "CLEAN")
        assert note.startswith("review body: cannot read: graphql ReviewBodies: POST ")
        assert note.endswith(f"HTTP 502 {FLATTENED_READ_ERROR}")


def test_retry_read_refusal_stays_in_one_notes_cell(tmp_path):
    """A multiline history failure holds the retry and lets the follower update."""
    forge = retry_forge("BEHIND")
    forge.rest_faults["RetryHistory"] = [(502, MULTILINE_READ_ERROR)] * 3
    done, summary = train(tmp_path, forge)

    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.rest_faults["RetryHistory"] == []
    assert forge.writes == [updated(2)]
    for text in (summary, done.stdout):
        note = pull_note_cell(text, 1, "BEHIND")
        assert note.startswith("held for retry: history unreadable: graphql RetryHistory: POST ")
        assert note.endswith(f"HTTP 502 {FLATTENED_READ_ERROR}")


FORGE_FAULT_BODY = b"first line\r\nsecond line"
FORGE_FAULT_LINE = "first line second line"
ALLOWLIST_ROUTE = "GET /repos/{owner}/{repo}/contents/.github/generated-files"
ARCHIVE_ROUTE = "GET /repos/{owner}/{repo}/actions/artifacts/{id}/zip"
STORAGE_ROUTE = "GET /storage/{id}"
STORAGE_CASE = f"{STORAGE_ROUTE} [redirect]"
FILE_CONTENTS_ROUTE = "GET /repos/{owner}/{repo}/contents/{path}"
COMPARE_ROUTE = "GET /repos/{owner}/{repo}/compare/{base}...{head}"
PERMISSION_ROUTE = "GET /repos/{owner}/{repo}/collaborators/{login}/permission"
RELEASE_NAME_ROUTE = "GET /repos/{owner}/{repo}"
RELEASE_ID_ROUTE = "GET /repositories/{id}"
RELEASE_RENAMED_CASE = f"{RELEASE_NAME_ROUTE} [redirect]"
RELEASE_ID_CASE = f"{RELEASE_ID_ROUTE} [redirect]"
LIST_INSTALLATIONS_ROUTE = "GET /app/installations"
MINT_ROUTE = "POST /app/installations/{id}/access_tokens"
ISSUE_COMMENT_ROUTE = "POST /repos/{owner}/{repo}/issues/{id}/comments"
ISSUE_CLOSE_ROUTE = "PATCH /repos/{owner}/{repo}/issues/{id}"
ADD_LABEL_ROUTE = "POST /repos/{owner}/{repo}/issues/{id}/labels"
REMOVE_LABEL_ROUTE = "DELETE /repos/{owner}/{repo}/issues/{id}/labels/{label}"

FORGE_ISSUED_URLS = {
    ("download_artifact", "art['archive_download_url']"): (ARCHIVE_ROUTE,),
    ("download_artifact", "redirect.get('location')"): (STORAGE_CASE,),
    ("release_off_repository_id", "answer['location']"): (RELEASE_RENAMED_CASE, RELEASE_ID_CASE),
}
TRANSPORT_URL = {
    "call": (0, "url"),
    "pages": (2, "path"),
    "release_off_get": (0, "url"),
}
TRANSPORT_NAMES = {*TRANSPORT_URL, "request_once"}
OPENER_SETUP = ast.dump(
    ast.parse(
        "forge_opener = urllib.request.build_opener() if redirect "
        "else urllib.request.build_opener(NoRedirect)"
    ).body[0]
)
TRANSPORT_FORWARDS = {
    ("pages", "call"): (
        '''f"{env['GITHUB_API_URL']}{path}?per_page=100&page={page}"''',
        {"env": "parameter", "path": "parameter", "page": "loop"},
    ),
    ("release_off_get", "call"): ("url", {"url": "parameter"}),
    ("call", "request_once"): ("url", {"url": "parameter"}),
    ("request_once", "urllib.request.Request"): ("url", {"url": "parameter"}),
    ("request_once", "forge_opener.open"): ("request", {"url": "parameter"}),
    ("graphql", "call"): ("env['GITHUB_GRAPHQL_URL']", {"env": "parameter"}),
}
URL_FRAGMENTS = {
    "api": "",
    "env['GITHUB_API_URL']": "",
    "env['GITHUB_REPOSITORY']": "~/~",
    "target['full_name']": "~/~",
    "urllib.parse.quote(entry, safe='/')": "~/~",
}


# These values occupy one dynamic route field. A request URL itself still needs a plain binding.
URL_RUNTIME_FIELDS = {
    "apply_dependency_holds": {"number"},
    "close_issues": {"issue"},
    "filter_changed_files": {"path"},
}


def scoped_nodes(tree):
    """Yield nodes with their innermost definition, lambda or module."""
    scopes = {tree: tree}
    for node in ast.walk(tree):
        scope = scopes[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            scope = node
        yield scope, node
        scopes.update(dict.fromkeys(ast.iter_child_nodes(node), scope))


def parent_nodes(tree):
    """Index each AST child by its immediate parent."""
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def top_level_functions(tree):
    """Index the module's synchronous functions by name."""
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def call_argument(node, index, name, *, default=None):
    """Find a positional or keyword argument on a direct call."""
    if index < len(node.args):
        return node.args[index]
    return next((arg.value for arg in node.keywords if arg.arg == name), default)


def names_bound_by(node):
    """Include both explicit name targets and bindings attached to other AST nodes."""
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return node.names
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [name.asname or name.name.split(".")[0] for name in node.names]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.name]
    return []


def url_binding_sites(tree):
    """Index every binding or deletion by name and its enclosing definition."""
    parents = parent_nodes(tree)
    scopes = {}
    sites = {}
    for scope, node in scoped_nodes(tree):
        scopes[node] = scope
        if scope is node and node in parents:
            scope = scopes[parents[node]]
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            parent = parents[node]
            binding = (
                parent if isinstance(parent, ast.Assign) and parent.targets == [node] else node
            )
            sites.setdefault(scope, {}).setdefault(node.id, []).append(binding)
        for name in names_bound_by(node):
            sites.setdefault(scope, {}).setdefault(name, []).append(node)
    return sites


def url_binding(name, sites):
    """Allow one single-target plain assignment whose value precedes the name's use."""
    bindings = sites.get(name.id, [])
    if not bindings:
        return None
    first, *others = sorted(bindings, key=lambda node: (node.lineno, node.col_offset))
    if not others and isinstance(first, ast.arg):
        return None  # Function parameters stay opaque inside f-string fields.
    if (
        not others
        and isinstance(first, ast.Assign)
        and (first.end_lineno, first.end_col_offset) <= (name.lineno, name.col_offset)
    ):
        return first.value
    bad = others[0] if others else first
    raise AssertionError(f"{SCRIPT}:{bad.lineno}: unsupported URL binding: {name.id}")


def expand_url_part(node, bindings, opaque=()):
    """Expand a local name or an f-string by one level, leaving runtime values opaque."""
    if isinstance(node, ast.JoinedStr):
        return node.values
    if isinstance(node, ast.FormattedValue):
        return [node.value]
    if (
        isinstance(node, ast.Name)
        and node.id not in opaque
        and (value := url_binding(node, bindings)) is not None
    ):
        return [value]
    return [node]


def url_expression(url, bindings):
    """Resolve an argument's local name to its defining expression."""
    for _ in range(len(bindings) + 1):
        (expression,) = expand_url_part(url, bindings) if isinstance(url, ast.Name) else [url]
        if expression is url:
            return url
        url = expression
    pytest.fail(f"cyclic transport URL at {SCRIPT}:{url.lineno}: {ast.unparse(url)}")


def source_path(url, bindings, opaque):
    """Fill f-string fields with markers and preserve literal path components."""
    parts = [url]
    for _ in range(3 * len(bindings) + 4):
        expanded = [part for node in parts for part in expand_url_part(node, bindings, opaque)]
        if expanded == parts:
            return "".join(
                str(part.value)
                if isinstance(part, ast.Constant)
                else URL_FRAGMENTS.get(ast.unparse(part), "~")
                for part in parts
            )
        parts = expanded
    pytest.fail(f"unresolved REST URL at {SCRIPT}:{url.lineno}: {ast.unparse(url)}")


def rest_call_routes(name, called, node, url, bindings):
    """Accept only URLs derived from literal, formatted or declared forge values."""
    expression = url_expression(url, bindings)
    issued = FORGE_ISSUED_URLS.get((name, ast.unparse(expression)))
    if issued:
        return issued
    assert isinstance(expression, ast.JoinedStr) or (
        isinstance(expression, ast.Constant) and isinstance(expression.value, str)
    ), f"{SCRIPT}:{url.lineno}: unsupported {called} URL: {ast.unparse(expression)}"
    method = call_argument(node, 2, "method", default=ast.Constant("POST"))
    assert called != "call" or isinstance(method, ast.Constant), (
        f"{SCRIPT}:{node.lineno}: unsupported transport method: {ast.unparse(method)}"
    )
    try:
        route = rest_route(
            method.value if called == "call" else "GET",
            source_path(expression, bindings, URL_RUNTIME_FIELDS.get(name, ())),
            source=True,
        )
    except AssertionError as failure:
        raise AssertionError(f"{SCRIPT}:{url.lineno}: {failure}") from failure
    return (route,)


def transport_reference(node, parent):
    """Identify transport references and reject aliases of the underlying urllib module."""
    if isinstance(node, ast.Name) and node.id in TRANSPORT_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and (
        node.attr in TRANSPORT_NAMES
        or (isinstance(node.value, ast.Name) and node.value.id == "forge_opener")
    ):
        return ast.unparse(node)
    if isinstance(node, ast.Name) and node.id == "forge_opener":
        if isinstance(parent, ast.Attribute) and parent.attr == "open":
            return None
        return node.id
    if (
        isinstance(node, ast.Name)
        and node.id == "urllib"
        and (
            not isinstance(parent, ast.Attribute)
            or parent.attr not in {"error", "parse", "request"}
        )
    ):
        return node.id
    if isinstance(node, ast.Attribute) and ast.unparse(node) == "urllib.request":
        if not isinstance(parent, ast.Attribute):
            return "urllib.request"
        return None
    if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "urllib.request":
        return ast.unparse(node)
    return None


def allowed_opener_use(name, ref, node, parents, tree):
    """Allow only the production request adapter's opener construction and handler class."""
    parent = parents.get(node)
    if ref == "urllib.request.HTTPRedirectHandler":
        return (
            isinstance(parent, ast.ClassDef)
            and parent.name == "NoRedirect"
            and parent in tree.body
            and node in parent.bases
        )
    if name != "request_once":
        return False
    if ref == "forge_opener" and isinstance(node.ctx, ast.Store):
        return isinstance(parent, ast.Assign) and ast.dump(parent) == OPENER_SETUP
    if (
        ref == "urllib.request.build_opener"
        and isinstance(parent, ast.Call)
        and parent.func is node
    ):
        branch = parents[parent]
        return isinstance(branch, ast.IfExp) and ast.dump(parents[branch]) == OPENER_SETUP
    return False


def transport_calls(tree):
    """Yield every direct transport call and refuse other transport references."""
    parents = parent_nodes(tree)
    top = set(top_level_functions(tree).values())
    for scope, node in scoped_nodes(tree):
        name = scope.name if scope in top else "<non-top-level>"
        parent = parents.get(node)
        if isinstance(node, ast.Import):
            assert not any(
                alias.asname and alias.name in {"urllib", "urllib.request"} for alias in node.names
            ), f"{SCRIPT}:{node.lineno}: urllib request import alias"
        if isinstance(node, ast.ImportFrom):
            assert node.module != "urllib.request" and (
                node.module != "urllib"
                or all(alias.name not in {"request", "*"} for alias in node.names)
            ), f"{SCRIPT}:{node.lineno}: urllib request import alias"
        ref = transport_reference(node, parent)
        if ref is None or allowed_opener_use(name, ref, node, parents, tree):
            continue
        assert (
            isinstance(parent, ast.Call)
            and parent.func is node
            and not isinstance(scope, (ast.Lambda, ast.ClassDef))
        ), f"{SCRIPT}:{node.lineno}: unsupported transport reference: {ref}"
        yield scope, name, ref, parent


def forwarding_url(ref, operand, bindings):
    """Resolve the opener's request object to the URL in its single constructor binding."""
    if ref != "forge_opener.open":
        return operand
    assert isinstance(operand, ast.Name) and operand.id == "request", (
        f"{SCRIPT}:{operand.lineno}: unsupported forwarding request: {ast.unparse(operand)}"
    )
    request = url_binding(operand, bindings)
    assert (
        isinstance(request, ast.Call) and ast.unparse(request.func) == "urllib.request.Request"
    ), f"{SCRIPT}:{operand.lineno}: unsupported forwarding request binding: {operand.id}"
    return call_argument(request, 0, "url")


def forwarding_loop_target(binding, url, parents):
    """Recognize the simple for target whose body contains the forwarding expression."""
    loop = parents.get(binding)
    return (
        isinstance(loop, ast.For)
        and loop.target is binding
        and any(url is item for statement in loop.body for item in ast.walk(statement))
    )


def check_forwarding_name(name, url, kind, parameters, body_bindings, parents):
    """Check a declared parameter or the single for target that may supply a URL name."""
    sites = body_bindings[name.id]
    if kind == "parameter":
        expected, allowed = None, name.id in parameters
    else:
        assert kind == "loop", kind
        expected = next(
            (site for site in sites if forwarding_loop_target(site, url, parents)), None
        )
        allowed = expected is not None and name.id not in parameters
    unexpected = next((site for site in sites if site is not expected), None)
    bad = unexpected if unexpected is not None else name
    assert allowed and unexpected is None, (
        f"{SCRIPT}:{bad.lineno}: unsupported forwarding URL binding: {name.id}"
    )


def check_forwarding_site(scope, ref, operand, spec, bindings, parents):
    """Check all names in the forwarded expression before accepting its declared shape."""
    template, declared = spec
    url = forwarding_url(ref, operand, bindings.get(scope, {}))
    assert url is not None, f"{SCRIPT}:{operand.lineno}: missing forwarding URL"
    parameters = {
        part.arg for part in ast.iter_child_nodes(scope.args) if isinstance(part, ast.arg)
    }
    body = {item for statement in scope.body for item in ast.walk(statement)}
    body_bindings = {
        name: [
            site
            for scope_sites in bindings.values()
            for site in scope_sites.get(name, ())
            if site in body
        ]
        for name in declared
    }
    for part in ast.walk(url):
        if isinstance(part, ast.Name):
            assert part.id in declared, (
                f"{SCRIPT}:{part.lineno}: unsupported forwarding URL name: {part.id}"
            )
            check_forwarding_name(part, url, declared[part.id], parameters, body_bindings, parents)
    assert ast.unparse(operand) == template, (
        f"{SCRIPT}:{operand.lineno}: unsupported forwarding URL: {ast.unparse(operand)}"
    )


def rest_fault_routes(tree):
    """Derive REST routes independently of the stand-in forge and fault scenarios.

    The static forwarding inventory checks only name bindings and declared names,
    leaves writes through a declared name (subscript, augmented subscript, or attribute)
    to issue 269, and relies on the runtime transport allow-list to refuse requests
    outside the declared routes.
    After every faulted scenario the runtime allow-list checks its ledger, and the
    stand-in refuses by name a second Location in one response and a pending redirect
    when route normalization raises during dispatch.
    """
    bindings = url_binding_sites(tree)
    parents = parent_nodes(tree)
    routes = {}
    for scope, name, ref, node in transport_calls(tree):
        url = call_argument(node, *TRANSPORT_URL.get(ref, (0, "url")))
        forwarding = TRANSPORT_FORWARDS.get((name, ref))
        assert url is not None and (forwarding is not None or ref in TRANSPORT_URL), (
            f"{SCRIPT}:{node.lineno}: unsupported transport call: {ast.unparse(node)}"
        )
        if forwarding is not None:
            check_forwarding_site(scope, ref, url, forwarding, bindings, parents)
            continue
        for route in rest_call_routes(name, ref, node, url, bindings.get(scope, {})):
            routes.setdefault(route, []).append(name)
    return routes


def rest_fault_cases(tree):
    """Keep distinct call-site fault cases when direct routes are shared."""
    cases = set()
    for route, callers in rest_fault_routes(tree).items():
        if len(callers) == 1:
            cases.add(route)
        else:
            assert len(callers) == len(set(callers)), (route, callers)
            cases.update(f"{route} [{caller}]" for caller in callers)
    return cases


def test_forge_fault_inventory(tmp_path, monkeypatch):
    """Require a fault scenario for every source-derived REST route and GraphQL document."""
    rules = load_rules(tmp_path)
    train_mod = load_train(tmp_path, monkeypatch)
    docs = train_graphql_documents(train_mod, rules, file_modes_document(train_mod, monkeypatch))
    static_cases = rest_fault_cases(ast.parse(script())) | {
        train_mod.graphql_name(doc) for doc in docs
    }
    assert FAULT_SCENARIOS.keys() == static_cases


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("api_get = call", "unsupported transport reference: call"),
        ("alias = pages", "unsupported transport reference: pages"),
        ("send(release_off_get)", "unsupported transport reference: release_off_get"),
        ("def get(): return call", "unsupported transport reference: call"),
        ("functools.partial(call, '/app')", "unsupported transport reference: call"),
        ("def get(api): return api.call('/app')", r"unsupported transport call: api\.call"),
        ("run = lambda: call('/app')", "unsupported transport reference: call"),
        ("def get(url): return call(url)", "unsupported call URL: url"),
        ("from urllib import request as api", "urllib request import alias"),
        ("opener = urllib.request.build_opener()", r"unsupported transport call: urllib\.request"),
        ("urllib.request.urlopen('/app')", r"unsupported transport call: urllib\.request"),
        ("def get(): return forge_opener", "unsupported transport reference: forge_opener"),
    ],
)
def test_transport_allow_list_refuses_undeclared_uses(fragment, message):
    """Reject indirect transport references and forwarding through undeclared wrappers."""
    with pytest.raises(AssertionError, match=rf"merge_train.py:\d+: {message}"):
        rest_fault_routes(ast.parse(f"{fragment}\n"))


@pytest.mark.parametrize(
    "mutation",
    [
        'url += "/extra"',
        'if enabled: url = "/app"',
        'url: str = "/app"',
        '(url := "/app")',
        "for url in routes: pass",
        "with make_url() as url: pass",
        "try: setup()\n    except Error as url: pass",
        "url, extra = parts",
        "global url",
        "nonlocal url",
        "del url",
    ],
    ids=[
        "augmented",
        "branch",
        "annotated",
        "walrus",
        "for",
        "with",
        "except",
        "unpacking",
        "global",
        "nonlocal",
        "delete",
    ],
)
def test_transport_url_binding_refuses_rebinding(mutation):
    """Inventory refuses any second or non-plain binding by name and source line."""
    source = f'def get():\n    url = "/app"\n    {mutation}\n    return call(url, method="GET")\n'
    line = 4 if mutation.startswith("try:") else 3
    with pytest.raises(
        AssertionError, match=rf"merge_train.py:{line}: unsupported URL binding: url"
    ):
        rest_fault_routes(ast.parse(source))


def test_source_route_does_not_match_another_routes_placeholder():
    """Keep literal contents endpoints intact when deriving source coverage."""
    source = """def get(api, target):
    return call(f"{api}/repos/{target['full_name']}/contents/fixed.txt", method="GET")
"""
    route = "GET /repos/{owner}/{repo}/contents/fixed.txt"
    assert rest_fault_cases(ast.parse(source)) == {route}
    assert route not in FAULT_SCENARIOS


@pytest.mark.parametrize(
    ("statement", "name"),
    [
        pytest.param('path += "/extra"', "path", id="augmented"),
        pytest.param('path = path + "/x"', "path", id="assignment"),
        pytest.param('(path := path + "/x")', "path", id="walrus"),
        pytest.param("for path in (): pass", "path", id="for"),
        pytest.param("del path", "path", id="delete"),
        pytest.param("page = 1", "page", id="loop-rebinding"),
        pytest.param(None, "unlisted", id="undeclared-f-string-name"),
    ],
)
def test_transport_forwarding_rejects_undeclared_bindings(statement, name):
    """Forwarding URL names must come only from the declared binding kind."""
    source = script()
    if statement is None:
        old = "?per_page=100&page={page}"
        assert source.count(old) == 1
        planted = source.replace(old, "?per_page=100&page={unlisted}")
        line = next(
            line for line, text in enumerate(planted.splitlines(), 1) if "{unlisted}" in text
        )
    else:
        planted = plant_statement(source, "pages", statement)
        pages = top_level_functions(ast.parse(planted))["pages"]
        line = pages.body[1].lineno
    with pytest.raises(
        AssertionError,
        match=rf"merge_train.py:{line}: unsupported forwarding URL (?:binding|name): {name}",
    ):
        rest_fault_routes(ast.parse(planted))


def artifact_fault_forge():
    """A dependency update with a changed artifact and a live merge-state recheck."""
    dep = pull(1, "BLOCKED", headRefName="renovate/demo-dep")
    forge = Forge(repository(dep))
    regenerable(forge, dep)
    return forge


def pull_fault_forge(state="CLEAN", *, queue=False, **fields):
    """One pull request in a repository with optional merge queue support."""
    return Forge(repository(pull(1, state, **fields), queue=queue))


def issue_fault_forge():
    """A recent train merge links to an open issue."""
    forge = pull_fault_forge("BLOCKED")
    forge.merged_pulls["fleet/app"] = [merged_pull(5, (3, "OPEN"))]
    return forge


def dependency_hold_fault_forge():
    """A security update holds an armed dependency update."""
    return Forge(repository(*held_pair(), rules=LINEAR))


def unsigned_label_fault_forge():
    """An unsigned dependency update asks the update bot to recreate it."""
    forge = Forge(repository(unsigned_dep_pull(1, "CLEAN")))
    forge.commits[1] = [("chore(deps): update foo to v2", 1)]
    return forge


def reviewed_fault_forge():
    """A reviewed pull request and a clean follower can both be served."""
    return Forge(
        repository(*(pull(number, "CLEAN", author={"login": "person"}) for number in (1, 2)))
    )


def release_fault_forge(location="/repositories/7000"):
    """An old name redirects to metadata for a release-enabled repository."""
    forge = Forge(repository(pull(1, "CLEAN"), release=(True, True)))
    forge.repository_moves["/repos/former/app"] = location
    return forge


HELD_DRAFT_SCENARIO = functools.partial(
    pull_fault_forge, headRefName="renovate/go-snaps", isDraft=True, labels=(HOLD,)
)
RECREATE_LABEL_CASE = f"{ADD_LABEL_ROUTE} [_ask_update_bot_recreate]"

FAULT_SCENARIOS = {
    **dict.fromkeys(
        (
            "LanderLease",
            "Merge",
            "Merged",
            "Commits",
            "PullPage",
            "PullPageWithStatus",
            LIST_INSTALLATIONS_ROUTE,
            "GET /app",
            "GET /users/{login}",
            INSTALLATION_REPOSITORIES_ROUTE,
            MINT_ROUTE,
            REVOKE_ROUTE,
        ),
        pull_fault_forge,
    ),
    **dict.fromkeys(
        ("FileModes", "RetryHistory", COMPARE_ROUTE), functools.partial(retry_forge, "BEHIND")
    ),
    **dict.fromkeys(
        (
            ARTIFACTS_ROUTE,
            ALLOWLIST_ROUTE,
            ARCHIVE_ROUTE,
            STORAGE_CASE,
            FILE_CONTENTS_ROUTE,
            "CreateCommit",
            "MergeState",
        ),
        artifact_fault_forge,
    ),
    **dict.fromkeys(
        (
            JOBS_ROUTE,
            f"{ADD_LABEL_ROUTE} [toggle_refresh]",
            f"{REMOVE_LABEL_ROUTE} [toggle_refresh]",
        ),
        stopped_forge,
    ),
    **dict.fromkeys((ISSUE_COMMENT_ROUTE, ISSUE_CLOSE_ROUTE), issue_fault_forge),
    **dict.fromkeys(
        ("Arm", "PUT /repos/{owner}/{repo}/pulls/{id}/update-branch"),
        functools.partial(pull_fault_forge, "BEHIND"),
    ),
    **dict.fromkeys(
        ("Draft", f"{ADD_LABEL_ROUTE} [apply_dependency_holds]"), dependency_hold_fault_forge
    ),
    **dict.fromkeys(
        ("Ready", f"{REMOVE_LABEL_ROUTE} [apply_dependency_holds]"), HELD_DRAFT_SCENARIO
    ),
    "Dequeue": functools.partial(pull_fault_forge, queue=True, isInMergeQueue=True),
    "Disarm": functools.partial(
        pull_fault_forge, "BLOCKED", autoMergeRequest=armed_squash("change 1 (#1)")
    ),
    "Enqueue": functools.partial(pull_fault_forge, queue=True),
    "ReviewBodies": reviewed_fault_forge,
    PERMISSION_ROUTE: functools.partial(_maintainer_forge, comments=(_maintainer_answer(),)),
    RECREATE_LABEL_CASE: unsigned_label_fault_forge,
    **dict.fromkeys((RELEASE_NAME_ROUTE, RELEASE_ID_CASE), release_fault_forge),
    RELEASE_RENAMED_CASE: functools.partial(release_fault_forge, "/repos/fleet/app"),
}
FAULT_SETTINGS = {
    **dict.fromkeys(
        ("Dequeue", "Disarm", "PullPageWithStatus"), {"required_status": "lanes/booked"}
    ),
    **dict.fromkeys(
        (RELEASE_NAME_ROUTE, RELEASE_RENAMED_CASE, RELEASE_ID_CASE), {"release_off": "former/app"}
    ),
    RECREATE_LABEL_CASE: {"min_age_days": "0"},
}


def fault_request_key(case):
    """Translate a case ID to the forge's method, route and stage key."""
    route, _, variant = case.partition(" [")
    method, separator, path = route.partition(" ")
    if not separator:
        return "POST", f"/graphql [{case}]", "direct"
    return method, path, "redirect" if variant == "redirect]" else "direct"


def assert_declared_requests(case, forge, *, retry_key=None):
    """Refuse request keys outside the declared scenarios and this scenario's retry."""
    covered = {fault_request_key(scenario) for scenario in FAULT_SCENARIOS}
    if retry_key is not None:
        covered.add(retry_key)
    assert not (missing := forge.request_ledger - covered), f"{case}: no fault cases for {missing}"


def test_transport_runtime_refuses_forwarded_subscript_write(tmp_path):
    """A URL changed through a declared name is still subject to the runtime allow-list."""
    source = script()
    planted = plant_statement(source, "pages", 'env["GITHUB_API_URL"] += "/extra"')
    assert rest_fault_routes(ast.parse(planted)) == rest_fault_routes(ast.parse(source))
    forge = Forge(repository())
    key = ("GET", "/extra/app/installations", "direct")
    forge.rest_faults[key] = [(200, b"[]")]

    done, _ = train(tmp_path, forge, planted)

    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.request_ledger == {key}
    missing = {key}
    with pytest.raises(AssertionError, match=re.escape(f"no fault cases for {missing}")):
        assert_declared_requests("pages", forge)


def test_transport_runtime_refuses_faulted_forwarded_subscript_write(
    tmp_path, monkeypatch, capfd, record_property
):
    """Refuse a forwarded URL subscript write reached only while handling a delivered fault."""
    source = script()
    planted = plant_statement(
        source,
        "pages",
        'if env.get("PLANTED_FAULT_HANDLING"):\n    env["GITHUB_API_URL"] += "/extra"',
    )
    assert rest_fault_routes(ast.parse(planted)) == rest_fault_routes(ast.parse(source))
    path = tmp_path / "planted_train.py"
    path.write_text(planted, encoding="utf-8")
    follow_ups = []

    class FaultFollowUpHandler(Handler):
        """Serve the planted route only for this test."""

        def _dispatch_app(self, request_path):
            """Answer the planted GET without consuming another scenario fault."""
            if request_path != "/extra/app/installations":
                return super()._dispatch_app(request_path)
            follow_ups.append((self.command, request_path))
            self.answer(200, [])
            return True

    def load(path_to_run, patch):
        """Load the plant and make a follow-up only after pages receives its refusal."""
        mod = load_train(path_to_run, patch, source=path)
        pages = mod.pages

        def pages_with_follow_up(env, token, request_path, key=None):
            """Follow up on the refused read and preserve its original disposition."""
            try:
                return pages(env, token, request_path, key)
            except mod.RefusedError:
                pages({**env, "PLANTED_FAULT_HANDLING": True}, token, request_path, key)
                raise

        mod.pages = pages_with_follow_up
        return mod

    monkeypatch.setattr(sys.modules[__name__], "Handler", FaultFollowUpHandler)
    case = LIST_INSTALLATIONS_ROUTE
    missing = {("GET", "/extra/app/installations", "direct")}
    with pytest.raises(AssertionError, match=re.escape(f"{case}: no fault cases for {missing}")):
        run_forge_fault_sweep(tmp_path, monkeypatch, capfd, record_property, case, load=load)
        pytest.fail(f"faulted sweep accepted undeclared requests: {follow_ups}")
    assert follow_ups == [("GET", "/extra/app/installations")]


def fault_case_caller(case):
    """Identify the expected caller when several write sites use the same route."""
    _, _, variant = case.partition(" [")
    return variant.removesuffix("]") if variant and variant != "redirect]" else None


def graphql_fault_attempts(case, train_mod, patch, path):
    """Use the document's read policy and include both pull-page splits."""
    docs = train_graphql_documents(
        train_mod, load_rules(path), file_modes_document(train_mod, patch)
    )
    (document,) = (doc for doc in docs if train_mod.graphql_name(doc) == case)
    if document.lstrip().startswith("mutation"):
        return 1
    splits = 2 if case in {"PullPage", "PullPageWithStatus"} else 0
    return train_mod.READ_ATTEMPTS + splits


def observe_rest_fault_callers(train_mod, patch, route):
    """Record the immediate caller of each final refusal on a shared REST route."""
    call = train_mod.call
    callers = []

    def observed(url, token=None, method="POST", *args, **kwargs):
        """Forward the call and record the source function if the route is refused."""
        try:
            return call(url, token, method, *args, **kwargs)
        except train_mod.RefusedError:
            if rest_route(method, url) == route:
                callers.append(sys._getframe(1).f_code.co_name)
            raise

    patch.setattr(train_mod, "call", observed)
    return callers


def observe_sweep_refusals(train_mod, patch):
    """Capture the sweep's refusals before the summary and annotation render them."""
    sweep = train_mod.sweep
    refusals = []

    def observed(*args):
        """Forward the sweep result and record the entries it passes to the renderer."""
        result = sweep(*args)
        refusals.extend(result[2])
        return result

    patch.setattr(train_mod, "sweep", observed)
    return refusals


ANNOTATION_COMMAND = re.compile(r"::(?:error|warning|notice)\b")


def assert_one_line_fault_output(text):
    """Reject traces, broken fault bodies and inline or duplicated workflow commands."""
    assert "Traceback" not in text
    for line in text.splitlines():
        if "second line" in line:
            assert "first line" in line.partition("second line")[0], line
        if command := ANNOTATION_COMMAND.search(line):
            assert command.start() == 0 and line.count("::") == 2, line


@pytest.mark.parametrize(
    "line",
    [
        "note: second line",
        "| #1 | CLEAN | second line |",
        "- ::warning title=Merge train::in a report row",
        "::warning title=Merge train::first ::notice title=Again::second",
    ],
)
def test_fault_output_check_rejects_broken_lines(line):
    """The scanner rejects broken copies even after an intact diagnostic."""
    with pytest.raises(AssertionError):
        assert_one_line_fault_output(f"first line second line\n{line}")


def fault_destinations(env, captured):
    """Read every log and output destination from this sweep."""
    files = dict(zip(("summary", "output", "release"), REPORT_FILES, strict=True))
    paths = {name: Path(env[key]) for name, key in files.items() if key in env}
    return {
        "stdout": captured.out,
        "stderr": captured.err,
        **{
            name: path.read_text(encoding="utf-8") if path.exists() else ""
            for name, path in paths.items()
        },
    }


def assert_fault_diagnostics(destinations, **expected):
    """Require the complete expected diagnostic lines in each destination, and no extra copies."""
    for name, text in destinations.items():
        assert_one_line_fault_output(text)
        found = [line for line in text.splitlines() if "HTTP 502" in line or "first line" in line]
        assert found == expected.get(name, []), (name, found, expected)


def fault_reason(key, forge, origin):
    """Reconstruct the HTTP refusal from the last faulted request."""
    method, path = forge.fault_requests[-1]
    reason = f"{method} {origin}{path}: HTTP 502 {FORGE_FAULT_LINE}"
    return f"graphql {key}: {reason}" if path == "/graphql" else reason


FAULT_NOTES = {
    "LanderLease": ("CLEAN", "lander lease unreadable: RefusedError: "),
    "ReviewBodies": ("CLEAN", "review body: cannot read: "),
    "RetryHistory": ("BEHIND", "held for retry: history unreadable: "),
    "FileModes": ("BEHIND", "held for retry: history unreadable: "),
    COMPARE_ROUTE: ("BEHIND", "held for retry: history unreadable: "),
    PERMISSION_ROUTE: ("CLEAN", "review body: cannot read the right of maintainer: "),
    **dict.fromkeys(
        (ARTIFACTS_ROUTE, ARCHIVE_ROUTE, STORAGE_ROUTE, FILE_CONTENTS_ROUTE),
        ("BLOCKED", "generated files refused: "),
    ),
}
RELEASE_FAULTS = {RELEASE_NAME_ROUTE, RELEASE_RENAMED_CASE, RELEASE_ID_CASE}


def assert_note_disposition(key, forge, reason, destinations):
    """Verify a held pull or artifact note in both reports and the resulting writes."""
    state, prefix = FAULT_NOTES[key]
    note = prefix + reason
    row = f"| #1 | {state} | {note} |"
    lines = [row]
    expected_writes = []
    if key == "LanderLease":
        lines.insert(0, f"### fleet/app: {note}")
    elif key == "ReviewBodies":
        expected_writes = [landed(2)]
    elif key in {"RetryHistory", "FileModes", COMPARE_ROUTE}:
        expected_writes = [updated(2)]
    assert forge.writes == expected_writes
    assert_fault_diagnostics(destinations, stdout=lines, summary=lines)


def assert_fault_warning(key, forge, reason, destinations):
    """Verify report warning placement and that the sweep continued as intended."""
    prefix = "::warning title=Merge train::"
    assert forge.writes == [landed(1)]
    if key == REVOKE_ROUTE:
        warning = f"{prefix}token not revoked, it expires within the hour: {reason}"
        assert len(forge.revoked) == len(forge.tokens) - 1
        assert_fault_diagnostics(destinations, stdout=[warning])
    else:
        warning = (
            f"{prefix}release_off entry former/app: HTTP 502; release train off for installation 7"
        )
        assert "Release train off: fleet/app" in destinations["summary"].splitlines()
        assert len(forge.revoked) == len(forge.tokens)
        assert json.loads(destinations["release"])["releases"] == []
        assert_fault_diagnostics(destinations, stdout=[warning], summary=[warning])


def assert_fatal_fault(case, reason, refusals, destinations):
    """Check the one error annotation and the matching summary refusal when written."""
    prefix = "::error title=Merge train refused::"
    if case == LIST_INSTALLATIONS_ROUTE:
        assert refusals == []
        assert not any(destinations[name] for name in ("summary", "output", "release"))
        assert_fault_diagnostics(destinations, stdout=[prefix + reason])
        return
    (refusal,) = refusals
    assert refusal.splitlines() == [refusal]
    if case in {"PullPage", "PullPageWithStatus"}:
        operation, _, request = reason.partition(": ")
        initial = reason.removesuffix(" second line")
        reason = (
            f"{operation}: Split open pull request page to 6 after {initial}: "
            f"page size 6, cursor start: {request}"
        )
    account_faults = {"GET /app", "GET /users/{login}", INSTALLATION_REPOSITORIES_ROUTE, MINT_ROUTE}
    target = "fleet" if case in account_faults else "fleet/app"
    if case == RECREATE_LABEL_CASE:
        reason = f"#1: could not ask the update bot to recreate it: {reason}"
    assert refusal == f"{target}: {reason}"
    assert_fault_diagnostics(
        destinations, stdout=[f"- {refusal}", prefix + refusal], summary=[f"- {refusal}"]
    )


def run_fault_scenario(tmp_path, forge, case, train_mod, monkeypatch, capfd, caller=None):
    """Run a clean or faulted scenario with the same fake clock and transport."""
    settings = {**FAULT_SETTINGS.get(case, {}), "release_file": str(tmp_path / "releases.json")}
    with train_environment(tmp_path, forge, settings) as env:
        clock = types.SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _: None, time=time.time)
        monkeypatch.setattr(train_mod, "time", clock)
        opener = urllib.request.build_opener
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *handlers: opener(urllib.request.ProxyHandler({}), *handlers),
        )
        refusals = observe_sweep_refusals(train_mod, monkeypatch)
        route = case.partition(" [")[0]
        callers = observe_rest_fault_callers(train_mod, monkeypatch, route) if caller else []
        code = train_mod.main(env)
    return (
        code,
        refusals,
        callers,
        fault_destinations(env, capfd.readouterr()),
        env["GITHUB_API_URL"],
    )


def assert_fault_disposition(case, forge, code, refusals, output, origin):
    """Check the scenario's exit, writes, exact diagnostics and release destinations."""
    route = case.partition(" [")[0]
    reason = fault_reason(route, forge, origin)
    if route in FAULT_NOTES or case in RELEASE_FAULTS or case == REVOKE_ROUTE:
        assert code == 0 and refusals == [], output
        if route in FAULT_NOTES:
            assert_note_disposition(route, forge, reason, output)
        else:
            assert_fault_warning(case, forge, reason, output)
    else:
        assert code == 1, output
        assert_fatal_fault(case, reason, refusals, output)
    if case != LIST_INSTALLATIONS_ROUTE:
        (line,) = output["output"].splitlines()
        assert line.startswith("releases=")
        assert (
            json.loads(line.removeprefix("releases=")) == json.loads(output["release"])["releases"]
        )


def assert_faults_consumed(case, faults, retries, consumed, attempts):
    """Report any scenario fault that still has a response queued."""
    assert not faults and not retries and consumed == attempts, (
        f"{case}: consumed {consumed} of {attempts} faults"
    )


@pytest.mark.parametrize("case", sorted(FAULT_SCENARIOS))
def test_forge_fault_sweep(tmp_path, monkeypatch, capfd, record_property, case):
    """Require faults for every clean request, then exhaust this case's fault queue."""
    run_forge_fault_sweep(tmp_path, monkeypatch, capfd, record_property, case)


def run_forge_fault_sweep(tmp_path, monkeypatch, capfd, record_property, case, *, load=load_train):
    """Run both sides of a sweep with the same train source."""
    key = fault_request_key(case)
    clean = FAULT_SCENARIOS[case]()
    clean_path = tmp_path / "clean"
    clean_path.mkdir()
    with monkeypatch.context() as patch:
        train_mod = load(clean_path, patch)
        code, refusals, _, output, _ = run_fault_scenario(
            clean_path, clean, case, train_mod, patch, capfd
        )
    assert_declared_requests(case, clean)
    assert code == 0 and not refusals, output
    assert key in clean.request_ledger, f"{case}: clean run never reached {key}"

    forge = FAULT_SCENARIOS[case]()
    train_mod = load(tmp_path, monkeypatch)
    attempts = train_mod.READ_ATTEMPTS if key[0] == "GET" else 1
    if key[1].startswith("/graphql ["):
        attempts = graphql_fault_attempts(case, train_mod, monkeypatch, tmp_path)
    response = (502, FORGE_FAULT_BODY)
    faults = [response] * attempts
    retries = []
    retry_key = None
    if key[2] == "redirect":
        faults = [response]
        retries = [response] * (attempts - 1)
        retry_key = (*key[:2], "direct")
        if retry_key == fault_request_key(RELEASE_NAME_ROUTE):
            # The old name uses the same route template. Queue retries when it issues Location.
            queue_faults_after_location(forge, retry_key, retries)
        else:
            forge.rest_faults[retry_key] = retries
    forge.rest_faults[key] = faults
    caller = fault_case_caller(case)
    code, refusals, callers, output, origin = run_fault_scenario(
        tmp_path, forge, case, train_mod, monkeypatch, capfd, caller
    )
    assert_declared_requests(case, forge, retry_key=retry_key)
    consumed = len(forge.fault_requests)
    record_property("request_key", key)
    record_property("faults_consumed", consumed)
    assert_faults_consumed(case, faults, retries, consumed, attempts)
    assert callers == ([caller] if caller else [])
    assert_fault_disposition(case, forge, code, refusals, output, origin)


def test_report_escapes_pipe_in_notes_cell(tmp_path, monkeypatch):
    """A literal pipe in the train's note preserves the three table cells."""
    mod = load_train(tmp_path, monkeypatch)
    p = pull(1, "CLEAN")
    records = mod.report("fleet/app", repository(p), [(p, "land")], {1: "left | right"}, "done")
    assert pull_note_cell("\n".join(records), 1, "CLEAN") == r"left \| right"


@pytest.mark.parametrize("suffix", ["", " | tail"], ids=["newline", "newline-and-pipe"])
def test_pull_page_merge_state_stays_in_one_cell(tmp_path, suffix):
    """The pull page's multiline merge state renders in one row in both reports."""
    state = "DIRTY\r\nsecond line" + suffix
    forge = Forge(repository(pull(1, state)))
    done, summary = train(tmp_path, forge)

    assert done.returncode == 0, done.stdout + done.stderr
    expected = "DIRTY second line" + suffix.replace("|", r"\|")
    for text in (done.stdout, summary):
        assert pull_note_cell(text, 1, expected) == f"merge state {expected}"


def test_emit_writes_each_record_on_one_line(tmp_path, monkeypatch, capfd):
    """Both destinations flatten raw records and preserve account table cells."""
    mod = load_train(tmp_path, monkeypatch)
    summary_path = tmp_path / "summary.md"
    env = watch_report_files({"GITHUB_STEP_SUMMARY": str(summary_path)})
    accounts = {
        "active|account": {"repositories": 4, "landed": 3, "updated": 2, "refused": 1},
        "stopped|account": "suspended\r\nsecond cell |",
    }
    records = mod.summary(env, accounts, [], [], [], [], [], [])
    mod.emit(env, [*records, "note: first line\r\nsecond line", ""])
    stdout, stderr = capfd.readouterr()
    assert stderr == ""
    assert summary_path.read_text(encoding="utf-8") == stdout
    assert stdout.endswith("note: first line second line\n\n")
    assert r"| active\|account | 4 | 3 | 2 | 1 |" in stdout.splitlines()
    assert r"| stopped\|account | suspended second cell \| | | | |" in stdout.splitlines()


@pytest.mark.parametrize("queued", [False, True], ids=["immediate", "queued"])
def test_annotation_title_stays_on_one_line(tmp_path, monkeypatch, capfd, queued):
    """A multiline annotation title remains on the same line as its diagnostic."""
    mod = load_train(tmp_path, monkeypatch)
    records = [] if queued else None
    written = []
    if not queued:
        monkeypatch.setattr(mod, "write_records", written.extend)

    mod.annotate("warning", "first line\r\nsecond line", "message", records=records)
    expected = "::warning title=first line second line::message\n"
    if not queued:
        assert written == [expected.removesuffix("\n")]
        return

    assert records == [expected.removesuffix("\n")]
    assert capfd.readouterr().out == ""
    summary = tmp_path / "summary"
    mod.emit(watch_report_files({"GITHUB_STEP_SUMMARY": str(summary)}), records)
    assert summary.read_text(encoding="utf-8") == expected
    captured = capfd.readouterr()
    assert captured.out == expected
    assert captured.err == ""


REPORT_SINK = "write_records"
PIPE_WRITER = "write_key"  # The app key goes to an openssl pipe.
RAW_WRITES = {
    "print",
    "write",
    "write_text",
    "write_bytes",
    "writelines",
    "json.dump",
    "logging.basicConfig",
    "logging.handlers",
    "logging.log",
    "logging.error",
    "logging.warning",
    "logging.info",
    "logging.debug",
    "logging.critical",
    "addHandler",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copytree",
    "shutil.move",
    "os.open",
}
WRITER_OPENS = {"open", "builtins.open", "io.open", "os.fdopen"}
WRITER_MODULES = {"builtins", "io", "json", "logging", "os", "shutil"}


def raw_writer_name(function):
    """Recognize sink APIs regardless of whether a reference invokes or aliases them."""
    name = function.rsplit(".", 1)[-1]
    return (
        function in RAW_WRITES
        or name in RAW_WRITES
        or (function.startswith("logging.") and name.endswith("Handler"))
    )


def writes_output(node, parent):
    """Recognize raw output functions and opens with writable or unresolved modes."""
    if isinstance(node, ast.alias):
        if isinstance(parent, ast.ImportFrom):
            module = parent.module or ""
            return module.split(".")[0] in WRITER_MODULES and (
                raw_writer_name(f"{module}.{node.name}") or node.name in {"open", "fdopen", "*"}
            )
        return bool(node.asname) and node.name.split(".")[0] in WRITER_MODULES
    if not isinstance(node, (ast.Attribute, ast.Name)):
        return False
    function = ast.unparse(node)
    if raw_writer_name(function):
        return True
    name = function.rsplit(".", 1)[-1]
    if name not in {"open", "fdopen"} or function == "forge_opener.open":
        return False
    if not isinstance(parent, ast.Call) or parent.func is not node:
        return True
    index = int(function in WRITER_OPENS)
    mode = call_argument(parent, index, "mode")
    if mode is None:
        return any(isinstance(arg, ast.Starred) for arg in parent.args) or any(
            arg.arg is None for arg in parent.keywords
        )
    return not isinstance(mode, ast.Constant) or any(flag in str(mode.value) for flag in "wax+")


def annotation_fragment(node, parents):
    """Reserve workflow command delimiters and split delimiter fragments for annotate."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    text = node.value
    parent = parents[node]
    if (
        isinstance(parent, ast.Call)
        and ast.unparse(parent.func) == "re.compile"
        and not ANNOTATION_COMMAND.search(text)
    ):
        text = text.replace("(?::", "(?:")
    if text == ":":
        keyword = parents[parent]
        call = parents.get(keyword)
        return not (
            isinstance(parent, ast.Tuple)
            and isinstance(keyword, ast.keyword)
            and keyword.arg == "separators"
            and isinstance(call, ast.Call)
            and ast.unparse(call.func) == "json.dumps"
        )
    return "::" in text or (text.startswith(":") and not text[1].isspace())


def writer_violations(tree):
    """Find writes and command fragments outside their exact top-level definitions."""
    functions = top_level_functions(tree)
    writers = {functions[REPORT_SINK], functions[PIPE_WRITER]}
    writer_body = {
        node: writer for writer in writers for stmt in writer.body for node in ast.walk(stmt)
    }
    annotation = functions["annotate"]
    annotation_body = {node for stmt in annotation.body for node in ast.walk(stmt)}
    parents = parent_nodes(tree)
    found = []
    for scope, node in scoped_nodes(tree):
        parent = parents.get(node)
        name = getattr(scope, "name", type(scope).__name__)
        writer = writer_body.get(node)
        if scope is node and writer is not None:
            found.append((node.lineno, f"nested {name} in {writer.name}"))
        if writes_output(node, parent) and (
            writer is not scope
            or (
                scope is functions[PIPE_WRITER]
                and ast.unparse(node) not in {"os.fdopen", "pipe.write"}
            )
        ):
            found.append((node.lineno, f"{name} writes through {ast.unparse(node)}"))
        if (scope is not annotation or node not in annotation_body) and annotation_fragment(
            node, parents
        ):
            found.append((node.lineno, f"{name} builds a workflow annotation"))
    return [f"{SCRIPT}:{line}: {message}" for line, message in found]


def test_train_writes_only_through_declared_writers():
    """Tripwire for listed writes, opens, import aliases and nested definitions in the sinks."""
    assert writer_violations(ast.parse(script())) == []


@pytest.mark.parametrize(
    ("statement", "target", "message"),
    [
        ('print("line")', "emit", "emit writes through print"),
        ('sys.stderr.write("line")', "summary", "summary writes through sys.stderr.write"),
        ('os.write(1, b"line")', "summary", "summary writes through os.write"),
        ('Path("file").write_text("line")', "summary", "summary writes through"),
        ('Path("file").write_bytes(b"line")', "summary", "summary writes through"),
        ('open("file", "w")', "summary", "summary writes through open"),
        ('io.open("file", mode)', "summary", "summary writes through io.open"),
        ('io.open("file", **options)', "summary", "summary writes through io.open"),
        ('os.fdopen(fd, "a")', "summary", "summary writes through os.fdopen"),
        ("json.dump(data, handle)", "summary", "summary writes through json.dump"),
        ("shutil.copyfile(src, dst)", "summary", "summary writes through shutil.copyfile"),
        ("logging.FileHandler(path)", "summary", "summary writes through logging.FileHandler"),
        ("from logging import FileHandler as log", "summary", "summary writes through FileHandler"),
        ('def emit(): sys.stdout.write("line")', "summary", "emit writes through sys.stdout.write"),
        ('def emit(): return "line"', REPORT_SINK, "nested emit in write_records"),
        ('action = lambda: "line"', REPORT_SINK, "nested Lambda in write_records"),
        ("class Writer: pass", PIPE_WRITER, "nested Writer in write_key"),
        ('sys.stdout.write("line")', PIPE_WRITER, "write_key writes through sys.stdout.write"),
    ],
)
def test_writer_boundary_refuses_raw_writes_and_nested_scopes(statement, target, message):
    """Tripwire for listed write APIs, aliases, and nested functions, classes and lambdas."""
    source = script()
    tree = ast.parse(plant_statement(source, target, statement))
    assert any(message in error for error in writer_violations(tree))


@pytest.mark.parametrize(
    "expression",
    [
        '"::notice title=Train::line"',
        '":" + ":warning title=Train::line"',
        '"%s%s%s" % (":", ":", "error title=Train")',
        '"{}{}notice".format(":", ":")',
        '"".join((":", ":", "warning"))',
        "f\"{':' * 2}error\"",
    ],
    ids=["literal", "concatenation", "percent", "format", "join", "f-string"],
)
def test_annotation_boundary_refuses_command_fragments(expression):
    """Tripwire for literal commands and colons in concatenation, format, joins and f-strings."""
    tree = ast.parse(f"{script()}\nCOMMAND = {expression}\n")
    assert any("Module builds a workflow annotation" in error for error in writer_violations(tree))


def raw_table_cells(tree):
    """Find fields that can introduce a row break or an extra cell."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        literals = "".join(value.value for value in node.values if isinstance(value, ast.Constant))
        if not literals.startswith("|"):
            continue
        for part in node.values:
            if isinstance(part, ast.FormattedValue) and not (
                isinstance(part.value, ast.Call)
                and ast.unparse(part.value.func) == "note_cell"
                and len(part.value.args) == 1
                and not part.value.keywords
                and part.conversion == -1
                and part.format_spec is None
            ):
                found.append(f"{SCRIPT}:{part.value.lineno}: {ast.unparse(part.value)}")
    return found


def test_table_rows_escape_every_cell():
    """Tripwire for table-row f-strings whose interpolations bypass note_cell."""
    assert raw_table_cells(ast.parse(script())) == []


def plant_statement(source, target, statement, *, after=0):
    """Insert an executable test statement after a known function-body statement."""
    function = top_level_functions(ast.parse(source))[target]
    lines = source.splitlines(keepends=True)
    lines.insert(
        function.body[after].end_lineno, "".join(f"    {line}\n" for line in statement.splitlines())
    )
    return "".join(lines)


TRAIN_LINE_SHAPE_PLANTS = {
    "inherited-fds": (
        "summary",
        (
            'subprocess.run([sys.executable, "-c", '
            "\"print('note: first', 'second', sep=chr(10))\"], check=True, timeout=5)"
        ),
        r"train stdout:2: unrecognized report record: 'second'",
        "DIRTY",
        0,
    ),
    "logging": (
        "summary",
        'import logging\nlogging.getLogger("x").warning("a\\nb")',
        r"train stderr:1: unexpected stderr record: 'a'",
        "DIRTY",
        0,
    ),
    "sink-comprehension": (
        REPORT_SINK,
        '[sys.stdout.write("note: first\\nsecond\\n") for raw in (1,)]',
        r"train stdout:2: unrecognized report record: 'second'",
        "DIRTY",
        0,
    ),
    "concatenated-row": (
        "report",
        (
            'state = rows[0][0]["mergeStateStatus"]\n'
            'lines.append("| #1 | " + state + " | no action |")'
        ),
        r"train stdout:\d+: expected 3 table cells, got 4",
        "DIRTY | extra",
        1,
    ),
    "annotation": (
        "summary",
        (
            'marker = ": ".strip() * 2\n'
            'title = "first\\r\\nsecond"\n'
            'command = marker + "warning title=" + title + marker + "message\\n"\n'
            'subprocess.run([sys.executable, "-c", '
            '"import sys; sys.stdout.write(sys.argv[1])", command], check=True, timeout=5)'
        ),
        r"train stdout:1: forbidden record separator or NUL",
        "DIRTY",
        0,
    ),
}

RELEASE_LINE_SHAPE_PLANTS = {
    "workflow-output": ("GITHUB_OUTPUT", b"releases=[\n]\n", "1: expected compact releases= JSON"),
    "two-release-records": (
        "GITHUB_OUTPUT",
        b'releases=[]\nreleases=[{"tag":"v1"}]\n',
        "2: more than one releases= record",
    ),
    "spaced-release": (
        "TRAIN_RELEASE_FILE",
        b'{"releases": []}',
        "1: expected one compact JSON document",
    ),
    "two-release-documents": (
        "TRAIN_RELEASE_FILE",
        b"{}\n{}",
        "1: expected one compact JSON document",
    ),
}


@pytest.fixture
def line_shape_plant(request):
    """Enable one deliberately failing inner test by its full case name."""
    if os.environ.get("HORISTES_LINE_SHAPE_PLANT") != request.node.name:
        pytest.skip("enabled only in the named line-shape plant session")


def line_shape_train_source(case):
    """Build the plant that emits through the actual train subprocess."""
    target, statement, _, _, after = TRAIN_LINE_SHAPE_PLANTS[case]
    return plant_statement(script(), target, statement, after=after)


@pytest.mark.parametrize("case", TRAIN_LINE_SHAPE_PLANTS)
def test_line_shape_train_plant(tmp_path, line_shape_plant, case):
    """Emit output whose writer passes the static tripwires."""
    state = TRAIN_LINE_SHAPE_PLANTS[case][3]
    done, _ = train(tmp_path, Forge(repository(pull(1, state))), line_shape_train_source(case))
    assert done.returncode == 0, done.stdout + done.stderr


def test_line_shape_fd_stderr_plant(line_shape_plant, capfd):
    """Write inherited stderr and drain the descriptors during the call phase."""
    subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('a\\nb\\n')"], check=True, timeout=5
    )
    capfd.readouterr()


def test_line_shape_step_summary_plant(tmp_path, line_shape_plant):
    """Write a carriage return to the named summary without writing stdout."""
    path = tmp_path / "any-summary-name"
    watch_report_files({"GITHUB_STEP_SUMMARY": str(path)})
    path.write_bytes(b"review bots: first\r\nsecond\n")


@pytest.mark.parametrize("case", RELEASE_LINE_SHAPE_PLANTS)
def test_line_shape_release_output_plant(tmp_path, line_shape_plant, case):
    """Write the selected malformed payload to its release destination."""
    name, content, _ = RELEASE_LINE_SHAPE_PLANTS[case]
    path = tmp_path / "release-output"
    watch_report_files({name: str(path)})
    path.write_bytes(content)


def test_line_shape_byte_cap_plant(line_shape_plant):
    """Write one byte over the cap using only valid note records on inherited stdout."""
    count, extra = divmod(LINE_SHAPE_MAX_BYTES + 1 - len(b"note: \n"), len(b"note: x\n"))
    command = (
        "import sys; count, extra = map(int, sys.argv[1:]); "
        "sys.stdout.buffer.write(b'note: x\\n' * count + b'note: ' + b'x' * extra + b'\\n')"
    )
    subprocess.run([sys.executable, "-c", command, str(count), str(extra)], check=True, timeout=5)


def test_line_shape_child_summary_fits_cap(tmp_path):
    """A 40 KiB file fits when both the child wrapper and teardown snapshot it."""
    path = tmp_path / "summary.md"
    env = watch_report_files({"GITHUB_STEP_SUMMARY": str(path)})
    write = (
        "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'note: x\\n' * 5120)"
    )
    done = run_train_process(
        [sys.executable, "-c", write, str(path)], descriptor="train", env=env, timeout=5
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert path.stat().st_size == 40 * 1024


def test_line_shape_summary_over_cap_names_destination(tmp_path, caplog):
    """One oversized file snapshot fails with its destination and full byte count."""
    path = tmp_path / "summary.md"
    size = LINE_SHAPE_MAX_BYTES + 1
    path.write_bytes(b"note: " + b"x" * (size - len(b"note: \n")) + b"\n")
    capture = LineShapeCapture(lambda: ("", ""), caplog, None)
    capture.watch_files({"GITHUB_STEP_SUMMARY": str(path)})
    message = f"GITHUB_STEP_SUMMARY ({path}):1: {size} bytes exceeds the "
    with pytest.raises(LineShapeError, match=re.escape(message)):
        capture.check()


@pytest.mark.usefixtures("line_shape")
@pytest.mark.parametrize(
    ("plant", "message"),
    [
        *(
            pytest.param(f"test_line_shape_train_plant[{case}]", plant[2], id=case)
            for case, plant in TRAIN_LINE_SHAPE_PLANTS.items()
        ),
        pytest.param(
            "test_line_shape_fd_stderr_plant",
            r"fd stderr:1: unexpected stderr record",
            id="fd-stderr",
        ),
        pytest.param(
            "test_line_shape_step_summary_plant",
            r"GITHUB_STEP_SUMMARY .*:1: forbidden record separator",
            id="step-summary",
        ),
        *(
            pytest.param(
                f"test_line_shape_release_output_plant[{case}]", rf"{name} .*:{message}", id=case
            )
            for case, (name, _, message) in RELEASE_LINE_SHAPE_PLANTS.items()
        ),
        pytest.param(
            "test_line_shape_byte_cap_plant",
            rf"fd stdout:1: {LINE_SHAPE_MAX_BYTES + 1} bytes exceeds the "
            rf"{LINE_SHAPE_MAX_BYTES}-byte capture limit",
            id="byte-cap",
        ),
    ],
)
def test_line_shape_rejects_unguarded_output(plant, message):
    """Require the actual autouse teardown to reject a plant that passed its call phase."""
    if plant.startswith("test_line_shape_train_plant["):
        case = plant.partition("[")[2].removesuffix("]")
        tree = ast.parse(line_shape_train_source(case))
        assert writer_violations(tree) == []
        assert raw_table_cells(tree) == []
    env = {**os.environ, "HORISTES_LINE_SHAPE_PLANT": plant, "PYTHONDONTWRITEBYTECODE": "1"}
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-vv",
        "--show-capture=no",
        f"tests/test_merge_train.py::{plant}",
    ]
    done = run_train_process(
        command, descriptor=LINE_SHAPE_PLANT_PYTEST, env=env, timeout=120, cwd=ROOT
    )
    output = done.stdout + done.stderr
    assert done.returncode == 1, output
    assert f"::{plant} PASSED" in output, output
    assert f"ERROR at teardown of {plant}" in output, output
    assert re.search(r"LineShapeError: " + message, output), output
    assert "1 passed, 1 error" in output, output


@pytest.mark.parametrize(
    "text", [pytest.param("releases=[]\n", id="one-record"), pytest.param("", id="empty-text")]
)
def test_line_shape_accepts_workflow_output(text):
    """Accept zero or one compact releases= record."""
    check_line_shape("GITHUB_OUTPUT", text)


@pytest.mark.parametrize(
    "separator", sorted(RECORD_CONTROLS), ids=lambda value: f"U+{ord(value):04X}"
)
def test_emit_flattens_record_controls(tmp_path, monkeypatch, capfd, separator):
    """Check the flattened value in both destinations for every forbidden separator."""
    mod = load_train(tmp_path, monkeypatch)
    path = tmp_path / "summary"
    mod.emit(
        watch_report_files({"GITHUB_STEP_SUMMARY": str(path)}), [f"note: first{separator}second"]
    )
    assert capfd.readouterr().out == "note: first second\n"
    assert path.read_bytes() == b"note: first second\n"


def test_table_cell_guard_refuses_raw_interpolation():
    """Tripwire for a table-row f-string mixing escaped and raw interpolations."""
    tree = ast.parse('row = f"| {note_cell(account)} | {raw_tally} |"')
    assert raw_table_cells(tree) == [f"{SCRIPT}:1: raw_tally"]


@pytest.mark.parametrize("operation", ["RetryHistory", "FileModes"])
@pytest.mark.parametrize("fatal", [True, False], ids=["document-error", "value-error"])
def test_anonymous_retry_document_stops_the_run(tmp_path, operation, fatal):
    forge = retry_forge("BEHIND")
    source = script()
    named = f"query {operation}("
    assert source.count(named) == 1
    source = source.replace(named, "query(")
    if not fatal:
        error = 'raise GraphQLDocumentError("anonymous GraphQL operation")'
        assert source.count(error) == 1
        source = source.replace(error, 'raise ValueError("anonymous GraphQL operation")')
    done, summary = train(tmp_path, forge, source, optimize=True)
    hold = "held for retry: history unreadable: anonymous GraphQL operation"
    expected_calls = []
    if operation == "FileModes":
        expected_calls = [1] if fatal else [1, 2]
    assert forge.retry_calls == expected_calls
    assert forge.mode_calls == []
    if fatal:
        assert done.returncode != 0, done.stdout + done.stderr
        assert "GraphQLDocumentError: anonymous GraphQL operation" in done.stderr
        assert "held for retry: history unreadable" not in done.stdout + done.stderr + summary
        assert forge.writes == []
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        assert hold in summary
        assert forge.writes == [updated(2 if operation == "FileModes" else 1)]


@pytest.mark.parametrize("count", [2, 6])
def test_retry_hold_all_held_releases_oldest_with_bounded_reads(tmp_path, count):
    pulls = [pull(n, "BEHIND", autoMergeRequest={"enabledAt": OLD}) for n in range(1, count + 1)]
    forge = Forge(repository(*pulls))
    for number in range(1, count + 1):
        forge.retry_histories[number] = retry_history(number, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == list(range(1, min(count, 5) + 1))
    assert "retry hold released: no other pull request to serve" in summary


def test_retry_hold_stops_reading_at_first_unheld(tmp_path):
    forge = retry_forge("BEHIND")
    forge.retry_histories.clear()
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == [1]


def test_retry_hold_landing_run_does_not_read_history(tmp_path):
    forge = retry_forge()
    forge.reads["fleet/app"][0]["pullRequests"]["nodes"].append(pull(3, "CLEAN"))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(3)]
    assert forge.retry_calls == []
    assert forge.compare_calls == []


@pytest.mark.parametrize("ordered", [False, True])
def test_retry_hold_live_lease_does_not_read_history(tmp_path, ordered):
    forge = retry_forge("BEHIND")
    lease = ordered_lease(1, 2)
    forge.reads["fleet/app"][0]["landerLease"] = {
        "description": lease if ordered else lease.partition(";")[0]
    }
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.retry_calls == []
    assert forge.compare_calls == []
    assert forge.writes == ([updated(1)] if ordered else [])


def test_ordered_lease_pending_first_blocks_clean_second(tmp_path):
    first = pull(7840, "BLOCKED", suites=[None], autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(first, pull(7842, "CLEAN"), lease=ordered_lease(7840, 7842)))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "### fleet/app: #7840 is armed and in flight; the lander holds" in summary
    assert "#7840 is next" in summary


def test_ordered_lease_closed_first_entry_serves_second(tmp_path):
    forge = Forge(repository(pull(7842, "CLEAN"), lease=ordered_lease(7840, 7842)))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7842)]
    assert "#7842 is next" in summary


def test_ordered_lease_skips_non_order_clean_pull(tmp_path):
    forge = Forge(
        repository(pull(7845, "CLEAN"), pull(7840, "CLEAN"), lease=ordered_lease(7840, 7842))
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7840)]
    assert "| #7845 | CLEAN | the lander holds" in summary


def test_ordered_lease_security_update_still_lands(tmp_path):
    forge = Forge(
        repository(
            pull(7840, "BLOCKED", suites=[None]),
            pull(7842, "CLEAN", labels=["security"]),
            lease=ordered_lease(7840),
        )
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7842)]
    assert forge.retry_calls == []
    assert forge.lease_calls == 0


def test_ordered_lease_expired_serves_oldest_clean(tmp_path):
    forge = Forge(
        repository(
            pull(7845, "CLEAN"), pull(7840, "CLEAN"), lease=ordered_lease(7840, expired=True)
        )
    )
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7845)]


def test_ordered_lease_no_lease_control_oldest_clean_lands(tmp_path):
    forge = Forge(repository(pull(7845, "CLEAN"), pull(7840, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7845)]


@pytest.mark.parametrize("unread", [False, True], ids=["all-read", "unread-pages"])
def test_ordered_lease_no_known_planned_pull_yields_all(tmp_path, unread):
    pulls = [pull(n, "CLEAN") for n in range(1, 402 if unread else 2)]
    lease = ordered_lease(7840, 7842)
    forge = Forge(repository(*pulls, lease=lease))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    # Past an unread page the order is unknown: the plain lease note, not "none is open".
    assert ("no planned pull request is open" in summary) is not unread
    stamp = lease.removeprefix("until=").partition(";")[0]
    assert f"| #1 | CLEAN | the lander holds the landing order until {stamp}" in summary
    assert ("Only the 100 oldest" in summary) is unread


def test_ordered_lease_unread_first_entry_does_not_serve_later_entry(tmp_path):
    """#401 may be open past the pages read, so #5 behind it in the order does not land."""
    pulls = [pull(n, "CLEAN") for n in range(1, 402)]
    lease = ordered_lease(401, 5)
    forge = Forge(repository(*pulls, lease=lease))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    stamp = lease.removeprefix("until=").partition(";")[0]
    assert f"| #5 | CLEAN | the lander holds the landing order until {stamp} |" in summary
    assert "#5 is next" not in summary


@pytest.mark.parametrize(
    "order",
    ["", "0", "1,,2", "1,", ",1", ",".join(str(n) for n in range(1, 22)), "1;", "01", "10000000"],
)
def test_ordered_lease_malformed_refuses_before_writes(tmp_path, order):
    lease = ordered_lease(1).partition("; order=")[0] + "; order=" + order
    forge = Forge(repository(pull(1, "CLEAN"), lease=lease))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 1, done.stdout + done.stderr
    assert forge.writes == []
    assert "lander lease description" in summary


@pytest.mark.parametrize("order", [(1,), tuple(range(1, 21)), (9999999,)])
def test_ordered_lease_until_keeps_stamp_at_valid_boundaries(tmp_path, order):
    rules = load_rules(tmp_path)
    description = ordered_lease(*order)
    repo = repository(lease=description)
    assert rules.lease_until(repo, NOW) == description[6:].partition(";")[0]


@pytest.mark.parametrize("kind", ["missing", "null", "stamp", "patch", "files"])
def test_retry_hold_malformed_history_is_held(tmp_path, monkeypatch, kind):
    history = retry_history(1, retry_attempt(suites=[retry_suite()]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    if kind == "missing":
        del history["baseRefOid"]
    elif kind == "null":
        history["commits"]["nodes"] = None
    elif kind == "stamp":
        history["commits"]["nodes"][0]["commit"]["checkSuites"]["nodes"][0]["createdAt"] = "bad"
    elif kind == "files":
        # A mapping where the compare answer holds a list: its keys are text, not file objects.
        monkeypatch.setattr(mod, "call", lambda *args: {"files": {"app.py": retry_file()}})
    else:
        monkeypatch.setattr(mod, "call", lambda *args: {"files": [retry_file(patch=123)]})
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert reason.startswith("held for retry: history unreadable:")


def test_retry_hold_naive_attempt_stamp_is_unreadable(tmp_path, monkeypatch):
    history = retry_history(1, retry_attempt(suites=[retry_suite(when=NOW.replace(tzinfo=None))]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert "history unreadable: retry check timestamp has no timezone" in reason


@pytest.mark.parametrize("app,name", [("third-party", "third-party"), (None, "unknown app")])
def test_retry_hold_non_actions_suite_uses_app_name(tmp_path, monkeypatch, app, name):
    history = retry_history(1, retry_attempt(suites=[retry_suite(workflow=None, app=app)]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert f"failed {name}; until" in reason


def test_retry_hold_newer_pending_suite_does_not_extend_failed_stamp(tmp_path, monkeypatch):
    failed = retry_suite(when=NOW - timedelta(days=7))
    pending = retry_suite(None, workflow=2, when=NOW, number=2)
    history = retry_history(1, retry_attempt(suites=[failed, pending]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    assert mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW) is None


def test_retry_hold_pending_app_suite_keeps_failure(tmp_path, monkeypatch):
    old = retry_attempt(suites=[retry_suite()])
    app = retry_attempt("d" * 40, [retry_suite(None, workflow=None, when=NOW)])
    history = retry_history(1, old, app)
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert reason and "diff unchanged since aaaaaaaaaaaa failed workflow-1; until" in reason


def test_retry_hold_latest_failure_names_all_failed_workflows(tmp_path, monkeypatch):
    first = retry_suite()
    second = retry_suite("TIMED_OUT", workflow=2, when=NOW, number=2)
    history = retry_history(1, retry_attempt(suites=[first, second]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert "failed workflow-1, workflow-2; until" in reason
    assert reason.endswith((NOW + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_retry_hold_compare_file_page_below_limit_is_readable(tmp_path, monkeypatch):
    history = retry_history(1, retry_attempt(suites=[retry_suite()]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)
    files = [retry_file(filename=f"file-{n}.py") for n in range(299)]
    monkeypatch.setattr(mod, "call", lambda *args: {"files": files})
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert "diff unchanged since" in reason


def test_retry_history_query_selects_bounded_attempts_and_workflow_names():
    text = script().split('RETRY_HISTORY = """', 1)[1].split('"""', 1)[0]
    assert "commits(last: 20) { nodes { commit { ...Attempt } } }" in " ".join(text.split())
    for field in (
        "headRefOid baseRefOid",
        "commits(last: 20)",
        "timelineItems(last: 20, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT])",
        "beforeCommit { ...Attempt }",
        "fragment Attempt on Commit",
        "checkSuites(first: 30)",
        "databaseId conclusion createdAt app { slug }",
        "workflowRun { databaseId workflow { databaseId name } }",
    ):
        assert field in text


@pytest.mark.parametrize("initial", [_ABSENT, ordered_lease(7840, 7842)], ids=["new", "reordered"])
def test_lander_lease_changed_since_page_read_lands_nothing(tmp_path, initial):
    forge = Forge(repository(pull(7840, "CLEAN"), pull(7842, "CLEAN"), lease=initial))
    forge.lease_read = {
        "data": {"repository": {"landerLease": {"description": ordered_lease(7842, 7840)}}}
    }
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert "### fleet/app: lander lease changed since the page read" in summary
    assert "| #7840 | CLEAN | lander lease changed since the page read" in summary
    assert forge.lease_calls == 1


@pytest.mark.parametrize("lease", [_ABSENT, ordered_lease(7840, 7842)], ids=["absent", "ordered"])
def test_lander_lease_unchanged_lands_after_one_read(tmp_path, lease):
    forge = Forge(repository(pull(7840, "CLEAN"), lease=lease))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(7840)]
    assert forge.lease_calls == 1


@pytest.mark.parametrize(
    "answer,cause",
    [
        ({"errors": [{"message": "lease read refused"}]}, "lease read refused"),
        ({"data": {"repository": {}}}, "landerLease"),
        ({"data": {"repository": None}}, "NoneType"),
        pytest.param(None, "HTTP 502 first line second line", id="multiline-502"),
    ],
)
def test_lander_lease_unreadable_lands_nothing(tmp_path, answer, cause):
    forge = Forge(repository(pull(1, "CLEAN"), pull(2, "CLEAN")))
    if answer is None:
        forge.rest_faults["LanderLease"] = [(502, FORGE_FAULT_BODY)] * 3
    else:
        forge.lease_read = answer
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    for output in (done.stdout, summary):
        (heading,) = [line for line in output.splitlines() if line.startswith("### fleet/app: ")]
        assert heading.startswith("### fleet/app: lander lease unreadable: ")
        assert cause in heading
        if answer is None:
            assert re.fullmatch(
                r"### fleet/app: lander lease unreadable: RefusedError: graphql LanderLease: "
                r"POST http://127\.0\.0\.1:\d+/graphql: HTTP 502 first line second line",
                heading,
            )
    if answer is None:
        assert forge.rest_faults["LanderLease"] == []
    else:
        assert forge.lease_calls == 1


def test_retry_hold_concluded_green_and_pending_app_releases(tmp_path, monkeypatch):
    old = retry_attempt(suites=[retry_suite()])
    green = retry_attempt(
        "d" * 40,
        [retry_suite("SUCCESS", when=NOW), retry_suite(None, workflow=None, when=NOW)],
    )
    mod, conf, env = retry_reader(tmp_path, monkeypatch, retry_history(1, old, green))
    assert mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW) is None


@pytest.mark.parametrize("mode,held", [(33188, True), (33261, False)], ids=["same", "changed"])
def test_retry_hold_file_modes_are_part_of_own_diff(tmp_path, mode, held):
    forge = retry_forge("BEHIND")
    forge.modes[("a" * 40, "app.py")] = 33188
    forge.modes[(f"{1:040x}", "app.py")] = mode
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2 if held else 1)]
    assert ("held for retry:" in summary) is held
    assert forge.mode_calls == [["app.py"]]


@pytest.mark.parametrize("only_removed", [False, True])
def test_retry_hold_removed_files_have_no_mode_reads(tmp_path, only_removed):
    forge = retry_forge()
    files = [retry_file(filename="gone.py", status="removed")]
    if not only_removed:
        files.append(retry_file(filename='odd"\\name\n.py'))
    for head in ("a" * 40, f"{1:040x}"):
        forge.comparisons[head] = (200, {"files": files})
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    assert "diff unchanged since" in summary
    assert forge.mode_calls == ([] if only_removed else [['odd"\\name\n.py']])


def test_retry_hold_mode_read_error_holds_with_cause(tmp_path):
    forge = retry_forge()
    forge.mode_error = "tree modes refused"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    assert "held for retry: history unreadable:" in summary
    assert "tree modes refused" in summary


@pytest.mark.parametrize("node", [{}, {"before": None}, {"before": {"f0": {}}, "after": {}}])
def test_retry_hold_missing_mode_is_unreadable(tmp_path, monkeypatch, node):
    history = retry_history(1, retry_attempt(suites=[retry_suite()]))
    mod, conf, env = retry_reader(tmp_path, monkeypatch, history)

    def read(env, query, variables):
        return {"repository": node if "fragment M on Commit" in query else {"pullRequest": history}}

    monkeypatch.setattr(mod, "read_graphql", read)
    reason = mod.retry_hold(env, conf, pull(1, "BEHIND"), NOW)
    assert reason.startswith("held for retry: history unreadable:")


@pytest.mark.parametrize("content", ["new\r", "ne\x0cw"], ids=["crlf", "form-feed"])
def test_retry_hold_changed_line_preserves_content(tmp_path, content):
    forge = retry_forge("BEHIND")
    forge.comparisons["a" * 40] = (
        200,
        {"files": [retry_file(patch=f"@@ -1 +1 @@\n-old\n+{content}\n")]},
    )
    # splitlines() truncates a changed line at form feed, discarding the rest as context.
    current = "new" if content == "new\r" else "ne"
    forge.comparisons[f"{1:040x}"] = (
        200,
        {"files": [retry_file(patch=f"@@ -1 +1 @@\n-old\n+{current}\n")]},
    )
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert "held for retry:" not in summary


@pytest.mark.parametrize("held", [True, False], ids=["held", "unheld"])
def test_retry_hold_in_flight_read_before_older_unarmed_behind(tmp_path, held):
    first = pull(1, "BEHIND")
    second = pull(2, "BLOCKED", suites=[None], autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(first, second))
    if held:
        forge.retry_histories[2] = retry_history(2, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    if held:
        assert forge.writes == [armed(1), updated(1)]
        assert "| #2 | BLOCKED | held for retry:" in summary
        assert forge.retry_calls == [2, 1]
    else:
        assert "### fleet/app: #2 is armed and in flight" in summary
        assert forge.writes == []
        assert forge.retry_calls == [2]


@pytest.mark.parametrize(
    "before,after,held",
    [
        ("-old\n+new\n\\ No newline at end of file", "-old\n+new", False),
        (
            "-old\n\\ No newline at end of file\n+new",
            "-old\n+new\n\\ No newline at end of file",
            False,
        ),
        (
            "-old\n+new\n\\ No newline at end of file",
            "-old\n+new\n\\ No newline at end of file",
            True,
        ),
        (
            "-old\n+new\n last\n\\ No newline at end of file",
            "-old\n+new\n last",
            True,
        ),
    ],
    ids=["repaired-newline", "moved-marker", "same-marker", "context-marker"],
)
def test_retry_hold_final_newline_marker_belongs_to_changed_line(tmp_path, before, after, held):
    forge = retry_forge("BEHIND")
    for oid, patch in (("a" * 40, before), (f"{1:040x}", after)):
        hunk = "@@ -1,2 +1,2 @@" if "\n last" in patch else "@@ -1 +1 @@"
        forge.comparisons[oid] = (200, {"files": [retry_file(patch=f"{hunk}\n{patch}")]})
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2 if held else 1)]
    assert ("held for retry:" in summary) is held


@pytest.mark.parametrize("attempts", ["held", "unheld", "all-held"])
def test_retry_hold_behind_reads_in_update_order(tmp_path, attempts):
    first = pull(1, "BEHIND")
    second = pull(2, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(first, second))
    if attempts != "unheld":
        forge.retry_histories[2] = retry_history(2, retry_attempt(suites=[retry_suite()]))
    if attempts == "all-held":
        forge.retry_histories[1] = retry_history(1, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    if attempts == "held":
        assert "| #2 | BEHIND | held for retry:" in summary
        assert forge.writes == [armed(1), updated(1)]
    else:
        assert forge.writes == [updated(2)]
    assert forge.retry_calls == ([2] if attempts == "unheld" else [2, 1])
    if attempts == "all-held":
        assert "| #2 | BEHIND | updated from the default branch; retry hold released:" in summary
        assert "retry hold released: no other pull request to serve" in summary


@pytest.mark.parametrize("waiting", [{}, {"autoMergeRequest": {"enabledAt": OLD}}])
def test_retry_hold_linear_history_behind_does_not_read_history(tmp_path, waiting):
    forge = Forge(repository(pull(1, "BEHIND", **waiting), rules=("REQUIRED_LINEAR_HISTORY",)))
    forge.retry_histories[1] = retry_history(1, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert forge.retry_calls == []
    assert "| #1 | BEHIND | behind: linear history, a person rebases |" in summary


def test_retry_hold_unarmed_behind_without_auto_merge_does_not_read_history(tmp_path):
    first = pull(1, "BEHIND")
    second = pull(2, "BEHIND", autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(first, second, auto_merge=False))
    forge.retry_histories[1] = retry_history(1, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(2)]
    assert forge.retry_calls == [2]
    assert "| #1 | BEHIND | behind: auto-merge off, not armed, not updated |" in summary


def retry_behind_forge(count, held, *, armed_pulls=True, **first_fields):
    fields = {"autoMergeRequest": {"enabledAt": OLD}} if armed_pulls else {}
    pulls = [
        pull(n, "BEHIND", **(fields | (first_fields if n == 1 else {})))
        for n in range(1, count + 1)
    ]
    forge = Forge(repository(*pulls))
    for number in held:
        forge.retry_histories[number] = retry_history(number, retry_attempt(suites=[retry_suite()]))
    return forge


def test_retry_hold_after_conflict_checks_next_candidate(tmp_path):
    forge = retry_behind_forge(2, [2])
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == [1, 2]
    assert "| #1 | BEHIND | conflicts with the default branch |" in summary
    assert "| #2 | BEHIND | held for retry:" in summary


def test_retry_hold_after_scanning_skip_checks_next_candidate(tmp_path):
    forge = retry_behind_forge(
        2,
        [2],
        armed_pulls=False,
        suites=[suite(1001, "FAILURE", 10, None, "github-code-scanning")],
    )
    # Prepare a BEHIND row to reach advance's defensive scanning guard; verdict
    # normally filters it earlier. The production rules module stays unchanged.
    helper = module().replace(
        "    if scanning:\n        return scanning",
        "    if scanning:\n"
        '        return "behind" if pull["mergeStateStatus"] == "BEHIND" else scanning',
    )
    assert helper != module()
    done, summary = train(tmp_path, forge, helper=helper)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert forge.retry_calls == [1, 2]
    assert "| #1 | BEHIND | not armed: code-scanning check failed |" in summary
    assert "| #2 | BEHIND | held for retry:" in summary


def test_retry_hold_after_unsigned_skip_checks_next_candidate(tmp_path):
    forge = retry_behind_forge(
        2,
        [2],
        armed_pulls=False,
        author={"login": "praetor-ops", "__typename": "Bot"},
        headRefName="fix/x",
    )
    forge.commits[1] = [("fix: automated update without trailer", 1)]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert forge.retry_calls == [1, 2]
    assert "| #1 | BEHIND | skipped: bot pull request without Signed-off-by" in summary
    assert "| #2 | BEHIND | held for retry:" in summary


@pytest.mark.parametrize("fifth_held", [True, False], ids=["deferred", "unheld-fifth"])
def test_retry_hold_after_conflict_shares_read_budget(tmp_path, fifth_held):
    forge = retry_behind_forge(6, range(2, 6 if fifth_held else 5))
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([updated(1)] if fifth_held else [updated(1), updated(5)])
    assert forge.retry_calls == [1, 2, 3, 4, 5]
    if fifth_held:
        assert "| #6 | BEHIND | retry history deferred: read budget spent this sweep |" in summary
    for number in range(2, 6 if fifth_held else 5):
        assert f"| #{number} | BEHIND | held for retry:" in summary


def test_retry_hold_all_held_conflict_keeps_other_holds(tmp_path):
    forge = retry_behind_forge(2, [1, 2])
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == [1, 2]
    assert "| #2 | BEHIND | held for retry:" in summary
    assert "| #1 | BEHIND | conflicts with the default branch; retry hold released:" in summary
    # The forge reports the conflict on the next sweep; #2 is now the lone candidate.
    next_forge = retry_behind_forge(2, [1, 2])
    next_forge.reads["fleet/app"][0]["pullRequests"]["nodes"][0]["mergeStateStatus"] = "DIRTY"
    following = tmp_path / "following"
    following.mkdir()
    done, summary = train(following, next_forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert next_forge.writes == [updated(2)]
    assert next_forge.retry_calls == [2]
    assert "| #2 | BEHIND | updated from the default branch; retry hold released:" in summary


def test_retry_hold_urgent_after_spent_budget_is_updated(tmp_path):
    forge = retry_behind_forge(6, range(2, 6))
    forge.reads["fleet/app"][0]["pullRequests"]["nodes"][-1]["labels"] = {
        "nodes": [{"name": "merge-train: urgent"}]
    }
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1), updated(6)]
    assert forge.retry_calls == [1, 2, 3, 4, 5]
    assert "| #6 | BEHIND | updated from the default branch |" in summary


def test_retry_hold_security_after_spent_budget_is_updated(tmp_path):
    pulls = [
        pull(n, "BLOCKED", autoMergeRequest={"enabledAt": OLD}, suites=[None]) for n in range(1, 6)
    ]
    security = pull(6, "BEHIND", autoMergeRequest={"enabledAt": OLD}, labels=["security"])
    forge = Forge(repository(*pulls, security))
    for number in range(1, 6):
        forge.retry_histories[number] = retry_history(number, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(6)]
    assert forge.retry_calls == [1, 2, 3, 4, 5]
    assert "| #6 | BEHIND | updated from the default branch |" in summary


def test_retry_hold_all_held_in_flight_keeps_other_holds(tmp_path):
    pulls = [pull(n, "BLOCKED", autoMergeRequest={"enabledAt": OLD}, suites=[None]) for n in (1, 2)]
    forge = Forge(repository(*pulls))
    for number in (1, 2):
        forge.retry_histories[number] = retry_history(number, retry_attempt(suites=[retry_suite()]))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert forge.retry_calls == [1, 2]
    assert "retry hold released: no other pull request to serve" in summary
    assert "| #2 | BLOCKED | held for retry:" in summary


def test_retry_hold_deferral_ends_advancement_before_later_urgent(tmp_path):
    forge = retry_behind_forge(7, range(2, 6))
    forge.reads["fleet/app"][0]["pullRequests"]["nodes"][-1]["labels"] = {
        "nodes": [{"name": "merge-train: urgent"}]
    }
    forge.update_answers[1] = (422, f"{CONFLICT}.")
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]
    assert forge.retry_calls == [1, 2, 3, 4, 5]
    assert "| #6 | BEHIND | retry history deferred: read budget spent this sweep |" in summary
    assert "| #7 | BEHIND | updated from the default branch" not in summary


# ADR-0090: a skipped review holds even when no review body has findings.
REVIEW_STATE_FIXTURES = ROOT / "tests/fixtures/review-state"
INCOMPLETE_3507 = "review incomplete: coderabbitai limit reached at 2026-11-16T16:40:51Z"


def _incomplete_review_pull(*, armed=False):
    """Replay PR 3507 through the train's existing forge and detail builders."""
    payload = json.loads((REVIEW_STATE_FIXTURES / "pr3507-review-state.json").read_text())
    node = payload["data"]["repository"]["pullRequest"]
    detail = _detail(reviews=[], comments=node["comments"]["nodes"])
    detail.update({key: node[key] for key in ("headRefOid", "author", "commits")})
    p = pull(
        1,
        "CLEAN",
        author=node["author"],
        headRefOid=node["headRefOid"],
        committedDate="2026-11-16T16:10:16Z",
        autoMergeRequest={"enabledAt": OLD} if armed else None,
    )
    return p, detail


def _completed_review_summary(when):
    """A completed CodeRabbit summary with a pre-merge table and no findings."""
    return {
        "author": {"login": "coderabbitai", "__typename": "Bot"},
        "createdAt": when,
        "body": "Pre-merge checks\n| Title check | ![Passed](passed.svg) | Title is clear |",
    }


def test_planted_incomplete_review_body_hold(tmp_path, monkeypatch):
    """No formal reviews: the current-head limit comment alone must hold admission."""
    merge_train = load_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    # The primary pull query's author is authoritative, even if detail has a bot author.
    detail["author"] = {"login": "coderabbitai"}
    reason = merge_train.review_body_hold(p, detail)
    assert reason is not None
    assert reason == merge_train.REVIEW_BODY + INCOMPLETE_3507


@pytest.mark.parametrize("armed", [False, True], ids=["unarmed", "armed"])
def test_planted_incomplete_review_train_holds(tmp_path, armed):
    """CLEAN PR 3507 lands nothing; an armed copy is disarmed with the same hold."""
    p, detail = _incomplete_review_pull(armed=armed)
    forge = Forge(repository(p))
    forge.review_details[1] = detail
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([disarmed(1)] if armed else []), forge.writes
    assert f"review body: {INCOMPLETE_3507}" in summary
    assert ("disarmed: review body:" in summary) is armed


def test_completed_review_clears_incomplete_hold(tmp_path, monkeypatch):
    """The same captured detail holds before, and clears only after scoped head evidence."""
    merge_train = load_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    assert merge_train.review_body_hold(p, detail) is not None
    for login in ("coderabbitai", "sourcery-ai"):
        summary = _completed_review_summary("2026-11-16T17:00:00Z")
        summary["author"]["login"] = login
        summary["body"] += (
            "\nReviewing files that changed from the base of the PR and between "
            + "a" * 40
            + " and "
            + p["headRefOid"]
            + "."
        )
        detail["comments"]["nodes"].append(summary)
    detail["comments"]["totalCount"] = len(detail["comments"]["nodes"])
    assert merge_train.review_body_hold(p, detail) is None
    forge = Forge(repository(p))
    forge.review_details[1] = detail
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("merge", "PR_1", p["headRefOid"], "SQUASH")]
    assert "### fleet/app: landed #1" in summary


@pytest.mark.parametrize(
    ("section", "fields"),
    [
        pytest.param("pullRequest", ("headRefOid", "author { login }"), id="head-and-author"),
        pytest.param(
            "reviews", ("submittedAt", "updatedAt", "commit { oid }", "url"), id="review-times"
        ),
        pytest.param("comments", ("createdAt", "updatedAt", "url"), id="comment-times"),
        pytest.param("commits", ("committedDate",), id="commit-times"),
    ],
)
def test_review_bodies_query_supplies_incomplete_reader(tmp_path, monkeypatch, section, fields):
    """The actual query retains every timestamp the new reader needs."""
    merge_train = load_train(tmp_path, monkeypatch)
    query = merge_train.REVIEW_BODIES
    if section == "pullRequest":
        selected = query.split("reviews(first:", 1)[0]
    else:
        selected = query.split(f"{section}(first: 100)", 1)[1]
        selected = selected.split("(first: 100)", 1)[0].rsplit("\n      }", 1)[0]
        assert "totalCount" in selected
    for field in fields[:4]:
        assert field in selected, (section, field)


def test_sparse_checkout_missing_review_state_is_refused():
    """A checkout omitting the reader cannot satisfy the train's source contract."""
    doc = ciwf.load(TRAIN)
    step = next(s for s in doc["jobs"]["train"]["steps"] if is_checkout(s))
    step["with"]["sparse-checkout"] = "tools/merge_train.py"
    assert checkout_problems(doc) == ["sparse-checkout: 'tools/merge_train.py'"]


def test_review_state_sparse_checkout_includes_reader():
    """The served workflow checks out the train and its reader together."""
    doc = ciwf.load(TRAIN)
    assert checkout_problems(doc) == []
    step = next(s for s in doc["jobs"]["train"]["steps"] if is_checkout(s))
    assert step["with"]["sparse-checkout-cone-mode"] is False


@pytest.mark.parametrize("armed", [False, True], ids=["unarmed", "armed"])
def test_incomplete_review_posted_at_landing_holds(tmp_path, armed):
    """A notice arriving after admission still prevents landing and disarms auto-merge."""
    p, detail = _incomplete_review_pull(armed=armed)
    forge = Forge(repository(p))
    forge.review_details[1] = [_detail(), detail]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([disarmed(1)] if armed else [])
    assert forge.review_body_calls == [1, 1]
    assert f"review body: {INCOMPLETE_3507}" in summary


def test_incomplete_review_priority_and_connection_guard(tmp_path, monkeypatch):
    """Count guards precede incomplete review; incomplete review precedes body findings."""
    merge_train = load_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    detail["reviews"] = {
        "totalCount": 1,
        "nodes": [_bot_review(101, "coderabbitai", "Nitpick comments: 1")],
    }
    assert merge_train.review_body_hold(p, detail) == merge_train.REVIEW_BODY + INCOMPLETE_3507
    detail["reviews"]["totalCount"] = 101
    assert merge_train.review_body_hold(p, detail) == (
        merge_train.REVIEW_BODY + "more than 100 reviews, cannot read every answer"
    )


def adr92_train(tmp_path, monkeypatch):
    """Load either live modules or the explicit offline baseline for regression replay."""
    reader = load_review_state()
    monkeypatch.setitem(sys.modules, "review_state", reader)
    root = Path(os.environ.get("HORISTES_READER_SOURCE", ROOT))
    monkeypatch.setattr(sys.modules[__name__], "SCRIPT", root / "tools/merge_train.py")
    return load_train(tmp_path, monkeypatch)


def adr92_head_detail(p, state="COMMENTED", login="coderabbitai", oid=None):
    review = {
        "author": {"login": login},
        "state": state,
        "body": "",
        "submittedAt": "2026-11-16T17:00:00Z",
        "commit": {"oid": p["headRefOid"] if oid is None else oid},
    }
    return {**_detail(reviews=[review]), "headRefOid": p["headRefOid"]}


@pytest.mark.parametrize(
    ("state", "login", "oid", "holds"),
    [
        ("COMMENTED", "coderabbitai", "a" * 40, True),
        ("COMMENTED", "coderabbitai", None, False),
        ("APPROVED", "coderabbitai", None, False),
        ("CHANGES_REQUESTED", "coderabbitai", None, False),
        ("DISMISSED", "coderabbitai", None, True),
        ("PENDING", "coderabbitai", None, True),
        ("COMMENTED", "other", None, True),
    ],
    ids=["A-old", "A-head", "A-approved", "A-changes", "A-dismissed", "A-pending", "A-other"],
)
def test_adr92_head_evidence(tmp_path, monkeypatch, state, login, oid, holds):
    mod = adr92_train(tmp_path, monkeypatch)
    p = pull(1, "CLEAN", author={"login": "person"})
    detail = adr92_head_detail(p, state, login, oid)
    reason = adr92_body_hold(mod, p, detail, frozenset({"coderabbitai"}))
    assert bool(reason) is holds
    if holds:
        assert reason == (
            "review body: review incomplete: no completed review by coderabbitai "
            "on head " + p["headRefOid"][:7]
        )


def test_adr92_zero_review(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    p = pull(1, "CLEAN", author={"login": "person"})
    reason = adr92_body_hold(mod, p, _detail(), frozenset({"coderabbitai"}))
    assert reason is not None
    assert "no completed review" in reason


def test_adr92_allowance_head_evidence(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    p = pull(1, "CLEAN", author={"login": "person"})
    body = (
        "Reviewing files that changed from the base of the PR and between "
        + "a" * 40
        + " and "
        + p["headRefOid"]
        + ".\n0 remain after this review"
    )
    summary = _completed_review_summary("2026-11-16T17:00:00Z") | {"body": body}
    detail = _detail(comments=[summary])
    detail["commits"]["nodes"] = [{"commit": {"committedDate": "2026-11-16T16:10:16Z"}}]
    detail["commits"]["totalCount"] = 1
    assert adr92_body_hold(mod, p, detail, frozenset({"coderabbitai"})) is None


@pytest.mark.parametrize("kind", ["reviews", "comments", "commits"])
@pytest.mark.parametrize("raw", [{"a": 1}, 1, "str"])
def test_adr92_malformed_connections(tmp_path, monkeypatch, kind, raw):
    mod = adr92_train(tmp_path, monkeypatch)
    detail = _detail() | {kind: {"nodes": raw}}
    assert mod.review_body_hold(pull(1, "CLEAN"), detail) == "review body: cannot read " + kind


def test_adr92_moved_head(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    detail = _detail() | {"headRefOid": "a" * 40}
    assert mod.review_body_hold(pull(1, "CLEAN"), detail) == (
        "review body: head moved while reading reviews"
    )


def test_adr92_missing_detail_head(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    detail.pop("headRefOid")
    detail["comments"]["nodes"] = [
        {
            "author": {"login": "coderabbitai"},
            "createdAt": "2026-11-16T16:00:00Z",
            "body": "Review limit reached for commit " + p["headRefOid"] + ".",
        }
    ]
    detail["comments"]["totalCount"] = 1
    reason = mod.review_body_hold(p, detail)
    assert reason is not None
    assert "review incomplete" in reason


@pytest.mark.parametrize("completed", [False, True], ids=["A-hold", "A-clear"])
def test_adr92_zero_review_train(tmp_path, completed):
    p = pull(1, "CLEAN", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = adr92_head_detail(p) if completed else _detail()
    done, summary = train(tmp_path, forge, review_bots="CodeRabbitAI[bot]")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([landed(1)] if completed else [])
    assert ("no completed review by coderabbitai" in summary) is not completed


def test_adr92_zero_review_off_summary(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN", author={"login": "person"})))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert summary.count("review bots: none set, zero-review hold off") == 1


@pytest.mark.parametrize("value", ["bad_name", "-bad", "a" * 40, "bot[other]", "bot user"])
def test_adr92_invalid_review_bots(tmp_path, value):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, review_bots=value)
    assert done.returncode == 3
    assert "review_bots" in done.stdout
    assert forge.writes == []


@pytest.mark.parametrize("late_notice", [True, False], ids=["I-hold", "I-clear"])
def test_adr92_arming_rereads(tmp_path, late_notice):
    p, detail = _incomplete_review_pull()
    p["mergeStateStatus"] = "BEHIND"
    forge = Forge(repository(p))
    forge.review_details[1] = [_detail(), detail if late_notice else _detail()]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    expected = [("arm", "PR_1", p["headRefOid"], "SQUASH"), ("update", 1, p["headRefOid"])]
    assert forge.writes == ([] if late_notice else expected)
    if late_notice:
        assert forge.review_body_calls == [1, 1]
    assert ("review body: review incomplete" in summary) is late_notice


def test_adr92_review_bots_updates_without_arming(tmp_path):
    p = pull(1, "BEHIND", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = adr92_head_detail(p)
    done, _ = train(tmp_path, forge, review_bots="CodeRabbitAI[bot]")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]

    forge_unset = Forge(repository(p))
    forge_unset.review_details[1] = adr92_head_detail(p)
    done_unset, _ = train(tmp_path, forge_unset)
    assert done_unset.returncode == 0, done_unset.stdout + done_unset.stderr
    assert forge_unset.writes == [("arm", "PR_1", p["headRefOid"], "SQUASH"), updated(1)]


def test_adr92_review_bots_disarms_before_update(tmp_path):
    p = pull(1, "BEHIND", author={"login": "person"}, autoMergeRequest={"enabledAt": OLD})
    forge = Forge(repository(p))
    forge.review_details[1] = adr92_head_detail(p)
    done, summary = train(tmp_path, forge, review_bots="CodeRabbitAI[bot]")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1), updated(1)]
    assert "| #1 | BEHIND | disarmed, updated from the default branch |" in summary


def test_adr92_review_bots_drifted_not_re_armed(tmp_path):
    p = pull(1, "BLOCKED", author={"login": "person"}, autoMergeRequest=armed_squash())
    forge = Forge(repository(p))
    forge.review_details[1] = adr92_head_detail(p)
    done, summary = train(tmp_path, forge, review_bots="CodeRabbitAI[bot]")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [disarmed(1)]
    assert "disarmed: title changed after arming, the train lands it directly" in summary


@pytest.mark.parametrize("late_notice", [False, True], ids=["R3-clear", "R3-hold"])
def test_adr92_re_arm_rereads(tmp_path, late_notice):
    p, detail = _incomplete_review_pull(armed=True)
    p.update(mergeStateStatus="BLOCKED", autoMergeRequest=armed_squash())
    forge = Forge(repository(p))
    forge.review_details[1] = [_detail(), detail if late_notice else _detail()]
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    expected = [disarmed(1), ("arm", "PR_1", p["headRefOid"], "SQUASH")]
    assert forge.writes == ([disarmed(1)] if late_notice else expected)
    assert forge.review_body_calls == [1, 1]
    assert ("disarmed: review body: review incomplete" in summary) is late_notice
    assert ("re-armed: title changed after arming" in summary) is not late_notice


def test_adr92_arming_allowance_beside_admission(tmp_path):
    pulls = [pull(number, "BEHIND", author={"login": "person"}) for number in range(1, 11)]
    forge = Forge(repository(*pulls))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.review_body_calls == [*range(1, 11), 1]
    assert forge.writes == [("arm", "PR_1", pulls[0]["headRefOid"], "SQUASH"), updated(1)]
    assert "budget spent" not in summary


def test_adr92_review_bots_arming_mutant_arms(tmp_path):
    mutant = module().replace(
        'arms = not conf.get("required_status") and not conf.get("review_bots")',
        'arms = not conf.get("required_status")',
    )
    assert mutant != module()
    p = pull(1, "BEHIND", author={"login": "person"})
    forge = Forge(repository(p))
    forge.review_details[1] = adr92_head_detail(p)
    done, _ = train(tmp_path, forge, helper=mutant, review_bots="CodeRabbitAI[bot]")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("arm", "PR_1", p["headRefOid"], "SQUASH"), updated(1)]


@pytest.fixture(autouse=True)
def adr92_offline_source(monkeypatch):
    """An explicit baseline root redirects only module loading, never repository state."""
    source = os.environ.get("HORISTES_READER_SOURCE")
    if source:
        monkeypatch.setitem(sys.modules, "review_state", load_review_state())
        monkeypatch.delitem(sys.modules, "train_rules", raising=False)
        monkeypatch.setattr(sys.modules[__name__], "SCRIPT", Path(source) / "tools/merge_train.py")
        workflow = Path(source) / ".github/workflows/merge-train.yml"
        if workflow.is_file():
            monkeypatch.setattr(sys.modules[__name__], "TRAIN", workflow)


def adr92_body_hold(mod, p, detail, review_bots):
    """Replay the old signature explicitly so baseline failures assert behavior, not API shape."""
    import inspect

    if "review_bots" not in inspect.signature(mod.review_body_hold).parameters:
        return mod.review_body_hold(p, detail)
    return mod.review_body_hold(p, detail, review_bots=review_bots)


@pytest.mark.parametrize(
    "when, holds",
    [
        ("2026-11-16T16:10:16Z", True),
        ("2026-11-16T16:10:15Z", False),
    ],
    ids=["H-equal", "H-before"],
)
def test_adr92_notice_boundary_without_evidence(tmp_path, monkeypatch, when, holds):
    mod = adr92_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    detail["comments"]["nodes"] = [
        {"author": {"login": "coderabbitai"}, "createdAt": when, "body": "Review limit reached."}
    ]
    detail["comments"]["totalCount"] = 1
    assert bool(mod.review_body_hold(p, detail)) is holds


def test_adr114_h2_open_findings_hold(tmp_path, monkeypatch):
    """Budget exemption clears incomplete only; unanswered Sourcery findings still hold."""
    from test_review_state import ADR92_COMMIT, ADR92_HEAD, ADR114_BUDGET, adr114_head_checks

    mod = load_train(tmp_path, monkeypatch)
    p = pull(1, "CLEAN", author={"login": "person"}, headRefOid=ADR92_HEAD)
    detail = _detail(
        reviews=[
            _bot_review(9101, "sourcery-ai[bot]", "## Overall Comments\nPlease address this."),
        ],
        comments=[
            {
                "author": {"login": "sourcery-ai[bot]"},
                "createdAt": "2026-11-16T16:40:51Z",
                "body": ADR114_BUDGET,
            }
        ],
        commits=["c"],
    )
    detail["headRefOid"] = ADR92_HEAD
    detail["commits"] = {
        "totalCount": 1,
        "nodes": [{"commit": {"message": "c", "committedDate": ADR92_COMMIT}}],
    }
    detail["headChecks"] = adr114_head_checks()
    reason = mod.review_body_hold(p, detail)
    assert reason is not None
    assert "review incomplete" not in reason
    assert "sourcery" in reason.lower() and "findings" in reason.lower()


def test_adr92_arming_read_budget(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    p, detail = _incomplete_review_pull()
    p["mergeStateStatus"] = "BEHIND"
    second = pull(2, "BEHIND", author={"login": "person"})
    forge = Forge(repository(p, second))
    forge.review_details[1] = [_detail(), detail]
    forge.retry_histories[1] = retry_history(1) | {"headRefOid": p["headRefOid"]}
    source = SCRIPT.read_text()
    assert source.count("ARMING_READS = 3") == 1
    done, summary = train(
        tmp_path, forge, text=source.replace("ARMING_READS = 3", "ARMING_READS = 1")
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []
    assert forge.review_body_calls == [1, 2, 1]
    assert "review body: review incomplete" in summary
    assert (
        "| #2 | BEHIND | review body: not armed: review read budget spent this sweep |" in summary
    )
    assert mod.ARMING_READS == 3
    assert mod.REVIEW_READS == 10


# Each row restores one admitted defect, with an exact replacement anchor.
def adr92_mutations():
    return [
        (
            "A",
            "merge_train",
            "if not review_bots:",
            "if review_bots:",
            "test_adr92_zero_review_train[A-hold]",
        ),
        ("B", "review_state", "|Sorry @", "|UnavailableSorry @", "B-sourcery"),
        (
            "C",
            "review_state",
            'updated = _instant(comment["updatedAt"])',
            "updated = created",
            "C-edited-ordinary-notice",
        ),
        (
            "D",
            "review_state",
            "if who != login or when is None:",
            "if when is None:",
            "D-other-login",
        ),
        (
            "E",
            "review_state",
            "holds = allowance or paused or names_head or when >= last_commit",
            "holds = allowance or paused or names_head or when > last_commit",
            "E-equal-notice",
        ),
        (
            "E-head-evidence",
            "review_state",
            "(on_head or not head_only)",
            "(not head_only)",
            "allowance-later-review-on-head-clears",
        ),
        (
            "F-nodes",
            "review_state",
            " if isinstance(raw, list) else []",
            "",
            "test_adr92_nodes_malformed[raw0]",
        ),
        (
            "F-head",
            "merge_train",
            'if detail.get("headRefOid") and pull.get("headRefOid")',
            'if not detail.get("headRefOid") and pull.get("headRefOid")',
            "test_adr92_moved_head",
        ),
        (
            "I",
            "merge_train",
            "if arming and late_review_hold(env, conf, pull, notes, arming=True):",
            "if arming and False:",
            "test_adr92_arming_rereads[I-hold]",
        ),
    ] + adr92_followup_mutations()


# ADR-0114 rows: each removes one leg of the Sourcery budget exemption.
def adr114_mutations():
    return [
        (
            "114",
            "review_state",
            'all(match.group("budget") for match in notices[:MAX_LINES])',
            "all(False for match in notices[:MAX_LINES])",
            "test_adr114_p1_pass",
        ),
        (
            "114-any-text",
            "review_state",
            'all(match.group("budget") for match in notices[:MAX_LINES])',
            "all(True for match in notices[:MAX_LINES])",
            "H3-skip-text",
        ),
        (
            "114-run-summary",
            "review_state",
            '                or not match.group("budget")',
            "                or False",
            "H3-run-limit",
        ),
        (
            "114-multi-commit",
            "review_state",
            "    if isinstance(count, bool) or not isinstance(count, int) or count < 1:",
            (
                "    if isinstance(count, bool) or not isinstance(count, int) "
                "or count < 1 or count != 1:"
            ),
            "p3_multi_commit",
        ),
    ]


def adr92_followup_mutations():
    return [
        (
            "C3-bool",
            "merge_train",
            "if isinstance(count, bool) or not isinstance(count, int) or count < 0:",
            "if not isinstance(count, int) or count < 0:",
            "test_adr92_unreadable_connection_counts and bool-equals-count",
        ),
        (
            "R1-disarm",
            "merge_train",
            '(conf.get("required_status") or conf.get("review_bots")) and pull["autoMergeRequest"]',
            'conf.get("required_status") and pull["autoMergeRequest"]',
            "test_adr92_review_bots_disarms_before_update",
        ),
        (
            "R1-drift",
            "merge_train",
            'if conf.get("required_status") or conf.get("review_bots"):',
            'if conf.get("required_status"):',
            "test_adr92_review_bots_drifted_not_re_armed",
        ),
        (
            "R3",
            "merge_train",
            "if late_review_hold(env, conf, pull, notes, arming=True):",
            "if False:",
            "test_adr92_re_arm_rereads[R3-hold]",
        ),
        (
            "R2",
            "merge_train",
            'counter = "arming_reads" if arming else "review_reads"',
            'counter = "review_reads"',
            "test_adr92_arming_allowance_beside_admission",
        ),
    ] + adr114_mutations()


def adr92_mutation_run(root, selector, log):
    """Run only the named regression files; writes bytecode nowhere."""
    env = {**os.environ, "HORISTES_READER_SOURCE": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "tests/test_review_state.py",
        "tests/test_merge_train.py",
        "-k",
        selector,
    ]
    done = run_train_process(command, descriptor="ADR-92/93 pytest", env=env, timeout=120, cwd=ROOT)
    log.write_text(done.stdout + done.stderr)
    return done


@pytest.mark.parametrize(
    ("section", "module_name", "old", "new", "selector"),
    adr92_mutations(),
    ids=[row[0] for row in adr92_mutations()[:20]],
)
def test_adr92_mutations_reject_defects(tmp_path, section, module_name, old, new, selector):
    root = tmp_path / "mutation"
    (root / "tools").mkdir(parents=True)
    for name in ("review_state", "merge_train", "release_workflows"):
        source = (ROOT / "tools" / f"{name}.py").read_text()
        (root / "tools" / f"{name}.py").write_text(source)
    path = root / "tools" / f"{module_name}.py"
    source = path.read_text()
    assert source.count(old) == 1, (section, old)
    control = adr92_mutation_run(root, selector, tmp_path / "control.log")
    assert control.returncode == 0, control.stdout + control.stderr
    path.write_text(source.replace(old, new))
    mutated = adr92_mutation_run(root, selector, tmp_path / "mutated.log")
    assert mutated.returncode == 1, mutated.stdout + mutated.stderr
    assert "1 failed" in mutated.stdout, mutated.stdout


@pytest.mark.parametrize(
    "connection",
    [
        None,
        [],
        {"nodes": [], "totalCount": "0"},
        {"nodes": [], "totalCount": -1},
        {"nodes": [], "totalCount": True},
        {"nodes": [], "totalCount": 1},
        {"nodes": [None], "totalCount": 1},
        pytest.param({"nodes": [{}], "totalCount": True}, id="bool-equals-count"),
        pytest.param({"nodes": [{}], "totalCount": 1.0}, id="float-equals-count"),
    ],
)
def test_adr92_unreadable_connection_counts(tmp_path, monkeypatch, connection):
    mod = adr92_train(tmp_path, monkeypatch)
    detail = _detail() | {"comments": connection}
    assert mod.review_body_hold(pull(1, "CLEAN"), detail) == "review body: cannot read comments"


def test_adr92_readable_connection_count(tmp_path, monkeypatch):
    mod = adr92_train(tmp_path, monkeypatch)
    detail = _detail(comments=[{}])
    assert detail["comments"]["totalCount"] == 1
    assert mod.review_body_hold(pull(1, "CLEAN"), detail) is None


def test_adr92_empty_review_bots_summary(tmp_path, monkeypatch, capfd):
    mod = adr92_train(tmp_path, monkeypatch)
    env = {
        "TRAIN_REVIEW_BOTS": " , \t\n",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }
    watch_report_files(env)
    records = mod.summary(env, {}, [], [], [], [], [], [])
    if records is not None:
        mod.emit(env, records)
    assert capfd.readouterr().out.count("review bots: none set, zero-review hold off") == 1


@pytest.mark.parametrize("all_bot", [False, True], ids=["person-holds", "bots-land"])
def test_adr93_dependency_paused_train(tmp_path, all_bot):
    """A bot PR with person commits reaches the reader; a pure bot update stays exempt."""
    node = json.loads((REVIEW_STATE_FIXTURES / "dependency-person-commits-paused.json").read_text())
    p = pull(
        1,
        "CLEAN",
        author=node["author"],
        headRefName=node["headRefName"],
        headRefOid=node["headRefOid"],
        authors=node["authors"],
        committedDate="2026-11-17T02:24:32Z",
    )
    if all_bot:
        p["authors"] = build_commit_authors({}, ["renovate[bot]"] * 5)
    forge = Forge(repository(p))
    forge.review_details[1] = {
        key: node[key] for key in ("headRefOid", "author", "comments", "reviews", "commits")
    }
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    if all_bot:
        assert forge.writes == [("merge", "PR_1", p["headRefOid"], "SQUASH")]
        assert forge.review_body_calls == []
        assert "### fleet/app: landed #1" in summary
    else:
        assert forge.writes == []
        assert forge.review_body_calls == [1]
        assert "review incomplete: coderabbitai limit reached" in summary


@pytest.mark.parametrize(
    ("logins", "exempt"),
    [
        pytest.param(["renovate[bot]", "plurano-merge-train[bot]"], True, id="all-bot"),
        pytest.param(["renovate[bot]", "a-person", "renovate[bot]"], False, id="interior-person"),
        pytest.param(["renovate[bot]", "a-person"], False, id="head-person"),
        pytest.param([None], False, id="null-user"),
        pytest.param(["renovate[bot]"] * 50, True, id="fifty"),
    ],
)
def test_adr93_bot_commit_authors(tmp_path, logins, exempt):
    rules = load_rules(tmp_path)
    p = pull(
        1,
        "CLEAN",
        headRefName="renovate/x",
        author={"login": "renovate", "__typename": "Bot"},
        commit_authors=logins,
    )
    assert rules.is_bot_dependency(p, {"dependency": ("renovate/",)}) is exempt


@pytest.mark.parametrize(
    "authors",
    [
        pytest.param(_ABSENT, id="missing"),
        pytest.param(None, id="null"),
        pytest.param([], id="not-mapping"),
        pytest.param(
            {"totalCount": 51, "nodes": build_commit_authors({}, ["renovate[bot]"] * 50)["nodes"]},
            id="truncated-51",
        ),
        pytest.param({"totalCount": 0, "nodes": []}, id="zero"),
        pytest.param({"totalCount": True, "nodes": [{}]}, id="bool-count"),
        pytest.param({"totalCount": "1", "nodes": [{}]}, id="string-count"),
        pytest.param({"totalCount": -1, "nodes": []}, id="negative-count"),
        pytest.param({"totalCount": 1, "nodes": []}, id="short-nodes"),
        pytest.param({"totalCount": 1, "nodes": {}}, id="mapping-nodes"),
        pytest.param({"totalCount": 1, "nodes": [None]}, id="null-node"),
        pytest.param({"totalCount": 1, "nodes": [{"commit": None}]}, id="null-commit"),
        pytest.param({"totalCount": 1, "nodes": [{"commit": {"author": []}}]}, id="bad-author"),
        pytest.param(
            {"totalCount": 1, "nodes": [{"commit": {"author": {"user": {"login": 3}}}}]},
            id="bad-login",
        ),
    ],
)
def test_adr93_unreadable_authors(tmp_path, authors):
    rules = load_rules(tmp_path)
    p = pull(
        1, "CLEAN", headRefName="renovate/x", author={"login": "renovate", "__typename": "Bot"}
    )
    if authors is _ABSENT:
        p.pop("authors")
    else:
        p["authors"] = authors
    assert rules.is_bot_dependency(p, {"dependency": ("renovate/",)}) is False


def test_adr93_author_query_bound(tmp_path):
    rules = load_rules(tmp_path)
    bound = load_review_state().BOT_COMMIT_READS
    assert bound == 50
    expected = (
        f"authors: commits(last: {bound}) {{\n"
        "          totalCount nodes { commit { author { user { login } } } } }"
    )
    assert expected in rules.QUERY
    assert expected in rules.STATUS_QUERY


def adr93_mutations():
    return [
        (
            "A1",
            "review_state",
            "return all(_is_bot_author(_commit_user(node)) for node in nodes[:BOT_COMMIT_READS])",
            "return True",
            "test_adr93_dependency_paused_train[person-holds]",
        ),
        (
            "A2",
            "review_state",
            "nodes[:BOT_COMMIT_READS]",
            "nodes[-1:]",
            "test_adr93_bot_commit_authors[interior-person]",
        ),
        (
            "B1",
            "review_state",
            "allowance or paused or names_head",
            "allowance or names_head",
            "test_adr93_paused_notice[stale]",
        ),
    ]


@pytest.mark.parametrize(
    ("section", "module_name", "old", "new", "selector"),
    adr93_mutations(),
    ids=[row[0] for row in adr93_mutations()[:3]],
)
def test_adr93_mutations_reject_defects(tmp_path, section, module_name, old, new, selector):
    """Restore each defect only in temporary source copies, after an unmutated pass."""
    root = tmp_path / "mutation"
    (root / "tools").mkdir(parents=True)
    workflow = root / ".github/workflows/merge-train.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(TRAIN.read_text())
    for name in ("review_state", "merge_train", "release_workflows"):
        (root / "tools" / f"{name}.py").write_text((ROOT / "tools" / f"{name}.py").read_text())
    path = workflow if module_name == "rules" else root / "tools" / f"{module_name}.py"
    source = path.read_text()
    expected_count = 2 if section == "B1" else 1
    assert source.count(old) == expected_count, (section, old)
    control = adr92_mutation_run(root, selector, tmp_path / "control.log")
    assert control.returncode == 0, control.stdout + control.stderr
    path.write_text(source.replace(old, new))
    mutated = adr92_mutation_run(root, selector, tmp_path / "mutated.log")
    assert mutated.returncode == 1, mutated.stdout + mutated.stderr
    assert "1 failed" in mutated.stdout, mutated.stdout


@pytest.mark.parametrize("write_file", [False, True])
def test_summary_release_file_matches_output(tmp_path, monkeypatch, write_file):
    mod = load_train(tmp_path, monkeypatch)
    path = tmp_path / "releases.json"
    env = {
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "TRAIN_HOLD_LABELS": "hold,exact ",
    }
    if write_file:
        env["TRAIN_RELEASE_FILE"] = str(path)
    watch_report_files(env)
    releases = [{"owner": "a", "name": f"r{i}"} for i in range(mod.MAX_RELEASES + 1)]
    mod.emit(env, mod.summary(env, {}, [], [], [], releases, [], []))
    mod.write_release_outputs(env, releases)
    assert len(releases) == mod.MAX_RELEASES
    expected = json.dumps(releases, separators=(",", ":"))
    assert (tmp_path / "output").read_text(encoding="utf-8") == f"releases={expected}\n"
    assert path.exists() == write_file
    if write_file:
        payload = {"format": 1, "hold_labels": "hold,exact ", "releases": releases_of(tmp_path)}
        assert path.read_bytes() == json.dumps(payload, separators=(",", ":")).encode()


@pytest.mark.parametrize("mutation", ["extra-open", "wrong-path", "wrong-mode"])
def test_report_writer_open_allow_list_refuses_other_files(mutation):
    source = script()
    if mutation == "extra-open":
        source += "\nopen('unexpected.txt', 'w')\n"
    elif mutation == "wrong-path":
        source = source.replace(
            'open(path, mode, encoding="utf-8")', 'open("unexpected.txt", mode, encoding="utf-8")'
        )
    else:
        source = source.replace('mode = "w" if replace else "a"', 'mode = "w"')
    assert not allowed_report_opens(source)
    assert key_on_disk(source) == ["open() outside the summary and release file writers"]


@pytest.mark.parametrize(
    "hold", ["x\ny", "x\ry", "x" * 4097], ids=["newline", "return", "oversize"]
)
def test_hold_labels_setting_refused_before_requests(tmp_path, hold):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, hold_labels=hold)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "hold_labels" in done.stdout
    assert (forge.minted, forge.writes) == ([], [])


def test_hold_labels_setting_boundary(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, hold_labels="x" * 4096)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
