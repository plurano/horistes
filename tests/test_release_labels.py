# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The release job's labels module, run as written against a stand-in forge (decision record 0040).

The module the step "Write the release labels module" writes is taken out of
the reusable workflow and run with `python3 -I`, as the steps after it run it.
A small HTTP server stands in for the forge's REST API: the config file, tags,
releases and label writes, each write recorded; and for its GraphQL API, which
answers the closed and open pull requests with the fields the checks read.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

TRAIN = ROOT / ".github/workflows/merge-train.yml"
MODULE_STEP = "Write the release labels module"
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TAG_OBJECT = "c" * 40
PENDING = "autorelease: pending"
TAGGED = "autorelease: tagged"
ONE = json.dumps([{"number": 10, "sha": COMMIT_A, "tag": "v1.0.0"}], separators=(",", ":"))
MOVED = [("post_label", 10, [TAGGED]), ("delete_label", 10, PENDING)]
HOLD = "hold,do-not-merge,planted"


def module() -> str:
    """The release labels module the step writes."""
    steps = [
        s for s in ciwf.load(TRAIN)["jobs"]["release"]["steps"] if s.get("name") == MODULE_STEP
    ]
    assert len(steps) == 1, f"one step named {MODULE_STEP!r}"
    lines = steps[0]["run"].splitlines()
    assert lines[0] == """cat > "$RUNNER_TEMP/release_labels.py" <<'PY'""", lines[0]
    assert lines[-1] == "PY", lines[-1]
    return "\n".join(lines[1:-1]) + "\n"


def release_pull(number: int = 10, labels: tuple = (PENDING,), **fields) -> dict:
    """One merged release pull request as the pulls API answers it."""
    pull = {
        "number": number,
        "merged_at": "2026-10-18T00:00:00Z",
        "merge_commit_sha": COMMIT_A,
        "base": {"ref": "main"},
        "head": {"ref": "release-please--branches--main"},
        "title": "chore(main): release 1.0.0",
        "labels": [{"name": name} for name in labels],
    }
    pull.update(fields)
    return pull


