# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Caller templates: the tool renders what the engine's template call renders, or refuses.

The expected texts of the first table are what Go 1.27.1 text/template printed
for the same template and values, with the delimiters `<%` and `%>` and a
struct as context. The second table holds what the tool refuses; Go renders
some of it, and none of it is needed by a caller.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from conftest import REUSABLE, ROOT, external

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import render

CONTEXT = {
    "RunnerTag": "ubuntu-26.04",
    "HasGPU": True,
    "Count": 7,
    "Shared": render.SAMPLE["Shared"],
}
COMMIT = render.SAMPLE["Shared"]["Commit"]
REAL = "templates/callers/licence.yml.tmpl"
TEMPLATE = "templates/callers/licence.yml.tmpl"
CALLED = ".github/workflows/licence.yml"
ISSUE = "cordanaLLM/praetor#1212"
GOOD = f"""\
<%- /*
Required check: the calling job stays out of the required checks ({ISSUE}).
*/ -%>
---
name: Licence check
on:
  pull_request:
permissions: {{}}
jobs:
  licence:
    name: Licence
    permissions:
      contents: read
    uses: <% .Shared.Repository %>/{CALLED}@<% .Shared.Commit %> # <% .Shared.Version %>
    with:
      path: docs
"""
USES = f"    uses: <% .Shared.Repository %>/{CALLED}@<% .Shared.Commit %> # <% .Shared.Version %>\n"
EXCEPTIONS = f"""\
---
exceptions:
  - rule: required-check-is-reported
    path: .github/rulesets/main.json
    issue: {ISSUE}
    reason: the engine declares a name no run reports
    expires: "2026-12-31"
"""
RENDERED = f"""\
---
# Licence check: every file carries its licence information. The job is the
# shared workflow of owner/shared, named by commit.
name: Licence check

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  push:
    branches: [main]
  merge_group:

permissions: {{}}

concurrency:
  group: licence-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  licence:
    name: Licence
    permissions:
      contents: read
    uses: owner/shared/{CALLED}@{COMMIT} # v1.2.3
    with:
      runner: ubuntu-26.04
"""


def check(tree, template=GOOD, called=REUSABLE, exceptions=EXCEPTIONS):
    files = {TEMPLATE: template, CALLED: called}
    if exceptions:
        files["exceptions.yaml"] = exceptions
    return tree.write(files).tool("render.py", "check")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a <% .RunnerTag %> b", "a ubuntu-26.04 b"),
        ("a <%.RunnerTag%> b", "a ubuntu-26.04 b"),
        ("a <%\n.RunnerTag\n%> b", "a ubuntu-26.04 b"),
        ("a  <%- .RunnerTag -%>  \n b", "aubuntu-26.04b"),
        ("a <% .RunnerTag -%>\n\n  b", "a ubuntu-26.04b"),
        ("a\t\r\n<%- .Shared.Version -%>\r\n\tb", "av1.2.3b"),
        ("a <%-\t.RunnerTag %> b", "aubuntu-26.04 b"),
        ("a <% .HasGPU %> <% .Count %> b", "a true 7 b"),
        ("a <% .RunnerTag %><% .Count %> b", "a ubuntu-26.047 b"),
        ("a <<% .Count %>> b", "a <7> b"),
        ("${{ github.ref }} <% .RunnerTag %> ${{ x }}", "${{ github.ref }} ubuntu-26.04 ${{ x }}"),
        ("a <%/* note */%> b", "a  b"),
        ("a <%/**/%> b", "a  b"),
        ("a <%- /* note */ -%> \n b", "ab"),
        ("a <%-\n/* note */\t-%> b", "ab"),
        ("a <%/* note %> still */%> b", "a  b"),
    ],
)
def test_renders_what_the_engine_renders(text, expected):
    assert render.render(text, CONTEXT, "t") == expected


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("a <% hostedGateOn %> b", "neither a field nor a note", id="function"),
        pytest.param('a <% .RunnerTag | printf "%s" %> b', "neither a field", id="pipeline"),
        pytest.param("a <% if .HasGPU %>x<% end %> b", "neither a field", id="condition"),
        pytest.param("a <% . %> b", "neither a field", id="whole-context"),
        pytest.param("a <% %> b", "neither a field", id="empty-action"),
        pytest.param("a <%-.RunnerTag%> b", "neither a field", id="trim-marker-without-space"),
        pytest.param("a <%- .RunnerTag-%> b", "neither a field", id="closing-marker-without-space"),
        pytest.param("a <%-x.RunnerTag %> b", "neither a field", id="text-behind-the-marker"),
        pytest.param("a <% .Missing %> b", "holds no field .Missing", id="undeclared-field"),
        pytest.param("a <% .Shared.Missing %> b", "holds no field", id="undeclared-inner-field"),
        pytest.param("a <% .runnerTag %> b", "holds no field", id="field-in-other-case"),
        pytest.param("a <% .Shared.Commit.X %> b", "holds no field", id="field-of-a-text"),
        pytest.param("a <% .Shared %> b", "no text, number or truth value", id="group-as-value"),
        pytest.param("a <% .RunnerTag b", "opened and not closed", id="unclosed-action"),
        pytest.param("a %> b", "closing delimiter is left", id="stray-closing-delimiter"),
        pytest.param("a <% /* note */ %> b", "neither a field", id="note-after-a-space"),
        pytest.param("a <%/* note */ %> b", "does not end at the closing", id="note-then-space"),
        pytest.param("a <%/* one */ two */%> b", "does not end at the closing", id="note-twice"),
        pytest.param("a <%/* never closed %> b", "note is opened and not closed", id="open-note"),
        pytest.param("", "template is empty", id="empty-template"),
    ],
)
def test_refuses_what_it_does_not_render(text, message):
    with pytest.raises(ciwf.ReadError, match=message):
        render.render(text, CONTEXT, "t")


