# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Runner labels: a workflow that names a floating label of a hosted machine fails.

The check asks no host and starts no engine, so the tool itself runs here on
planted workflows. `GOOD` names the floating label in every place that chooses
no machine (a description, a variable, a name, a command, a comment) and a
release in every place that does; each planted case moves the floating label
into one of those places.
"""

from __future__ import annotations

import shlex
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import pins
import render

WORKFLOW = ".github/workflows/build.yml"
GOOD = """\
name: Build
on:
  workflow_call:
    inputs:
      runner:
        description: The machine of the job; ubuntu-latest is refused.
        type: string
        default: ubuntu-26.04
  workflow_dispatch:
    inputs:
      machine:
        type: choice
        options:
          - ubuntu-24.04
          - ubuntu-26.04
permissions: {}
env:
  NOTE: this is not ubuntu-latest
jobs:
  build:
    name: Build on what ubuntu-latest once was
    runs-on: ubuntu-26.04
    strategy:
      matrix:
        os: [ubuntu-22.04, windows-2025]
        include:
          - os: macos-26
    steps:
      - name: Say ubuntu-latest
        run: echo ubuntu-latest  # ubuntu-latest
  called:
    name: Called
    uses: ./.github/workflows/other.yml
    with:
      runner: ubuntu-24.04-arm
