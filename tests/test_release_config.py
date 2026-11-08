# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Tests for release-please configuration and manifest invariants (ADR-0069).

Validates release-please-config.json, .release-please-manifest.json, and REUSE.toml
against the release train specification defined in ADR-0069.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from conftest import ROOT

CONFIG_FILE = ROOT / "release-please-config.json"
MANIFEST_FILE = ROOT / ".release-please-manifest.json"
REUSE_FILE = ROOT / "REUSE.toml"

EXPECTED_SCHEMA = (
    "https://raw.githubusercontent.com/googleapis/release-please/v17.6.0/schemas/config.json"
)
EXPECTED_PACKAGE: dict[str, Any] = {
    "release-type": "simple",
    "package-name": "horistes",
    "changelog-path": "CHANGELOG.md",
    "initial-version": "0.1.0",
    "bump-minor-pre-major": True,
    "include-component-in-tag": False,
}

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")

REQUIRED_REUSE_PATHS = frozenset(
    {
        "release-please-config.json",
        ".release-please-manifest.json",
        "CHANGELOG.md",
    }
)


def _pairs_refusing_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dictionary from key-value pairs, refusing duplicate keys."""
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in JSON object: {key!r}")
        seen.add(key)
        result[key] = value
    return result


def load_unique_json(source: Path | str) -> Any:
    """Read JSON from a Path or string, refusing duplicate keys via object_pairs_hook."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    return json.loads(text, object_pairs_hook=_pairs_refusing_duplicates)


def validate_release_config(config: Any) -> None:
    """Verify release-please configuration structure and strict equality of the root package."""
    assert isinstance(config, dict), f"config must be a dict, got {type(config).__name__}"
    assert set(config.keys()) == {"$schema", "packages"}, (
        f"top-level keys must be exactly {{'$schema', 'packages'}}, got {set(config.keys())}"
    )
    assert config["$schema"] == EXPECTED_SCHEMA, (
        f"schema must be {EXPECTED_SCHEMA}, got {config['$schema']}"
    )
    packages = config["packages"]
    assert isinstance(packages, dict), f"packages must be a dict, got {type(packages).__name__}"
    assert set(packages.keys()) == {"."}, (
        f"packages keys must be exactly {{'.'}}, got {set(packages.keys())}"
    )
    assert packages["."] == EXPECTED_PACKAGE, (
        f"package '.' must equal {EXPECTED_PACKAGE}, got {packages['.']}"
    )


def validate_release_manifest(manifest: Any) -> None:
    """Verify release-please manifest keys are a subset of {'.'} and versions match semver."""
    assert isinstance(manifest, dict), f"manifest must be a dict, got {type(manifest).__name__}"
    assert set(manifest.keys()).issubset({"."}), (
        f"manifest keys must be a subset of {{'.'}}, got {set(manifest.keys())}"
    )
    for key, value in manifest.items():
        assert isinstance(value, str), (
            f"version for {key!r} must be str, got {type(value).__name__}"
        )
        assert VERSION_RE.fullmatch(value), (
            f"version {value!r} for {key!r} must be three ASCII digit groups, as 0.1.0"
        )


def validate_reuse_annotations(reuse_file: Path) -> None:
    """Verify the first annotation in REUSE.toml contains the release files and changelog."""
    data = tomllib.loads(reuse_file.read_text(encoding="utf-8"))
    annotations = data.get("annotations", [])
    assert annotations, "REUSE.toml has no annotations"
    first_paths = set(annotations[0].get("path", []))
    missing = REQUIRED_REUSE_PATHS - first_paths
    assert not missing, f"REUSE.toml first annotation missing required paths: {sorted(missing)}"


def test_real_release_config() -> None:
    """The real release-please-config.json meets all required schema and package invariants."""
    config = load_unique_json(CONFIG_FILE)
    validate_release_config(config)


def test_real_release_manifest() -> None:
    """The real .release-please-manifest.json is valid (empty or proposed semver for '.')."""
    manifest = load_unique_json(MANIFEST_FILE)
    validate_release_manifest(manifest)


def test_real_reuse_annotations() -> None:
    """The real REUSE.toml first annotation list names the three release-related files."""
    validate_reuse_annotations(REUSE_FILE)


