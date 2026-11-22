# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""CodeQL treats expressions whose names look sensitive as sources of secret data.

Workflow secret names logged through a dict lookup or helper function raised alerts 1 and 12
(false positives). This test mirrors the name-based source classes in codeql/python-all 7.2.10
`semmle/python/dataflow/new/SensitiveDataSources.qll` (`SensitiveSubscript`, `SensitiveGetCall`,
`SensitiveFunctionCall`, `SensitiveAttributeAccess`, `SensitiveVariableAssignment` and
`SensitiveParameter`) to keep all seven scanned source kinds (`subscript`, `get`, `function`,
`call`, `attribute`, `assign` and `parameter`) out of `tools/*.py` before CI. Lookup keys mirror
`sensitiveLookupStringConst` (local flow: same-scope literal aliases, if-expressions, boolean
operators, walrus; never cross-module) and `assign` mirrors `Flow.qll` `assigned_value` plus
`with ... as <Name>`, measured against CodeQL.
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


_BODY_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _default_child_scope(node: ast.AST, child: ast.AST, scope: int) -> int:
    """Evaluation scope inherited by `child` before comprehension-iter overrides."""
    if isinstance(node, _BODY_SCOPE_NODES):
        return id(node) if isinstance(child, ast.stmt) else scope
    if isinstance(node, ast.Lambda):
        return id(node) if child is node.body else scope
    if isinstance(node, _COMPREHENSION_NODES):
        return id(node)
    return scope


def _node_scopes(tree: ast.AST, max_steps: int) -> dict[int, int]:
    """Map `id(node)` to its evaluation scope id across `tree`, without recursion."""
    scopes: dict[int, int] = {}
    overrides: dict[int, int] = {}
    stack: list[tuple[ast.AST, int]] = [(tree, id(tree))]
    for _ in range(max_steps):
        if not stack:
            break
        node, scope = stack.pop()
        scopes[id(node)] = scope
        if isinstance(node, _COMPREHENSION_NODES) and node.generators:
            overrides[id(node.generators[0].iter)] = scope
        children = list(ast.iter_child_nodes(node))
        for child in reversed(children):
            child_scope = overrides.get(id(child), _default_child_scope(node, child, scope))
            stack.append((child, child_scope))
    return scopes


def _prefix_before_starred(elts: list[ast.expr]) -> list[ast.expr]:
    """Sequence elements appearing before any `ast.Starred` element."""
    prefix: list[ast.expr] = []
    for elt in elts:
        if isinstance(elt, ast.Starred):
            break
        prefix.append(elt)
    return prefix


def _after_starred_pairs(
    lhs_elts: list[ast.expr], rhs_elts: list[ast.expr]
) -> list[tuple[ast.expr, ast.expr]]:
    """Suffix `(target, value)` pairs after one `ast.Starred` target when `rhs_elts` has none."""
    if any(isinstance(elt, ast.Starred) for elt in rhs_elts):
        return []
    starred = [idx for idx, elt in enumerate(lhs_elts) if isinstance(elt, ast.Starred)]
    if len(starred) != 1:
        return []
    split = starred[0]
    after = lhs_elts[split + 1 :]
    if len(rhs_elts) < split + len(after):
        return []
    offset = len(rhs_elts) - len(after)
    return [(elt, rhs_elts[offset + j]) for j, elt in enumerate(after)]


def _assign_value_bindings(node: ast.Assign, max_steps: int) -> list[tuple[str, ast.expr]]:
    """Index-paired `(name, value)` bindings from an `ast.Assign`, without recursion."""
    pairs: list[tuple[str, ast.expr]] = []
    stack: list[tuple[ast.expr, ast.expr]] = [
        (target, node.value) for target in reversed(node.targets)
    ]
    for _ in range(max_steps):
        if not stack:
            break
        target, value = stack.pop()
        if isinstance(target, ast.Name):
            pairs.append((target.id, value))
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            lhs = _prefix_before_starred(target.elts)
            rhs = _prefix_before_starred(value.elts)
            seq_pairs = list(zip(lhs, rhs, strict=False))
            seq_pairs.extend(_after_starred_pairs(target.elts, value.elts))
            stack.extend(reversed(seq_pairs))
    return pairs


