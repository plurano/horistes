# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Update bot: what `renovate.json` promises holds in the files it reads.

Renovate reads three things here, and each can stop matching without any
error: the command in the header of `requirements/tools.txt`, the comment
above the image line of the licence workflow, and the list of managers that
keeps it away from the files the Praetor engine writes.

`default.json` is the preset other repositories extend. It holds what is the
same in every repository, `renovate.json` extends it and holds the rest, and
no value stands in both. `settings-sync.json` is a second preset, for a
repository whose toolchain file is held equal to a source elsewhere: it
switches the managers of such files off and holds nothing else. No test asks a
forge or starts the bot; a planted case shows what each check refuses.
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
SYNCED = json.loads((ROOT / "settings-sync.json").read_text(encoding="utf-8"))
PLANTED_SYNCED = "tests/fixtures/update-bot/planted-settings-sync.json"
# Without a file name Renovate reads `default.json` of the named repository.
REFERENCE = "github>plurano/horistes"

# What the preset may hold, and what one of its rules may hold. Allow-lists on
# purpose: a key that names a manager, a file or a package of one repository
# fails here before it reaches every repository that extends the preset.
SHARED = {
    "$schema",
    "description",
    "extends",
    "minimumReleaseAge",
    "osvVulnerabilityAlerts",
    "vulnerabilityAlerts",
    "automerge",
    "gitIgnoredAuthors",
    "postUpdateOptions",
    "packageRules",
}
# The commit address of the merge-train app's bot user (numeric user id, then the
# app slug), read from GET /users/plurano-merge-train[bot]. Renovate compares an
# entry for equality before it reads it as a glob, so the brackets stay literal.
TRAIN_BOT = "342223998+plurano-merge-train[bot]@users.noreply.github.com"
RULE = {
    "description",
    "matchManagers",
    "matchDepTypes",
    "enabled",
    "matchDatasources",
    "matchPackageNames",
    "minimumReleaseAge",
}
# The fleet organisations whose own releases wait for no release age.
ORGS = (
    "lusoris",
    "cordanallm",
    "golusoris",
    "vmafx",
    "tekmora",
    "plurano",
    "vidiko",
    "jellysin",
    "goph-arr",
)
# Forks of outside projects hosted in those organisations that keep the
# three-day waiting time.
FORKS = (
    "lusoris/llama-swap",
    "lusoris/controller-tools",
    "lusoris/hindsight",
    "lusoris/Vulkan-ValidationLayers",
    "lusoris/helm-charts",
    "lusoris/paperclip",
    "lusoris/model_server",
    "lusoris/paperclip-aperture",
    "VMAFx/netflix-vmaf-contributions",
)
# A fix of a known vulnerability waits for no release age (ADR-0050) and carries
# the label the merge train serves first (ADR-0060); the block holds nothing else.
SECURITY_LABEL = "security"
# Renovate's default (`auto`) rebases only a conflicted branch unless it automerges
# or reads a required up-to-date branch; a linear-history ruleset is neither, and the
# train never updates there (ADR-0044), so the fix would wait for a person.
REBASE = "behind-base-branch"
VULNERABILITY = {"minimumReleaseAge": None, "addLabels": [SECURITY_LABEL], "rebaseWhen": REBASE}
# Renovate 44 runs `go mod tidy` after a gomod update only with this option
# (lib/modules/manager/gomod/artifacts.ts); without it a go.sum misses the hash
# of a newly needed module.
TIDY = "gomodTidy"
# The keys both files may hold: Renovate appends these lists, the repository's
# entries behind the preset's.
APPENDED = {"$schema", "extends", "packageRules"}
# The settings-sync preset: the managers it switches off, and the one block
# such a manager may hold. Allow-lists again: a misspelt manager is an unknown
# key here, and a block that does more than switch off is refused.
HELD = {"rust-toolchain"}
SWITCH = {"enabled": False}
TOOLS = "requirements/tools.txt"
LICENCE = ".github/workflows/licence.yml"
GATE = ".github/workflows/ci.yml"
SECRET_SCAN = ".github/workflows/secret-scan.yml"
WORKFLOW_CHECK = ".github/workflows/workflow-check.yml"
MAKEFILE = "Makefile"
ACTIONLINT_SHA = "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8"
GITLEAKS_SHA = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"

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
    alerts = preset.get("vulnerabilityAlerts", {})
    out += [f"vulnerabilityAlerts.{key}" for key in sorted(set(alerts) - set(VULNERABILITY))]
    return out


