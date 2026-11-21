# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""CodeQL treats expressions whose names look sensitive as sources of secret data.

Workflow secret names logged through a dict lookup or helper function raised alerts 1 and 12
(false positives). This test mirrors the name-based source classes in codeql/python-all 7.2.10
`semmle/python/dataflow/new/SensitiveDataSources.qll` (`SensitiveSubscript`, `SensitiveGetCall`,
`SensitiveFunctionCall`, `SensitiveAttributeAccess`, `SensitiveVariableAssignment` and
`SensitiveParameter`) to keep all seven scanned source kinds (`subscript`, `get`, `function`,
`call`, `attribute`, `assign` and `parameter`) out of `tools/*.py` before CI.
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


def _is_sensitive(text: str) -> bool:
    """Whether `text` matches `MAYBE_SENSITIVE` and not `NOT_SENSITIVE_REGEXP`."""
    return any(p.fullmatch(text) for p in MAYBE_SENSITIVE) and not NOT_SENSITIVE_REGEXP.fullmatch(
        text
    )


def _str_arg(node: ast.Call, index: int) -> str | None:
    """Positional string literal argument at `index`, or None."""
    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
        value = node.args[index].value
        return value if isinstance(value, str) else None
    return None


def _subscript_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveSubscript: string literal index in a subscript."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return [(node.lineno, "subscript", node.slice.value)]
    return []


def _get_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveGetCall: first positional string literal in `.get(...)`."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and (key := _str_arg(node, 0)) is not None
    ):
        return [(node.lineno, "get", key)]
    return []


def _function_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveFunctionCall: sync or async function definition name."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [(node.lineno, "function", node.name)]
    return []


def _call_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveFunctionCall: bare-name call callee (`NameNode` branch)."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return [(node.lineno, "call", node.func.id)]
    return []


def _attribute_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveAttributeAccess: attribute load, from-import member or literal `getattr`."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        return [(node.lineno, "attribute", node.attr)]
    if isinstance(node, ast.ImportFrom):
        return [(alias.lineno, "attribute", alias.name) for alias in node.names]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and (attr := _str_arg(node, 1)) is not None
    ):
        return [(node.lineno, "attribute", attr)]
    return []


def _assign_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveVariableAssignment: store-context variable name."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return [(node.lineno, "assign", node.id)]
    return []


def _parameter_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveParameter: function or lambda parameter name."""
    if isinstance(node, ast.arg):
        return [(node.lineno, "parameter", node.arg)]
    return []


_KIND_SCANNERS = (
    _subscript_names,
    _get_names,
    _function_names,
    _call_names,
    _attribute_names,
    _assign_names,
    _parameter_names,
)


def _node_candidates(node: ast.AST) -> list[tuple[int, str, str]]:
    """Candidate (line, kind, text) entries extracted from one AST node."""
    return [entry for scanner in _KIND_SCANNERS for entry in scanner(node)]


def scan_sensitive_names(source: str):
    """Yield (line, kind, text) for CodeQL name-based sensitive sources in `source`.

    Covers `subscript` (`SensitiveSubscript`), `get` (`SensitiveGetCall`), `function` and `call`
    (`SensitiveFunctionCall`), `attribute` (`SensitiveAttributeAccess`: attribute load,
    `from ... import` member and literal `getattr`), `assign` (`SensitiveVariableAssignment`)
    and `parameter` (`SensitiveParameter`).
    """
    for node in ast.walk(ast.parse(source)):
        for line, kind, text in _node_candidates(node):
            if _is_sensitive(text):
                yield (line, kind, text)


def test_tools_hold_no_sensitive_lookup_keys_or_function_names() -> None:
    """Every tools/*.py file scans to zero findings across all covered CodeQL source kinds.

    Covers SensitiveSubscript, SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess,
    SensitiveVariableAssignment and SensitiveParameter (`subscript`, `get`, `function`, `call`,
    `attribute`, `assign` and `parameter`).
    """
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
        ("secret_value()", "call", "secret_value"),
        ("x.secrets", "attribute", "secrets"),
        ("from m import api_key", "attribute", "api_key"),
        ('getattr(x, "password")', "attribute", "password"),
        ('getattr(x, "password", None)', "attribute", "password"),
        ("secret = 1", "assign", "secret"),
        ("secret += 1", "assign", "secret"),
        ("secret: int = 1", "assign", "secret"),
        ("(secret := 1)", "assign", "secret"),
        ("for secret in s: pass", "assign", "secret"),
        ("with f() as secret: pass", "assign", "secret"),
        ("def f(password): pass", "parameter", "password"),
        ("def f(password, /): pass", "parameter", "password"),
        ("def f(*, password): pass", "parameter", "password"),
        ("def f(*passwords): pass", "parameter", "passwords"),
        ("def f(**passwords): pass", "parameter", "passwords"),
        ("lambda api_key: 0", "parameter", "api_key"),
    ],
)
def test_scanner_reports_planted_sensitive_sources(source: str, kind: str, text: str) -> None:
    """Each planted sensitive source yields one finding of its kind.

    Covers `subscript` (SensitiveSubscript), `get` (SensitiveGetCall), `function` and `call`
    (SensitiveFunctionCall), `attribute` (SensitiveAttributeAccess), `assign`
    (SensitiveVariableAssignment) and `parameter` (SensitiveParameter).
    """
    assert list(scan_sensitive_names(source)) == [(1, kind, text)]


@pytest.mark.parametrize(
    "source",
    [
        'd["is_secret"]',
        'd["secretary"]',
        'd["secret_path"]',
        'd["untrusted"]',
        'd["cert_format"]',
        '"secrets"',
        # SensitiveGetCall reads the first positional argument only.
        'd.get(key="secrets")',
        "getattr(x, name)",
        "secretary = 1",
        "def f(secret_path): pass",
        "x.is_secret",
        "x.secrets = 1",
    ],
)
def test_scanner_ignores_excluded_and_non_lookup_names(source: str) -> None:
    """Excluded patterns and non-source expressions yield zero findings across all source kinds.

    Covers `NOT_SENSITIVE_REGEXP` exclusions plus non-sources of SensitiveSubscript,
    SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess, SensitiveVariableAssignment
    and SensitiveParameter.
    """
    assert list(scan_sensitive_names(source)) == []


def test_credential_names_key_matches_forge_workflow_key() -> None:
    """The workflow key name is fixed by the forge format."""
    assert ciwf.CREDENTIAL_NAMES_KEY == "secrets"
