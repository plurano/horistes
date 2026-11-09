# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for shared installer actions (verified-download, install-gitleaks)."""

from __future__ import annotations

import hashlib
import io
import os
import secrets
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml
from conftest import ROOT, STUB_GH_TOKEN

INSTALL_GITLEAKS_SH = ROOT / "actions/install-gitleaks/install-gitleaks.sh"
VERIFIED_DOWNLOAD_SH = ROOT / "actions/verified-download/verified-download.sh"
ACTION_INSTALL_GITLEAKS_YML = ROOT / "actions/install-gitleaks/action.yml"
ACTION_VERIFIED_DOWNLOAD_YML = ROOT / "actions/verified-download/action.yml"
DEFAULT_VERSION = "8.30.1"
# The endless gh stub offers this many chunks of this size, 64 MiB in all.
ENDLESS_CHUNK = 65536
ENDLESS_CHUNKS = 1024
ENDLESS_BYTES = ENDLESS_CHUNK * ENDLESS_CHUNKS


def make_archive(dest_dir: Path, name: str, version: str, shape: str = "regular") -> Path:
    """Create a mock gitleaks tar.gz archive with regular binary or symlink."""
    archive_path = dest_dir / f"{name}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        if shape == "symlink":
            ti = tarfile.TarInfo(name="gitleaks")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/bin/true"
            tar.addfile(ti)
        else:
            content = f"#!/bin/sh\nprintf '%s\\n' '{version}'\n".encode()
            ti = tarfile.TarInfo(name="gitleaks")
            ti.size = len(content)
            ti.mode = 0o755
            tar.addfile(ti, io.BytesIO(content))
        license_data = b"MIT\n"
        ti_lic = tarfile.TarInfo(name="LICENSE")
        ti_lic.size = len(license_data)
        ti_lic.mode = 0o644
        tar.addfile(ti_lic, io.BytesIO(license_data))
    return archive_path


def sha256_of(path: Path) -> str:
    """Return lowercase hex SHA-256 digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def corrupt_digest(digest: str) -> str:
    """Flip the first character of a hex digest so it differs."""
    first = "1" if digest[0] == "0" else "0"
    return f"{first}{digest[1:]}"


CURL_STUB_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail
output=''
url=''
while (($# > 0)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --retry | --connect-timeout | --max-time | --max-filesize | --proto | --proto-redir)
      printf '%s %s\\n' "$1" "$2" >>"{logs_dir}/curl_args.log"
      shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\\n' "$url" >>"{logs_dir}/curl.log"
if [[ -n "${{CURL_HANG:-}}" ]]; then
  exec sleep "${{CURL_HANG}}"
fi
if [[ "$url" != https://* ]]; then
  printf 'curl: (1) Protocol not supported: %s\\n' "$url" >&2
  exit 1
fi
if [[ -n "${{CURL_OUTAGE:-}}" ]]; then
  if [[ "$url" == *"github.com"* ]]; then
    printf 'curl: (22) The requested URL returned error: 504\\n' >&2
    exit 22
  fi
fi
if [[ -n "${{CURL_FAIL:-}}" ]]; then
  exit 1
fi
if [[ -n "${{FIXTURE_ARCHIVE:-}}" && -n "$output" ]]; then
  cp "${{FIXTURE_ARCHIVE}}" "$output"
fi
"""

