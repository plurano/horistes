# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Release read-back: one good release passes, and each planted defect is named in a FAIL line.

No test reaches the network. The forge, the registry and the verifier are one
stand-in (`World`) that holds a release of two files and one image; a test
plants its defect in a field of it or in one line of the parameter file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tomllib

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import readback
import stranger

REPOSITORY = "fixture/tool"
TAG = "v1.2.3"
REF = f"refs/tags/{TAG}"
COMMIT = "c0ffee" + "0" * 34
TAG_OBJECT = "e" * 40
SIGNER = ".github/workflows/build.yml"
CALLER = ".github/workflows/release.yml"
ARCHIVE, SBOM = "tool_1.2.3_linux.tar.gz", "tool_1.2.3_linux.tar.gz.sbom.json"
SUMS, BUNDLE = "checksums.txt", "checksums.txt.sigstore.json"
NAME = "registry.test/fixture/tool"
DIGEST, OTHER_DIGEST = "sha256:" + "a" * 64, "sha256:" + "b" * 64
RELEASE = f"{readback.API}/repos/{REPOSITORY}/releases/tags/{TAG}"
DOWNLOAD = f"{readback.FORGE}/{REPOSITORY}/releases/download/{TAG}/"
FILES = {ARCHIVE: b"archive", SBOM: b"bill of materials"}
HEAD = f'repository = "{REPOSITORY}"\nsigner = "{SIGNER}"\n'
OTHERS = f'other_workflows = ["{CALLER}"]\n'
IMAGES = f'images = ["{NAME}:{{version}}"]\n'
BLOBS = f'\n[blobs]\nchecksums = "{SUMS}"\nsignature = "{BUNDLE}"\n'
PARAMS = HEAD + OTHERS + IMAGES + BLOBS
ACCEPTED, REFUSED = 15, 43
VERIFIER_CONTROLS = 41


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def claim(workflow=SIGNER, ref=REF, commit=COMMIT, repository=REPOSITORY) -> dict[str, str]:
    """A certificate: the five things the tool has to ask the verifier for."""
    return {
        "--certificate-identity": f"{readback.FORGE}/{repository}/{workflow}@{ref}",
        "--certificate-oidc-issuer": readback.ISSUER,
        "--certificate-github-workflow-repository": repository,
        "--certificate-github-workflow-ref": ref,
        "--certificate-github-workflow-sha": commit,
    }


def statement(*digests: str, kind: str = readback.PROVENANCE) -> dict:
    subjects = [{"name": "subject", "digest": {"sha256": digest}} for digest in digests]
    return {"predicateType": kind, "subject": subjects}


def sealed(found: dict) -> str:
    return base64.b64encode(json.dumps(found).encode()).decode()


def signature(data: bytes, by: dict | None = None) -> bytes:
    return json.dumps({"signs": sha(data), "claim": by or claim()}).encode()


def provenance(digests, by: dict | None = None, kind: str = readback.PROVENANCE) -> dict:
    payload = sealed(statement(*digests, kind=kind))
    return {"dsseEnvelope": {"payload": payload}, "claim": by or claim()}


def split(words: list[str]) -> tuple[dict[str, str], list[str]]:
    """The flags of a verifier call with their values, and the words that are no flag."""
    flags, rest, left = {}, [], iter(words)
    for word in left:
        if word == "--new-bundle-format":
            flags[word] = ""
        elif word.startswith("--"):
            flags[word] = next(left)
        else:
            rest.append(word)
    return flags, rest


