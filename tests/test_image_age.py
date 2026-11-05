# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for tools/image_age.py: verify the licence image age check offline."""

from __future__ import annotations

import datetime
import io
import json
import re
import sys
import urllib.error
import urllib.request

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import image_age

# The pin the update bot last wrote; every version below is derived from it,
# so a release update of the licence image keeps these tests valid.
PINNED_VERSION, PINNED_DIGEST, _DEP = image_age.read_pin(ROOT)
MAJOR, MINOR, PATCH = (int(part) for part in PINNED_VERSION.split("."))
NEWER_VERSION = f"{MAJOR}.{MINOR + 1}.0"
OLDER_VERSION = "0.0.0"
TODAY = datetime.date(2026, 10, 16)


def make_tag(name: str, date_str: str, digest: str = PINNED_DIGEST) -> dict:
    """Build a tag entry resembling Docker Hub's response."""
    return {"name": name, "tag_last_pushed": date_str, "digest": digest}


def test_up_to_date_passes(capsys: pytest.CaptureFixture[str]):
    """When the pinned release is the newest on Hub, the check passes (exit 0)."""
    tags = [make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z")]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Pinned release: {PINNED_VERSION}" in out
    assert f"Newest release: {PINNED_VERSION}" in out
    assert f"PASS: pinned release {PINNED_VERSION} is up to date." in out


def test_planted_newer_release_older_than_14_days_fails(capsys: pytest.CaptureFixture[str]):
    """Planted (1) red: newest release > 14 days old and pin behind fails (exit 1)."""
    # 2026-10-01 is 15 days before 2026-10-16 (> 14 days)
    tags = [
        make_tag(
            NEWER_VERSION,
            "2026-10-01T00:00:00Z",
            "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        ),
        make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z"),
    ]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 1
    captured = capsys.readouterr()
    assert f"Pinned release: {PINNED_VERSION}" in captured.out
    assert f"Newest release: {NEWER_VERSION}" in captured.out
    assert (
        f"FAIL: newest release {NEWER_VERSION} is 15 days old (> 14 days), "
        f"but pinned release is {PINNED_VERSION}." in captured.err
    )


def test_planted_newer_release_younger_than_14_days_passes(capsys: pytest.CaptureFixture[str]):
    """Planted (2) green: newest release <= 14 days old passes (exit 0) with a note."""
    # 2026-10-10 is 6 days before 2026-10-16 (<= 14 days)
    tags = [
        make_tag(
            NEWER_VERSION,
            "2026-10-10T00:00:00Z",
            "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        ),
        make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z"),
    ]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 0
    captured = capsys.readouterr()
    assert f"Pinned release: {PINNED_VERSION}" in captured.out
    assert f"Newest release: {NEWER_VERSION}" in captured.out
    assert (
        f"PASS: newer release {NEWER_VERSION} is 6 day(s) old (Renovate proposes it after 3 days)."
        in captured.out
    )


def test_planted_timeout_fails_with_exit_3(capsys: pytest.CaptureFixture[str]):
    """Planted (3) timeout: reading registry times out, fails with exit 3."""

    def raise_timeout():
        raise image_age.ReadError("Docker Hub request timed out after 20s")

    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=raise_timeout)
    assert rc == 3
    err = capsys.readouterr().err
    assert "ERROR: Docker Hub request timed out after 20s" in err


def test_planted_http_500_fails_with_exit_3(capsys: pytest.CaptureFixture[str]):
    """Planted (3) HTTP error: registry returns HTTP 500, fails with exit 3."""

    def raise_http():
        raise image_age.ReadError("Docker Hub HTTP 500: Server Error")

    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=raise_http)
    assert rc == 3
    err = capsys.readouterr().err
    assert "ERROR: Docker Hub HTTP 500: Server Error" in err


def test_planted_garbage_json_fails_with_exit_3(capsys: pytest.CaptureFixture[str]):
    """Planted (3) garbage JSON: registry returns invalid JSON, fails with exit 3."""

    def raise_json():
        raise image_age.ReadError(
            "Docker Hub returned malformed JSON: Expecting value: line 1 column 1 (char 0)"
        )

    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=raise_json)
    assert rc == 3
    err = capsys.readouterr().err
    assert "ERROR: Docker Hub returned malformed JSON" in err


