# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@proton.me>
# SPDX-License-Identifier: EUPL-1.2
"""Host scan: an undeclared host, an action outside the list and a stale declaration all fail."""

from __future__ import annotations

import pytest
from conftest import ACTION, CHECKOUT, HOSTS, REUSABLE

WORKFLOW = ".github/workflows/licence.yml"
RUN = '          test -d "$TARGET"\n'
DECLARED = (
    HOSTS
    + f'\n[files."{WORKFLOW}"."rekor.sigstore.dev"]\n'
    + 'reason = "public transparency log"\ninput = "publish"\n'
)


def scan(tree, workflow=REUSABLE, hosts=HOSTS):
    tree.write({WORKFLOW: workflow, "actions/draft-stop/action.yml": ACTION, "hosts.toml": hosts})
    return tree.tool("hosts.py", "check")


def planted(line):
    assert RUN in REUSABLE
    return REUSABLE.replace(RUN, RUN + f"          {line}\n")


def test_forge_only_tree_passes(tree):
    done = scan(tree)
    assert done.returncode == 0, done.stderr
    assert "2 files, 1 outside actions" in done.stdout


UNDECLARED = [
    (
        "curl -fsSL https://sdk.example.com/tool.tgz -o tool.tgz",
        "host sdk.example.com (URL) is not declared",
    ),
    (
        "curl -fsSL https://user:pw@sdk.example.com:8443/tool.tgz",
        "host sdk.example.com (URL) is not declared",
    ),
    ('curl -fsSL "https://$MIRROR/tool.tgz"', "host <dynamic> (URL) is not declared"),
    ("curl -fsSL https://${{ inputs.path }}/tool.tgz", "host <dynamic> (URL) is not declared"),
    (
        "docker pull registry.example.com/base/image:1",
        "host registry.example.com (registry path) is not declared",
    ),
    ("pip install reuse", "host pypi.org (command `pip install`) is not declared"),
    (
        "go install example.org/x/tool@v1",
        "host proxy.golang.org (command `go install`) is not declared",
    ),
    ("npm ci", "host registry.npmjs.org (command `npm ci`) is not declared"),
    ("sudo apt-get install -y jq", "host <os-package-archive>"),
]


@pytest.mark.parametrize(
    ("line", "message"),
    UNDECLARED,
    ids=[u[0].split()[0] + str(i) for i, u in enumerate(UNDECLARED)],
)
def test_undeclared_host_fails(tree, line, message):
    done = scan(tree, planted(line))
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def test_offline_command_implies_no_registry(tree):
    assert scan(tree, planted("cargo test --locked --offline")).returncode == 0


def test_forge_url_needs_no_declaration(tree):
    assert scan(tree, planted("curl -fsSL https://api.github.com/meta")).returncode == 0


def test_container_image_host_is_found(tree):
    workflow = REUSABLE.replace(
        "    runs-on: ubuntu-24.04\n",
        "    runs-on: ubuntu-24.04\n    container: registry.internal:5000/fixture/image:1\n",
    )
    done = scan(tree, workflow)
    assert done.returncode == 1
    # No URL and no known top-level domain: only the image reading can find this host.
    assert "host registry.internal (container image) is not declared" in done.stderr


def test_image_without_registry_is_the_default_registry(tree):
    workflow = REUSABLE.replace(
        "    runs-on: ubuntu-24.04\n",
        "    runs-on: ubuntu-24.04\n    services:\n      db:\n        image: postgres:17\n",
    )
    done = scan(tree, workflow)
    assert done.returncode == 1
    assert "host docker.io (container image) is not declared" in done.stderr


def test_action_outside_the_allowed_list_fails(tree):
    step = "      - name: Run\n"
    pin = "0123456789abcdef0123456789abcdef01234567"
    extra = f"      - name: Set up\n        uses: fixture-org/setup-tool@{pin} # v1\n"
    done = scan(tree, REUSABLE.replace(step, extra + step))
    assert done.returncode == 1
    assert "action fixture-org/setup-tool is not in the allowed list" in done.stderr


