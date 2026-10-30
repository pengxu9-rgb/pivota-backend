"""The X-Internal-Key guards compare the provided key in constant time, as bytes.

`==` / `!=` on the key is behaviourally identical, so no request-level test can tell it from
hmac.compare_digest; this reads the source instead. The guards in scope hold
AGENT_AUTH_INTROSPECT_INTERNAL_KEY, which also exempts its holder from the rate limiter and
(through /agent/internal/decision-slates) writes to the attribution ledger.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARDED_MODULES = [
    "routes/agent_internal_decision_slates.py",
    "routes/agent_identity_issuers.py",
    "routes/payment_grant_issuers.py",
]
KEY_NAMES = {"provided", "x_internal_key", "expected"}


def _guard(path: str) -> ast.FunctionDef:
    tree = ast.parse((REPO_ROOT / path).read_text(), filename=path)
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_require_internal_key"]
    assert len(found) == 1, f"{path}: expected one _require_internal_key"
    return found[0]


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


@pytest.mark.parametrize("path", GUARDED_MODULES)
def test_the_guard_calls_hmac_compare_digest_on_bytes(path: str) -> None:
    guard = _guard(path)
    calls = [
        n for n in ast.walk(guard)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "compare_digest" and isinstance(n.func.value, ast.Name) and n.func.value.id == "hmac"
    ]
    assert calls, f"{path}: _require_internal_key must use hmac.compare_digest"
    for call in calls:
        # both sides .encode(...)d: compare_digest on str raises on non-ASCII (a 500, the #1883 class)
        assert all(
            isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "encode"
            for arg in call.args
        ), f"{path}: compare_digest arguments must be bytes (.encode)"


@pytest.mark.parametrize("path", GUARDED_MODULES)
def test_the_guard_never_compares_the_key_with_eq(path: str) -> None:
    guard = _guard(path)
    for node in ast.walk(guard):
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)) for op in node.ops):
            touched = _names(node.left) | set().union(*(_names(c) for c in node.comparators))
            assert not touched & KEY_NAMES, f"{path}:{node.lineno}: key compared with {ast.unparse(node)}"
