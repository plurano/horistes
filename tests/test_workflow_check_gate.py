# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Workflow-check gate: reusable workflow structure, pinned tools, callers render correctly."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import yaml
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf
import render

WORKFLOW = ROOT / ".github/workflows/workflow-check.yml"
TEMPLATE = ROOT / "templates/callers/workflow-check.yml.tmpl"


def test_workflow_permissions_and_shape() -> None:
    doc = ciwf.load(WORKFLOW)
    assert doc.get("permissions") == {}
    assert doc["jobs"]["check"]["permissions"] == {"contents": "read"}
    assert doc["jobs"]["check"]["timeout-minutes"] == 10
    assert doc["jobs"]["check"]["name"] == "Workflow check"


def test_workflow_actionlint_pins_match_ci() -> None:
    doc = ciwf.load(WORKFLOW)
    ci_doc = ciwf.load(ROOT / ".github/workflows/ci.yml")
    assert doc["env"]["ACTIONLINT_VERSION"] == ci_doc["env"]["ACTIONLINT_VERSION"]
    assert doc["env"]["ACTIONLINT_SHA256"] == ci_doc["env"]["ACTIONLINT_SHA256"]


def test_workflow_steps_guarded_after_draft_stop() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["check"]["steps"]
    assert steps[0]["name"] == "Stop on a draft pull request"
    assert steps[0]["if"] == "github.event.pull_request.draft == true"
    for step in steps[1:]:
        assert step.get("if") == "github.event.pull_request.draft != true"


def test_workflow_tools_checkout_parameters() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["check"]["steps"]
    checkout = [s for s in steps if s.get("name") == "Check out the check tools"]
    assert len(checkout) == 1
    with_block = checkout[0]["with"]
    assert with_block["repository"] == "${{ job.workflow_repository }}"
    assert with_block["ref"] == "${{ job.workflow_sha }}"
    assert with_block["path"] == "workflow-check-source"
    assert with_block["persist-credentials"] is False
    assert with_block["sparse-checkout-cone-mode"] is False

    sparse_paths = [p.strip() for p in with_block["sparse-checkout"].splitlines() if p.strip()]
    assert "tools/pins.py" in sparse_paths
    assert "tools/ciwf.py" in sparse_paths
    assert "tools/forge.py" in sparse_paths
    assert "tools/results.py" in sparse_paths
    assert "requirements/tools.txt" in sparse_paths
    for rel in sparse_paths:
        assert (ROOT / rel).is_file(), f"sparse-checkout path {rel} must exist"


def _sparse_paths() -> list[str]:
    steps = ciwf.load(WORKFLOW)["jobs"]["check"]["steps"]
    checkout = next(s for s in steps if s.get("name") == "Check out the check tools")
    return [p.strip() for p in checkout["with"]["sparse-checkout"].splitlines() if p.strip()]


def _local_imports_missing(sparse: list[str]) -> set[str]:
    """Modules of tools/ that pins.py needs, directly or through each other, outside `sparse`."""
    seen: set[str] = set()
    todo = ["pins"]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        tree = ast.parse((ROOT / "tools" / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            heads = [n.split(".")[0] for n in names]
            todo += [n for n in heads if (ROOT / "tools" / f"{n}.py").is_file()]
    return {f"tools/{n}.py" for n in seen} - set(sparse)


def test_sparse_checkout_holds_every_module_pins_needs() -> None:
    assert _local_imports_missing(_sparse_paths()) == set()


def test_planted_dropped_module_is_found() -> None:
    planted = [p for p in _sparse_paths() if p != "tools/forge.py"]
    assert _local_imports_missing(planted) == {"tools/forge.py"}


def test_workflow_caller_checkout_parameters() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["check"]["steps"]
    checkout = [s for s in steps if s.get("name") == "Check out the caller"]
    assert len(checkout) == 1
    assert checkout[0]["with"]["persist-credentials"] is False


def _picker() -> str:
    steps = ciwf.load(WORKFLOW)["jobs"]["check"]["steps"]
    install = [s for s in steps if s.get("name") == "Install the pinned zizmor and PyYAML"]
    assert len(install) == 1
    script = install[0]["run"]
    assert "--require-hashes" in script
    return script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def _pick(tmp_path: Path, requirements: str) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "tools.txt"
    source.write_text(requirements, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-I", "-", str(source), str(tmp_path / "picked.txt")],
        input=_picker(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_picks_the_hashed_zizmor_and_pyyaml_entries(tmp_path: Path) -> None:
    result = _pick(tmp_path, (ROOT / "requirements/tools.txt").read_text(encoding="utf-8"))
    assert result.returncode == 0, result.stderr
    picked = (tmp_path / "picked.txt").read_text(encoding="utf-8")
    heads = [line for line in picked.splitlines() if not line.startswith((" ", "#"))]
    assert sorted(h.split("==")[0] for h in heads) == ["pyyaml", "zizmor"]
    assert picked.count("--hash=sha256:") >= 2


def test_planted_missing_pyyaml_entry_fails(tmp_path: Path) -> None:
    text = (ROOT / "requirements/tools.txt").read_text(encoding="utf-8")
    planted = text.replace("\npyyaml==", "\nnot-pyyaml==")
    result = _pick(tmp_path, planted)
    assert result.returncode != 0
    assert "pyyaml" in result.stderr


def test_workflow_actionlint_installed_and_run() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["check"]["steps"]
    install = [s for s in steps if s.get("name") == "Install the pinned actionlint"]
    assert len(install) == 1
    assert "${ACTIONLINT_SHA256}" in install[0]["run"]
    assert "sha256sum --check --strict" in install[0]["run"]

    lint_step = [s for s in steps if s.get("name") == "Run actionlint on caller workflows"]
    assert len(lint_step) == 1
    assert "$RUNNER_TEMP/bin/actionlint" in lint_step[0]["run"]


def test_workflow_pins_check_invocation() -> None:
    doc = ciwf.load(WORKFLOW)
    steps = doc["jobs"]["check"]["steps"]
    check_step = [s for s in steps if s.get("name") == "Check action pins and comments"]
    assert len(check_step) == 1
    step = check_step[0]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["ZIZMOR"] == "${{ runner.temp }}/bin/zizmor"
    command = '"$RUNNER_TEMP/venv/bin/python" -I workflow-check-source/tools/pins.py check --root .'
    assert command in step["run"]


def test_caller_template_renders_and_reports_required_name() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, str(TEMPLATE))
    problems = render.caller_problems(rendered, ROOT)
    assert problems == []

    doc = yaml.load(rendered, Loader=ciwf.StrictLoader)
    assert doc["name"] == "Workflow check"
    assert "result" in doc["jobs"]
    assert doc["jobs"]["result"]["name"] == "Workflow check"
    assert doc["jobs"]["result"]["needs"] == "workflow-check"
    assert doc["jobs"]["workflow-check"]["name"] == "Workflow check"


def test_caller_template_planted_missing_result_job_fails() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, str(TEMPLATE))
    doc = yaml.load(rendered, Loader=ciwf.StrictLoader)
    del doc["jobs"]["result"]
    mutilated = yaml.dump(doc)
    problems = render.caller_problems(mutilated, ROOT)
    assert any("result" in p for p in problems)
