# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Caller templates: render one, and check that each renders into a caller of a workflow kept here.

    render.py render --template FILE --context FILE [--root DIR]
    render.py check [--root DIR]

A template under templates/callers is written for the engine: Go text/template
with the delimiters `<%` and `%>`. Until the engine renders templates kept
outside it, this tool reads the part of that syntax a thin caller needs:

    <% .Shared.Commit %>      a field of the context, by its dotted name
    <%/* text */%>            a maintainer note, which renders as nothing
    <%- ... -%>               the trim markers, which remove the white space
                              before or after the action

Anything else between the delimiters (a function, a pipeline, a condition) is
refused, and so is a field the context does not hold. The context of `render`
is a TOML file; `check` renders every template with the sample context below.

`check` then reads the result as a workflow: its one calling job must call a
workflow file of this repository by a full commit id, pass only inputs that
workflow declares, and grant the scopes that workflow's jobs ask for, no more
and no fewer. An input that switches on a host hosts.toml marks `public_only` is
passed as the repository's visibility (PUBLIC below) or as false. A caller that
runs on pull requests also holds the job `result`, in exactly the shape that
reports the calling job's own name and fails unless the call passed; the forge
reports a called workflow's checks only as "<calling job> / <called job>". While
exceptions.yaml holds an entry on the required check's name, the note of every
template must name that entry's issue.

Exit codes: 0 pass, 1 a template fails the check, 3 input error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf
import exceptions
import hosts