GH_STUB_TEMPLATE = """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
logs = Path('{logs_dir}')
with open(logs / 'gh.log', 'a', encoding='utf-8') as handle:
    handle.write(' '.join(args) + '\\n')

token = os.environ.get('GH_TOKEN')
if not token:
    sys.stderr.write('gh stub: GH_TOKEN unset or empty\\n')
    sys.exit(1)

if (
    len(args) != 9
    or args[0] != 'release'
    or args[1] != 'download'
    or args[2].startswith('-')
    or args[3] != '--repo'
    or not args[4]
    or args[5] != '--pattern'
    or not args[6]
    or args[7] != '--output'
    or args[8] != '-'
):
    sys.stderr.write(f'gh stub: args differ from required shape: {{args}}\\n')
    sys.exit(1)

mode = os.environ.get('STUB_GH_MODE', 'healthy')
fix_path = os.environ.get('FIXTURE_ARCHIVE')
out = sys.stdout.buffer

if mode == 'healthy':
    if fix_path:
        out.write(Path(fix_path).read_bytes())
    sys.exit(0)

if mode == 'wrong_bytes':
    out.write(b'corrupted bytes payload\\n')
    sys.exit(0)

if mode == 'endless':
    written = 0
    chunk = b'x' * {endless_chunk}
    try:
        for _ in range({endless_chunks}):
            out.write(chunk)
            out.flush()
            written += len(chunk)
    except BrokenPipeError:
        (logs / 'gh_written').write_text(str(written), encoding='utf-8')
        os._exit(1)
    (logs / 'gh_written').write_text(str(written), encoding='utf-8')
    sys.exit(0)

if mode == 'hang':
    time.sleep(float(os.environ.get('STUB_GH_HANG', '30')))
    if fix_path:
        out.write(Path(fix_path).read_bytes())
    sys.exit(0)

if mode == 'missing_asset':
    sys.stderr.write('no assets match the file pattern\\n')
    sys.exit(1)

sys.stderr.write(f'gh stub: unknown mode {{mode}}\\n')
sys.exit(1)
"""

UNAME_STUB_SH = """#!/usr/bin/env bash
case "$1" in
  -s) printf '%s\\n' "${FAKE_UNAME_S:-Linux}" ;;
  -m) printf '%s\\n' "${FAKE_UNAME_M:-x86_64}" ;;
  *) exit 2 ;;
esac
"""


def setup_fake_environment(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create fake curl, uname and gh binaries on PATH and return paths."""
    bin_dir = tmp_path / "bin"
    logs_dir = tmp_path / "logs"
    bin_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    curl_file = bin_dir / "curl"
    curl_file.write_text(CURL_STUB_TEMPLATE.format(logs_dir=logs_dir), encoding="utf-8")
    curl_file.chmod(0o755)

    gh_file = bin_dir / "gh"
    gh_file.write_text(
        GH_STUB_TEMPLATE.format(
            logs_dir=logs_dir, endless_chunk=ENDLESS_CHUNK, endless_chunks=ENDLESS_CHUNKS
        ),
        encoding="utf-8",
    )
    gh_file.chmod(0o755)

    uname_file = bin_dir / "uname"
    uname_file.write_text(UNAME_STUB_SH, encoding="utf-8")
    uname_file.chmod(0o755)

    return bin_dir, logs_dir, curl_file


def run_bash_cmd(
    cmd: list[str],
    bin_dir: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run bash command with fake bin on PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    # nosemgrep: dangerous-subprocess-use-audit callers pass fixed argv with stubs on PATH
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False, timeout=60)


# ---------------------------------------------------------------------------
# verified-download.sh unit tests
# ---------------------------------------------------------------------------


def test_verified_download_positive(tmp_path: Path) -> None:
    """fetch_verified downloads file and succeeds when sha256 matches."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    fixture_file = tmp_path / "data.txt"
    fixture_file.write_bytes(b"sample payload contents\n")
    digest = sha256_of(fixture_file)
    output_file = tmp_path / "downloaded.txt"

    script = (
        f'. "{VERIFIED_DOWNLOAD_SH}"\n'
        f'fetch_verified "https://example.com/asset" "{digest}" 1048576 60 "{output_file}"\n'
    )
    res = run_bash_cmd(
        ["bash", "-c", script],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(fixture_file)},
    )
    assert res.returncode == 0, res.stderr
    assert output_file.read_bytes() == fixture_file.read_bytes()
    logged_args = (logs_dir / "curl_args.log").read_text(encoding="utf-8")
    assert "--proto =https" in logged_args
    assert "--proto-redir =https" in logged_args
    assert "--max-filesize 1048576" in logged_args
    assert "--max-time 60" in logged_args


