# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Action pins: every outside use is on a commit, its comment names it, its inputs exist.

    pins.py list  [--root DIR]
    pins.py check [--root DIR]
    pins.py pin   [--root DIR]

`list` prints every use of an outside action or workflow that is on a tag or
a branch; it asks no host. `check` fails on such a use, on a commit whose
comment names another release, no release or nothing, and on a `with:` key
the action or the called workflow does not declare at the ref that is
written. `pin` rewrites a tag or a branch to the commit it names today, puts
the comment right where the commit is known, and then checks. It moves no
pin to a newer release. A commit that no release names is not judged: zizmor
reports nothing for it, whatever its comment says (ADR-0016).

The first two findings are zizmor's (`unpinned-uses`, `ref-version-mismatch`,
persona pedantic); this tool runs it with every other audit switched off and
with ignore comments off. Three things zizmor does without an error are
errors here:

- With no token, or told by its environment to stay offline, zizmor leaves
  the comment check out and reports nothing. Every run therefore carries a
  planted workflow with one use on a tag and one commit under a wrong
  comment, and fails unless both are reported.
- A name in its configuration that it does not know is ignored, so a newer
  zizmor would run its new audits and `--fix` would apply their fixes. A
  finding of any other audit ends the run before a file is written.
- An exit code outside its documented set (0, 11 to 14) ends the run.

The `with:` keys are read here: the metadata of an action (`action.yml` or
`action.yaml`) or the called workflow file comes from the forge at the
written ref, through the forge's client (`gh api`, taken from GH). Names are
compared without case. An action that runs an image also takes `args` and
`entrypoint`. A use inside the repository (`./`) and an image (`docker://`)
are not read; actionlint reads the first.

The token for zizmor is GH_TOKEN, GITHUB_TOKEN or ZIZMOR_GITHUB_TOKEN, else
the one the forge's client holds. zizmor is taken from ZIZMOR.

Exit codes: 0 pass, 1 a finding, 3 the check could not be made.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf
import forge

UNPINNED = "unpinned-uses"
COMMENT = "ref-version-mismatch"
# Every other audit of zizmor 1.30.1 (docs.zizmor.sh/audits, read 2026-10-11).
OTHER_AUDITS = (
    "adhoc-packages",
    "anonymous-definition",
    "archived-uses",
    "artipacked",
    "bot-conditions",
    "cache-poisoning",
    "concurrency-limits",
    "dangerous-triggers",
    "dependabot-cooldown",
    "dependabot-execution",
    "excessive-permissions",
    "forbidden-uses",
    "github-app",
    "github-env",
    "hardcoded-container-credentials",
    "impostor-commit",
    "insecure-commands",
    "insecure-url-scheme",
    "known-vulnerable-actions",
    "misfeature",
    "obfuscation",
    "overprovisioned-secrets",
    "ref-confusion",
    "secrets-inherit",
    "secrets-outside-env",
    "self-hosted-runner",
    "self-repository",
    "stale-action-refs",
    "superfluous-actions",
    "template-injection",
    "typosquat-uses",
    "undocumented-permissions",
    "unpinned-images",
    "unpinned-tools",
    "unredacted-secrets",
    "unsound-condition",
    "unsound-contains",
    "unsound-ternary",
    "use-trusted-publishing",
)
# The planted workflow. The commit is the release v4.2.2 of actions/checkout.
PLANTED = """\
name: planted
on: workflow_dispatch
permissions: {}
jobs:
  planted:
    name: planted
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4.2.2
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.1
"""
FINDING_CODES = (0, 11, 12, 13, 14)
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "ZIZMOR_GITHUB_TOKEN")
ENGINE_SECONDS = 600
CLIENT_SECONDS = 60
LOCAL = ("./", "../", "docker://")
WORKFLOW_FOLDER = ".github/workflows/"
IMAGE_KEYS = ("args", "entrypoint")


class PinError(Exception):
    """The check could not be made."""


class Finding(NamedTuple):
    path: str
    line: int
    text: str


class Call(NamedTuple):
    """One `uses`: its line, its text, whether a job calls a workflow, its `with` keys by line."""

    line: int
    uses: str
    workflow: bool
    keys: dict[str, int]


def _run(command: list[str], seconds: int, env: dict[str, str] | None = None):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=seconds,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise PinError(f"{command[0]}: {err}") from err


def _engine() -> list[str]:
    return shlex.split(os.environ.get("ZIZMOR", "zizmor"))


def _version() -> str:
    done = _run([*_engine(), "--version"], CLIENT_SECONDS)
    if done.returncode != 0 or not done.stdout.strip():
        raise PinError(f"zizmor --version: exit {done.returncode}")
    return done.stdout.strip()


def _token() -> str:
    for name in TOKENS:
        if os.environ.get(name):
            return os.environ[name]
    done = _run([*shlex.split(os.environ.get("GH", "gh")), "auth", "token"], CLIENT_SECONDS)
    if done.returncode != 0 or not done.stdout.strip():
        raise PinError(
            f"no token in {', '.join(TOKENS)} and none from the forge's client: "
            "without one zizmor leaves the comment check out"
        )
    return done.stdout.strip()


