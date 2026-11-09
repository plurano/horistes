# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for GitHub release download fallbacks in workflows."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ROOT, STUB_GH_TOKEN

VERIFIED_DOWNLOAD_SH = ROOT / "actions/verified-download/verified-download.sh"

PLANTED_ORIGIN_MAIN_STEP = """\
archive="$RUNNER_TEMP/actionlint.tar.gz"
curl --fail --silent --show-error --location --max-time 120 --retry 2 \\
  --output "$archive" \\
  "https://github.com/rhysd/actionlint/releases/download/${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION#v}_linux_amd64.tar.gz"
echo "${ACTIONLINT_SHA256}  ${archive}" | sha256sum --check --strict
tar -xzf "$archive" -C "$RUNNER_TEMP/bin" actionlint
"""

CURL_STUB_SCRIPT = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = os.environ.get("STUB_CURL_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")

url = next((a for a in args if a.startswith("http")), "")
out_path = None
for i in range(min(len(args), 50)):
    if args[i] == "--output" and i + 1 < len(args):
        out_path = args[i + 1]
        break

mode = os.environ.get("STUB_MODE", "healthy")
fix_path = os.environ.get("STUB_FIXTURE")

if "releases/download/" in url:
    if mode in ("outage", "wrong_bytes", "missing_asset"):
        sys.stderr.write("curl: (22) The requested URL returned error: 504\\n")
        sys.exit(22)
    if out_path and fix_path:
        Path(out_path).write_bytes(Path(fix_path).read_bytes())
    sys.exit(0)

sys.stderr.write(f"curl stub: unexpected url {url}\\n")
sys.exit(1)
"""

GH_STUB_SCRIPT = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = os.environ.get("STUB_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(" ".join(args) + "\\n")

token = os.environ.get("GH_TOKEN")
if not token:
    sys.stderr.write("gh stub: GH_TOKEN unset or empty\\n")
    sys.exit(1)

if (
    len(args) != 9
    or args[0] != "release"
    or args[1] != "download"
    or args[2].startswith("-")
    or args[3] != "--repo"
    or not args[4]
    or args[5] != "--pattern"
    or not args[6]
    or args[7] != "--output"
    or args[8] != "-"
):
    sys.stderr.write(f"gh stub: args differ from required shape: {args}\\n")
    sys.exit(1)

mode = os.environ.get("STUB_MODE", "healthy")
fix_path = os.environ.get("STUB_FIXTURE")

if mode in ("outage", "healthy"):
    if fix_path:
        sys.stdout.buffer.write(Path(fix_path).read_bytes())
    sys.exit(0)

if mode == "wrong_bytes":
    sys.stdout.buffer.write(b"corrupted bytes payload\\n")
    sys.exit(0)

if mode == "missing_asset":
    sys.stderr.write("no assets match the file pattern\\n")
    sys.exit(1)

sys.stderr.write(f"gh stub: unknown mode {mode}\\n")
sys.exit(1)
"""


def find_all_workflow_steps() -> list[tuple[Path, str, dict[str, Any], dict[str, str]]]:
    """Find all workflow steps across .github/workflows/*.yml."""
    results = []
    workflows_dir = ROOT / ".github/workflows"
    workflow_files = sorted(workflows_dir.glob("*.yml"))
    for file_idx in range(min(len(workflow_files), 100)):
        path = workflow_files[file_idx]
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        wf_env = data.get("env", {})
        jobs = data.get("jobs", {})
        for _, job in list(jobs.items())[:100]:
            if not isinstance(job, dict):
                continue
            for step in job.get("steps", [])[:100]:
                if isinstance(step, dict):
                    results.append((path, step.get("name", "step"), step, wf_env))
    return results


def find_release_download_steps() -> list[tuple[Path, str, dict[str, Any], dict[str, str]]]:
    """Find all workflow steps that contain releases/download/ in their run script."""
    all_steps = find_all_workflow_steps()
    return [s for s in all_steps if "releases/download/" in s[2].get("run", "")]


