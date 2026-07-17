"""
crypto_service.verify_agent_signature must accept ES256 signatures in BOTH
encodings: ASN.1 DER (OpenSSL / this service's sign_receipt) and raw r||s /
IEEE P1363 (JOSE, WebCrypto, standards-based DID/VC agents — the callers the
AP2 signed rail targets). Previously only DER verified, so a real external
agent's raw signature was silently rejected.
"""
import base64
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.crypto_service import crypto_service  # noqa: E402

PAYLOAD = {"agent_id": "a1", "scope": ["read"], "nonce": "n1"}


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pem


def _der_b64(priv, payload):
    msg = crypto_service.canonicalize_json(payload).encode()
    return base64.b64encode(priv.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()


def _raw_b64(priv, payload):
    """Sign then re-encode the DER signature as raw r||s (P1363, 64 bytes)."""
    msg = crypto_service.canonicalize_json(payload).encode()
    r, s = decode_dss_signature(priv.sign(msg, ec.ECDSA(hashes.SHA256())))
    return base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()


def test_der_signature_still_verifies():  # regression
    priv, pem = _keypair()
    assert crypto_service.verify_agent_signature(pem, _der_b64(priv, PAYLOAD), PAYLOAD, "ES256")


def test_raw_p1363_signature_verifies():  # the fix
    priv, pem = _keypair()
    sig = _raw_b64(priv, PAYLOAD)
    assert len(base64.b64decode(sig)) == 64  # confirm it really is the raw form
    assert crypto_service.verify_agent_signature(pem, sig, PAYLOAD, "ES256")


def test_raw_signature_wrong_key_rejected():
    priv, _ = _keypair()
    _, other_pem = _keypair()
    assert not crypto_service.verify_agent_signature(other_pem, _raw_b64(priv, PAYLOAD), PAYLOAD, "ES256")


def test_raw_signature_tampered_payload_rejected():
    priv, pem = _keypair()
    sig = _raw_b64(priv, PAYLOAD)
    tampered = {**PAYLOAD, "scope": ["read", "write"]}
    assert not crypto_service.verify_agent_signature(pem, sig, tampered, "ES256")


def test_garbage_64_byte_signature_rejected():
    _, pem = _keypair()
    junk = base64.b64encode(b"\x01" * 64).decode()
    assert not crypto_service.verify_agent_signature(pem, junk, PAYLOAD, "ES256")


def test_grant_accepts_raw_es256_signature(monkeypatch):
    """End-to-end: POST /ap2/consent/grant with a raw (JOSE-form) ES256 sig."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    priv, pem = _keypair()
    AGENT = "agent_raw"

    class _DB:
        def __init__(self):
            self.nonces = set()
            self.consents = []

        async def fetch_one(self, query, values=None):
            if "FROM agents" in query:
                return {"public_key": pem} if values["agent_id"] == AGENT else None
            if "FROM nonce_tracker" in query:
                return {"nonce": values["nonce"]} if values["nonce"] in self.nonces else None
            return None

        async def execute(self, query, values=None):
            if "INSERT INTO nonce_tracker" in query:
                self.nonces.add(values["nonce"])
            elif "INSERT INTO agent_consents" in query:
                self.consents.append(values)

    db = _DB()
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", db.fetch_one)
    monkeypatch.setattr(database, "execute", db.execute)

    from routes.ap2_routes import router as ap2_router
    app = FastAPI()
    app.include_router(ap2_router)
    client = TestClient(app)

    scope = ["read", "create_payment"]
    nonce = "raw-grant-nonce"
    signed = {"agent_id": AGENT, "scope": scope, "duration_hours": 24, "nonce": nonce}
    raw_sig = _raw_b64(priv, signed)

    res = client.post(
        "/ap2/consent/grant",
        json={"agent_id": AGENT, "scope": scope, "duration_hours": 24},
        headers={"X-AP2-Signature": raw_sig, "X-AP2-Nonce": nonce},
    )
    assert res.status_code == 200, res.text
    assert res.json()["consent_token"].startswith("consent_")
    assert len(db.consents) == 1
