# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Release read-back: a published release is verified the way a stranger does it.

    readback.py check --params FILE
    readback.py verify --params FILE --tag TAG [--work DIR]
    readback.py verify --params FILE --tag TAG --local DIR [--work DIR]

No request and no command of a run carries credentials (tools/stranger.py).
The commit of the tag is asked from the forge with `git ls-remote`; every
signature and every provenance attestation must then come from the signer
workflow of the repository, run on that tag at that commit.

With `--local DIR` the assets are the files directly in DIR instead of the
assets of a published release, so a release workflow reads its build files
back before the draft exists (ADR-0033). A link, a subdirectory or a name that
is not plain stops the run. Nothing else changes: the tag must be pushed and
the files attested, and every check and control runs as for a published
release.

The parameter file is the only thing a repository writes:

    repository = "owner/name"
    signer = ".github/workflows/release.yml"   # the workflow that signs
    other_workflows = [".github/workflows/ci.yml"]   # optional, see controls
    unverified = ["notes-{version}.txt"]   # assets nothing here verifies
    images = ["ghcr.io/owner/name:{version}"]

    [blobs]
    checksums = "checksums.txt"
    signature = "checksums.txt.sigstore.json"

`{tag}` is the tag and `{version}` the tag without a leading `v`. A file
names `blobs`, `images` or both.

Blobs. The assets of the release are exactly the checksums file, its
signature, the files the checksums list and the files declared `unverified`;
one more or one less is a failure, and so is a declared file that the
checksums list. Every listed file matches its digest. The
signature (a bundle in the new format) verifies the checksums. For every
listed file the forge serves a provenance attestation that names its digest,
and that attestation verifies the file.

Images. The signature verifies the reference; the answer must hold an entry
of the signature type, because the tool lists an image's provenance
attestation in the same answer. The provenance attestation verifies the
image by the digest the signature named, and its statement names that
digest. Of an image with several platforms the index is verified, not each
platform's image.

Controls. Each accepted check is repeated with one expectation changed, and
every such call must be refused: another tag, the branch main, the signer's
path in another repository, another commit, another repository or ref in
the certificate, another issuer, and another workflow of the repository in
the signer's place (`other_workflows`, for a signer that is called by one).
For blobs also a file with one byte appended, a file that is no subject, and
the signature bundle offered as provenance. A control that gets no answer
(a command that does not start or runs out of time) is a failure, never a
refusal.

Lines: `PASS accept`, `PASS refuse`, `FAIL ...`, `NOTE ...`. Exit codes:
0 every line passed, 1 a check failed or a control was accepted, 3 the
parameters, the tag or an answer of the forge could not be read.

Read with cosign v2.6.3. The forge's API is asked once for the assets of the
release and once for each attestation the listed files need (one, where one
attestation names them all); without credentials it answers 60 requests an
hour per address.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from copy import replace
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stranger

FORGE = "https://github.com"
API = "https://api.github.com"
API_ACCEPT = "application/vnd.github+json"
ISSUER = "https://token.actions.githubusercontent.com"
OTHER_ISSUER = "https://accounts.google.com"
PROVENANCE = "https://slsa.dev/provenance/v1"
SIGNATURE = "https://sigstore.dev/cosign/sign/v1"
NOT_SIGNER = ".github/workflows/not-the-signer.yml"
ABSENT = "no-such-asset.readback"
MAX_ASSETS = 200
MAX_IMAGES = 20
MAX_BUNDLES = 30
SMALL = 16 << 20
KEYS = ("repository", "signer", "other_workflows", "unverified", "blobs", "images")
BLOB_KEYS = ("checksums", "signature")
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")
NAME = re.compile(r"[A-Za-z0-9{][A-Za-z0-9._+{}-]*")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
IMAGE = re.compile(
    r"[a-z0-9.-]+(:[0-9]+)?(/[a-z0-9._{}-]+)+(:[A-Za-z0-9._{}-]+|@sha256:[0-9a-f]{64})"
)
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
LINE = re.compile(r"([0-9a-f]{64}) [ *](\S+)")


class ParamError(Exception):
    """A parameter file that does not say what to verify."""