def held_vulnerability_fix(preset: dict) -> list[str]:
    """What keeps a fix of a known vulnerability from arriving before the waiting time."""
    out = [] if preset.get("osvVulnerabilityAlerts") is True else ["osvVulnerabilityAlerts"]
    alerts = preset.get("vulnerabilityAlerts", {})
    if "minimumReleaseAge" not in alerts or alerts["minimumReleaseAge"] is not None:
        out.append("vulnerabilityAlerts.minimumReleaseAge")
    return out


def unlabelled_vulnerability_fix(preset: dict) -> list[str]:
    """What keeps a fix of a known vulnerability from carrying the security label."""
    labels = preset.get("vulnerabilityAlerts", {}).get("addLabels", [])
    return [] if SECURITY_LABEL in labels else ["vulnerabilityAlerts.addLabels"]


def stale_vulnerability_fix(preset: dict) -> list[str]:
    """What leaves a fix of a known vulnerability behind its base branch."""
    rebase = preset.get("vulnerabilityAlerts", {}).get("rebaseWhen")
    return [] if rebase == REBASE else ["vulnerabilityAlerts.rebaseWhen"]


def untidy_go_update(preset: dict) -> list[str]:
    """What leaves a Go module update without running go mod tidy."""
    options = preset.get("postUpdateOptions")
    if not isinstance(options, list) or TIDY not in options:
        return ["postUpdateOptions"]
    return []


def said_twice(config: dict, preset: dict) -> list[str]:
    """The keys a repository's own file repeats from the preset it extends."""
    return sorted((set(config) & set(preset)) - APPENDED)


def outside_settings_sync(preset: dict) -> list[str]:
    """What a settings-sync preset holds beyond switching a listed manager off."""
    unknown = sorted(set(preset) - HELD - {"$schema", "description"})
    return unknown + sorted(f"{key} block" for key in HELD & set(preset) if preset[key] != SWITCH)


def pattern_matches(pattern: str, package: str) -> bool:
    """True when a package name matches a Renovate pattern."""
    if pattern.startswith("/") and pattern.endswith("/i"):
        return bool(re.search(pattern[1:-2], package, re.IGNORECASE))
    if pattern.startswith("/") and pattern.endswith("/"):
        return bool(re.search(pattern[1:-1], package))
    return pattern.lower() == package.lower()


def release_age(preset: dict, datasource: str, package: str) -> str | None:
    """Emulate Renovate packageRules matching for minimumReleaseAge."""
    current_age = preset.get("minimumReleaseAge")
    for rule in preset.get("packageRules", [])[:50]:
        if "enabled" in rule:
            continue
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        if "matchPackageNames" in rule and not any(
            pattern_matches(pattern, package) for pattern in rule["matchPackageNames"][:50]
        ):
            continue
        if "minimumReleaseAge" in rule:
            current_age = rule["minimumReleaseAge"]
    return current_age


def held_forks(preset: dict) -> list[str]:
    """Forks of outside projects that are not held for three days."""
    problems = []
    for fork in FORKS[:20]:
        for datasource, package in (
            ("github-tags", fork),
            ("go", f"github.com/{fork}"),
            ("docker", f"ghcr.io/{fork}"),
        ):
            if release_age(preset, datasource, package) != "3 days":
                problems.append(f"{datasource}:{package}")
    return problems


def own_pattern_problems(preset: dict) -> list[str]:
    """Outside packages proposed early because an own rule matches too broadly."""
    outside = (
        ("github-tags", "notplurano/repo"),
        ("github-tags", "evilplurano/x"),
        ("github-tags", "lusoris-x/repo"),
        ("go", "github.com/notplurano/repo"),
        ("docker", "ghcr.io/notplurano/repo"),
        ("npm", "@notplurano/repo"),
    )
    return [
        f"{datasource}:{package}"
        for datasource, package in outside[:20]
        if release_age(preset, datasource, package) is None
    ]