def _node_value_bindings(node: ast.AST, max_steps: int) -> list[tuple[str, ast.expr]]:
    """Same-scope `(name, value)` bindings introduced by `node`."""
    if isinstance(node, ast.Assign):
        return _assign_value_bindings(node, max_steps)
    if (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and isinstance(node.target, ast.Name)
    ):
        return [(node.target.id, node.value)]
    if isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
        return [(node.target.id, node.value)]
    return []


def _scope_bindings(
    nodes: list[ast.AST], scopes: dict[int, int]
) -> dict[int, dict[str, list[ast.expr]]]:
    """Flow-insensitive `name -> [value_expr, ...]` table per evaluation scope."""
    table: dict[int, dict[str, list[ast.expr]]] = {}
    max_steps = max(len(nodes), 1)
    for node in nodes:
        scope_map = table.setdefault(scopes[id(node)], {})
        for name, value in _node_value_bindings(node, max_steps):
            scope_map.setdefault(name, []).append(value)
    return table


def _joined_str_value(node: ast.JoinedStr) -> str | None:
    """Joined text when every part of `node` is a string `ast.Constant`, else None."""
    parts: list[str] = []
    for value in node.values:
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            return None
        parts.append(value.value)
    return "".join(parts)


def _literal_str(node: ast.AST) -> str | None:
    """Direct string value for a string `Constant` or all-constant `JoinedStr`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _joined_str_value(node)
    return None


def _step_exprs(node: ast.AST, scope_map: dict[str, list[ast.expr]]) -> list[ast.expr]:
    """Immediate value expressions reachable in one local-flow step from `node`."""
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.NamedExpr):
        return [node.value]
    if isinstance(node, ast.Name):
        return list(scope_map.get(node.id, ()))
    return []


def _resolve_strings(
    expr: ast.AST, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[str]:
    """Possible strings of `expr` in `scope_map`, via bounded iterative worklist."""
    strings: list[str] = []
    visited: set[int] = {id(expr)}
    stack: list[ast.AST] = [expr]
    for _ in range(max_steps):
        if not stack:
            break
        current = stack.pop()
        text = _literal_str(current)
        if text is not None and text not in strings:
            strings.append(text)
        for nxt in reversed(_step_exprs(current, scope_map)):
            if id(nxt) not in visited:
                visited.add(id(nxt))
                stack.append(nxt)
    return strings


def _arg_strings(
    node: ast.Call, index: int, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[str]:
    """Possible string values of positional argument `index`, or empty list."""
    if len(node.args) > index:
        return _resolve_strings(node.args[index], scope_map, max_steps)
    return []


def _subscript_names(
    node: ast.AST, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[tuple[int, str, str]]:
    """SensitiveSubscript: `sensitiveLookupStringConst` index in a subscript."""
    if isinstance(node, ast.Subscript):
        return [
            (node.lineno, "subscript", key)
            for key in _resolve_strings(node.slice, scope_map, max_steps)
        ]
    return []


def _get_names(
    node: ast.AST, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[tuple[int, str, str]]:
    """SensitiveGetCall: `sensitiveLookupStringConst` positional arg 0 in `.get(...)`."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
    ):
        return [(node.lineno, "get", key) for key in _arg_strings(node, 0, scope_map, max_steps)]
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