def make_fixture(tool_name: str, dest: Path) -> str:
    """Create a minimal tar.gz archive holding tool_name; return its sha256 hex digest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=tool_name)
        info.size = len(b"fixture binary content\n")
        tar.addfile(info, io.BytesIO(b"fixture binary content\n"))
    data = buf.getvalue()
    dest.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def step_tool_info(name: str, wf_env: dict[str, str]) -> tuple[str, str, str, str, str]:
    """Resolve (tool_name, asset_name, sha256_env_var, repo, tag) for a step."""
    if "actionlint" in name.lower():
        try:
            tag = str(wf_env["ACTIONLINT_VERSION"])
        except KeyError:
            pytest.fail("Workflow env missing ACTIONLINT_VERSION")
        ver = tag.lstrip("v")
        return (
            "actionlint",
            f"actionlint_{ver}_linux_amd64.tar.gz",
            "ACTIONLINT_SHA256",
            "rhysd/actionlint",
            tag,
        )
    raise ValueError(f"Unknown tool in step: {name}")


def setup_tmp_dirs(tmp_path: Path, custom_vd_sh: bytes | None = None) -> Path:
    """Create stub bin, bin dir and populate actions directories with verified-download.sh."""
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    (stub_bin / "curl").write_text(CURL_STUB_SCRIPT, encoding="utf-8")
    (stub_bin / "curl").chmod(0o755)
    (stub_bin / "gh").write_text(GH_STUB_SCRIPT, encoding="utf-8")
    (stub_bin / "gh").chmod(0o755)
    (tmp_path / "bin").mkdir(parents=True, exist_ok=True)

    vd_bytes = custom_vd_sh if custom_vd_sh is not None else VERIFIED_DOWNLOAD_SH.read_bytes()
    for sub in ("actions", "workflow-check-source/actions"):
        target_dir = tmp_path / sub / "verified-download"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "verified-download.sh").write_bytes(vd_bytes)
    return stub_bin


def run_step_script(
    run_script: str,
    tmp_path: Path,
    mode: str,
    tool_name: str,
    sha_var: str,
    wf_env: dict[str, str],
    custom_vd_sh: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Execute run_script using bash with stubs configured for mode in a laid-out tmp dir."""
    if shutil.which("bash") is None:
        pytest.skip("bash executable not found")

    stub_bin = setup_tmp_dirs(tmp_path, custom_vd_sh)
    fix_path = tmp_path / "fixture.tar.gz"
    fix_sha = make_fixture(tool_name, fix_path)
    curl_log = tmp_path / "curl.log"
    gh_log = tmp_path / "gh.log"

    env = dict(os.environ)
    for k, v in wf_env.items():
        env[k] = str(v)
    env.update(
        {
            "PATH": f"{stub_bin}:{env.get('PATH', '')}",
            "RUNNER_TEMP": str(tmp_path),
            "GH_TOKEN": STUB_GH_TOKEN,
            "STUB_MODE": mode,
            "STUB_FIXTURE": str(fix_path),
            "STUB_LOG": str(gh_log),
            "STUB_CURL_LOG": str(curl_log),
            "LC_ALL": "C",
            sha_var: fix_sha,
        }
    )

    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", run_script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc, curl_log, gh_log


def test_static_release_download_steps_source_and_call_fetch_verified() -> None:
    """Every step with releases/download/ sources verified-download.sh and calls fetch_verified."""
    steps = find_release_download_steps()
    assert len(steps) == 2, f"Expected exactly 2 release download run steps, found {len(steps)}"
    step_keys = {(s[0].name, s[1]) for s in steps}
    assert ("ci.yml", "Install the pinned actionlint") in step_keys
    assert ("workflow-check.yml", "Install the pinned actionlint") in step_keys

    for path, name, step, _ in steps:
        run = step.get("run", "")
        assert "verified-download.sh" in run, (
            f"{path.name}: {name} does not reference verified-download.sh"
        )
        assert ". " in run, f"{path.name}: {name} does not source script with '. '"
        assert "fetch_verified" in run, f"{path.name}: {name} does not call fetch_verified"


def test_static_no_run_holds_curl_and_releases_download() -> None:
    """No workflow step run script holds curl and releases/download/ together."""
    all_steps = find_all_workflow_steps()
    for path, name, step, _ in all_steps:
        run = step.get("run", "")
        assert not ("curl" in run and "releases/download/" in run), (
            f"{path.name}: {name} holds curl and releases/download/ together"
        )


def test_static_installer_action_counts() -> None:
    """Exact count of steps using install-gitleaks, verified-download, or download run script."""
    all_steps = find_all_workflow_steps()
    run_steps = [s for s in all_steps if "releases/download/" in s[2].get("run", "")]
    gitleaks_uses = [
        s
        for s in all_steps
        if s[2].get("uses", "").split("#", 1)[0].rstrip().endswith("actions/install-gitleaks")
    ]
    verified_download_uses = [
        s
        for s in all_steps
        if s[2].get("uses", "").split("#", 1)[0].rstrip().endswith("actions/verified-download")
    ]

    assert len(run_steps) == 2
    assert {s[0].name for s in run_steps} == {"ci.yml", "workflow-check.yml"}

    assert len(gitleaks_uses) == 2
    assert {s[0].name for s in gitleaks_uses} == {"ci.yml", "secret-scan.yml"}

    assert len(verified_download_uses) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="Bash steps run on Linux/macOS only")