class World:
    """A forge, a registry and a verifier around one release.

    The verifier is strict where the tool leans on it: a certificate passes
    only when all five expectations are asked and equal. It is naive where
    the tool has a check of its own: it answers for an image by name and
    does not compare the statement it returns with the image's digest.
    """

    def __init__(self) -> None:
        self.assets = dict(FILES)
        self.recorded: dict[str, str] = {}
        self.broken: dict[str, int] = {}
        self.tags = [f"{COMMIT}\t{REF}\n"]
        self.codes: dict[str, int | None] = {"ls-remote": 0, "version": 0}
        self.lenient = self.silent = False
        self.calls: list[list[str]] = []
        self.relist(FILES)
        self.attest(FILES)
        self.images = {
            NAME: {
                "entries": [(readback.SIGNATURE, DIGEST), (readback.PROVENANCE, DIGEST)],
                "claim": claim(),
                "statement": statement(DIGEST.removeprefix("sha256:")),
                "attested_by": claim(),
            }
        }

    def relist(self, files: dict[str, bytes], more: bytes = b"") -> None:
        """Write the checksums over `files`, with `more` at the end, and sign them."""
        lines = "".join(f"{sha(data)}  {name}\n" for name, data in files.items())
        self.assets[SUMS] = lines.encode() + more
        self.assets[BUNDLE] = signature(self.assets[SUMS])

    def attest(self, files: dict[str, bytes], by: dict | None = None, kind=readback.PROVENANCE):
        """One attestation over all of `files`, served for the digest of each."""
        digests = [sha(data) for data in files.values()]
        self.attestations = {digest: [provenance(digests, by, kind)] for digest in digests}

    def _answer(self, url: str) -> bytes:
        for part, status in self.broken.items():
            if part in url:
                raise stranger.FetchError(f"GET {url}: HTTP {status}", status)
        name = url.rsplit("/", 1)[1]
        if url == RELEASE:
            listed = [
                {"name": name, "digest": "sha256:" + self.recorded.get(name, sha(data))}
                for name, data in self.assets.items()
            ]
            return json.dumps({"assets": listed}).encode()
        if url.startswith(readback.API) and name.removeprefix("sha256:") in self.attestations:
            bundles = self.attestations[name.removeprefix("sha256:")]
            return json.dumps({"attestations": [{"bundle": bundle} for bundle in bundles]}).encode()
        if url.startswith(DOWNLOAD) and name in self.assets:
            return self.assets[name]
        raise stranger.FetchError(f"GET {url}: HTTP 404", 404)

    def fetch(self, url: str, path, accept: str = "*/*", limit: int = 0) -> str:
        self.calls.append(["GET", url])
        data = self._answer(url)
        path.write_bytes(data)
        return sha(data)

    def run(self, command: list[str]) -> tuple[int | None, str, str]:
        self.calls.append(command)
        verb = command[1]
        if verb in self.codes:
            out = "".join(self.tags) if verb == "ls-remote" else "GitVersion:    v2.6.3\n"
            return self.codes[verb], out, "no answer" if self.codes[verb] else ""
        if self.silent:
            return None, "", "timed out after 240 seconds"
        flags, rest = split(command[2:])
        ok, out = getattr(self, "_" + verb.replace("-", "_"))(flags, rest[0])
        return (0, out, "") if ok or self.lenient else (1, "", "Error: none found\nexit status 1")

    def _passes(self, flags: dict[str, str], certificate: dict[str, str]) -> bool:
        return all(flags.get(key) == value for key, value in certificate.items())

    def _verify_blob(self, flags: dict[str, str], file: str) -> tuple[bool, str]:
        with open(flags["--bundle"], "rb") as handle:
            bundle = json.load(handle)
        with open(file, "rb") as handle:
            same = bundle.get("signs") == sha(handle.read())
        return same and "--new-bundle-format" in flags and self._passes(flags, bundle["claim"]), ""

    def _verify_blob_attestation(self, flags: dict[str, str], file: str) -> tuple[bool, str]:
        with open(flags["--bundle"], "rb") as handle:
            bundle = json.load(handle)
        payload = bundle.get("dsseEnvelope", {}).get("payload", "")
        with open(file, "rb") as handle:
            named = sha(handle.read()) in readback._subjects(readback._statement(payload))
        kind = flags.get("--type") == "slsaprovenance1" and "--new-bundle-format" in flags
        return named and kind and self._passes(flags, bundle["claim"]), ""

    def _verify(self, flags: dict[str, str], reference: str) -> tuple[bool, str]:
        name = reference.split("@")[0] if "@" in reference else reference.rsplit(":", 1)[0]
        image = self.images.get(name)
        if image is None or flags.get("--output") != "json":
            return False, ""
        entries = [
            {"critical": {"type": kind, "image": {"docker-manifest-digest": digest}}}
            for kind, digest in image["entries"]
        ]
        return self._passes(flags, image["claim"]), json.dumps(entries)

    def _verify_attestation(self, flags: dict[str, str], reference: str) -> tuple[bool, str]:
        image = self.images.get(reference.split("@")[0])
        if image is None or image["statement"] is None:
            return False, ""
        kind = flags.get("--type") == "slsaprovenance1"
        out = json.dumps({"payload": sealed(image["statement"])}) + "\n"
        return kind and self._passes(flags, image["attested_by"]), out