class ReadError(Exception):
    """An input of the judgement that could not be read: the tag, an answer of the forge, a tool."""


@dataclass(frozen=True)
class Blobs:
    checksums: str
    signature: str


@dataclass(frozen=True)
class Params:
    repository: str
    signer: str
    other_workflows: tuple[str, ...]
    unverified: tuple[str, ...]
    blobs: Blobs | None
    images: tuple[str, ...]


@dataclass(frozen=True)
class Claim:
    """What a certificate must say: who signed, for which repository, ref and commit."""

    identity: str
    repository: str
    ref: str
    commit: str
    issuer: str = ISSUER

    def flags(self) -> list[str]:
        return [
            "--certificate-identity",
            self.identity,
            "--certificate-oidc-issuer",
            self.issuer,
            "--certificate-github-workflow-repository",
            self.repository,
            "--certificate-github-workflow-ref",
            self.ref,
            "--certificate-github-workflow-sha",
            self.commit,
        ]


class Report:
    """The lines of a read-back, written as they come, and their count."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def _line(self, ok: bool, kind: str, label: str, detail: str) -> None:
        self.passed += ok
        self.failed += not ok
        sys.stdout.write(f"{'PASS' if ok else 'FAIL'} {kind}  {label}\n")
        if detail:
            sys.stdout.write(f"             {detail}\n")
        sys.stdout.flush()

    def accept(self, label: str, ok: bool, detail: str = "") -> None:
        self._line(bool(ok), "accept", label, "" if ok else detail)

    def refuse(self, label: str, code: int | None, detail: str = "") -> None:
        """A control: `code` is the exit code of the call that had to be refused."""
        if code is None:
            self._line(False, "refuse", f"{label} (no answer)", detail)
        elif code == 0:
            self._line(False, "refuse", f"{label} (was accepted)", "")
        else:
            self._line(True, "refuse", label, detail)

    def note(self, text: str) -> None:
        sys.stdout.write(f"NOTE {text}\n")

    def end(self) -> int:
        if self.failed or not self.passed:
            sys.stdout.write(f"FAIL: {self.failed} of {self.passed + self.failed} line(s) failed\n")
            return 1
        sys.stdout.write(
            f"PASS: {self.passed} line(s), every check accepted, every control refused\n"
        )
        return 0


def _tail(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _fill(text: str, tag: str) -> str:
    return text.replace("{tag}", tag).replace("{version}", tag.removeprefix("v"))


def _only(table: dict, keys: tuple[str, ...], where: str) -> None:
    unknown = sorted(set(table) - set(keys))
    if unknown:
        raise ParamError(f"{where} has unknown key(s) {', '.join(unknown)}")


def _text(table: dict, key: str, pattern: re.Pattern, what: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ParamError(f"`{key}` is {what}")
    if set("{}") & set(_fill(value, "")):
        raise ParamError(f"`{key}` holds a placeholder other than {{tag}} and {{version}}")
    return value


def _texts(table: dict, key: str, pattern: re.Pattern, what: str) -> tuple[str, ...]:
    values = table.get(key, [])
    if not isinstance(values, list) or len(set(map(str, values))) != len(values):
        raise ParamError(f"`{key}` is a list in which nothing is named twice")
    return tuple(_text({key: value}, key, pattern, f"a list of {what}") for value in values)


def parse(table: dict) -> Params:
    """The parameters of one repository, or ParamError with the reason."""
    _only(table, KEYS, "the file")
    blobs = table.get("blobs")
    if blobs is not None:
        if not isinstance(blobs, dict):
            raise ParamError("`blobs` is a table")
        _only(blobs, BLOB_KEYS, "`blobs`")
        blobs = Blobs(
            _text(blobs, "checksums", NAME, "the name of the checksums asset"),
            _text(blobs, "signature", NAME, "the name of the signature bundle"),
        )
    params = Params(
        repository=_text(table, "repository", REPOSITORY, "written as owner/name"),
        signer=_text(table, "signer", WORKFLOW, "a workflow file under .github/workflows"),
        other_workflows=_texts(table, "other_workflows", WORKFLOW, "workflow files"),
        unverified=_texts(table, "unverified", NAME, "asset names"),
        blobs=blobs,
        images=_texts(table, "images", IMAGE, "image references with a tag or a digest"),
    )
    if blobs is None and not params.images:
        raise ParamError("names neither `blobs` nor `images`, so there is nothing to verify")
    if params.signer in params.other_workflows:
        raise ParamError("`other_workflows` names the signer")
    if len(params.images) > MAX_IMAGES:
        raise ParamError(f"`images` names more than {MAX_IMAGES} references")
    return params


def load(path: Path) -> Params:
    try:
        return parse(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raise ParamError(str(err)) from err


def _identity(repository: str, workflow: str, ref: str) -> str:
    return f"{FORGE}/{repository}/{workflow}@{ref}"


def controls(params: Params, claim: Claim) -> list[tuple[str, Claim]]:
    """The claim with one expectation changed at a time; none of them may be accepted."""
    owner, name = params.repository.split("/")
    stranger_repository = f"{owner}/not-{name}"
    other_ref = f"{claim.ref}-other"
    other_commit = ("1" if claim.commit[0] == "0" else "0") + claim.commit[1:]
    here, signer = params.repository, params.signer
    made = [
        ("identity of another tag", replace(claim, identity=_identity(here, signer, other_ref))),
        (
            "identity of the branch main",
            replace(claim, identity=_identity(here, signer, "refs/heads/main")),
        ),
        (
            "the signer's path in another repository",
            replace(claim, identity=_identity(stranger_repository, signer, claim.ref)),
        ),
        ("another commit", replace(claim, commit=other_commit)),
        ("another repository in the certificate", replace(claim, repository=stranger_repository)),
        ("another ref in the certificate", replace(claim, ref=other_ref)),
        ("another issuer", replace(claim, issuer=OTHER_ISSUER)),
    ]
    for workflow in (NOT_SIGNER, *params.other_workflows):
        changed = replace(claim, identity=_identity(here, workflow, claim.ref))
        made.append((f"identity of {workflow}, not the signer", changed))
    return made


def _cosign() -> list[str]:
    return shlex.split(os.environ.get("COSIGN", "cosign"))


def _prove(report: Report, world, label: str, command, claim: Claim, others, reading=None):
    """One call that must be accepted and its controls; the output of the accepted call, or None.

    `command(claim)` gives the call. `reading(output)` names what is wrong
    with an accepted answer, or nothing.
    """
    code, out, err = world.run(command(claim))
    problem = _tail(err) or f"exit code {code}" if code != 0 else (reading(out) if reading else "")
    report.accept(label, not problem, problem)
    for what, other in others:
        code, _, err = world.run(command(other))
        report.refuse(f"{label}, control: {what}", code, _tail(err))
    return None if problem else out


def _json(path: Path, what: str) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError) as err:
        raise ReadError(f"{what}: {err}") from err


def _commit(world, repository: str, tag: str) -> str:
    """The commit the tag names on the forge now; of an annotated tag, the commit it points at."""
    ref = f"refs/tags/{tag}"
    git = shlex.split(os.environ.get("GIT", "git"))
    code, out, err = world.run([*git, "ls-remote", f"{FORGE}/{repository}", ref, f"{ref}^{{}}"])
    if code != 0:
        raise ReadError(f"git ls-remote {repository}: {_tail(err) or code}")
    found = {}
    for line in out.splitlines()[:4]:
        commit, _, name = line.partition("\t")
        found[name.strip()] = commit.strip()
    commit = found.get(f"{ref}^{{}}") or found.get(ref) or ""
    if not COMMIT.fullmatch(commit):
        raise ReadError(f"{repository} has no tag {tag}")
    return commit


class Published:
    """Assets downloaded from a published release on the forge."""

    def __init__(self, world, repository: str, tag: str) -> None:
        self.world = world
        self.repository = repository
        self.tag = tag
        self.base = f"{FORGE}/{repository}/releases/download/{tag}"

    def note(self, commit: str) -> str:
        return f"read-back of {self.repository} {self.tag} at commit {commit}, no credentials"

    def assets(self, work: Path) -> dict[str, str | None]:
        url = f"{API}/repos/{self.repository}/releases/tags/{self.tag}"
        path = work / "release.json"
        try:
            self.world.fetch(url, path, API_ACCEPT, SMALL)
        except stranger.FetchError as err:
            raise ReadError(str(err)) from err
        answer = _json(path, url)
        listed = answer.get("assets") if isinstance(answer, dict) else None
        if not isinstance(listed, list) or len(listed) > MAX_ASSETS:
            raise ReadError(f"{url}: no list of at most {MAX_ASSETS} assets")
        assets: dict[str, str | None] = {}
        for entry in listed:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not PLAIN.fullmatch(name):
                raise ReadError(f"{url}: asset name {name!r} is not a plain file name")
            recorded = entry.get("digest")
            assets[name] = recorded.removeprefix("sha256:") if isinstance(recorded, str) else None
        return assets

    def fetch(self, name: str, target: Path) -> str:
        return self.world.fetch(f"{self.base}/{name}", target)


class Local:
    """Assets read from a local folder before a release exists."""

    def __init__(self, folder: Path, repository: str, tag: str) -> None:
        self.folder = folder
        self.repository = repository
        self.tag = tag

    def note(self, commit: str) -> str:
        return (
            f"read-back of {self.repository} {self.tag} at commit {commit}, "
            f"no credentials, files from local folder {self.folder}"
        )

    def _entries(self) -> list[os.DirEntry]:
        if not self.folder.is_dir():
            raise ReadError(f"--local {self.folder}: not a directory")
        entries = []
        try:
            with os.scandir(self.folder) as iterator:
                for _ in range(MAX_ASSETS + 2):
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    entries.append(entry)
        except OSError as err:
            raise ReadError(f"--local {self.folder}: {err}") from err
        if len(entries) > MAX_ASSETS:
            raise ReadError(f"--local {self.folder}: holds more than {MAX_ASSETS} assets")
        return entries

    def assets(self, _work: Path) -> dict[str, str | None]:
        found: dict[str, str | None] = {}
        for entry in self._entries()[: MAX_ASSETS + 1]:
            if entry.is_symlink():
                raise ReadError(f"--local {self.folder}: entry {entry.name!r} is a symlink")
            if entry.is_dir():
                raise ReadError(f"--local {self.folder}: entry {entry.name!r} is a directory")
            if not entry.is_file():
                raise ReadError(
                    f"--local {self.folder}: entry {entry.name!r} is not a regular file"
                )
            if not PLAIN.fullmatch(entry.name):
                raise ReadError(
                    f"--local {self.folder}: asset name {entry.name!r} is not a plain file name"
                )
            found[entry.name] = None
        return found

    def fetch(self, name: str, target: Path) -> str:
        path = self.folder / name
        if not path.is_file() or path.is_symlink():
            raise stranger.FetchError(f"{path}: no such file", 404)
        try:
            shutil.copyfile(path, target)
        except OSError as err:
            raise stranger.FetchError(str(err)) from err
        return stranger.digest(target)


def _name_set(report: Report, assets: dict, expected: dict[str, str]) -> None:
    """The release holds the expected assets and no other; `expected` maps a name to its reason."""
    for name in sorted(set(assets) - set(expected)):
        report.accept(f"asset {name} is listed in the checksums or declared unverified", False)
    for name in sorted(set(expected) - set(assets)):
        report.accept(f"{name} ({expected[name]}) is on the release", False)
    if set(assets) == set(expected):
        report.accept(f"the release holds exactly the {len(expected)} expected asset(s)", True)


def _download(
    report: Report, source: Published | Local, names: list[str], folder: Path, assets: dict
):
    """Save the named assets; the names that came and equal the forge's recorded digest."""
    got = []
    for name in names:
        try:
            found = source.fetch(name, folder / name)
        except stranger.FetchError as err:
            report.accept(f"download {name}", False, str(err))
            continue
        recorded = assets.get(name)
        same = recorded is None or recorded == found
        report.accept(f"download {name}", same, f"forge records {recorded}, download is {found}")
        if same:
            got.append(name)
    return got


