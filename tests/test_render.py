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
    "Licence": render.SAMPLE["Licence"],
}
COMMIT = render.SAMPLE["Shared"]["Commit"]
REAL = "templates/callers/licence.yml.tmpl"
REAL_SCORECARD = "templates/callers/scorecard.yml.tmpl"
REAL_TRAIN = "templates/callers/merge-train.yml.tmpl"
TEMPLATE = "templates/callers/licence.yml.tmpl"
CALLED = ".github/workflows/licence.yml"
ISSUE = "cordanaLLM/praetor#1212"
GOOD = f"""\
<%- /*
Required check: the job `result` reports the calling job's name ({ISSUE}).
*/ -%>
<%- /* REUSE-IgnoreStart */ -%>
# SPDX-FileCopyrightText: <% .Licence.CopyrightText %>
# SPDX-License-Identifier: <% .Licence.Identifier %>
<%/* REUSE-IgnoreEnd */ -%>
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

  result:
    name: Licence
    needs: licence
    if: always()
    runs-on: <% .RunnerTag %>
    timeout-minutes: 5
    permissions: {{}}
    steps:
      - name: Fail unless the licence check passed
        if: needs.licence.result != 'success'
        shell: bash
        run: |
          echo "The licence check did not pass."
          exit 1
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
# SPDX-FileCopyrightText: 2026 Owner <owner@example.org>
# SPDX-License-Identifier: EUPL-1.2
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

  # The forge reports the calling job above only as "Licence / REUSE lint",
  # never as "Licence". This job reports "Licence" and fails unless the licence
  # check passed; it runs under always() so a failed or cancelled check still
  # reports here.
  result:
    name: Licence
    needs: licence
    if: always()
    runs-on: ubuntu-26.04
    timeout-minutes: 5
    permissions: {{}}
    steps:
      - name: Fail unless the licence check passed
        if: needs.licence.result != 'success'
        shell: bash
        run: |
          echo "The licence check did not pass."
          exit 1
"""


RENDERED_SCORECARD = f"""\
# SPDX-FileCopyrightText: 2026 Owner <owner@example.org>
# SPDX-License-Identifier: EUPL-1.2
---
# Scorecard: the supply-chain checks of this repository, reported to code
# scanning. The job is the shared workflow of owner/shared, named
# by commit. It runs in a public repository only.
name: Scorecard check

on:
  push:
    branches: [main]
  schedule:
    - cron: "23 4 * * 2"

permissions: {{}}

concurrency:
  group: scorecard-${{{{ github.ref }}}}
  cancel-in-progress: true

jobs:
  scorecard:
    name: Scorecard
    permissions:
      contents: read
      security-events: write # the findings go to the forge's code scanning
      id-token: write # asked for by the called job; used only for a publication
    uses: owner/shared/.github/workflows/scorecard.yml@{COMMIT} # v1.2.3
    with:
      outside_hosts: {render.PUBLIC}
"""


def check(tree, template=GOOD, called=REUSABLE, exceptions=EXCEPTIONS, hosts=""):
    files = {TEMPLATE: template, CALLED: called}
    if exceptions:
        files["exceptions.yaml"] = exceptions
    if hosts:
        files["hosts.toml"] = hosts
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


def rendered_real_template(template=REAL):
    return render.render((ROOT / template).read_text(encoding="utf-8"), render.SAMPLE, template)


def test_licence_template_renders_into_the_recorded_caller():
    assert rendered_real_template() == RENDERED


def test_scorecard_template_renders_into_the_recorded_caller():
    assert rendered_real_template(REAL_SCORECARD) == RENDERED_SCORECARD