class Forge:
    """What the stand-in forge serves, and every write it saw."""

    def __init__(self) -> None:
        self.config: dict | None = None
        self.workflows: dict[str, str] | None = None
        self.pulls: list[dict] = []
        self.tags: dict[str, tuple[str, str]] = {}
        self.tag_objects: dict[str, tuple[str, str]] = {}
        self.releases: set[str] = set()
        self.writes: list[tuple] = []
        self.release_writes: list[str] = []
        self.cut: list[int] = []
        self.short: list[int] = []
        self.url = ""

    def contents(self, _rest: str) -> tuple[int, object]:
        if self.config is None:
            return 404, {"message": "Not Found"}
        content = base64.b64encode(json.dumps(self.config).encode()).decode()
        return 200, {"type": "file", "encoding": "base64", "content": content}

    def workflow_contents(self, rest: str) -> tuple[int, object]:
        if self.workflows is None:
            return 404, {"message": "Not Found"}
        rest = rest.lstrip("/")
        if not rest:
            entries = [
                {
                    "name": name,
                    "path": f".github/workflows/{name}",
                    "type": "file",
                    "url": f"{self.url}/repos/fleet/app/contents/.github/workflows/{name}",
                }
                for name in sorted(self.workflows)
            ]
            return 200, entries
        if rest not in self.workflows:
            return 404, {"message": "Not Found"}
        content = base64.b64encode(self.workflows[rest].encode("utf-8")).decode("utf-8")
        return 200, {"type": "file", "encoding": "base64", "content": content}

    def pull(self, rest: str) -> tuple[int, object]:
        found = [p for p in self.pulls if str(p["number"]) == rest]
        return (200, found[0]) if found else (404, {"message": "Not Found"})

    def matching_refs(self, tag: str) -> tuple[int, object]:
        if tag not in self.tags:
            return 200, []
        sha, kind = self.tags[tag]
        return 200, [{"ref": f"refs/tags/{tag}", "object": {"sha": sha, "type": kind}}]

    def tag_object(self, sha: str) -> tuple[int, object]:
        if sha not in self.tag_objects:
            return 404, {"message": "Not Found"}
        target, kind = self.tag_objects[sha]
        return 200, {"sha": sha, "object": {"sha": target, "type": kind}}

    def release(self, tag: str) -> tuple[int, object]:
        return (200, {"tag_name": tag}) if tag in self.releases else (404, {"message": "Not Found"})

    def graphql(self, request: dict) -> tuple[int, object]:
        """The pull request read: the closed and open lists, in the shape GraphQL answers."""
        assert request["variables"] == {"owner": "fleet", "name": "app"}, request["variables"]
        if self.config is None:
            return 200, {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND"}]}

        def node(pull: dict) -> dict:
            sha = pull.get("merge_commit_sha")
            return {
                "number": pull["number"],
                "state": "OPEN" if pull.get("state") == "open" else "MERGED",
                "title": pull.get("title"),
                "mergedAt": pull.get("merged_at"),
                "mergeCommit": {"oid": sha} if sha else None,
                "baseRefName": (pull.get("base") or {}).get("ref"),
                "headRefName": (pull.get("head") or {}).get("ref"),
                "labels": {"nodes": pull.get("labels", [])},
            }

        opened = [node(p) for p in self.pulls if p.get("state") == "open"]
        closed = [node(p) for p in self.pulls if p.get("state") != "open"]
        repo = {"closed": {"nodes": closed}, "open": {"nodes": opened}}
        body = json.dumps({"data": {"repository": repo}}).encode()
        return 200, body[: self.cut.pop(0)] if self.cut else body

    def get(self, path: str) -> tuple[int, object]:
        """The answer to one GET, by the part of the path after fleet/app."""
        rest = path.removeprefix("/repos/fleet/app/")
        routes = (
            ("contents/release-please-config.json", self.contents),
            ("contents/.github/workflows", self.workflow_contents),
            ("pulls/", self.pull),
            ("git/matching-refs/tags/", self.matching_refs),
            ("git/tags/", self.tag_object),
            ("releases/tags/", self.release),
        )
        for prefix, answer in routes:
            if rest.startswith(prefix):
                return answer(unquote(rest.removeprefix(prefix)))
        return 404, {"message": "Not Found"}


class Handler(BaseHTTPRequestHandler):
    """Serves the forge on the server and records its writes."""

    def log_message(self, *args: object) -> None:
        del args

    def answer(self, code: int, body: object, short: int | None = None) -> None:
        """The body whole, or cut at `short` bytes under the whole length, as the forge cuts it."""
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data if short is None else data[:short])

    def do_GET(self) -> None:
        self.answer(*self.server.forge.get(urlsplit(self.path).path))  # type: ignore[attr-defined]

    def do_POST(self) -> None:
        forge: Forge = self.server.forge  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        if path == "/graphql":
            code, body = forge.graphql(json.loads(self.rfile.read(length)))
            self.answer(code, body, forge.short.pop(0) if forge.short else None)
            return
        if "/releases" in path:
            forge.release_writes.append(path)
            self.answer(405, {"message": "no release is written in these tests"})
            return
        labels = json.loads(self.rfile.read(length) or b"{}").get("labels", [])
        number = int(path.split("/issues/")[1].removesuffix("/labels"))
        forge.writes.append(("post_label", number, labels))
        self.answer(200, [{"name": name} for name in labels])

    def do_DELETE(self) -> None:
        forge: Forge = self.server.forge  # type: ignore[attr-defined]
        head, _, label = urlsplit(self.path).path.partition("/labels/")
        forge.writes.append(("delete_label", int(head.split("/issues/")[1]), unquote(label)))
        self.answer(200, [])


@pytest.fixture
def forge() -> Iterator[Forge]:
    """A stand-in forge on a free local port."""
    stand_in = Forge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.forge = stand_in  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stand_in.url = f"http://127.0.0.1:{server.server_port}"
    yield stand_in
    server.shutdown()
    server.server_close()