def test_verified_download_digest_mismatch_fails(tmp_path: Path) -> None:
    """fetch_verified fails when the downloaded file digest differs."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    fixture_file = tmp_path / "data.txt"
    fixture_file.write_bytes(b"sample payload\n")
    mismatched = corrupt_digest(sha256_of(fixture_file))
    output_file = tmp_path / "downloaded.txt"

    script = (
        f'. "{VERIFIED_DOWNLOAD_SH}"\n'
        f'fetch_verified "https://example.com/asset" "{mismatched}" 1048576 60 "{output_file}"\n'
    )
    res = run_bash_cmd(
        ["bash", "-c", script],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(fixture_file)},
    )
    assert res.returncode != 0
    assert "archive digest mismatch for https://example.com/asset" in res.stderr


def test_verified_download_invalid_digest_regex_fails(tmp_path: Path) -> None:
    """fetch_verified fails immediately when sha256 is not 64 hex characters."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    output_file = tmp_path / "out.txt"

    invalid_digests = ["not-a-hash", "abc", "1" * 63, "1" * 65, "G" * 64]
    for bad in invalid_digests:
        script = (
            f'. "{VERIFIED_DOWNLOAD_SH}"\n'
            f'fetch_verified "https://example.com/asset" "{bad}" 1048576 60 "{output_file}"\n'
        )
        res = run_bash_cmd(["bash", "-c", script], bin_dir)
        assert res.returncode != 0
        assert f"invalid pinned archive digest: {bad}" in res.stderr
    assert not (logs_dir / "curl.log").exists()


def test_verified_download_non_https_url_refused_by_curl(tmp_path: Path) -> None:
    """fetch_verified enforces HTTPS through curl proto arguments and refuses http."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    fixture_file = tmp_path / "data.txt"
    fixture_file.write_bytes(b"data\n")
    digest = sha256_of(fixture_file)
    output_file = tmp_path / "out.txt"

    url = "http://insecure.example.com/asset"
    script = (
        f'. "{VERIFIED_DOWNLOAD_SH}"\n'
        f'fetch_verified "{url}" "{digest}" 1048576 60 "{output_file}"\n'
    )
    res = run_bash_cmd(
        ["bash", "-c", script],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(fixture_file)},
    )
    assert res.returncode != 0
    logged_args = (logs_dir / "curl_args.log").read_text(encoding="utf-8")
    assert "--proto =https" in logged_args
    assert "--proto-redir =https" in logged_args


def test_verified_download_max_bytes_passed_as_max_filesize(tmp_path: Path) -> None:
    """max-bytes parameter is passed directly as --max-filesize to curl."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    fixture_file = tmp_path / "data.txt"
    fixture_file.write_bytes(b"small payload\n")
    digest = sha256_of(fixture_file)
    output_file = tmp_path / "out.txt"

    script = (
        f'. "{VERIFIED_DOWNLOAD_SH}"\n'
        f'fetch_verified "https://example.com/asset" "{digest}" 33554432 120 "{output_file}"\n'
    )
    res = run_bash_cmd(
        ["bash", "-c", script],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(fixture_file)},
    )
    assert res.returncode == 0
    logged_args = (logs_dir / "curl_args.log").read_text(encoding="utf-8")
    assert "--max-filesize 33554432" in logged_args


# ---------------------------------------------------------------------------
# install-gitleaks.sh tests
# ---------------------------------------------------------------------------


