# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Same-verdict check: the recorded cases P1 and N1 to N13, each with its exit code.

Every case is the passing record P1 with one thing changed, so a case that
stops being refused shows which rule went missing.
"""

from __future__ import annotations

import json
import sys

import pytest
from conftest import CHECKOUT, HOSTS, PAGE, ROOT, forge_answers, forge_job, forge_run

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import verdict

OLD, NEW = "REUSE lint", "Licence / REUSE lint"
OLD_FILE = ".github/workflows/reuse.yml"
CALLER_FILE = ".github/workflows/licence-check.yml"
CALLED_FILE = ".github/workflows/licence.yml"
DRAFT = "Stop on a draft pull request"
MUST_FAIL, MUST_PASS = "f" * 40, "a" * 40

EVENTS = """\
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  push:
    branches: [main]
  merge_group:
"""
STEPS = f"""\
    steps:
      - name: {DRAFT}
        if: github.event.pull_request.draft == true
        run: exit 1
      - name: Check out
        uses: {CHECKOUT}
        with:
          persist-credentials: false
      - name: reuse lint
        run: reuse lint
"""
OLD_WORKFLOW = f"""\
---
name: REUSE
{EVENTS}permissions:
  contents: read
jobs:
  reuse:
    name: {OLD}
    runs-on: ubuntu-24.04
    timeout-minutes: 10
{STEPS}"""
CALLER = f"""\
---
name: Licence
{EVENTS}permissions: {{}}
jobs:
  licence:
    name: Licence
    permissions:
      contents: read
    uses: ./{CALLED_FILE}
    with:
      runner: ubuntu-24.04
"""
CALLED = f"""\
---
name: Licence check
on:
  workflow_call:
    inputs:
      runner:
        description: Label of the runner.
        type: string
        required: false
        default: ubuntu-24.04
permissions: {{}}
jobs:
  reuse:
    name: {OLD}
    runs-on: ${{{{ inputs.runner }}}}
    timeout-minutes: 10
    permissions:
      contents: read
{STEPS}"""

PAIR = f"""\
repository = "fixture/consumer"
branches = ["trial/licence"]