"""
RUNS_ON = "    runs-on: ubuntu-26.04\n"
BUILD = "`runs-on` of job `build`"
MATRIX = "`strategy` of job `build`"
CHOICE = "ubuntu-latest' || 'windows-latest"


def planted(old: str, new: str) -> str:
    assert GOOD.count(old) == 1
    return GOOD.replace(old, new)


def line_of(text: str, part: str) -> int:
    """The line a text first holds a part on."""
    return text[: text.index(part)].count("\n") + 1


def run(tree, workflow=GOOD, env=None):
    tree.write({WORKFLOW: workflow})
    return tree.tool("pins.py", "runners", env=env)


def test_workflow_on_releases_passes_whatever_its_names_and_commands_say(tree):
    done = run(tree)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("PASS: 1 file(s), 1 job(s) with a `runs-on`, no floating")
    assert "0 of them take(s) the label from an expression" in done.stdout
    assert done.stderr == ""


@pytest.mark.parametrize(
    ("old", "new", "label", "where"),
    [
        (RUNS_ON, "    runs-on: ubuntu-latest\n", "ubuntu-latest", BUILD),
        (RUNS_ON, '    runs-on: "ubuntu-latest"\n', "ubuntu-latest", BUILD),
        (RUNS_ON, "    runs-on: Ubuntu-Latest\n", "ubuntu-latest", BUILD),
        (RUNS_ON, "    runs-on: windows-latest\n", "windows-latest", BUILD),
        (RUNS_ON, "    runs-on: macos-latest\n", "macos-latest", BUILD),
        (RUNS_ON, "    runs-on: macos-latest-large\n", "macos-latest-large", BUILD),
        (RUNS_ON, "    runs-on: macos-latest-xlarge\n", "macos-latest-xlarge", BUILD),
        (RUNS_ON, "    runs-on: [self-hosted, ubuntu-latest]\n", "ubuntu-latest", BUILD),
        (
            RUNS_ON,
            "    runs-on:\n      - self-hosted\n      - ubuntu-latest\n",
            "ubuntu-latest",
            BUILD,
        ),
        (
            RUNS_ON,
            "    runs-on:\n      group: large\n      labels: [windows-latest]\n",
            "windows-latest",
            BUILD,
        ),
        (
            RUNS_ON,
            "    runs-on: ${{ github.event_name == 'push' && 'ubuntu-latest' || 'macos-26' }}\n",
            "ubuntu-latest",
            BUILD,
        ),
        (
            "os: [ubuntu-22.04, windows-2025]",
            "os: [ubuntu-latest, windows-2025]",
            "ubuntu-latest",
            MATRIX,
        ),
        ("- os: macos-26", "- os: macos-latest", "macos-latest", MATRIX),
        (
            "        os: [ubuntu-22.04, windows-2025]\n",
            """        os: ${{ fromJSON('["ubuntu-latest"]') }}\n""",
            "ubuntu-latest",
            MATRIX,
        ),
        (
            "default: ubuntu-26.04",
            "default: ubuntu-latest",
            "ubuntu-latest",
            "`default` of input `runner` of workflow_call",
        ),
        (
            "- ubuntu-24.04\n",
            "- ubuntu-latest\n",
            "ubuntu-latest",
            "`options` of input `machine` of workflow_dispatch",
        ),
        (
            "runner: ubuntu-24.04-arm",
            "runner: ubuntu-latest",
            "ubuntu-latest",
            "`with` of job `called`",
        ),
    ],
)
def test_floating_label_where_a_workflow_chooses_a_machine_fails(tree, old, new, label, where):
    workflow = planted(old, new)
    done = run(tree, workflow)
    assert done.returncode == 1, done.stdout
    assert done.stdout == ""
    line = line_of(workflow, new.strip().splitlines()[-1])
    assert done.stderr == (
        f"FAIL: {WORKFLOW}:{line}: {where}: `{label}` is a floating runner label; "
        "name the release it stands for\n"
    )


@pytest.mark.parametrize(
    "label", ["ubuntu-latest-xl", "team-ubuntu-latest", "ubuntu-latest_8core", "ubuntu-latest.eu"]
)
def test_label_of_its_own_that_holds_the_floating_one_passes(tree, label):
    done = run(tree, planted(RUNS_ON, f"    runs-on: {label}\n"))
    assert done.returncode == 0, done.stderr


def test_every_floating_label_of_one_value_is_a_finding(tree):
    workflow = planted(RUNS_ON, f"    runs-on: ${{{{ inputs.big && '{CHOICE}' }}}}\n")
    done = run(tree, workflow)
    assert done.returncode == 1, done.stdout
    line = line_of(workflow, CHOICE)
    assert [text.split(": ")[1:4:2] for text in done.stderr.splitlines()] == [
        [
            f"{WORKFLOW}:{line}",
            "`ubuntu-latest` is a floating runner label; name the release it stands for",
        ],
        [
            f"{WORKFLOW}:{line}",
            "`windows-latest` is a floating runner label; name the release it stands for",
        ],
    ]


def test_label_taken_from_an_expression_is_counted_and_its_values_are_read(tree):
    done = run(tree, planted(RUNS_ON, "    runs-on: ${{ inputs.runner }}\n"))
    assert done.returncode == 0, done.stderr
    assert "1 job(s) with a `runs-on`" in done.stdout
    assert "1 of them take(s) the label from an expression" in done.stdout


def test_aliased_label_is_a_finding_for_each_job_that_takes_it(tree):
    anchored = planted(RUNS_ON, "    runs-on: &machine ubuntu-latest\n")
    again = "  second:\n    name: Second\n    runs-on: *machine\n    steps: []\n"
    done = run(tree, anchored + again)
    assert done.returncode == 1, done.stdout
    assert "`runs-on` of job `build`: `ubuntu-latest`" in done.stderr
    assert "`runs-on` of job `second`: `ubuntu-latest`" in done.stderr
    assert len(done.stderr.splitlines()) == 2


def test_value_shared_by_many_aliases_is_read_once(tree):
    levels = [f"        l{n}: &l{n} [*l{n - 1}, *l{n - 1}]\n" for n in range(1, 17)]
    nest = "        l0: &l0 [ubuntu-latest]\n" + "".join(levels)
    done = run(tree, planted("      matrix:\n", "      matrix:\n" + nest))
    assert done.returncode == 1, done.stdout
    assert len(done.stderr.splitlines()) == 1
    assert f"{MATRIX}: `ubuntu-latest`" in done.stderr


def test_check_starts_no_engine_and_asks_no_client(tree):
    marker = tree.root / "started"
    starts = shlex.join([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])
    names = ("GH_TOKEN", "GITHUB_TOKEN", "ZIZMOR_GITHUB_TOKEN")
    done = run(tree, env={"ZIZMOR": starts, "GH": starts, **dict.fromkeys(names, "")})
    assert done.returncode == 0, done.stderr
    assert not marker.exists()


def test_action_file_chooses_no_machine_and_is_not_read(tree):
    action = (
        "name: Stop\ninputs:\n  runner:\n    default: ubuntu-latest\n"
        "runs:\n  using: composite\n  steps: []\n"
    )
    tree.write({"actions/stop/action.yml": action})
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("PASS: 1 file(s), 0 job(s) with a `runs-on`")
    done = run(tree, planted(RUNS_ON, "    runs-on: ubuntu-latest\n"))
    assert done.returncode == 1, done.stdout
    assert "actions/stop" not in done.stderr


def test_tree_without_a_workflow_is_an_error(tree):
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 3, done.stdout
    assert "no workflow and no action file" in done.stderr


@pytest.mark.parametrize(
    "new",
    ["    runs-on:\n      <<: {labels: ubuntu-latest}\n", "    runs-on: [ubuntu-latest\n"],
)
def test_workflow_that_cannot_be_read_is_an_error_and_no_pass(tree, new):
    done = run(tree, planted(RUNS_ON, new))
    assert done.returncode == 3, done.stdout
    assert done.stderr.startswith("ERROR: ")
    assert "PASS" not in done.stdout


def test_workflows_of_this_repository_name_no_floating_label(capsys):
    assert pins.runners(ROOT) == 0
    said = capsys.readouterr()
    assert "no floating runner label" in said.out
    assert said.err == ""


def test_licence_workflow_input_runner_default_is_pinned(tree):
    """The licence workflow declares its default runner as the current pinned release."""
    licence_path = ROOT / ".github/workflows/licence.yml"
    doc = ciwf.load(licence_path)
    default = doc["on"]["workflow_call"]["inputs"]["runner"]["default"]
    assert default == "ubuntu-26.04"
    # A planted floating default is refused by the runner check.
    text = licence_path.read_text(encoding="utf-8")
    planted = text.replace(f"default: {default}", "default: ubuntu-latest")
    tree.write({".github/workflows/licence.yml": planted})
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 1, done.stdout
    assert (
        "default` of input `runner` of workflow_call: `ubuntu-latest` is a floating runner label"
        in done.stderr
    )


def test_caller_passing_pinned_runner_is_accepted_and_floating_label_is_refused(tree):
    """A caller passing the pinned runner label is accepted; a floating label is refused."""
    tmpl = (ROOT / "templates/callers/licence.yml.tmpl").read_text(encoding="utf-8")
    caller = render.render(tmpl, render.SAMPLE, "licence.yml.tmpl")
    licence_text = (ROOT / ".github/workflows/licence.yml").read_text(encoding="utf-8")
    tree.write(
        {
            ".github/workflows/licence.yml": licence_text,
            ".github/workflows/licence-check.yml": caller,
        }
    )
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 0, done.stderr
    assert "no floating runner label" in done.stdout

    # Planted negative: a caller passing a floating runner label is refused.
    planted = caller.replace("runner: ubuntu-26.04", "runner: ubuntu-latest")
    assert planted != caller
    tree.write({".github/workflows/licence-check.yml": planted})
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 1, done.stdout
    assert (
        "`with` of job `licence`: `ubuntu-latest` is a floating runner label; "
        "name the release it stands for"
    ) in done.stderr


def test_caller_without_runner_input_relies_on_pinned_workflow_default(tree):
    """A caller omitting the runner input relies on the workflow's pinned default."""
    licence_path = ROOT / ".github/workflows/licence.yml"
    doc = ciwf.load(licence_path)
    inputs = doc["on"]["workflow_call"]["inputs"]
    assert inputs["runner"].get("required") is not True
    default = inputs["runner"]["default"]
    assert default == "ubuntu-26.04"

    # A caller omitting the runner input passes while the workflow default is pinned.
    tmpl = (ROOT / "templates/callers/licence.yml.tmpl").read_text(encoding="utf-8")
    caller = render.render(tmpl, render.SAMPLE, "licence.yml.tmpl")
    without_runner = caller.replace(f"    with:\n      runner: {default}\n", "")
    assert without_runner != caller
    licence_text = licence_path.read_text(encoding="utf-8")
    tree.write(
        {
            ".github/workflows/licence.yml": licence_text,
            ".github/workflows/licence-check.yml": without_runner,
        }
    )
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 0, done.stderr
    assert "no floating runner label" in done.stdout

    # Planted negative: when the workflow default is floating, the check refuses it.
    planted_licence = licence_text.replace(f"default: {default}", "default: ubuntu-latest")
    assert planted_licence != licence_text
    tree.write({".github/workflows/licence.yml": planted_licence})
    done = tree.tool("pins.py", "runners")
    assert done.returncode == 1, done.stdout
    assert (
        "default` of input `runner` of workflow_call: `ubuntu-latest` is a floating runner label"
        in done.stderr
    )
