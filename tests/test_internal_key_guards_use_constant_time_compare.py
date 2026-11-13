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


# ── the internal trusted agent key (routes/agent_auth.py) ─────────────────────
# Same reason, another helper: _is_internal_trusted_api_key decides whether a presented agent
# key is one of the configured trusted keys. Read from the source, and once through a spy.

TRUSTED_KEY_MODULE = "routes/agent_auth.py"
TRUSTED_KEY_NAMES = {"api_key", "candidate", "candidate_bytes", "trusted_key"}


def _trusted_key_helper() -> ast.FunctionDef:
    tree = ast.parse((REPO_ROOT / TRUSTED_KEY_MODULE).read_text(), filename=TRUSTED_KEY_MODULE)
    found = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_is_internal_trusted_api_key"]
    assert len(found) == 1
    return found[0]


def _is_encode_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "encode"


def test_the_trusted_key_helper_calls_hmac_compare_digest_on_bytes() -> None:
    helper = _trusted_key_helper()
    encoded_names = {
        target.id
        for node in ast.walk(helper) if isinstance(node, ast.Assign) and _is_encode_call(node.value)
        for target in node.targets if isinstance(target, ast.Name)
    }
    calls = [
        n for n in ast.walk(helper)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "compare_digest" and isinstance(n.func.value, ast.Name) and n.func.value.id == "hmac"
    ]
    assert len(calls) == 1, "one constant-time compare decides a trusted key"
    for arg in calls[0].args:
        # bytes on both sides: compare_digest on str raises on a character that is not ASCII
        assert _is_encode_call(arg) or (isinstance(arg, ast.Name) and arg.id in encoded_names), ast.unparse(arg)


def test_the_trusted_key_helper_never_compares_the_key_with_eq() -> None:
    helper = _trusted_key_helper()
    for node in ast.walk(helper):
        if isinstance(node, ast.Compare):
            touched = _names(node.left) | set().union(*(_names(c) for c in node.comparators))
            assert not touched & TRUSTED_KEY_NAMES, f"{TRUSTED_KEY_MODULE}:{node.lineno}: key compared with {ast.unparse(node)}"


def test_the_trusted_key_answer_is_the_constant_time_compares_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through a spy: every configured key is put to hmac.compare_digest as bytes, and the
    helper's answer is that call's answer, not the answer of a second comparison beside it."""
    import routes.agent_auth as agent_auth

    trusted = ("trusted-fixture-one", "trusted-fixture-twó")
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", trusted)
    seen: list[tuple[bytes, bytes]] = []
    answers: list[bool] = []
    real = agent_auth.hmac.compare_digest

    def spy(a, b):
        assert isinstance(a, bytes) and isinstance(b, bytes)
        seen.append((a, b))
        return answers.pop(0) if answers else real(a, b)

    monkeypatch.setattr(agent_auth.hmac, "compare_digest", spy)

    assert agent_auth._is_internal_trusted_api_key(" trusted-fixture-one ") is True
    assert seen == [(b"trusted-fixture-one", b"trusted-fixture-one")]
    seen.clear()
    assert agent_auth._is_internal_trusted_api_key("not-a-trusted-key") is False
    assert [b for _a, b in seen] == [key.encode("utf-8") for key in trusted]
    assert {a for a, _b in seen} == {b"not-a-trusted-key"}
    # A key that is not ASCII is compared, not raised on.
    seen.clear()
    assert agent_auth._is_internal_trusted_api_key("kéy") is False and len(seen) == 2
    # The answer is the compare's: say "equal" for a different key, "different" for the same.
    answers[:] = [False, True]
    assert agent_auth._is_internal_trusted_api_key("not-a-trusted-key") is True
    answers[:] = [False, False]
    assert agent_auth._is_internal_trusted_api_key("trusted-fixture-one") is False
    # Nothing to compare: no call at all.
    seen.clear()
    assert agent_auth._is_internal_trusted_api_key("") is False and agent_auth._is_internal_trusted_api_key(None) is False
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", ())
    assert agent_auth._is_internal_trusted_api_key("trusted-fixture-one") is False and seen == []
