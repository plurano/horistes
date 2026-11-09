# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Action pins: a use on a tag, a wrong comment and an undeclared `with` key fail.

The gate asks no host, so two engines stand in the place of zizmor here. What
zizmor does without a token is run with the real one: the listing, the audits
that are switched off, and the run that stays offline without saying so. What
needs the forge is run with a stand-in that answers as zizmor 1.30.1 was
measured to answer on 2026-10-11 (ADR-0016); the metadata of an action comes
from the stand-in client of conftest.py.
"""

from __future__ import annotations

import base64
import datetime
import json
import shlex
import shutil
import subprocess
import sys

import pytest
import yaml
from conftest import ROOT, external

sys.path.insert(0, str(ROOT / "tools"))
import pins

CHECKOUT = "11bd71901bbe5b1630ceea73d27597364c9af683"
OPENER = "271a8d0340265f705b14b6d32b9829c1cb33d45e"
SHARED = "0123456789abcdef0123456789abcdef01234567"
WORKFLOW = ".github/workflows/build.yml"
USE = f"        uses: actions/checkout@{CHECKOUT} # v4.2.2\n"
KEY = "          sign-commits: true\n"
CALL = f"    uses: plurano/shared/.github/workflows/licence.yml@{SHARED} # v1.2.0\n"
GOOD = f"""\
name: Build
on: workflow_dispatch
permissions: {{}}
jobs:
  build:
    name: Build
    runs-on: ubuntu-24.04
    steps:
      - name: Check out
{USE}        with:
          persist-credentials: false
      - name: Open a pull request
        uses: peter-evans/create-pull-request@{OPENER} # v7.0.8
        with:
{KEY}      - name: Inside the repository
        uses: ./actions/stop
        with:
          anything: goes
  licence:
    name: Licence
{CALL}    with:
      path: .
"""
# A stand-in for zizmor. It reports a use on a tag or a branch always, and a
# commit under a comment that is not its release only when it has a token and
# was not told to stay offline; a branch comment is judged against that branch's
# head commit; a commit no release names is never reported.
# With --fix=all it rewrites what it knows. Every call is recorded.
STAND_IN = r"""
import json
import os
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("zizmor 0.0.0 (stand-in)")
    sys.exit(0)
with open(os.environ["FAKE_ENGINE"], encoding="utf-8") as source:
    told = json.load(source)
with open(os.environ["FAKE_ENGINE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": args, "token": os.environ.get("GH_TOKEN", "")}) + "\n")
if "exit" in told:
    sys.stderr.write("stand-in: told to end with this code\n")
    sys.exit(told["exit"])
config = args[args.index("--config") + 1]
files = [a for a in args if a.endswith((".yml", ".yaml")) and a != config]
quiet = "--offline" in args or os.environ.get("ZIZMOR_OFFLINE") == "true"
online = bool(os.environ.get("GH_TOKEN")) and not quiet
fix = "--fix=all" in args
USES = re.compile(
    r'^(\s*(?:- )?uses:\s*)["\']?([^\s@"\']+?)@([^\s"\']+?)["\']?(?:\s+#\s*(.*))?$'
)
found = []


def report(audit, path, row, note):
    spot = {"key": {"Local": {"verbatim_path": path}}, "annotation": note, "kind": "Primary"}
    spot["route"] = {"route": [{"Key": "jobs"}, {"Key": "uses"}]}
    place = {"location": {"start_point": {"row": row, "column": 0}}}
    spots = [{"symbolic": spot, "concrete": place}]
    found.append({"ident": audit, "desc": audit, "locations": spots})


def judge(path, lines, row):
    head, action, ref, note = USES.match(lines[row]).groups()
    if not re.fullmatch("[0-9a-f]{40}", ref):
        known = told["tags"].get(f"{action}@{ref}") if online else None
        if fix and known:
            lines[row] = f"{head}{action}@{known[0]} # {known[1]}"
        else:
            report("unpinned-uses", path, row, "action is not pinned to a hash")
        return
    branches = told.get("branches", {}).get(action, {}) if online else {}
    if note and note in branches:
        head_commit = branches[note]
        if ref == head_commit:
            return
        report("ref-version-mismatch", path, row, f"branch points to commit {head_commit[:12]}")
        return
    release = told["releases"].get(f"{action}@{ref}") if online else None
    if release is None or note == release:
        return
    if note is not None and not re.fullmatch(r"v[0-9.]+", note):
        report("ref-version-mismatch", path, row, "comment does not contain a version")
    elif fix:
        lines[row] = f"{head}{action}@{ref} # {release}"
    else:
        said = f"is pointed to by tag {release}" if note else "missing version comment"
        report("ref-version-mismatch", path, row, said)


for path in files:
    with open(path, encoding="utf-8") as source:
        lines = source.read().split("\n")
    for row, line in enumerate(lines):
        found_use = USES.match(line)
        if found_use and not found_use.group(2).startswith((".", "docker:")):
            judge(path, lines, row)
    if fix:
        with open(path, "w", encoding="utf-8") as target:
            target.write("\n".join(lines))
if told.get("other"):
    report(told["other"], files[0], 0, "a finding of another audit")