@pytest.mark.parametrize(
    "step_entry", find_release_download_steps(), ids=lambda s: f"{s[0].name}:{s[1]}"
)
def test_behaviour_outage_fallback(tmp_path: Path, step_entry: tuple) -> None:
    """When browser download returns 504 outage, gh fallback succeeds and warning is emitted."""
    _, name, step, wf_env = step_entry
    tool, asset, sha_var, repo, tag = step_tool_info(name, wf_env)
    proc, _, gh_log = run_step_script(step["run"], tmp_path, "outage", tool, sha_var, wf_env)
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert (tmp_path / "bin" / tool).is_file(), f"{tool} binary was not extracted"
    assert "::warning title=Release download fallback::" in proc.stdout
    assert "fetching it with gh release download" in proc.stdout

    assert gh_log.is_file(), "gh log file was not created"
    gh_calls = gh_log.read_text(encoding="utf-8").splitlines()
    assert len(gh_calls) == 1, f"Expected 1 gh call, found {len(gh_calls)}"
    expected_args = [
        "release",
        "download",
        tag,
        "--repo",
        repo,
        "--pattern",
        asset,
        "--output",
        "-",
    ]
    assert gh_calls[0].split() == expected_args


@pytest.mark.skipif(sys.platform == "win32", reason="Bash steps run on Linux/macOS only")
@pytest.mark.parametrize(
    "step_entry", find_release_download_steps(), ids=lambda s: f"{s[0].name}:{s[1]}"
)
def test_behaviour_healthy_browser_download(tmp_path: Path, step_entry: tuple) -> None:
    """When browser download succeeds, tool installs without warning and without gh call."""
    _, name, step, wf_env = step_entry
    tool, _, sha_var, _, _ = step_tool_info(name, wf_env)
    proc, curl_log, gh_log = run_step_script(
        step["run"], tmp_path, "healthy", tool, sha_var, wf_env
    )
    assert proc.returncode == 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    assert (tmp_path / "bin" / tool).is_file(), f"{tool} binary was not extracted"
    assert "::warning title=Release download fallback::" not in proc.stdout
    assert not gh_log.exists() or len(gh_log.read_text(encoding="utf-8").splitlines()) == 0
    assert curl_log.is_file(), "curl log file was not created"
    curl_calls = curl_log.read_text(encoding="utf-8").splitlines()
    assert len(curl_calls) == 1
    assert "releases/download/" in curl_calls[0]


@pytest.mark.skipif(sys.platform == "win32", reason="Bash steps run on Linux/macOS only")
@pytest.mark.parametrize(
    "step_entry", find_release_download_steps(), ids=lambda s: f"{s[0].name}:{s[1]}"
)
def test_behaviour_wrong_bytes(tmp_path: Path, step_entry: tuple) -> None:
    """When fallback delivers corrupt bytes, digest mismatch fails the step."""
    _, name, step, wf_env = step_entry
    tool, _, sha_var, _, _ = step_tool_info(name, wf_env)
    proc, _, _ = run_step_script(step["run"], tmp_path, "wrong_bytes", tool, sha_var, wf_env)
    assert proc.returncode != 0
    combined = f"{proc.stdout}\n{proc.stderr}"
    assert "archive digest mismatch" in combined or "digest mismatch" in combined


@pytest.mark.skipif(sys.platform == "win32", reason="Bash steps run on Linux/macOS only")
def test_planted_origin_main_step_fails_on_outage(tmp_path: Path) -> None:
    """Origin/main step without fallback fails when the browser download suffers an outage."""
    wf_env = {"ACTIONLINT_VERSION": "v1.7.7"}
    tool, _, sha_var, _, _ = step_tool_info("Install the pinned actionlint", wf_env)
    proc, _, _ = run_step_script(
        PLANTED_ORIGIN_MAIN_STEP, tmp_path, "outage", tool, sha_var, wf_env
    )
    assert proc.returncode != 0
    assert "The requested URL returned error: 504" in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Bash steps run on Linux/macOS only")
def test_planted_verified_download_without_fallback_fails_on_outage(tmp_path: Path) -> None:
    """verified-download.sh with fetch_release_fallback call removed fails on outage."""
    vd_text = VERIFIED_DOWNLOAD_SH.read_text(encoding="utf-8")
    fallback_call = 'fetch_release_fallback "$url" "$max_bytes" "$timeout_seconds" "$output" || '
    assert fallback_call in vd_text, "fetch_release_fallback call pattern not found in script"
    planted_vd = vd_text.replace(fallback_call, "")

    steps = find_release_download_steps()
    assert steps, "No release download steps found"
    _, name, step, wf_env = steps[0]
    tool, _, sha_var, _, _ = step_tool_info(name, wf_env)

    proc, _, _ = run_step_script(
        step["run"],
        tmp_path,
        "outage",
        tool,
        sha_var,
        wf_env,
        custom_vd_sh=planted_vd.encode("utf-8"),
    )
    assert proc.returncode != 0
    assert "The requested URL returned error: 504" in proc.stderr or "504" in proc.stderr