@pytest.fixture
def read(tmp_path, capsys, monkeypatch):
    """Run a read-back on a world; the exit code, the lines and the complaint."""
    monkeypatch.delenv("COSIGN", raising=False)
    monkeypatch.delenv("GIT", raising=False)

    def run(world, params: str = PARAMS, tag: str = TAG, command: str = "verify"):
        file = tmp_path / "release.toml"
        file.write_text(params, encoding="utf-8")
        words = [command, "--params", str(file)]
        if command == "verify":
            words += ["--tag", tag, "--work", str(tmp_path / "work")]
        code = readback.main(words, world)
        taken = capsys.readouterr()
        return code, taken.out, taken.err

    return run


def failed(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("FAIL ")]


def test_good_release_passes_with_every_control_refused(read):
    world = World()
    code, out, err = read(world)
    assert code == 0, out + err
    assert out.count("PASS accept") == ACCEPTED
    assert out.count("PASS refuse") == REFUSED
    assert out.endswith(
        f"PASS: {ACCEPTED + REFUSED} line(s), every check accepted, every control refused\n"
    )
    assert f"NOTE read-back of {REPOSITORY} {TAG} at commit {COMMIT}, no credentials" in out


def test_api_is_asked_once_for_the_assets_and_once_for_the_attestations(read):
    world = World()
    read(world)
    asked = [call[1] for call in world.calls if call[0] == "GET" and readback.API in call[1]]
    assert asked == [
        RELEASE,
        f"{readback.API}/repos/{REPOSITORY}/attestations/sha256:{sha(b'archive')}",
    ]


def test_annotated_tag_is_read_at_the_commit_it_points_at(read):
    world = World()
    world.tags = [f"{TAG_OBJECT}\t{REF}\n", f"{COMMIT}\t{REF}^{{}}\n"]
    code, out, _ = read(world)
    assert code == 0, out
    assert f"at commit {COMMIT}" in out


def test_image_named_by_its_digest_passes(read):
    code, out, _ = read(World(), PARAMS.replace(":{version}", f"@{DIGEST}"))
    assert code == 0, out
    assert f"PASS accept  signature of {NAME}@{DIGEST}" in out


def test_declared_unverified_asset_is_named_and_passes(read):
    world = World()
    world.assets["notes-1.2.3.txt"] = b"notes"
    code, out, _ = read(world, 'unverified = ["notes-{version}.txt"]\n' + PARAMS)
    assert code == 0, out
    assert f"NOTE not verified: notes-1.2.3.txt, forge records sha256 {sha(b'notes')}" in out


def test_release_of_images_alone_passes(read):
    world = World()
    world.assets = {}
    code, out, _ = read(world, HEAD + IMAGES)
    assert code == 0, out
    assert "PASS accept  the release holds exactly the 0 expected asset(s)" in out


def extra_asset(world):
    world.assets["notes.txt"] = b"notes"


def listed_file_gone(world):
    del world.assets[ARCHIVE]


def listed_file_altered(world):
    world.assets[ARCHIVE] = b"another archive"


def line_that_is_none(world):
    world.relist(FILES, b"not a line\n")