@pytest.mark.parametrize(
    ("template", "name"),
    [
        (REAL, ".github/workflows/licence-check.yml"),
        (REAL_SCORECARD, ".github/workflows/scorecard-check.yml"),
        (REAL_TRAIN, ".github/workflows/merge-train.yml"),
    ],
    ids=["licence", "scorecard", "merge-train"],
)
def test_rendered_caller_passes_both_linters(tree, template, name):
    tree.write({name: rendered_real_template(template)})
    # The runner label a consumer's adoption declares to actionlint, as here.
    config = ["-config-file", str(ROOT / ".github/actionlint.yaml")]
    done = subprocess.run(
        [*external("ACTIONLINT", "actionlint"), *config, str(tree.root / name)],
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
    planted = [
        case.values[0] for case in (*PLANTED, *PLANTED_RESULT, *PLANTED_CALLED, *PLANTED_PUBLIC)
    ]
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
        GOOD.replace("<% .Shared.Repository %>", "plurano/horistes"),
        "calls plurano/horistes, not the shared repository's field",
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
        f"the job grants `contents: write`, which {CALLED} does not ask for",
        id="write-scope",
    ),
    pytest.param(
        GOOD.replace("      contents: read\n", "      contents: read\n      actions: read\n"),
        f"the job grants `actions: read`, which {CALLED} does not ask for",
        id="scope-not-asked-for",
    ),
    pytest.param(
        GOOD.replace("    permissions:\n      contents: read\n", "    permissions: {}\n"),
        f"the job does not grant `contents: read`, which {CALLED} asks for",
        id="scope-not-granted",
    ),
    pytest.param(
        GOOD.replace("    permissions:\n      contents: read\n", ""),
        f"the job does not grant `contents: read`, which {CALLED} asks for",
        id="grant-left-to-the-empty-workflow-level",
    ),
    pytest.param(
        GOOD.replace("\npermissions: {}\n", "\n").replace(
            "    permissions:\n      contents: read\n", ""
        ),
        "the job's permissions name no scopes: <unset>",
        id="no-permissions",
    ),
    pytest.param(
        GOOD.replace("    permissions:\n      contents: read\n", "    permissions: read-all\n"),
        "the job's permissions name no scopes: read-all",
        id="permissions-by-word",
    ),
    pytest.param(
        GOOD.replace("    with:\n", "    secrets: inherit\n    with:\n"),
        f"the job passes secrets as `inherit`; it names each one {CALLED} declares",
        id="secrets-inherit",
    ),
    pytest.param(
        GOOD.replace("    with:\n", "    secrets:\n      key: ${{ secrets.KEY }}\n    with:\n"),
        f"the job passes the secret `key`, which {CALLED} does not declare",
        id="secret-not-declared",
    ),
    pytest.param(
        GOOD.replace("    with:\n", "    secrets:\n      token: ${{ github.token }}\n    with:\n"),
        "the job passes the secret `token` as something other than one secret",
        id="secret-not-a-secret",
    ),
    pytest.param(
        GOOD.replace("    with:\n", "    secrets:\n      token: planted-value\n    with:\n"),
        "the job passes the secret `token` as something other than one secret",
        id="secret-written-out",
    ),
    pytest.param(
        GOOD.replace(
            "    with:\n",
            "    secrets:\n      token: ${{ secrets.A }}${{ secrets.B }}\n    with:\n",
        ),
        "something other than one secret of the repository",
        id="two-secrets-in-one",
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
    pytest.param(
        GOOD.replace(
            "<%- /* REUSE-IgnoreStart */ -%>\n"
            "# SPDX-FileCopyrightText: <% .Licence.CopyrightText %>\n"
            "# SPDX-License-Identifier: <% .Licence.Identifier %>\n"
            "<%/* REUSE-IgnoreEnd */ -%>\n",
            "",
        ),
        "the rendered caller does not start with the two SPDX header lines",
        id="no-spdx-header",
    ),
    pytest.param(
        GOOD.replace("# SPDX-" + "License-Identifier: <% .Licence.Identifier %>\n", ""),
        "the rendered caller does not start with the two SPDX header lines",
        id="spdx-missing-identifier",
    ),
]