def test_install_gitleaks_positive(tmp_path: Path) -> None:
    """Matching digest installs binary, verifies read-back version, creates mode 0755."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "good", DEFAULT_VERSION, shape="regular")
    digest = sha256_of(archive)
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, digest, str(install_dir)],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(archive)},
    )
    assert res.returncode == 0, res.stderr
    assert f"installed verified gitleaks {DEFAULT_VERSION}" in res.stdout

    installed_binary = install_dir / "gitleaks"
    assert installed_binary.is_file()
    assert os.access(installed_binary, os.X_OK)

    version_proc = subprocess.run(
        [str(installed_binary), "version"], capture_output=True, text=True, check=True
    )
    assert version_proc.stdout.strip() == DEFAULT_VERSION

    expected_url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/"
        f"v{DEFAULT_VERSION}/gitleaks_{DEFAULT_VERSION}_linux_x64.tar.gz"
    )
    assert (logs_dir / "curl.log").read_text(encoding="utf-8").strip() == expected_url


def test_install_gitleaks_digest_mismatch_fails(tmp_path: Path) -> None:
    """Corrupted archive digest fails and leaves no binary behind."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "mismatch", DEFAULT_VERSION, shape="regular")
    bad_digest = corrupt_digest(sha256_of(archive))
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, bad_digest, str(install_dir)],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(archive)},
    )
    assert res.returncode != 0
    assert "archive digest mismatch" in res.stderr
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_invalid_digest_regex_fails(tmp_path: Path) -> None:
    """Invalid digest string is rejected before download."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "good", DEFAULT_VERSION, shape="regular")
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, "not-a-digest", str(install_dir)],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(archive)},
    )
    assert res.returncode == 1
    assert "invalid pinned gitleaks digest: not-a-digest" in res.stderr
    assert not (logs_dir / "curl.log").exists()
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_unsupported_platform_fails(tmp_path: Path) -> None:
    """Non-Linux/x86_64 host platform is refused."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "good", DEFAULT_VERSION, shape="regular")
    digest = sha256_of(archive)
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, digest, str(install_dir)],
        bin_dir,
        env_extra={
            "FIXTURE_ARCHIVE": str(archive),
            "FAKE_UNAME_S": "Darwin",
            "FAKE_UNAME_M": "arm64",
        },
    )
    assert res.returncode == 1
    assert "no pinned gitleaks archive digest for Darwin/arm64" in res.stderr
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_archive_with_symlink_fails(tmp_path: Path) -> None:
    """Archive with symlinked binary entry is refused."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "symlink", DEFAULT_VERSION, shape="symlink")
    digest = sha256_of(archive)
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, digest, str(install_dir)],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(archive)},
    )
    assert res.returncode == 1
    assert "gitleaks archive does not contain a regular gitleaks binary" in res.stderr
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_empty_version_refused(tmp_path: Path) -> None:
    """Empty version string is refused."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    fake_digest = "a" * 64
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), "", fake_digest, str(install_dir)],
        bin_dir,
    )
    assert res.returncode != 0
    assert "invalid GITLEAKS_VERSION:" in res.stderr
    assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_malformed_version_refused(tmp_path: Path) -> None:
    """Version not matching semver regex is refused."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    fake_digest = "a" * 64
    install_dir = tmp_path / "installed_bin"

    for bad_ver in ["8.30", "v8.30.1", "version-8", "8.30.1-rc1"]:
        res = run_bash_cmd(
            ["bash", str(INSTALL_GITLEAKS_SH), bad_ver, fake_digest, str(install_dir)],
            bin_dir,
        )
        assert res.returncode == 1
        assert f"invalid GITLEAKS_VERSION: {bad_ver}" in res.stderr
        assert not (install_dir / "gitleaks").exists()


def test_install_gitleaks_version_mismatch_fails(tmp_path: Path) -> None:
    """Archive binary reporting unexpected version is refused."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "wrong_ver", "9.9.9", shape="regular")
    digest = sha256_of(archive)
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, digest, str(install_dir)],
        bin_dir,
        env_extra={"FIXTURE_ARCHIVE": str(archive)},
    )
    assert res.returncode == 1
    assert "gitleaks reports version 9.9.9, want 8.30.1" in res.stderr
    assert not (install_dir / "gitleaks").exists()


# ---------------------------------------------------------------------------
# verified-download action.yml run step
# ---------------------------------------------------------------------------