print(json.dumps(found))
sys.exit(14 if found else 0)
"""
RELEASES = {
    f"actions/checkout@{CHECKOUT}": "v4.2.2",
    f"peter-evans/create-pull-request@{OPENER}": "v7.0.8",
    f"plurano/shared/.github/workflows/licence.yml@{SHARED}": "v1.2.0",
}
TAGS = {
    "actions/checkout@v4": [CHECKOUT, "v4.2.2"],
    "actions/checkout@v4.2.2": [CHECKOUT, "v4.2.2"],
}
NODE = "runs:\n  using: node20\n  main: dist/index.js\n"
IMAGE = "runs:\n  using: docker\n  image: Dockerfile\n"
CALLED = "on:\n  workflow_call:\n    inputs:\n      path:\n        type: string\n"


def held(text: str) -> dict:
    """A file as the forge's contents request answers it."""
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


def at(repository: str, path: str, ref: str) -> str:
    return f"repos/{repository}/contents/{path}?ref={ref}"


ANSWERS = {
    at("actions/checkout", "action.yml", CHECKOUT): held(
        "name: Checkout\ninputs:\n  persist-credentials:\n    default: true\n  ref: {}\n" + NODE
    ),
    at("peter-evans/create-pull-request", "action.yml", OPENER): held(
        "name: Open\ninputs:\n  sign-commits:\n    default: false\n  token: {}\n" + NODE
    ),
    at("plurano/shared", ".github/workflows/licence.yml", SHARED): held(CALLED),
}


def run(tree, command="check", workflow=GOOD, answers=None, engine=None, env=None):
    """The tool on one workflow, with the stand-in engine and the stand-in client."""
    told = {"releases": RELEASES, "tags": TAGS, **(engine or {})}
    tree.write({WORKFLOW: workflow, "fake_zizmor.py": STAND_IN, "engine.json": json.dumps(told)})
    surroundings = {
        **tree.forge(ANSWERS if answers is None else answers),
        "ZIZMOR": shlex.join([sys.executable, "-B", str(tree.root / "fake_zizmor.py")]),
        "FAKE_ENGINE": str(tree.root / "engine.json"),
        "FAKE_ENGINE_LOG": str(tree.root / "engine.log"),
        "GH_TOKEN": "a-token-for-the-stand-in",
        "GITHUB_TOKEN": "",
        "ZIZMOR_GITHUB_TOKEN": "",
        "ZIZMOR_OFFLINE": "",
        **(env or {}),
    }
    return tree.tool("pins.py", command, env=surroundings)


def engine_calls(tree) -> list[dict]:
    log = tree.root / "engine.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def planted(old: str, new: str) -> str:
    assert GOOD.count(old) == 1
    return GOOD.replace(old, new)


def real(tree, command, workflow, env=None):
    """The tool with the zizmor of the gate; nothing here may ask a host."""
    tree.write({WORKFLOW: workflow})
    names = ("GH_TOKEN", "GITHUB_TOKEN", "ZIZMOR_GITHUB_TOKEN")
    surroundings = {
        "ZIZMOR": shlex.join(external("ZIZMOR", "zizmor")),
        "GH": shlex.join([sys.executable, "-c", "import sys; sys.exit(4)"]),
        **dict.fromkeys(names, ""),
        **(env or {}),
    }
    return tree.tool("pins.py", command, env=surroundings)


def test_tree_on_commits_with_right_comments_and_declared_keys_passes(tree):
    done = run(tree)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("PASS: 1 file(s), 3 outside use(s)")
    assert "3 `with` key(s) declared, 1 job(s) on no floating runner label" in done.stdout
    assert sorted(tree.forge_calls()) == sorted(ANSWERS)
    assert [call["token"] for call in engine_calls(tree)] == ["a-token-for-the-stand-in"]


# The end of the finding on a commit pin whose comment is not one ref.
ONE_REF = "or the update bot skips the pin (ADR-0075)"


@pytest.mark.parametrize(
    ("old", "new", "line", "message"),
    [
        (USE, "        uses: actions/checkout@v4\n", 10, "actions/checkout@v4: action is not"),
        (USE, "        uses: actions/checkout@main\n", 10, "actions/checkout@main: action is not"),
        (USE, USE.replace("v4.2.2", "v4.2.1"), 10, "# v4.2.1: is pointed to by tag v4.2.2"),
        (USE, USE.replace(" # v4.2.2", ""), 10, f"@{CHECKOUT}: missing version comment"),
        (
            USE,
            USE.replace("v4.2.2", "v4.2.2 kept by policy"),
            10,
            "# v4.2.2 kept by policy: comment does not contain a version",
        ),
        (CALL, CALL.replace(f"{SHARED} # v1.2.0", "v1"), 23, "licence.yml@v1: action is not"),
        (CALL, CALL.replace("v1.2.0", "v1.1.9"), 23, "# v1.1.9: is pointed to by tag v1.2.0"),
    ],
    ids=["tag", "branch", "other-release", "no-comment", "words", "job-on-tag", "job-comment"],
)
def test_use_that_is_not_a_commit_under_its_release_fails(tree, old, new, line, message):
    """Each planted use off its release fails, and every FAIL line names that use's line."""
    answers = dict(ANSWERS)
    for ref in ("v4", "main"):
        answers[at("actions/checkout", "action.yml", ref)] = ANSWERS[
            at("actions/checkout", "action.yml", CHECKOUT)
        ]
    answers[at("plurano/shared", ".github/workflows/licence.yml", "v1")] = held(CALLED)
    done = run(tree, workflow=planted(old, new), answers=answers)
    assert done.returncode == 1, done.stdout
    failures = [text for text in done.stderr.splitlines() if text.startswith("FAIL")]
    assert all(text.startswith(f"FAIL: {WORKFLOW}:{line}: ") for text in failures), failures
    (failure,) = [text for text in failures if message in text]
    assert [text for text in failures if text != failure and ONE_REF not in text] == []