@pytest.mark.parametrize(("template", "message"), PLANTED)
def test_planted_template_fails(tree, template, message):
    done = check(tree, template)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr
    assert f"FAIL: {TEMPLATE}:" in done.stderr


RESULT_JOB = GOOD[GOOD.index("\n  result:\n") :]
STEP_IF = "if: needs.licence.result != 'success'"
ECHO = 'echo "The licence check did not pass."'
PLANTED_RESULT = [
    pytest.param(
        GOOD.replace(RESULT_JOB, "\n"),
        "the caller runs on pull requests and no job `result` reports `Licence`",
        id="result-missing",
    ),
    pytest.param(
        GOOD.replace("    if: always()\n", "    if: success()\n"),
        "job `result` has `if: success()`, not `always()`",
        id="result-without-always",
    ),
    pytest.param(
        GOOD.replace("    if: always()\n", ""),
        "job `result` has keys other than",
        id="result-without-condition",
    ),
    pytest.param(
        GOOD.replace("    needs: licence\n", "    needs: [licence, other]\n"),
        "job `result` has `needs: ['licence', 'other']`, not `licence`",
        id="result-needs-more",
    ),
    pytest.param(
        GOOD.replace("    name: Licence\n    needs:", "    name: Licence result\n    needs:"),
        "job `result` has `name: Licence result`, not `Licence`",
        id="result-under-another-name",
    ),
    pytest.param(
        GOOD.replace(
            "    permissions: {}\n    steps:", "    permissions:\n      contents: write\n    steps:"
        ),
        "job `result` has `permissions: {'contents': 'write'}`, not `{}`",
        id="result-with-scopes",
    ),
    pytest.param(
        GOOD.replace(
            "    timeout-minutes: 5\n", "    timeout-minutes: 5\n    continue-on-error: true\n"
        ),
        "job `result` has keys other than",
        id="result-may-fail",
    ),
    pytest.param(
        GOOD.replace("timeout-minutes: 5", "timeout-minutes: 60"),
        "job `result` has no timeout of 1 to 10 minutes",
        id="result-timeout-too-long",
    ),
    pytest.param(
        GOOD.replace("timeout-minutes: 5", "timeout-minutes: 0"),
        "job `result` has no timeout of 1 to 10 minutes",
        id="result-timeout-zero",
    ),
    pytest.param(
        GOOD + "      - name: Second\n        run: exit 0\n",
        "job `result` has not exactly one step",
        id="result-second-step",
    ),
    pytest.param(
        GOOD.replace(STEP_IF, "if: needs.licence.result == 'failure'"),
        "the step of job `result` does not run on `needs.licence.result != 'success'`",
        id="result-passes-a-cancelled-call",
    ),
    pytest.param(
        GOOD.replace(STEP_IF + "\n", STEP_IF + "\n        continue-on-error: true\n"),
        "the step of job `result` has keys other than",
        id="result-step-may-fail",
    ),
    pytest.param(
        GOOD.replace("shell: bash", "shell: pwsh"),
        "the step of job `result` does not run in bash",
        id="result-step-in-another-shell",
    ),
    pytest.param(
        GOOD.replace("exit 1", "exit 0"),
        "the step of job `result` does more than print a line and `exit 1`",
        id="result-step-passes",
    ),
    pytest.param(
        GOOD.replace(ECHO, "curl -fsS https://example.org"),
        "the step of job `result` does more than print a line and `exit 1`",
        id="result-step-runs-a-command",
    ),
    pytest.param(
        GOOD.replace(ECHO, 'echo "$(id)"'),
        "the step of job `result` does more than print a line and `exit 1`",
        id="result-step-expands",
    ),
]