TEMPLATES = "templates/callers"
SUFFIX = ".tmpl"
LEFT, RIGHT = "<%", "%>"
SPACE = " \t\r\n"
ENGINE_ISSUE = "cordanaLLM/praetor#1211"
REQUIRED_CHECK = "required-check-is-reported"
FIELD = re.compile(r"\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
CALL = re.compile(
    r"(?P<repository>[\w.-]+/[\w.-]+)/(?P<path>\.github/workflows/[\w.-]+)@(?P<ref>.+)"
)
LEVELS = {"none": 0, "read": 1, "write": 2}
# True in a public repository and false in any other, and false too where the
# event names no repository: the switch fails towards off.
PUBLIC = "${{ github.event.repository.visibility == 'public' }}"
SECRET = re.compile(r"\$\{\{ secrets\.[A-Z][A-Z0-9_]* \}\}")
# The one job beside the call, read by an allow-list: the forge never reports a
# called workflow's checks under the calling job's own name, so this job does.
RESULT = "result"
RESULT_KEYS = ("if", "name", "needs", "permissions", "runs-on", "steps", "timeout-minutes")
STEP_KEYS = ("if", "name", "run", "shell")
ECHO = re.compile(r'echo "[^"$`\\]*"')
GATING = {"pull_request", "pull_request_target", "merge_group"}
LONGEST_MINUTES = 10
SAMPLE = {
    "RunnerTag": "ubuntu-26.04",
    "Shared": {
        "Repository": "owner/shared",
        "Commit": "0123456789abcdef0123456789abcdef01234567",
        "Version": "v1.2.3",
    },
}


def _value(name: str, context: dict, where: str) -> str:
    """The text of one field of the context, as the engine prints it."""
    node: object = context
    for part in name[1:].split("."):
        if not isinstance(node, dict) or part not in node:
            raise ciwf.ReadError(f"{where}: the context holds no field {name}")
        node = node[part]
    if isinstance(node, bool):
        return "true" if node else "false"
    if isinstance(node, (str, int)):
        return str(node)
    raise ciwf.ReadError(f"{where}: field {name} is no text, number or truth value")


def _note(text: str, inner: int, where: str) -> tuple[int, bool]:
    """End of the note that opens at `inner`, and whether it trims what follows."""
    close = text.find("*/", inner)
    if close < 0:
        raise ciwf.ReadError(f"{where}: a note is opened and not closed")
    after = close + 2
    trim = text[after : after + 1] in tuple(SPACE) and text.startswith("-" + RIGHT, after + 1)
    after += 2 if trim else 0
    if not text.startswith(RIGHT, after):
        raise ciwf.ReadError(f"{where}: a note does not end at the closing delimiter")
    return after + len(RIGHT), trim


def _action(text: str, start: int, context: dict, where: str) -> tuple[str, int, bool, bool]:
    """What the action at `start` renders as, where it ends, and its two trim markers."""
    inner = start + len(LEFT)
    trim_before = text[inner : inner + 1] == "-" and text[inner + 1 : inner + 2] in tuple(SPACE)
    inner += 2 if trim_before else 0
    if text.startswith("/*", inner):
        end, trim_after = _note(text, inner, where)
        return "", end, trim_before, trim_after
    end = text.find(RIGHT, inner)
    if end < 0:
        raise ciwf.ReadError(f"{where}: an action is opened and not closed")
    body = text[inner:end]
    trim_after = body.endswith("-") and body[-2:-1] in tuple(SPACE)
    body = (body[:-1] if trim_after else body).strip(SPACE)
    if not FIELD.fullmatch(body):
        raise ciwf.ReadError(
            f"{where}: `{LEFT} {body} {RIGHT}` is neither a field nor a note; this tool renders "
            f"nothing else, the engine will ({ENGINE_ISSUE})"
        )
    return _value(body, context, where), end + len(RIGHT), trim_before, trim_after


def render(text: str, context: dict, name: str) -> str:
    """The template's text with every action replaced; a ReadError names what stops it."""
    if not text:
        raise ciwf.ReadError(f"{name}: the template is empty")
    parts: list[str] = []
    position, strip = 0, False
    for _ in range(text.count(LEFT) + 1):
        start = text.find(LEFT, position)
        literal = text[position:] if start < 0 else text[position:start]
        where = f"{name}:{text.count(chr(10), 0, position) + 1}"
        if RIGHT in literal:
            raise ciwf.ReadError(f"{where}: a closing delimiter is left without its opening one")
        literal = literal.lstrip(SPACE) if strip else literal
        if start < 0:
            parts.append(literal)
            break
        where = f"{name}:{text.count(chr(10), 0, start) + 1}"
        value, position, trim_before, strip = _action(text, start, context, where)
        parts.append(literal.rstrip(SPACE) if trim_before else literal)
        parts.append(value)
    return "".join(parts)


def _called(root: Path, path: str) -> dict | None:
    """The called workflow; None when it is no reusable workflow kept here."""
    if not (root / path).is_file():
        return None
    doc = ciwf.load(root / path)
    return doc if "workflow_call" in ciwf.triggers(doc) else None


def _public_inputs(root: Path, path: str) -> set[str]:
    """Inputs of the called workflow that switch on a host for public repositories only."""
    if not (root / hosts.DECLARATION).is_file():
        return set()
    return hosts.public_inputs(hosts.declaration(root), path)


def _with_problems(job: dict, called: dict, path: str, public: set[str]) -> list[str]:
    """What is wrong with the inputs a caller's job passes."""
    inputs = ciwf.triggers(called)["workflow_call"].get("inputs") or {}
    passed = job.get("with") or {}
    problems = [
        f"the job passes `{key}`, which {path} does not declare"
        for key in sorted(set(passed) - set(inputs))
    ]
    for key, spec in sorted(inputs.items()):
        if (spec or {}).get("required") and key not in passed:
            problems.append(f"the job does not pass `{key}`, which {path} requires")
    for key in sorted(public & set(passed)):
        if passed[key] is not False and passed[key] != PUBLIC:
            written = "true" if passed[key] is True else passed[key]
            problems.append(
                f"the job passes `{key}` as `{written}`; it switches on a host for public "
                f"repositories only and is passed as `{PUBLIC}` or as false"
            )
    return problems


def _asked(doc: dict) -> dict[str, str] | None:
    """Each scope with the highest level a job of the workflow asks for; None without scopes."""
    asked: dict[str, str] = {}
    for job in (doc.get("jobs") or {}).values():
        block = job.get("permissions", doc.get("permissions")) if isinstance(job, dict) else None
        if not isinstance(block, dict):
            return None
        for scope, level in block.items():
            if LEVELS.get(str(level), 0) > LEVELS.get(asked.get(str(scope), "none"), 0):
                asked[str(scope)] = str(level)
    return asked


def _grant_problems(job: dict, doc: dict, called: dict, path: str) -> list[str]:
    """Where the scopes a caller's job grants differ from what the called workflow asks for.

    A scope that is asked for and not granted stops the run before it starts;
    a scope that is granted and not asked for is access nothing needs.
    """
    granted = job.get("permissions", doc.get("permissions"))
    if not isinstance(granted, dict):
        return [
            f"the job's permissions name no scopes: {'<unset>' if granted is None else granted}"
        ]
    asked = _asked(called)
    if asked is None:
        return [f"a job of {path} names no scopes, so the grant cannot be compared"]
    problems = []
    for scope in sorted({*map(str, granted), *asked}):
        have, want = str(granted.get(scope, "none")), asked.get(scope, "none")
        if LEVELS.get(have, 0) > LEVELS.get(want, 0):
            problems.append(f"the job grants `{scope}: {have}`, which {path} does not ask for")
        if LEVELS.get(have, 0) < LEVELS.get(want, 0):
            problems.append(f"the job does not grant `{scope}: {want}`, which {path} asks for")
    return problems


def _secret_problems(job: dict, called: dict, path: str) -> list[str]:
    """What is wrong with the secrets a caller's job passes.

    A caller passes by name exactly the secrets the called workflow declares,
    each as one secret of the calling repository. `inherit` would hand over
    every secret the caller holds, so it is refused.
    """
    passed = job.get("secrets", {})
    if not isinstance(passed, dict):
        return [f"the job passes secrets as `{passed}`; it names each one {path} declares"]
    declared = ciwf.triggers(called)["workflow_call"].get("secrets") or {}
    problems = [
        f"the job passes the secret `{key}`, which {path} does not declare"
        for key in sorted(set(passed) - set(declared))
    ]
    for key, spec in sorted(declared.items()):
        if (spec or {}).get("required") and key not in passed:
            problems.append(f"the job does not pass the secret `{key}`, which {path} requires")
    problems += [
        f"the job passes the secret `{key}` as `{value}`, not as one secret of the repository"
        for key, value in sorted(passed.items())
        if not SECRET.fullmatch(str(value))
    ]
    return problems


def _job_problems(job: dict, doc: dict, root: Path) -> list[str]:
    """What is wrong with the one job of a rendered caller."""
    call = CALL.fullmatch(str(job.get("uses", "")))
    if call is None:
        return ["the job does not call a workflow of a repository by path and reference"]
    problems = []
    if call["repository"] != SAMPLE["Shared"]["Repository"]:
        problems.append(f"the job calls {call['repository']}, not the shared repository's field")
    if call["ref"] != SAMPLE["Shared"]["Commit"]:
        problems.append("the called workflow is not named by the commit field alone")
    path = call["path"]
    called = _called(root, path)
    if called is None:
        return [*problems, f"{path} is no reusable workflow of this repository"]
    problems += _with_problems(job, called, path, _public_inputs(root, path))
    problems += _grant_problems(job, doc, called, path)
    problems += _secret_problems(job, called, path)
    return problems


def _step_problems(step: object, key: str) -> list[str]:
    """What keeps the one step of job `result` from failing exactly when the call did not pass."""
    if not isinstance(step, dict) or sorted(step) != sorted(STEP_KEYS):
        return [f"the step of job `{RESULT}` has keys other than {', '.join(STEP_KEYS)}"]
    problems = []
    if step["if"] != f"needs.{key}.result != 'success'":
        problems.append(
            f"the step of job `{RESULT}` does not run on `needs.{key}.result != 'success'`"
        )
    if step["shell"] != "bash":
        problems.append(f"the step of job `{RESULT}` does not run in bash")
    lines = str(step["run"]).strip().split("\n")
    if lines[-1] != "exit 1" or not all(ECHO.fullmatch(line) for line in lines[:-1]):
        problems.append(f"the step of job `{RESULT}` does more than print a line and `exit 1`")
    return problems


def _result_problems(jobs: dict, key: str, doc: dict) -> list[str]:
    """What is wrong with the job that reports the calling job's own name."""
    name = jobs[key].get("name", key)
    result = jobs.get(RESULT)
    if result is None:
        if not GATING & set(ciwf.triggers(doc)):
            return []
        return [
            f"the caller runs on pull requests and no job `{RESULT}` reports `{name}`; the "
            f"forge reports the calling job only as `{name} / <called job>`"
        ]
    if not isinstance(result, dict) or sorted(result) != sorted(RESULT_KEYS):
        return [f"job `{RESULT}` has keys other than {', '.join(RESULT_KEYS)}"]
    wanted = {"name": name, "needs": key, "if": "always()", "permissions": {}}
    problems = [
        f"job `{RESULT}` has `{field}: {result[field]}`, not `{value}`"
        for field, value in wanted.items()
        if result[field] != value
    ]
    minutes = result["timeout-minutes"]
    if type(minutes) is not int or not 1 <= minutes <= LONGEST_MINUTES:
        problems.append(f"job `{RESULT}` has no timeout of 1 to {LONGEST_MINUTES} minutes")
    steps = result["steps"]
    if not isinstance(steps, list) or len(steps) != 1:
        return [*problems, f"job `{RESULT}` has not exactly one step"]
    return problems + _step_problems(steps[0], key)


def caller_problems(text: str, root: Path) -> list[str]:
    """What keeps a rendered text from being a thin caller of a workflow kept in `root`."""
    try:
        doc = yaml.load(text, Loader=ciwf.StrictLoader)
    except yaml.YAMLError as err:
        return [f"the result is no YAML: {err}"]
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    calls = [key for key in jobs if key != RESULT] if isinstance(jobs, dict) else []
    if len(calls) != 1:
        return [f"the result is no workflow with exactly one job besides `{RESULT}`"]
    job = jobs[calls[0]]
    if not isinstance(job, dict) or "steps" in job:
        return ["the job has steps of its own; a caller only calls"]
    problems = _job_problems(job, doc, root) + _result_problems(jobs, calls[0], doc)
    if f"@{SAMPLE['Shared']['Commit']} # {SAMPLE['Shared']['Version']}\n" not in text:
        problems.append("the version field is not written as a comment behind the commit")
    return problems


def _unnamed(source: str, root: Path) -> list[str]:
    """Open issues on the required check's name that a template's note does not name."""
    return [
        f"the note does not name {issue}, the open exception on the required check's name"
        for issue in exceptions.issues(root, REQUIRED_CHECK)
        if issue not in source
    ]


def check(root: Path) -> int:
    folder = root / TEMPLATES
    names = sorted(path for path in folder.glob(f"*{SUFFIX}")) if folder.is_dir() else []
    if not names:
        ciwf.complain(f"ERROR: no template under {TEMPLATES}")
        return 3
    failed = False
    for path in names:
        name = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            problems = _unnamed(source, root)
            problems += caller_problems(render(source, SAMPLE, name), root)
            lines = [f"{name}: {problem}" for problem in problems]
        except (OSError, UnicodeDecodeError) as err:
            ciwf.complain(f"ERROR: {name}: {err}")
            return 3
        except ciwf.ReadError as err:
            lines = [str(err)]
        for line in lines:
            ciwf.complain(f"FAIL: {line}")
        failed = failed or bool(lines)
    if failed:
        return 1
    ciwf.say(
        f"PASS: {len(names)} caller template(s), each renders into a caller of a workflow here"
    )
    return 0


def _render(template: Path, context: Path) -> int:
    try:
        values = tomllib.loads(context.read_text(encoding="utf-8"))
        text = render(template.read_text(encoding="utf-8"), values, str(template))
    except (OSError, ValueError, ciwf.ReadError) as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    sys.stdout.write(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("render", "check"))
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--template", type=Path)
    parser.add_argument("--context", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        return check(args.root)
    if args.template is None or args.context is None:
        ciwf.complain("ERROR: render needs --template and --context")
        return 3
    return _render(args.template, args.context)


if __name__ == "__main__":
    sys.exit(main())