def file_named_twice(world):
    world.relist(FILES, f"{sha(b'archive')}  {ARCHIVE}\n".encode())


def name_with_a_path(world):
    world.relist(FILES, f"{sha(b'archive')}  ../{ARCHIVE}\n".encode())


def no_file_listed(world):
    world.relist({})


def forge_records_another_digest(world):
    world.recorded[ARCHIVE] = "0" * 64


def signature_gone(world):
    del world.assets[BUNDLE]


def checksums_gone(world):
    del world.assets[SUMS]


def signed_on_the_branch(world):
    world.assets[BUNDLE] = signature(world.assets[SUMS], claim(ref="refs/heads/main"))


def signed_by_the_caller(world):
    world.assets[BUNDLE] = signature(world.assets[SUMS], claim(workflow=CALLER))


def signed_at_another_commit(world):
    world.assets[BUNDLE] = signature(world.assets[SUMS], claim(commit="d" * 40))


def signed_in_another_repository(world):
    world.assets[BUNDLE] = signature(world.assets[SUMS], claim(repository="fixture/fork"))


def signature_of_other_content(world):
    world.assets[BUNDLE] = signature(b"other checksums")


def no_attestation(world):
    world.attestations = {}


def attestation_leaves_a_file_out(world):
    world.attest({ARCHIVE: FILES[ARCHIVE]})


def attestation_of_the_first_file_alone_gone(world):
    world.attestations = {sha(FILES[SBOM]): [provenance([sha(FILES[SBOM])])]}


def attestation_of_another_kind(world):
    world.attest(FILES, kind="https://spdx.dev/Document")


def attested_in_another_repository(world):
    world.attest(FILES, claim(repository="fixture/fork"))


def attested_by_the_caller(world):
    world.attest(FILES, claim(workflow=CALLER))


def image_with_an_attestation_alone(world):
    world.images[NAME]["entries"] = [(readback.PROVENANCE, DIGEST)]


def image_unknown(world):
    del world.images[NAME]


def image_signed_by_the_caller(world):
    world.images[NAME]["claim"] = claim(workflow=CALLER)


def image_signed_at_another_commit(world):
    world.images[NAME]["claim"] = claim(commit="d" * 40)


def image_signatures_name_two_digests(world):
    world.images[NAME]["entries"].append((readback.SIGNATURE, OTHER_DIGEST))


def image_statement_names_another_digest(world):
    world.images[NAME]["statement"] = statement("f" * 64)


def image_statement_of_another_kind(world):
    world.images[NAME]["statement"] = statement("a" * 64, kind="https://spdx.dev/Document")


def image_without_attestation(world):
    world.images[NAME]["statement"] = None


def image_attested_on_the_branch(world):
    world.images[NAME]["attested_by"] = claim(ref="refs/heads/main")