@pytest.mark.parametrize(("template", "message"), PLANTED_RESULT)
def test_planted_result_job_fails(tree, template, message):
    done = check(tree, template)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def test_caller_that_gates_no_pull_request_needs_no_result_job(tree):
    pushed = GOOD.replace(RESULT_JOB, "\n").replace("on:\n  pull_request:\n", "on:\n  push:\n")
    assert check(tree, pushed).returncode == 0
    for event in ("pull_request_target", "merge_group"):
        gated = pushed.replace("on:\n  push:\n", f"on:\n  {event}:\n")
        done = check(tree, gated)
        assert done.returncode == 1, done.stdout
        assert "no job `result` reports `Licence`" in done.stderr


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
    pytest.param(
        REUSABLE.replace("  contents: read\njobs:", "  contents: read\n  id-token: write\njobs:"),
        f"the job does not grant `id-token: write`, which {CALLED} asks for",
        id="called-workflow-asks-for-more",
    ),
    pytest.param(
        REUSABLE.replace(
            "    timeout-minutes: 5\n", "    timeout-minutes: 5\n    permissions: {}\n"
        ),
        f"the job grants `contents: read`, which {CALLED} does not ask for",
        id="called-job-asks-for-less-than-its-workflow",
    ),
    pytest.param(
        REUSABLE.replace(
            "        required: false\n    outputs:", "        required: true\n    outputs:"
        ),
        f"the job does not pass the secret `token`, which {CALLED} requires",
        id="required-secret-not-passed",
    ),
    pytest.param(
        REUSABLE.replace("permissions:\n  contents: read\njobs:", "jobs:"),
        f"a job of {CALLED} names no scopes, so the grant cannot be compared",
        id="called-workflow-without-scopes",
    ),
]


PASSES_TOKEN = GOOD.replace(
    "    with:\n", "    secrets:\n      token: ${{ secrets.LICENCE_TOKEN }}\n    with:\n"
)


def test_secret_the_called_workflow_declares_is_passed_by_name(tree):
    done = check(tree, PASSES_TOKEN)
    assert done.returncode == 0, done.stderr


def test_secret_renamed_in_the_called_workflow_fails(tree):
    called = REUSABLE.replace("    secrets:\n      token:\n", "    secrets:\n      key:\n")
    assert called != REUSABLE
    done = check(tree, PASSES_TOKEN, called=called)
    assert done.returncode == 1, done.stdout
    assert f"the job passes the secret `token`, which {CALLED} does not declare" in done.stderr


def test_secret_written_out_is_not_printed(tree):
    # Built at run time so the secret scan of the gate does not take the test
    # for a leak; the shape is that of a forge token.
    planted = "ghp_" + "Zq7" * 12
    text = GOOD.replace("    with:\n", f"    secrets:\n      token: {planted}\n    with:\n")
    done = check(tree, text)
    assert done.returncode == 1, done.stdout
    assert planted not in done.stdout + done.stderr
    assert "the job passes the secret `token` as something other than one secret" in done.stderr


@pytest.mark.parametrize(("called", "message"), PLANTED_CALLED)
def test_template_fails_on_a_changed_called_workflow(tree, called, message):
    done = check(tree, called=called)
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def other_job(name, level):
    head = f"  {name}:\n    name: {name}\n    runs-on: ubuntu-24.04\n    permissions:\n"
    return head + f"      contents: read\n      checks: {level}\n    steps:\n      - run: exit 0\n"


def test_grant_is_the_highest_level_over_the_called_jobs(tree):
    called = REUSABLE + other_job("report", "write") + other_job("last", "read")
    done = check(tree, called=called)
    assert done.returncode == 1, done.stdout
    assert f"the job does not grant `checks: write`, which {CALLED} asks for" in done.stderr
    lower = GOOD.replace("      contents: read\n", "      contents: read\n      checks: read\n")
    assert check(tree, lower, called=called).returncode == 1
    template = GOOD.replace("      contents: read\n", "      contents: read\n      checks: write\n")
    assert check(tree, template, called=called).returncode == 0


