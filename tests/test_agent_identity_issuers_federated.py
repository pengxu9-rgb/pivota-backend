"""Federated buyer identity: an agent's OWN issuer, bound to that agent.

Contracts pinned:
1. verify_agent_user_jwt_for_agent: a token from an issuer registered to agent A verifies for
   agent A (its JWKS / aud / algs), is REFUSED for agent B (no binding, no global issuer), and
   falls back to the global env issuer only when no binding matches.
2. The binding enforces azp / required_scopes when registered.
3. Registration route: shape validation names the field; a JWKS that cannot be dereferenced is
   refused (422) and nothing is stored; an issuer active under another agent is 409; the owner
   rule is the same as api-keys.
4. The internal registry requires X-Internal-Key and emits the gateway's issuer-entry shape.
"""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt import algorithms


def _mk_rsa_jwks(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, {"keys": [jwk]}


def _token(key, kid, **claims):
    now = int(time.time())
    payload = {"iat": now, "exp": now + 60, **claims}
    return jwt.encode(payload, key=key, algorithm="RS256", headers={"kid": kid})


def _install_jwks_http(monkeypatch, url_to_jwks, calls=None):
    """The federated verifier fetches a binding's JWKS with httpx.AsyncClient; serve from memory by URL."""
    import services.agent_user_jwt as mod

    class _Resp:
        def __init__(self, doc, status=200):
            self._doc, self.status_code = doc, status

        def json(self):
            return self._doc

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            if calls is not None:
                calls.append(url)
            if url in url_to_jwks:
                return _Resp(url_to_jwks[url])
            return _Resp({}, 404)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    mod._JWKS_CACHE.clear()
    mod._JWKS_LAST_REFRESH.clear()


def _install_bindings(monkeypatch, bindings):
    """bindings: {(agent_id, iss): row-dict}"""
    import services.agent_user_jwt as mod  # noqa: F401 — ensures module import order
    import db.agent_identity_issuers as store

    async def fake_get_active_issuer(agent_id, issuer):
        return bindings.get((agent_id, issuer))

    monkeypatch.setattr(store, "get_active_issuer", fake_get_active_issuer)


# ── 1 + 2: verification bound to the agent ────────────────────────────────────

@pytest.mark.asyncio
async def test_federated_token_verifies_only_for_the_agent_that_registered_the_issuer(monkeypatch):
    from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt_for_agent

    for var in ("AGENT_USER_JWKS_JSON", "AGENT_USER_JWKS_URL", "AGENT_USER_JWKS_FILE", "AGENT_USER_JWT_ISSUER", "AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    key, jwks = _mk_rsa_jwks("minds-k1")
    _install_jwks_http(monkeypatch, {"https://id.minds.example/.well-known/jwks.json": jwks})
    _install_bindings(monkeypatch, {
        ("agent_minds", "https://id.minds.example"): {
            "issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/.well-known/jwks.json",
            "audience": "https://commerce.mcp.pivota.cc", "algs": ["RS256"], "authorized_party": None, "required_scopes": None,
        },
    })
    token = _token(key, "minds-k1", iss="https://id.minds.example", sub="u-42", aud="https://commerce.mcp.pivota.cc")

    ident = await verify_agent_user_jwt_for_agent(token, "agent_minds")
    assert ident.issuer == "https://id.minds.example"
    assert ident.subject == "u-42"
    assert ident.agent_user_ref == "https://id.minds.example:u-42"

    # Same token presented with ANOTHER agent's key: no binding, no global issuer → refused.
    with pytest.raises(AgentUserJwtError):
        await verify_agent_user_jwt_for_agent(token, "agent_other")
    # …and with no agent at all.
    with pytest.raises(AgentUserJwtError):
        await verify_agent_user_jwt_for_agent(token, None)


@pytest.mark.asyncio
async def test_federated_binding_enforces_audience_algs_azp_and_scopes(monkeypatch):
    from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt_for_agent

    key, jwks = _mk_rsa_jwks("k")
    _install_jwks_http(monkeypatch, {"https://idp.example/jwks": jwks})
    _install_bindings(monkeypatch, {
        ("agent_a", "https://idp.example"): {
            "issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks", "audience": "pivota",
            "algs": ["RS256"], "authorized_party": "client-1", "required_scopes": ["pivota.checkout"],
        },
    })
    good = _token(key, "k", iss="https://idp.example", sub="s", aud="pivota", azp="client-1", scope="openid pivota.checkout")
    assert (await verify_agent_user_jwt_for_agent(good, "agent_a")).subject == "s"

    wrong_aud = _token(key, "k", iss="https://idp.example", sub="s", aud="other", azp="client-1", scope="pivota.checkout")
    wrong_azp = _token(key, "k", iss="https://idp.example", sub="s", aud="pivota", azp="client-9", scope="pivota.checkout")
    no_scope = _token(key, "k", iss="https://idp.example", sub="s", aud="pivota", azp="client-1", scope="openid")
    for bad in (wrong_aud, wrong_azp, no_scope):
        with pytest.raises(AgentUserJwtError):
            await verify_agent_user_jwt_for_agent(bad, "agent_a")


@pytest.mark.asyncio
async def test_falls_back_to_global_issuer_when_no_binding_matches(monkeypatch):
    from services.agent_user_jwt import verify_agent_user_jwt_for_agent

    key, jwks = _mk_rsa_jwks("g")
    monkeypatch.setenv("AGENT_USER_JWKS_JSON", json.dumps(jwks))
    monkeypatch.setenv("AGENT_USER_JWT_ISSUER", "https://global.example")
    monkeypatch.setenv("AGENT_USER_JWT_AUDIENCE", "pivota")
    import services.agent_user_jwt as mod
    mod._JWKS_CACHE.clear()
    _install_bindings(monkeypatch, {})
    token = _token(key, "g", iss="https://global.example", sub="gu", aud="pivota")
    assert (await verify_agent_user_jwt_for_agent(token, "agent_any")).subject == "gu"


# ── 3: registration route ─────────────────────────────────────────────────────

def _portal_client(monkeypatch, *, agent_id="agent_minds", role="agent"):
    import routes.agent_identity_issuers as module
    import db.agent_identity_issuers as store
    from utils.auth import get_current_user

    # Example hosts do not resolve in CI; the SSRF resolver has its own tests below.
    monkeypatch.setattr(store, "host_resolves_public_only", lambda host: host not in {"10.0.0.1", "169.254.169.254", "192.168.1.5"})
    for var in ("AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_ISSUER", "AGENT_IDENTITY_RESERVED_ISSUERS", "MCP_OAUTH_AS_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(module.router)
    app.include_router(module.internal_router)
    app.dependency_overrides[get_current_user] = lambda: {"agent_id": agent_id, "role": role, "email": "m@minds.example"}
    return TestClient(app), module


def test_registration_validates_shape_and_names_the_field(monkeypatch):
    client, _ = _portal_client(monkeypatch)
    cases = [
        ({"issuer": "https://a|b", "jwks_uri": "https://x/j", "audience": "p"}, "issuer"),
        ({"issuer": "https://a", "jwks_uri": "http://x/j", "audience": "p"}, "jwks_uri"),
        ({"issuer": "https://a", "jwks_uri": "https://localhost/j", "audience": "p"}, "jwks_uri"),
        ({"issuer": "https://a", "jwks_uri": "https://x/j", "audience": " "}, "audience"),
        ({"issuer": "https://a", "jwks_uri": "https://x/j", "audience": "p", "algs": ["HS256"]}, "algs"),
        ({"issuer": "https://a", "jwks_uri": "https://x/j", "audience": "p", "algs": []}, "algs"),
        ({"issuer": "https://a", "jwks_uri": "https://10.0.0.1/j", "audience": "p"}, "jwks_uri"),
        ({"issuer": "https://a", "jwks_uri": "https://169.254.169.254/latest", "audience": "p"}, "jwks_uri"),
        ({"issuer": "https://a", "jwks_uri": "https://user:pw@x/j", "audience": "p"}, "jwks_uri"),
    ]
    for body, field in cases:
        resp = client.put("/agents/agent_minds/identity-issuers", json=body)
        assert resp.status_code == 422, (body, resp.text)
        assert resp.json()["detail"]["field"] == field, (body, resp.text)


def test_registration_refuses_unreachable_jwks_and_stores_nothing(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    stored = []

    async def no_owner(_iss):
        return None

    async def boom(_uri):
        raise store.IssuerValidationError("jwks_uri", "JWKS returned HTTP 404")

    async def record(*a, **k):
        stored.append((a, k))
        return {}

    monkeypatch.setattr(store, "find_active_owner", no_owner)
    monkeypatch.setattr(store, "dereference_jwks", boom)
    monkeypatch.setattr(store, "upsert_issuer", record)

    resp = client.put("/agents/agent_minds/identity-issuers", json={"issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks", "audience": "pivota"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "JWKS_UNREACHABLE"
    assert stored == []


def test_registration_is_409_when_another_agent_owns_the_issuer_and_403_for_non_owner(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    async def other_owner(_iss):
        return "agent_other"

    monkeypatch.setattr(store, "find_active_owner", other_owner)
    resp = client.put("/agents/agent_minds/identity-issuers", json={"issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks", "audience": "pivota"})
    assert resp.status_code == 409

    resp = client.put("/agents/agent_someone_else/identity-issuers", json={"issuer": "https://x", "jwks_uri": "https://x/j", "audience": "p"})
    assert resp.status_code == 403
    assert client.get("/agents/agent_someone_else/identity-issuers").status_code == 403


def test_registration_happy_path_dereferences_then_upserts(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    calls = {"deref": 0}

    async def no_owner(_iss):
        return None

    async def ok_jwks(_uri):
        calls["deref"] += 1
        return {"keys": [{"kty": "RSA", "kid": "a"}]}

    async def upsert(agent_id, reg, *, jwks_ok):
        assert agent_id == "agent_minds" and jwks_ok is True
        # Scope enforcement defaults ON when the registration omits required_scopes.
        assert reg.required_scopes == ["pivota.checkout"]
        return {"id": 1, "agent_id": agent_id, "issuer": reg.issuer, "jwks_uri": reg.jwks_uri, "audience": reg.audience,
                "algs": reg.algs, "authorized_party": None, "required_scopes": reg.required_scopes, "status": "active",
                "last_jwks_ok_at": None, "created_at": None, "updated_at": None}

    monkeypatch.setattr(store, "find_active_owner", no_owner)
    monkeypatch.setattr(store, "dereference_jwks", ok_jwks)
    monkeypatch.setattr(store, "upsert_issuer", upsert)

    resp = client.put("/agents/agent_minds/identity-issuers", json={"issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks", "audience": "https://commerce.mcp.pivota.cc"})
    assert resp.status_code == 200, resp.text
    assert calls["deref"] == 1
    assert resp.json()["issuer"]["algs"] == ["RS256", "ES256"]
    assert resp.json()["issuer"]["required_scopes"] == ["pivota.checkout"]


# ── 3b: required_scopes defaults fail-closed ──────────────────────────────────

def test_required_scopes_default_and_explicit_values(monkeypatch):
    import db.agent_identity_issuers as store

    monkeypatch.setattr(store, "host_resolves_public_only", lambda host: True)
    base = {"issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks", "audience": "p"}

    # Omitted and explicit null both take the default — neither is a conscious opt-out.
    assert store.normalize_registration(dict(base)).required_scopes == ["pivota.checkout"]
    assert store.normalize_registration({**base, "required_scopes": None}).required_scopes == ["pivota.checkout"]
    # Explicit scopes are preserved (deduped, stripped), NOT merged with the default.
    assert store.normalize_registration({**base, "required_scopes": ["a.b", " a.b ", "c"]}).required_scopes == ["a.b", "c"]
    # The explicit opt-out stores no scope check at all.
    assert store.normalize_registration({**base, "allow_unscoped_tokens": True}).required_scopes is None
    assert store.normalize_registration({**base, "allow_unscoped_tokens": True, "required_scopes": []}).required_scopes is None
    # allow_unscoped_tokens: false behaves like the flag being absent.
    assert store.normalize_registration({**base, "allow_unscoped_tokens": False}).required_scopes == ["pivota.checkout"]


def test_required_scopes_refusals_name_the_field(monkeypatch):
    import db.agent_identity_issuers as store

    monkeypatch.setattr(store, "host_resolves_public_only", lambda host: True)
    base = {"issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks", "audience": "p"}
    cases = [
        # An empty array alone must not silently disable the check (empty multi-select hazard).
        ({**base, "required_scopes": []}, "required_scopes"),
        ({**base, "required_scopes": ["  "]}, "required_scopes"),
        # Opting out while also requiring scopes is a contradiction, not a merge.
        ({**base, "allow_unscoped_tokens": True, "required_scopes": ["pivota.checkout"]}, "allow_unscoped_tokens"),
        ({**base, "allow_unscoped_tokens": "true"}, "allow_unscoped_tokens"),
    ]
    for body, field in cases:
        with pytest.raises(store.IssuerValidationError) as ei:
            store.normalize_registration(body)
        assert ei.value.field == field, body


def test_registration_route_empty_scopes_is_422_and_opt_out_stores_none(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    stored = []

    async def no_owner(_iss):
        return None

    async def ok_jwks(_uri):
        return {"keys": [{"kty": "RSA", "kid": "a"}]}

    async def upsert(agent_id, reg, *, jwks_ok):
        stored.append(reg.required_scopes)
        return {"id": 1, "agent_id": agent_id, "issuer": reg.issuer, "jwks_uri": reg.jwks_uri, "audience": reg.audience,
                "algs": reg.algs, "authorized_party": None, "required_scopes": reg.required_scopes, "status": "active",
                "last_jwks_ok_at": None, "created_at": None, "updated_at": None}

    monkeypatch.setattr(store, "find_active_owner", no_owner)
    monkeypatch.setattr(store, "dereference_jwks", ok_jwks)
    monkeypatch.setattr(store, "upsert_issuer", upsert)

    base = {"issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks", "audience": "p"}
    resp = client.put("/agents/agent_minds/identity-issuers", json={**base, "required_scopes": []})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["field"] == "required_scopes"
    assert stored == []

    resp = client.put("/agents/agent_minds/identity-issuers", json={**base, "allow_unscoped_tokens": True})
    assert resp.status_code == 200, resp.text
    assert stored == [None]


def test_row_to_public_surfaces_unscoped_bindings():
    import db.agent_identity_issuers as store

    scoped = store._row_to_public({"required_scopes": ["pivota.checkout"]})
    assert scoped["required_scopes"] == ["pivota.checkout"]
    assert scoped["unscoped_tokens_allowed"] is False
    unscoped = store._row_to_public({"required_scopes": None})
    assert unscoped["required_scopes"] is None
    assert unscoped["unscoped_tokens_allowed"] is True


# ── 4: internal registry ──────────────────────────────────────────────────────

def test_internal_registry_requires_the_internal_key_and_emits_gateway_entries(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    async def rows():
        return [{"id": 1, "agent_id": "agent_minds", "issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks",
                 "audience": "pivota", "algs": ["RS256"], "authorized_party": None, "required_scopes": ["pivota.checkout"],
                 "status": "active", "last_jwks_ok_at": None, "created_at": None, "updated_at": None}]

    monkeypatch.setattr(store, "list_active_registry", rows)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "secret-internal")

    assert client.get("/agent/internal/identity-issuers").status_code == 403
    assert client.get("/agent/internal/identity-issuers", headers={"X-Internal-Key": "wrong"}).status_code == 403
    resp = client.get("/agent/internal/identity-issuers", headers={"X-Internal-Key": "secret-internal"})
    assert resp.status_code == 200
    entry = resp.json()["issuers"][0]
    assert entry == {
        "agent_id": "agent_minds", "iss": "https://id.minds.example", "jwksUri": "https://id.minds.example/jwks",
        "aud": "pivota", "algs": ["RS256"], "azp": None, "requiredScopes": ["pivota.checkout"], "updated_at": None,
    }

    monkeypatch.delenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", raising=False)
    assert client.get("/agent/internal/identity-issuers", headers={"X-Internal-Key": "secret-internal"}).status_code == 500


# ── 5: reserved / global issuers, races, SSRF, max age, kid-miss cap ──────────

def test_reserved_issuers_cannot_be_registered(monkeypatch):
    client, module = _portal_client(monkeypatch)
    monkeypatch.setenv("AGENT_USER_JWT_ISSUERS", "https://identity.pivota.cc, https://partner.example/")
    monkeypatch.setenv("MCP_OAUTH_AS_ISSUER", "https://api.pivota.cc")
    monkeypatch.setenv("AGENT_IDENTITY_RESERVED_ISSUERS", "https://accounts.google.com")
    for iss in ("https://identity.pivota.cc", "https://PARTNER.example", "https://api.pivota.cc/", "https://accounts.google.com"):
        resp = client.put("/agents/agent_minds/identity-issuers", json={"issuer": iss, "jwks_uri": "https://x/j", "audience": "p"})
        assert resp.status_code == 422, (iss, resp.text)
        assert resp.json()["detail"]["field"] == "issuer"


@pytest.mark.asyncio
async def test_a_binding_can_never_shadow_the_global_issuer(monkeypatch):
    """Even if a binding row for the global issuer existed, the global verifier decides."""
    from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt_for_agent

    key_global, jwks_global = _mk_rsa_jwks("g")
    key_attacker, jwks_attacker = _mk_rsa_jwks("a")
    monkeypatch.setenv("AGENT_USER_JWKS_JSON", json.dumps(jwks_global))
    monkeypatch.setenv("AGENT_USER_JWT_ISSUER", "https://global.example")
    monkeypatch.setenv("AGENT_USER_JWT_AUDIENCE", "pivota")
    import services.agent_user_jwt as mod
    mod._JWKS_CACHE.clear()
    _install_jwks_http(monkeypatch, {"https://evil.example/jwks": jwks_attacker})
    _install_bindings(monkeypatch, {
        ("agent_evil", "https://global.example"): {
            "issuer": "https://global.example", "jwks_uri": "https://evil.example/jwks", "audience": "pivota", "algs": ["RS256"],
            "authorized_party": None, "required_scopes": None,
        },
    })
    forged = _token(key_attacker, "a", iss="https://global.example", sub="victim", aud="pivota")
    with pytest.raises(AgentUserJwtError):
        await verify_agent_user_jwt_for_agent(forged, "agent_evil")
    legit = _token(key_global, "g", iss="https://global.example", sub="victim", aud="pivota")
    assert (await verify_agent_user_jwt_for_agent(legit, "agent_evil")).subject == "victim"


def test_registration_race_maps_unique_violation_to_409(monkeypatch):
    client, module = _portal_client(monkeypatch)
    import db.agent_identity_issuers as store

    async def no_owner(_iss):
        return None

    async def ok_jwks(_uri):
        return {"keys": [{"kty": "RSA"}]}

    async def boom(*a, **k):
        raise RuntimeError('duplicate key value violates unique constraint "agent_identity_issuers_active_issuer_uidx"')

    monkeypatch.setattr(store, "find_active_owner", no_owner)
    monkeypatch.setattr(store, "dereference_jwks", ok_jwks)
    monkeypatch.setattr(store, "upsert_issuer", boom)
    resp = client.put("/agents/agent_minds/identity-issuers", json={"issuer": "https://id.minds.example", "jwks_uri": "https://id.minds.example/jwks", "audience": "p"})
    assert resp.status_code == 409


def test_ssrf_resolver_rejects_private_link_local_and_loopback_literals():
    import db.agent_identity_issuers as store

    for bad in ("10.0.0.1", "172.16.5.5", "192.168.1.5", "169.254.169.254", "127.0.0.1", "0.0.0.0", "100.64.0.1", "fc00::1", "::1"):
        assert store.host_resolves_public_only(bad) is False, bad
    assert store.host_resolves_public_only("8.8.8.8") is True
    assert store.host_resolves_public_only("2606:4700:4700::1111") is True


@pytest.mark.asyncio
async def test_jwks_failures_are_one_opaque_message(monkeypatch):
    import db.agent_identity_issuers as store

    class _Resp:
        def __init__(self, status, body=None, bad_json=False):
            self.status_code = status
            self._body, self._bad = body, bad_json

        def json(self):
            if self._bad:
                raise ValueError("nope")
            return self._body

    outcomes = iter([_Resp(404), _Resp(200, bad_json=True), _Resp(200, {"keys": [{"kty": "oct"}]})])

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return next(outcomes)

    monkeypatch.setattr(store.httpx, "AsyncClient", _Client)
    msgs = set()
    for _ in range(3):
        with pytest.raises(store.IssuerValidationError) as ei:
            await store.dereference_jwks("https://x/j")
        msgs.add(str(ei.value))
    assert msgs == {store.JWKS_OPAQUE_FAILURE}


@pytest.mark.asyncio
async def test_federated_path_requires_iat_sub_and_enforces_max_age(monkeypatch):
    from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt_for_agent

    for var in ("AGENT_USER_JWKS_JSON", "AGENT_USER_JWT_ISSUER", "AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    key, jwks = _mk_rsa_jwks("k")
    _install_jwks_http(monkeypatch, {"https://idp.example/jwks": jwks})
    _install_bindings(monkeypatch, {("agent_a", "https://idp.example"): {
        "issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks", "audience": "p", "algs": ["RS256"],
        "authorized_party": None, "required_scopes": None}})
    now = int(time.time())
    no_sub = jwt.encode({"iss": "https://idp.example", "aud": "p", "iat": now, "exp": now + 60}, key=key, algorithm="RS256", headers={"kid": "k"})
    with pytest.raises(AgentUserJwtError):
        await verify_agent_user_jwt_for_agent(no_sub, "agent_a")
    ancient = jwt.encode({"iss": "https://idp.example", "sub": "s", "aud": "p", "iat": now - 100000, "exp": now + 60}, key=key, algorithm="RS256", headers={"kid": "k"})
    with pytest.raises(AgentUserJwtError):
        await verify_agent_user_jwt_for_agent(ancient, "agent_a")
    fresh = _token(key, "k", iss="https://idp.example", sub="s", aud="p")
    assert (await verify_agent_user_jwt_for_agent(fresh, "agent_a")).subject == "s"


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_at_most_once_per_window(monkeypatch):
    from services.agent_user_jwt import AgentUserJwtError, verify_agent_user_jwt_for_agent

    for var in ("AGENT_USER_JWKS_JSON", "AGENT_USER_JWT_ISSUER", "AGENT_USER_JWT_ISSUERS", "AGENT_USER_JWT_AUDIENCE"):
        monkeypatch.delenv(var, raising=False)
    key, jwks = _mk_rsa_jwks("real")
    calls = []
    _install_jwks_http(monkeypatch, {"https://idp.example/jwks": jwks}, calls=calls)
    _install_bindings(monkeypatch, {("agent_a", "https://idp.example"): {
        "issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks", "audience": "p", "algs": ["RS256"],
        "authorized_party": None, "required_scopes": None}})
    for i in range(5):
        bogus = _token(key, f"random-{i}", iss="https://idp.example", sub="s", aud="p")
        with pytest.raises(AgentUserJwtError):
            await verify_agent_user_jwt_for_agent(bogus, "agent_a")
    # first call: initial fetch + one refresh; the next four: cache hit + refresh capped ⇒ no new fetches
    assert len(calls) == 2, calls
