# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Forge reader: the answers of a stand-in client become subjects with checks.

The canned answers have the shape of real runs of this repository: a draft
pull request marked ready gives two runs per workflow on one commit, and a
failing script step leaves only its exit code as annotation.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import time
import types

import pytest
from conftest import (
    PAGE,
    ROOT,
    archive,
    forge_answers,
    forge_artifact,
    forge_job,
    forge_run,
    sarif_log,
)

sys.path.insert(0, str(ROOT / "tools"))
import forge

REPOSITORY = "fixture/consumer"
OLD_FILE, NEW_FILE = ".github/workflows/reuse.yml", ".github/workflows/licence-check.yml"
WATCHED = {OLD_FILE, NEW_FILE}
DRAFT = "Stop on a draft pull request"
COMMIT = "bfe95b5170e7e5e1e9681ea4d766c14d55e28851"
LISTING = f"repos/{REPOSITORY}/actions/runs?branch=trial%2Flicence&{PAGE}"
FOLDER = ".github/workflows"
HELD = f"repos/{REPOSITORY}/contents/{FOLDER}?ref={COMMIT}"


def entry(path: str) -> dict:
    """One file as the forge lists it in a folder."""
    return {"name": path.rsplit("/", 1)[-1], "path": path, "type": "file"}


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
    answers[HELD] = [entry(OLD_FILE), entry(NEW_FILE)]
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


SCORE_FILE = ".github/workflows/scorecard.yml"
SCORE_JOB, SCORE_ARTIFACT = "Scorecard / Analysis", "scorecard-new"
UPLOADS = {SCORE_JOB: SCORE_ARTIFACT}
SCORE_RULES = ("TokenPermissionsID", "VulnerabilitiesID")
WRITES = ("TokenPermissionsID", ".github/workflows/ci.yml", 1, "score is 9: one workflow writes")
SCORE_TEXT = sarif_log((WRITES,), SCORE_RULES)
SARIF = archive({"results.sarif": SCORE_TEXT})
LIMIT = len(SCORE_TEXT)
ARTIFACTS = f"repos/{REPOSITORY}/actions/runs/61/artifacts?name={SCORE_ARTIFACT}&{PAGE}"
LATER = "2026-10-11T01:40:00Z"


def archive_of(number: int) -> str:
    return f"repos/{REPOSITORY}/actions/artifacts/{number}/zip"


def score_run(artifacts: list, *attempts: list) -> dict:
    """One run of the scorecard workflow that holds these artifacts; one passing job by default."""
    jobs = attempts or ([forge_job(601, SCORE_JOB, "success")],)
    return forge_run(61, SCORE_FILE, COMMIT, "02:20:00", *jobs, artifacts=artifacts)


def uploaded(client, run: dict, change=None) -> dict:
    """What the reader gives for this run; `change` alters the canned answers first."""
    answers = forge_answers(REPOSITORY, [run], (SCORE_ARTIFACT,))
    if change:
        change(answers)
    client(answers)
    return forge.collect(REPOSITORY, ["trial/licence"], {SCORE_FILE}, UPLOADS)


def result_of(data: dict) -> dict:
    assert data["errors"] == []
    (check,) = data["subjects"][0]["checks"]
    return check["result"]


def test_result_of_a_passing_job_is_read_from_its_artifact(tree, client):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF)
    data = uploaded(client, score_run([held]))
    assert result_of(data) == {
        "state": "read",
        "artifact": SCORE_ARTIFACT,
        "sha256": held["digest"].removeprefix("sha256:"),
        "tools": ["Scorecard v5.5.0"],
        "checks": list(SCORE_RULES),
        "findings": [
            "TokenPermissionsID | .github/workflows/ci.yml:1 | score is 9: one workflow writes | "
            "e3b0c44298fc"
        ],
    }
    assert tree.forge_calls()[-2:] == [ARTIFACTS, archive_of(901)]