def _listed(report: Report, path: Path) -> dict[str, str] | None:
    """The checksums file as name to digest; None when it cannot be taken as one."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        report.accept("the checksums are text", False, str(err))
        return None
    listed: dict[str, str] = {}
    for number, line in enumerate(text.splitlines()[: MAX_ASSETS + 1], 1):
        found = LINE.fullmatch(line)
        if not found or not PLAIN.fullmatch(found[2]) or found[2] in listed:
            what = f"checksums line {number} is a digest and a plain file name, named once"
            report.accept(what, False, line[:120])
            return None
        listed[found[2]] = found[1]
    ok = 0 < len(listed) <= MAX_ASSETS
    report.accept(f"the checksums list {len(listed)} file(s), 1 to {MAX_ASSETS}", ok)
    return listed if ok else None


def _altered(folder: Path, name: str) -> Path:
    """A copy of a downloaded file with one byte appended, for the controls."""
    target = folder.parent / "altered" / name
    target.parent.mkdir(exist_ok=True)
    shutil.copyfile(folder / name, target)
    with target.open("ab") as handle:
        handle.write(b"x")
    return target


def _digests(report: Report, listed: dict[str, str], got: list[str], folder: Path) -> None:
    for name in got:
        found = stranger.digest(folder / name)
        report.accept(f"{name} matches its listed digest", found == listed[name], f"is {found}")
    if got:
        same = stranger.digest(_altered(folder, got[0])) == listed[got[0]]
        report.refuse(f"{got[0]} matches its listed digest, control: one byte appended", not same)


def _signature(report: Report, world, params: Params, claim: Claim, folder: Path, got, others):
    sums, bundle = folder / params.blobs.checksums, folder / params.blobs.signature

    def command(expected: Claim, file: Path = sums) -> list[str]:
        start = [*_cosign(), "verify-blob", "--new-bundle-format", "--bundle", str(bundle)]
        return [*start, *expected.flags(), str(file)]

    label = f"signature on {params.blobs.checksums}"
    _prove(report, world, label, command, claim, others)
    planted = [("the checksums with one byte appended", _altered(folder, sums.name))]
    planted += [(f"the bundle against {name}", folder / name) for name in got[:1]]
    for what, file in planted:
        code, _, err = world.run(command(claim, file))
        report.refuse(f"{label}, control: {what}", code, _tail(err))


def _statement(payload: object) -> dict:
    """The statement inside an envelope's payload; empty when it is none."""
    try:
        statement = json.loads(base64.b64decode(str(payload), validate=True))
    except ValueError:
        return {}
    return statement if isinstance(statement, dict) else {}