SIGNED = f"FAIL accept  signature on {SUMS}"
PROVEN = f"FAIL accept  provenance of {ARCHIVE}"
SERVED = "FAIL accept  the forge serves provenance that names the digest of"
IMAGE_SIGNED = f"FAIL accept  signature of {NAME}:1.2.3"
IMAGE_PROVEN = f"FAIL accept  provenance of {NAME}@{DIGEST}"
CALLER_TAKEN = f"control: identity of {CALLER}, not the signer (was accepted)"
PLANTED = [
    (extra_asset, "FAIL accept  asset notes.txt is listed in the checksums or declared unverified"),
    (listed_file_gone, f"FAIL accept  {ARCHIVE} (listed in the checksums) is on the release"),
    (listed_file_altered, f"FAIL accept  {ARCHIVE} matches its listed digest"),
    (listed_file_altered, PROVEN),
    (line_that_is_none, "FAIL accept  checksums line 3 is a digest and a plain file name"),
    (file_named_twice, "FAIL accept  checksums line 3 is a digest and a plain file name"),
    (name_with_a_path, "FAIL accept  checksums line 3 is a digest and a plain file name"),
    (no_file_listed, "FAIL accept  the checksums list 0 file(s), 1 to 200"),
    (forge_records_another_digest, f"FAIL accept  download {ARCHIVE}"),
    (signature_gone, f"FAIL accept  {BUNDLE} (the signature) is on the release"),
    (checksums_gone, f"FAIL accept  {SUMS} (the checksums) is on the release"),
    (signed_on_the_branch, SIGNED),
    (signed_by_the_caller, SIGNED),
    (signed_by_the_caller, f"FAIL refuse  signature on {SUMS}, {CALLER_TAKEN}"),
    (signed_at_another_commit, SIGNED),
    (signed_in_another_repository, SIGNED),
    (signature_of_other_content, SIGNED),
    (no_attestation, f"{SERVED} {ARCHIVE}"),
    (attestation_leaves_a_file_out, f"{SERVED} {SBOM}"),
    (attestation_of_another_kind, f"{SERVED} {ARCHIVE}"),
    (attested_in_another_repository, PROVEN),
    (attested_by_the_caller, PROVEN),
    (attested_by_the_caller, f"FAIL refuse  provenance of {ARCHIVE}, {CALLER_TAKEN}"),
    (image_with_an_attestation_alone, IMAGE_SIGNED),
    (image_with_an_attestation_alone, "accepted on an attestation alone"),
    (image_with_an_attestation_alone, f"FAIL accept  provenance of {NAME}:1.2.3"),
    (image_unknown, IMAGE_SIGNED),
    (image_signed_by_the_caller, IMAGE_SIGNED),
    (image_signed_by_the_caller, f"FAIL refuse  signature of {NAME}:1.2.3, {CALLER_TAKEN}"),
    (image_signed_at_another_commit, IMAGE_SIGNED),
    (image_signatures_name_two_digests, "the signatures do not name one digest"),
    (image_statement_names_another_digest, IMAGE_PROVEN),
    (image_statement_of_another_kind, IMAGE_PROVEN),
    (image_without_attestation, IMAGE_PROVEN),
    (image_attested_on_the_branch, IMAGE_PROVEN),
]


@pytest.mark.parametrize(
    ("plant", "line"), PLANTED, ids=[f"{p.__name__}-{n}" for n, (p, _) in enumerate(PLANTED)]
)
def test_planted_defect_fails_and_is_named(read, plant, line):
    world = World()
    plant(world)
    code, out, err = read(world)
    assert code == 1, out + err
    assert line in out
    assert out.splitlines()[-1].startswith(f"FAIL: {len(failed(out))} of ")


def test_one_file_without_provenance_fails_that_file_alone(read):
    world = World()
    attestation_leaves_a_file_out(world)
    _, out, _ = read(world)
    assert failed(out) == [f"{SERVED} {SBOM}"]


def test_file_without_provenance_does_not_hide_the_provenance_of_the_next(read):
    world = World()
    attestation_of_the_first_file_alone_gone(world)
    _, out, _ = read(world)
    assert failed(out) == [f"{SERVED} {ARCHIVE}"]
    assert f"PASS accept  provenance of {SBOM}" in out


def test_attestation_per_file_is_asked_per_file_and_passes(read):
    world = World()
    world.attestations = {sha(data): [provenance([sha(data)])] for data in FILES.values()}
    code, out, _ = read(world)
    assert code == 0, out
    assert sum("/attestations/" in call[1] for call in world.calls if call[0] == "GET") == 2


def test_image_reference_by_a_digest_the_registry_does_not_answer_fails(read):
    code, out, _ = read(World(), PARAMS.replace(":{version}", f"@{OTHER_DIGEST}"))
    assert code == 1
    assert f"the registry answered {DIGEST} for a reference by another digest" in out


def test_declared_unverified_asset_that_is_missing_fails(read):
    code, out, _ = read(World(), 'unverified = ["notes-{version}.txt"]\n' + PARAMS)
    assert code == 1
    assert "FAIL accept  notes-1.2.3.txt (declared unverified) is on the release" in out