@pytest.mark.parametrize("command", ["check", "pin"])
def test_floating_runner_label_fails_the_check_and_is_not_rewritten(tree, command):
    floating = planted("runs-on: ubuntu-24.04", "runs-on: ubuntu-latest")
    done = run(tree, command, workflow=floating)
    assert done.returncode == 1, done.stdout
    assert done.stderr == (
        f"FAIL: {WORKFLOW}:7: `runs-on` of job `build`: `ubuntu-latest` is a floating runner "
        "label; name the release it stands for\n"
    )
    assert (tree.root / WORKFLOW).read_text() == floating


def test_misspelled_key_of_an_outside_action_fails_with_its_line(tree):
    done = run(tree, workflow=planted(KEY, KEY.replace("sign-commits", "sign-commit")))
    assert done.returncode == 1, done.stdout
    assert done.stderr.strip() == (
        f"FAIL: {WORKFLOW}:16: peter-evans/create-pull-request@{OPENER}: "
        "input `sign-commit` is not declared at that ref; it declares `sign-commits`"
    )


def test_key_of_a_called_workflow_that_it_does_not_declare_fails(tree):
    done = run(tree, workflow=planted("      path: .\n", "      paths: .\n"))
    assert done.returncode == 1, done.stdout
    assert f"{WORKFLOW}:25: plurano/shared/.github/workflows/licence.yml@{SHARED}: " in done.stderr
    assert "input `paths` is not declared at that ref; it declares `path`" in done.stderr


def test_declared_key_in_another_case_passes(tree):
    done = run(tree, workflow=planted(KEY, KEY.replace("sign-commits", "Sign-Commits")))
    assert done.returncode == 0, done.stderr


def test_key_of_an_aliased_mapping_is_read(tree):
    anchored = planted("        with:\n" + KEY, "        with: &given\n" + KEY)
    again = (
        f"      - uses: peter-evans/create-pull-request@{OPENER} # v7.0.8\n        with: *given\n"
    )
    done = run(tree, workflow=anchored.replace("  licence:\n", again + "  licence:\n"))
    assert done.returncode == 0, done.stderr
    assert "4 `with` key(s) declared" in done.stdout
    wrong = anchored.replace("sign-commits", "sign-commit")
    done = run(tree, workflow=wrong.replace("  licence:\n", again + "  licence:\n"))
    assert done.returncode == 1, done.stdout
    assert done.stderr.count(f"{WORKFLOW}:16: ") == 2


def test_merge_key_is_refused_before_anything_is_checked(tree):
    done = run(tree, workflow=planted(KEY, "          <<: {sign-commits: true}\n"))
    assert done.returncode == 3, done.stdout
    assert done.stderr.startswith("ERROR: ")
    assert "tag:yaml.org,2002:merge" in done.stderr
    assert engine_calls(tree) == []


@pytest.mark.parametrize(
    ("runs", "code"),
    [(IMAGE, 0), (NODE, 1)],
    ids=["image-takes-args", "other-action-does-not"],
)
def test_args_and_entrypoint_belong_to_an_action_that_runs_an_image(tree, runs, code):
    answers = {**ANSWERS, at("peter-evans/create-pull-request", "action.yml", OPENER): held(runs)}
    new = "          args: --all\n          entrypoint: /run\n"
    done = run(tree, workflow=planted(KEY, new), answers=answers)
    assert done.returncode == code, done.stdout + done.stderr
    assert done.stderr.count("is not declared at that ref") == (2 if code else 0)


def test_metadata_named_action_yaml_is_read(tree):
    answers = dict(ANSWERS)
    answers[at("actions/checkout", "action.yaml", CHECKOUT)] = answers.pop(
        at("actions/checkout", "action.yml", CHECKOUT)
    )
    done = run(tree, answers=answers)
    assert done.returncode == 0, done.stderr


def test_action_in_a_folder_is_read_from_that_folder(tree):
    new = f"        uses: actions/checkout/sub/dir@{CHECKOUT} # v4.2.2\n"
    answers = {
        **ANSWERS,
        at("actions/checkout", "sub/dir/action.yml", CHECKOUT): held(
            "inputs:\n  persist-credentials: {}\n" + NODE
        ),
    }
    done = run(
        tree,
        workflow=planted(USE, new),
        answers=answers,
        engine={"releases": {**RELEASES, f"actions/checkout/sub/dir@{CHECKOUT}": "v4.2.2"}},
    )
    assert done.returncode == 0, done.stderr
    assert at("actions/checkout", "sub/dir/action.yml", CHECKOUT) in tree.forge_calls()


@pytest.mark.parametrize(
    ("gone", "message"),
    [
        (
            at("actions/checkout", "action.yml", CHECKOUT),
            f"{WORKFLOW}:10: actions/checkout@{CHECKOUT}: no action.yml or action.yaml at that ref",
        ),
        (
            at("plurano/shared", ".github/workflows/licence.yml", SHARED),
            f"licence.yml@{SHARED}: no workflow with `on: workflow_call` at that ref",
        ),
    ],
    ids=["action", "workflow"],
)
def test_use_whose_metadata_the_forge_does_not_hold_fails(tree, gone, message):
    done = run(tree, answers={key: value for key, value in ANSWERS.items() if key != gone})
    assert done.returncode == 1, done.stdout
    assert message in done.stderr


