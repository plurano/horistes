# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The merge train's script, run as written against a stand-in forge (decision record 0029).

The script of the step "Land or update one pull request in every repository of
the app" is taken out of the reusable workflow and run with the environment the
step sets. A small HTTP server stands in for the forge: it checks the app's JWT
against a key made for the test run, lists installations and their
repositories, mints and revokes tokens, answers the GraphQL and REST calls of
the train and records every write. Only the wait between two reads of an
UNKNOWN merge state is shortened.
"""

from __future__ import annotations

import base64
import functools
import hashlib
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
from urllib.parse import parse_qs, urlsplit

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
CLIENT_ID = "Iv23planted"
WRITE = {
    "actions": "read",
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "checks": "read",
}
READ = {"contents": "read", "pull_requests": "read", "checks": "read"}


def script() -> str:
    steps = [s for s in ciwf.load(TRAIN)["jobs"]["train"]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1, f"one step named {STEP!r}"
    return steps[0]["run"]


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
    node = {
        "number": number,
        "id": f"PR_{number}",
        "title": fields.pop("title", f"change {number}"),
        "isDraft": False,
        "isCrossRepository": False,
        "baseRefName": "main",
        "headRefName": f"feat/change-{number}",
        "headRefOid": f"{number:040x}",
        "mergeStateStatus": state,
        "autoMergeRequest": None,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "history": {
            "nodes": [
                {"commit": {"committedDate": when, "parents": {"totalCount": parents}}}
                for when, parents in commits
            ]
        },
        "latest": {
            "nodes": [{"commit": {"checkSuites": {"nodes": [{"conclusion": c} for c in suites]}}}]
        },
    }
    node.update(fields)
    return node


def repository(*pulls: dict, auto_merge: bool = True, release: tuple = (False, False)) -> dict:
    """One repository as the query reads it; `release` says which release-please files it has."""
    config, manifest = release
    return {
        "defaultBranchRef": {"name": "main"},
        "autoMergeAllowed": auto_merge,
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
        self.archived: set[str] = set()
        self.tokens: dict[str, tuple] = {}
        self.minted: list[tuple[int, dict]] = []
        self.revoked: list[str] = []
        self.writes: list[tuple] = []
        self.where: list[tuple[str, tuple]] = []
        self.merged = True
        self.refuse_merge = ""
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

    def graphql(self, header: str, query: str, variables: dict) -> dict:
        full, rights = self.repo_of(header)
        if (
            "mergePullRequest" in query
            or "enablePullRequestAutoMerge" in query
            or "createCommitOnBranch" in query
        ):
            assert rights == WRITE, rights
        if "mergePullRequest" in query:
            self.write(full, ("merge", variables["id"], variables["oid"], variables["method"]))
            if self.refuse_merge or full in self.refuse_merge_in:
                return {"errors": [{"message": self.refuse_merge or "refused here"}]}
            return {"data": {"mergePullRequest": {"pullRequest": {"merged": self.merged}}}}
        if "enablePullRequestAutoMerge" in query:
            self.write(full, ("arm", variables["id"], variables["oid"], variables["method"]))
            armed = {"autoMergeRequest": {"enabledAt": NOW.isoformat()}}
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": armed}}}
        if "createCommitOnBranch" in query:
            return self._handle_commit_mutation(full, variables)
        owner, name = full.split("/")
        assert variables == {"owner": owner, "name": name}, variables
        reads = self.reads[full]
        read = reads.pop(0) if len(reads) > 1 else reads[0]
        return {"data": {"repository": read}}

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
            if self.headers.get("Authorization"):
                self.server.forge.users_auth_seen.append(self.headers["Authorization"])
                self.answer(401, {"message": "Users endpoint does not accept Authorization header"})
                return True
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
        else:
            raise AssertionError(self.path)

    def do_POST(self) -> None:
        if self.path == "/graphql":
            request = self.body()
            header = self.headers["Authorization"]
            self.answer(
                200, self.server.forge.graphql(header, request["query"], request["variables"])
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

    def do_DELETE(self) -> None:
        assert self.path == "/installation/token", self.path
        self.server.forge.revoked.append(self.headers["Authorization"].removeprefix("Bearer "))
        self.answer(204, None)


def serve(forge: Forge) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.forge = forge
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def train(tmp_path: Path, forge: Forge, text: str | None = None, **settings: str):
    """Run the step's script against the stand-in forge; the result and the summary written."""
    source = (text or script()).replace("SETTLE_SECONDS = 5", "SETTLE_SECONDS = 0")
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
        "TRAIN_APP_CLIENT_ID": CLIENT_ID,
        "TRAIN_APP_KEY": app_key()[0],
        "TRAIN_SWITCH": "on",
        "TRAIN_MERGE_METHOD": "SQUASH",
        "TRAIN_ACCEPT_STATES": "",
        "TRAIN_MIN_AGE_DAYS": "3",
        "TRAIN_HOLD_LABELS": "hold,do-not-merge,planted",
        "TRAIN_DEPENDENCY_BRANCHES": "renovate/ dependabot/",
        "TRAIN_PERSON_BRANCHES": "release-please--",
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


