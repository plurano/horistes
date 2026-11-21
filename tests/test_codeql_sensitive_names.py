# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""CodeQL reads a lookup key or a function name that looks sensitive as a source of secret data.

Workflow secret names logged through such a key or function raised alerts 1 and 12 (false
positives). This test keeps both source kinds out of tools/*.py before CI.
"""

from __future__ import annotations

import ast
import re
import sys

import pytest
from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import ciwf

# Mirrors codeql/python-all 7.2.10 semmle/python/security/internal/SensitiveDataHeuristics.qll
# (maybeSecret, maybePassword, maybeCertificate, notSensitiveRegexp), with alternation
# look-behinds split into fixed-width look-behinds for Python's `re`.
MAYBE_SECRET = re.compile(
    r"(?is).*((?<!is)(?<!is_)secret"
    r"|(?<!un)(?<!un_)(?<!is)(?<!is_)trusted(?!_iter|_?schema)"
    r"|confidential).*"
)
MAYBE_PASSWORD = re.compile(
    r"(?is).*(pass(wd|word|code|.?phrase)(?!.*question)"
    r"|(auth(entication|ori[sz]ation)?).?key|oauth|api.?(key|tok)"
    r"|([_-]|\b)mfa([_-]|\b)).*"
)
MAYBE_CERTIFICATE = re.compile(r"(?is).*(cert)(?!.*(format|name|ification)).*")
NOT_SENSITIVE_REGEXP = re.compile(
    r"(?is).*([^\w$.-]|redact|censor|obfuscate|hash|md5|sha|random"
    r"|(?<!unen)crypt|(?<!un)encode|certain|concert|secretar|wildcard"
    r"|coauthor|account(ant|ab|ing|ed)|(?<!pro)file|path|([_-]|\b)url).*"
)
MAYBE_SENSITIVE = (MAYBE_SECRET, MAYBE_PASSWORD, MAYBE_CERTIFICATE)


def scan_sensitive_names(source: str):
    """Yield (line, kind, text) for sensitive subscript keys, .get() keys and function names."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            kind, text = "subscript", node.slice.value
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            kind, text = "get", node.args[0].value
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind, text = "function", node.name
        else:
            continue
        if any(p.fullmatch(text) for p in MAYBE_SENSITIVE) and not NOT_SENSITIVE_REGEXP.fullmatch(
            text
        ):
            yield (node.lineno, kind, text)


def test_tools_hold_no_sensitive_lookup_keys_or_function_names() -> None:
    """Every tools/*.py file scans to zero sensitive lookup keys or function names."""
    findings = [
        f"{path.relative_to(ROOT).as_posix()}:{line} {kind} {text}"
        for path in sorted((ROOT / "tools").glob("*.py"))
        for line, kind, text in scan_sensitive_names(path.read_text(encoding="utf-8"))
    ]
    assert not findings, "\n".join(findings)


@pytest.mark.parametrize(
    ("source", "kind", "text"),
    [
        ('d.get("secrets")', "get", "secrets"),
        ('d["password"]', "subscript", "password"),
        ("def _secret_problems(): pass", "function", "_secret_problems"),
        ('d.get("api_key")', "get", "api_key"),
        ('d["client_cert"]', "subscript", "client_cert"),
    ],
)
def test_scanner_reports_planted_sensitive_sources(source: str, kind: str, text: str) -> None:
    """Each planted sensitive lookup key or function name yields one finding of its kind."""
    assert list(scan_sensitive_names(source)) == [(1, kind, text)]


@pytest.mark.parametrize(
    "source",
    [
        'd["is_secret"]',
        'd["secretary"]',
        'd["secret_path"]',
        'd["untrusted"]',
        'd["cert_format"]',
        "x.secrets",
        '"secrets"',
    ],
)
def test_scanner_ignores_excluded_and_non_lookup_names(source: str) -> None:
    """Excluded patterns, attributes and plain string expressions yield zero findings."""
    assert list(scan_sensitive_names(source)) == []


def test_credential_names_key_matches_forge_workflow_key() -> None:
    """The workflow key name is fixed by the forge format."""
    assert ciwf.CREDENTIAL_NAMES_KEY == "secrets"