def test_asset_beside_images_alone_fails(read):
    code, out, _ = read(World(), HEAD + IMAGES)
    assert code == 1
    assert len(failed(out)) == len(World().assets)


def test_verifier_that_accepts_everything_fails_every_control(read):
    world = World()
    world.lenient = True
    code, out, _ = read(world)
    assert code == 1
    assert out.count("(was accepted)") == VERIFIER_CONTROLS
    assert out.count("PASS refuse") == REFUSED - VERIFIER_CONTROLS


def test_verifier_that_gives_no_answer_refuses_nothing(read):
    world = World()
    world.silent = True
    code, out, _ = read(world)
    assert code == 1
    assert out.count("(no answer)") == VERIFIER_CONTROLS - 9
    assert out.count("PASS refuse") == REFUSED - VERIFIER_CONTROLS
    assert f"FAIL accept  provenance of {NAME}:1.2.3" in out
    assert "timed out after 240 seconds" in out


def no_verifier(world):
    world.codes["version"] = None


def verifier_that_fails(world):
    world.codes["version"] = 1


def forge_does_not_list_tags(world):
    world.codes["ls-remote"] = 128


def tag_unknown(world):
    world.tags = []


def tag_of_another_name(world):
    world.tags = [f"{COMMIT}\t{REF}-rc.1\n"]


def commit_that_is_none(world):
    world.tags = [f"{COMMIT[:12]}\t{REF}\n"]


def no_release_for_the_tag(world):
    world.broken["/releases/tags/"] = 404


def release_list_rate_limited(world):
    world.broken["/releases/tags/"] = 403


def attestations_rate_limited(world):
    world.broken["/attestations/"] = 429


def asset_name_with_a_path(world):
    world.assets["../notes.txt"] = b"notes"


UNREADABLE = [
    (no_verifier, "the signature tool does not run"),
    (verifier_that_fails, "the signature tool does not run"),
    (forge_does_not_list_tags, f"git ls-remote {REPOSITORY}"),
    (tag_unknown, f"{REPOSITORY} has no tag {TAG}"),
    (tag_of_another_name, f"{REPOSITORY} has no tag {TAG}"),
    (commit_that_is_none, f"{REPOSITORY} has no tag {TAG}"),
    (no_release_for_the_tag, f"GET {RELEASE}: HTTP 404"),
    (release_list_rate_limited, f"GET {RELEASE}: HTTP 403"),
    (attestations_rate_limited, "HTTP 429"),
    (asset_name_with_a_path, "is not a plain file name"),
]


@pytest.mark.parametrize(
    ("plant", "complaint"), UNREADABLE, ids=[p.__name__ for p, _ in UNREADABLE]
)
def test_input_that_cannot_be_read_is_exit_three_and_no_verdict(read, plant, complaint):
    world = World()
    plant(world)
    code, out, err = read(world)
    assert code == 3, out
    assert complaint in err
    assert "PASS:" not in out
    assert "FAIL:" not in out


def test_tag_that_is_no_plain_name_is_exit_three(read):
    world = World()
    code, _, err = read(world, tag="v1 --upload-pack=x")
    assert code == 3
    assert "is not a plain tag name" in err
    assert world.calls == []


def test_work_folder_that_exists_is_exit_three(read, tmp_path):
    (tmp_path / "work").mkdir()
    code, _, err = read(World())
    assert code == 3
    assert "it must not exist yet" in err


