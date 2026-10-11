# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Forge reader: the answers of a stand-in client become subjects with checks.

The canned answers have the shape of real runs of this repository: a draft
pull request marked ready gives two runs per workflow on one commit, and a
failing script step leaves only its exit code as annotation.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
import types

import pytest
from conftest import PAGE, ROOT, forge_answers, forge_job, forge_run

sys.path.insert(0, str(ROOT / "tools"))
import forge

REPOSITORY = "fixture/consumer"
OLD_FILE, NEW_FILE = ".github/workflows/reuse.yml", ".github/workflows/licence-check.yml"
WATCHED = {OLD_FILE, NEW_FILE}
DRAFT = "Stop on a draft pull request"
COMMIT = "bfe95b5170e7e5e1e9681ea4d766c14d55e28851"
LISTING = f"repos/{REPOSITORY}/actions/runs?branch=trial%2Flicence&{PAGE}"


def draft_then_ready() -> list[dict]:
    """One commit, first as a draft (both jobs stop), then marked ready (both pass)."""
    note = "Gate not run on a draft"
    return [
        forge_run(
            11, OLD_FILE, COMMIT, "01:28:36", [forge_job(101, "REUSE lint", "failure", DRAFT, note)]
        ),
        forge_run(
            12,
            NEW_FILE,
            COMMIT,
            "01:28:36",
            [forge_job(102, "Licence / REUSE lint", "failure", DRAFT, note)],
        ),
        forge_run(
            13, ".github/workflows/ci.yml", COMMIT, "01:28:36", [forge_job(103, "Gate", "failure")]
        ),
        forge_run(21, OLD_FILE, COMMIT, "01:29:05", [forge_job(201, "REUSE lint", "success")]),
        forge_run(
            22, NEW_FILE, COMMIT, "01:29:05", [forge_job(202, "Licence / REUSE lint", "success")]
        ),
    ]


@pytest.fixture
def client(tree, monkeypatch):
    """Puts the stand-in client in the forge's place for calls made inside this process."""

    def serve(answers: dict) -> None:
        for name, value in tree.forge(answers).items():
            monkeypatch.setenv(name, value)

    # The reader's name `time` alone is replaced: the interpreter's own module also
    # paces its wait for a child process, and those steps are not the reader's waits.
    serve.waits = []
    monkeypatch.setattr(forge, "time", types.SimpleNamespace(sleep=serve.waits.append))
    return serve


def test_two_events_on_one_commit_give_two_subjects(tree, client):
    client(forge_answers(REPOSITORY, draft_then_ready()))
    data = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)
    assert data["errors"] == []
    assert data["source"] == "live forge" and data["repository"] == REPOSITORY
    first, second = data["subjects"]
    for number, subject in enumerate((first, second), 1):
        assert {k: v for k, v in subject.items() if k != "checks"} == {
            "commit": COMMIT,
            "event": "pull_request",
            "ref": "trial/licence",
            "occurrence": number,
        }
        assert [(c["name"], c["workflow"]) for c in subject["checks"]] == [
            ("REUSE lint", OLD_FILE),
            ("Licence / REUSE lint", NEW_FILE),
        ]
    assert [c["attempts"] for c in first["checks"]] == 2 * [
        [
            {
                "attempt": 1,
                "status": "completed",
                "conclusion": "failure",
                "failed_steps": [DRAFT],
                "annotations": ["Gate not run on a draft"],
                "seconds": 21.0,
            }
        ]
    ]
    assert [c["attempts"][0]["conclusion"] for c in second["checks"]] == ["success", "success"]
    assert [c["attempts"][0]["failed_steps"] for c in second["checks"]] == [[], []]


def test_only_the_named_workflows_are_read_and_only_failures_are_annotated(tree, client):
    client(forge_answers(REPOSITORY, draft_then_ready()))
    forge.collect(REPOSITORY, ["trial/licence"], WATCHED)
    runs = f"repos/{REPOSITORY}/actions/runs"
    assert sorted(tree.forge_calls()) == sorted(
        [
            LISTING,
            *(f"{runs}/{run}/attempts/1/jobs?{PAGE}" for run in (11, 12, 21, 22)),
            *(f"repos/{REPOSITORY}/check-runs/{job}/annotations?{PAGE}" for job in (101, 102)),
        ]
    )