def _subjects(statement: dict) -> set[str]:
    """The SHA-256 digests a provenance statement names; empty for any other statement."""
    subjects = statement.get("subject")
    if statement.get("predicateType") != PROVENANCE or not isinstance(subjects, list):
        return set()
    found = set()
    for subject in subjects[:1000]:
        digests = subject.get("digest") if isinstance(subject, dict) else None
        if isinstance(digests, dict) and isinstance(digests.get("sha256"), str):
            found.add(digests["sha256"])
    return found


def _served(world, params: Params, digest: str, folder: Path, number: int):
    """The provenance bundles the forge serves for one digest: (saved file, digests it names)."""
    url = f"{API}/repos/{params.repository}/attestations/sha256:{digest}"
    path = folder.parent / f"attestations-{number}.json"
    try:
        world.fetch(url, path, API_ACCEPT, SMALL)
    except stranger.FetchError as err:
        if err.status == 404:
            return []
        raise ReadError(str(err)) from err
    answer = _json(path, url)
    entries = answer.get("attestations") if isinstance(answer, dict) else None
    if not isinstance(entries, list):
        raise ReadError(f"{url}: no list of attestations")
    found = []
    for index, entry in enumerate(entries[:MAX_BUNDLES]):
        bundle = entry.get("bundle") if isinstance(entry, dict) else None
        envelope = bundle.get("dsseEnvelope") if isinstance(bundle, dict) else None
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        subjects = _subjects(_statement(payload))
        if subjects:
            saved = folder.parent / f"provenance-{number}-{index}.json"
            saved.write_text(json.dumps(bundle), encoding="utf-8")
            found.append((saved, subjects))
    return found