def test_job_that_is_not_named_as_uploading_is_asked_for_nothing(tree, client):
    run = score_run([forge_artifact(901, SCORE_ARTIFACT, SARIF)])
    client(forge_answers(REPOSITORY, [run], (SCORE_ARTIFACT,)))
    for named in (None, {}, {"Another job": SCORE_ARTIFACT}):
        data = forge.collect(REPOSITORY, ["trial/licence"], {SCORE_FILE}, named)
        assert data["errors"] == []
        assert "result" not in data["subjects"][0]["checks"][0]
    assert not any("artifacts" in call for call in tree.forge_calls())


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_job_that_did_not_pass_is_asked_for_no_result(tree, client, conclusion):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF)
    data = uploaded(client, score_run([held], [forge_job(601, SCORE_JOB, conclusion)]))
    assert data["errors"] == []
    assert "result" not in data["subjects"][0]["checks"][0]
    assert not any("artifacts" in call for call in tree.forge_calls())


def test_run_without_the_artifact_gives_no_result(tree, client):
    other = forge_artifact(902, "coverage", SARIF)
    assert result_of(uploaded(client, score_run([other]))) == {
        "state": "none",
        "artifact": SCORE_ARTIFACT,
    }
    assert archive_of(902) not in tree.forge_calls()


def test_artifact_of_another_name_in_the_answer_is_not_taken(tree, client):
    other = {k: v for k, v in forge_artifact(902, "scorecard-new-2", SARIF).items() if k != "data"}

    def change(answers: dict) -> None:
        answers[ARTIFACTS] = {"total_count": 1, "artifacts": [other]}

    assert result_of(uploaded(client, score_run([]), change))["state"] == "none"


def test_expired_artifact_is_recorded_and_not_fetched(tree, client):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF, expired=True)
    assert result_of(uploaded(client, score_run([held]))) == {
        "state": "expired",
        "artifact": SCORE_ARTIFACT,
    }
    assert archive_of(901) not in tree.forge_calls()


def test_artifact_of_an_earlier_attempt_is_not_the_result_of_the_last(tree, client):
    """A re-run that uploads nothing leaves the first attempt's artifact in the listing."""
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF)
    first = [forge_job(601, SCORE_JOB, "success")]
    again = [{**forge_job(602, SCORE_JOB, "success"), "started_at": LATER}]
    assert result_of(uploaded(client, score_run([held], first)))["state"] == "read"
    assert result_of(uploaded(client, score_run([held], first, again))) == {
        "state": "none",
        "artifact": SCORE_ARTIFACT,
    }


def test_newest_of_two_artifacts_of_the_name_is_the_result(tree, client):
    newer = archive({"results.sarif": sarif_log((), SCORE_RULES)})
    held = [
        forge_artifact(902, SCORE_ARTIFACT, newer, created_at="2026-10-11T01:40:20Z"),
        forge_artifact(901, SCORE_ARTIFACT, SARIF),
    ]
    read = result_of(uploaded(client, score_run(held)))
    assert (read["state"], read["findings"]) == ("read", [])
    assert read["sha256"] == held[0]["digest"].removeprefix("sha256:")
    assert archive_of(901) not in tree.forge_calls()


def test_archive_that_is_not_the_one_listed_is_unreadable(tree, client):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF, digest="sha256:" + "0" * 64)
    read = result_of(uploaded(client, score_run([held])))
    assert (read["state"], read["artifact"]) == ("unreadable", SCORE_ARTIFACT)
    found = hashlib.sha256(SARIF).hexdigest()[:12]
    assert read["detail"] == f"archive is sha256:{found}, the forge lists sha256:000000000000"
    assert set(read) == {"state", "artifact", "detail"}


@pytest.mark.parametrize("size", [LIMIT + 1, None, "small"])
def test_archive_over_the_limit_is_unreadable_and_not_fetched(tree, client, monkeypatch, size):
    monkeypatch.setattr(forge.results, "MAX_BYTES", LIMIT)
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF, size_in_bytes=LIMIT)
    assert result_of(uploaded(client, score_run([held])))["state"] == "read"
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF, size_in_bytes=size)
    assert result_of(uploaded(client, score_run([held]))) == {
        "state": "unreadable",
        "artifact": SCORE_ARTIFACT,
        "detail": f"archive of {size} bytes, at most {LIMIT} are read",
    }
    assert tree.forge_calls().count(archive_of(901)) == 1