def test_refusal_names_the_line_and_the_engine_issue():
    with pytest.raises(ciwf.ReadError) as caught:
        render.render("a\nb\n<% hostedGateOn %>\n", CONTEXT, "t.tmpl")
    assert str(caught.value).startswith("t.tmpl:3: ")
    assert "cordanaLLM/praetor#1211" in str(caught.value)


def test_value_that_is_a_fraction_or_a_list_is_refused():
    for value in (1.5, ["a"], None):
        with pytest.raises(ciwf.ReadError, match="no text, number or truth value"):
            render.render("<% .Value %>", {"Value": value}, "t")


def test_value_is_not_read_as_template_text():
    assert render.render("<% .Value %>", {"Value": "<% .Other %> %>"}, "t") == "<% .Other %> %>"


def rendered_real_template():
    return render.render((ROOT / REAL).read_text(encoding="utf-8"), render.SAMPLE, REAL)


def test_licence_template_renders_into_the_recorded_caller():
    assert rendered_real_template() == RENDERED


def test_rendered_licence_caller_passes_both_linters(tree):
    name = ".github/workflows/licence-check.yml"
    tree.write({name: rendered_real_template()})
    done = subprocess.run(
        [*external("ACTIONLINT", "actionlint"), str(tree.root / name)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    command = [*external("ZIZMOR", "zizmor"), "--offline", "--strict-collection"]
    done = subprocess.run(
        [*command, "--persona", "pedantic", str(tree.root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_good_template_passes(tree):
    done = check(tree)
    assert done.returncode == 0, done.stderr
    assert "1 caller template(s)" in done.stdout


def test_planted_templates_differ_from_the_good_one():
    planted = [case.values[0] for case in (*PLANTED, *PLANTED_CALLED)]
    assert len({GOOD, REUSABLE, *planted}) == len(planted) + 2


PLANTED = [
    pytest.param(
        GOOD.replace("@<% .Shared.Commit %>", "@<% .Shared.Version %>"),
        "not named by the commit field alone",
        id="named-by-version",
    ),
    pytest.param(
        GOOD.replace("@<% .Shared.Commit %>", "@main"),
        "not named by the commit field alone",
        id="named-by-branch",
    ),
    pytest.param(
        GOOD.replace("<% .Shared.Repository %>", "plurano/shared-ci"),
        "calls plurano/shared-ci, not the shared repository's field",
        id="repository-written-out",
    ),
    pytest.param(
        GOOD.replace(" # <% .Shared.Version %>", ""),
        "version field is not written",
        id="no-version-comment",
    ),
    pytest.param(
        GOOD.replace("      path: docs\n", "      path: docs\n      branch: main\n"),
        "passes `branch`, which .github/workflows/licence.yml does not declare",
        id="input-not-declared",
    ),
    pytest.param(
        GOOD.replace(f"{CALLED}@", ".github/workflows/absent.yml@"),
        "absent.yml is no reusable workflow of this repository",
        id="called-file-absent",
    ),
    pytest.param(
        GOOD.replace("contents: read", "contents: write"),
        "permissions allow writing: contents",
        id="write-scope",
    ),
    pytest.param(
        GOOD.replace("permissions: {}\n", "").replace(
            "    permissions:\n      contents: read\n", ""
        ),
        "permissions allow writing: <unset>",
        id="no-permissions",
    ),
    pytest.param(
        GOOD.replace("    with:\n", "    secrets: inherit\n    with:\n"),
        "passes secrets",
        id="secrets",
    ),
    pytest.param(
        GOOD.replace(USES, "    runs-on: ubuntu-26.04\n    steps:\n      - run: reuse lint\n"),
        "steps of its own",
        id="own-steps",
    ),
    pytest.param(
        GOOD + f"  second:\n{USES}",
        "exactly one job",
        id="two-jobs",
    ),
    pytest.param(
        GOOD.replace("on:\n  pull_request:\n", "on: <% hostedGateOn %>\n"),
        "neither a field nor a note",
        id="engine-function",
    ),
    pytest.param(
        GOOD.replace("<% .Shared.Version %>", "<% .Shared.Tag %>"),
        "holds no field .Shared.Tag",
        id="undeclared-field",
    ),
    pytest.param(
        GOOD.replace("name: Licence check\n", "name: [Licence check\n"),
        "is no YAML",
        id="no-yaml",
    ),
    pytest.param(
        GOOD.replace(f" ({ISSUE})", ""),
        f"note does not name {ISSUE}",
        id="note-without-the-open-issue",
    ),
]


@pytest.mark.parametrize(("template", "message"), PLANTED)
def test_planted_template_fails(tree, template, message):
    done = check(tree, template)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr
    assert f"FAIL: {TEMPLATE}:" in done.stderr


DEPTH = """\
      depth:
        description: Depth of the search.
        type: number
        required: true
    secrets:"""
PLANTED_CALLED = [
    pytest.param(
        REUSABLE.replace("  workflow_call:\n", "  workflow_dispatch:\n"),
        "licence.yml is no reusable workflow of this repository",
        id="called-file-not-reusable",
    ),
    pytest.param(
        REUSABLE.replace(
            "        default: false\n    secrets:", f"        default: false\n{DEPTH}"
        ),
        "does not pass `depth`, which .github/workflows/licence.yml requires",
        id="required-input-not-passed",
    ),
    pytest.param(
        REUSABLE.replace("      path:\n", "      folder:\n"),
        "passes `path`, which .github/workflows/licence.yml does not declare",
        id="input-renamed-in-the-called-workflow",
    ),
]


@pytest.mark.parametrize(("called", "message"), PLANTED_CALLED)
def test_template_fails_on_a_changed_called_workflow(tree, called, message):
    done = check(tree, called=called)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def test_note_need_not_name_an_issue_that_is_not_open(tree):
    bare = GOOD.replace(f" ({ISSUE})", "")
    assert check(tree, bare, exceptions="").returncode == 0
    other = EXCEPTIONS.replace("required-check-is-reported", "another-rule")
    assert check(tree, bare, exceptions=other).returncode == 0


def test_every_template_is_checked(tree):
    tree.write(
        {"templates/callers/second.yml.tmpl": GOOD.replace("contents: read", "contents: write")}
    )
    done = check(tree)
    assert done.returncode == 1
    assert "FAIL: templates/callers/second.yml.tmpl: " in done.stderr
    assert "licence.yml.tmpl" not in done.stderr


def test_no_template_is_an_error(tree):
    done = tree.write({CALLED: REUSABLE}).tool("render.py", "check")
    assert done.returncode == 3
    assert "no template under templates/callers" in done.stderr


def test_unreadable_called_workflow_is_a_failure(tree):
    done = check(tree, called="---\non: [\n")
    assert done.returncode == 1
    assert f"FAIL: {tree.root / CALLED}" in done.stderr


def test_refused_template_is_reported_with_its_line(tree):
    done = check(tree, GOOD.replace("on:\n  pull_request:\n", "on: <% hostedGateOn %>\n"))
    assert done.returncode == 1
    assert done.stderr.startswith(f"FAIL: {TEMPLATE}:6: `<% hostedGateOn %>`")


CONTEXT_FILE = f"""\
RunnerTag = "ubuntu-26.04"

[Shared]
Repository = "owner/shared"
Commit = "{COMMIT}"
Version = "v1.2.3"
"""


def run_render(tree, context, template=REAL):
    tree.write({"context.toml": context})
    arguments = ["--template", str(ROOT / template), "--context", str(tree.root / "context.toml")]
    return tree.tool("render.py", "render", *arguments)


def test_render_command_prints_the_caller(tree):
    done = run_render(tree, CONTEXT_FILE)
    assert done.returncode == 0, done.stderr
    assert done.stdout == RENDERED


def test_render_command_fails_on_a_context_without_a_field(tree):
    done = run_render(tree, CONTEXT_FILE.replace('Version = "v1.2.3"\n', ""))
    assert done.returncode == 3
    assert "holds no field .Shared.Version" in done.stderr
    assert done.stdout == ""


def test_render_command_fails_on_an_unreadable_context(tree):
    done = run_render(tree, "RunnerTag = \n")
    assert done.returncode == 3
    assert done.stdout == ""


def test_render_command_needs_template_and_context(tree):
    done = tree.tool("render.py", "render")
    assert done.returncode == 3
    assert "needs --template and --context" in done.stderr
