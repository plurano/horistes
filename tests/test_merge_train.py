# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The merge train's script, run as written against a stand-in forge (decision record 0029).

The script of the step "Land one clean pull request, or update the next one"
is taken out of the reusable workflow and run with the environment the step
sets. A small HTTP server answers the GraphQL and REST calls it makes and
records every write. Only the wait between two reads of an UNKNOWN merge state
is shortened.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

TRAIN = ROOT / ".github/workflows/merge-train.yml"
STEP = "Land one clean pull request, or update the next one"
NOW = datetime.now(UTC)
OLD = (NOW - timedelta(days=10)).isoformat()
YOUNG = (NOW - timedelta(hours=5)).isoformat()
CONFLICT = "merge conflict between base and head"
NO_NEW = "There are no new commits on the base branch."


def script() -> str:
    steps = [s for s in ciwf.load(TRAIN)["jobs"]["train"]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1, f"one step named {STEP!r}"
    return steps[0]["run"]


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
    """What the stand-in forge serves, and every write it received."""

    def __init__(self, *reads: dict) -> None:
        self.reads = list(reads)
        self.writes: list[tuple] = []
        self.merged = True
        self.refuse_merge = ""
        self.update_answers: dict[int, tuple[int, str]] = {}

    def graphql(self, query: str, variables: dict) -> dict:
        if "mergePullRequest" in query:
            self.writes.append(("merge", variables["id"], variables["oid"], variables["method"]))
            if self.refuse_merge:
                return {"errors": [{"message": self.refuse_merge}]}
            return {"data": {"mergePullRequest": {"pullRequest": {"merged": self.merged}}}}
        if "enablePullRequestAutoMerge" in query:
            self.writes.append(("arm", variables["id"], variables["oid"], variables["method"]))
            armed = {"autoMergeRequest": {"enabledAt": NOW.isoformat()}}
            return {"data": {"enablePullRequestAutoMerge": {"pullRequest": armed}}}
        assert variables == {"owner": "fleet", "name": "app"}, variables
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        return {"data": {"repository": read}}

    def update(self, number: int, body: dict) -> tuple[int, dict]:
        self.writes.append(("update", number, body["expected_head_sha"]))
        code, message = self.update_answers.get(number, (202, "Updating pull request branch."))
        return code, {"message": message}


def serve(forge: Forge) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def answer(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> dict:
            assert self.headers["Authorization"] == "Bearer planted-token"
            return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

        def do_POST(self) -> None:
            assert self.path == "/graphql", self.path
            request = self.body()
            self.answer(200, forge.graphql(request["query"], request["variables"]))

        def do_PUT(self) -> None:
            prefix, _, tail = self.path.rpartition("/pulls/")
            number, _, action = tail.partition("/")
            assert (prefix, action) == ("/repos/fleet/app", "update-branch"), self.path
            self.answer(*forge.update(int(number), self.body()))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def train(tmp_path: Path, forge: Forge, text: str | None = None, **settings: str):
    """Run the step's script against the stand-in forge; the result and the summary written."""
    source = (text or script()).replace("SETTLE_SECONDS = 10", "SETTLE_SECONDS = 0")
    path = tmp_path / "train.py"
    path.write_text(source, encoding="utf-8")
    summary = tmp_path / "summary.md"
    server = serve(forge)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    env = {
        "GITHUB_REPOSITORY": "fleet/app",
        "GITHUB_API_URL": url,
        "GITHUB_GRAPHQL_URL": f"{url}/graphql",
        "GITHUB_STEP_SUMMARY": str(summary),
        "TRAIN_TOKEN": "planted-token",
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
            timeout=60,
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
    assert "Merge train: landed #1" in summary


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
    assert forge.writes == []


def test_merge_method_merge_is_passed(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    done, _ = train(tmp_path, forge, merge_method="merge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == [("merge", "PR_1", f"{1:040x}", "MERGE")]


def test_token_is_never_printed(tmp_path):
    forge = Forge(repository(pull(1, "CLEAN")))
    forge.refuse_merge = "refused"
    done, summary = train(tmp_path, forge)
    assert "planted-token" not in done.stdout + done.stderr + summary


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


def test_app_rights_are_the_rights_the_token_step_asks_for():
    """The app holds what the train's token needs, metadata read (always granted), nothing else."""
    token = ciwf.load(TRAIN)["jobs"]["train"]["steps"][0]
    assert token["uses"].startswith("actions/create-github-app-token@")
    asked = {
        key.removeprefix("permission-").replace("-", "_"): value
        for key, value in token["with"].items()
        if key.startswith("permission-")
    }
    assert asked == {
        "contents": "write",
        "pull_requests": "write",
        "workflows": "write",
        "checks": "read",
    }
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["default_permissions"] == {**asked, "metadata": "read"}


def test_token_is_minted_for_the_calling_repository_only():
    token = ciwf.load(TRAIN)["jobs"]["train"]["steps"][0]["with"]
    assert not {"owner", "repositories", "skip-token-revoke"} & set(token)


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