def _covering(report: Report, world, params: Params, listed: dict, got, folder: Path) -> dict:
    """For each downloaded file the provenance bundle that names its digest, and what it names."""
    covered: dict[str, tuple[Path, set[str]]] = {}
    for number, asked in enumerate(got):
        if asked in covered:
            continue
        for saved, subjects in _served(world, params, listed[asked], folder, number):
            for name in got:
                if listed[name] in subjects:
                    covered.setdefault(name, (saved, subjects))
    for name in got:
        report.accept(
            f"the forge serves provenance that names the digest of {name}", name in covered
        )
    return covered


def _provenance(report: Report, world, params: Params, claim: Claim, folder: Path, found, others):
    listed, got = found
    covered = _covering(report, world, params, listed, got, folder)
    names = [name for name in got if name in covered]

    def command(expected: Claim, name: str, file: Path | None = None, bundle: Path | None = None):
        start = [*_cosign(), "verify-blob-attestation", "--new-bundle-format", "--bundle"]
        start += [str(bundle or covered[name][0]), "--type", "slsaprovenance1"]
        return [*start, *expected.flags(), str(file or folder / name)]

    for number, name in enumerate(names):
        first = number == 0
        _prove(
            report,
            world,
            f"provenance of {name}",
            lambda expected, name=name: command(expected, name),
            claim,
            others if first else [],
        )
    if not names:
        return
    first, signature = names[0], folder / params.blobs.signature
    planted = [("the file with one byte appended", _altered(folder, first), None)]
    if signature.is_file():
        planted.append(("the signature bundle offered as provenance", folder / first, signature))
        if stranger.digest(signature) not in covered[first][1]:
            planted.append(("a file that is no subject", signature, None))
    for what, file, bundle in planted:
        code, _, err = world.run(command(claim, first, file, bundle))
        report.refuse(f"provenance of {first}, control: {what}", code, _tail(err))


