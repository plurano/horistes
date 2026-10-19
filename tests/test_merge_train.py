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
import functools
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

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


def script() -> str:
    steps = [s for s in ciwf.load(TRAIN)["jobs"]["train"]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1, f"one step named {STEP!r}"
    return steps[0]["run"]


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
def app_key(which: str = "app") -> tuple[str, bytes]:
    """A key made for this test run: the private PEM text and the public PEM."""
    del which
    private = openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048")
    return private.decode(), openssl("pkey", "-pubout", data=private)


def pull(number: int, state: str, **fields) -> dict:
    """One open pull request as the query reads it."""
    commits = fields.pop("commits", [(OLD, 1)])
    suites = fields.pop("suites", ["SUCCESS"])
    labels = fields.pop("labels", ())
    suite_nodes = []
    base_time = datetime(2026, 10, 18, 0, 0, 0, tzinfo=UTC)
    for i, item in enumerate(suites):
        if isinstance(item, dict):
            suite_nodes.append(item)
        else:
            created_at = (base_time + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            suite_nodes.append(
                {
                    "databaseId": 1000 + i,
                    "conclusion": item,
                    "createdAt": created_at,
                    "app": {"slug": "github-actions"},
                    "workflowRun": {"workflow": {"databaseId": 100 + i}},
                }
            )
    node = {
        "number": number,
        "id": f"PR_{number}",
        "title": fields.pop("title", f"change {number}"),
        "body": fields.pop("body", ""),
        "isDraft": False,
        "isCrossRepository": False,
        "baseRefName": "main",
        "headRefName": f"feat/change-{number}",
        "headRefOid": f"{number:040x}",
        "mergeStateStatus": state,
        "autoMergeRequest": None,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "refreshes": {"nodes": []},
        "history": {
            "nodes": [
                {"commit": {"committedDate": when, "parents": {"totalCount": parents}}}
                for when, parents in commits
            ]
        },
        "latest": {"nodes": [{"commit": {"checkSuites": {"nodes": suite_nodes}}}]},
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
        self.storage_auth_seen: list[str] = []
        self.users_auth_seen: list[str] = []
        self.artifact_redirect_locations: dict[int, str | None] = {}
        self.refuse_commit = False
        self.messages: list[tuple[str, str, str | None, str | None]] = []
        self.commits: dict[int, list[tuple[str, int]]] = {}
        self.commit_counts: dict[int, int] = {}
        self.merged_pulls: dict[str, list[dict]] = {}
        self.issue_right = True
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
        if (
            "mergePullRequest" in query
            or "enqueuePullRequest" in query
            or "enablePullRequestAutoMerge" in query
            or "createCommitOnBranch" in query
        ):
            assert rights == WRITE, rights
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
        if "pullRequest(" in query:
            return self.pull_commits(full, variables)
        if "states: MERGED" in query:
            nodes = self.merged_pulls.get(full, [])
            return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}
        return self.pull_page(full, query, variables)

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
    source = (text or script()).replace("SETTLE_SECONDS = 5", "SETTLE_SECONDS = 0")
    source = source.replace("READ_BACKOFF_SECONDS = 5", "READ_BACKOFF_SECONDS = 0")
    path = tmp_path / "train.py"
    path.write_text(source, encoding="utf-8")
    summary = tmp_path / "summary.md"
    output = tmp_path / "output"
    forge.workdir = tmp_path
    server = serve(forge)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    env = {
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
        "TRAIN_SECURITY_LABELS": "security",
        **{f"TRAIN_{key.upper()}": value for key, value in settings.items()},
    }
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(path)],
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


def queued(number: int) -> tuple:
    return ("enqueue", f"PR_{number}", f"{number:040x}")


def updated(number: int) -> tuple:
    return ("update", number, f"{number:040x}")


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
    ("commits", "fields", "lands"),
    [
        ([(YOUNG, 1)], {}, False),
        ([(OLD, 1)], {}, True),
        ([(OLD, 1), (YOUNG, 2)], {}, True),
        ([(OLD, 1), (YOUNG, 1)], {}, True),
        ([(YOUNG, 2)], {}, False),
        ([(YOUNG, 1)], {"labels": ("security",)}, True),
        ([(YOUNG, 1)], {"title": "fix(deps): update x [SECURITY]"}, True),
    ],
    ids=["young", "old", "merge-on-top", "repin-on-top", "only-merges", "label", "title"],
)
def test_dependency_update_waits_for_the_minimum_age(tmp_path, commits, fields, lands):
    update = pull(1, "CLEAN", headRefName="renovate/tool-1.x", commits=commits, **fields)
    forge = Forge(repository(update))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == ([landed(1)] if lands else [])


