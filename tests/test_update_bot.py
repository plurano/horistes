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
from conftest import ROOT, TRAIN_BOT

CONFIG = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
PRESET = json.loads((ROOT / "default.json").read_text(encoding="utf-8"))
PR_TEMPLATE = (ROOT / ".github/pull_request_template.md").read_text(encoding="utf-8")
PR_HEADINGS = re.findall(r"^## .+$", PR_TEMPLATE, re.MULTILINE)
PR_CHECKBOX = re.compile(r"^- \[ \] [^\n]+(?:\n[ \t]+[^\n]+)*", re.MULTILINE)
PR_CHECKLIST = PR_CHECKBOX.findall(PR_TEMPLATE)
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
    "prHeader",
    "packageRules",
}
RULE = {
    "description",
    "matchManagers",
    "matchDepTypes",
    "enabled",
    "matchDatasources",
    "matchUpdateTypes",
    "matchPackageNames",
    "minimumReleaseAge",
    "prBodyNotes",
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
# The release the update bot last wrote into the licence workflow. The planted
# lines below are built from it, so a release update keeps them valid.
(PINNED_RELEASE,) = re.findall(
    r"^ +# release (\d+\.\d+\.\d+)$",
    (ROOT / LICENCE).read_text(encoding="utf-8"),
    flags=re.MULTILINE,
)
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


MAX_RULES = 50
MAX_PATTERNS = 50


def _glob_to_regex(glob: str) -> str:
    """Convert a segment-aware Renovate glob pattern to an anchored regex."""
    if "{" in glob or "[" in glob:
        raise ValueError(f"unsupported glob pattern `{glob}`")
    if glob == "*":
        return r"^.*$"
    res: list[str] = ["^"]
    n = len(glob)
    skip = False
    for i in range(n):
        if skip:
            skip = False
            continue
        if glob[i : i + 2] == "**":
            res.append(r".*")
            skip = True
        elif glob[i] == "*":
            res.append(r"[^/]*")
        elif glob[i] == "?":
            res.append(r"[^/]")
        else:
            res.append(re.escape(glob[i]))
    res.append("$")
    return "".join(res)


def _single_pattern_match(pattern: str, package: str) -> tuple[bool, bool]:
    """Return (is_negative, matches) for one pattern against a package name."""
    is_neg = pattern.startswith("!")
    raw = pattern[1:] if is_neg else pattern
    if raw.startswith("/") and (raw.endswith("/") or raw.endswith("/i")):
        if raw.endswith("/i"):
            return is_neg, bool(re.search(raw[1:-2], package, re.IGNORECASE))
        return is_neg, bool(re.search(raw[1:-1], package))
    reg = _glob_to_regex(raw)
    return is_neg, bool(re.fullmatch(reg, package, re.IGNORECASE))


def patterns_match(patterns: list[str], package: str) -> bool:
    """True when a package name matches a list of Renovate string patterns."""
    if len(patterns) > MAX_PATTERNS:
        raise ValueError(f"rule has {len(patterns)} patterns, at most {MAX_PATTERNS} supported")
    if not patterns:
        return False
    has_positive = False
    positive_matched = False
    for pattern in patterns[:MAX_PATTERNS]:
        is_neg, matched = _single_pattern_match(pattern, package)
        if is_neg:
            if matched:
                return False
        else:
            has_positive = True
            if matched:
                positive_matched = True
    return not (has_positive and not positive_matched)


def release_age(
    preset: dict, datasource: str, package: str, update_type: str | None = None
) -> str | None:
    """Emulate Renovate packageRules matching for minimumReleaseAge."""
    current_age = preset.get("minimumReleaseAge")
    rules = preset.get("packageRules", [])
    if len(rules) > MAX_RULES:
        raise ValueError(f"preset has {len(rules)} packageRules, at most {MAX_RULES} supported")
    for rule in rules[:MAX_RULES]:
        if "enabled" in rule:
            continue
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        if "matchUpdateTypes" in rule and update_type not in rule["matchUpdateTypes"]:
            continue
        if "matchPackageNames" in rule and not patterns_match(rule["matchPackageNames"], package):
            continue
        if "minimumReleaseAge" in rule:
            current_age = rule["minimumReleaseAge"]
    return current_age


def disabled_package_rules(
    preset: dict,
    manager: str,
    datasource: str,
    update_type: str,
    package: str,
    dep_type: str,
) -> list[dict]:
    """Rules with `enabled: false` whose every match condition holds for this dependency."""
    rules = preset.get("packageRules", [])
    if len(rules) > MAX_RULES:
        raise ValueError(f"preset has {len(rules)} packageRules, at most {MAX_RULES} supported")
    matching = []
    for rule in rules[:MAX_RULES]:
        if rule.get("enabled") is False:
            if "matchManagers" in rule and manager not in rule["matchManagers"]:
                continue
            if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
                continue
            if "matchUpdateTypes" in rule and update_type not in rule["matchUpdateTypes"]:
                continue
            if "matchDepTypes" in rule and dep_type not in rule["matchDepTypes"]:
                continue
            if "matchPackageNames" in rule and not patterns_match(
                rule["matchPackageNames"], package
            ):
                continue
            matching.append(rule)
    return matching


def held_forks(preset: dict) -> list[str]:
    """Forks of outside projects that are not held for three days."""
    problems = []
    for fork in FORKS[:20]:
        for datasource, package, update_type in (
            ("github-tags", fork, None),
            ("go", f"github.com/{fork}", None),
            ("docker", f"ghcr.io/{fork}", None),
            ("docker", f"ghcr.io/{fork}", "digest"),
        ):
            if release_age(preset, datasource, package, update_type) != "3 days":
                suffix = f"@{update_type}" if update_type else ""
                problems.append(f"{datasource}:{package}{suffix}")
    return problems


def digest_rule_problems(preset: dict) -> list[str]:
    """Missing digest-only release-age exception or its explanation in the PR body."""
    rules = preset.get("packageRules", [])
    if len(rules) > MAX_RULES:
        raise ValueError(f"preset has {len(rules)} packageRules, at most {MAX_RULES} supported")
    matching = [
        rule
        for rule in rules[:MAX_RULES]
        if rule.get("matchDatasources") == ["docker"] and rule.get("matchUpdateTypes") == ["digest"]
    ]
    if len(matching) != 1:
        return ["digest rule"]
    rule = matching[0]
    problems = []
    if "minimumReleaseAge" not in rule or rule["minimumReleaseAge"] is not None:
        problems.append("digest minimumReleaseAge")
    notes = rule.get("prBodyNotes")
    if (
        not isinstance(notes, list)
        or len(notes) != 1
        or not isinstance(notes[0], str)
        or not notes[0].strip()
    ):
        problems.append("digest prBodyNotes")
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


def pr_header_problems(preset: dict) -> list[str]:
    """Missing template sections or claims of verification made by the bot."""
    header = preset.get("prHeader")
    if not isinstance(header, str) or not header.strip():
        return ["prHeader"]
    problems = [heading for heading in PR_HEADINGS if heading not in header.splitlines()]
    checklist = PR_CHECKBOX.findall(header)
    items = {" ".join(item.split()) for item in checklist}
    problems.extend(item for item in PR_CHECKLIST if " ".join(item.split()) not in items)
    if re.search(r"\[[xX]\]", header):
        problems.append("ticked box")
    return problems


def test_preset_pr_header_carries_the_repository_template():
    assert PR_HEADINGS and PR_CHECKLIST
    assert pr_header_problems(PRESET) == []


def test_planted_preset_without_a_pr_header_is_refused():
    preset = {key: value for key, value in PRESET.items() if key != "prHeader"}
    assert pr_header_problems(preset) == ["prHeader"]


@pytest.mark.parametrize("mark", ["x", "X"])
def test_planted_ticked_pr_header_is_refused(mark):
    header = PR_TEMPLATE + f"\n- [{mark}] Verification claimed by the bot\n"
    assert pr_header_problems({**PRESET, "prHeader": header}) == ["ticked box"]


@pytest.mark.parametrize("missing", PR_HEADINGS + PR_CHECKLIST)
def test_planted_incomplete_pr_header_is_refused(missing):
    header = PR_TEMPLATE.replace(missing, "")
    assert pr_header_problems({**PRESET, "prHeader": header}) == [missing]


def test_pr_header_accepts_a_folded_checklist_item():
    header = re.sub(r"\n[ \t]+", " ", PR_TEMPLATE)
    assert pr_header_problems({**PRESET, "prHeader": header}) == []


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
        (f"        # release {PINNED_RELEASE}\n", ""),
        (f"        # release {PINNED_RELEASE}\n", f"        # the release is {PINNED_RELEASE}\n"),
        ("docker.io/fsfe/reuse@sha256:", f"docker.io/fsfe/reuse:{PINNED_RELEASE}@sha256:"),
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


@pytest.mark.parametrize(
    ("path", "dep_name"),
    [
        pytest.param(GATE, "gitleaks/gitleaks", id="gitleaks_in_gate"),
        pytest.param(SECRET_SCAN, "gitleaks/gitleaks", id="gitleaks_in_secret_scan"),
        pytest.param(WORKFLOW_CHECK, "rhysd/actionlint", id="actionlint_in_workflow_check"),
    ],
)
def test_tool_is_read_with_release_and_checksum(path, dep_name):
    text = (ROOT / path).read_text(encoding="utf-8")
    matches = [
        groups for mgr, groups in tool_matches(path, text) if mgr["depNameTemplate"] == dep_name
    ]
    assert len(matches) == 1
    tool = matches[0]
    assert re.fullmatch(r"v\d+\.\d+\.\d+", tool["currentValue"])
    assert re.fullmatch(r"[0-9a-f]{64}", tool["currentDigest"])


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


def test_docker_digest_rule_has_a_pr_body_explanation():
    assert digest_rule_problems(PRESET) == []
    assert PRESET["packageRules"][-2]["matchUpdateTypes"] == ["digest"]
    assert PRESET["packageRules"][-3]["matchDatasources"] == ["npm"]


@pytest.mark.parametrize(
    "package", ["docker.io/library/ubuntu", "ubuntu", "docker.io/library/python"]
)
def test_outside_docker_digest_updates_skip_the_release_age(package):
    assert release_age(PRESET, "docker", package, "digest") is None


@pytest.mark.parametrize("update_type", ["major", "minor", "patch", None])
def test_outside_docker_releases_keep_the_release_age(update_type):
    assert release_age(PRESET, "docker", "docker.io/library/ubuntu", update_type) == "3 days"


@pytest.mark.parametrize("org", ORGS)
def test_own_docker_digest_updates_skip_the_release_age(org):
    assert release_age(PRESET, "docker", f"ghcr.io/{org}/img", "digest") is None


@pytest.mark.parametrize("fork", FORKS)
def test_fork_docker_digest_updates_keep_the_release_age(fork):
    assert release_age(PRESET, "docker", f"ghcr.io/{fork}", "digest") == "3 days"


@pytest.mark.parametrize(
    ("datasource", "package"),
    [("github-digest", "actions/checkout"), ("npm", "@types/node")],
)
def test_outside_non_docker_digest_updates_keep_the_release_age(datasource, package):
    assert release_age(PRESET, datasource, package, "digest") == "3 days"


def test_planted_missing_docker_digest_rule_is_refused():
    preset = copy.deepcopy(PRESET)
    preset["packageRules"].pop(-2)
    assert release_age(preset, "docker", "docker.io/library/ubuntu", "digest") == "3 days"
    assert digest_rule_problems(preset)


def test_planted_docker_digest_rule_without_update_types_is_refused():
    preset = copy.deepcopy(PRESET)
    del preset["packageRules"][-2]["matchUpdateTypes"]
    assert release_age(preset, "docker", "docker.io/library/ubuntu", "major") is None
    assert digest_rule_problems(preset)


def test_planted_docker_digest_rule_after_forks_is_refused():
    preset = copy.deepcopy(PRESET)
    preset["packageRules"].append(preset["packageRules"].pop(-2))
    assert release_age(preset, "docker", "ghcr.io/lusoris/llama-swap", "digest") is None
    problems = held_forks(preset)
    assert "docker:ghcr.io/lusoris/llama-swap@digest" in problems
    assert "docker:ghcr.io/lusoris/llama-swap" not in problems


@pytest.mark.parametrize("age", [None, "3 days"])
def test_planted_docker_digest_rule_with_a_release_age_is_refused(age):
    preset = copy.deepcopy(PRESET)
    rule = preset["packageRules"][-2]
    del rule["minimumReleaseAge"]
    if age is not None:
        rule["minimumReleaseAge"] = age
    assert digest_rule_problems(preset) == ["digest minimumReleaseAge"]


@pytest.mark.parametrize("notes", [None, [], "digest update", [""], [1]])
def test_planted_docker_digest_rule_without_pr_body_notes_is_refused(notes):
    preset = copy.deepcopy(PRESET)
    rule = preset["packageRules"][-2]
    del rule["prBodyNotes"]
    if notes is not None:
        rule["prBodyNotes"] = notes
    assert digest_rule_problems(preset) == ["digest prBodyNotes"]


def test_planted_docker_digest_rule_with_singular_pr_body_note_is_refused():
    preset = copy.deepcopy(PRESET)
    rule = preset["packageRules"][-2]
    rule["prBodyNote"] = rule.pop("prBodyNotes")[0]
    assert outside_the_preset(preset) == [
        f"packageRules[{len(preset['packageRules']) - 2}].prBodyNote"
    ]


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


# Renovate 44.181.7 gives a reusable workflow pin the dependency type `workflow`
# (measured with --dry-run=lookup) and a step's action pin `action`.
@pytest.mark.parametrize("dep_type", ["workflow", "action"])
def test_preset_permits_horistes_digest_updates(dep_type):
    """default.json lets plurano/horistes digest updates through at once and disables none."""
    assert release_age(PRESET, "github-digest", "plurano/horistes") is None
    found = disabled_package_rules(
        PRESET, "github-actions", "github-digest", "digest", "plurano/horistes", dep_type
    )
    assert found == []


def test_planted_presets_refuse_disabled_or_aged_horistes_digest():
    """A preset that disables or delays plurano/horistes digest updates is refused."""
    planted_disabled = copy.deepcopy(PRESET)
    planted_disabled["packageRules"].append(
        {
            "matchPackageNames": ["plurano/horistes"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    disabled = disabled_package_rules(
        planted_disabled,
        "github-actions",
        "github-digest",
        "digest",
        "plurano/horistes",
        "workflow",
    )
    assert disabled == [planted_disabled["packageRules"][-1]]

    planted_delayed = copy.deepcopy(PRESET)
    for rule in planted_delayed["packageRules"]:
        if "github-digest" in rule.get("matchDatasources", []):
            rule["minimumReleaseAge"] = "3 days"
    assert release_age(planted_delayed, "github-digest", "plurano/horistes") == "3 days"


def test_planted_preset_with_glob_plurano_star_disables_package():
    """A planted preset disabling plurano/* matches plurano/horistes."""
    planted = copy.deepcopy(PRESET)
    planted["packageRules"].append(
        {
            "matchPackageNames": ["plurano/*"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    disabled = disabled_package_rules(
        planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
    )
    assert disabled == [planted["packageRules"][-1]]


def test_planted_preset_with_glob_plurano_double_star_disables_package():
    """A planted preset disabling plurano/** matches plurano/horistes."""
    planted = copy.deepcopy(PRESET)
    planted["packageRules"].append(
        {
            "matchPackageNames": ["plurano/**"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    disabled = disabled_package_rules(
        planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
    )
    assert disabled == [planted["packageRules"][-1]]


def test_planted_preset_with_star_alone_disables_package():
    """A planted preset disabling * matches any package."""
    planted = copy.deepcopy(PRESET)
    planted["packageRules"].append(
        {
            "matchPackageNames": ["*"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    disabled = disabled_package_rules(
        planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
    )
    assert disabled == [planted["packageRules"][-1]]


def test_planted_preset_with_positive_and_negative_patterns_excludes_package():
    """A negative pattern in matchPackageNames excludes the package from disabling."""
    planted = copy.deepcopy(PRESET)
    planted["packageRules"].append(
        {
            "matchPackageNames": ["/^plurano/", "!plurano/horistes"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    disabled = disabled_package_rules(
        planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
    )
    assert disabled == []


def test_preset_with_more_than_50_rules_fails_with_error():
    """A preset with more than 50 packageRules raises an error instead of silently passing."""
    planted = copy.deepcopy(PRESET)
    while len(planted["packageRules"]) < 59:
        planted["packageRules"].append({"matchPackageNames": ["other/dep"], "enabled": True})
    planted["packageRules"].append(
        {
            "matchPackageNames": ["plurano/horistes"],
            "matchUpdateTypes": ["digest"],
            "enabled": False,
        }
    )
    assert len(planted["packageRules"]) == 60
    with pytest.raises(ValueError, match="at most 50"):
        disabled_package_rules(
            planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
        )


def test_unsupported_brace_glob_pattern_fails_with_error():
    """A glob pattern containing braces raises an error naming the pattern."""
    planted = copy.deepcopy(PRESET)
    planted["packageRules"].append(
        {
            "matchPackageNames": ["plurano/{a,b}"],
            "enabled": False,
        }
    )
    with pytest.raises(ValueError, match=r"unsupported glob pattern `plurano/\{a,b\}`"):
        disabled_package_rules(
            planted, "github-actions", "github-digest", "digest", "plurano/a", "workflow"
        )


def test_rule_with_more_than_50_patterns_fails_with_error():
    """A disabling rule with more than 50 package patterns raises an error naming the count."""
    planted = copy.deepcopy(PRESET)
    names = [f"other/dep-{index}" for index in range(50)] + ["plurano/horistes"]
    planted["packageRules"].append({"matchPackageNames": names, "enabled": False})
    with pytest.raises(ValueError, match="rule has 51 patterns, at most 50"):
        disabled_package_rules(
            planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
        )


def test_preset_at_both_50_bounds_is_read_to_its_end():
    """Exactly 50 rules, the last with exactly 50 patterns: its last pattern still disables."""
    planted = copy.deepcopy(PRESET)
    while len(planted["packageRules"]) < 49:
        planted["packageRules"].append({"matchPackageNames": ["other/dep"], "enabled": True})
    names = [f"other/dep-{index}" for index in range(49)] + ["plurano/horistes"]
    last = {"matchPackageNames": names, "matchUpdateTypes": ["digest"], "enabled": False}
    planted["packageRules"].append(last)
    assert len(planted["packageRules"]) == 50
    found = disabled_package_rules(
        planted, "github-actions", "github-digest", "digest", "plurano/horistes", "workflow"
    )
    assert found == [last]