def test_planted_duplicate_key_fails(tmp_path: Path) -> None:
    """Duplicate key in JSON input is refused by load_unique_json."""
    p = tmp_path / "duplicate_key.json"
    p.write_text('{"packages": {}, "packages": {}}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate key"):
        load_unique_json(p)


def test_planted_extra_package_key_fails(tmp_path: Path) -> None:
    """An extra key in the package configuration fails validation."""
    data = {
        "$schema": EXPECTED_SCHEMA,
        "packages": {
            ".": {**EXPECTED_PACKAGE, "release-as": "1.0.0"},
        },
    }
    p = tmp_path / "extra_key.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    config = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_config(config)


def test_planted_missing_include_component_in_tag_fails(tmp_path: Path) -> None:
    """Missing include-component-in-tag key in package configuration fails validation."""
    pkg = dict(EXPECTED_PACKAGE)
    del pkg["include-component-in-tag"]
    data = {"$schema": EXPECTED_SCHEMA, "packages": {".": pkg}}
    p = tmp_path / "missing_key.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    config = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_config(config)


def test_planted_initial_version_mismatch_fails(tmp_path: Path) -> None:
    """An initial-version other than 0.1.0 fails validation."""
    pkg = {**EXPECTED_PACKAGE, "initial-version": "1.0.0"}
    data = {"$schema": EXPECTED_SCHEMA, "packages": {".": pkg}}
    p = tmp_path / "bad_initial.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    config = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_config(config)


def test_planted_second_package_fails(tmp_path: Path) -> None:
    """Defining more than the single '.' package fails validation."""
    data = {
        "$schema": EXPECTED_SCHEMA,
        "packages": {
            ".": EXPECTED_PACKAGE,
            "sub": EXPECTED_PACKAGE,
        },
    }
    p = tmp_path / "second_pkg.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    config = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_config(config)


def test_planted_manifest_disallowed_package_fails(tmp_path: Path) -> None:
    """Manifest referencing a package other than '.' fails validation."""
    p = tmp_path / "manifest_disallowed.json"
    p.write_text(json.dumps({"lib": "0.1.0"}), encoding="utf-8")
    manifest = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_manifest(manifest)


def test_planted_manifest_v_prefix_fails(tmp_path: Path) -> None:
    """Manifest version containing a 'v' prefix fails regex validation."""
    p = tmp_path / "manifest_v_prefix.json"
    p.write_text(json.dumps({".": "v0.1.0"}), encoding="utf-8")
    manifest = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_manifest(manifest)


def test_planted_manifest_trailing_newline_fails(tmp_path: Path) -> None:
    """Manifest version with a trailing newline fails: the whole string must match."""
    p = tmp_path / "manifest_newline.json"
    p.write_text(json.dumps({".": "0.1.0\n"}), encoding="utf-8")
    manifest = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_manifest(manifest)


def test_planted_manifest_non_ascii_digits_fails(tmp_path: Path) -> None:
    """Manifest version in Arabic-Indic digits fails: only ASCII digits form a version."""
    p = tmp_path / "manifest_digits.json"
    p.write_text(json.dumps({".": "\u0660.\u0661.\u0660"}), encoding="utf-8")
    manifest = load_unique_json(p)
    with pytest.raises(AssertionError):
        validate_release_manifest(manifest)


def test_planted_manifest_released_version_passes(tmp_path: Path) -> None:
    """Manifest proposing 0.1.0 for '.' passes validation (positive boundary test)."""
    p = tmp_path / "manifest_released.json"
    p.write_text(json.dumps({".": "0.1.0"}), encoding="utf-8")
    manifest = load_unique_json(p)
    validate_release_manifest(manifest)


def test_planted_manifest_empty_boundary_passes(tmp_path: Path) -> None:
    """Empty manifest '{}' passes validation (current initial boundary state)."""
    p = tmp_path / "manifest_empty.json"
    p.write_text("{}\n", encoding="utf-8")
    manifest = load_unique_json(p)
    validate_release_manifest(manifest)


def test_planted_valid_config_passes(tmp_path: Path) -> None:
    """Valid config written to tmp_path passes validation (positive test)."""
    p = tmp_path / "valid_config.json"
    content = json.dumps({"$schema": EXPECTED_SCHEMA, "packages": {".": EXPECTED_PACKAGE}})
    p.write_text(content, encoding="utf-8")
    config = load_unique_json(p)
    validate_release_config(config)