def _engine_run(work: Path, files: list[str], token: str | None, fix: bool):
    """One run of zizmor on these files with the two audits; without a token it stays offline."""
    config = work / "zizmor.yml"
    lines = [f"  {name}:\n    disable: true\n" for name in OTHER_AUDITS]
    config.write_text("rules:\n" + "".join(lines), encoding="utf-8")
    command = [*_engine(), "--config", str(config), "--persona", "pedantic", "--no-ignores"]
    command += ["--no-progress", "--color", "never", "--format", "json"]
    command += ["--fix=all"] if fix else []
    command += [] if token else ["--offline"]
    env = {key: value for key, value in os.environ.items() if key not in TOKENS}
    if token:
        env["GH_TOKEN"] = token
    done = _run([*command, *files], ENGINE_SECONDS, env)
    if done.returncode not in FINDING_CODES:
        tail = " | ".join(done.stderr.strip().splitlines()[-4:])
        raise PinError(f"zizmor: exit {done.returncode}: {tail[:600]}")
    return done


def _place(finding: dict) -> tuple[str, int, str]:
    """Path and line of the use a finding is about, and what zizmor says of it."""
    try:
        spots = finding["locations"]
        on_uses = [s for s in spots if s["symbolic"]["route"]["route"][-1:] == [{"Key": "uses"}]]
        spot = (on_uses or spots)[0]
        path = spot["symbolic"]["key"]["Local"]["verbatim_path"]
        line = spot["concrete"]["location"]["start_point"]["row"] + 1
        notes = [
            s["symbolic"].get("annotation") for s in spots if s["symbolic"]["kind"] != "Hidden"
        ]
    except (KeyError, IndexError, TypeError) as err:
        raise PinError(f"zizmor: a finding in a shape this tool does not read ({err!r})") from err
    return path, line, "; ".join(note for note in notes if note) or str(finding.get("desc"))


def _written(path: str, line: int) -> str:
    """The use as the file has it on that line, with its comment."""
    try:
        text = Path(path).read_text(encoding="utf-8").splitlines()[line - 1]
    except (OSError, UnicodeDecodeError, IndexError) as err:
        raise PinError(f"{path}:{line}: {err}") from err
    return text.partition("uses:")[2].strip() or text.strip()


def _scan(work: Path, named: dict[str, str], token: str | None) -> dict[str, list[Finding]]:
    """Findings per audit on the files of `named` (path given to zizmor: path to report).

    The planted workflow is scanned with them; a run that does not report it is refused.
    """
    planted = work / "planted.yml"
    planted.write_text(PLANTED, encoding="utf-8")
    done = _engine_run(work, [*named, str(planted)], token, fix=False)
    try:
        findings = json.loads(done.stdout)
    except ValueError as err:
        raise PinError(f"zizmor: output is not JSON ({err})") from err
    found: dict[str, list[Finding]] = {UNPINNED: [], COMMENT: []}
    seen = set()
    for finding in findings:
        audit = finding.get("ident")
        if audit not in found:
            raise PinError(
                f"zizmor reports `{audit}`: it has an audit this tool does not switch off; "
                "no file was written"
            )
        path, line, said = _place(finding)
        if path == str(planted):
            seen.add(audit)
        elif path in named:
            found[audit].append(Finding(named[path], line, f"{_written(path, line)}: {said}"))
        else:
            raise PinError(f"zizmor reports a file it was not given: {path}")
    for audit in (UNPINNED, *((COMMENT,) if token else ())):
        if audit not in seen:
            raise PinError(
                f"zizmor did not report the planted `{audit}` case: the check did not run"
            )
    return found