def test_moved_digest_fails_with_exit_1(capsys: pytest.CaptureFixture[str]):
    """If the pinned release tag's digest on Hub moved, fails with exit 1."""
    moved_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    tags = [make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z", digest=moved_digest)]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 1
    captured = capsys.readouterr()
    assert f"FAIL: pinned release {PINNED_VERSION} tag digest on Docker Hub moved" in captured.err
    assert PINNED_DIGEST in captured.err
    assert moved_digest in captured.err


@pytest.mark.parametrize(
    ("date_str", "expected_code", "expected_status"),
    [
        # Exactly 14 days old: 2026-10-02 -> 14 days old -> PASS (not > 14 days)
        ("2026-10-02T00:00:00Z", 0, f"PASS: newer release {NEWER_VERSION} is 14 day(s) old"),
        # Exactly 15 days old: 2026-10-01 -> 15 days old -> FAIL (> 14 days)
        (
            "2026-10-01T00:00:00Z",
            1,
            f"FAIL: newest release {NEWER_VERSION} is 15 days old (> 14 days)",
        ),
    ],
)
def test_boundary_exactly_14_days(
    capsys: pytest.CaptureFixture[str], date_str: str, expected_code: int, expected_status: str
):
    """Boundary test: older than 14 days means strictly > 14 days."""
    tags = [
        make_tag(
            NEWER_VERSION,
            date_str,
            "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        ),
        make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z"),
    ]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == expected_code
    captured = capsys.readouterr()
    output = captured.out if expected_code == 0 else captured.err
    assert expected_status in output


def test_non_release_tags_ignored(capsys: pytest.CaptureFixture[str]):
    """Tags not matching the release regex (two-part, latest, suffixed, pre-release) are ignored."""
    tags = [
        make_tag("latest", "2026-10-15T00:00:00Z"),
        make_tag(f"{MAJOR}.{MINOR}", "2026-10-15T00:00:00Z"),
        make_tag(f"{PINNED_VERSION}-debian", "2026-10-15T00:00:00Z"),
        make_tag(f"{MAJOR + 1}.0.0-rc1", "2026-10-15T00:00:00Z"),
        make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z"),
    ]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 0
    out = capsys.readouterr().out
    assert f"Newest release: {PINNED_VERSION}" in out
    assert f"PASS: pinned release {PINNED_VERSION} is up to date." in out


def test_renovate_regex_mismatch_fails_with_exit_3(tree, capsys: pytest.CaptureFixture[str]):
    """When the regex from renovate.json does not match licence.yml, fails with exit 3."""
    bad_renovate = json.dumps(
        {
            "customManagers": [
                {
                    "matchStrings": [
                        "# release (?<currentValue>\\S+)\\n\\s+uses: docker://(?<depName>docker\\.io/other/tool)@(?<currentDigest>sha256:[0-9a-f]{64})\\n"
                    ]
                }
            ]
        }
    )
    licence_content = (ROOT / ".github/workflows/licence.yml").read_text(encoding="utf-8")
    tree.write(
        {
            "renovate.json": bad_renovate,
            ".github/workflows/licence.yml": licence_content,
        }
    )
    rc = image_age.check(root=tree.root, today=TODAY, fetch_fn=lambda: [])
    assert rc == 3
    err = capsys.readouterr().err
    assert "regex from renovate.json did not match image line" in err


def test_pinned_release_missing_on_hub_fails_with_exit_3(capsys: pytest.CaptureFixture[str]):
    """When the pinned release is absent on Docker Hub, fails with exit 3."""
    tags = [make_tag(OLDER_VERSION, "2025-05-01T00:00:00Z")]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 3
    err = capsys.readouterr().err
    assert f"pinned release {PINNED_VERSION} missing on Docker Hub" in err


def test_no_release_tags_on_hub_fails_with_exit_3(capsys: pytest.CaptureFixture[str]):
    """When Hub contains no release tags matching the regex, fails with exit 3."""
    tags = [make_tag("latest", "2026-10-15T00:00:00Z"), make_tag("dev", "2026-10-15T00:00:00Z")]
    rc = image_age.check(root=ROOT, today=TODAY, fetch_fn=lambda: tags)
    assert rc == 3
    err = capsys.readouterr().err
    assert "no release tags found on Docker Hub" in err


def test_fetch_hub_tags_parses_json(monkeypatch: pytest.MonkeyPatch):
    """fetch_hub_tags successfully parses and bounds valid Docker Hub JSON."""
    raw_json = json.dumps(
        {"results": [{"name": "1.2.3", "tag_last_pushed": "2025-10-27T15:28:22Z"}]}
    ).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return raw_json

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResponse())
    results = image_age.fetch_hub_tags("https://hub.example/tags")
    assert len(results) == 1
    assert results[0]["name"] == "1.2.3"


def test_fetch_hub_tags_http_error(monkeypatch: pytest.MonkeyPatch):
    """fetch_hub_tags wraps HTTPError in ReadError."""

    def raise_http(req, timeout):
        raise urllib.error.HTTPError(
            "https://hub.docker.com", 404, "Not Found", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_http)
    with pytest.raises(image_age.ReadError, match="Docker Hub HTTP 404"):
        image_age.fetch_hub_tags("https://hub.example/tags")


def test_main_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """main CLI handles check subcommand with arguments."""
    tags = [make_tag(PINNED_VERSION, "2025-10-27T15:28:22Z")]
    asked = []
    monkeypatch.setattr(image_age, "fetch_hub_tags", lambda url: asked.append(url) or tags)
    rc = image_age.main(
        [
            "check",
            "--tags-url",
            "https://hub.example/tags",
            "--root",
            str(ROOT),
            "--today",
            "2026-10-16",
        ]
    )
    assert asked == ["https://hub.example/tags"]
    assert rc == 0
    assert f"PASS: pinned release {PINNED_VERSION} is up to date." in capsys.readouterr().out


def test_pin_matches_an_independent_read_of_the_licence_workflow():
    """read_pin returns the release and digest the licence workflow carries."""
    text = (ROOT / image_age.LICENCE_FILE).read_text(encoding="utf-8")
    (found,) = re.findall(
        r"^ +# release (\d+\.\d+\.\d+)\n +uses: docker://(\S+)@(sha256:[0-9a-f]{64})$",
        text,
        flags=re.MULTILINE,
    )
    assert found == (PINNED_VERSION, _DEP, PINNED_DIGEST)
    assert OLDER_VERSION != PINNED_VERSION


def pin_literals(text: str) -> list[str]:
    """The live pin's release or digest as they appear literally in a test source."""
    found = re.findall(rf"(?<![\d.]){re.escape(PINNED_VERSION)}(?![\d.])", text)
    return found + re.findall(re.escape(PINNED_DIGEST.split(":", 1)[1]), text)


def test_no_test_names_the_live_pin_literally():
    """A release update rewrites the pin, so a test that spells it out breaks on every update."""
    offenders = {
        path.name: pin_literals(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "tests").glob("*.py"))
    }
    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_planted_pin_literal_is_found():
    """Planted: a test source that spells the live release or digest is reported."""
    assert pin_literals(f'make_tag("{PINNED_VERSION}", ...)') == [PINNED_VERSION]
    assert pin_literals(f"digest = '{PINNED_DIGEST}'") == [PINNED_DIGEST.split(":", 1)[1]]
    assert pin_literals(f'make_tag("1{PINNED_VERSION}.9", ...)') == []
