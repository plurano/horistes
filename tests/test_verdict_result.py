# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Same-verdict check, class U: the recorded cases U-P1 and U-N1 to U-N14.

A job of class U passes whatever it finds; what it says is the result file
it uploads. Every case is the passing record U-P1 with one thing changed.
The fixture has the shape of a scorecard job: the old job in the consumer's
own workflow, the new one a caller of the shared workflow.
"""

from __future__ import annotations

import json
import sys

import pytest
from conftest import (
    CHECKOUT,
    HOSTS,
    ROOT,
    archive,
    forge_answers,
    forge_artifact,
    forge_job,
    forge_run,
    sarif_log,
)

sys.path.insert(0, str(ROOT / "tools"))
import results

OLD, NEW = "Scorecard analysis", "Scorecard / Analysis"
OLD_FILE = ".github/workflows/scorecard-old.yml"
CALLER_FILE = ".github/workflows/scorecard-check.yml"
CALLED_FILE = ".github/workflows/scorecard.yml"
OLD_NAME, NEW_NAME = "scorecard-old", "scorecard-new"
SHOWS, CLEAN = "f" * 40, "a" * 40
ANALYSIS = "ossf/scorecard-action@2d1146689b8cda280b9bc96326124645441f03bc # v2.4.4"

EVENTS = """\
on:
  pull_request:
  push:
    branches: [main]
  schedule:
    - cron: "17 3 * * 1"
"""
JOB = f"""\
    runs-on: ubuntu-24.04
    timeout-minutes: 10
    permissions:
      contents: read
      id-token: write
      security-events: write
    steps:
      - name: Check out
        uses: {CHECKOUT}
        with:
          persist-credentials: false
      - name: Analyse
        uses: {ANALYSIS}
        with:
          results_file: results.sarif
          results_format: sarif
"""
OLD_WORKFLOW = f"""\
---
name: Scorecard, old form
{EVENTS}permissions: {{}}
jobs:
  analysis:
    name: {OLD}
{JOB}"""
CALLER = f"""\
---
name: Scorecard
{EVENTS}permissions: {{}}
jobs:
  scorecard:
    name: Scorecard
    permissions:
      contents: read
      id-token: write
      security-events: write
    uses: ./{CALLED_FILE}
"""
CALLED = f"""\
---
name: Scorecard analysis
on:
  workflow_call:
permissions: {{}}
jobs:
  analysis:
    name: Analysis
{JOB}"""
ALLOWED = HOSTS + '"ossf/scorecard-action" = []\n'

PAIR = f"""\
repository = "fixture/consumer"
branches = ["trial/licence"]

[[pair]]
family = "scorecard"
class = "U"
old = "{OLD}"
new = "{NEW}"
old_workflow = "{OLD_FILE}"
new_workflow = "{CALLER_FILE}"
called_workflow = "{CALLED_FILE}"
old_artifact = "{OLD_NAME}"
new_artifact = "{NEW_NAME}"
"""
MARKER = r"marker = '^PinnedDependenciesID \| \.github/workflows/planted\.yml:7 '"
PLANTED_SHOWS = f"""
[[planted]]
family = "scorecard"
commit = "{SHOWS}"
expect = "FINDING"
{MARKER}
"""
PLANTED_CLEAN = f"""
[[planted]]
family = "scorecard"
commit = "{CLEAN}"
expect = "CLEAN"
{MARKER}
"""
MAP = PAIR + PLANTED_SHOWS + PLANTED_CLEAN

RULES = ("PinnedDependenciesID", "TokenPermissionsID", "VulnerabilitiesID")
TOKEN = ("TokenPermissionsID", ".github/workflows/ci.yml", 1, "score is 9: one workflow writes")
PINNED = (
    "PinnedDependenciesID",
    ".github/workflows/planted.yml",
    7,
    "score is 7: dependency not pinned by hash\nWarn: third-party action not pinned",
)


def read(*findings, rules=RULES, name=OLD_NAME) -> dict:
    """The record of a result that was read: these findings, and the checks of the tool."""
    return {"state": "read", "artifact": name, **results.from_sarif(sarif_log(findings, rules))}


def check(name, workflow, conclusion="success", result=None) -> dict:
    attempt = {
        "attempt": 1,
        "status": "completed",
        "conclusion": conclusion,
        "failed_steps": ["Analyse"] if conclusion == "failure" else [],
        "annotations": [],
        "seconds": 21.0,
    }
    made = {"name": name, "workflow": workflow, "attempts": [attempt]}
    return made if result is None else {**made, "result": result}


def subject(number, old=None, new=None, commit=None, conclusion="success") -> dict:
    """One subject where both jobs pass; a side's result is the usual one unless given."""
    usual = read(TOKEN, PINNED) if commit == SHOWS else read(TOKEN)
    given = (usual if old is None else old), (usual if new is None else new)
    return {
        "commit": commit or f"{number:040x}",
        "event": "pull_request",
        "ref": "trial/licence",
        "occurrence": 1,
        "checks": [
            check(OLD, OLD_FILE, conclusion, given[0] or None),
            check(NEW, CALLER_FILE, conclusion, given[1] or None),
        ],
    }