def _attribute_names(
    node: ast.AST, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[tuple[int, str, str]]:
    """SensitiveAttributeAccess: attribute load, from-import member or `getattr`."""
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        return [(node.lineno, "attribute", node.attr)]
    if isinstance(node, ast.ImportFrom):
        return [(alias.lineno, "attribute", alias.name) for alias in node.names]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
        return [
            (node.lineno, "attribute", attr) for attr in _arg_strings(node, 1, scope_map, max_steps)
        ]
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


_LOOKUP_SCANNERS = (
    _subscript_names,
    _get_names,
    _attribute_names,
)
_DIRECT_SCANNERS = (
    _function_names,
    _call_names,
    _assign_names,
    _parameter_names,
)


def _node_candidates(
    node: ast.AST, scope_map: dict[str, list[ast.expr]], max_steps: int
) -> list[tuple[int, str, str]]:
    """Candidate (line, kind, text) entries extracted from one AST node."""
    entries = [
        entry for scanner in _LOOKUP_SCANNERS for entry in scanner(node, scope_map, max_steps)
    ]
    entries.extend(entry for scanner in _DIRECT_SCANNERS for entry in scanner(node))
    return entries


def scan_sensitive_names(source: str):
    """Yield (line, kind, text) for CodeQL name-based sensitive sources in `source`.

    Covers `subscript` (`SensitiveSubscript`), `get` (`SensitiveGetCall`), `function` and `call`
    (`SensitiveFunctionCall`), `attribute` (`SensitiveAttributeAccess`: attribute load,
    `from ... import` member and `getattr`), `assign` (`SensitiveVariableAssignment`:
    `assigned_value` plus `with ... as <Name>`) and `parameter` (`SensitiveParameter`). Lookup
    keys mirror `sensitiveLookupStringConst` (local flow: same-scope literal aliases,
    if-expressions, boolean operators, walrus; never cross-module), measured against CodeQL.
    """
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    max_steps = max(len(nodes), 1)
    scopes = _node_scopes(tree, max_steps)
    bindings = _scope_bindings(nodes, scopes)
    seen: set[tuple[int, str, str]] = set()
    for node in nodes:
        scope_map = bindings.get(scopes[id(node)], {})
        for entry in _node_candidates(node, scope_map, max_steps):
            if _is_sensitive(entry[2]) and entry not in seen:
                seen.add(entry)
                yield entry


def test_tools_hold_no_sensitive_lookup_keys_or_function_names() -> None:
    """Every tools/*.py file scans to zero findings across all covered CodeQL source kinds.

    Covers SensitiveSubscript, SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess,
    SensitiveVariableAssignment (`assigned_value` plus `with ... as <Name>`) and
    SensitiveParameter (`subscript`, `get`, `function`, `call`, `attribute`, `assign` and
    `parameter`), with lookup keys mirroring `sensitiveLookupStringConst` (local flow:
    same-scope literal aliases, if-expressions, boolean operators, walrus; never cross-module),
    measured against CodeQL.
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
        ('key = "secrets"; d.get(key)', "get", "secrets"),
        ('key = "secrets"; d[key]', "subscript", "secrets"),
        ('key = "secrets"; k2 = key; d[k2]', "subscript", "secrets"),
        ('key = "secret"; getattr(o, key)', "attribute", "secret"),
        ('key = "secrets" if c else "plain"; d[key]', "subscript", "secrets"),
        ('key = ("secrets" if a else "x") if b else "y"; d.get(key)', "get", "secrets"),
        ('d["secrets" if c else "plain"]', "subscript", "secrets"),
        ('d.get("plain" if c else "secrets")', "get", "secrets"),
        ('d[(key := "secrets")]', "subscript", "secrets"),
        ('key, other = "secrets", "plain"; d[key]', "subscript", "secrets"),
        ('key, *rest = "secrets", "plain"; d[key]', "subscript", "secrets"),
        ('*rest, key = "plain", "secrets"; d[key]', "subscript", "secrets"),
        ('first, *rest, key = "plain", "x", "secrets"; d[key]', "subscript", "secrets"),
        ('rest, key = ["plain", "secrets"]; d[key]', "subscript", "secrets"),
        ('(key, other), z = ("secrets", "plain"), 1; d[key]', "subscript", "secrets"),
        ('key: str = "secrets"; d[key]', "subscript", "secrets"),
        ('key = c or "secrets"; d[key]', "subscript", "secrets"),
        ('key = f"secrets"; d[key]', "subscript", "secrets"),
        # CodeQL does not report this reassigned alias; flow-insensitive per scope on purpose.
        ('key = "secrets"; key = "plain"; d[key]', "subscript", "secrets"),
    ],
)
def test_scanner_reports_planted_sensitive_sources(source: str, kind: str, text: str) -> None:
    """Each planted sensitive source yields one finding of its kind.

    Covers `subscript` (SensitiveSubscript), `get` (SensitiveGetCall), `function` and `call`
    (SensitiveFunctionCall), `attribute` (SensitiveAttributeAccess), `assign`
    (SensitiveVariableAssignment: `assigned_value` plus `with ... as <Name>`) and `parameter`
    (SensitiveParameter), with lookup keys mirroring `sensitiveLookupStringConst` (local flow:
    same-scope literal aliases, if-expressions, boolean operators, walrus; never cross-module),
    measured against CodeQL.
    """
    assert list(scan_sensitive_names(source)) == [(1, kind, text)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("async def g():\n    async for secret in s: pass", [(2, "assign", "secret")]),
        ("async def g():\n    async with f() as secret: pass", [(2, "assign", "secret")]),
        (
            'if c:\n    key = "secrets"\nelse:\n    key = "plain"\nd.get(key)',
            [(5, "get", "secrets")],
        ),
        ('def f():\n    key = "secrets"\n    return d.get(key)', [(3, "get", "secrets")]),
        ('def f():\n    key = "secrets"\n    return d[key]', [(3, "subscript", "secrets")]),
        ('class C:\n    key = "secrets"\n    val = {}[key]', [(3, "subscript", "secrets")]),
        ('with ctx():\n    key = "secrets"\nd[key]', [(3, "subscript", "secrets")]),
        (
            'try:\n    key = "secrets"\nexcept ValueError:\n    key = "plain"\nd[key]',
            [(5, "subscript", "secrets")],
        ),
        ('key = "secrets"\nwhile cond:\n    d[key]', [(3, "subscript", "secrets")]),
        # CodeQL does not report the except-branch alias; flow-insensitive per scope on purpose.
        (
            'try:\n    key = "plain"\nexcept ValueError:\n    key = "secrets"\nd[key]',
            [(5, "subscript", "secrets")],
        ),
    ],
)
def test_scanner_reports_multiline_sensitive_sources(
    source: str, expected: list[tuple[int, str, str]]
) -> None:
    """Multiline `assign` bindings and `sensitiveLookupStringConst` lookup aliases.

    Lookup keys mirror `sensitiveLookupStringConst` (local flow: same-scope literal aliases,
    if-expressions, boolean operators, walrus; never cross-module), measured against CodeQL
    (`codeql/python-all` 7.2.10).
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
        # CodeQL (codeql/python-all 7.2.10) is the oracle: sensitiveLookupStringConst stops here.
        'KEY = "secrets"\ndef f():\n    return d[KEY]',
        "from consts import KEY as IMPORTED; d[IMPORTED]",
        "d[consts.KEY]",
        'def f(key="secrets"): return d[key]',
        'lambda key="secrets": d[key]',
        'def f():\n    key = "secrets"\n    def g():\n        return d[key]',
        'key = "secrets"; (lambda: d[key])()',
        'key = "secrets"; [d[key] for _ in s]',
        '[d[k] for k in ["secrets"]]',
        'for key in ("secrets",): d[key]',
        'key = "sec"; key += "rets"; d[key]',
        'd["sec" + "rets"]',
        'key = "sec" + "rets"; d[key]',
        "d.get(key=key)",
        'key = "secrets"; d.get(key=key)',
        'key = "is_secret"; d[key]',
        'key = "secrets"\nprint(key)',
        '*rest, key = "plain", f(); d[key]',
        'a, *rest, key = "plain"; d[key]',
    ],
)
def test_scanner_ignores_excluded_and_non_lookup_names(source: str) -> None:
    """Excluded patterns and non-source expressions yield zero findings across all source kinds.

    Covers `NOT_SENSITIVE_REGEXP` exclusions plus non-sources of SensitiveSubscript,
    SensitiveGetCall, SensitiveFunctionCall, SensitiveAttributeAccess,
    SensitiveVariableAssignment (`assigned_value` plus `with ... as <Name>`) and
    SensitiveParameter, where lookup keys mirror `sensitiveLookupStringConst` (local flow:
    same-scope literal aliases, if-expressions, boolean operators, walrus; never cross-module),
    measured against CodeQL.
    """
    assert list(scan_sensitive_names(source)) == []


def test_credential_names_key_matches_forge_workflow_key() -> None:
    """The workflow key name is fixed by the forge format."""
    assert ciwf.CREDENTIAL_NAMES_KEY == "secrets"
