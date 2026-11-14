# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""The rendered caller templates carry their licence header and pass reuse lint."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CALLER_IDS, CALLER_TEMPLATES, ROOT, external

sys.path.insert(0, str(ROOT / "tools"))
import render

# The two phrasings a template header uses to name its consumer path.
CONSUMER_PATH = re.compile(
    r"(?:as a consumer keeps it in|repository as) (\.github/workflows/[\w.-]+\.yml)\."
)


def stated_consumer_paths(text: str) -> list[str]:
    header = text.split("*/ -%>", 1)[0]
    return CONSUMER_PATH.findall(" ".join(header.split()))


def map_gaps(templates, mapped) -> tuple[list[str], list[str]]:
    missing = sorted(set(templates) - set(mapped))
    extra = sorted(set(mapped) - set(templates))
    return missing, extra


def test_every_caller_template_is_mapped() -> None:
    templates = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / render.TEMPLATES).glob("*" + render.SUFFIX)
    )
    mapped = sorted(CALLER_TEMPLATES)
    missing, extra = map_gaps(templates, mapped)
    assert (missing, extra) == ([], []), (
        f"Missing caller templates: {missing}; extra caller templates: {extra}"
    )


@pytest.mark.parametrize(
    ("template", "name"),
    sorted(CALLER_TEMPLATES.items()),
    ids=CALLER_IDS,
)
def test_each_template_names_its_consumer_path(template: str, name: str) -> None:
    """The map entry is the consumer path the template's header states."""
    paths = stated_consumer_paths((ROOT / template).read_text(encoding="utf-8"))
    assert paths == [name], f"{template}: stated consumer paths {paths}; expected {[name]}"


@pytest.mark.parametrize(
    ("templates", "mapped", "expected"),
    [
        pytest.param(
            ["templates/callers/x.yml.tmpl"],
            [],
            (["templates/callers/x.yml.tmpl"], []),
            id="unmapped-template",
        ),
        pytest.param(
            [],
            ["templates/callers/x.yml.tmpl"],
            ([], ["templates/callers/x.yml.tmpl"]),
            id="map-entry-without-template",
        ),
        pytest.param(
            ["templates/callers/x.yml.tmpl"],
            ["templates/callers/x.yml.tmpl"],
            ([], []),
            id="complete-map",
        ),
    ],
)
def test_map_gaps(templates, mapped, expected) -> None:
    assert map_gaps(templates, mapped) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            """<%- /*
SPDX-FileCopyrightText: 2026 Example
SPDX-License-Identifier: EUPL-1.2
Caller, as a consumer stores it in .github/workflows/x.yml.
*/ -%>
""",
            [],
            id="changed-consumer-phrasing",
        ),
        pytest.param(
            """<%- /*
SPDX-FileCopyrightText: 2026 Example
SPDX-License-Identifier: EUPL-1.2
Caller of the shared workflow.
*/ -%>
Caller, as a consumer keeps it in .github/workflows/x.yml.
""",
            [],
            id="consumer-sentence-after-header",
        ),
        pytest.param(
            """<%- /*
SPDX-FileCopyrightText: 2026 Example
SPDX-License-Identifier: EUPL-1.2
Caller, as a consumer keeps it in .github/workflows/x.yml.
Copy into your repository as .github/workflows/y.yml.
*/ -%>
""",
            [".github/workflows/x.yml", ".github/workflows/y.yml"],
            id="two-consumer-sentences",
        ),
        pytest.param(
            """<%- /*
SPDX-FileCopyrightText: 2026 Example
SPDX-License-Identifier: EUPL-1.2
Caller of .github/workflows/x.yml of the shared repository.
*/ -%>
""",
            [],
            id="shared-workflow-only",
        ),
        pytest.param(
            """<%- /*
SPDX-FileCopyrightText: 2026 Example
SPDX-License-Identifier: EUPL-1.2
    Caller, as a consumer keeps it in
        .github/workflows/x.yml.
*/ -%>
""",
            [".github/workflows/x.yml"],
            id="wrapped-indented-consumer-sentence",
        ),
    ],
)
def test_stated_consumer_paths(text: str, expected: list[str]) -> None:
    assert stated_consumer_paths(text) == expected


def lint(root: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the licence tool in `root`; a tool path relative to the repository stays valid there."""
    tool, *options = external("REUSE", "reuse")
    if "/" in tool:
        tool = str((ROOT / tool).resolve())
    command = [tool, *options, *args, "lint", "--lines"]
    env = {**os.environ, "LC_ALL": "C"}
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, env=env)


@pytest.mark.parametrize(
    ("template", "name"),
    sorted(CALLER_TEMPLATES.items()),
    ids=CALLER_IDS,
)
def test_rendered_caller_passes_reuse_lint(tmp_path: Path, template: str, name: str) -> None:
    source = (ROOT / template).read_text(encoding="utf-8")
    rendered = render.render(source, render.SAMPLE, template)
    workflow = tmp_path / name
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(rendered, encoding="utf-8")

    identifier = render.SAMPLE["Licence"]["Identifier"]
    licenses = tmp_path / "LICENSES"
    licenses.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "LICENSES" / f"{identifier}.txt", licenses / f"{identifier}.txt")

    done = lint(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr

    lines = rendered.splitlines(keepends=True)
    unlicensed = "".join(lines[2:])
    workflow.write_text(unlicensed, encoding="utf-8")
    planted = lint(tmp_path)
    assert planted.returncode == 1, planted.stdout + planted.stderr
    assert name in planted.stdout
