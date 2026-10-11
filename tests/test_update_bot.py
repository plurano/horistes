# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Update bot: what `renovate.json` promises holds in the files it reads.

Renovate reads three things here, and each can stop matching without any
error: the command in the header of `requirements/tools.txt`, the comment
above the image line of the licence workflow, and the list of managers that
keeps it away from the files the Praetor engine writes.

`default.json` is the preset other repositories extend. It holds what is the
same in every repository, `renovate.json` extends it and holds the rest, and
no value stands in both. No test asks a forge or starts the bot; a planted
case shows what each check refuses.
"""

from __future__ import annotations

import copy
import json
import re
import shlex

import pytest
from conftest import ROOT

CONFIG = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
PRESET = json.loads((ROOT / "default.json").read_text(encoding="utf-8"))
PLANTED_PRESET = "tests/fixtures/update-bot/planted-preset.json"
# Without a file name Renovate reads `default.json` of the named repository.
REFERENCE = "github>plurano/shared-ci"

# What the preset may hold, and what one of its rules may hold. Allow-lists on
# purpose: a key that names a manager, a file or a package of one repository
# fails here before it reaches every repository that extends the preset.
SHARED = {"$schema", "description", "extends", "minimumReleaseAge", "automerge", "packageRules"}
RULE = {"description", "matchManagers", "matchDepTypes", "enabled"}
# The keys both files may hold: Renovate appends these lists, the repository's
# entries behind the preset's.
APPENDED = {"$schema", "extends", "packageRules"}
TOOLS = "requirements/tools.txt"
LICENCE = ".github/workflows/licence.yml"

# The options Renovate 44 accepts in a `uv pip compile` header (its
# pip-compile manager, `common.ts`); it refuses the update on any other.
# An allow-list on purpose: a new option fails here before it fails there.
PLAIN = {
    "--no-strip-extras",
    "--universal",
    "-v",
    "--generate-hashes",
    "--emit-index-url",
    "--index-url",
    "--all-extras",
}
VALUED = {
    "--constraints",
    "--constraint",
    "--python-version",
    "--no-emit-package",
    "--prerelease",
    "--format",
    "--resolution",
    "--fork-strategy",
    "--exclude-newer",
    "--exclude-newer-package",
    "--group",
    "--override",
    "--overrides",
    "--output-file",
    "--extra",
    "--extra-index-url",
}
HEADER = re.compile(r"^(?:#.*?\r?\n)+# {4}(?P<command>\S*)(?P<arguments> .*?)?\r?\n")


def header_command(text: str) -> list[str]:
    found = HEADER.match(text)
    assert found, "no command in the header"
    return [found["command"], *shlex.split(found["arguments"] or "")]


def refused(argv: list[str]) -> list[str]:
    """The arguments of a header command Renovate would stop on."""
    out = []
    for arg in argv[3:]:
        if not arg.startswith("-"):
            continue
        name, joined, _ = arg.partition("=")
        if name in VALUED and joined:
            continue
        if name in PLAIN and not joined:
            continue
        out.append(arg)
    return out


def outside_the_preset(preset: dict) -> list[str]:
    """The keys of a preset that belong to one repository."""
    out = sorted(set(preset) - SHARED)
    for number, rule in enumerate(preset.get("packageRules", [])):
        out += [f"packageRules[{number}].{key}" for key in sorted(set(rule) - RULE)]
    return out


def said_twice(config: dict, preset: dict) -> list[str]:
    """The keys a repository's own file repeats from the preset it extends."""
    return sorted((set(config) & set(preset)) - APPENDED)


def rules() -> list[dict]:
    return PRESET["packageRules"] + CONFIG["packageRules"]


def python_pattern(pattern: str) -> re.Pattern:
    return re.compile(pattern.replace("(?<", "(?P<"))


def file_patterns(manager: dict) -> list[re.Pattern]:
    return [re.compile(entry.strip("/")) for entry in manager["managerFilePatterns"]]


def image_matches(path: str, text: str) -> list[dict]:
    """What the custom managers of the configuration read in one file."""
    out = []
    for manager in CONFIG["customManagers"]:
        if not any(pattern.search(path) for pattern in file_patterns(manager)):
            continue
        for expression in manager["matchStrings"]:
            out += [found.groupdict() for found in python_pattern(expression).finditer(text)]
    return out


def image_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "docker://" in line.split("#", 1)[0]]


def workflows() -> list[str]:
    found = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    return [str(path.relative_to(ROOT)) for path in found]


def test_only_the_three_managers_run():
    assert CONFIG["enabledManagers"] == ["github-actions", "pip-compile", "custom.regex"]


def test_waiting_time_is_three_days_and_nothing_merges_itself():
    assert PRESET["minimumReleaseAge"] == "3 days"
    assert PRESET["automerge"] is False
    assert all("automerge" not in rule for rule in rules())


def test_commits_of_the_bot_carry_the_sign_off_the_gate_asks_for():
    assert PRESET["extends"] == ["config:recommended", ":gitSignOff"]


def test_this_repository_extends_the_preset_and_nothing_else():
    assert CONFIG["extends"] == [REFERENCE]
    assert (ROOT / "default.json").is_file()


def test_preset_holds_only_what_is_the_same_in_every_repository():
    assert outside_the_preset(PRESET) == []


