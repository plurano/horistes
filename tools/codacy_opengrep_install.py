# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Install support for the offline Codacy Opengrep gate.

adapted from lusoris/SnatchArr tools/ci/codacy_opengrep_scan.py at 21f590a3535f (EUPL-1.2)
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from codacy_opengrep_common import (
    ADAPTER,
    CHUNK_BYTES,
    CLI_PACKAGE,
    DOWNLOAD_DEADLINE_SECONDS,
    DOWNLOAD_PREFIX,
    DOWNLOAD_TIMEOUT_SECONDS,
    INSTALL_LOCK,
    LOCK_SLEEP_SECONDS,
    LOCK_TRIES,
    MAX_DOWNLOAD_BYTES,
    MAX_PACKAGES,
    MAX_READS,
    PACKAGE,
    PREFERRED_RE,
    SHA_RE,
    TOOL_VERSIONS,
    VERSION_RE,
    WRAPPER,
    ScanError,
    emit,
    engine_run,
    load_json,
    npm_run,
)


def tool_versions(root: Path) -> dict[str, str]:
    """Read the exact Opengrep release pin without sourcing shell text."""
    values = {}
    for line in (root / TOOL_VERSIONS).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    required = ("OPENGREP_VERSION", "OPENGREP_SHA256", "OPENGREP_URL")
    if any(not values.get(key) for key in required):
        raise ScanError(f"{TOOL_VERSIONS}: missing Opengrep pin")
    if not VERSION_RE.fullmatch(values["OPENGREP_VERSION"]) or not SHA_RE.fullmatch(
        values["OPENGREP_SHA256"]
    ):
        raise ScanError(f"{TOOL_VERSIONS}: OPENGREP_VERSION or OPENGREP_SHA256 malformed")
    if not values["OPENGREP_URL"].startswith(f"{DOWNLOAD_PREFIX}{values['OPENGREP_VERSION']}/"):
        raise ScanError(
            f"{TOOL_VERSIONS}: OPENGREP_URL must start with "
            f"{DOWNLOAD_PREFIX}{values['OPENGREP_VERSION']}/"
        )
    return values


def copy_capped(response: object, output: object, digest: object) -> int:
    """Copy RESPONSE into OUTPUT and DIGEST in bounded reads under MAX_DOWNLOAD_BYTES; return
    the size.
    """
    size = 0
    for _ in range(MAX_READS):
        chunk = response.read(CHUNK_BYTES)
        if not chunk:
            return size
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            raise ScanError(f"download passed {MAX_DOWNLOAD_BYTES} bytes")
        digest.update(chunk)
        output.write(chunk)
    raise ScanError(f"download did not end within {MAX_READS} reads")


