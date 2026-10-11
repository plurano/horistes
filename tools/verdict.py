# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Same-verdict check: the caller of a shared workflow judges like the job it replaces.

    verdict.py --map FILE (--runs FILE | --live [--record FILE])
               [--root DIR] [--called-root DIR] [--json FILE] [--markdown FILE]

During a trial the old job and the new one run side by side. The map names
the pairs (old check name, new check name) and the planted cases; the run
data comes from a recorded file or from the forge (tools/forge.py). One row
per subject and pair:

    equal         both pass, or both fail (on a planted failing case: both
                  with the case's reason marker)
    wrong-reason  both fail on a planted case, one without the marker
    differ        one passes and the other fails, or only one of them ran,
                  or only one stopped on a draft
    no-evidence   neither ran
    invalid       a check was cancelled, is still running or is absent
    draft         both stopped on a draft pull request
    flaky         attempts of one check on one subject disagree

The trial passes when no row differs, no check of a watched workflow is
left unnamed, every pair has 10 equal rows with an ordinary pass among
them, every planted case shows as planted, at most 2 rows are flaky (none
on a planted case), and the new job asks for no write scope, secret, host,
runner kind or trigger loss the old one did not have.

A planted case is a commit ([[planted]]: both jobs of the family's pair
must fail, or pass, on that commit) or a fixture ([[fixture]]: a pair of
jobs of its own that run the old and the new form on a planted folder of
the repository, and must fail, or pass, on every subject). A fixture needs
one equal row with the verdict it names and none against it; the 10 rows
and the ordinary pass are asked of the family's pair, not of the fixture.

A pair of class R is judged by its verdicts alone. A pair of class U is
a job that passes whatever it finds and says it in a result file it uploads
(a scorecard run): where both jobs pass, the checks and findings of the two
files are compared (tools/results.py), and a row is

    equal         both results hold the same checks and the same findings
    differ        they do not, or only one job uploaded a result
    no-evidence   neither uploaded one, both hold no check, or both failed
    invalid       a result has expired on the forge

Its planted cases are commits: one where a finding must show in both
results (expect FINDING), one where it must show in neither (expect
CLEAN), each with a marker that is searched in the findings.

What the side conditions read: the old workflow and the new caller below
--root, the called workflow below --called-root (this repository, whose
hosts.toml gives the forge's hosts and the allowed actions). Triggers are
compared by event name, pull-request events by their types as well; branch
and path filters are not compared. The hosts of the called workflow itself
are judged by tools/hosts.py in this repository's gate.

Exit codes: 0 pass; 1 a verdict, a reason or a side condition differs;
2 evidence insufficient; 3 input or data error (unnamed check, absent
check, unknown conclusion, unreadable map, forge error, a result that is
unreadable or not recorded). An error is never read as equal.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf
import forge
import hosts
import results

# Conclusions of a job, from the forge's REST reference (version 2022-11-28),
# plus `stale`, which a check run can carry. Any other value is an error.
VERDICTS = {
    "success": "PASS",
    "failure": "FAIL",
    "timed_out": "FAIL",
    "skipped": "NOT-RUN",
    "neutral": "NOT-RUN",
    "cancelled": "INVALID",
    "stale": "INVALID",
    "action_required": "INVALID",
}
RUNNING = ("queued", "in_progress", "waiting", "requested", "pending")
DECIDED = ("PASS", "FAIL")
# Per class of a pair: what a planted case may ask for, and the words of the report.
EXPECTED = {"R": ("FAIL", "PASS"), "U": ("FINDING", "CLEAN")}
WORDS = {"FAIL": "fail", "PASS": "pass", "FINDING": "show the finding", "CLEAN": "stay clean"}
MIN_EQUAL = 10
MAX_FLAKY = 2
STATES = ("equal", "differ", "wrong-reason", "no-evidence", "invalid", "draft", "flaky")
RESULT = {
    0: "PASS",
    1: "FAIL: a verdict, a reason or a side condition differs",
    2: "INSUFFICIENT: the evidence does not carry the switch",
    3: "ERROR: input or data error, nothing is judged equal",
}

COMMIT = re.compile(r"^[0-9a-f]{40}$")
HOSTED = re.compile(r"^(?:ubuntu|windows|macos)-")
INPUT = re.compile(r"^\$\{\{\s*inputs\.([\w-]+)\s*\}\}$")
SECRET = re.compile(r"\bsecrets\.([A-Za-z_][A-Za-z0-9_]*)")
EXPRESSION = re.compile(r"(\$\{\{.*?\}\})")
PULL_REQUEST_TYPES = ("opened", "synchronize", "reopened")


@dataclass(frozen=True)
class Pair:
    family: str
    old: str
    new: str
    old_workflow: str
    new_workflow: str
    called_workflow: str
    draft_stop: str
    runner: str
    # Set on a fixture only: the verdict both jobs must reach on every subject.
    expect: str = ""
    marker: re.Pattern | None = None
    # Class U names the artifact each job uploads its result as.
    kind: str = "R"
    old_artifact: str = ""
    new_artifact: str = ""


@dataclass(frozen=True)
class Planted:
    family: str
    commit: str
    expect: str
    marker: re.Pattern | None


@dataclass(frozen=True)
class Trial:
    repository: str
    branches: tuple[str, ...]
    pairs: tuple[Pair, ...]
    planted: tuple[Planted, ...]


@dataclass(frozen=True)
class Side:
    """What one check says about one subject."""

    verdict: str
    flaky: bool = False
    failed_steps: tuple[str, ...] = ()
    text: str = ""
    seconds: float | None = None
    result: object = None


def _text(table: dict, key: str, where: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or (default is None and not value.strip()):
        raise ciwf.ReadError(f"{where}: `{key}` must be a non-empty string")
    return value


def _pair(entry: object, where: str) -> Pair:
    if not isinstance(entry, dict):
        raise ciwf.ReadError(f"{where}: not a table")
    kind = entry.get("class")
    if kind not in EXPECTED:
        raise ciwf.ReadError(
            f"{where}: class {kind!r} is not judged here; "
            "only class R (read-only verdict) and class U (uploaded result) are"
        )
    runner = _text(entry, "runner", where, "")
    if runner not in ("", "hosted", "self-hosted"):
        raise ciwf.ReadError(f"{where}: `runner` is hosted or self-hosted")
    uploads = None if kind == "U" else ""
    return Pair(
        kind=kind,
        old_artifact=_text(entry, "old_artifact", where, uploads),
        new_artifact=_text(entry, "new_artifact", where, uploads),
        family=_text(entry, "family", where),
        old=_text(entry, "old", where),
        new=_text(entry, "new", where),
        old_workflow=_text(entry, "old_workflow", where),
        new_workflow=_text(entry, "new_workflow", where),
        called_workflow=_text(entry, "called_workflow", where, ""),
        draft_stop=_text(entry, "draft_stop", where, ""),
        runner=runner,
    )


def _expectation(entry: dict, where: str, kind: str) -> tuple[str, re.Pattern | None]:
    """What a planted case must show, and the marker of a failure's reason or of a finding."""
    expect = entry.get("expect")
    if expect not in EXPECTED[kind]:
        allowed = " or ".join(sorted(EXPECTED[kind], reverse=True))
        raise ciwf.ReadError(f"{where}: `expect` is {allowed} in class {kind}")
    if expect == "PASS":
        return expect, None
    try:
        return expect, re.compile(_text(entry, "marker", where))
    except re.error as err:
        raise ciwf.ReadError(f"{where}: `marker` is no pattern: {err}") from err


def _planted(entry: object, where: str, kinds: dict[str, str]) -> Planted:
    if not isinstance(entry, dict):
        raise ciwf.ReadError(f"{where}: not a table")
    commit = _text(entry, "commit", where)
    if not COMMIT.match(commit):
        raise ciwf.ReadError(f"{where}: `commit` must be a full commit id")
    family = _text(entry, "family", where)
    if family not in kinds:
        raise ciwf.ReadError(f"{where}: planted case for family {family!r} has no pair")
    expect, marker = _expectation(entry, where, kinds[family])
    return Planted(family, commit, expect, marker)


def _fixture(entry: object, where: str) -> Pair:
    """A pair of jobs that run on a planted folder, with the verdict both must reach."""
    pair = _pair(entry, where)
    if pair.kind != "R":
        raise ciwf.ReadError(f"{where}: a fixture is judged in class R only")
    expect, marker = _expectation(entry, where, pair.kind)
    return replace(pair, expect=expect, marker=marker)


def _kinds(pairs: tuple[Pair, ...], path: Path) -> dict[str, str]:
    """The class of every family; a family holds pairs of one class."""
    kinds: dict[str, str] = {}
    for pair in pairs:
        if kinds.setdefault(pair.family, pair.kind) != pair.kind:
            raise ciwf.ReadError(f"{path}: family {pair.family!r} holds pairs of two classes")
    return kinds


def read_map(path: Path) -> Trial:
    """The pair map of a trial; anything unclear in it is an error."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise ciwf.ReadError(f"{path}: {err}") from err
    pairs = tuple(_pair(e, f"{path}: pair {i}") for i, e in enumerate(data.get("pair") or [], 1))
    if not pairs:
        raise ciwf.ReadError(f"{path}: no [[pair]]")
    fixtures = tuple(
        _fixture(e, f"{path}: fixture {i}") for i, e in enumerate(data.get("fixture") or [], 1)
    )
    names = [name for pair in pairs + fixtures for name in (pair.old, pair.new)]
    if len(set(names)) != len(names):
        raise ciwf.ReadError(f"{path}: a check name stands in two places")
    kinds = _kinds(pairs, path)
    planted = tuple(
        _planted(e, f"{path}: planted {i}", kinds)
        for i, e in enumerate(data.get("planted") or [], 1)
    )
    for case in fixtures:
        if case.family not in kinds:
            raise ciwf.ReadError(f"{path}: planted case for family {case.family!r} has no pair")
    branches = data.get("branches") or []
    if not isinstance(branches, list) or not all(isinstance(b, str) and b for b in branches):
        raise ciwf.ReadError(f"{path}: `branches` must list branch names")
    return Trial(_text(data, "repository", str(path)), tuple(branches), pairs + fixtures, planted)


def _verdict(attempt: object, where: str, errors: list[str]) -> str:
    """Verdict of one attempt; an unknown status or conclusion is an error, never a pass."""
    if not isinstance(attempt, dict):
        errors.append(f"{where}: attempt is not a record")
        return "INVALID"
    status = attempt.get("status")
    if status != "completed":
        if status not in RUNNING:
            errors.append(f"{where}: unknown status {status!r}")
        return "INVALID"
    conclusion = attempt.get("conclusion")
    if conclusion not in VERDICTS:
        errors.append(f"{where}: unknown conclusion {conclusion!r}")
        return "INVALID"
    return VERDICTS[conclusion]


def _side(check: dict | None, where: str, errors: list[str]) -> Side:
    """The verdict of a check's latest attempt, and whether its attempts disagree."""
    if check is None:
        return Side("ABSENT")
    recorded = [a for a in check.get("attempts") or [] if isinstance(a, dict)]
    attempts = sorted(recorded, key=lambda a: a.get("attempt") or 0)
    if not attempts:
        errors.append(f"{where}: no attempt recorded")
        return Side("INVALID")
    verdicts = [_verdict(attempt, where, errors) for attempt in attempts]
    last = attempts[-1]
    steps = tuple(str(step) for step in last.get("failed_steps") or [])
    notes = [str(note) for note in last.get("annotations") or []]
    flaky = len({verdict for verdict in verdicts if verdict in DECIDED}) > 1
    seconds = last.get("seconds")
    return Side(
        verdicts[-1],
        flaky,
        steps,
        "\n".join([*steps, *notes]),
        float(seconds) if isinstance(seconds, int | float) else None,
        check.get("result"),
    )


def _state(old: Side, new: Side, pair: Pair, case: Planted | None) -> tuple[str, str]:
    """State of one row, and whether the reason marker was found (yes, no, or - when not asked)."""
    if old.flaky or new.flaky:
        return "flaky", "-"
    if not {old.verdict, new.verdict} <= {"PASS", "FAIL", "NOT-RUN"}:
        return "invalid", "-"
    stopped = [bool(pair.draft_stop) and pair.draft_stop in s.failed_steps for s in (old, new)]
    if all(stopped):
        return "draft", "-"
    if any(stopped) or old.verdict != new.verdict:
        return "differ", "-"
    if old.verdict == "NOT-RUN":
        return "no-evidence", "-"
    if old.verdict == "FAIL" and case is not None and case.expect == "FAIL":
        if all(case.marker.search(side.text) for side in (old, new)):
            return "equal", "yes"
        return "wrong-reason", "no"
    return "equal", "-"


def _uploaded(
    old: Side, new: Side, case: Planted | None, where: str, errors: list[str]
) -> tuple[str, str, str]:
    """Class U, both jobs decided the same: state, marker and note from their results."""
    if old.verdict != "PASS":
        return "no-evidence", "-", "both jobs failed, no result is compared"
    told = [
        (side, results.problem(given)) for side, given in (("old", old.result), ("new", new.result))
    ]
    if any(reason for _, reason in told):
        errors.extend(f"{where}: {side} job: {reason}" for side, reason in told if reason)
        return "invalid", "-", ""
    return results.compare(old.result, new.result, case.marker if case else None)


def _key(subject: dict) -> str:
    commit = str(subject.get("commit"))[:12]
    return f"{commit} {subject.get('event')} {subject.get('ref')} #{subject.get('occurrence', 1)}"


def _case(pair: Pair, subject: dict, trial: Trial) -> Planted | None:
    """The planted case a row shows: every subject of a fixture, or a planted commit."""
    commit = subject.get("commit")
    if pair.expect:
        return Planted(pair.family, str(commit), pair.expect, pair.marker)
    cases = (c for c in trial.planted if c.family == pair.family and c.commit == commit)
    return next(cases, None)


def _row(subject: dict, pair: Pair, checks: dict, trial: Trial, errors: list[str]) -> dict | None:
    """The row of one pair on one subject; no row when neither check is recorded."""
    key = _key(subject)
    found = {name: checks.get(name) for name in (pair.old, pair.new)}
    if found[pair.old] is None and found[pair.new] is None:
        return None
    for name, check in found.items():
        if check is None:
            errors.append(f"{key}: check {name!r} is absent while its counterpart ran")
    case = _case(pair, subject, trial)
    old = _side(found[pair.old], f"{key}: {pair.old}", errors)
    new = _side(found[pair.new], f"{key}: {pair.new}", errors)
    state, marker = _state(old, new, pair, case)
    note = ""
    if pair.kind == "U" and state == "equal":
        state, marker, note = _uploaded(old, new, case, f"{key}: {pair.new}", errors)
    return {
        "subject": key,
        "id": [subject.get(part) for part in ("commit", "event", "ref", "occurrence")],
        "commit": subject.get("commit"),
        "pair": f"{pair.old} -> {pair.new}",
        "old": old.verdict,
        "new": new.verdict,
        "state": state,
        "marker": marker,
        "planted": case.expect if case else "",
        "old_seconds": old.seconds,
        "new_seconds": new.seconds,
        "note": note,
    }


def judge(trial: Trial, data: object) -> tuple[list[dict], list[str], list[str]]:
    """Rows, data errors and unnamed checks of a trial's run data."""
    if not isinstance(data, dict) or not isinstance(data.get("subjects"), list):
        raise ciwf.ReadError("run data: no list `subjects`")
    rows: list[dict] = []
    errors = [f"data source: {error}" for error in data.get("errors") or []]
    unmapped: list[str] = []
    watched = {path for pair in trial.pairs for path in (pair.old_workflow, pair.new_workflow)}
    named = {name for pair in trial.pairs for name in (pair.old, pair.new)}
    for subject in data["subjects"]:
        if not isinstance(subject, dict):
            raise ciwf.ReadError("run data: a subject is not a record")
        checks: dict[str, dict] = {}
        for check in subject.get("checks") or []:
            name = str(check.get("name"))
            if name in checks:
                errors.append(f"{_key(subject)}: check {name!r} is recorded twice")
            checks[name] = check
            where = f"check {name!r} of {check.get('workflow')}"
            if check.get("workflow") in watched and name not in named and where not in unmapped:
                unmapped.append(where)
        for pair in trial.pairs:
            row = _row(subject, pair, checks, trial, errors)
            if row is not None:
                rows.append(row)
    return rows, errors, unmapped


def _fixture_gaps(pair: Pair, name: str, mine: list[dict], equal: list[dict]) -> list[str]:
    """What is missing before a fixture shows its planted case: one row as named, none against."""
    word = pair.expect.lower()
    shown = [row for row in equal if row["old"] == pair.expect]
    gaps = []
    if not shown:
        gaps.append(f"{name}: the fixture has no equal row where both {word}")
    if len(shown) < len(equal):
        gaps.append(f"{name}: {len(equal) - len(shown)} equal row(s) where both do not {word}")
    if any(row["state"] == "flaky" for row in mine):
        gaps.append(f"{name}: a planted case is flaky")
    return gaps


def _shown(row: dict, case: Planted) -> bool:
    """Whether an equal row shows a planted case as planted."""
    if case.expect in DECIDED:
        return row["old"] == case.expect
    return row["marker"] == ("yes" if case.expect == "FINDING" else "no")


def evidence_gaps(pair: Pair, rows: list[dict], trial: Trial) -> list[str]:
    """What is missing before the rows of one pair carry the switch (rules 3 to 5)."""
    name = f"{pair.old} -> {pair.new}"
    mine = [row for row in rows if row["pair"] == name]
    equal = [row for row in mine if row["state"] == "equal"]
    if pair.expect:
        return _fixture_gaps(pair, name, mine, equal)
    gaps = []
    subjects = {tuple(row["id"]) for row in equal}
    if len(subjects) < MIN_EQUAL:
        gaps.append(f"{name}: {len(subjects)} equal rows on distinct subjects, {MIN_EQUAL} needed")
    if not any(row["old"] == "PASS" and not row["planted"] for row in equal):
        gaps.append(f"{name}: no equal row where both pass on a subject that is not planted")
    cases = [case for case in trial.planted if case.family == pair.family]
    fixtures = [other for other in trial.pairs if other.family == pair.family and other.expect]
    for expect in EXPECTED[pair.kind]:
        if not any(case.expect == expect for case in cases + fixtures):
            gaps.append(f"{name}: the map holds no planted case that must {WORDS[expect]}")
    for case in cases:
        if not any(row["commit"] == case.commit and _shown(row, case) for row in equal):
            gaps.append(
                f"{name}: planted case {case.commit[:12]} has no equal row "
                f"where both {WORDS[case.expect]}"
            )
    flaky = [row for row in mine if row["state"] == "flaky"]
    if len(flaky) > MAX_FLAKY:
        gaps.append(f"{name}: {len(flaky)} flaky rows, at most {MAX_FLAKY} allowed")
    if any(row["planted"] for row in flaky):
        gaps.append(f"{name}: a planted case is flaky")
    return gaps


def _job(doc: dict, check: str, path: str) -> dict:
    """The job of a workflow that the forge reports under a check name."""
    for ident, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        name = str(job.get("name", ident))
        pattern = "".join(
            ".+" if part.startswith("${{") else re.escape(part) for part in EXPRESSION.split(name)
        )
        if check == name or check.startswith(f"{name} (") or re.fullmatch(pattern, check):
            return job
    raise ciwf.ReadError(f"{path}: no job is reported as {check!r}")


def _label(caller: dict, called: dict | None, called_doc: dict | None) -> object:
    """The runner label of the new job: its own, or the called job's with the caller's input."""
    if "uses" not in caller:
        return caller.get("runs-on")
    if called is None or called_doc is None:
        return None
    label = called.get("runs-on")
    named = INPUT.match(label) if isinstance(label, str) else None
    if not named:
        return label
    given = (caller.get("with") or {}).get(named.group(1))
    if given is not None:
        return given
    inputs = (ciwf.triggers(called_doc).get("workflow_call") or {}).get("inputs") or {}
    return (inputs.get(named.group(1)) or {}).get("default")


def _kind(label: object) -> str:
    """hosted, self-hosted, or unreadable for a label that is computed or absent."""
    labels = [label] if isinstance(label, str) else label
    if not isinstance(labels, list) or not labels:
        return "unreadable"
    if not all(isinstance(item, str) and "${{" not in item for item in labels):
        return "unreadable"
    hosted = "self-hosted" not in labels and all(HOSTED.match(item) for item in labels)
    return "hosted" if hosted else "self-hosted"


def _events(doc: dict) -> set[str]:
    """Events a workflow runs on; pull-request events are named with each of their types."""
    events = set()
    for event, settings in ciwf.triggers(doc).items():
        if not event.startswith("pull_request"):
            events.add(event)
            continue
        types = (settings.get("types") if isinstance(settings, dict) else None) or list(
            PULL_REQUEST_TYPES
        )
        events.update(
            f"{event} ({kind})" for kind in ([types] if isinstance(types, str) else types)
        )
    return events


def _secrets(job: dict) -> set[str]:
    if job.get("secrets") == "inherit":
        return {"<inherit>"}
    return {name for text in ciwf.strings(job) for name in SECRET.findall(text)}


def side_conditions(pair: Pair, root: Path, called_root: Path) -> tuple[list[str], list[str]]:
    """Violations that fail the trial, and lines that are only reported."""
    old_doc = ciwf.load(root / pair.old_workflow)
    new_doc = ciwf.load(root / pair.new_workflow)
    old = _job(old_doc, pair.old, pair.old_workflow)
    outer, _, inner = pair.new.partition(" / ")
    caller = _job(new_doc, outer if inner else pair.new, pair.new_workflow)
    called_doc = called = None
    if pair.called_workflow:
        called_doc = ciwf.load(called_root / pair.called_workflow)
        called = _job(called_doc, inner or pair.new, pair.called_workflow)
    declared = hosts.declaration(called_root)
    name = f"{pair.old} -> {pair.new}"
    problems = []

    def scopes(doc: dict, job: dict) -> set[str]:
        return set(ciwf.write_scopes(job.get("permissions", doc.get("permissions"))))

    for scope in sorted(scopes(new_doc, caller) - scopes(old_doc, old)):
        problems.append(f"{name}: the new job asks to write {scope}, the old one did not")
    for secret in sorted(_secrets(caller) - _secrets(old)):
        problems.append(f"{name}: the new job receives the secret {secret}, the old one did not")
    for event in sorted(_events(old_doc) - _events(new_doc)):
        problems.append(f"{name}: the new workflow does not run on {event}")
    was, now = _kind(old.get("runs-on")), _kind(_label(caller, called, called_doc))
    wanted = pair.runner or was
    if "unreadable" in (wanted, now) or wanted != now:
        problems.append(f"{name}: runner kind of the new job is {now}, {wanted} expected")
    new_hosts, unknown = hosts.found_hosts(new_doc, declared.get("actions", {}))
    old_hosts, _ = hosts.found_hosts(old_doc, declared.get("actions", {}))
    for host in sorted(set(new_hosts) - set(declared["forge"]) - set(old_hosts)):
        problems.append(f"{name}: the new workflow names the host {host}, the old one did not")
    for action in unknown:
        if "/.github/workflows/" not in action:
            problems.append(
                f"{name}: the new workflow uses {action}, which is not an allowed action"
            )
    if pair.expect:
        return problems, [f"{name}: fixture, both must {pair.expect.lower()} on every subject"]
    told = [f"{name}: required check moves from {pair.old!r} to {pair.new!r}"]
    if pair.kind == "U":
        told.append(
            f"{name}: results compared, artifact {pair.old_artifact!r} "
            f"against {pair.new_artifact!r}"
        )
    return problems, told


def wall_time(pair: Pair, rows: list[dict]) -> str:
    """Report line: median duration of the new job against the old one's, where both passed."""
    name = f"{pair.old} -> {pair.new}"
    timed = [
        (row["old_seconds"], row["new_seconds"])
        for row in rows
        if row["pair"] == name and row["old"] == row["new"] == "PASS"
    ]
    timed = [(old, new) for old, new in timed if old is not None and new is not None]
    if not timed:
        return f"{name}: no durations recorded"
    old = statistics.median(old for old, _ in timed)
    new = statistics.median(new for _, new in timed)
    limit = old * 1.25 + 30
    word = "within" if new <= limit else "OVER"
    return f"{name}: median {new:.0f} s against {old:.0f} s, {word} the limit of {limit:.0f} s"


def exit_code(rows: list[dict], errors: list, unmapped: list, problems: list, gaps: list) -> int:
    if errors or unmapped:
        return 3
    if problems or any(row["state"] in ("differ", "wrong-reason") for row in rows):
        return 1
    return 2 if gaps else 0


def _table(rows: list[dict]) -> list[str]:
    lines = [
        "| Subject | Pair | Old | New | State | Marker |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        subject = row["subject"] + (f" (planted {row['planted']})" if row["planted"] else "")
        lines.append(
            f"| {subject} | {row['pair']} | {row['old']} | {row['new']} "
            f"| {row['state']} | {row['marker']} |"
        )
    return lines


def _run_data(args: argparse.Namespace, trial: Trial) -> tuple[object, str]:
    """The run data and the path it came by: a recorded file, or the forge."""
    if bool(args.runs) == bool(args.live):
        raise ciwf.ReadError("give exactly one of --runs FILE and --live")
    if args.runs:
        try:
            return json.loads(args.runs.read_text(encoding="utf-8")), f"recorded file {args.runs}"
        except (OSError, ValueError) as err:
            raise ciwf.ReadError(f"{args.runs}: {err}") from err
    if not trial.branches:
        raise ciwf.ReadError("--live needs `branches` in the map")
    watched = {path for pair in trial.pairs for path in (pair.old_workflow, pair.new_workflow)}
    uploads = {
        name: artifact
        for pair in trial.pairs
        if pair.kind == "U"
        for name, artifact in ((pair.old, pair.old_artifact), (pair.new, pair.new_artifact))
    }
    data = forge.collect(trial.repository, list(trial.branches), watched, uploads)
    if args.record:
        args.record.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return data, f"live forge, {trial.repository}, branches {', '.join(trial.branches)}"


def _report(args: argparse.Namespace, source: str, rows: list[dict], found: dict) -> int:
    """Write the table, the findings and the summary line; return the exit code."""
    code = exit_code(rows, found["error"], found["unmapped"], found["differs"], found["gap"])
    counts = {state: sum(1 for row in rows if row["state"] == state) for state in STATES}
    counts["unmapped"] = len(found["unmapped"])
    summary = (
        f"{RESULT[code]} | "
        + ", ".join(f"{state} {count}" for state, count in counts.items())
        + f" | source: {source}"
    )
    lines = _table(rows) + [""]
    for kind in ("error", "unmapped", "differs", "gap", "report"):
        lines += [f"- {kind}: {line}" for line in found[kind]]
    lines += ["", summary]
    if args.markdown:
        args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.json:
        record = {"exit": code, "source": source, "counts": counts, "rows": rows, "findings": found}
        args.json.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    for line in lines[:-1]:
        ciwf.say(line)
    (ciwf.say if code == 0 else ciwf.complain)(summary)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--record", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--called-root", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    found: dict[str, list[str]] = {"differs": [], "report": []}
    try:
        trial = read_map(args.map)
        data, source = _run_data(args, trial)
        rows, found["error"], found["unmapped"] = judge(trial, data)
        for pair in trial.pairs:
            problems, told = side_conditions(pair, args.root, args.called_root or args.root)
            found["differs"] += problems
            found["report"] += told if pair.expect else [*told, wall_time(pair, rows)]
    except ciwf.ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3
    found["differs"] += [
        f"{row['subject']}: {row['pair']} is {row['state']} ({row['old']} against {row['new']})"
        + (f": {row['note']}" if row["note"] else "")
        for row in rows
        if row["state"] in ("differ", "wrong-reason")
    ]
    found["gap"] = [g for pair in trial.pairs for g in evidence_gaps(pair, rows, trial)]
    return _report(args, source, rows, found)


if __name__ == "__main__":
    sys.exit(main())
