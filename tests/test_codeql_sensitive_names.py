# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""CodeQL treats expressions whose names look sensitive as sources of secret data.

Workflow secret names logged through a dict lookup or helper function raised alerts 1 and 12
(false positives). This test mirrors the name-based source classes in codeql/python-all 7.2.10
`semmle/python/dataflow/new/SensitiveDataSources.qll` (`SensitiveSubscript`, `SensitiveGetCall`,
`SensitiveFunctionCall`, `SensitiveAttributeAccess`, `SensitiveVariableAssignment` and
`SensitiveParameter`) to keep all seven scanned source kinds (`subscript`, `get`, `function`,
`call`, `attribute`, `assign` and `parameter`) out of `tools/*.py` before CI; `assign` mirrors
`Flow.qll` `assigned_value` plus `with ... as <Name>`, measured against CodeQL.
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


_NAME_TARGET_NODES = (
    ast.AugAssign,
    ast.NamedExpr,
    ast.For,
    ast.AsyncFor,
    ast.comprehension,
)


def _paired_assign_names(node: ast.Assign) -> list[tuple[int, str, str]]:
    """Index-paired Name targets from an `ast.Assign` statement, without recursion."""
    found: list[tuple[int, str, str]] = []
    stack: list[tuple[ast.expr, ast.expr]] = [
        (target, node.value) for target in reversed(node.targets)
    ]
    for _ in range(len(list(ast.walk(node)))):
        if not stack:
            break
        target, value = stack.pop()
        if isinstance(target, ast.Name):
            found.append((target.lineno, "assign", target.id))
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            stack.extend(reversed(list(zip(target.elts, value.elts, strict=False))))
    return found


def _with_assign_names(node: ast.With | ast.AsyncWith) -> list[tuple[int, str, str]]:
    """Bare-Name `as` bindings in a `with` or `async with` statement."""
    return [
        (item.optional_vars.lineno, "assign", item.optional_vars.id)
        for item in node.items
        if isinstance(item.optional_vars, ast.Name)
    ]


def _import_assign_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """Local bindings introduced by `import` and `from ... import ... as ...`."""
    if isinstance(node, ast.Import):
        return [
            (alias.lineno, "assign", alias.asname or alias.name.partition(".")[0])
            for alias in node.names
        ]
    if isinstance(node, ast.ImportFrom):
        return [
            (alias.lineno, "assign", alias.asname)
            for alias in node.names
            if alias.asname is not None
        ]
    return []


def _single_assign_target(node: ast.AST) -> ast.Name | None:
    """Bound `ast.Name` for single-target assignment, walrus or loop constructs."""
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return node.target if isinstance(node.target, ast.Name) else None
    if isinstance(node, _NAME_TARGET_NODES) and isinstance(node.target, ast.Name):
        return node.target
    return None