def _pairs(node: yaml.Node | None) -> dict[str, tuple[yaml.Node, yaml.Node]]:
    """Key and value nodes of a mapping by key. A merge key never gets here: ciwf refuses it."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {key.value: (key, value) for key, value in node.value}


def _call(pairs: dict, workflow: bool) -> Call | None:
    if "uses" not in pairs or not isinstance(pairs["uses"][1], yaml.ScalarNode):
        return None
    key, value = pairs["uses"]
    given = _pairs(pairs["with"][1]) if "with" in pairs else {}
    keys = {name: nodes[0].start_mark.line + 1 for name, nodes in given.items()}
    return Call(key.start_mark.line + 1, value.value, workflow, keys)


def _steps(pairs: dict) -> list[dict]:
    steps = pairs["steps"][1] if "steps" in pairs else None
    return [_pairs(step) for step in steps.value] if isinstance(steps, yaml.SequenceNode) else []


def _calls(path: Path, kind: str) -> list[Call]:
    """Every `uses` of a workflow or an action file, with the line of each `with` key."""
    try:
        root = _pairs(yaml.compose(path.read_text(encoding="utf-8"), Loader=ciwf.StrictLoader))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise PinError(f"{path}: {err}") from err
    if kind == "action":
        found = [_call(step, False) for step in _steps(_pairs(root.get("runs", (None, None))[1]))]
        return [call for call in found if call]
    found = []
    for _, node in _pairs(root.get("jobs", (None, None))[1]).values():
        job = _pairs(node)
        found += [_call(job, True), *(_call(step, False) for step in _steps(job))]
    return [call for call in found if call]


def _document(repository: str, path: str, ref: str) -> dict | None:
    """One file of a repository at a ref as a mapping; None when the forge has no such file."""
    asked = f"repos/{repository}/contents/{quote(path)}?ref={quote(ref, safe='')}"
    try:
        answer = forge.api(asked)
        text = base64.b64decode(answer["content"]).decode("utf-8")
        doc = yaml.load(text, Loader=ciwf.StrictLoader)
    except forge.ForgeError as err:
        if "HTTP 404" in str(err):
            return None
        raise PinError(str(err)) from err
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as err:
        raise PinError(f"GET {asked}: no readable file in the answer ({err!r})") from err
    if not isinstance(doc, dict):
        raise PinError(f"GET {asked}: not a mapping")
    return doc


def _declared(call: Call) -> set[str] | str:
    """The inputs the target of a call declares, or the reason they cannot be read."""
    name, _, ref = call.uses.partition("@")
    parts = name.split("/")
    if len(parts) < 2 or not ref:
        return "not of the form owner/repository[/path]@ref"
    repository, inside = "/".join(parts[:2]), "/".join(parts[2:])
    if call.workflow:
        doc = _document(repository, inside, ref)
        calling = ciwf.triggers(doc or {}).get("workflow_call")
        if doc is None or calling is None:
            return "no workflow with `on: workflow_call` at that ref, or not readable"
        return set((calling or {}).get("inputs") or {})
    folder = f"{inside}/" if inside else ""
    doc = _document(repository, f"{folder}action.yml", ref)
    if doc is None:
        doc = _document(repository, f"{folder}action.yaml", ref)
    if doc is None:
        return "no action.yml or action.yaml at that ref, or not readable"
    runs = doc.get("runs") if isinstance(doc.get("runs"), dict) else {}
    extra = IMAGE_KEYS if runs.get("using") == "docker" else ()
    return {*(doc.get("inputs") or {}), *extra}


def _inputs(root: Path, documents: list) -> tuple[list[Finding], int, int]:
    """Findings on `with:` keys, the number of outside uses, and the number of keys read."""
    findings, known, uses, keys = [], {}, 0, 0
    for path, kind, _ in documents:
        for call in _calls(root / path, kind):
            if call.uses.startswith(LOCAL):
                continue
            uses += 1
            if (call.uses, call.workflow) not in known:
                known[call.uses, call.workflow] = _declared(call)
            declared = known[call.uses, call.workflow]
            if isinstance(declared, str):
                findings.append(Finding(path, call.line, f"{call.uses}: {declared}"))
                continue
            names = {name.casefold(): name for name in declared}
            keys += len(call.keys)
            for key, line in call.keys.items():
                if key.casefold() in names:
                    continue
                near = difflib.get_close_matches(key.casefold(), list(names), n=1)
                hint = f"; it declares `{names[near[0]]}`" if near else ""
                text = f"{call.uses}: input `{key}` is not declared at that ref{hint}"
                findings.append(Finding(path, line, text))
    return findings, uses, keys


def _tree(root: Path) -> tuple[list, dict[str, str]]:
    documents = ciwf.files(root)
    if not documents:
        raise PinError(f"{root}: no workflow and no action file")
    return documents, {str(root / path): path for path, _, _ in documents}


def listing(root: Path, work: Path) -> int:
    _, named = _tree(root)
    found = _scan(work, named, None)[UNPINNED]
    for finding in sorted(found):
        ciwf.say(f"{finding.path}:{finding.line}: {finding.text}")
    ciwf.say(f"{len(found)} use(s) on a tag or a branch in {len(named)} file(s); {_version()}")
    return 0


def check(root: Path, work: Path, rewrite: bool) -> int:
    documents, named = _tree(root)
    token = _token()
    found = _scan(work, named, token)
    if rewrite:
        _engine_run(work, list(named), token, fix=True)
        found = _scan(work, named, token)
    late, uses, keys = _inputs(root, documents)
    problems = sorted([*found[UNPINNED], *found[COMMENT], *late])
    for finding in problems:
        ciwf.complain(f"FAIL: {finding.path}:{finding.line}: {finding.text}")
    if problems:
        return 1
    ciwf.say(
        f"PASS: {len(named)} file(s), {uses} outside use(s) on a commit, no comment refuted, "
        f"{keys} `with` key(s) declared; {_version()}, online"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("list", "check", "pin"))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory() as work:
            if args.command == "list":
                return listing(args.root, Path(work))
            return check(args.root, Path(work), rewrite=args.command == "pin")
    except (PinError, ciwf.ReadError) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