def passing() -> list[dict]:
    """U-P1: eight ordinary subjects, the clean commit, the commit that shows the finding."""
    return [
        *(subject(number) for number in range(1, 9)),
        subject(9, commit=CLEAN),
        subject(10, commit=SHOWS),
    ]


def write(tree, mapping=MAP, caller=CALLER):
    tree.write(
        {
            OLD_FILE: OLD_WORKFLOW,
            CALLER_FILE: caller,
            CALLED_FILE: CALLED,
            "hosts.toml": ALLOWED,
            "trial.toml": mapping,
        }
    )


def run(tree, subjects, mapping=MAP, caller=CALLER):
    write(tree, mapping, caller)
    tree.write({"runs.json": json.dumps({"errors": [], "subjects": subjects})})
    done = tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--runs",
        str(tree.root / "runs.json"),
        "--json",
        str(tree.root / "result.json"),
    )
    result = tree.root / "result.json"
    done.counts = json.loads(result.read_text())["counts"] if result.exists() else {}
    return done


def test_u_p1_same_results_on_ten_subjects_with_both_planted_cases_pass(tree):
    done = run(tree, passing())
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.counts["equal"] == 10
    assert sum(done.counts.values()) == 10
    assert f"| {SHOWS[:12]} pull_request trial/licence #1 (planted FINDING) |" in done.stdout
    assert done.stdout.count("| PASS | PASS | equal | yes |") == 1
    assert f"(planted CLEAN) | {OLD} -> {NEW} | PASS | PASS | equal | no |" in done.stdout
    assert done.stdout.count("| PASS | PASS | equal | - |") == 8
    assert f"results compared, artifact '{OLD_NAME}' against '{NEW_NAME}'" in done.stdout
    assert f"required check moves from '{OLD}' to '{NEW}'" in done.stdout


def test_u_n1_a_finding_only_in_the_old_result(tree):
    subjects = passing()
    subjects[0] = subject(1, old=read(TOKEN, PINNED))
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["differ"] == 1 and done.counts["equal"] == 9
    assert "is differ (PASS against PASS): findings only in the old result: " in done.stdout
    assert "PinnedDependenciesID | .github/workflows/planted.yml:7 | score is 7" in done.stdout


def test_u_n2_the_score_of_one_finding_differs(tree):
    lower = (*TOKEN[:3], TOKEN[3].replace("score is 9", "score is 8"))
    subjects = passing()
    subjects[0] = subject(1, new=read(lower))
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["differ"] == 1
    assert "ci.yml:1 | score is 9: one workflow writes" in done.stdout
    assert "findings only in the new result: TokenPermissionsID" in done.stdout


def test_u_n2_a_detail_line_of_one_finding_differs(tree):
    other = (*PINNED[:3], PINNED[3].replace("third-party action", "container image"))
    subjects = passing()
    subjects[9] = subject(10, new=read(TOKEN, other), commit=SHOWS)
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["differ"] == 1


def test_u_n3_a_check_only_in_the_new_result(tree):
    subjects = passing()
    subjects[0] = subject(1, new=read(TOKEN, rules=(*RULES, "BranchProtectionID")))
    done = run(tree, subjects)
    assert done.returncode == 1
    assert "checks only in the new result: BranchProtectionID" in done.stdout


def test_u_n4_the_new_job_uploaded_nothing(tree):
    subjects = passing()
    subjects[0] = subject(1, new={"state": "none", "artifact": NEW_NAME})
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["differ"] == 1
    assert "is differ (PASS against PASS): the new job uploaded no result" in done.stdout


def test_u_n5_neither_job_uploaded_a_result_on_any_subject(tree):
    none = {"state": "none", "artifact": OLD_NAME}
    done = run(tree, [subject(number, none, none) for number in range(1, 11)])
    assert done.returncode == 2
    assert done.counts["no-evidence"] == 10 and done.counts["equal"] == 0
    assert "0 equal rows on distinct subjects, 10 needed" in done.stdout