def updated(number: int) -> tuple:
    return ("update", number, f"{number:040x}")


def test_clean_pull_request_lands(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, summary = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [landed(1)]
    assert "### fleet/app: landed #1" in summary


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
    mutant = script().replace('if state in conf["accept"]:', "if True:")
    assert mutant != script()
    forge = Forge(repository(pull(1, "BLOCKED", suites=["FAILURE"])))
    done, _ = train(tmp_path, forge, mutant)
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


def test_behind_is_only_updated_without_auto_merge(tmp_path):
    forge = Forge(repository(pull(1, "BEHIND"), auto_merge=False))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [updated(1)]


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
    ]


def test_script_passes_the_python_linter(tmp_path):
    path = tmp_path / "train.py"
    path.write_text(script(), encoding="utf-8")
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
    assert manifest["default_permissions"] == {**WRITE, "metadata": "read"}


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
    if len(minting) != 1 or len(running) != 1:
        return [f"{len(minting)} token steps and {len(running)} release-please steps"]
    mint, run = minting[0]["with"], running[0]["with"]
    if run.get("token") != f"${{{{ steps.{minting[0].get('id')}.outputs.token }}}}":
        found.append(f"release-please token is {run.get('token')!r}, not the app's")
    if mint.get("owner") != "${{ matrix.repository.owner }}":
        found.append("token not minted for the repository's owner")
    if mint.get("repositories") != "${{ matrix.repository.name }}":
        found.append("token not limited to the repository")
    rights = {key: value for key, value in mint.items() if key.startswith("permission-")}
    if rights != TOKEN_RIGHTS:
        found.append(f"token rights {rights}")
    if run.get("repo-url") != "${{ matrix.repository.owner }}/${{ matrix.repository.name }}":
        found.append("release-please runs on another repository")
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
    ("change", "finding"),
    [
        (
            lambda w: w.update(token="${{ github.token }}"),
            "release-please token is '${{ github.token }}', not the app's",
        ),
        (lambda w: w.pop("token"), "release-please token is None, not the app's"),
    ],
)
def test_github_token_for_release_please_is_found(change, finding):
    """GITHUB_TOKEN opens release pull requests that start no checks (cordanaLLM/imago#58)."""
    doc = ciwf.load(TRAIN)
    step = [s for s in doc["jobs"]["release"]["steps"] if s["uses"].startswith(RELEASE_PLEASE)][0]
    change(step["with"])
    assert release_job_findings(doc) == [finding]


def test_release_job_mutants_are_found():
    doc = ciwf.load(TRAIN)
    job = doc["jobs"]["release"]
    mint = [s for s in job["steps"] if s["uses"].startswith(APP_TOKEN)][0]["with"]
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


def test_bot_identity_users_endpoint_refuses_authorization(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge)
    assert done.returncode == 0
    assert len(forge.users_auth_seen) == 0


def test_without_bot_identity_unauthenticated_users_call_fails(tmp_path):
    mutant = script().replace(
        'call(f"{env[\'GITHUB_API_URL\']}/users/{login}", None, "GET")',
        'call(f"{env[\'GITHUB_API_URL\']}/users/{login}", jwt, "GET")',
    )
    assert mutant != script()
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, mutant)
    assert done.returncode == 1
    assert len(forge.users_auth_seen) > 0


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
