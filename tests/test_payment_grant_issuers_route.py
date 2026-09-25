"""Payment-grant issuer registry — the admin gate, the validation wall, and the internal shape.

The trust decision under test: an agent must never be able to grant a PSP charge authority,
and a registration typo must be a 4xx, never a silently-weaker constraint.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

import db.payment_grant_issuers as store_mod
from main import app
from utils.auth import get_current_user

INVALID = 400  # house middleware maps 422 -> 400 on the wire


def _user(role: str):
    return {"role": role, "email": f"{role}@pivota.cc", "user_id": f"uid_{role}"}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    state: Dict[str, Any] = {"upserts": [], "disabled": [], "active_rows": [], "all_rows": []}

    async def fake_upsert(reg, *, registered_by, jwks_ok):
        state["upserts"].append((reg, registered_by, jwks_ok))
        return {
            "id": 1, "issuer": reg.issuer, "jwks_uri": reg.jwks_uri, "audience": reg.audience,
            "algs": reg.algs, "authorized_party": reg.authorized_party, "methods": reg.methods,
            "expected_vct": reg.expected_vct, "status": "active", "registered_by": registered_by,
            "last_jwks_ok_at": None, "created_at": None, "updated_at": None,
        }

    async def fake_deref(uri):
        state["deref"] = uri
        return {"keys": [{"kty": "EC"}]}

    async def fake_disable(issuer_id):
        state["disabled"].append(issuer_id)
        return issuer_id == 1

    async def fake_list_active():
        return state["active_rows"]

    async def fake_list_all():
        return state["all_rows"]

    monkeypatch.setattr(store_mod, "upsert_issuer", fake_upsert)
    monkeypatch.setattr(store_mod, "dereference_jwks", fake_deref)
    monkeypatch.setattr(store_mod, "disable_issuer", fake_disable)
    monkeypatch.setattr(store_mod, "list_active", fake_list_active)
    monkeypatch.setattr(store_mod, "list_all", fake_list_all)
    # shape rules must be testable without sockets
    monkeypatch.setattr(store_mod, "host_resolves_public_only", lambda host: True)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "ik_test_123")

    app.dependency_overrides[get_current_user] = lambda: _user("admin")
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _body(**over) -> Dict[str, Any]:
    base = {
        "issuer": "https://antom.example/payments",
        "jwks_uri": "https://antom.example/.well-known/jwks.json",
        "audience": "https://commerce.mcp.pivota.cc/mcp",
    }
    base.update(over)
    return base


# --- the admin gate --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["agent", "merchant", "user", "viewer", "employee"])
def test_every_route_refuses_non_admin(rig, role):
    client, state = rig
    app.dependency_overrides[get_current_user] = lambda: _user(role)
    assert client.get("/admin/payment-issuers").status_code == 403
    assert client.put("/admin/payment-issuers", json=_body()).status_code == 403
    assert client.delete("/admin/payment-issuers/1").status_code == 403
    assert state["upserts"] == [] and state["disabled"] == []


def test_super_admin_may_register_and_registered_by_is_recorded(rig):
    """The role set is the MONEY-admin norm (admin/super_admin), not the identity registry's
    (admin/employee): an employee JWT cannot even read settlements, so it must not be able to
    grant a PSP charge authority."""
    client, state = rig
    app.dependency_overrides[get_current_user] = lambda: _user("super_admin")
    r = client.put("/admin/payment-issuers", json=_body())
    assert r.status_code == 200
    (_, registered_by, jwks_ok) = state["upserts"][0]
    assert registered_by == "super_admin@pivota.cc" and jwks_ok is True


# --- the validation wall ---------------------------------------------------------------------


def test_unknown_field_is_refused_not_dropped(rig):
    client, state = rig
    r = client.put("/admin/payment-issuers", json=_body(audiences="typo"))
    assert r.status_code == INVALID
    assert state["upserts"] == []


@pytest.mark.parametrize("bad", [
    {"algs": ["HS256"]},                       # symmetric — forging key = the secret
    {"algs": ["none"]},
    {"algs": []},
    {"jwks_uri": "http://antom.example/jwks"}, # not https
    {"jwks_uri": "https://user:pw@x.example/jwks"},
    {"jwks_uri": "https://internal.svc.internal/jwks"},
    {"methods": ["settlement"]},               # not a known method
    {"methods": []},
    {"expected_vct": "PaymentMandate"},        # vct without ap2_mandate = fake AP2 trust
    {"issuer": "https://x.example/a b"},       # whitespace
    {"issuer": "https://x.example|evil"},      # gateway derives ${iss}|${sub}; its registry
                                               # builder THROWS on a piped iss — for ALL rows
])
def test_bad_registrations_are_422(rig, bad):
    client, state = rig
    r = client.put("/admin/payment-issuers", json=_body(**bad))
    assert r.status_code == INVALID, bad
    assert state["upserts"] == []


def test_expected_vct_with_ap2_method_is_accepted(rig):
    client, state = rig
    r = client.put(
        "/admin/payment-issuers",
        json=_body(methods=["signed_grant", "ap2_mandate"], expected_vct="PaymentMandate"),
    )
    assert r.status_code == 200
    reg = state["upserts"][0][0]
    assert reg.methods == ["signed_grant", "ap2_mandate"] and reg.expected_vct == "PaymentMandate"


def test_unfetchable_jwks_is_422_and_nothing_stored(rig, monkeypatch):
    client, state = rig
    from db.payment_grant_issuers import IssuerValidationError

    async def boom(uri):
        raise IssuerValidationError("jwks_uri", "JWKS could not be fetched as a usable key set")

    monkeypatch.setattr(store_mod, "dereference_jwks", boom)
    r = client.put("/admin/payment-issuers", json=_body())
    assert r.status_code == INVALID
    assert state["upserts"] == []


def test_default_methods_is_signed_grant_only(rig):
    client, state = rig
    client.put("/admin/payment-issuers", json=_body())
    reg = state["upserts"][0][0]
    assert reg.methods == ["signed_grant"]  # AP2 trust is never implicit


# --- disable ---------------------------------------------------------------------------------


def test_disable_unknown_id_is_404(rig):
    client, _ = rig
    assert client.delete("/admin/payment-issuers/999").status_code == 404
    assert client.delete("/admin/payment-issuers/1").status_code == 200


# --- internal registry -----------------------------------------------------------------------


def test_internal_registry_requires_the_key(rig, monkeypatch):
    client, _ = rig
    assert client.get("/agent/internal/payment-issuers").status_code == 403
    assert client.get(
        "/agent/internal/payment-issuers", headers={"X-Internal-Key": "wrong"}
    ).status_code == 403
    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY")
    assert client.get(
        "/agent/internal/payment-issuers", headers={"X-Internal-Key": "ik_test_123"}
    ).status_code == 500  # unconfigured is a server bug, never an open registry


def test_non_ascii_internal_key_is_403_not_500(rig):
    """Starlette decodes headers latin-1; hmac.compare_digest on str raises on non-ASCII —
    the #1883 class: an unauthenticated request must never be able to pick a 500. Pinned at
    the helper seam because httpx refuses to send a non-ASCII header at all."""
    from routes.payment_grant_issuers import _require_internal_key
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        _require_internal_key(b"ik_caf\xe9".decode("latin-1"))
    assert err.value.status_code == 403


def test_internal_registry_serves_the_verifier_entry_shape(rig):
    client, state = rig
    state["active_rows"] = [{
        "issuer": "https://antom.example/payments",
        "jwks_uri": "https://antom.example/jwks",
        "audience": "aud1",
        "algs": ["ES256"],
        "authorized_party": "antom-client",
        "methods": ["signed_grant", "ap2_mandate"],
        "expected_vct": "PaymentMandate",
        "updated_at": None,
    }]
    r = client.get("/agent/internal/payment-issuers", headers={"X-Internal-Key": "ik_test_123"})
    assert r.status_code == 200
    (entry,) = r.json()["issuers"]
    # the KEY NAMES are the contract: these rows become createSignedGrantVerifier config entries
    assert entry == {
        "iss": "https://antom.example/payments",
        "jwksUri": "https://antom.example/jwks",
        "aud": "aud1",
        "algs": ["ES256"],
        "azp": "antom-client",
        "methods": ["signed_grant", "ap2_mandate"],
        "expectedVct": "PaymentMandate",
        "updated_at": None,
    }