def _assign_names(node: ast.AST) -> list[tuple[int, str, str]]:
    """SensitiveVariableAssignment: `assigned_value` bindings plus `with ... as <Name>`."""
    if isinstance(node, ast.Assign):
        return _paired_assign_names(node)
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return _with_assign_names(node)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return _import_assign_names(node)
    if (target := _single_assign_target(node)) is not None:
        return [(target.lineno, "assign", target.id)]
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
    `from ... import` member and literal `getattr`), `assign` (`SensitiveVariableAssignment`:
    `assigned_value` plus `with ... as <Name>`, measured against CodeQL) and `parameter`
    (`SensitiveParameter`).
    """
    for node in ast.walk(ast.parse(source)):
        for line, kind, text in _node_candidates(node):
            if _is_sensitive(text):
                yield (line, kind, text)


def test_tools_hold_no_sensitive_lookup_keys_or_function_names() -> None:
    """Every tools/*.py file scans to zero findings across all covered CodeQL source kinds.

    Covers SensitiveSubscript, SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess,
    SensitiveVariableAssignment (`assigned_value` plus `with ... as <Name>`, measured against
    CodeQL) and SensitiveParameter (`subscript`, `get`, `function`, `call`, `attribute`, `assign`
    and `parameter`).
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
        ('os.environ["SECRET"]', "subscript", "SECRET"),
        ("secret_value()", "call", "secret_value"),
        ("x.secrets", "attribute", "secrets"),
        ("x.get_password()", "attribute", "get_password"),
        ("from m import api_key", "attribute", "api_key"),
        ("from m import password as pw", "attribute", "password"),
        ('getattr(x, "password")', "attribute", "password"),
        ('getattr(x, "password", None)', "attribute", "password"),
        ("import secrets", "assign", "secrets"),
        ("import x as secret", "assign", "secret"),
        ("from m import x as secret", "assign", "secret"),
        ("import secret_box.a", "assign", "secret_box"),
        ("import a as b, secret_box", "assign", "secret_box"),
        ("secret = 1", "assign", "secret"),
        ("secret += 1", "assign", "secret"),
        ("secret: int = 1", "assign", "secret"),
        ("(secret := 1)", "assign", "secret"),
        ("a = secret = 1", "assign", "secret"),
        ("[a, secret] = [1, 2]", "assign", "secret"),
        ("(a, secret) = (1, 2)", "assign", "secret"),
        ("a, (b, secret) = 1, (2, 3)", "assign", "secret"),
        ("(secret) = 1", "assign", "secret"),
        ("secret = lambda: 0", "assign", "secret"),
        ("secret = None", "assign", "secret"),
        ("for secret in s: pass", "assign", "secret"),
        ("[1 for secret in s]", "assign", "secret"),
        ("[(secret := 1) for _ in s]", "assign", "secret"),
        ("with f() as secret: pass", "assign", "secret"),
        ("with f() as secret, g() as b: pass", "assign", "secret"),
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
    (SensitiveVariableAssignment: `assigned_value` plus `with ... as <Name>`, measured against
    CodeQL) and `parameter` (SensitiveParameter).
    """
    assert list(scan_sensitive_names(source)) == [(1, kind, text)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("async def g():\n    async for secret in s: pass", [(2, "assign", "secret")]),
        ("async def g():\n    async with f() as secret: pass", [(2, "assign", "secret")]),
    ],
)
def test_scanner_reports_multiline_sensitive_sources(
    source: str, expected: list[tuple[int, str, str]]
) -> None:
    """Multiline `assign` bindings (`assigned_value` plus `with ... as <Name>`).

    Measured against CodeQL (`codeql/python-all` 7.2.10).
    """
    assert list(scan_sensitive_names(source)) == expected


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
        # CodeQL (codeql/python-all 7.2.10) is the oracle: zero SensitiveDataSource findings here.
        "with f() as (a, secret): pass",
        "for secret, b in s: pass",
        "for (a, secret) in s: pass",
        "a, secret = f()",
        "secret: int",
        "a, *secret = 1, 2, 3",
        "try:\n    pass\nexcept Exception as secret:\n    pass",
        "match v:\n    case secret:\n        pass",
        "match v:\n    case [*secret]:\n        pass",
        'match v:\n    case {"k": 1, **secret}:\n        pass',
        "import a.secret_box",
        "a.secret = 1",
        "f(password=1)",
        '{"password": 1}',
        'd.pop("password")',
        "print(secret)",
        "secret_obj.method()",
    ],
)
def test_scanner_ignores_excluded_and_non_lookup_names(source: str) -> None:
    """Excluded patterns and non-source expressions yield zero findings across all source kinds.

    Covers `NOT_SENSITIVE_REGEXP` exclusions plus non-sources of SensitiveSubscript,
    SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess, SensitiveVariableAssignment
    (`assigned_value` plus `with ... as <Name>`, measured against CodeQL) and SensitiveParameter.
    """
    assert list(scan_sensitive_names(source)) == []


def test_credential_names_key_matches_forge_workflow_key() -> None:
    """The workflow key name is fixed by the forge format."""
    assert ciwf.CREDENTIAL_NAMES_KEY == "secrets"