def _blobs(
    report: Report,
    world,
    params: Params,
    tag: str,
    claim: Claim,
    assets: dict,
    work: Path,
    others,
    source: Published | Local,
) -> None:
    blobs, folder = params.blobs, work / "assets"
    folder.mkdir()
    try:
        source.fetch(ABSENT, work / ABSENT)
        report.refuse("download, control: an asset that does not exist", 0)
    except stranger.FetchError as err:
        report.refuse("download, control: an asset that does not exist", err.status, str(err))
    head = [name for name in (blobs.checksums, blobs.signature) if name in assets]
    head = _download(report, source, head, folder, assets)
    listed = _listed(report, folder / blobs.checksums) if blobs.checksums in head else None
    expected = dict.fromkeys(listed or {}, "listed in the checksums")
    expected |= {blobs.checksums: "the checksums", blobs.signature: "the signature"}
    declared = [_fill(name, tag) for name in params.unverified]
    for name in sorted(set(declared) & set(expected)):
        report.accept(f"{name} (declared unverified) is not {expected[name]}", False)
    expected |= dict.fromkeys(declared, "declared unverified")
    _name_set(report, assets, expected)
    if listed is None:
        return
    got = _download(report, source, [name for name in listed if name in assets], folder, assets)
    _digests(report, listed, got, folder)
    if blobs.signature in head:
        _signature(report, world, params, claim, folder, got, others)
    _provenance(report, world, params, claim, folder, (listed, got), others)


def _dig(node: object, *keys: str) -> object:
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _signed(out: str, reference: str) -> tuple[str, str]:
    """The digest a signature answer names and what is wrong with the answer, if anything."""
    try:
        entries = json.loads(out)
    except ValueError:
        return "", "the answer is not JSON"
    entries = entries if isinstance(entries, list) else []
    signed = [entry for entry in entries if _dig(entry, "critical", "type") == SIGNATURE]
    if not signed:
        return "", "no entry of the signature type; the image was accepted on an attestation alone"
    digests = {str(_dig(entry, "critical", "image", "docker-manifest-digest")) for entry in signed}
    found = digests.pop() if len(digests) == 1 else ""
    if not DIGEST.fullmatch(found):
        return "", "the signatures do not name one digest"
    if "@" in reference and not reference.endswith(f"@{found}"):
        return "", f"the registry answered {found} for a reference by another digest"
    return found, ""