def rules() -> list[dict]:
    return PRESET["packageRules"] + CONFIG["packageRules"]


def python_pattern(pattern: str) -> re.Pattern:
    return re.compile(pattern.replace("(?<", "(?P<"))


def file_patterns(manager: dict) -> list[re.Pattern]:
    return [re.compile(entry.strip("/")) for entry in manager["managerFilePatterns"]]


def image_matches(path: str, text: str) -> list[dict]:
    """What the custom image managers of the configuration read in one file."""
    out = []
    for manager in CONFIG["customManagers"]:
        if manager.get("datasourceTemplate") != "docker":
            continue
        if not any(pattern.search(path) for pattern in file_patterns(manager)):
            continue
        for expression in manager["matchStrings"]:
            out += [found.groupdict() for found in python_pattern(expression).finditer(text)]
    return out


def tool_managers() -> list[dict]:
    """Custom managers configuring github-release-attachments tool releases."""
    return [
        manager
        for manager in CONFIG["customManagers"]
        if manager.get("datasourceTemplate") == "github-release-attachments"
    ]


def tool_matches(path: str, text: str) -> list[tuple[dict, dict]]:
    """What the custom tool managers read in one file."""
    out = []
    for manager in tool_managers():
        if not any(pattern.search(path) for pattern in file_patterns(manager)):
            continue
        for expression in manager["matchStrings"]:
            out += [
                (manager, found.groupdict()) for found in python_pattern(expression).finditer(text)
            ]
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


def test_a_vulnerability_fix_skips_the_waiting_time():
    assert held_vulnerability_fix(PRESET) == []
    assert PRESET["vulnerabilityAlerts"] == VULNERABILITY
    assert PRESET["minimumReleaseAge"] == "3 days"


@pytest.mark.parametrize(
    ("change", "held"),
    [
        ({"osvVulnerabilityAlerts": False}, ["osvVulnerabilityAlerts"]),
        (
            {"vulnerabilityAlerts": {"minimumReleaseAge": "3 days"}},
            ["vulnerabilityAlerts.minimumReleaseAge"],
        ),
        ({"vulnerabilityAlerts": {}}, ["vulnerabilityAlerts.minimumReleaseAge"]),
    ],
)
def test_planted_vulnerability_fix_held_by_the_waiting_time_is_refused(change, held):
    assert held_vulnerability_fix({**PRESET, **change}) == held


def test_a_vulnerability_fix_carries_the_security_label():
    assert unlabelled_vulnerability_fix(PRESET) == []


@pytest.mark.parametrize(
    "alerts",
    [{"minimumReleaseAge": None}, {"minimumReleaseAge": None, "addLabels": ["dependencies"]}],
    ids=["no-labels", "other-label"],
)
def test_planted_vulnerability_fix_without_the_security_label_is_refused(alerts):
    preset = {**PRESET, "vulnerabilityAlerts": alerts}
    assert unlabelled_vulnerability_fix(preset) == ["vulnerabilityAlerts.addLabels"]


def test_a_vulnerability_fix_is_rebased_when_behind():
    assert stale_vulnerability_fix(PRESET) == []


@pytest.mark.parametrize("rebase", [None, "auto", "conflicted"], ids=["unset", "auto", "conflict"])
def test_planted_vulnerability_fix_left_behind_is_refused(rebase):
    alerts = {k: v for k, v in PRESET["vulnerabilityAlerts"].items() if k != "rebaseWhen"}
    if rebase:
        alerts["rebaseWhen"] = rebase
    preset = {**PRESET, "vulnerabilityAlerts": alerts}
    assert stale_vulnerability_fix(preset) == ["vulnerabilityAlerts.rebaseWhen"]


def test_planted_preset_without_the_vulnerability_keys_is_refused():
    preset = {key: value for key, value in PRESET.items() if "ulnerabilityAlerts" not in key}
    assert held_vulnerability_fix(preset) == [
        "osvVulnerabilityAlerts",
        "vulnerabilityAlerts.minimumReleaseAge",
    ]


def test_planted_extra_key_in_the_vulnerability_block_is_refused():
    preset = copy.deepcopy(PRESET)
    preset["vulnerabilityAlerts"]["automerge"] = True
    assert outside_the_preset(preset) == ["vulnerabilityAlerts.automerge"]