def test_every_attempt_of_a_run_is_read(tree, client):
    again = forge_run(
        31,
        NEW_FILE,
        COMMIT,
        "01:40:00",
        [forge_job(301, "Licence / REUSE lint", "failure", "reuse lint", "exit code 1")],
        [forge_job(302, "Licence / REUSE lint", "success")],
    )
    client(forge_answers(REPOSITORY, [again]))
    data = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)
    (check,) = data["subjects"][0]["checks"]
    assert [(a["attempt"], a["conclusion"]) for a in check["attempts"]] == [
        (1, "failure"),
        (2, "success"),
    ]
    assert check["attempts"][0]["failed_steps"] == ["reuse lint"]


def test_runs_of_two_branches_and_two_events_stay_apart(tree, client):
    runs = [
        forge_run(41, OLD_FILE, COMMIT, "02:00:00", [forge_job(401, "REUSE lint", "success")]),
        forge_run(
            42,
            OLD_FILE,
            COMMIT,
            "02:00:01",
            [forge_job(402, "REUSE lint", "success")],
            event="push",
        ),
    ]
    answers = forge_answers(REPOSITORY, runs)
    answers[LISTING.replace("trial%2Flicence", "main")] = {"workflow_runs": []}
    client(answers)
    data = forge.collect(REPOSITORY, ["trial/licence", "main"], WATCHED)
    assert data["errors"] == []
    assert [(s["event"], s["occurrence"]) for s in data["subjects"]] == [
        ("pull_request", 1),
        ("push", 1),
    ]


def test_running_job_is_recorded_as_running(tree, client):
    run = forge_run(51, OLD_FILE, COMMIT, "02:10:00", [forge_job(501, "REUSE lint", None)])
    client(forge_answers(REPOSITORY, [run]))
    (attempt,) = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)["subjects"][0]["checks"][0][
        "attempts"
    ]
    assert (attempt["status"], attempt["conclusion"], attempt["seconds"]) == (
        "in_progress",
        None,
        None,
    )


def test_failed_request_is_recorded_and_ends_the_reading(tree, client):
    answers = forge_answers(REPOSITORY, draft_then_ready())
    failing = f"repos/{REPOSITORY}/actions/runs/12/attempts/1/jobs?{PAGE}"
    answers[failing] = {"fail": "gh: Server Error (HTTP 502)"}
    client(answers)
    data = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)
    assert data["errors"] == [f"GET {failing}: gh: Server Error (HTTP 502)"]
    assert not any(f"/runs/{run}/" in call for call in tree.forge_calls() for run in (21, 22))


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (None, "gh: Not Found (HTTP 404)"),
        ({"raw": "<html>"}, "answer is not JSON"),
        ({"total_count": 0}, "no list workflow_runs in the answer"),
    ],
)
def test_unreadable_listing_is_an_error(tree, client, answer, message):
    client({} if answer is None else {LISTING: answer})
    (error,) = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)["errors"]
    assert message in error


def test_missing_client_is_an_error(tree, monkeypatch):
    monkeypatch.setenv("GH", str(tree.root / "no-such-client"))
    (error,) = forge.collect(REPOSITORY, ["trial/licence"], WATCHED)["errors"]
    assert error.startswith(f"GET {LISTING}: ") and "no-such-client" in error


def test_rate_limit_is_waited_out_and_then_given_up(tree, client):
    client({LISTING: {"fail": "gh: API rate limit exceeded for installation (HTTP 403)"}})
    with pytest.raises(forge.ForgeError, match="rate limit, 4 attempts used"):
        forge.api(LISTING)
    assert client.waits == [20, 40, 60]
    assert tree.forge_calls() == 4 * [LISTING]


def test_only_the_readers_own_waits_are_recorded(client):
    """The interpreter waits for a child process in steps of its own; those are not the reader's."""
    time.sleep(0.001)
    assert client.waits == []


def test_listing_is_read_page_by_page(monkeypatch):
    asked = []

    def answer(path: str) -> dict:
        asked.append(path)
        return {"jobs": [{"id": n} for n in range(100 if path.endswith("page=1") else 3)]}

    monkeypatch.setattr(forge, "api", answer)
    assert len(forge.paged("repos/x/y/jobs", "jobs")) == 103
    assert asked == [
        "repos/x/y/jobs?per_page=100&page=1",
        "repos/x/y/jobs?per_page=100&page=2",
    ]


def test_listing_longer_than_the_limit_is_an_error_not_a_cut(monkeypatch):
    monkeypatch.setattr(forge, "api", lambda path: [{"id": n} for n in range(100)])
    with pytest.raises(forge.ForgeError, match="more than 1000 entries"):
        forge.paged("repos/x/y/check-runs/1/annotations", None)


def test_request_other_than_a_get_is_refused_by_the_stand_in(tree):
    env = tree.forge({})
    command = [*shlex.split(env["GH"]), "api", "-X", "DELETE", "repos/x/y"]
    done = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    assert done.returncode == 2
