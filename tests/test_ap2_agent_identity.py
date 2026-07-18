"""
ConsentService.get_agent_identity (ADR-012): the DID (agents.did) is the source
of record for an agent's AP2 identity, with the legacy DID-in-public_key and the
raw-PEM pilot key as fallbacks. This is the seam that moves identity off the
overloaded public_key column without breaking existing agents.
"""
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.consent_service import consent_service  # noqa: E402

_DID = "did:key:z6MkPreferred"
_LEGACY_DID = "did:web:legacy.example.com"
_PEM = "-----BEGIN PUBLIC KEY-----\nMFkw...\n-----END PUBLIC KEY-----\n"


def _patch_row(monkeypatch, row):
    async def fake_fetch_one(query, values=None):
        assert "SELECT did, public_key FROM agents" in query
        return row

    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)


async def test_prefers_agents_did(monkeypatch):
    # agents.did set → used even when public_key also holds something.
    _patch_row(monkeypatch, {"did": _DID, "public_key": _PEM})
    assert await consent_service.get_agent_identity("agent_1") == _DID


async def test_falls_back_to_did_in_public_key(monkeypatch):
    # Legacy placement: no agents.did, DID stored in public_key.
    _patch_row(monkeypatch, {"did": None, "public_key": _LEGACY_DID})
    assert await consent_service.get_agent_identity("agent_1") == _LEGACY_DID


async def test_falls_back_to_raw_pem(monkeypatch):
    _patch_row(monkeypatch, {"did": None, "public_key": _PEM})
    assert await consent_service.get_agent_identity("agent_1") == _PEM


async def test_non_did_in_did_column_is_ignored(monkeypatch):
    # A malformed agents.did value must not be treated as a DID; fall back.
    _patch_row(monkeypatch, {"did": "not-a-did", "public_key": _PEM})
    assert await consent_service.get_agent_identity("agent_1") == _PEM


async def test_missing_did_column_falls_back(monkeypatch):
    # Back-compat: a row shape without a `did` key (e.g. an older fake) → fallback.
    _patch_row(monkeypatch, {"public_key": _PEM})
    assert await consent_service.get_agent_identity("agent_1") == _PEM


async def test_neither_registered_returns_none(monkeypatch):
    _patch_row(monkeypatch, {"did": None, "public_key": None})
    assert await consent_service.get_agent_identity("agent_1") is None


async def test_unknown_agent_raises_lookup(monkeypatch):
    _patch_row(monkeypatch, None)
    with pytest.raises(LookupError):
        await consent_service.get_agent_identity("agent_nobody")


# --------------------------------------------------------------------------- #
# end-to-end: agents.did (with public_key NULL) drives the grant flow
# --------------------------------------------------------------------------- #

def test_grant_resolves_identity_from_agents_did(monkeypatch):
    import base64
    import json

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    def _b58(data: bytes) -> str:
        num = int.from_bytes(data, "big")
        out = b""
        while num > 0:
            num, r = divmod(num, 58)
            out = bytes([_B58[r]]) + out
        return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode()

    priv = ec.generate_private_key(ec.SECP256R1())
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    did = "did:key:z" + _b58(b"\x80\x24" + comp)
    AGENT = "agent_internal_x"
    consents, used = [], set()

    async def fetch_one(query, values=None):
        if "SELECT did, public_key FROM agents" in query:
            # DID lives ONLY in agents.did; public_key is NULL.
            return {"did": did, "public_key": None} if values["agent_id"] == AGENT else None
        if "FROM nonce_tracker" in query:
            return {"nonce": values["nonce"]} if values["nonce"] in used else None
        return None

    async def execute(query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            used.add(values["nonce"])
        elif "INSERT INTO agent_consents" in query:
            consents.append(values)

    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fetch_one)
    monkeypatch.setattr(database, "execute", execute)

    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    client = TestClient(app)

    scope, nonce = ["read"], "id-nonce-1"
    payload = {"agent_id": AGENT, "scope": scope, "duration_hours": 24, "nonce": nonce}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    sig = base64.b64encode(priv.sign(canonical.encode(), ec.ECDSA(hashes.SHA256()))).decode()

    res = client.post(
        "/ap2/consent/grant",
        json={"agent_id": AGENT, "scope": scope, "duration_hours": 24},
        headers={"X-AP2-Signature": sig, "X-AP2-Nonce": nonce},
    )
    assert res.status_code == 200, res.text
    assert len(consents) == 1