def test_hosts_of_an_allowed_action_count_for_the_file(tree):
    hosts = HOSTS.replace(
        '"actions/checkout" = []', '"actions/checkout" = ["downloads.example.net"]'
    )
    done = scan(tree, hosts=hosts)
    assert done.returncode == 1
    assert "host downloads.example.net (action actions/checkout) is not declared" in done.stderr


def test_declared_host_behind_an_off_switch_passes(tree):
    done = scan(tree, planted("curl -fsSL https://rekor.sigstore.dev/api/v1/log"), DECLARED)
    assert done.returncode == 0, done.stderr


def test_stale_declaration_fails(tree):
    done = scan(tree, hosts=DECLARED)
    assert done.returncode == 1
    assert "declared host rekor.sigstore.dev is not found in the file any more" in done.stderr


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('input = "publish"', 'input = "path"'),
        ('input = "publish"', 'input = "absent"'),
        ('input = "publish"\n', ""),
    ],
    ids=["input-not-boolean", "input-absent", "no-input"],
)
def test_reusable_workflow_needs_an_off_switch(tree, old, new):
    assert old in DECLARED
    done = scan(
        tree,
        planted("curl -fsSL https://rekor.sigstore.dev/api/v1/log"),
        DECLARED.replace(old, new),
    )
    assert done.returncode == 1
    assert "needs `input` naming a boolean input that defaults to false" in done.stderr


def test_switch_that_defaults_to_on_fails(tree):
    workflow = planted("curl -fsSL https://rekor.sigstore.dev/api/v1/log").replace(
        "        required: false\n        default: false\n",
        "        required: false\n        default: true\n",
    )
    done = scan(tree, workflow, DECLARED)
    assert done.returncode == 1
    assert "needs `input` naming a boolean input that defaults to false" in done.stderr


def test_declaration_without_reason_fails(tree):
    done = scan(
        tree,
        planted("curl -fsSL https://rekor.sigstore.dev/api/v1/log"),
        DECLARED.replace('reason = "public transparency log"', 'reason = ""'),
    )
    assert done.returncode == 1
    assert "declared without a reason" in done.stderr


def test_unused_allowed_action_and_unknown_file_fail(tree):
    hosts = (
        HOSTS
        + '"fixture-org/unused" = []\n\n'
        + '[files.".github/workflows/gone.yml"."example.com"]\nreason = "left over"\n'
    )
    done = scan(tree, hosts=hosts)
    assert done.returncode == 1
    assert "allows fixture-org/unused, which no file uses" in done.stderr
    assert "names .github/workflows/gone.yml, which does not exist" in done.stderr


def test_missing_declaration_is_an_error(tree):
    tree.write({WORKFLOW: REUSABLE})
    assert tree.tool("hosts.py", "check").returncode == 3


MAKE_STEP = REUSABLE.replace('test -d "$TARGET"', "make verify")
MAKEFILE = "verify:\n\tpython3 -m pip install --require-hashes -r requirements.txt\n"


def test_makefile_of_a_file_that_runs_make_is_read(tree):
    done = scan(tree.write({"Makefile": MAKEFILE}), MAKE_STEP)
    assert done.returncode == 1
    why = "host pypi.org (Makefile, command `python3 -m pip install`) is not declared"
    assert why in done.stderr


def test_makefile_is_not_read_for_a_file_that_runs_no_make(tree):
    assert scan(tree.write({"Makefile": MAKEFILE})).returncode == 0


def test_action_used_twice_is_reported_once(tree):
    step = "      - name: Run\n"
    again = f"      - name: Again\n        uses: {CHECKOUT}\n{step}"
    assert step in REUSABLE
    done = scan(tree, REUSABLE.replace(step, again), HOSTS.replace('"actions/checkout" = []\n', ""))
    assert done.returncode == 1
    assert done.stderr.count("action actions/checkout is not in the allowed list") == 1