MARKED = f"""\
forge = ["github.com"]

[actions]
"actions/checkout" = []

[files."{CALLED}"."rekor.sigstore.dev"]
reason = "public transparency log"
input = "publish"
public_only = true
"""
PASSED = "      path: docs\n"
REFUSED = "it switches on a host for public repositories only and is passed as"
PLANTED_PUBLIC = [
    pytest.param(GOOD.replace(PASSED, PASSED + "      publish: true\n"), "true", id="true"),
    pytest.param(GOOD.replace(PASSED, PASSED + '      publish: "true"\n'), "true", id="text"),
    pytest.param(
        GOOD.replace(PASSED, PASSED + "      publish: ${{ true }}\n"),
        "${{ true }}",
        id="expression-that-is-always-true",
    ),
    pytest.param(
        GOOD.replace(PASSED, PASSED + "      publish: ${{ !github.event.repository.private }}\n"),
        "${{ !github.event.repository.private }}",
        id="expression-that-is-on-where-the-event-names-no-repository",
    ),
    pytest.param(
        GOOD.replace(PASSED, PASSED + f"      publish: {render.PUBLIC.replace('==', '!=')}\n"),
        "${{ github.event.repository.visibility != 'public' }}",
        id="expression-turned-round",
    ),
]


@pytest.mark.parametrize(("template", "written"), PLANTED_PUBLIC)
def test_switch_for_public_repositories_is_not_passed_as_anything_else(tree, template, written):
    done = check(tree, template, hosts=MARKED)
    assert done.returncode == 1, done.stdout
    assert f"the job passes `publish` as `{written}`; {REFUSED}" in done.stderr


@pytest.mark.parametrize(
    "line",
    ["", "      publish: false\n", f"      publish: {render.PUBLIC}\n"],
    ids=["not-passed", "false", "visibility"],
)
def test_switch_for_public_repositories_is_passed_off_or_by_visibility(tree, line):
    done = check(tree, GOOD.replace(PASSED, PASSED + line), hosts=MARKED)
    assert done.returncode == 0, done.stderr


def test_switch_without_the_public_mark_takes_a_plain_true(tree):
    template = GOOD.replace(PASSED, PASSED + "      publish: true\n")
    done = check(tree, template, hosts=MARKED.replace("public_only = true\n", ""))
    assert done.returncode == 0, done.stderr
    assert check(tree, template).returncode == 0


def test_real_scorecard_caller_in_a_private_repository_cannot_pass_true(tree):
    """The ruled plant: a caller switches the outside hosts on without asking the visibility."""
    called = ".github/workflows/scorecard.yml"
    source = (ROOT / REAL_SCORECARD).read_text(encoding="utf-8")
    assert f"outside_hosts: {render.PUBLIC}\n" in source
    files = {name: (ROOT / name).read_text(encoding="utf-8") for name in (called, "hosts.toml")}
    tree.write({**files, "exceptions.yaml": EXCEPTIONS, "templates/callers/s.yml.tmpl": source})
    done = tree.tool("render.py", "check")
    assert done.returncode == 0, done.stderr
    planted = source.replace(f"outside_hosts: {render.PUBLIC}\n", "outside_hosts: true\n")
    tree.write({"templates/callers/s.yml.tmpl": planted})
    done = tree.tool("render.py", "check")
    assert done.returncode == 1, done.stdout
    assert f"the job passes `outside_hosts` as `true`; {REFUSED}" in done.stderr
    publish = source + "      publish: true\n"
    tree.write({"templates/callers/s.yml.tmpl": publish})
    done = tree.tool("render.py", "check")
    assert done.returncode == 1, done.stdout
    assert f"the job passes `publish` as `true`; {REFUSED}" in done.stderr


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
    assert done.stderr.startswith(f"FAIL: {TEMPLATE}:10: `<% hostedGateOn %>`")


CONTEXT_FILE = f"""\
RunnerTag = "ubuntu-26.04"

[Shared]
Repository = "owner/shared"
Commit = "{COMMIT}"
Version = "v1.2.3"

[Licence]
CopyrightText = "2026 Owner <owner@example.org>"
Identifier = "EUPL-1.2"
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