def test_u_n6_both_results_hold_no_check_on_any_subject(tree):
    empty = read(rules=())
    assert empty["checks"] == []
    done = run(tree, [subject(number, empty, empty) for number in range(1, 11)])
    assert done.returncode == 2
    assert done.counts["no-evidence"] == 10 and done.counts["equal"] == 0


def test_u_n7_the_planted_finding_shows_in_neither_result(tree):
    subjects = passing()
    subjects[9] = subject(10, read(TOKEN), read(TOKEN), commit=SHOWS)
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["equal"] == 10
    assert "| PASS | PASS | equal | yes |" not in done.stdout
    assert f"planted case {SHOWS[:12]} has no equal row where both show the finding" in done.stdout


def test_u_n8_the_finding_shows_on_the_commit_that_must_stay_clean(tree):
    subjects = passing()
    subjects[8] = subject(9, read(TOKEN, PINNED), read(TOKEN, PINNED), commit=CLEAN)
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["equal"] == 10
    assert f"planted case {CLEAN[:12]} has no equal row where both stay clean" in done.stdout


@pytest.mark.parametrize(
    ("mapping", "missing"),
    [(PAIR + PLANTED_CLEAN, "show the finding"), (PAIR + PLANTED_SHOWS, "stay clean")],
)
def test_u_n9_map_with_one_kind_of_planted_case(tree, mapping, missing):
    done = run(tree, [*passing(), subject(11), subject(12)], mapping)
    assert done.returncode == 2
    assert done.counts["equal"] == 12
    assert f"the map holds no planted case that must {missing}" in done.stdout


def test_u_n10_a_result_expired_on_three_of_ten_subjects(tree):
    subjects = passing()
    for index in range(3):
        subjects[index] = subject(index + 1, new={"state": "expired", "artifact": NEW_NAME})
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["invalid"] == 3 and done.counts["equal"] == 7


def test_u_n11_both_jobs_fail_on_every_subject(tree):
    done = run(tree, [subject(number, conclusion="failure") for number in range(1, 11)])
    assert done.returncode == 2
    assert done.counts["no-evidence"] == 10 and done.counts["equal"] == 0


def test_u_n11_a_subject_where_both_fail_is_no_equal_row(tree):
    subjects = passing()
    subjects[0] = subject(1, conclusion="failure")
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["no-evidence"] == 1 and done.counts["equal"] == 9


@pytest.mark.parametrize("side", ["old", "new"])
def test_u_n12_no_result_is_recorded_for_one_job(tree, side):
    subjects = passing()
    subjects[0] = subject(1, **{side: {}})
    assert "result" not in subjects[0]["checks"][side == "new"]
    done = run(tree, subjects)
    assert done.returncode == 3
    assert done.counts["invalid"] == 1 and done.counts["equal"] == 9
    assert f"{NEW}: {side} job: no result is recorded" in done.stdout


def test_u_n13_a_result_that_cannot_be_read(tree):
    broken = {"state": "unreadable", "artifact": OLD_NAME, "detail": "archive cannot be read (x)"}
    subjects = passing()
    subjects[0] = subject(1, old=broken)
    done = run(tree, subjects)
    assert done.returncode == 3
    assert done.counts["invalid"] == 1
    assert "old job: result is unreadable: archive cannot be read (x)" in done.stdout


def test_u_n14_a_record_without_its_list_of_findings(tree):
    cut = {key: value for key, value in read(TOKEN).items() if key != "findings"}
    subjects = passing()
    subjects[0] = subject(1, new=cut)
    done = run(tree, subjects)
    assert done.returncode == 3
    assert "new job: result record has no list `findings`" in done.stdout


def test_verdicts_are_judged_before_any_result(tree):
    subjects = passing()
    subjects[0]["checks"][1] = check(NEW, CALLER_FILE, "failure")
    done = run(tree, subjects)
    assert done.returncode == 1
    assert "is differ (PASS against FAIL)" in done.stdout
    assert "no result is recorded" not in done.stdout


def test_side_conditions_hold_for_a_job_that_uploads(tree):
    wider = CALLER.replace("contents: read", "contents: write")
    assert wider != CALLER
    done = run(tree, passing(), caller=wider)
    assert done.returncode == 1
    assert "the new job asks to write contents, the old one did not" in done.stdout