def test_commits_of_the_bot_carry_the_sign_off_the_gate_asks_for():
    assert PRESET["extends"] == ["config:recommended", ":gitSignOff"]


def test_generated_file_commits_of_the_train_do_not_mark_an_update_as_edited():
    assert PRESET["gitIgnoredAuthors"] == [TRAIN_BOT]


def test_a_go_module_update_is_tidied():
    assert untidy_go_update(PRESET) == []
    assert PRESET["postUpdateOptions"] == [TIDY]


@pytest.mark.parametrize(
    "preset",
    [
        {key: value for key, value in PRESET.items() if key != "postUpdateOptions"},
        {**PRESET, "postUpdateOptions": []},
        {**PRESET, "postUpdateOptions": ["gomodVendor"]},
        {**PRESET, "postUpdateOptions": TIDY},
    ],
)
def test_planted_untidy_go_update_is_refused(preset):
    assert untidy_go_update(preset) == ["postUpdateOptions"]


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
        ("rust-toolchain", SWITCH),
    ],
)
def test_planted_key_of_one_repository_is_refused_in_the_preset(key, value):
    preset = {**PRESET, key: value}
    assert outside_the_preset(preset) == [key]


@pytest.mark.parametrize("key", ["matchFileNames", "matchDepNames", "automerge"])
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


def test_settings_sync_preset_switches_the_toolchain_manager_off_and_nothing_else():
    assert outside_settings_sync(SYNCED) == []
    assert {key: SYNCED[key] for key in HELD} == {"rust-toolchain": SWITCH}
    assert all(isinstance(line, str) and line for line in SYNCED["description"])


def test_no_preset_and_no_file_here_extends_the_settings_sync_preset():
    assert "extends" not in SYNCED
    assert not [
        entry for entry in PRESET["extends"] + CONFIG["extends"] if "settings-sync" in entry
    ]


@pytest.mark.parametrize(
    ("key", "value", "found"),
    [
        ("rust-toolchainn", SWITCH, ["rust-toolchainn"]),
        ("cargo", SWITCH, ["cargo"]),
        ("enabledManagers", ["cargo"], ["enabledManagers"]),
        ("extends", [REFERENCE], ["extends"]),
        (
            "packageRules",
            [{"matchManagers": ["rust-toolchain"], "enabled": False}],
            ["packageRules"],
        ),
        ("rust-toolchain", {"enabled": True}, ["rust-toolchain block"]),
        ("rust-toolchain", {}, ["rust-toolchain block"]),
        (
            "rust-toolchain",
            {**SWITCH, "managerFilePatterns": ["/^rust-toolchain$/"]},
            ["rust-toolchain block"],
        ),
    ],
)
def test_planted_change_of_the_settings_sync_preset_is_refused(key, value, found):
    assert outside_settings_sync({**SYNCED, key: value}) == found


def test_planted_settings_sync_file_is_refused():
    planted = json.loads((ROOT / PLANTED_SYNCED).read_text(encoding="utf-8"))
    assert outside_settings_sync(planted) == ["rust-toolchainn"]
    assert not HELD & set(planted)


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


def test_first_custom_manager_is_the_licence_manager():
    first = CONFIG["customManagers"][0]
    assert first["description"].startswith("The licence tool image")
    assert first["datasourceTemplate"] == "docker"
    assert first["managerFilePatterns"] == ["/^\\.github/workflows/licence\\.yml$/"]


def test_custom_tool_managers_declare_github_release_attachments():
    managers = tool_managers()
    assert len(managers) == 3
    for manager in managers:
        assert manager["datasourceTemplate"] == "github-release-attachments"
        assert manager["depNameTemplate"] in {"rhysd/actionlint", "gitleaks/gitleaks"}


def test_a_checksum_pin_reads_the_release_tag_itself():
    """The datasource fetches releases/tags/<currentValue> to map the checksum.

    A version stripped of its v by extractVersion names no release there, and
    the bot then proposes the version with no checksum.
    """
    for manager in tool_managers():
        if "(?<currentDigest>" in manager["matchStrings"][0]:
            assert "extractVersionTemplate" not in manager
            assert "(?<currentValue>v" in manager["matchStrings"][0]
        else:
            assert manager["extractVersionTemplate"] == "^v(?<version>.+)$"