def test_called_file_that_is_no_called_workflow_fails(tree):
    key = at("plurano/shared", ".github/workflows/licence.yml", SHARED)
    done = run(tree, answers={**ANSWERS, key: held("on: push\njobs: {}\n")})
    assert done.returncode == 1, done.stdout
    assert "no workflow with `on: workflow_call` at that ref" in done.stderr


@pytest.mark.parametrize(
    "answer",
    [{"fail": "gh: Server Error (HTTP 502)"}, {"content": 7}, held("- a list\n"), held("a: [")],
    ids=["forge-error", "no-content", "not-a-mapping", "not-yaml"],
)
def test_metadata_that_cannot_be_read_ends_the_check(tree, answer):
    key = at("actions/checkout", "action.yml", CHECKOUT)
    done = run(tree, answers={**ANSWERS, key: answer})
    assert done.returncode == 3, done.stdout
    assert done.stderr.startswith("ERROR: GET " + key)
    assert "PASS" not in done.stdout


def test_action_file_of_the_repository_is_read_too(tree):
    step = "    - uses: actions/checkout@v4\n      with:\n        reff: main\n"
    action = "name: Stop\ndescription: Stops.\nruns:\n  using: composite\n  steps:\n" + step
    tree.write({"actions/stop/action.yml": action})
    answers = {**ANSWERS, at("actions/checkout", "action.yml", "v4"): held("inputs: {ref: {}}\n")}
    done = run(tree, answers=answers)
    assert done.returncode == 1, done.stdout
    assert "FAIL: actions/stop/action.yml:6: actions/checkout@v4: action is not" in done.stderr
    assert "FAIL: actions/stop/action.yml:8: actions/checkout@v4: input `reff`" in done.stderr


def test_tree_without_a_workflow_is_an_error(tree):
    done = tree.tool("pins.py", "check")
    assert done.returncode == 3, done.stdout
    assert "no workflow and no action file" in done.stderr


def test_without_a_token_the_engine_is_not_started(tree):
    done = run(tree, env={"GH_TOKEN": ""})
    assert done.returncode == 3, done.stdout
    assert "no token in GH_TOKEN, GITHUB_TOKEN, ZIZMOR_GITHUB_TOKEN" in done.stderr
    assert engine_calls(tree) == []


def test_token_of_another_name_reaches_the_engine_under_one_name(tree):
    done = run(tree, env={"GH_TOKEN": "", "ZIZMOR_GITHUB_TOKEN": "the-other-token"})
    assert done.returncode == 0, done.stderr
    assert [call["token"] for call in engine_calls(tree)] == ["the-other-token"]


def test_engine_that_stays_offline_without_saying_so_is_refused(tree):
    wrong = planted(USE, USE.replace("v4.2.2", "v4.2.1"))
    done = run(tree, workflow=wrong, env={"ZIZMOR_OFFLINE": "true"})
    assert done.returncode == 3, done.stdout
    assert "did not report the planted `ref-version-mismatch` case" in done.stderr
    assert "PASS" not in done.stdout


def test_finding_of_another_audit_ends_the_run_before_a_file_is_written(tree):
    on_tag = planted(USE, "        uses: actions/checkout@v4\n")
    done = run(tree, "pin", workflow=on_tag, engine={"other": "new-audit"})
    assert done.returncode == 3, done.stdout
    assert "zizmor reports `new-audit`" in done.stderr
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == on_tag
    assert all("--fix=all" not in call["args"] for call in engine_calls(tree))
    assert tree.forge_calls() == []


@pytest.mark.parametrize("code", [1, 2, 3, 7, 10, 15])
def test_exit_code_outside_the_findings_ends_the_run(tree, code):
    done = run(tree, engine={"exit": code})
    assert done.returncode == 3, done.stdout
    assert f"ERROR: zizmor: exit {code}: stand-in: told to end with this code" in done.stderr


def test_engine_is_run_without_ignores_and_with_every_other_audit_off(tree):
    assert run(tree).returncode == 0
    (call,) = engine_calls(tree)
    for option in ("--no-ignores", "--persona", "pedantic", "--format", "json"):
        assert option in call["args"]
    assert "--offline" not in call["args"]
    assert len(set(pins.OTHER_AUDITS)) == len(pins.OTHER_AUDITS) == 39
    assert not {pins.UNPINNED, pins.COMMENT} & set(pins.OTHER_AUDITS)


def test_pin_writes_the_commit_and_its_release_and_no_other_line(tree):
    before = planted(USE, "        uses: actions/checkout@v4\n").replace("v7.0.8", "v7.0.7")
    done = run(tree, "pin", workflow=before)
    assert done.returncode == 0, done.stderr
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == GOOD
    assert ["--fix=all" in call["args"] for call in engine_calls(tree)] == [False, True, False]


def test_pin_leaves_a_tag_the_forge_does_not_know_and_fails(tree):
    before = planted(USE, "        uses: actions/checkout@v9\n")
    answers = {**ANSWERS, at("actions/checkout", "action.yml", "v9"): held("inputs: {}\n")}
    done = run(tree, "pin", workflow=before, answers=answers)
    assert done.returncode == 1, done.stdout
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == before
    assert f"FAIL: {WORKFLOW}:10: actions/checkout@v9: action is not" in done.stderr


def test_check_writes_no_file(tree):
    before = planted(USE, "        uses: actions/checkout@v4\n")
    done = run(tree, workflow=before)
    assert done.returncode == 1, done.stdout
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == before


# With the zizmor of the gate, offline.
OTHER_FINDINGS = GOOD.replace("permissions: {}\n", "").replace(
    "        with:\n          persist-credentials: false\n", ""
)