[[pair]]
family = "licence check"
class = "R"
old = "{OLD}"
new = "{NEW}"
old_workflow = "{OLD_FILE}"
new_workflow = "{CALLER_FILE}"
called_workflow = "{CALLED_FILE}"
draft_stop = "{DRAFT}"
"""
PLANTED_FAIL = f"""
[[planted]]
family = "licence check"
commit = "{MUST_FAIL}"
expect = "FAIL"
marker = "reuse lint"
"""
PLANTED_PASS = f"""
[[planted]]
family = "licence check"
commit = "{MUST_PASS}"
expect = "PASS"
"""
MAP = PAIR + PLANTED_FAIL + PLANTED_PASS


def check(name, workflow, *conclusions, steps=("reuse lint",), status="completed"):
    """One check with one attempt per conclusion; a failing attempt names its failed steps."""
    attempts = [
        {
            "attempt": number,
            "status": status,
            "conclusion": conclusion,
            "failed_steps": list(steps) if conclusion == "failure" else [],
            "annotations": ["Process completed with exit code 1."]
            if conclusion == "failure"
            else [],
            "seconds": 20.0 + number,
        }
        for number, conclusion in enumerate(conclusions, 1)
    ]
    return {"name": name, "workflow": workflow, "attempts": attempts}


def subject(number, old="success", new="success", commit=None):
    """One subject; old and new are a conclusion, a ready check, or None for an absent check."""
    checks = []
    for name, workflow, given in ((OLD, OLD_FILE, old), (NEW, CALLER_FILE, new)):
        if given is not None:
            checks.append(given if isinstance(given, dict) else check(name, workflow, given))
    return {
        "commit": commit or f"{number:040x}",
        "event": "pull_request",
        "ref": "trial/licence",
        "occurrence": 1,
        "checks": checks,
    }


def passing():
    """P1: eight ordinary subjects where both pass, one planted pass, one planted failure."""
    return [
        *(subject(number) for number in range(1, 9)),
        subject(9, commit=MUST_PASS),
        subject(10, "failure", "failure", commit=MUST_FAIL),
    ]


def run(tree, subjects, mapping=MAP, caller=CALLER, errors=(), extra=(), old_workflow=OLD_WORKFLOW):
    tree.write(
        {
            OLD_FILE: old_workflow,
            CALLER_FILE: caller,
            CALLED_FILE: CALLED,
            "hosts.toml": HOSTS,
            "trial.toml": mapping,
            "runs.json": json.dumps({"errors": list(errors), "subjects": subjects}),
        }
    )
    done = tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--runs",
        str(tree.root / "runs.json"),
        "--json",
        str(tree.root / "result.json"),
        *extra,
    )
    result = tree.root / "result.json"
    done.counts = json.loads(result.read_text())["counts"] if result.exists() else {}
    return done


def test_p1_ten_equal_rows_with_both_planted_cases_pass(tree):
    done = run(tree, passing(), extra=("--markdown", str(tree.root / "result.md")))
    assert done.returncode == 0, done.stderr + done.stdout
    assert done.counts == {
        "equal": 10,
        "differ": 0,
        "wrong-reason": 0,
        "no-evidence": 0,
        "invalid": 0,
        "draft": 0,
        "flaky": 0,
        "unmapped": 0,
    }
    assert "PASS | equal 10" in done.stdout
    assert "source: recorded file" in done.stdout
    assert f"| {MUST_FAIL[:12]} pull_request trial/licence #1 (planted FAIL) |" in done.stdout
    assert "| FAIL | FAIL | equal | yes |" in done.stdout
    assert "median 21 s against 21 s, within the limit of 56 s" in done.stdout
    assert "required check moves from 'REUSE lint' to 'Licence / REUSE lint'" in done.stdout
    assert (tree.root / "result.md").read_text().rstrip().endswith(str(tree.root / "runs.json"))


def test_n1_old_fails_new_passes_on_a_planted_case(tree):
    subjects = passing()
    subjects[9] = subject(10, "failure", "success", commit=MUST_FAIL)
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["differ"] == 1
    assert "is differ (FAIL against PASS)" in done.stdout


def test_n2_old_passes_new_fails_on_an_ordinary_commit(tree):
    done = run(tree, [*passing(), subject(11, "success", "failure")])
    assert done.returncode == 1
    assert done.counts["differ"] == 1 and done.counts["equal"] == 10


def test_n3_new_check_absent_on_one_subject(tree):
    done = run(tree, [*passing(), subject(11, "success", None)])
    assert done.returncode == 3
    assert f"check '{NEW}' is absent while its counterpart ran" in done.stdout
    assert done.counts["invalid"] == 1 and done.counts["equal"] == 10


def test_n4_both_skipped_on_every_subject(tree):
    subjects = [subject(number, "skipped", "skipped") for number in range(1, 11)]
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["no-evidence"] == 10 and done.counts["equal"] == 0
    assert "0 equal rows on distinct subjects, 10 needed" in done.stdout


def test_n5_new_fails_on_a_planted_case_for_another_reason(tree):
    subjects = passing()
    at_checkout = check(NEW, CALLER_FILE, "failure", steps=("Check out",))
    subjects[9] = subject(10, "failure", at_checkout, commit=MUST_FAIL)
    done = run(tree, subjects)
    assert done.returncode == 1
    assert done.counts["wrong-reason"] == 1
    assert "| FAIL | FAIL | wrong-reason | no |" in done.stdout


def test_n6_twelve_passes_and_no_planted_failure_present(tree):
    done = run(tree, [*passing()[:9], *(subject(number) for number in range(11, 14))])
    assert done.returncode == 2
    assert done.counts["equal"] == 12
    assert f"planted case {MUST_FAIL[:12]} has no equal row where both fail" in done.stdout


@pytest.mark.parametrize(
    ("mapping", "missing"), [(PAIR + PLANTED_PASS, "fail"), (PAIR + PLANTED_FAIL, "pass")]
)
def test_n6_map_without_a_planted_case_of_each_kind(tree, mapping, missing):
    done = run(tree, [*passing(), subject(11), subject(12)], mapping)
    assert done.returncode == 2
    assert f"the map holds no planted case that must {missing}" in done.stdout


def test_n7_new_run_cancelled_on_three_of_ten_subjects(tree):
    subjects = passing()
    for index in range(3):
        subjects[index] = subject(index + 1, "success", "cancelled")
    done = run(tree, subjects)
    assert done.returncode == 2
    assert done.counts["invalid"] == 3 and done.counts["equal"] == 7


def test_n8_matrix_leg_of_the_old_job_that_no_pair_names(tree):
    subjects = passing()
    subjects[0]["checks"].append(check(f"{OLD} (windows)", OLD_FILE, "success"))
    done = run(tree, subjects)
    assert done.returncode == 3
    assert done.counts["unmapped"] == 1 and done.counts["equal"] == 10
    assert f"unmapped: check '{OLD} (windows)' of {OLD_FILE}" in done.stdout


def test_check_of_a_workflow_outside_the_trial_is_not_unmapped(tree):
    subjects = passing()
    subjects[0]["checks"].append(check("Verification gate", ".github/workflows/ci.yml", "failure"))
    assert run(tree, subjects).returncode == 0


def test_n9_data_source_reports_an_error(tree):
    done = run(tree, passing(), errors=["GET repos/fixture/consumer/actions/runs: HTTP 502"])
    assert done.returncode == 3
    assert "error: data source: GET repos/fixture/consumer/actions/runs: HTTP 502" in done.stdout
    assert done.counts["equal"] == 10


def test_n10_new_job_fails_then_passes_on_a_planted_case(tree):
    subjects = passing()
    rerun = check(NEW, CALLER_FILE, "failure", "success")
    subjects[9] = subject(10, "failure", rerun, commit=MUST_FAIL)
    done = run(tree, [*subjects, subject(11)])
    assert done.returncode == 2
    assert done.counts["flaky"] == 1 and done.counts["differ"] == 0
    assert "a planted case is flaky" in done.stdout


def test_n11_all_rows_are_draft_stops(tree):
    stopped = [
        subject(
            number,
            check(OLD, OLD_FILE, "failure", steps=(DRAFT,)),
            check(NEW, CALLER_FILE, "failure", steps=(DRAFT,)),
        )
        for number in range(1, 11)
    ]
    done = run(tree, stopped)
    assert done.returncode == 2
    assert done.counts["draft"] == 10 and done.counts["equal"] == 0


def test_n12_unknown_conclusion_value(tree):
    done = run(tree, [*passing(), subject(11, "success", "startup_failure")])
    assert done.returncode == 3
    assert "unknown conclusion 'startup_failure'" in done.stdout
    assert done.counts["invalid"] == 1


def test_n13_new_job_asks_for_a_write_scope_the_old_one_lacks(tree):
    assert "      contents: read\n" in CALLER
    done = run(tree, passing(), caller=CALLER.replace("contents: read", "contents: write"))
    assert done.returncode == 1
    assert "the new job asks to write contents, the old one did not" in done.stdout
    assert done.counts["equal"] == 10


def test_draft_stop_on_one_side_only_differs(tree):
    stopped = check(OLD, OLD_FILE, "failure", steps=(DRAFT,))
    for new in ("success", "failure"):
        done = run(tree, [*passing(), subject(11, stopped, new)])
        assert done.returncode == 1, new
        assert done.counts["differ"] == 1


@pytest.mark.parametrize(
    ("old", "new", "state"),
    [
        ("timed_out", "failure", "equal"),
        ("neutral", "skipped", "no-evidence"),
        ("neutral", "success", "differ"),
        ("stale", "success", "invalid"),
        ("action_required", "success", "invalid"),
    ],
)
def test_every_known_conclusion_has_its_verdict(tree, old, new, state):
    done = run(tree, [*passing(), subject(11, old, new)])
    assert done.counts[state] == (11 if state == "equal" else 1)
    assert done.returncode == (1 if state == "differ" else 0)


def test_running_check_is_invalid_and_an_unknown_status_is_an_error(tree):
    running = check(NEW, CALLER_FILE, None, status="in_progress")
    done = run(tree, [*passing(), subject(11, "success", running)])
    assert done.returncode == 0 and done.counts["invalid"] == 1
    odd = check(NEW, CALLER_FILE, "success", status="finished")
    done = run(tree, [*passing(), subject(11, "success", odd)])
    assert done.returncode == 3
    assert "unknown status 'finished'" in done.stdout


def test_flaky_rows_are_counted_and_limited(tree):
    def flaky(number):
        return subject(number, "success", check(NEW, CALLER_FILE, "failure", "success"))

    done = run(tree, [*passing(), flaky(11), flaky(12)])
    assert done.returncode == 0 and done.counts["flaky"] == 2
    done = run(tree, [*passing(), flaky(11), flaky(12), flaky(13)])
    assert done.returncode == 2
    assert "3 flaky rows, at most 2 allowed" in done.stdout


def test_ten_equal_rows_need_ten_distinct_subjects(tree):
    subjects = passing()
    subjects[1] = subject(1)
    done = run(tree, subjects)
    assert done.returncode == 2
    assert "9 equal rows on distinct subjects, 10 needed" in done.stdout


def test_ordinary_pass_is_needed(tree):
    subjects = [subject(n, "failure", "failure") for n in range(1, 9)] + passing()[8:]
    done = run(tree, subjects)
    assert done.returncode == 2
    assert "no equal row where both pass on a subject that is not planted" in done.stdout


def test_planted_failure_that_both_pass_is_no_evidence_for_the_case(tree):
    subjects = passing()
    subjects[9] = subject(10, commit=MUST_FAIL)
    done = run(tree, subjects)
    assert done.returncode == 2
    assert f"planted case {MUST_FAIL[:12]} has no equal row where both fail" in done.stdout


def test_check_recorded_twice_is_an_error(tree):
    subjects = passing()
    subjects[0]["checks"].append(check(OLD, OLD_FILE, "success"))
    done = run(tree, subjects)
    assert done.returncode == 3
    assert f"check '{OLD}' is recorded twice" in done.stdout


SIDE_CONDITIONS = [
    (
        "    with:\n",
        "    secrets:\n      token: ${{ secrets.DEPLOY_KEY }}\n    with:\n",
        "the new job receives the secret DEPLOY_KEY",
    ),
    ("    with:\n", "    secrets: inherit\n    with:\n", "receives the secret <inherit>"),
    ("  merge_group:\n", "", "the new workflow does not run on merge_group"),
    (
        ", ready_for_review]",
        "]",
        "the new workflow does not run on pull_request (ready_for_review)",
    ),
    (
        "runner: ubuntu-24.04",
        "runner: arc-runner-set",
        "runner kind of the new job is self-hosted, hosted expected",
    ),
    (
        "runner: ubuntu-24.04",
        "runner: ${{ vars.RUNNER }}",
        "runner kind of the new job is unreadable, hosted expected",
    ),
    (
        "permissions: {}\njobs:\n  licence:\n    name: Licence\n    permissions:\n"
        "      contents: read\n",
        "jobs:\n  licence:\n    name: Licence\n",
        "the new job asks to write <unset>",
    ),
    (
        "jobs:\n",
        "jobs:\n  fetch:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: curl -fsSL https://sdk.example.com/tool.tgz -o tool.tgz\n",
        "the new workflow names the host sdk.example.com",
    ),
    (
        "jobs:\n",
        "jobs:\n  fetch:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - uses: fixture-org/setup-tool@0123456789abcdef0123456789abcdef01234567\n",
        "the new workflow uses fixture-org/setup-tool, which is not an allowed action",
    ),
]


@pytest.mark.parametrize(
    ("old", "new", "message"), SIDE_CONDITIONS, ids=[c[2][:40] for c in SIDE_CONDITIONS]
)
def test_side_condition_fails_the_trial(tree, old, new, message):
    assert CALLER.count(old) == 1
    done = run(tree, passing(), caller=CALLER.replace(old, new))
    assert done.returncode == 1, done.stdout + done.stderr
    assert message in done.stdout
    assert done.counts["equal"] == 10


def test_job_without_permissions_takes_those_of_its_workflow(tree):
    caller = CALLER.replace("    permissions:\n      contents: read\n", "")
    assert caller != CALLER
    assert run(tree, passing(), caller=caller).returncode == 0


def test_runner_kind_from_the_routing_table_is_accepted(tree):
    caller = CALLER.replace("runner: ubuntu-24.04", "runner: arc-runner-set")
    mapping = MAP.replace('draft_stop = "', 'runner = "self-hosted"\ndraft_stop = "')
    done = run(tree, passing(), mapping, caller)
    assert done.returncode == 0, done.stdout


def test_runner_that_cannot_be_read_on_both_sides_is_refused(tree):
    caller = CALLER.replace("runner: ubuntu-24.04", "runner: ${{ vars.RUNNER }}")
    old = OLD_WORKFLOW.replace("runs-on: ubuntu-24.04", "runs-on: ${{ vars.RUNNER }}")
    assert caller != CALLER and old != OLD_WORKFLOW
    done = run(tree, passing(), caller=caller, old_workflow=old)
    assert done.returncode == 1
    assert "runner kind of the new job is unreadable, unreadable expected" in done.stdout


def test_latest_attempt_gives_the_verdict_whatever_the_order_of_the_record(tree):
    rerun = check(NEW, CALLER_FILE, "cancelled", "success")
    rerun["attempts"].reverse()
    done = run(tree, [*passing()[1:], subject(11, "success", rerun)])
    assert done.returncode == 0, done.stdout
    assert done.counts["equal"] == 10 and done.counts["invalid"] == 0


def test_default_of_the_called_workflow_gives_the_runner(tree):
    caller = CALLER.replace("    with:\n      runner: ubuntu-24.04\n", "")
    assert run(tree, passing(), caller=caller).returncode == 0


INPUT_ERRORS = [
    (MAP.replace('class = "R"', 'class = "U"'), "class 'U' is not judged here"),
    (MAP.replace(f'old = "{OLD}"\n', ""), "`old` must be a non-empty string"),
    (MAP.replace(f'new = "{NEW}"', f'new = "{OLD}"'), "a check name stands in two places"),
    (MAP.replace(MUST_FAIL, "abc123"), "`commit` must be a full commit id"),
    (MAP.replace('marker = "reuse lint"\n', ""), "`marker` must be a non-empty string"),
    (MAP.replace('marker = "reuse lint"', 'marker = "reuse (lint"'), "`marker` is no pattern"),
    (MAP.replace('expect = "PASS"', 'expect = "SKIP"'), "`expect` is PASS or FAIL"),
    (MAP + PLANTED_PASS.replace("licence check", "go test"), "has no pair"),
    (MAP.replace(f'old = "{OLD}"', 'old = "No such job"'), "no job is reported as 'No such job'"),
    (MAP.replace(OLD_FILE, ".github/workflows/gone.yml"), "gone.yml"),
    ("[[pair", "trial.toml"),
    (MAP.replace('draft_stop = "', 'runner = "arm"\ndraft_stop = "'), "`runner` is hosted or self"),
    ('repository = "fixture/consumer"\n' + PLANTED_PASS, "no [[pair]]"),
    (MAP.replace('["trial/licence"]', '"trial/licence"'), "`branches` must list branch names"),
]


@pytest.mark.parametrize(("mapping", "message"), INPUT_ERRORS, ids=[e[1] for e in INPUT_ERRORS])
def test_unclear_map_is_an_input_error(tree, mapping, message):
    done = run(tree, passing(), mapping)
    assert done.returncode == 3
    assert message in done.stderr
    assert done.counts == {}


def test_run_data_must_come_from_exactly_one_place(tree):
    run(tree, passing())
    asked = ["--map", str(tree.root / "trial.toml")]
    recorded = ["--runs", str(tree.root / "runs.json")]
    for given in ([*recorded, "--live"], []):
        done = tree.tool("verdict.py", *asked, *given, env=tree.forge({}))
        assert done.returncode == 3, given
        assert "give exactly one of --runs FILE and --live" in done.stderr
    assert tree.forge_calls() == []


def live_runs() -> list[dict]:
    """P1 as the forge would list it: two runs per subject, the last subject failing in both."""
    runs = []
    for number in range(1, 11):
        commit = {9: MUST_PASS, 10: MUST_FAIL}.get(number, f"{number:040x}")
        how = ("failure", "reuse lint", "exit code 1") if number == 10 else ("success",)
        at = f"2026-10-11T02:{number:02d}:00Z"
        old = forge_job(1000 + number, OLD, *how)
        new = forge_job(2000 + number, NEW, *how)
        runs += [forge_run(number, OLD_FILE, commit, at, [old])]
        runs += [forge_run(100 + number, CALLER_FILE, commit, at, [new])]
    return runs


def live(tree, answers, mapping=MAP):
    tree.write(
        {
            OLD_FILE: OLD_WORKFLOW,
            CALLER_FILE: CALLER,
            CALLED_FILE: CALLED,
            "hosts.toml": HOSTS,
            "trial.toml": mapping,
        }
    )
    return tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--live",
        "--record",
        str(tree.root / "recorded.json"),
        env=tree.forge(answers),
    )


def test_p1_read_from_the_forge_passes_and_is_recorded(tree):
    done = live(tree, forge_answers("fixture/consumer", live_runs()))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASS | equal 10" in done.stdout
    assert "source: live forge, fixture/consumer, branches trial/licence" in done.stdout
    assert len(tree.forge_calls()) == 1 + 20 + 2
    again = tree.tool(
        "verdict.py",
        "--map",
        str(tree.root / "trial.toml"),
        "--runs",
        str(tree.root / "recorded.json"),
    )
    assert again.returncode == 0
    assert again.stdout.splitlines()[:-1] == done.stdout.splitlines()[:-1]


def test_n9_request_to_the_forge_fails_midway(tree):
    answers = forge_answers("fixture/consumer", live_runs())
    failing = f"repos/fixture/consumer/actions/runs/107/attempts/1/jobs?{PAGE}"
    answers[failing] = {"fail": "gh: Server Error (HTTP 502)"}
    done = live(tree, answers)
    assert done.returncode == 3
    assert f"error: data source: GET {failing}: gh: Server Error (HTTP 502)" in done.stdout
    assert done.stderr.startswith("ERROR: input or data error, nothing is judged equal")


def test_live_reading_needs_the_branches_of_the_trial(tree):
    assert MAP.count('branches = ["trial/licence"]\n') == 1
    done = live(tree, {}, MAP.replace('branches = ["trial/licence"]\n', ""))
    assert done.returncode == 3
    assert "--live needs `branches` in the map" in done.stderr
    assert tree.forge_calls() == []


def test_unreadable_run_data_is_an_input_error(tree):
    done = run(tree, "no list")
    assert done.returncode == 3
    assert "run data: no list `subjects`" in done.stderr


def test_job_is_found_under_the_name_the_forge_reports():
    doc = {
        "jobs": {
            "plain": {"name": "Unit tests"},
            "legs": {"strategy": {"matrix": {"os": ["a", "b"]}}},
            "named": {"name": "Build on ${{ matrix.os }}"},
        }
    }
    assert verdict._job(doc, "Unit tests", "x.yml") is doc["jobs"]["plain"]
    assert verdict._job(doc, "legs (a)", "x.yml") is doc["jobs"]["legs"]
    assert verdict._job(doc, "Build on ubuntu-24.04", "x.yml") is doc["jobs"]["named"]
    with pytest.raises(ciwf.ReadError):
        verdict._job(doc, "Unit", "x.yml")