def fetch_into(
    url: str, destination: Path, opener: Callable[..., object], outcome: dict[str, object]
) -> None:
    """Download URL to DESTINATION; record the sha256 and size, or the error, in OUTCOME."""
    digest = hashlib.sha256()
    try:
        with (
            opener(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as output,
        ):
            outcome["size"] = copy_capped(response, output, digest)
        outcome["sha256"] = digest.hexdigest()
    except (OSError, ScanError) as err:
        outcome["error"] = f"download failed: {err}"


def fetch_within(
    url: str, destination: Path, deadline: float, opener: Callable[..., object]
) -> tuple[str, int]:
    """Download URL in a worker thread and give up once DEADLINE seconds pass, however the
    socket behaves.

    Serves the installation of the pinned Opengrep binary.

    A server that trickles bytes just inside the socket timeout cannot hold the scan past
    DEADLINE; the abandoned daemon thread ends with the process.
    """
    outcome: dict[str, object] = {}
    worker = threading.Thread(
        target=fetch_into,
        args=(url, destination, opener, outcome),
        name="opengrep-download",
        daemon=True,
    )
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise ScanError(f"download did not finish within {deadline} s")
    if "error" in outcome:
        raise ScanError(str(outcome["error"]))
    return str(outcome["sha256"]), int(outcome["size"])


def write_wrapper(directory: Path, binary: Path) -> Path:
    """Write DIRECTORY/opengrep, the WRAPPER the CLI runs in place of BINARY, and return its
    path.
    """
    wrapper = directory / "opengrep"
    wrapper.write_text(WRAPPER.format(binary=shlex.quote(str(binary))), encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def cli_path(root: Path) -> Path:
    """Return the installed CLI after checking it is the exact version package.json pins."""
    package = root / PACKAGE
    wanted = load_json(package / "package.json")
    dependencies = wanted.get("dependencies") if isinstance(wanted, dict) else None
    pin = dependencies.get(CLI_PACKAGE) if isinstance(dependencies, dict) else None
    if not isinstance(pin, str) or not VERSION_RE.fullmatch(pin):
        raise ScanError(f"{PACKAGE}/package.json: {CLI_PACKAGE} needs an exact x.y.z pin")
    installed_file = package / "node_modules" / CLI_PACKAGE / "package.json"
    cli = package / "node_modules/.bin/codacy-analysis"
    if not installed_file.is_file() or not cli.is_file():
        raise ScanError(f"{CLI_PACKAGE} not installed: run make opengrep-tools")
    installed = load_json(installed_file)
    if not isinstance(installed, dict) or installed.get("version") != pin:
        raise ScanError(
            f"{CLI_PACKAGE} {installed.get('version') if isinstance(installed, dict) else '?'} "
            f"installed, the pin is {pin}"
        )
    return cli


def cache_path(versions: dict[str, str]) -> Path:
    """Locate the content-addressed binary without contacting a host."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "horistes/opengrep" / versions["OPENGREP_SHA256"] / "opengrep"


def file_sha256(path: Path) -> str:
    """Hash a local file using the stdlib streaming digest."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def npm_stamp(root: Path) -> tuple[Path, str]:
    """Return the npm installation stamp and the current lockfile digest."""
    package = root / PACKAGE
    return package / "node_modules/.horistes-stamp", file_sha256(package / "package-lock.json")


def stamp_current(root: Path) -> bool:
    """Refuse missing, stale or unreadable npm stamps."""
    stamp, expected = npm_stamp(root)
    return stamp.is_file() and stamp.read_bytes().strip() == expected.encode("ascii")


def preferred_version(root: Path, versions: dict[str, str]) -> None:
    """Read the adapter's one literal preferredVersion without importing code."""
    text = (root / PACKAGE / ADAPTER).read_text(encoding="utf-8")
    preferred = PREFERRED_RE.findall(text)
    if preferred != [versions["OPENGREP_VERSION"]]:
        raise ScanError(
            "Opengrep pin differs from adapter preferredVersion; run make opengrep-tools"
        )


def cached_binary(versions: dict[str, str]) -> Path:
    """Require the pinned binary's bytes in the cache, including on every scan."""
    binary = cache_path(versions)
    if not binary.is_file() or file_sha256(binary) != versions["OPENGREP_SHA256"]:
        raise ScanError("Opengrep cache missing or sha256 differs; run make opengrep-tools")
    return binary


def check_install(root: Path) -> None:
    """Verify the stamp, full locked tree, adapter and engine offline."""
    try:
        if not stamp_current(root):
            raise ScanError("npm stamp missing or stale")
        installed_tree(root)
        cached_binary(tool_versions(root))
    except (OSError, UnicodeDecodeError, ScanError) as err:
        raise ScanError(f"installation check failed: {err}; run make opengrep-tools") from err


def install_npm(root: Path) -> bool:
    """Reuse only a stamped, intact tree; otherwise reinstall and verify before stamping."""
    try:
        if stamp_current(root):
            installed_tree(root)
            return False
    except (OSError, UnicodeDecodeError, ScanError):
        pass  # A damaged install is repaired below, then verified before stamping.
    if shutil.which("npm") is None:
        raise ScanError("npm not found on PATH; run make opengrep-tools")
    result = npm_run(root)
    if result.returncode != 0:
        raise ScanError(f"npm ci failed (exit {result.returncode})")
    installed_tree(root)
    stamp, expected = npm_stamp(root)
    stamp.write_text(expected + "\n", encoding="utf-8")
    return True


def install_binary(versions: dict[str, str]) -> bool:
    """Atomically install verified bytes; leave an existing cache intact on failure."""
    destination = cache_path(versions)
    if destination.is_file() and file_sha256(destination) == versions["OPENGREP_SHA256"]:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="horistes-opengrep-install-", dir=destination.parent
    ) as temp:
        pending = Path(temp) / "opengrep"
        digest, size = fetch_within(
            versions["OPENGREP_URL"],
            pending,
            DOWNLOAD_DEADLINE_SECONDS,
            urllib.request.urlopen,
        )
        if digest != versions["OPENGREP_SHA256"] or file_sha256(pending) != digest:
            raise ScanError(f"Opengrep sha256 mismatch ({size} bytes)")
        pending.chmod(0o755)
        pending.replace(destination)
    return True


def install(root: Path) -> None:
    """Provision the locked tree and binary under one exclusive installation lease."""
    with install_lock(root, exclusive=True):
        npm_changed = install_npm(root)
        versions = tool_versions(root)
        binary_changed = install_binary(versions)
        check_install(root)
    state = (
        "installed pinned tools" if npm_changed or binary_changed else "installation current; no-op"
    )
    emit(f"codacy-opengrep: {state}")


@contextmanager
def codacy_home(versions: dict[str, str]) -> Iterator[Path]:
    """Copy and reverify the cached engine before wrapping it in the isolated home."""
    binary = cached_binary(versions)
    with tempfile.TemporaryDirectory(prefix="horistes-opengrep-") as temporary:
        home = Path(temporary) / "global"
        tools = home / "tools/Semgrep"
        tools.mkdir(parents=True)
        copied = tools / "opengrep-release"
        shutil.copyfile(binary, copied)
        if file_sha256(copied) != versions["OPENGREP_SHA256"]:
            raise ScanError("copied Opengrep sha256 mismatch")
        copied.chmod(0o755)
        wrapper = write_wrapper(tools, copied)
        result = engine_run(wrapper, home)
        if result.returncode != 0 or result.stdout.strip() != versions["OPENGREP_VERSION"]:
            raise ScanError(
                "cached Opengrep cannot run at its pinned version; refusing CLI auto-repair"
            )
        yield home


def installed_tree(root: Path) -> None:
    """Verify every versioned package in the bounded lockfile against its installed manifest."""
    lock = load_json(root / PACKAGE / "package-lock.json")
    packages = lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(packages, dict) or not 1 <= len(packages) <= MAX_PACKAGES:
        raise ScanError(
            f"lock packages must hold 1 to {MAX_PACKAGES} entries; run make opengrep-tools"
        )
    for path, package in packages.items():
        check_package(root, path, package)
    cli_path(root)
    preferred_version(root, tool_versions(root))


@contextmanager
def install_lock(root: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Hold a deadline-bounded flock; import Linux support only on the supported path."""
    import fcntl

    path = root / INSTALL_LOCK
    with path.open("a+b") as lock:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        for _ in range(LOCK_TRIES):
            try:
                fcntl.flock(lock, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(LOCK_SLEEP_SECONDS)
        else:
            raise ScanError(f"installation lock busy: {path}")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def check_package(root: Path, path: str, package: object) -> None:
    """Check one lock entry and its corresponding on-disk package manifest."""
    if not isinstance(package, dict):
        raise ScanError(f"invalid lock package {path}; run make opengrep-tools")
    if "version" in package:
        if path and (not path.startswith("node_modules/") or ".." in path.split("/")):
            raise ScanError(f"invalid lock path {path}; run make opengrep-tools")
        installed = load_json(root / PACKAGE / path / "package.json")
        if not isinstance(installed, dict) or installed.get("version") != package["version"]:
            raise ScanError(f"installed version differs: {path}; run make opengrep-tools")