def test_listing_names_a_use_on_a_tag_and_asks_no_host(tree):
    on_tag = planted(USE, "        uses: actions/checkout@v4 # zizmor: ignore[unpinned-uses]\n")
    done = real(tree, "list", on_tag)
    assert done.returncode == 0, done.stderr
    first, count = done.stdout.splitlines()
    assert first.startswith(f"{WORKFLOW}:10: actions/checkout@v4 # zizmor: ignore[unpinned-uses]: ")
    assert count.startswith("1 use(s) on a tag or a branch in 1 file(s); zizmor ")


def test_listing_of_a_pinned_tree_is_empty_whatever_else_zizmor_would_find(tree):
    done = real(tree, "list", OTHER_FINDINGS)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("0 use(s) on a tag or a branch in 1 file(s); zizmor ")


def test_audit_that_is_not_switched_off_is_refused(tree, monkeypatch):
    tree.write({WORKFLOW: OTHER_FINDINGS})
    work = tree.root / "work"
    work.mkdir()
    monkeypatch.setenv("ZIZMOR", shlex.join(external("ZIZMOR", "zizmor")))
    named = {str(tree.root / WORKFLOW): WORKFLOW}
    assert pins._scan(work, named, None) == {pins.UNPINNED: [], pins.COMMENT: []}
    monkeypatch.setattr(pins, "OTHER_AUDITS", [n for n in pins.OTHER_AUDITS if n != "artipacked"])
    with pytest.raises(pins.PinError, match="zizmor reports `artipacked`"):
        pins._scan(work, named, None)


def test_real_engine_told_to_stay_offline_is_refused(tree):
    env = {"GH_TOKEN": "not-a-token", "ZIZMOR_OFFLINE": "true"}
    done = real(tree, "check", GOOD, env=env)
    assert done.returncode == 3, done.stdout
    assert "did not report the planted `ref-version-mismatch` case" in done.stderr


def test_check_without_any_token_is_refused(tree):
    done = real(tree, "check", GOOD)
    assert done.returncode == 3, done.stdout
    assert "no token in GH_TOKEN, GITHUB_TOKEN, ZIZMOR_GITHUB_TOKEN" in done.stderr


def test_engine_that_is_not_there_is_an_error(tree):
    done = real(tree, "list", GOOD, env={"ZIZMOR": str(tree.root / "no-such-zizmor")})
    assert done.returncode == 3, done.stdout
    assert done.stderr.startswith("ERROR: ")


FORBIDDEN_REFS = [
    "main..bad",
    "release/",
    "main.",
    "a//b",
    "a/.b",
    "main.lock",
    "a.lock/b",
]

ALLOWED_REFS = [
    "main",
    "v1.2.3",
    "release/v2",
]


