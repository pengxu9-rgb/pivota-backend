"""
Tests for services/ap2_did_web.py — did:web resolution (ADR-012, second slice).

No real network: resolution uses an injected fetcher, and the SSRF guard is
tested with IP literals (getaddrinfo on a literal does not hit DNS). One
route-level grant test patches the module fetcher to prove end-to-end did:web
identity through POST /ap2/consent/grant.
"""
import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import ap2_did_web  # noqa: E402
from services.ap2_did_web import (  # noqa: E402
    did_web_to_url,
    is_did_web,
    resolve_did_web,
    _assert_public_host,
)
from services.crypto_service import crypto_service  # noqa: E402

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _clear_cache():
    ap2_did_web._clear_cache()
    yield
    ap2_did_web._clear_cache()


# --------------------------------------------------------------------------- #
# helpers — build DID documents from generated keys
# --------------------------------------------------------------------------- #

def _b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode("ascii")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _multibase_ed25519(priv) -> str:
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "z" + _b58encode(b"\xed\x01" + raw)


def _multibase_p256(priv) -> str:
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return "z" + _b58encode(b"\x80\x24" + comp)


def _jwk_p256(priv) -> dict:
    nums = priv.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(nums.x.to_bytes(32, "big")),
        "y": _b64url(nums.y.to_bytes(32, "big")),
    }


def _jwk_ed25519(priv) -> dict:
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(raw)}


def _doc(did, vm_id, key_field, key_value):
    return {
        "id": did,
        "verificationMethod": [
            {"id": vm_id, "type": "X", "controller": did, key_field: key_value}
        ],
    }


def _fetcher_returning(doc):
    async def _f(url):
        return doc
    return _f


def _canonical(agent_id, scope, duration_hours, nonce) -> bytes:
    payload = {"agent_id": agent_id, "scope": scope,
               "duration_hours": duration_hours, "nonce": nonce}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


# --------------------------------------------------------------------------- #
# URL derivation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("did,expected", [
    ("did:web:example.com", "https://example.com/.well-known/did.json"),
    ("did:web:example.com:agents:bot1", "https://example.com/agents/bot1/did.json"),
    ("did:web:example.com%3A3000", "https://example.com:3000/.well-known/did.json"),
])
def test_did_web_to_url(did, expected):
    assert did_web_to_url(did) == expected


def test_is_did_web():
    assert is_did_web("did:web:example.com")
    assert not is_did_web("did:key:z6Mk")
    assert not is_did_web("https://example.com")


# --------------------------------------------------------------------------- #
# SSRF guard (IP literals — no DNS)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("host", [
    "localhost", "127.0.0.1", "10.0.0.5", "192.168.1.10",
    "169.254.169.254",  # cloud metadata
    "0.0.0.0",
])
def test_ssrf_guard_blocks_non_public(host):
    with pytest.raises(ValueError):
        _assert_public_host(host)


def test_ssrf_guard_allows_public_ip_literal():
    _assert_public_host("8.8.8.8")  # global; should not raise


