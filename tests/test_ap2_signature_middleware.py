"""
Dependency-level coverage for middleware.ap2_security.verify_ap2_signature.

verify_ap2_signature is the generic per-route AP2 signature guard. It must
enforce the SAME canonical signed-payload contract as the consent-grant route
(services/ap2_signing.py): canonical JSON of the request body with `nonce`
injected from the header and `algorithm` excluded, verified against the signing
agent's REGISTERED public key (resolved via the consent token). It must also
verify the signature BEFORE consuming the nonce.
"""

import base64
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


AGENT_ID = "agent_mw_test"
CONSENT_TOKEN = "consent_mw_token"


def _generate_es256_keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, public_pem


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign(private_key, payload: dict) -> str:
    signature = private_key.sign(_canonical(payload).encode("utf-8"),
                                 ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode("utf-8")


class FakeDB:
    """
    Dispatches verify_consent (agent_consents), get_agent_public_key (agents),
    and the nonce_tracker replay check/insert that verify_ap2_signature issues.
    """

    def __init__(self, *, agents, consents):
        self.agents = agents            # agent_id -> public_key (or None)
        self.consents = consents        # consent_id -> consent row dict
        self.used_nonces = set()

    async def fetch_one(self, query, values=None):
        if "FROM agent_consents" in query:
            return self.consents.get(values["consent_id"])
        if "FROM agents" in query:
            agent_id = values["agent_id"]
            if agent_id not in self.agents:
                return None
            return {"public_key": self.agents[agent_id]}
        if "FROM nonce_tracker" in query:
            nonce = values["nonce"]
            return {"present": 1} if nonce in self.used_nonces else None
        return None

    async def execute(self, query, values=None):
        if "INSERT INTO nonce_tracker" in query:
            self.used_nonces.add(values["nonce"])


def _active_consent_row(agent_id=AGENT_ID, consent_id=CONSENT_TOKEN):
    return {
        "consent_id": consent_id,
        "agent_id": agent_id,
        "scope": json.dumps({"actions": ["read"]}),
        "status": "active",
        "expires_at": datetime.utcnow() + timedelta(hours=1),
    }


@pytest.fixture
def keypair():
    return _generate_es256_keypair()


@pytest.fixture
def fake_db(keypair, monkeypatch):
    _, public_pem = keypair
    fake = FakeDB(
        agents={AGENT_ID: public_pem},
        consents={CONSENT_TOKEN: _active_consent_row()},
    )
    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(database, "execute", fake.execute)
    return fake


@pytest.fixture
def client():
    from middleware.ap2_security import verify_ap2_signature

    app = FastAPI()

    @app.post("/ap2/protected")
    async def protected(ok: bool = Depends(verify_ap2_signature)):
        return {"ok": ok}

    return TestClient(app)


def _post(client, *, body, signature, nonce, consent=CONSENT_TOKEN):
    headers = {}
    if signature is not None:
        headers["X-AP2-Signature"] = signature
    if nonce is not None:
        headers["X-AP2-Nonce"] = nonce
    if consent is not None:
        headers["X-Agent-Consent"] = consent
    return client.post("/ap2/protected", json=body, headers=headers)


def test_valid_signature_passes(client, fake_db, keypair):
    private_key, _ = keypair
    body = {"amount": 10, "currency": "USD"}
    payload = {**body, "nonce": "mw-001"}
    signature = _sign(private_key, payload)

    res = _post(client, body=body, signature=signature, nonce="mw-001")

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert "mw-001" in fake_db.used_nonces  # consumed on success


def test_algorithm_excluded_from_signed_payload(client, fake_db, keypair):
    """
    The contract fix: `algorithm` is transport metadata and must be dropped
    before signing. A signature over {body + nonce} WITHOUT algorithm verifies
    even when the body advertises `algorithm`.
    """
    private_key, _ = keypair
    body = {"amount": 10, "algorithm": "ES256"}
    payload = {"amount": 10, "nonce": "mw-alg"}  # algorithm intentionally excluded
    signature = _sign(private_key, payload)

    res = _post(client, body=body, signature=signature, nonce="mw-alg")

    assert res.status_code == 200


def test_signing_algorithm_field_breaks_verification_401(client, fake_db, keypair):
    """
    Conversely, a client that WRONGLY includes `algorithm` in the signed bytes
    does not match the canonical payload and is rejected -- proving the two
    paths agree on one shape.
    """
    private_key, _ = keypair
    body = {"amount": 10, "algorithm": "ES256"}
    payload = {**body, "nonce": "mw-alg2"}  # includes algorithm -> wrong shape
    signature = _sign(private_key, payload)

    res = _post(client, body=body, signature=signature, nonce="mw-alg2")

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid signature"


def test_tampered_body_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    signed_payload = {"amount": 10, "currency": "USD", "nonce": "mw-002"}
    signature = _sign(private_key, signed_payload)
    # Send a different amount than what was signed.
    res = _post(client, body={"amount": 999, "currency": "USD"},
                signature=signature, nonce="mw-002")

    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid signature"
    assert "mw-002" not in fake_db.used_nonces  # bad sig must NOT consume nonce


def test_wrong_key_rejected_401(client, fake_db):
    other_key, _ = _generate_es256_keypair()
    body = {"amount": 10}
    signature = _sign(other_key, {**body, "nonce": "mw-003"})

    res = _post(client, body=body, signature=signature, nonce="mw-003")

    assert res.status_code == 401


def test_replayed_nonce_rejected_409(client, fake_db, keypair):
    private_key, _ = keypair
    fake_db.used_nonces.add("mw-used")
    body = {"amount": 10}
    signature = _sign(private_key, {**body, "nonce": "mw-used"})

    res = _post(client, body=body, signature=signature, nonce="mw-used")

    assert res.status_code == 409
    assert "replay" in res.json()["detail"].lower()


def test_missing_signature_rejected_401(client, fake_db):
    res = _post(client, body={"amount": 10}, signature=None, nonce="mw-004")
    assert res.status_code == 401
    assert res.json()["detail"] == "Missing request signature"


def test_missing_nonce_rejected_400(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign(private_key, {"amount": 10, "nonce": "x"})
    res = _post(client, body={"amount": 10}, signature=signature, nonce=None)
    assert res.status_code == 400


def test_missing_consent_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign(private_key, {"amount": 10, "nonce": "mw-005"})
    res = _post(client, body={"amount": 10}, signature=signature,
                nonce="mw-005", consent=None)
    assert res.status_code == 401
    assert res.json()["detail"] == "Missing agent consent"


def test_invalid_consent_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    signature = _sign(private_key, {"amount": 10, "nonce": "mw-006"})
    res = _post(client, body={"amount": 10}, signature=signature,
                nonce="mw-006", consent="consent_does_not_exist")
    assert res.status_code == 401
    # verify_ap2_consent surfaces the ValueError from verify_consent verbatim.
    assert res.json()["detail"] == "Consent not found or inactive"


def test_unsupported_algorithm_rejected_400(client, fake_db, keypair):
    private_key, _ = keypair
    body = {"amount": 10, "algorithm": "RS256"}
    signature = _sign(private_key, {"amount": 10, "nonce": "mw-007"})
    res = _post(client, body=body, signature=signature, nonce="mw-007")
    assert res.status_code == 400
    assert "Unsupported algorithm" in res.json()["detail"]


def test_keyless_agent_rejected_401(client, fake_db, keypair):
    private_key, _ = keypair
    fake_db.agents[AGENT_ID] = None  # registered agent, no key on file
    signature = _sign(private_key, {"amount": 10, "nonce": "mw-008"})
    res = _post(client, body={"amount": 10}, signature=signature, nonce="mw-008")
    assert res.status_code == 401
    assert "no registered public key" in res.json()["detail"]
