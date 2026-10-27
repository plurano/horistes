# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for shared installer actions (verified-download, install-gitleaks)."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

from conftest import ROOT

INSTALL_GITLEAKS_SH = ROOT / "actions/install-gitleaks/install-gitleaks.sh"
VERIFIED_DOWNLOAD_SH = ROOT / "actions/verified-download/verified-download.sh"
ACTION_INSTALL_GITLEAKS_YML = ROOT / "actions/install-gitleaks/action.yml"
ACTION_VERIFIED_DOWNLOAD_YML = ROOT / "actions/verified-download/action.yml"
DEFAULT_VERSION = "8.30.1"


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


def setup_fake_environment(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create fake curl and uname binaries on PATH and return paths."""
    bin_dir = tmp_path / "bin"
    logs_dir = tmp_path / "logs"
    bin_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    curl_sh = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "output=''\n"
        "url=''\n"
        "while (($# > 0)); do\n"
        '  case "$1" in\n'
        '    --output) output="$2"; shift 2 ;;\n'
        "    --retry | --connect-timeout | --max-time | --max-filesize | --proto | --proto-redir)\n"
        f'      printf \'%s %s\\n\' "$1" "$2" >>"{logs_dir}/curl_args.log"\n'
        "      shift 2 ;;\n"
        "    -*) shift ;;\n"
        '    *) url="$1"; shift ;;\n'
        "  esac\n"
        "done\n"
        f'printf \'%s\\n\' "$url" >>"{logs_dir}/curl.log"\n'
        'if [[ "$url" != https://* ]]; then\n'
        "  printf 'curl: (1) Protocol not supported: %s\\n' \"$url\" >&2\n"
        "  exit 1\n"
        "fi\n"
        'if [[ -n "${CURL_FAIL:-}" ]]; then\n'
        "  exit 1\n"
        "fi\n"
        'if [[ -n "${FIXTURE_ARCHIVE:-}" && -n "$output" ]]; then\n'
        '  cp "${FIXTURE_ARCHIVE}" "$output"\n'
        "fi\n"
    )
    curl_file = bin_dir / "curl"
    curl_file.write_text(curl_sh, encoding="utf-8")
    curl_file.chmod(0o755)

    uname_sh = (
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  -s) printf '%s\\n' \"${FAKE_UNAME_S:-Linux}\" ;;\n"
        "  -m) printf '%s\\n' \"${FAKE_UNAME_M:-x86_64}\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    uname_file = bin_dir / "uname"
    uname_file.write_text(uname_sh, encoding="utf-8")
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
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


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
