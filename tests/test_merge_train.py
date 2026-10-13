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
import json
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

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
WRITE = {"contents": "write", "pull_requests": "write", "workflows": "write", "checks": "read"}
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


def repository(*pulls: dict, auto_merge: bool = True) -> dict:
    return {
        "defaultBranchRef": {"name": "main"},
        "autoMergeAllowed": auto_merge,
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
        self.install(7, "fleet", app=list(reads))

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

    def graphql(self, header: str, query: str, variables: dict) -> dict:
        full, rights = self.repo_of(header)
        if "mergePullRequest" in query or "enablePullRequestAutoMerge" in query:
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

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/app/installations":
            if not self.app_call():
                self.answer(200, self.server.forge.installation_list())
        elif path == "/installation/repositories":
            self.answer(200, self.server.forge.repositories(self.headers["Authorization"]))
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
    forge.workdir = tmp_path
    server = serve(forge)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_API_URL": url,
        "GITHUB_GRAPHQL_URL": f"{url}/graphql",
        "GITHUB_STEP_SUMMARY": str(summary),
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
    out = done.stdout + done.stderr + summary
    assert "planted-token" not in out and "list-token" not in out
    body = "".join(app_key()[0].splitlines()[1:-1])
    assert body[:40] not in out.replace("\n", "")
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "jwt.pub",
        "jwt.sig",
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