@pytest.mark.parametrize("missing", ["created_at", "started_at"])
def test_artifact_without_a_time_is_an_error_not_a_guess(tree, client, missing):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF)
    job = forge_job(601, SCORE_JOB, "success")
    if missing == "created_at":
        held[missing] = None
    else:
        job[missing] = "soon"
    data = uploaded(client, score_run([held], [job]))
    assert data["errors"] == [
        f"GET {ARTIFACTS.removesuffix('&' + PAGE)}: "
        f"no time to tell the attempts of {SCORE_ARTIFACT!r} apart"
    ]
    assert data["subjects"] == []
    assert archive_of(901) not in tree.forge_calls()


def test_archive_that_cannot_be_fetched_is_an_error(tree, client):
    held = forge_artifact(901, SCORE_ARTIFACT, SARIF)

    def change(answers: dict) -> None:
        answers[archive_of(901)] = {"fail": "gh: Gone (HTTP 410)"}

    data = uploaded(client, score_run([held]), change)
    assert data["errors"] == [f"GET {archive_of(901)}: gh: Gone (HTTP 410)"]


def pushed(client, held: object, files: tuple = (OLD_FILE,), event: str = "push") -> dict:
    """What the reader gives for one event on main where only `files` ran; `held` is the folder."""
    names = {OLD_FILE: "REUSE lint", NEW_FILE: "Licence / REUSE lint"}
    runs = [
        forge_run(
            70 + number,
            path,
            COMMIT,
            "03:00:00",
            [forge_job(700 + number, names[path], "success")],
            event=event,
            head_branch="main",
        )
        for number, path in enumerate(files)
    ]
    answers = forge_answers(REPOSITORY, runs)
    answers[LISTING.replace("trial%2Flicence", "main")] = answers.pop(LISTING)
    if held is not None:
        answers[HELD] = held
    client(answers)
    return forge.collect(REPOSITORY, ["main"], WATCHED)


def test_push_names_the_watched_file_its_commit_does_not_hold(tree, client):
    data = pushed(client, [entry(OLD_FILE), entry(f"{FOLDER}/ci.yml")])
    assert data["errors"] == []
    (subject,) = data["subjects"]
    assert subject["not_in_tree"] == [NEW_FILE]
    assert tree.forge_calls().count(HELD) == 1


def test_file_the_commit_holds_is_not_named_whatever_kind_of_entry_it_is(tree, client):
    for kind in ("file", "symlink", "dir"):
        data = pushed(client, [entry(OLD_FILE), {**entry(NEW_FILE), "type": kind}])
        assert data["errors"] == []
        assert "not_in_tree" not in data["subjects"][0], kind


def test_folder_is_not_asked_when_every_watched_workflow_ran(tree, client):
    data = pushed(client, None, files=(OLD_FILE, NEW_FILE))
    assert data["errors"] == []
    assert "not_in_tree" not in data["subjects"][0]
    assert HELD not in tree.forge_calls()


def test_pull_request_is_asked_for_no_folder_its_merge_commit_is_not_named(tree, client):
    data = pushed(client, [entry(OLD_FILE)], event="pull_request")
    assert data["errors"] == []
    assert "not_in_tree" not in data["subjects"][0]
    assert HELD not in tree.forge_calls()


def test_folder_that_is_not_answered_is_an_error_not_a_missing_file(tree, client):
    data = pushed(client, None)
    assert data["errors"] == [f"GET {HELD}: gh: Not Found (HTTP 404)"]
    assert data["subjects"] == []


@pytest.mark.parametrize(
    "held",
    [
        entry(OLD_FILE),
        [entry(f"{FOLDER}/{number}.yml") for number in range(999)] + [entry(OLD_FILE)],
    ],
    ids=["a file, not a folder", "as many entries as the forge lists at most"],
)
def test_listing_that_may_be_cut_or_is_no_listing_is_an_error(tree, client, held):
    data = pushed(client, held)
    assert data["errors"] == [f"GET {HELD}: no complete listing of the folder"]
    assert data["subjects"] == []


def test_listing_without_the_workflow_that_ran_is_no_evidence(tree, client):
    data = pushed(client, [entry(f"{FOLDER}/ci.yml")])
    assert data["errors"] == [
        f"GET {HELD}: the listing lacks {OLD_FILE}, whose run this commit started"
    ]
    assert data["subjects"] == []