def test_actionlint_is_read_with_release_and_checksum_in_gate_and_makefile():
    gate_text = (ROOT / GATE).read_text(encoding="utf-8")
    make_text = (ROOT / MAKEFILE).read_text(encoding="utf-8")

    ci_matches = [
        groups
        for mgr, groups in tool_matches(GATE, gate_text)
        if mgr["depNameTemplate"] == "rhysd/actionlint"
    ]
    assert len(ci_matches) == 1
    ci_actionlint = ci_matches[0]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ci_actionlint["currentValue"])
    assert re.fullmatch(r"[0-9a-f]{64}", ci_actionlint["currentDigest"])

    make_matches = [
        groups
        for mgr, groups in tool_matches(MAKEFILE, make_text)
        if mgr["depNameTemplate"] == "rhysd/actionlint"
    ]
    assert len(make_matches) == 1
    make_actionlint = make_matches[0]
    assert re.fullmatch(r"\d+\.\d+\.\d+", make_actionlint["currentValue"])

    assert ci_actionlint["currentValue"] == "v" + make_actionlint["currentValue"]


def test_gitleaks_is_read_with_release_and_checksum_in_gate():
    gate_text = (ROOT / GATE).read_text(encoding="utf-8")
    ci_matches = [
        groups
        for mgr, groups in tool_matches(GATE, gate_text)
        if mgr["depNameTemplate"] == "gitleaks/gitleaks"
    ]
    assert len(ci_matches) == 1
    ci_gitleaks = ci_matches[0]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ci_gitleaks["currentValue"])
    assert re.fullmatch(r"[0-9a-f]{64}", ci_gitleaks["currentDigest"])


def test_gitleaks_is_read_with_release_and_checksum_in_secret_scan():
    text = (ROOT / SECRET_SCAN).read_text(encoding="utf-8")
    matches = [
        groups
        for mgr, groups in tool_matches(SECRET_SCAN, text)
        if mgr["depNameTemplate"] == "gitleaks/gitleaks"
    ]
    assert len(matches) == 1
    gitleaks = matches[0]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", gitleaks["currentValue"])
    assert re.fullmatch(r"[0-9a-f]{64}", gitleaks["currentDigest"])


def test_actionlint_is_read_with_release_and_checksum_in_workflow_check():
    text = (ROOT / WORKFLOW_CHECK).read_text(encoding="utf-8")
    matches = [
        groups
        for mgr, groups in tool_matches(WORKFLOW_CHECK, text)
        if mgr["depNameTemplate"] == "rhysd/actionlint"
    ]
    assert len(matches) == 1
    actionlint = matches[0]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", actionlint["currentValue"])
    assert re.fullmatch(r"[0-9a-f]{64}", actionlint["currentDigest"])


@pytest.mark.parametrize(
    ("dep_name", "path", "new_version"),
    [
        ("rhysd/actionlint", GATE, "v1.7.13"),
        ("rhysd/actionlint", WORKFLOW_CHECK, "v1.7.13"),
        ("rhysd/actionlint", MAKEFILE, "1.7.13"),
        ("gitleaks/gitleaks", GATE, "v8.30.2"),
        ("gitleaks/gitleaks", SECRET_SCAN, "v8.30.2"),
    ],
)
def test_auto_replace_string_template_renders_and_rematches(dep_name, path, new_version):
    managers = [
        m
        for m in tool_managers()
        if m["depNameTemplate"] == dep_name
        and any(pattern.search(path) for pattern in file_patterns(m))
    ]
    assert len(managers) == 1
    manager = managers[0]
    template = manager.get("autoReplaceStringTemplate")
    assert template, f"missing autoReplaceStringTemplate for {dep_name} in {path}"

    rendered = template.replace(
        "{{{replace '^v' '' newValue}}}", new_version.removeprefix("v")
    ).replace("{{{newValue}}}", new_version)
    assert "{{" not in rendered.replace("{{{newDigest}}}", "")
    fake_digest = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    if "{{{newDigest}}}" in template:
        rendered = rendered.replace("{{{newDigest}}}", fake_digest)

    pattern = python_pattern(manager["matchStrings"][0])
    matches = list(pattern.finditer(rendered))
    assert len(matches) == 1
    matched_groups = matches[0].groupdict()
    assert matched_groups["currentValue"] == new_version
    if "{{{newDigest}}}" in template:
        assert matched_groups["currentDigest"] == fake_digest

    original_text = (ROOT / path).read_text(encoding="utf-8")
    (orig_match,) = pattern.finditer(original_text)
    old_version = orig_match.group("currentValue")
    assert old_version not in rendered