SECOND = f"""
[[pair]]
family = "scorecard"
class = "R"
old = "Other old"
new = "Other new"
old_workflow = "{OLD_FILE}"
new_workflow = "{CALLER_FILE}"
"""
AS_FIXTURE = PAIR.split("[[pair]]")[1].replace(OLD, "Planted old").replace(NEW, "Planted new")
INPUT_ERRORS = [
    (MAP.replace('expect = "CLEAN"', 'expect = "PASS"'), "`expect` is FINDING or CLEAN in class U"),
    (MAP.replace('expect = "FINDING"', 'expect = "FAIL"'), "`expect` is FINDING or CLEAN in cl"),
    (PAIR + PLANTED_SHOWS + PLANTED_CLEAN.replace(MARKER, ""), "`marker` must be a non-empty"),
    (MAP.replace("planted\\.yml:7 '", "planted\\.yml:7 ('"), "`marker` is no pattern"),
    (MAP.replace(f'old_artifact = "{OLD_NAME}"\n', ""), "`old_artifact` must be a non-empty"),
    (MAP.replace(f'"{NEW_NAME}"', '" "'), "`new_artifact` must be a non-empty string"),
    (MAP + SECOND, "family 'scorecard' holds pairs of two classes"),
    (MAP + "\n[[fixture]]" + AS_FIXTURE + 'expect = "FINDING"\n', "judged in class R only"),
]


@pytest.mark.parametrize(("mapping", "message"), INPUT_ERRORS, ids=[e[1] for e in INPUT_ERRORS])
def test_unclear_map_of_class_u_is_an_input_error(tree, mapping, message):
    assert mapping != MAP
    done = run(tree, passing(), mapping)
    assert done.returncode == 3
    assert message in done.stderr
    assert done.counts == {}


def live_runs(changed: dict | None = None) -> list[dict]:
    """U-P1 as the forge lists it: two runs per subject, each with the result it uploaded.

    `changed` replaces fields of the artifact the new job uploaded on subject 1.
    """
    runs = []
    for number in range(1, 11):
        commit = {9: CLEAN, 10: SHOWS}.get(number, f"{number:040x}")
        found = (TOKEN, PINNED) if number == 10 else (TOKEN,)
        packed = archive({"results.sarif": sarif_log(found, RULES)})
        at = f"2026-10-11T02:{number:02d}:00Z"
        sides = ((number, OLD_FILE, OLD, OLD_NAME), (100 + number, CALLER_FILE, NEW, NEW_NAME))
        for run_id, path, job, name in sides:
            fields = (changed or {}) if run_id == 101 else {}
            uploaded = forge_artifact(5000 + run_id, name, packed, **fields)
            jobs = [forge_job(1000 + run_id, job, "success")]
            runs.append(forge_run(run_id, path, commit, at, jobs, artifacts=[uploaded]))
    return runs


def live(tree, runs):
    write(tree)
    return tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--live",
        "--record",
        str(tree.root / "recorded.json"),
        env=tree.forge(forge_answers("fixture/consumer", runs)),
    )


def test_u_p1_read_from_the_forge_passes_and_is_recorded(tree):
    runs = live_runs()
    done = live(tree, runs)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASS | equal 10" in done.stdout
    assert done.stdout.count("| PASS | PASS | equal | yes |") == 1
    calls = tree.forge_calls()
    assert len(calls) == 1 + 20 + 20 + 20
    assert sum(1 for call in calls if call.endswith("/zip")) == 20
    recorded = json.loads((tree.root / "recorded.json").read_text())
    first = next(s for s in recorded["subjects"] if s["commit"] == runs[0]["head_sha"])
    kept = next(c for c in first["checks"] if c["name"] == OLD)["result"]
    listed = runs[0]["artifacts"][0]["digest"].removeprefix("sha256:")
    assert kept == {**read(TOKEN), "sha256": listed}
    again = tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--runs",
        str(tree.root / "recorded.json"),
    )
    assert again.returncode == 0
    assert again.stdout.splitlines()[:-1] == done.stdout.splitlines()[:-1]


def test_u_n13_archive_that_is_not_the_one_the_forge_lists(tree):
    done = live(tree, live_runs({"digest": "sha256:" + "0" * 64}))
    assert done.returncode == 3
    assert "new job: result is unreadable: archive is sha256:" in done.stdout
    assert "the forge lists sha256:000000000000" in done.stdout


def test_u_n10_expired_result_is_not_fetched(tree):
    done = live(tree, live_runs({"expired": True}))
    assert done.returncode == 2
    assert "| PASS | PASS | invalid | - |" in done.stdout
    assert sum(1 for call in tree.forge_calls() if call.endswith("/zip")) == 19