def test_minimum_age_zero_lands_a_young_update(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN", headRefName="dependabot/x", commits=[(YOUNG, 1)])))
    done, _ = train(tmp_path, forge, min_age_days="0")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]


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
    ],
)
def test_invalid_setting_stops_before_any_call(tmp_path, key, value):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, **{key: value})
    assert done.returncode == 3, done.stdout + done.stderr
    assert done.stdout.startswith("::error title=Merge train setting::")
    assert (forge.minted, forge.writes) == ([], [])


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
        "train.py",
        "train_rules.py",
    ]


@pytest.mark.parametrize("name", ["train", "train_rules"])
def test_script_passes_the_python_linter(tmp_path, name):
    path = tmp_path / f"{name}.py"
    path.write_text(script() if name == "train" else module(), encoding="utf-8")
    ruff = ROOT / ".venv/bin/ruff"
    command = [str(ruff)] if ruff.exists() else [sys.executable, "-m", "ruff"]
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


def test_release_job_runs_release_please_with_the_app_token():
    assert release_job_findings(ciwf.load(TRAIN)) == []


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
    doc = ciwf.load(TRAIN)
    job = doc["jobs"]["release"]
    mint = [s for s in job["steps"] if s.get("uses", "").startswith(APP_TOKEN)][0]["with"]
    mint.pop("repositories")
    mint["permission-issues"] = "write"
    job["if"] = job["if"].replace("inputs.switch == 'on'", "true")
    assert release_job_findings(doc) == [
        "token not limited to the repository",
        "token rights {'permission-contents': 'write', 'permission-pull-requests': 'write', "
        "'permission-workflows': 'write', 'permission-issues': 'write'}",
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
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    assert "| #1 | BEHIND | generated files committed |" in summary
    assert "| fleet | 1 | 0 | 1 | 0 |" in summary


def test_negative_no_allowlist_file(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    assert "artifact exceeds" in summary


def test_negative_switch_off(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep", labels=["hold"])
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
    mutant = script().replace("or p not in allowlist", "or False")
    assert mutant != script()
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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


def test_negative_artifact_storage_scheme_mismatch(tmp_path):
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
    forge.artifact_redirect_locations[101] = "ftp://storage.example.com/evil.zip"
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "storage URL scheme 'ftp' != 'http'" in summary


def test_negative_artifact_storage_location_empty(tmp_path):
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
    forge.artifact_redirect_locations[101] = ""
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0
    assert not any(w[0] == "commit" for w in forge.writes)
    assert "generated files refused:" in summary
    assert "expected 302 redirect to storage URL" in summary


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
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    head1 = f"{1:040x}"
    head2 = f"{2:040x}"
    p1 = pull(1, "BEHIND", headRefName="renovate/demo-1", headRefOid=head1)
    p2 = pull(2, "BEHIND", headRefName="renovate/demo-2", headRefOid=head2)
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
        "| #1 | BEHIND | generated files refused: fleet/app #1: path 'forbidden.txt' not allowed |"
        in summary
    )
    assert "| #2 | BEHIND | generated files committed |" in summary
    assert "### fleet/app: committed generated files to #2" in summary


def test_refused_artifact_falls_through_to_land_clean_pull(tmp_path):
    head1 = f"{1:040x}"
    p1 = pull(1, "BEHIND", headRefName="renovate/demo-1", headRefOid=head1)
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
        "| #1 | BEHIND | generated files refused: fleet/app #1: path 'forbidden.txt' not allowed |"
        in summary
    )
    assert "| #2 | CLEAN | landed |" in summary
    assert "### fleet/app: landed #2" in summary


def test_commit_mutation_refusal_fails_repository_run(tmp_path):
    head = f"{1:040x}"
    dep_pull = pull(1, "BEHIND", headRefName="renovate/demo-dep")
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
    head = f"{1:040x}"
    rel_pull = pull(1, "BEHIND", headRefName="release-please--branches--main")
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
        "Merge branch 'main' into feat/change-1\n\n"
        "Signed-off-by: plurano-merge-train[bot] "
        "<1+plurano-merge-train[bot]@users.noreply.github.com>",
        2,
    ),
    (
        "fix: follow up\n\nSecond body.\n\n"
        "Co-authored-by: plurano-merge-train[bot] "
        "<1+plurano-merge-train[bot]@users.noreply.github.com>\n"
        "Signed-off-by: Dev <dev@example.org>",
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
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.commits[1] = [
        ("feat: a\n\nSigned-off-by: First <first@example.org>", 1),
        (
            "feat: b\n\n"
            "Signed-off-by: Second <second@example.org>\n"
            "Signed-off-by: First <first@example.org>",
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
            "* feat: a\n\n* feat: b\n\n"
            "Signed-off-by: First <first@example.org>\n"
            "Signed-off-by: Second <second@example.org>",
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
    forge.commits[1] = [("feat: unsigned", 1)]
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
    "        rows, notes, lambda run: run_jobs(env, run),"
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
