# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Licence image age: check that the pinned licence tool image is not behind an older release.

    image_age.py check --tags-url URL [--root DIR] [--today YYYY-MM-DD]

Reads the pinned release and digest from .github/workflows/licence.yml via the
regex declared in renovate.json. Compares with the release tags that URL lists
(Docker Hub's tag API). The workflow names the URL, so the host scan
(hosts.py) sees the host this tool asks.
Bounded to 14 days: a newer release older than 14 days fails (exit 1). A newer
release up to 14 days passes with a note that Renovate proposes it after 3 days.
If the pinned release digest moved on Docker Hub, fails (exit 1). If Docker Hub
cannot be read, fails with an error (exit 3).

Exit codes: 0 pass, 1 finding (outdated pin or moved digest), 3 could not check.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ciwf

BOUND_DAYS = 14
DEADLINE_SECONDS = 20
RENOVATE_FILE = "renovate.json"
LICENCE_FILE = ".github/workflows/licence.yml"
RELEASE_TAG = re.compile(r"^\d+\.\d+\.\d+$")


class ReadError(Exception):
    """An input file or remote registry response that cannot be read."""


def read_pin(root: Path) -> tuple[str, str, str]:
    """Read the pinned release, digest and dependency name via renovate.json regex."""
    renovate_path = root / RENOVATE_FILE
    licence_path = root / LICENCE_FILE
    try:
        config = json.loads(renovate_path.read_text(encoding="utf-8"))
        pattern_str = config["customManagers"][0]["matchStrings"][0]
        pattern = re.compile(pattern_str.replace("(?<", "(?P<"))
        licence_text = licence_path.read_text(encoding="utf-8")
    except (OSError, KeyError, IndexError, json.JSONDecodeError) as err:
        raise ReadError(f"could not load configuration: {err}") from err

    match = pattern.search(licence_text)
    if not match:
        raise ReadError(f"regex from {RENOVATE_FILE} did not match image line in {LICENCE_FILE}")
    return match.group("currentValue"), match.group("currentDigest"), match.group("depName")


def fetch_hub_tags(url: str, timeout: int = DEADLINE_SECONDS) -> list[dict]:
    """Fetch tags from Docker Hub.

    Docker Hub tags are requested with ordering=last_updated and page_size=100.
    Because release gaps for fsfe/reuse span from days to months, the first page
    of 100 entries covers all recent releases across multiple years of history.
    Thus, a single page suffices. The loop is bounded to 1 iteration.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "horistes-image-age"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raise ReadError(f"Docker Hub HTTP {err.code}: {err.reason}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ReadError(f"Docker Hub request failed: {err}") from err
    except json.JSONDecodeError as err:
        raise ReadError(f"Docker Hub returned malformed JSON: {err}") from err

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ReadError("Docker Hub response missing 'results' list")

    results = []
    # Explicitly bounded loop: at most 100 results from the single page
    for item in payload["results"][:100]:
        if isinstance(item, dict) and "name" in item:
            results.append(item)
    return results


def semver_key(name: str) -> tuple[int, ...] | None:
    """Parse a 3-part semantic release tag name into numeric components."""
    if not RELEASE_TAG.match(name):
        return None
    return tuple(int(part) for part in name.split("."))


def parse_pushed_date(ts: str) -> datetime.date:
    """Parse ISO timestamp string from tag_last_pushed into date."""
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except (ValueError, TypeError) as err:
        raise ReadError(f"invalid tag_last_pushed date {ts!r}: {err}") from err


def evaluate_age(
    pinned_version: str,
    pinned_digest: str,
    newest_tag: dict,
    today: datetime.date,
) -> tuple[int, str]:
    """Evaluate whether the newest release exceeds the BOUND_DAYS upper bound."""
    newest_version = str(newest_tag["name"])
    newest_date = parse_pushed_date(str(newest_tag.get("tag_last_pushed", "")))
    age_days = (today - newest_date).days

    ciwf.say(f"Pinned release: {pinned_version} ({pinned_digest})")
    ciwf.say(
        f"Newest release: {newest_version} (pushed {newest_date.isoformat()}, "
        f"age {age_days} day(s), today {today.isoformat()})"
    )

    if newest_version == pinned_version:
        return 0, f"PASS: pinned release {pinned_version} is up to date."

    # Boundary: older than 14 days means strictly > 14 days
    if age_days > BOUND_DAYS:
        return (
            1,
            f"FAIL: newest release {newest_version} is {age_days} days old "
            f"(> {BOUND_DAYS} days), but pinned release is {pinned_version}.",
        )
    return (
        0,
        f"PASS: newer release {newest_version} is {age_days} day(s) old "
        f"(Renovate proposes it after 3 days).",
    )


def check(
    root: Path = Path("."),
    today: datetime.date | None = None,
    fetch_fn: Callable[[], list[dict]] | None = None,
    tags_url: str = "",
) -> int:
    """Run the image age check against Docker Hub tags."""
    today = today or datetime.date.today()
    try:
        pinned_version, pinned_digest, _ = read_pin(root)
        tags = fetch_fn() if fetch_fn else fetch_hub_tags(tags_url)
    except ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3

    release_tags = [t for t in tags if semver_key(str(t.get("name", ""))) is not None]
    if not release_tags:
        ciwf.complain("ERROR: no release tags found on Docker Hub")
        return 3

    pinned_tags = [t for t in release_tags if t["name"] == pinned_version]
    if not pinned_tags:
        ciwf.complain(f"ERROR: pinned release {pinned_version} missing on Docker Hub")
        return 3

    hub_digest = pinned_tags[0].get("digest")
    if hub_digest and hub_digest != pinned_digest:
        ciwf.say(f"Pinned release: {pinned_version} ({pinned_digest})")
        ciwf.complain(
            f"FAIL: pinned release {pinned_version} tag digest on Docker Hub moved: "
            f"expected {pinned_digest}, found {hub_digest}"
        )
        return 1

    newest_tag = max(release_tags, key=lambda t: semver_key(str(t["name"])) or ())
    try:
        code, message = evaluate_age(pinned_version, pinned_digest, newest_tag, today)
    except ReadError as err:
        ciwf.complain(f"ERROR: {err}")
        return 3

    if code == 0:
        ciwf.say(message)
    else:
        ciwf.complain(message)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--tags-url", required=True)
    check_parser.add_argument("--root", type=Path, default=Path("."))
    check_parser.add_argument(
        "--today", type=datetime.date.fromisoformat, default=datetime.date.today()
    )
    args = parser.parse_args(argv)
    if args.command == "check":
        return check(root=args.root, today=args.today, tags_url=args.tags_url)
    return 3


if __name__ == "__main__":
    sys.exit(main())