def test_no_value_stands_in_both_files():
    assert said_twice(CONFIG, PRESET) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("enabledManagers", ["github-actions"]),
        ("pip-compile", {"managerFilePatterns": ["/^requirements/tools\\.txt$/"]}),
        ("customManagers", []),
        ("ignorePaths", [".devcontainer/**"]),
        ("minimumReleaseAgee", "3 days"),
    ],
)
def test_planted_key_of_one_repository_is_refused_in_the_preset(key, value):
    preset = {**PRESET, key: value}
    assert outside_the_preset(preset) == [key]


@pytest.mark.parametrize("key", ["matchFileNames", "matchPackageNames", "automerge"])
def test_planted_rule_for_one_repository_is_refused_in_the_preset(key):
    preset = copy.deepcopy(PRESET)
    preset["packageRules"][0][key] = ["x"]
    assert outside_the_preset(preset) == [f"packageRules[0].{key}"]


def test_planted_preset_file_is_refused():
    planted = json.loads((ROOT / PLANTED_PRESET).read_text(encoding="utf-8"))
    assert outside_the_preset(planted) == ["minimumReleaseAgee"]
    assert planted["automerge"] != PRESET["automerge"]


@pytest.mark.parametrize(
    ("key", "value"),
    [("minimumReleaseAge", "3 days"), ("automerge", False), ("description", ["x"])],
)
def test_planted_repeat_of_a_preset_value_is_refused(key, value):
    assert said_twice({**CONFIG, key: value}, PRESET) == [key]


def test_the_compiled_file_is_the_one_the_bot_is_pointed_at():
    assert any(pattern.search(TOOLS) for pattern in file_patterns(CONFIG["pip-compile"]))
    assert not any(
        pattern.search("requirements/tools.in") for pattern in file_patterns(CONFIG["pip-compile"])
    )


def test_header_of_the_compiled_file_is_one_the_bot_accepts():
    argv = header_command((ROOT / TOOLS).read_text(encoding="utf-8"))
    assert argv[:3] == ["uv", "pip", "compile"]
    assert refused(argv) == []
    assert f"--output-file={TOOLS}" in argv
    assert [arg for arg in argv[3:] if not arg.startswith("-")] == ["requirements/tools.in"]


def test_header_keeps_the_waiting_time_and_the_hashes():
    argv = header_command((ROOT / TOOLS).read_text(encoding="utf-8"))
    assert "--exclude-newer=P3D" in argv
    assert "--generate-hashes" in argv


def test_header_is_the_command_of_the_makefile():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^requirements:\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe
    command = shlex.split(recipe[1].replace("\\\n", " "))
    command.remove("--quiet")
    assert command == header_command((ROOT / TOOLS).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "option",
    [
        "--no-header",
        "--no-annotate",
        "--quiet",
        "-o requirements/tools.txt",
        "--python-version 3.12",
        "--exclude-newer 2026-10-08T00:00:00Z",
        "--generate-hashes=yes",
    ],
)
def test_planted_header_option_is_refused(option):
    text = f"# made by uv:\n#    uv pip compile --universal {option} requirements/tools.in\n"
    assert refused(header_command(text)) == [option.split()[0]]


def test_planted_file_without_header_has_no_command():
    with pytest.raises(AssertionError, match="no command in the header"):
        header_command("attrs==26.1.0 \\\n    --hash=sha256:00\n")


def test_every_image_a_workflow_runs_is_read_by_the_bot():
    lines = 0
    for path in workflows():
        text = (ROOT / path).read_text(encoding="utf-8")
        assert len(image_matches(path, text)) == len(image_lines(text)), path
        lines += len(image_lines(text))
    assert lines == 1


def test_licence_image_is_read_with_its_release_and_its_digest():
    (found,) = image_matches(LICENCE, (ROOT / LICENCE).read_text(encoding="utf-8"))
    assert found["depName"] == "docker.io/fsfe/reuse"
    assert re.fullmatch(r"\d+\.\d+\.\d+", found["currentValue"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", found["currentDigest"])
    assert CONFIG["customManagers"][0]["datasourceTemplate"] == "docker"


def planted_licence(old: str, new: str) -> str:
    text = (ROOT / LICENCE).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return text.replace(old, new)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("        # release 6.2.0\n", ""),
        ("        # release 6.2.0\n", "        # the release is 6.2.0\n"),
        ("docker.io/fsfe/reuse@sha256:", "docker.io/fsfe/reuse:6.2.0@sha256:"),
        ("docker.io/fsfe/reuse@", "docker.io/other/reuse@"),
        ("        uses: docker://", "        env: {}\n        uses: docker://"),
    ],
)
def test_planted_image_line_is_not_read(old, new):
    text = planted_licence(old, new)
    assert len(image_lines(text)) == 1
    assert image_matches(LICENCE, text) == []


def test_planted_image_in_another_workflow_is_not_read():
    text = (ROOT / LICENCE).read_text(encoding="utf-8")
    assert image_matches(".github/workflows/ci.yml", text) == []


@pytest.mark.parametrize("kind", ["docker", "github-runner"])
def test_actions_manager_proposes_no_image_and_no_machine(kind):
    rule = {"matchManagers": ["github-actions"], "matchDepTypes": [kind], "enabled": False}
    kept = [{key: entry[key] for key in rule if key in entry} for entry in rules()]
    assert rule in kept