def run_download_step(
    tmp_path: Path, bin_dir: Path, inputs: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the action's composite step as the runner would and return its output file."""
    action = yaml.safe_load(ACTION_VERIFIED_DOWNLOAD_YML.read_text(encoding="utf-8"))
    step = action["runs"]["steps"][0]
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    env = {
        "GITHUB_ACTION_PATH": str(VERIFIED_DOWNLOAD_SH.parent),
        "GITHUB_OUTPUT": str(out_file),
        "DOWNLOAD_URL": inputs.get("url", ""),
        "DOWNLOAD_SHA256": inputs.get("sha256", ""),
        "DOWNLOAD_OUTPUT": inputs.get("output", ""),
        "DOWNLOAD_MAX_BYTES": "1048576",
        "DOWNLOAD_TIMEOUT": "60",
        "FIXTURE_ARCHIVE": inputs.get("fixture", ""),
    }
    res = run_bash_cmd(["bash", "-eo", "pipefail", "-c", step["run"]], bin_dir, env_extra=env)
    return res, out_file.read_text(encoding="utf-8")


def test_action_lib_only_mode_skips_download(tmp_path: Path) -> None:
    """With url, sha256 and output empty the step only reports the library path."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    res, outputs = run_download_step(tmp_path, bin_dir, {})
    assert res.returncode == 0, res.stderr
    assert outputs == f"lib={VERIFIED_DOWNLOAD_SH}\n"
    assert not (logs_dir / "curl.log").exists()


def test_action_partial_inputs_fail(tmp_path: Path) -> None:
    """A url without sha256 and output is refused before any download."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    res, _ = run_download_step(tmp_path, bin_dir, {"url": "https://example.com/asset"})
    assert res.returncode != 0
    assert "set together or not at all" in res.stderr
    assert not (logs_dir / "curl.log").exists()


def test_action_full_inputs_download(tmp_path: Path) -> None:
    """With all three inputs the step downloads and verifies the file."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    fixture_file = tmp_path / "data.txt"
    fixture_file.write_bytes(b"action payload\n")
    output_file = tmp_path / "downloaded.txt"
    res, _ = run_download_step(
        tmp_path,
        bin_dir,
        {
            "url": "https://example.com/asset",
            "sha256": sha256_of(fixture_file),
            "output": str(output_file),
            "fixture": str(fixture_file),
        },
    )
    assert res.returncode == 0, res.stderr
    assert output_file.read_bytes() == fixture_file.read_bytes()


# ---------------------------------------------------------------------------
# fetch_verified fallback tests
# ---------------------------------------------------------------------------

RELEASE_URL = (
    "https://github.com/rhysd/actionlint/releases/download/v1.7.7/"
    "actionlint_1.7.7_linux_amd64.tar.gz"
)
# The bounds reach fetch_verified through the environment, so a planted value
# arrives as written, with no expansion by the calling script.
FETCH_SCRIPT = (
    '. "$VERIFIED_DOWNLOAD"\n'
    'fetch_verified "$FETCH_URL" "$FETCH_SHA256" '
    '"$FETCH_MAX_BYTES" "$FETCH_TIMEOUT" "$FETCH_OUTPUT"\n'
)
FALLBACK_SCRIPT = (
    '. "$VERIFIED_DOWNLOAD"\n'
    'fetch_release_fallback "$FETCH_URL" "$FETCH_MAX_BYTES" "$FETCH_TIMEOUT" "$FETCH_OUTPUT"\n'
)


class Fetched(NamedTuple):
    """What one fetch left behind: the process, the stub logs, the output and its source."""

    res: subprocess.CompletedProcess[str]
    logs_dir: Path
    output: Path
    fixture: Path


def fetch_release(
    tmp_path: Path,
    env: dict[str, str],
    *,
    url: str = RELEASE_URL,
    max_bytes: str = "1048576",
    timeout_seconds: str = "60",
    payload: bytes = b"sample payload contents\n",
    script: str = FETCH_SCRIPT,
) -> Fetched:
    """Run `script` against the stubs, pinned to the digest of `payload`."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    fixture = tmp_path / "data.txt"
    fixture.write_bytes(payload)
    output = tmp_path / "downloaded.txt"
    res = run_bash_cmd(
        ["bash", "-c", script],
        bin_dir,
        env_extra={
            "VERIFIED_DOWNLOAD": str(VERIFIED_DOWNLOAD_SH),
            "FETCH_URL": url,
            "FETCH_SHA256": sha256_of(fixture),
            "FETCH_MAX_BYTES": max_bytes,
            "FETCH_TIMEOUT": timeout_seconds,
            "FETCH_OUTPUT": str(output),
            "FIXTURE_ARCHIVE": str(fixture),
            "GH_TOKEN": STUB_GH_TOKEN,
            **env,
        },
    )
    return Fetched(res, logs_dir, output, fixture)


def test_fetch_verified_fallback_outage(tmp_path: Path) -> None:
    """Under outage, fetch_verified streams the asset from gh release download and verifies it."""
    got = fetch_release(tmp_path, {"CURL_OUTAGE": "1"})
    assert got.res.returncode == 0, got.res.stderr
    assert got.output.read_bytes() == got.fixture.read_bytes()
    assert "::warning title=Release download fallback::" in got.res.stdout
    assert "failed; fetching it with gh release download" in got.res.stdout
    calls = (got.logs_dir / "gh.log").read_text(encoding="utf-8").splitlines()
    expected_args = [
        "release",
        "download",
        "v1.7.7",
        "--repo",
        "rhysd/actionlint",
        "--pattern",
        "actionlint_1.7.7_linux_amd64.tar.gz",
        "--output",
        "-",
    ]
    assert [call.split() for call in calls] == [expected_args]


def test_fetch_verified_fallback_healthy_curl(tmp_path: Path) -> None:
    """When curl succeeds, gh is not called and no warning is emitted."""
    got = fetch_release(tmp_path, {})
    assert got.res.returncode == 0, got.res.stderr
    assert got.output.read_bytes() == got.fixture.read_bytes()
    assert "::warning title=Release download fallback::" not in got.res.stdout
    assert not (got.logs_dir / "gh.log").exists()


@pytest.mark.parametrize(
    ("url", "env"),
    [
        ("https://example.com/releases/download/v1.0.0/asset.tar.gz", {"CURL_FAIL": "1"}),
        (
            "https://github.com/rhysd/actionlint/releases/download/v1.7.7/*.tar.gz",
            {"CURL_OUTAGE": "1"},
        ),
        (
            "https://example.com/rhysd/actionlint/releases/download/v1.7.7/actionlint.tar.gz",
            {"CURL_FAIL": "1"},
        ),
        (
            "https://github.com.example.org/rhysd/actionlint/releases/download/v1.7.7/a.tar.gz",
            {"CURL_FAIL": "1"},
        ),
        (
            "http://github.com/rhysd/actionlint/releases/download/v1.7.7/a.tar.gz",
            {"CURL_FAIL": "1"},
        ),
    ],
    ids=["non-github", "glob-asset", "other-host", "lookalike-host", "plain-http"],
)
def test_fetch_verified_fallback_refuses_a_url_off_the_release_path(
    tmp_path: Path, url: str, env: dict[str, str]
) -> None:
    """A URL that is not a github.com release asset never reaches gh."""
    got = fetch_release(tmp_path, env, url=url)
    assert got.res.returncode != 0
    assert not (got.logs_dir / "gh.log").exists()


def test_fetch_verified_fallback_missing_gh_fails(tmp_path: Path) -> None:
    """When gh is missing from PATH, fallback fails and names the missing tool."""
    bin_dir, _, _ = setup_fake_environment(tmp_path)
    (bin_dir / "gh").unlink()
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for tool in ("bash", "sh", "sha256sum", "awk", "wc", "sleep", "head", "rm"):
        real = shutil.which(tool)
        if real:
            (tools_dir / tool).symlink_to(real)
    env = {
        "PATH": f"{bin_dir}:{tools_dir}",
        "CURL_OUTAGE": "1",
        "VERIFIED_DOWNLOAD": str(VERIFIED_DOWNLOAD_SH),
        "FETCH_URL": RELEASE_URL,
        "FETCH_SHA256": "0" * 64,
        "FETCH_MAX_BYTES": "1048576",
        "FETCH_TIMEOUT": "60",
        "FETCH_OUTPUT": str(tmp_path / "out.txt"),
    }
    # nosemgrep: dangerous-subprocess-use-audit runs bash on a fixed script with stub tools on PATH
    res = subprocess.run(
        ["bash", "-c", FETCH_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert res.returncode != 0
    assert f"no gh on PATH for the release download fallback of {RELEASE_URL}" in res.stderr


def test_fetch_verified_fallback_gh_wrong_bytes_fails(tmp_path: Path) -> None:
    """When gh writes wrong bytes, digest mismatch causes failure."""
    got = fetch_release(tmp_path, {"CURL_OUTAGE": "1", "STUB_GH_MODE": "wrong_bytes"})
    assert got.res.returncode != 0
    assert f"archive digest mismatch for {RELEASE_URL}" in got.res.stderr


def test_fetch_verified_fallback_stops_an_endless_asset_at_the_bound(tmp_path: Path) -> None:
    """gh's stream is cut one byte past max-bytes, so the fetch stops and leaves no file.

    The stub offers ENDLESS_BYTES; the pipe takes about one buffer before head
    exits, so gh writes far less than it offers.
    """
    got = fetch_release(tmp_path, {"CURL_OUTAGE": "1", "STUB_GH_MODE": "endless"}, max_bytes="1024")
    assert got.res.returncode != 0
    assert (
        f"release download for {RELEASE_URL} exceeded max bytes 1024; stopped and removed"
        in got.res.stderr
    )
    assert not got.output.exists()
    written = int((got.logs_dir / "gh_written").read_text(encoding="utf-8"))
    assert written < ENDLESS_BYTES // 16, written


@pytest.mark.parametrize(
    ("slack", "lands"), [(0, True), (-1, False)], ids=["at-the-bound", "one-byte-over"]
)
def test_fetch_verified_fallback_size_bound_is_inclusive(
    tmp_path: Path, slack: int, lands: bool
) -> None:
    """An asset of exactly max-bytes lands; one byte more is refused and removed."""
    payload = b"exact payload\n"
    got = fetch_release(
        tmp_path,
        {"CURL_OUTAGE": "1"},
        max_bytes=str(len(payload) + slack),
        payload=payload,
    )
    assert (got.res.returncode == 0) is lands, got.res.stderr
    assert got.output.exists() is lands


def test_fetch_verified_fallback_gh_exit_1_fails(tmp_path: Path) -> None:
    """When gh exits 1 (missing asset), fallback fails, names the status and leaves no file."""
    got = fetch_release(tmp_path, {"CURL_OUTAGE": "1", "STUB_GH_MODE": "missing_asset"})
    assert got.res.returncode != 0
    assert "no assets match the file pattern" in got.res.stderr
    assert f"gh release download for {RELEASE_URL} failed with status 1" in got.res.stderr
    assert not got.output.exists()


def test_fetch_verified_fallback_gh_is_bounded_by_the_timeout(tmp_path: Path) -> None:
    """A gh that hangs past the timeout is stopped; the call fails instead of waiting."""
    started = time.monotonic()
    got = fetch_release(
        tmp_path,
        {"CURL_OUTAGE": "1", "STUB_GH_MODE": "hang", "STUB_GH_HANG": "20"},
        timeout_seconds="1",
    )
    assert got.res.returncode != 0
    assert time.monotonic() - started < 15
    assert (got.logs_dir / "gh.log").is_file()
    assert not got.output.exists()


def test_fetch_verified_curl_is_bounded_by_the_timeout(tmp_path: Path) -> None:
    """A curl that hangs past the timeout is stopped and the fallback still runs."""
    started = time.monotonic()
    got = fetch_release(tmp_path, {"CURL_HANG": "20"}, timeout_seconds="1")
    assert got.res.returncode == 0, got.res.stderr
    assert time.monotonic() - started < 15
    assert got.output.read_bytes() == got.fixture.read_bytes()
    assert (got.logs_dir / "gh.log").is_file()


@pytest.mark.parametrize("script", [FETCH_SCRIPT, FALLBACK_SCRIPT], ids=["fetch", "fallback"])
@pytest.mark.parametrize(
    ("max_bytes", "timeout_seconds", "named"),
    [
        ("x[$(touch {marker})]", "60", "invalid max bytes"),
        ("1048576", "x[$(touch {marker})]", "invalid timeout seconds"),
        ("0", "60", "invalid max bytes"),
        ("1048576", "0", "invalid timeout seconds"),
        ("-1", "60", "invalid max bytes"),
        ("1048576", "1000000", "invalid timeout seconds"),
    ],
    ids=[
        "max-substitution",
        "timeout-substitution",
        "max-zero",
        "timeout-zero",
        "negative",
        "timeout-7-digits",
    ],
)
def test_bounds_that_are_not_plain_numbers_are_refused(
    tmp_path: Path, script: str, max_bytes: str, timeout_seconds: str, named: str
) -> None:
    """Both entry points refuse a bound before bash arithmetic could evaluate it."""
    marker = tmp_path / "marker"
    got = fetch_release(
        tmp_path,
        {"CURL_OUTAGE": "1"},
        max_bytes=max_bytes.format(marker=marker),
        timeout_seconds=timeout_seconds.format(marker=marker),
        script=script,
    )
    assert got.res.returncode != 0
    assert named in got.res.stderr
    assert not marker.exists()
    assert not (got.logs_dir / "curl.log").exists()
    assert not (got.logs_dir / "gh.log").exists()


def test_run_with_deadline_leaves_no_sleep_behind(tmp_path: Path) -> None:
    """Stopping the deadline watcher at any point stops its sleep as well.

    A child that exits at once races the watcher's start; the old watcher left
    about one sleep in twelve calls running.
    """
    seconds = str(700 + secrets.randbelow(200))
    script = (
        f'. "{VERIFIED_DOWNLOAD_SH}"\n'
        f"for _ in $(seq 100); do run_with_deadline {seconds} true; done\n"
    )
    res = run_bash_cmd(["bash", "-c", script], tmp_path)
    assert res.returncode == 0, res.stderr
    listing = run_bash_cmd(["ps", "-eo", "pid=,args="], tmp_path)
    left = [
        int(fields[0])
        for fields in (line.split() for line in listing.stdout.splitlines())
        if fields[1:] == ["sleep", seconds]
    ]
    for pid in left:
        os.kill(pid, signal.SIGTERM)
    assert left == []


def test_install_gitleaks_outage_fallback(tmp_path: Path) -> None:
    """install-gitleaks.sh succeeds during curl outage via gh fallback."""
    bin_dir, logs_dir, _ = setup_fake_environment(tmp_path)
    archive = make_archive(tmp_path, "good", DEFAULT_VERSION, shape="regular")
    digest = sha256_of(archive)
    install_dir = tmp_path / "installed_bin"

    res = run_bash_cmd(
        ["bash", str(INSTALL_GITLEAKS_SH), DEFAULT_VERSION, digest, str(install_dir)],
        bin_dir,
        env_extra={
            "FIXTURE_ARCHIVE": str(archive),
            "CURL_OUTAGE": "1",
            "GH_TOKEN": STUB_GH_TOKEN,
        },
    )
    assert res.returncode == 0, res.stderr
    assert f"installed verified gitleaks {DEFAULT_VERSION}" in res.stdout
    installed_binary = install_dir / "gitleaks"
    assert installed_binary.is_file()
    assert (logs_dir / "gh.log").is_file()
    assert "::warning title=Release download fallback::" in res.stdout


def test_action_yml_declares_gh_token_env() -> None:
    """Both composite actions declare GH_TOKEN in step environment."""
    for action_path in [ACTION_VERIFIED_DOWNLOAD_YML, ACTION_INSTALL_GITLEAKS_YML]:
        data = yaml.safe_load(action_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        step_env = data["runs"]["steps"][0].get("env", {})
        assert step_env.get("GH_TOKEN") == "${{ github.token }}", (
            f"{action_path.name} missing GH_TOKEN in step env"
        )