def planted_gate(old: str, new: str) -> str:
    text = (ROOT / GATE).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return text.replace(old, new)


def planted_makefile(old: str, new: str) -> str:
    text = (ROOT / MAKEFILE).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return text.replace(old, new)


@pytest.mark.parametrize(
    ("old", "new", "dep_name"),
    [
        (
            (
                "  # SHA-256 of actionlint_1.7.12_linux_amd64.tar.gz, "
                "from the release's checksum file.\n"
            ),
            "  # checksum of actionlint_1.7.12_linux_amd64.tar.gz\n",
            "rhysd/actionlint",
        ),
        (
            (
                "  # SHA-256 of actionlint_1.7.12_linux_amd64.tar.gz, "
                "from the release's checksum file.\n"
            ),
            "",
            "rhysd/actionlint",
        ),
        (
            (
                "  # SHA-256 of gitleaks_8.30.1_linux_x64.tar.gz, "
                "from the release's checksum file.\n"
            ),
            "  # checksum of gitleaks_8.30.1_linux_x64.tar.gz\n",
            "gitleaks/gitleaks",
        ),
        (
            (
                "  # SHA-256 of gitleaks_8.30.1_linux_x64.tar.gz, "
                "from the release's checksum file.\n"
            ),
            "",
            "gitleaks/gitleaks",
        ),
        (
            (
                '  ACTIONLINT_VERSION: "v1.7.12"\n'
                "  # SHA-256 of actionlint_1.7.12_linux_amd64.tar.gz, "
                "from the release's checksum file.\n"
                f'  ACTIONLINT_SHA256: "{ACTIONLINT_SHA}"\n'
            ),
            (
                '  ACTIONLINT_VERSION: "v1.7.12"\n'
                "  # SHA-256 of actionlint_1.7.12_linux_amd64.tar.gz, "
                "from the release's checksum file.\n"
                '  OTHER_VAR: "value"\n'
                f'  ACTIONLINT_SHA256: "{ACTIONLINT_SHA}"\n'
            ),
            "rhysd/actionlint",
        ),
        (
            (
                '  GITLEAKS_VERSION: "v8.30.1"\n'
                "  # SHA-256 of gitleaks_8.30.1_linux_x64.tar.gz, "
                "from the release's checksum file.\n"
                f'  GITLEAKS_SHA256: "{GITLEAKS_SHA}"\n'
            ),
            (
                '  GITLEAKS_VERSION: "v8.30.1"\n'
                "  # SHA-256 of gitleaks_8.30.1_linux_x64.tar.gz, "
                "from the release's checksum file.\n"
                '  OTHER_VAR: "value"\n'
                f'  GITLEAKS_SHA256: "{GITLEAKS_SHA}"\n'
            ),
            "gitleaks/gitleaks",
        ),
        (
            '  ACTIONLINT_VERSION: "v1.7.12"\n',
            '  ACTIONLINT_VERSION: "1.7.12"\n',
            "rhysd/actionlint",
        ),
        ('  GITLEAKS_VERSION: "v8.30.1"\n', '  GITLEAKS_VERSION: "8.30.1"\n', "gitleaks/gitleaks"),
    ],
)
def test_planted_ci_tool_pin_is_not_read(old, new, dep_name):
    text = planted_gate(old, new)
    matches = [m for mgr, m in tool_matches(GATE, text) if mgr["depNameTemplate"] == dep_name]
    assert matches == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("ACTIONLINT_VERSION := 1.7.12\n", "ACTIONLINT_VER := 1.7.12\n"),
        ("ACTIONLINT_VERSION := 1.7.12\n", "ACTIONLINT_VERSION = 1.7.12\n"),
        ("ACTIONLINT_VERSION := 1.7.12\n", ""),
    ],
)
def test_planted_makefile_tool_pin_is_not_read(old, new):
    text = planted_makefile(old, new)
    matches = [
        m for mgr, m in tool_matches(MAKEFILE, text) if mgr["depNameTemplate"] == "rhysd/actionlint"
    ]
    assert matches == []


