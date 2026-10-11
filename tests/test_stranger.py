# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""A stranger's reach: no request and no command carries what the user's session holds.

The commands are real processes of this interpreter. The requests go to a
stand-in for the opener, so no test reaches the network.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import sys
import urllib.error

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import stranger

URL = "https://forge.test/file"
SECRETS = ("GH_TOKEN", "GITHUB_TOKEN", "COSIGN_PASSWORD", "REGISTRY_AUTH_FILE", "XDG_CONFIG_HOME")
SHOW = "import json, os; print(json.dumps(dict(os.environ)))"


def opener(monkeypatch, answer):
    """Stand in for the opener; the requests it was given."""
    seen = []

    def urlopen(request, timeout):
        seen.append((request, timeout))
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    monkeypatch.setattr(stranger.urllib.request, "urlopen", urlopen)
    return seen


def refusal(status: int, headers: dict[str, str]) -> urllib.error.HTTPError:
    message = email.message.Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError(URL, status, "refused", message, None)


def test_command_sees_no_secret_and_an_empty_home(tmp_path, monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "planted")
    monkeypatch.setenv("DOCKER_CONFIG", "/home/user/.docker")
    code, out, _ = stranger.Stranger(tmp_path).run([sys.executable, "-I", "-c", SHOW])
    seen = json.loads(out)
    assert code == 0
    assert "planted" not in out
    assert not set(SECRETS) & set(seen)
    assert seen["HOME"] == str(tmp_path)
    assert seen["DOCKER_CONFIG"] == str(tmp_path / "docker")
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


def test_command_gives_its_exit_code_and_its_complaint(tmp_path):
    script = "import sys; sys.stderr.write('refused'); sys.exit(4)"
    assert stranger.Stranger(tmp_path).run([sys.executable, "-I", "-c", script]) == (
        4,
        "",
        "refused",
    )


def test_command_that_does_not_start_gives_no_code(tmp_path):
    code, out, complaint = stranger.Stranger(tmp_path).run([str(tmp_path / "no-such-tool")])
    assert (code, out) == (None, "")
    assert "no-such-tool" in complaint


def test_command_that_runs_out_of_time_gives_no_code(tmp_path, monkeypatch):
    monkeypatch.setattr(stranger, "COMMAND_SECONDS", 0.2)
    script = "import time; time.sleep(30)"
    code, _, complaint = stranger.Stranger(tmp_path).run([sys.executable, "-I", "-c", script])
    assert code is None
    assert "timed out" in complaint


def test_request_carries_no_authorisation_and_has_a_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "planted")
    seen = opener(monkeypatch, b"body")
    found = stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file", "application/json")
    request, timeout = seen[0]
    assert found == hashlib.sha256(b"body").hexdigest() == stranger.digest(tmp_path / "file")
    assert sorted(request.headers) == ["Accept", "User-agent"]
    assert timeout == stranger.DEADLINE_SECONDS


def test_request_to_an_address_without_tls_is_not_made(tmp_path, monkeypatch):
    seen = opener(monkeypatch, b"body")
    with pytest.raises(stranger.FetchError, match="not an https address"):
        stranger.Stranger(tmp_path).fetch("http://forge.test/file", tmp_path / "file")
    assert seen == []


@pytest.mark.parametrize(("size", "passes"), [(8, True), (9, False)])
def test_body_over_the_limit_is_refused(tmp_path, monkeypatch, size, passes):
    opener(monkeypatch, b"x" * size)
    if passes:
        stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file", limit=8)
        return
    with pytest.raises(stranger.FetchError, match="more than 8 bytes"):
        stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file", limit=8)


def test_refused_request_names_its_status_and_the_limit_left(tmp_path, monkeypatch):
    opener(monkeypatch, refusal(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "17"}))
    with pytest.raises(stranger.FetchError) as caught:
        stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file")
    assert caught.value.status == 403
    assert "HTTP 403 (x-ratelimit-remaining 0, x-ratelimit-reset 17)" in str(caught.value)


def test_missing_file_names_its_status_and_no_limit(tmp_path, monkeypatch):
    opener(monkeypatch, refusal(404, {"x-ratelimit-remaining": "41"}))
    with pytest.raises(stranger.FetchError) as caught:
        stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file")
    assert caught.value.status == 404
    assert str(caught.value) == f"GET {URL}: HTTP 404"


def test_request_without_an_answer_has_no_status(tmp_path, monkeypatch):
    opener(monkeypatch, urllib.error.URLError("no route"))
    with pytest.raises(stranger.FetchError) as caught:
        stranger.Stranger(tmp_path).fetch(URL, tmp_path / "file")
    assert caught.value.status is None
    assert "no route" in str(caught.value)