def _attested(out: str, digest: str) -> str:
    """What is wrong with a provenance answer for the image of that digest, if anything."""
    for line in out.splitlines()[:MAX_BUNDLES]:
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if digest.removeprefix("sha256:") in _subjects(_statement(_dig(envelope, "payload"))):
            return ""
    return "no provenance statement in the answer names the image's digest"


def _image(report: Report, world, claim: Claim, reference: str, others) -> None:
    def signed(expected: Claim) -> list[str]:
        return [*_cosign(), "verify", reference, *expected.flags(), "--output", "json"]

    label = f"signature of {reference}"
    out = _prove(
        report, world, label, signed, claim, others, lambda out: _signed(out, reference)[1]
    )
    if out is None:
        report.accept(f"provenance of {reference}", False, "not asked: no signed digest to ask by")
        return
    digest = _signed(out, reference)[0]
    name = reference.split("@")[0] if "@" in reference else reference.rsplit(":", 1)[0]
    pinned = f"{name}@{digest}"

    def attested(expected: Claim) -> list[str]:
        start = [*_cosign(), "verify-attestation", "--type", "slsaprovenance1", pinned]
        return [*start, *expected.flags()]

    label = f"provenance of {pinned}"
    _prove(report, world, label, attested, claim, others, lambda out: _attested(out, digest))


def verify(
    params: Params,
    tag: str,
    work: Path,
    world,
    report: Report,
    local: Path | None = None,
) -> None:
    """Read one release back; the lines go to the report."""
    code, out, err = world.run([*_cosign(), "version"])
    if code != 0:
        raise ReadError(f"the signature tool does not run: {_tail(err) or code}")
    version = [line.split()[-1] for line in out.splitlines() if line.startswith("GitVersion")]
    commit = _commit(world, params.repository, tag)
    ref = f"refs/tags/{tag}"
    claim = Claim(_identity(params.repository, params.signer, ref), params.repository, ref, commit)
    others = controls(params, claim)
    source = (
        Local(local, params.repository, tag)
        if local is not None
        else Published(world, params.repository, tag)
    )
    report.note(source.note(commit))
    report.note(f"signer {claim.identity}; cosign {' '.join(version) or 'of unknown version'}")
    assets = source.assets(work)
    if params.blobs:
        _blobs(report, world, params, tag, claim, assets, work, others, source)
    else:
        expected = {_fill(name, tag): "declared unverified" for name in params.unverified}
        _name_set(report, assets, expected)
    for name in params.unverified:
        filled = _fill(name, tag)
        report.note(f"not verified: {filled}, forge records sha256 {assets.get(filled)}")
    for reference in params.images:
        _image(report, world, claim, _fill(reference, tag), others)


def _folder(given: Path | None) -> Path:
    if given is None:
        return Path(tempfile.mkdtemp(prefix="readback-"))
    try:
        given.mkdir(parents=True)
    except OSError as err:
        raise ReadError(f"--work {given}: {err} (it must not exist yet)") from err
    return given


def main(arguments: list[str] | None = None, world=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=("check", "verify"))
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--local", type=Path)
    args = parser.parse_args(arguments)
    try:
        params = load(args.params)
    except ParamError as err:
        sys.stderr.write(f"ERROR: {args.params}: {err}\n")
        return 3
    if args.command == "check":
        if args.local is not None:
            sys.stderr.write("ERROR: --local is only valid with verify\n")
            return 3
        sys.stdout.write(f"PASS: {args.params} names what to verify for {params.repository}\n")
        return 0
    report = Report()
    try:
        if not TAG.fullmatch(args.tag):
            raise ReadError(f"--tag {args.tag!r} is not a plain tag name")
        work = _folder(args.work)
        (work / "home").mkdir()
        verify(
            params, args.tag, work, world or stranger.Stranger(work / "home"), report, args.local
        )
    except (ReadError, OSError) as err:
        sys.stderr.write(f"ERROR: {err}\n")
        return 3
    return report.end()


if __name__ == "__main__":
    sys.exit(main())