@pytest.mark.parametrize("org", ORGS)
@pytest.mark.parametrize(
    ("datasource", "template"),
    [
        ("github-tags", "{org}/repo"),
        ("go", "github.com/{org}/repo/v2"),
        ("docker", "ghcr.io/{org}/img"),
        ("npm", "@{org}/pkg"),
    ],
)
def test_own_releases_skip_the_waiting_time(org, datasource, template):
    package = template.format(org=org)
    assert release_age(PRESET, datasource, package) is None


@pytest.mark.parametrize(
    ("datasource", "package"),
    [
        ("github-tags", "actions/checkout"),
        ("go", "golang.org/x/net"),
        ("docker", "docker.io/library/python"),
        ("npm", "@types/node"),
        ("github-tags", "lusoris-x/repo"),
        ("github-tags", "notplurano/repo"),
        ("go", "github.com/plurano"),
        ("docker", "ghcr.io/plurano"),
    ],
)
def test_outside_releases_keep_the_waiting_time(datasource, package):
    assert release_age(PRESET, datasource, package) == "3 days"


@pytest.mark.parametrize("fork", FORKS)
@pytest.mark.parametrize(
    ("datasource", "template"),
    [
        ("github-tags", "{fork}"),
        ("go", "github.com/{fork}"),
        ("docker", "ghcr.io/{fork}"),
    ],
)
def test_forks_of_outside_projects_keep_the_waiting_time(fork, datasource, template):
    package = template.format(fork=fork)
    assert release_age(PRESET, datasource, package) == "3 days"


@pytest.mark.parametrize(
    ("datasource", "package"),
    [
        ("github-tags", "LUSORIS/Llama-Swap"),
        ("go", "github.com/LUSORIS/Llama-Swap"),
        ("docker", "ghcr.io/LUSORIS/Llama-Swap"),
    ],
)
def test_mixed_case_forks_keep_the_waiting_time(datasource, package):
    assert release_age(PRESET, datasource, package) == "3 days"


def test_forks_are_held_in_preset():
    assert held_forks(PRESET) == []
    assert own_pattern_problems(PRESET) == []


def test_fork_rule_is_the_last_rule_and_holds_three_days():
    last_rule = PRESET["packageRules"][-1]
    assert last_rule["minimumReleaseAge"] == "3 days"
    assert "matchDatasources" not in last_rule
    assert any("llama-swap" in pattern for pattern in last_rule.get("matchPackageNames", []))


def test_planted_fork_rule_before_own_rules_leaves_fork_unheld():
    planted = copy.deepcopy(PRESET)
    fork_rule = planted["packageRules"].pop()
    planted["packageRules"].insert(1, fork_rule)
    problems = held_forks(planted)
    assert problems != []
    assert "github-tags:lusoris/llama-swap" in problems
    assert release_age(planted, "github-tags", "lusoris/llama-swap") is None


def test_planted_unanchored_own_rule_is_refused():
    planted = copy.deepcopy(PRESET)
    patterns = planted["packageRules"][1]["matchPackageNames"]
    planted["packageRules"][1]["matchPackageNames"] = [p.replace("/^", "/") for p in patterns]
    problems = own_pattern_problems(planted)
    assert "github-tags:evilplurano/x" in problems
    assert release_age(planted, "github-tags", "evilplurano/x") is None


def test_planted_missing_fork_in_fork_rule_is_refused():
    planted = copy.deepcopy(PRESET)
    patterns = planted["packageRules"][-1]["matchPackageNames"]
    planted["packageRules"][-1]["matchPackageNames"] = [
        p.replace("llama-swap|", "") for p in patterns
    ]
    problems = held_forks(planted)
    assert "github-tags:lusoris/llama-swap" in problems
    assert release_age(planted, "github-tags", "lusoris/llama-swap") is None
