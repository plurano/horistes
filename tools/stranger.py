# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""A stranger's reach: GET requests and commands that carry no credentials.

A request is made by this process with no authorisation header; it follows
redirects and has a deadline and a size limit. A command runs with an
environment that holds the search path and a home folder made for the run,
so it finds no token, no registry login and no configuration of the user.
On Windows the variables a program needs to start (PASSED) are kept as well.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

DEADLINE_SECONDS = 60
COMMAND_SECONDS = 240
CHUNK = 1 << 20
MAX_BYTES = 2 << 30
AGENT = "plurano-horistes-readback"
PASSED = ("PATH", "SYSTEMROOT", "TEMP", "TMP")
LIMIT_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset")


class FetchError(Exception):
    """A request that was not answered with a body; `status` is the HTTP status when one came."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _copy(answer, out, limit: int, url: str) -> str:
    digest, size = hashlib.sha256(), 0
    for _ in range(limit // 1024 + 2):
        block = answer.read(CHUNK)
        if not block:
            return digest.hexdigest()
        size += len(block)
        if size > limit:
            break
        digest.update(block)
        out.write(block)
    raise FetchError(f"GET {url}: more than {limit} bytes")


def _limit(err: urllib.error.HTTPError) -> str:
    seen = [f"{key} {err.headers.get(key)}" for key in LIMIT_HEADERS if err.headers.get(key)]
    return f" ({', '.join(seen)})" if seen and err.code in (403, 429) else ""


def digest(path: Path) -> str:
    """The SHA-256 of a file, as hexadecimal text."""
    found = hashlib.sha256()
    with path.open("rb") as handle:
        for _ in range(MAX_BYTES // CHUNK + 2):
            block = handle.read(CHUNK)
            if not block:
                return found.hexdigest()
            found.update(block)
    raise OSError(f"{path}: more than {MAX_BYTES} bytes")


class Stranger:
    """Requests and commands without credentials; `home` is the empty home of its commands."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def fetch(self, url: str, path: Path, accept: str = "*/*", limit: int = MAX_BYTES) -> str:
        """Save the body of one GET under `path` and return its SHA-256."""
        if not url.startswith("https://"):
            raise FetchError(f"GET {url}: not an https address")
        request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": accept})
        try:
            with (
                urllib.request.urlopen(request, timeout=DEADLINE_SECONDS) as answer,
                path.open("wb") as out,
            ):
                return _copy(answer, out, limit, url)
        except urllib.error.HTTPError as err:
            raise FetchError(f"GET {url}: HTTP {err.code}{_limit(err)}", err.code) from err
        except (OSError, ValueError) as err:
            raise FetchError(f"GET {url}: {err}") from err

    def run(self, command: list[str]) -> tuple[int | None, str, str]:
        """Exit code, output and complaint of a command; the code is None when it gave none."""
        env = {key: os.environ[key] for key in PASSED if key in os.environ}
        env |= {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "DOCKER_CONFIG": str(self.home / "docker"),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=COMMAND_SECONDS,
                env=env,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            return None, "", str(err)
        return (
            done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"),
        )