@pytest.mark.parametrize("ref", FORBIDDEN_REFS)
def test_git_forbidden_refs_fail_bare_ref_check(ref):
    """Git-forbidden ref names fail _bare_ref and agree with git check-ref-format."""
    assert pins._bare_ref(ref) is False
    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git not on PATH")
    res = subprocess.run(
        [git_bin, "check-ref-format", f"refs/heads/{ref}"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert res.returncode != 0


@pytest.mark.parametrize("ref", ALLOWED_REFS)
def test_git_allowed_refs_pass_bare_ref_check(ref):
    """Git-allowed ref names pass _bare_ref and agree with git check-ref-format."""
    assert pins._bare_ref(ref) is True
    git_bin = shutil.which("git")
    if git_bin is None:
        pytest.skip("git not on PATH")
    res = subprocess.run(
        [git_bin, "check-ref-format", f"refs/heads/{ref}"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0


RED_COMMENTS = [
    "# main 2026-11-02 (#96)",
    "# v1.2.3 main",
    "# tag=v1.2.3",
    "",
    "#",
    *(f"# {r}" for r in FORBIDDEN_REFS),
]


@pytest.mark.parametrize(
    "comment",
    RED_COMMENTS,
    ids=[
        "dated",
        "words",
        "tag-equals",
        "none",
        "hash-only",
        "two-dots",
        "trailing-slash",
        "trailing-dot",
        "double-slash",
        "dot-component",
        "lock-suffix",
        "lock-component",
    ],
)
@pytest.mark.parametrize(
    ("target", "line", "lead"),
    [
        (USE, 10, f"        uses: actions/checkout@{CHECKOUT}"),
        (CALL, 23, f"    uses: plurano/shared/.github/workflows/licence.yml@{SHARED}"),
    ],
    ids=["step-uses", "job-uses"],
)
def test_outside_commit_use_with_red_comment_fails(tree, comment, target, line, lead):
    """An outside commit pin commented with a non-bare or forbidden ref fails."""
    suffix = f" {comment}\n" if comment else "\n"
    new = f"{lead}{suffix}"
    done = run(tree, workflow=planted(target, new))
    assert done.returncode == 1, done.stdout
    assert f"FAIL: {WORKFLOW}:{line}: " in done.stderr
    assert ONE_REF in done.stderr


GREEN_COMMENTS = [
    "# main",
    "# v1.2.3",
    "#v1.2.3",
    "# release/v2",
    "# v1.2.3   ",
]


@pytest.mark.parametrize(
    "comment",
    GREEN_COMMENTS,
    ids=["main", "version", "no-space", "branch", "trailing-spaces"],
)
def test_outside_commit_use_with_green_comment_passes(tree, comment):
    """An outside commit pin commented with a bare ref passes."""
    new = f"    uses: plurano/shared/.github/workflows/licence.yml@{SHARED} {comment}\n"
    engine = {"releases": {f"actions/checkout@{CHECKOUT}": "v4.2.2"}}
    done = run(tree, workflow=planted(CALL, new), engine=engine)
    assert done.returncode == 0, done.stderr
    assert "FAIL" not in done.stderr
    assert "3 on a bare ref comment" in done.stdout


def test_quoted_uses_holding_hash_inside_quotes_with_good_comment_passes(tree):
    """A quoted uses value with a hash inside passes when followed by a bare ref comment."""
    new = f'    uses: "plurano/shared/.github/workflows/licence.yml#part@{SHARED}" # main\n'
    answers = {
        **ANSWERS,
        at("plurano/shared", ".github/workflows/licence.yml%23part", SHARED): held(CALLED),
    }
    engine = {"releases": {f"actions/checkout@{CHECKOUT}": "v4.2.2"}}
    done = run(tree, workflow=planted(CALL, new), answers=answers, engine=engine)
    assert done.returncode == 0, done.stderr
    assert "FAIL" not in done.stderr
    assert "3 on a bare ref comment" in done.stdout


def test_use_on_tag_gets_no_comment_finding_from_this_check(tree):
    """A use on a tag gets an unpinned-uses finding, not a comment error."""
    new = "        uses: actions/checkout@v4.2.2 # main 2026-11-02 (#96)\n"
    done = run(tree, workflow=planted(USE, new))
    assert done.returncode == 1, done.stdout
    assert "action is not pinned to a hash" in done.stderr
    assert ONE_REF not in done.stderr


def test_pin_on_red_comment_tree_fails_and_writes_no_comment_change(tree):
    """Running pin on a tree with a non-bare ref comment fails without rewriting."""
    bad = planted(
        CALL,
        f"    uses: plurano/shared/.github/workflows/licence.yml@{SHARED}"
        " # main 2026-11-02 (#96)\n",
    )
    done = run(tree, command="pin", workflow=bad)
    assert done.returncode == 1, done.stdout
    assert ONE_REF in done.stderr
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == bad


ACTION_LICENCE = "plurano/shared/.github/workflows/licence.yml"
MAIN_HEAD = "5e08088c776c1234567890abcdef1234567890ab"
CALL_MAIN = f"    uses: {ACTION_LICENCE}@{SHARED} # main\n"
WORKFLOW_MAIN = planted(CALL, CALL_MAIN)
TAGS_PATH = "repos/plurano/shared/git/matching-refs/tags/main"
COMPARE_PATH = f"repos/plurano/shared/compare/{SHARED}...refs/heads/main"
ENGINE_BRANCH = {
    "releases": {k: v for k, v in RELEASES.items() if SHARED not in k},
    "branches": {ACTION_LICENCE: {"main": MAIN_HEAD}},
}


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_branch_comment_on_commit_in_branch_history_passes(tree, status):
    """A commit in the branch history passes forge compare."""
    answers = {**ANSWERS, TAGS_PATH: [], COMPARE_PATH: {"status": status}}
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 0, done.stderr
    if status == "ahead":
        assert "1 behind its branch head on its history" in done.stdout
    assert TAGS_PATH in tree.forge_calls()
    assert COMPARE_PATH in tree.forge_calls()


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            {"status": "behind"},
            "; the forge compare puts the commit off the history of `main` (behind)",
        ),
        (
            {"status": "diverged"},
            "; the forge compare puts the commit off the history of `main` (diverged)",
        ),
        (
            {"status": "unknown"},
            "; the forge compare puts the commit off the history of `main` (unknown)",
        ),
        ({}, "; the forge compare could not be read: no status in answer"),
        (
            {"fail": "gh: Server Error (HTTP 500)"},
            (
                "; the forge compare could not be read: GET "
                f"repos/plurano/shared/compare/{SHARED}...refs/heads/main: "
                "gh: Server Error (HTTP 500)"
            ),
        ),
        (
            None,
            (
                "; the forge compare could not be read: GET "
                f"repos/plurano/shared/compare/{SHARED}...refs/heads/main: gh: Not Found (HTTP 404)"
            ),
        ),
        (["ahead"], "; the forge compare could not be read: answer is not a mapping"),
        ({"status": 3}, "; the forge compare could not be read: status is not text"),
    ],
    ids=[
        "behind",
        "diverged",
        "unknown",
        "no-status",
        "error-500",
        "not-found",
        "not-a-mapping",
        "status-not-text",
    ],
)
def test_branch_comment_off_history_or_failing_compare_fails(tree, answer, reason):
    """Off-history commit or a failing compare call fails the check."""
    answers = (
        {**ANSWERS, TAGS_PATH: []}
        if answer is None
        else {**ANSWERS, TAGS_PATH: [], COMPARE_PATH: answer}
    )
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 1, done.stdout
    failures = [text for text in done.stderr.splitlines() if text.startswith("FAIL")]
    assert len(failures) == 1, failures
    (failure,) = failures
    assert failure.startswith(f"FAIL: {WORKFLOW}:23: ")
    assert "branch points to commit 5e08088c776c" in failure
    assert reason in failure


def test_branch_comment_with_tag_twin_fails_and_asks_no_compare(tree):
    """When the ref also names a tag, finding stays and compare is not asked."""
    answers = {
        **ANSWERS,
        TAGS_PATH: [{"ref": "refs/tags/main"}],
        COMPARE_PATH: {"status": "ahead"},
    }
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 1, done.stdout
    assert "FAIL: .github/workflows/build.yml:23: " in done.stderr
    assert "; `main` also names a tag, so the comment is not judged as a branch" in done.stderr
    assert TAGS_PATH in tree.forge_calls()
    assert COMPARE_PATH not in tree.forge_calls()


def test_branch_comment_tag_lookup_error_fails(tree):
    """When matching-refs tag query fails, finding stays and compare is not asked."""
    answers = {**ANSWERS, TAGS_PATH: {"fail": "gh: Server Error (HTTP 500)"}}
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 1, done.stdout
    assert "; the forge tag lookup could not be read: GET " in done.stderr
    assert "matching-refs/tags/main: gh: Server Error (HTTP 500)" in done.stderr
    assert COMPARE_PATH not in tree.forge_calls()


def test_branch_comment_tag_lookup_not_a_list_fails(tree):
    """When matching-refs tag query returns a non-list, finding stays and compare is not asked."""
    answers = {**ANSWERS, TAGS_PATH: {"status": "ok"}}
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 1, done.stdout
    assert "; the forge tag lookup could not be read: answer is not a list" in done.stderr
    assert COMPARE_PATH not in tree.forge_calls()


def test_branch_comment_tag_prefix_match_is_not_a_twin(tree):
    """A tag that only starts with the ref name is not a twin and compare is asked."""
    answers = {
        **ANSWERS,
        TAGS_PATH: [{"ref": "refs/tags/main-old"}],
        COMPARE_PATH: {"status": "ahead"},
    }
    done = run(tree, workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 0, done.stderr
    assert "1 behind its branch head on its history" in done.stdout
    assert TAGS_PATH in tree.forge_calls()
    assert COMPARE_PATH in tree.forge_calls()


@pytest.mark.parametrize(
    ("uses", "comment", "reason"),
    [
        (f"plurano@{SHARED}", " main", "target is not owner/repo@commit"),
        (f"{ACTION_LICENCE}@v1.2.0", " main", "target is not owner/repo@commit"),
        (f"{ACTION_LICENCE}@{SHARED}", "  ", "no ref in comment"),
        (f"{ACTION_LICENCE}@{SHARED}", None, "no ref in comment"),
    ],
    ids=["no-repo", "not-a-commit", "blank-comment", "no-comment"],
)
def test_branch_status_refuses_a_call_it_cannot_ask_about(monkeypatch, uses, comment, reason):
    """A use with no owner/repo@commit or no ref keeps its finding and asks no compare."""
    asked: list[str] = []
    monkeypatch.setattr(
        pins.forge, "api", lambda path, **kwargs: asked.append(path) or {"status": "ahead"}
    )
    drop, said = pins._branch_status(pins.Call(23, uses, True, {}, comment))
    assert drop is False
    assert said == f"; the forge compare could not be read: {reason}"
    assert asked == []


def test_branch_status_asks_one_compare_for_a_branch_comment(monkeypatch):
    """A commit use under a branch comment asks tag matching and compare and drops on ahead."""
    asked: list[str] = []

    def fake_api(path: str, *, token: str | None = None) -> object:
        """Stand-in forge API answering matching-refs and compare requests."""
        asked.append(path)
        if "matching-refs" in path:
            return []
        return {"status": "ahead"}

    monkeypatch.setattr(pins.forge, "api", fake_api)
    call = pins.Call(23, f"{ACTION_LICENCE}@{SHARED}", True, {}, " main ")
    assert pins._branch_status(call) == (True, "")
    assert asked == [TAGS_PATH, COMPARE_PATH]


def test_branch_finding_without_its_call_keeps_finding_and_asks_nothing(tmp_path, monkeypatch):
    """A branch finding on a line holding no use stays; any other finding passes unchanged."""
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    (tmp_path / WORKFLOW).write_text(WORKFLOW_MAIN, encoding="utf-8")
    asked: list[str] = []
    monkeypatch.setattr(
        pins.forge, "api", lambda path, **kwargs: asked.append(path) or {"status": "ahead"}
    )
    note = "branch points to commit 5e08088c776c"
    lost = pins.Finding(WORKFLOW, 99, f"{WORKFLOW}:99: x: {note}", note)
    other = pins.Finding(WORKFLOW, 23, f"{WORKFLOW}:23: x: points to unknown ref", "")
    documents = pins.ciwf.files(tmp_path)
    kept, lagging = pins._judge_branches(tmp_path, documents, [lost, other])
    assert lagging == 0
    assert asked == []
    assert kept == [
        pins.Finding(
            WORKFLOW,
            99,
            f"{lost.text}; the forge compare could not be read: no call found",
            note,
        ),
        other,
    ]


def test_two_uses_on_one_line_keeps_finding_and_asks_no_compare(tmp_path, monkeypatch):
    """When more than one use sits on one line, finding is kept and no compare is asked."""
    content = (
        "name: Flow\n"
        "on: workflow_dispatch\n"
        "jobs:\n"
        "  j:\n"
        '    steps: [{uses: "actions/checkout@v4"}, {uses: "actions/checkout@v4"}]\n'
    )
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    (tmp_path / WORKFLOW).write_text(content, encoding="utf-8")
    asked: list[str] = []
    monkeypatch.setattr(
        pins.forge, "api", lambda path, **kw: asked.append(path) or {"status": "ahead"}
    )
    note = "branch points to commit 5e08088c776c"
    finding = pins.Finding(WORKFLOW, 5, f"{WORKFLOW}:5: uses: {note}", note)
    documents = pins.ciwf.files(tmp_path)
    kept, lagging = pins._judge_branches(tmp_path, documents, [finding])
    assert lagging == 0
    assert asked == []
    assert len(kept) == 1
    assert (
        kept[0].text
        == f"{finding.text}; the forge compare was not asked: more than one use on this line"
    )


def test_aliased_step_keeps_finding_and_asks_no_compare(tmp_path, monkeypatch):
    """A step reused through a YAML alias gives two uses on the anchor's line; none is judged."""
    content = (
        "name: Flow\n"
        "on: workflow_dispatch\n"
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        "      - &step\n"
        f"        uses: {ACTION_LICENCE}@{SHARED} # main\n"
        "  b:\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        "      - *step\n"
    )
    (tmp_path / WORKFLOW).parent.mkdir(parents=True)
    (tmp_path / WORKFLOW).write_text(content, encoding="utf-8")
    asked: list[str] = []
    monkeypatch.setattr(
        pins.forge, "api", lambda path, **kw: asked.append(path) or {"status": "ahead"}
    )
    note = "branch points to commit 5e08088c776c"
    finding = pins.Finding(WORKFLOW, 8, f"{WORKFLOW}:8: uses: {note}", note)
    kept, lagging = pins._judge_branches(tmp_path, pins.ciwf.files(tmp_path), [finding])
    assert (lagging, asked) == (0, [])
    assert [k.text for k in kept] == [
        f"{finding.text}; the forge compare was not asked: more than one use on this line"
    ]


def test_branch_compare_receives_token_from_zizmor_github_token(monkeypatch):
    """With only ZIZMOR_GITHUB_TOKEN set, the compare call receives that token."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ZIZMOR_GITHUB_TOKEN", "fake-token-for-test")
    assert pins._token() == "fake-token-for-test"

    received_tokens: list[str | None] = []

    def spy_api(path: str, *, token: str | None = None) -> object:
        """Spy recording token parameter passed to forge API."""
        received_tokens.append(token)
        if "matching-refs" in path:
            return []
        return {"status": "ahead"}

    monkeypatch.setattr(pins.forge, "api", spy_api)
    call = pins.Call(23, f"{ACTION_LICENCE}@{SHARED}", True, {}, " main ")
    drop, _ = pins._branch_status(call, token=pins._token())
    assert drop is True
    assert received_tokens == ["fake-token-for-test", "fake-token-for-test"]


def test_branch_comment_at_branch_head_passes_without_compare(tree):
    """A commit at the branch head passes without asking forge compare."""
    engine = {
        "releases": {k: v for k, v in RELEASES.items() if SHARED not in k},
        "branches": {ACTION_LICENCE: {"main": SHARED}},
    }
    done = run(tree, workflow=WORKFLOW_MAIN, engine=engine)
    assert done.returncode == 0, done.stderr
    assert not any("/compare/" in call for call in tree.forge_calls())


def test_tag_mismatch_asks_no_compare(tree):
    """A release tag mismatch asks no compare call to the forge."""
    done = run(
        tree,
        workflow=planted(CALL, CALL.replace(f"{SHARED} # v1.2.0", f"{SHARED} # v1.1.9")),
    )
    assert done.returncode == 1
    assert not any("/compare/" in call for call in tree.forge_calls())

    done = run(
        tree,
        workflow=planted(
            USE,
            USE.replace(f"{CHECKOUT} # v4.2.2", f"{CHECKOUT} # v4.2.1"),
        ),
    )
    assert done.returncode == 1
    assert not any("/compare/" in call for call in tree.forge_calls())


def test_pin_on_diverged_branch_fails_and_leaves_file_unchanged(tree):
    """pin on diverged branch fails and leaves the workflow file unchanged."""
    answers = {**ANSWERS, TAGS_PATH: [], COMPARE_PATH: {"status": "diverged"}}
    done = run(tree, command="pin", workflow=WORKFLOW_MAIN, answers=answers, engine=ENGINE_BRANCH)
    assert done.returncode == 1, done.stdout
    assert (tree.root / WORKFLOW).read_text(encoding="utf-8") == WORKFLOW_MAIN


def test_pin_rewrite_to_branch_comment_judges_fresh_document_and_passes(tree):
    """pin on a tree whose fix pass writes @commit # main and rescan finds branch ahead passes."""
    workflow = planted(CALL, f"    uses: {ACTION_LICENCE}@main\n")
    engine = {
        "tags": {**TAGS, f"{ACTION_LICENCE}@main": [SHARED, "main"]},
        "branches": {ACTION_LICENCE: {"main": MAIN_HEAD}},
    }
    answers = {**ANSWERS, TAGS_PATH: [], COMPARE_PATH: {"status": "ahead"}}
    done = run(tree, command="pin", workflow=workflow, answers=answers, engine=engine)
    assert done.returncode == 0, done.stderr
    assert "1 behind its branch head on its history" in done.stdout


def test_branch_rule_exception_in_exceptions_yaml():
    """exceptions.yaml holds the declared exception for branch comment history check."""
    exceptions_data = yaml.safe_load((ROOT / "exceptions.yaml").read_text(encoding="utf-8"))
    entries = [
        e
        for e in exceptions_data.get("exceptions", [])
        if e.get("rule") == pins.BRANCH_RULE and e.get("path") == "tools/pins.py"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.get("category") == "D"
    assert entry.get("issue") == "zizmorcore/zizmor#2378"
    entered = datetime.date.fromisoformat(entry["entered"])
    expires = datetime.date.fromisoformat(entry["expires"])
    assert expires == entered + datetime.timedelta(days=120)