MANY = json.dumps([f"r.test/a{number}:1" for number in range(21)])
BAD_PARAMS = [
    (PARAMS.replace(REPOSITORY, "tool"), "`repository` is written as owner/name"),
    (PARAMS.replace(SIGNER, "build.yml"), "`signer` is a workflow file"),
    (PARAMS.replace(HEAD, f'repository = "{REPOSITORY}"\n'), "`signer` is a workflow file"),
    ("colour = 1\n" + PARAMS, "the file has unknown key(s) colour"),
    (PARAMS + "provenance = false\n", "`blobs` has unknown key(s) provenance"),
    (PARAMS.replace(f'signature = "{BUNDLE}"\n', ""), "`signature` is the name of the signature"),
    (PARAMS.replace(f'checksums = "{SUMS}"\n', ""), "`checksums` is the name of the checksums"),
    (PARAMS.replace(SUMS + '"', '../sums"'), "`checksums` is the name of the checksums"),
    (HEAD + "blobs = 1\n", "`blobs` is a table"),
    (HEAD + OTHERS, "names neither `blobs` nor `images`"),
    (PARAMS.replace(CALLER, SIGNER), "`other_workflows` names the signer"),
    (PARAMS.replace(f'["{CALLER}"]', f'"{CALLER}"'), "`other_workflows` is a list"),
    ('unverified = ["a-{sha}.txt"]\n' + PARAMS, "a placeholder other than {tag} and {version}"),
    ('unverified = ["a.txt", "a.txt"]\n' + PARAMS, "nothing is named twice"),
    ('unverified = ["notes/a.txt"]\n' + PARAMS, "`unverified` is a list of asset names"),
    (PARAMS.replace(":{version}", ""), "`images` is a list of image references"),
    (PARAMS.replace(f'["{NAME}:{{version}}"]', "true"), "`images` is a list"),
    (PARAMS.replace(IMAGES, f"images = {MANY}\n"), "`images` names more than 20 references"),
    (PARAMS + "[blobs]\n", "Cannot declare"),
]


@pytest.mark.parametrize(("params", "complaint"), BAD_PARAMS, ids=[c[:32] for _, c in BAD_PARAMS])
def test_parameter_file_that_does_not_say_what_to_verify_is_exit_three(read, params, complaint):
    world = World()
    for command in ("check", "verify"):
        code, out, err = read(world, params, command=command)
        assert (code, out) == (3, "")
        assert complaint in err
    assert world.calls == []


def test_parameter_file_that_is_missing_is_exit_three(tmp_path, capsys):
    code = readback.main(["check", "--params", str(tmp_path / "none.toml")])
    assert code == 3
    assert "none.toml" in capsys.readouterr().err


def test_parameter_file_passes_the_check_without_a_call(read):
    world = World()
    code, out, _ = read(world, command="check")
    assert code == 0
    assert f"names what to verify for {REPOSITORY}" in out
    assert world.calls == []


KEPT = sorted((ROOT / "tests" / "fixtures" / "readback").glob("*.toml"))


def test_parameter_files_of_the_three_read_releases_are_kept():
    assert [path.stem for path in KEPT] == ["diallak", "epitrop", "snatcharr"]


@pytest.mark.parametrize("path", KEPT, ids=[path.stem for path in KEPT])
def test_kept_parameter_file_says_what_to_verify(path):
    params = readback.load(path)
    assert params.blobs or params.images


def the_claim(commit: str = COMMIT) -> readback.Claim:
    identity = f"{readback.FORGE}/{REPOSITORY}/{SIGNER}@{REF}"
    return readback.Claim(identity, REPOSITORY, REF, commit)


def test_every_control_changes_one_expectation():
    params, expected = readback.parse(tomllib.loads(PARAMS)), the_claim()
    made = readback.controls(params, expected)
    assert len(made) == 9
    assert len({other for _, other in made}) == len({what for what, _ in made}) == 9
    for _, other in made:
        changed = [key for key in vars(expected) if getattr(other, key) != getattr(expected, key)]
        assert len(changed) == 1
    assert expected.flags() == [word for pair in claim().items() for word in pair]


@pytest.mark.parametrize("commit", ["0" * 40, "1" * 40, "f" * 40])
def test_control_of_another_commit_is_a_commit_and_another(commit):
    params = readback.parse(tomllib.loads(PARAMS))
    made = dict(readback.controls(params, the_claim(commit)))
    assert readback.COMMIT.fullmatch(made["another commit"].commit)
    assert made["another commit"].commit != commit


def test_report_without_a_line_is_no_pass(capsys):
    assert readback.Report().end() == 1
    assert capsys.readouterr().out.startswith("FAIL: 0 of 0")