# --------------------------------------------------------------------------- #
# resolution via injected fetcher + real signature verification
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_resolve_multibase_p256_and_verify():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:example.com"
    doc = _doc(did, did + "#k1", "publicKeyMultibase", _multibase_p256(priv))

    pem, alg = await resolve_did_web(did, fetcher=_fetcher_returning(doc))

    assert alg == "ES256"
    payload = {"agent_id": did, "scope": ["read"], "nonce": "n1"}
    msg = crypto_service.canonicalize_json(payload).encode()
    sig = base64.b64encode(priv.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()
    assert crypto_service.verify_agent_signature(pem, sig, payload, "ES256")


@pytest.mark.asyncio
async def test_resolve_jwk_ed25519_and_verify():
    priv = ed25519.Ed25519PrivateKey.generate()
    did = "did:web:example.com:agents:bot"
    doc = _doc(did, did + "#k1", "publicKeyJwk", _jwk_ed25519(priv))

    pem, alg = await resolve_did_web(did, fetcher=_fetcher_returning(doc))

    assert alg == "Ed25519"
    payload = {"agent_id": did, "scope": ["read"], "nonce": "n1"}
    msg = crypto_service.canonicalize_json(payload).encode()
    sig = base64.b64encode(priv.sign(msg)).decode()
    assert crypto_service.verify_agent_signature(pem, sig, payload, "Ed25519")


@pytest.mark.asyncio
async def test_resolve_selects_by_kid():
    p1 = ec.generate_private_key(ec.SECP256R1())
    p2 = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:example.com"
    doc = {
        "id": did,
        "verificationMethod": [
            {"id": did + "#k1", "publicKeyMultibase": _multibase_p256(p1)},
            {"id": did + "#k2", "publicKeyMultibase": _multibase_p256(p2)},
        ],
    }
    pem, _ = await resolve_did_web(did, kid="k2", fetcher=_fetcher_returning(doc))

    payload = {"agent_id": did, "scope": ["read"], "nonce": "n1"}
    msg = crypto_service.canonicalize_json(payload).encode()
    sig = base64.b64encode(p2.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()
    assert crypto_service.verify_agent_signature(pem, sig, payload, "ES256")
    # k1's signature must NOT verify against the k2-selected key.
    sig1 = base64.b64encode(p1.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()
    assert not crypto_service.verify_agent_signature(pem, sig1, payload, "ES256")


@pytest.mark.asyncio
async def test_resolution_is_cached():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:example.com"
    doc = _doc(did, did + "#k1", "publicKeyMultibase", _multibase_p256(priv))

    calls = {"n": 0}

    async def counting_fetch(url):
        calls["n"] += 1
        return doc

    await resolve_did_web(did, fetcher=counting_fetch)
    await resolve_did_web(did, fetcher=counting_fetch)
    assert calls["n"] == 1  # second call served from cache


@pytest.mark.asyncio
async def test_doc_id_mismatch_fails_closed():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:example.com"
    doc = _doc("did:web:evil.example", did + "#k1",
               "publicKeyMultibase", _multibase_p256(priv))
    with pytest.raises(ValueError):
        await resolve_did_web(did, fetcher=_fetcher_returning(doc))


@pytest.mark.asyncio
async def test_no_verification_method_fails_closed():
    did = "did:web:example.com"
    with pytest.raises(ValueError):
        await resolve_did_web(did, fetcher=_fetcher_returning({"id": did}))


@pytest.mark.asyncio
async def test_unsupported_jwk_fails_closed():
    did = "did:web:example.com"
    doc = _doc(did, did + "#k1", "publicKeyJwk", {"kty": "RSA", "n": "x", "e": "AQAB"})
    with pytest.raises(ValueError):
        await resolve_did_web(did, fetcher=_fetcher_returning(doc))


# --------------------------------------------------------------------------- #
# end-to-end grant via did:web (module fetcher patched)
# --------------------------------------------------------------------------- #

class _FakeDB:
    def __init__(self, agents):
        self.agents = agents
        self.used_nonces = set()
        self.consents = []

    async def fetch_one(self, query, values=None):
        if "FROM agents" in query:
            aid = values["agent_id"]
            return {"public_key": self.agents[aid]} if aid in self.agents else None
        if "FROM nonce_tracker" in query:
            n = values["nonce"]
            return {"nonce": n} if n in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])
        elif "INSERT INTO agent_consents" in query:
            self.consents.append(values)


def test_grant_with_did_web_identity(monkeypatch):
    priv = ec.generate_private_key(ec.SECP256R1())
    did = "did:web:agents.example.com:bot1"
    doc = _doc(did, did + "#k1", "publicKeyMultibase", _multibase_p256(priv))

    async def fake_default_fetch(url):
        return doc

    ap2_did_web._clear_cache()
    monkeypatch.setattr(ap2_did_web, "_default_fetch", fake_default_fetch)

    fake = _FakeDB({did: did})  # agents.public_key holds the did:web string
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)

    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    client = TestClient(app)

    scope = ["read", "create_payment"]
    sig = base64.b64encode(
        priv.sign(_canonical(did, scope, 24, "nweb1"), ec.ECDSA(hashes.SHA256()))
    ).decode()

    res = client.post(
        "/ap2/consent/grant",
        json={"agent_id": did, "scope": scope, "duration_hours": 24},
        headers={"X-AP2-Signature": sig, "X-AP2-Nonce": "nweb1"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["consent_token"].startswith("consent_")
    assert len(fake.consents) == 1