def run(
    tmp_path: Path, command: str, env: dict, source: str | None = None
) -> subprocess.CompletedProcess[str]:
    """The module run with python3 -I, as a step runs it."""
    path = tmp_path / "release_labels.py"
    path.write_text(source or module(), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", str(path), command],
        cwd=tmp_path,
        env={**os.environ, "TOKEN": "planted", "REPO": "fleet/app", **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def settings(
    tmp_path: Path, forge: Forge, source: str | None = None
) -> tuple[subprocess.CompletedProcess[str], str]:
    """The settings command and the outputs it wrote."""
    out = tmp_path / "output"
    out.write_text("", encoding="utf-8")
    env = {
        "GITHUB_API_URL": forge.url,
        "GITHUB_GRAPHQL_URL": f"{forge.url}/graphql",
        "GITHUB_OUTPUT": str(out),
        "HOLD_LABELS": HOLD,
    }
    done = run(tmp_path, "settings", env, source)
    return done, out.read_text(encoding="utf-8")


def after(
    tmp_path: Path,
    forge: Forge,
    skip: str,
    outcome: str,
    pending: str = ONE,
    source: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The command of the step after the release step."""
    env = {"GITHUB_API_URL": forge.url, "SKIP": skip, "OUTCOME": outcome, "PENDING": pending}
    return run(tmp_path, "after", env, source)


def test_module_passes_ruff(tmp_path):
    path = tmp_path / "release_labels.py"
    path.write_text(module(), encoding="utf-8")
    ruff = ROOT / ".venv/bin/ruff"
    command = [str(ruff)] if ruff.exists() else [sys.executable, "-m", "ruff"]
    for args in (["check"], ["format", "--check"]):
        done = subprocess.run(
            [*command, *args, str(path)], capture_output=True, text=True, check=False, timeout=60
        )
        assert done.returncode == 0, done.stdout + done.stderr


def test_skipped_release_moves_the_label_and_writes_no_release(tmp_path, forge):
    """The planted repository writes its own release: no release, label moved, tagged first."""
    forge.config = {"skip-github-release": True}
    forge.pulls = [release_pull()]
    forge.tags = {"v1.0.0": (COMMIT_A, "commit")}
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == f"skip=true\npending={ONE}\nfrozen=false\n"
    done = after(tmp_path, forge, "true", "skipped")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (forge.writes, forge.release_writes) == (MOVED, [])


def test_without_the_move_the_label_stays(tmp_path, forge):
    mutant = module().replace(
        'call(post_url, token, method="POST", body={"labels": [TAGGED_LABEL]})', "pass"
    )
    mutant = mutant.replace('call(del_url, token, method="DELETE")', "pass")
    assert mutant != module()
    forge.tags = {"v1.0.0": (COMMIT_A, "commit")}
    done = after(tmp_path, forge, "true", "skipped", source=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []


@pytest.mark.parametrize("mutated", [False, True])
def test_label_waits_for_the_tag_on_the_merge_commit(tmp_path, forge, mutated):
    """A tag on another commit leaves the label; the mutant without the check moves it."""
    source = module()
    if mutated:
        source = source.replace('if commit == item["sha"]:', "if True:")
        assert source != module()
    forge.tags = {"v1.0.0": (COMMIT_B, "commit")}
    done = after(tmp_path, forge, "true", "skipped", source=source)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == (MOVED if mutated else [])
    assert ("stays pending" in done.stdout) is not mutated


@pytest.mark.parametrize(("kind", "moved"), [("commit", MOVED), ("tree", [])])
def test_annotated_tag_is_followed_to_a_commit_only(tmp_path, forge, kind, moved):
    forge.tags = {"v1.0.0": (TAG_OBJECT, "tag")}
    forge.tag_objects = {TAG_OBJECT: (COMMIT_A, kind)}
    done = after(tmp_path, forge, "true", "skipped")
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == moved


def existing_release(forge: Forge) -> None:
    """release-please moved the label, the release exists, its tag names the merge commit."""
    forge.pulls = [release_pull(labels=(TAGGED,))]
    forge.tags = {"v1.0.0": (COMMIT_A, "commit")}
    forge.releases = {"v1.0.0"}


def test_existing_release_on_the_release_commit_is_done(tmp_path, forge):
    existing_release(forge)
    done = after(tmp_path, forge, "false", "failure")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::notice::release v1.0.0 existed on the release commit; done" in done.stdout
    assert forge.writes == []


def tag_elsewhere(forge: Forge) -> None:
    forge.tags = {"v1.0.0": (COMMIT_B, "commit")}


def still_pending(forge: Forge) -> None:
    forge.pulls = [release_pull()]


def no_release(forge: Forge) -> None:
    forge.releases = set()


CHECKS = [
    (tag_elsewhere, ONE, 'if commit != item["sha"]:', f"commit {COMMIT_B} != {COMMIT_A}"),
    (still_pending, ONE, "if PENDING_LABEL in labels:", f"still carries {PENDING}"),
    (no_release, ONE, "if status != 200:", "release v1.0.0 returned HTTP 404"),
    (lambda forge: None, "[]", "if not pending:", "release step failed and pending is empty"),
]


@pytest.mark.parametrize(("plant", "pending", "check", "error"), CHECKS)
def test_any_other_failed_release_stays_red(tmp_path, forge, plant, pending, check, error):
    existing_release(forge)
    plant(forge)
    done = after(tmp_path, forge, "false", "failure", pending)
    assert done.returncode == 1, done.stdout + done.stderr
    assert error in done.stdout


@pytest.mark.parametrize(("plant", "pending", "check", "error"), CHECKS)
def test_without_each_check_the_failure_turns_green(tmp_path, forge, plant, pending, check, error):
    del error
    mutant = module().replace(check, "if False:")
    assert mutant != module()
    existing_release(forge)
    plant(forge)
    done = after(tmp_path, forge, "false", "failure", pending, source=mutant)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("outcome", ["success", "skipped"])
def test_release_step_that_did_not_fail_needs_no_judge(tmp_path, forge, outcome):
    done = after(tmp_path, forge, "false", outcome)
    assert done.returncode == 0, done.stdout + done.stderr
    assert forge.writes == []


@pytest.mark.parametrize("outcome", ["cancelled", ""])
def test_unexpected_outcome_is_red(tmp_path, forge, outcome):
    done = after(tmp_path, forge, "false", outcome)
    assert done.returncode == 1
    assert "unexpected release outcome" in done.stdout


@pytest.mark.parametrize(
    ("config", "skip"),
    [
        ({}, "false"),
        ({"skip-github-release": True, "packages": {".": {}}}, "true"),
        (
            {"packages": {"a": {"skip-github-release": True}, "b": {"skip-github-release": True}}},
            "true",
        ),
        ({"skip-github-release": True, "packages": {".": {"skip-github-release": False}}}, "false"),
    ],
)
def test_skip_is_the_effective_value_of_every_package(tmp_path, forge, config, skip):
    forge.config = config
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == f"skip={skip}\npending=[]\nfrozen=false\n"


def test_mixed_skip_is_an_error(tmp_path, forge):
    forge.config = {"packages": {"a": {"skip-github-release": True}, "b": {}}}
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 1
    assert "::error::mixed skip-github-release across packages: a=True, b=False" in done.stdout
    assert outputs == ""


def test_tag_without_v_when_the_config_says_so(tmp_path, forge):
    forge.config = {"include-v-in-tag": False}
    forge.pulls = [release_pull(title="chore(main): release 0.2.0")]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert '"tag":"0.2.0"' in outputs


@pytest.mark.parametrize(
    "fields",
    [
        {"merged_at": None},
        {"head": {"ref": "feat/release"}},
        {"title": "chore(main): release 1.0"},
        {"merge_commit_sha": "a" * 39},
    ],
)
def test_pending_pull_failing_a_release_check_is_named_not_listed(tmp_path, forge, fields):
    forge.config = {}
    forge.pulls = [release_pull(**fields), release_pull(11, labels=(TAGGED,))]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::warning::pull #10 labelled autorelease: pending fails release checks" in done.stdout
    assert outputs == "skip=false\npending=[]\nfrozen=false\n"


def test_refused_read_is_an_error_line(tmp_path, forge):
    done, _ = settings(tmp_path, forge)
    assert done.returncode == 1
    assert done.stdout.startswith("::error title=Release labels::settings:")
    assert "Traceback" not in done.stderr


SANDBOX_HEAD = "release-please--branches--main--components--sandbox"
SANDBOX_CONFIG = {
    "include-component-in-tag": False,
    "packages": {".": {"package-name": "sandbox"}},
}


def component_release(forge: Forge) -> None:
    """jellysin/release-helper-sandbox pull 87: a component branch, no component in the tag."""
    forge.config = SANDBOX_CONFIG
    forge.pulls = [release_pull(head={"ref": SANDBOX_HEAD})]


def test_component_branch_is_a_release_pull(tmp_path, forge):
    component_release(forge)
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == f"skip=false\npending={ONE}\nfrozen=false\n"


def test_component_branch_release_that_existed_is_done(tmp_path, forge):
    """The k8s main run of 7e34991b: release-please moved the label, the release existed."""
    component_release(forge)
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    pending = outputs.split("pending=", 1)[1].split("\n", 1)[0]
    existing_release(forge)
    forge.pulls = [release_pull(labels=(TAGGED,), head={"ref": SANDBOX_HEAD})]
    done = after(tmp_path, forge, "false", "failure", pending)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "release v1.0.0 existed on the release commit; done" in done.stdout


def test_without_the_component_branch_the_release_stays_red(tmp_path, forge):
    """The mutant that reads only the bare branch name leaves pending empty, as on k8s."""
    source = module().replace("(?:--components--(.+))?", "")
    assert source != module()
    component_release(forge)
    settings_path = tmp_path / "output"
    settings_path.write_text("", encoding="utf-8")
    env = {"GITHUB_API_URL": forge.url, "GITHUB_OUTPUT": str(settings_path)}
    done = run(tmp_path, "settings", env, source)
    assert "fails release checks" in done.stdout
    pending = settings_path.read_text(encoding="utf-8").split("pending=", 1)[1].split("\n", 1)[0]
    existing_release(forge)
    done = after(tmp_path, forge, "false", "failure", pending, source=source)
    assert done.returncode == 1
    assert "release step failed and pending is empty" in done.stdout


@pytest.mark.parametrize(
    ("config", "head", "title", "tag"),
    [
        ({}, "main--components--app", "chore(main): release app 1.0.0", "app-v1.0.0"),
        (
            {"tag-separator": "/"},
            "main--components--app",
            "chore(main): release 1.0.0",
            "app/v1.0.0",
        ),
        (
            {
                "packages": {
                    "a": {"component": "a"},
                    "b": {"component": "b", "include-v-in-tag": False},
                }
            },
            "main--components--b",
            "chore(main): release b 2.0.0",
            "b-2.0.0",
        ),
        ({}, "main", "chore: release 1.0.0-rc.1", "v1.0.0-rc.1"),
        ({}, "main", "chore(main): release v1.0.0", "v1.0.0"),
    ],
)
def test_tag_follows_release_please(tmp_path, forge, config, head, title, tag):
    forge.config = config
    forge.pulls = [release_pull(head={"ref": f"release-please--branches--{head}"}, title=title)]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f'"tag":"{tag}"' in outputs


@pytest.mark.parametrize(
    ("config", "title"),
    [
        ({"pull-request-title-pattern": "release: ${version}"}, "release: 1.0.0"),
        ({}, "chore(other): release 1.0.0"),
        ({}, "chore(main): release main"),
    ],
)
def test_title_release_please_would_not_read_is_named(tmp_path, forge, config, title):
    forge.config = config
    forge.pulls = [release_pull(title=title)]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::warning::pull #10 labelled autorelease: pending fails release checks" in done.stdout
    assert outputs == "skip=false\npending=[]\nfrozen=false\n"


def open_release_pull(
    number: int, labels: tuple, head: str = "release-please--branches--main"
) -> dict:
    """One open release pull request as the pulls API answers it."""
    return {
        "number": number,
        "state": "open",
        "merged_at": None,
        "base": {"ref": "main"},
        "head": {"ref": head},
        "title": "chore(main): release 1.0.0",
        "labels": [{"name": name} for name in labels],
    }


@pytest.mark.parametrize(
    ("labels", "frozen"),
    [
        ((PENDING, "autorelease: cut"), "true"),
        ((PENDING, "hold"), "true"),
        ((PENDING, "type:chore", "autorelease: cut", "hold"), "true"),
        ((PENDING,), "false"),
        ((PENDING, "type:chore"), "false"),
    ],
)
def test_frozen_release_pull_request_is_not_updated(tmp_path, forge, labels, frozen):
    """The planted case: an open release pull request a person cut or held stays as it is."""
    forge.config = {}
    forge.pulls = [open_release_pull(11, labels)]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs.endswith(f"frozen={frozen}\n"), outputs
    assert ("is frozen; not updated" in done.stdout) == (frozen == "true")


def test_label_on_another_head_freezes_nothing(tmp_path, forge):
    forge.config = {}
    forge.pulls = [open_release_pull(11, (PENDING, "hold"), head="feat/change-1")]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs.endswith("frozen=false\n"), outputs


def test_without_the_frozen_read_the_cut_pull_request_is_updated(tmp_path, forge):
    mutant = module().replace("named = sorted((labels & hold) | release)", "named = []")
    assert mutant != module()
    forge.config = {}
    forge.pulls = [open_release_pull(11, (PENDING, "autorelease: cut", "hold"))]
    done, outputs = settings(tmp_path, forge, mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs.endswith("frozen=false\n"), outputs


def test_truncated_read_is_retried(tmp_path, forge):
    forge.config = {}
    forge.pulls = [release_pull()]
    forge.cut = [100]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"pending={ONE}" in outputs
    assert forge.cut == []


def test_three_truncated_reads_fail_by_name(tmp_path, forge):
    forge.config = {}
    forge.pulls = [release_pull()]
    forge.cut = [100, 100, 100, 100]
    done, _ = settings(tmp_path, forge)
    assert done.returncode == 1
    assert "invalid JSON response after 3 attempts" in done.stdout
    assert forge.cut == [100]


def test_cut_body_is_retried(tmp_path, forge):
    """The forge's own cut: the whole length announced, the connection closed early."""
    forge.config = {}
    forge.pulls = [release_pull()]
    forge.short = [100]
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"pending={ONE}" in outputs
    assert forge.short == []


def test_three_cut_bodies_fail_by_name(tmp_path, forge):
    """The planted case of cordanaLLM/imago run 40041378238: a cut that repeats is named a cut."""
    forge.config = {}
    forge.pulls = [release_pull()]
    forge.short = [100, 100, 100, 100]
    done, _ = settings(tmp_path, forge)
    assert done.returncode == 1
    assert "/graphql: body cut after 3 attempts: 100 of " in done.stdout, done.stdout
    assert forge.short == [100]


def test_without_the_length_check_the_cut_is_not_named(tmp_path, forge):
    check = "if length and length.isdigit() and len(raw) != int(length):"
    mutant = module().replace(check, "if False:")
    assert mutant != module()
    forge.config = {}
    forge.pulls = [release_pull()]
    forge.short = [100, 100, 100]
    done, _ = settings(tmp_path, forge, mutant)
    assert done.returncode == 1
    assert "body cut" not in done.stdout, done.stdout


def test_large_page_of_full_pulls_reads_whole(tmp_path, forge):
    """Twenty closed and a hundred open pulls, near 900 kB, read to the end."""
    forge.config = {}
    long_title = "chore: " + "x" * 7500
    forge.pulls = [
        release_pull(),
        *(release_pull(20 + n, labels=(TAGGED,), title=long_title) for n in range(19)),
        *(open_release_pull(100 + n, ("type:chore",), head=f"feat/{n}") for n in range(100)),
    ]
    for pull in forge.pulls[20:]:
        pull["title"] = long_title
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == f"skip=false\npending={ONE}\nfrozen=false\n", outputs


PLANTED_WORKFLOW = """name: Release
on:
  push:
    branches: [main]
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7 # v5.0.0
"""
UNRELATED_WORKFLOW = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      # mentions googleapis/release-please-action in a comment
      - uses: actions/checkout@v4
"""


@pytest.mark.parametrize("mutated", [False, True])
def test_planted_own_release_workflow_skips_release(tmp_path, forge, mutated):
    """A repository running release-please in its own workflow sets skip=true."""
    source = module()
    if mutated:
        source = source.replace(
            "def reads_own_release(api_url, repo, token):",
            "def reads_own_release(api_url, repo, token):\n    return False",
        )
        assert source != module()
    forge.config = {}
    forge.workflows = {"release-please.yml": PLANTED_WORKFLOW}
    done, outputs = settings(tmp_path, forge, source=source)
    assert done.returncode == 0, done.stdout + done.stderr
    expected_skip = "false" if mutated else "true"
    assert outputs == f"skip={expected_skip}\npending=[]\nfrozen=false\n"
    assert (
        "::notice::fleet/app runs release-please in its own workflow release-please.yml; "
        "the train creates no release\n" in done.stdout
    ) is not mutated


def test_unrelated_workflow_leaves_skip_false(tmp_path, forge):
    """Workflows without release-please-action keep skip=false."""
    forge.config = {}
    forge.workflows = {"ci.yml": UNRELATED_WORKFLOW}
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == "skip=false\npending=[]\nfrozen=false\n"
    assert "runs release-please in its own workflow" not in done.stdout


def test_missing_workflows_directory_leaves_skip_false(tmp_path, forge):
    """A 404 on .github/workflows leaves skip=false."""
    forge.config = {}
    forge.workflows = None
    done, outputs = settings(tmp_path, forge)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == "skip=false\npending=[]\nfrozen=false\n"
    assert "runs release-please in its own workflow" not in done.stdout


def test_without_reads_own_release_the_skip_stays_false(tmp_path, forge):
    """The mutant where reads_own_release returns False leaves skip=false in the planted case."""
    mutant = module().replace(
        "def reads_own_release(api_url, repo, token):",
        "def reads_own_release(api_url, repo, token):\n    return False",
    )
    assert mutant != module()
    forge.config = {}
    forge.workflows = {"release-please.yml": PLANTED_WORKFLOW}
    done, outputs = settings(tmp_path, forge, source=mutant)
    assert done.returncode == 0, done.stdout + done.stderr
    assert outputs == "skip=false\npending=[]\nfrozen=false\n"
    assert "runs release-please in its own workflow" not in done.stdout
