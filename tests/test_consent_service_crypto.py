"""
Signature-verification path of ConsentService.create_consent.

Regression for the lazy import that pointed at the nonexistent
pivota_infra.services.crypto_service — any signed consent request with a
public_key raised ModuleNotFoundError instead of verifying.
"""
import base64
import json

import pytest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from services.consent_service import ConsentService


AGENT_ID = "agent_test_crypto"
SCOPE = ["create_payment"]
DURATION_HOURS = 24
NONCE = "nonce-consent-crypto-test"


def _keypair_and_signature():
    """Generate a P-256 keypair and sign the canonical consent payload."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    payload = {
        "agent_id": AGENT_ID,
        "scope": SCOPE,
        "duration_hours": DURATION_HOURS,
        "nonce": NONCE,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = base64.b64encode(
        private_key.sign(canonical.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    ).decode("utf-8")
    return public_key_pem, signature


async def test_create_consent_with_valid_signature():
    public_key_pem, signature = _keypair_and_signature()

    with patch("services.consent_service.database.fetch_one", new=AsyncMock(return_value=None)), patch(
        "services.consent_service.database.execute", new=AsyncMock()
    ) as mock_execute:
        result = await ConsentService().create_consent(
            agent_id=AGENT_ID,
            scope=SCOPE,
            duration_hours=DURATION_HOURS,
            signature=signature,
            nonce=NONCE,
            public_key=public_key_pem,
        )

    assert result["agent_id"] == AGENT_ID
    assert result["scope"] == SCOPE
    assert result["token"].startswith("consent_")
    # nonce recorded + consent inserted
    assert mock_execute.await_count == 2


async def test_create_consent_rejects_invalid_signature():
    public_key_pem, signature = _keypair_and_signature()
    # Signature from a different key must not verify against this public key
    other_key = ec.generate_private_key(ec.SECP256R1())
    forged = base64.b64encode(
        other_key.sign(b"unrelated message", ec.ECDSA(hashes.SHA256()))
    ).decode("utf-8")

    with patch("services.consent_service.database.fetch_one", new=AsyncMock(return_value=None)), patch(
        "services.consent_service.database.execute", new=AsyncMock()
    ) as mock_execute:
        with pytest.raises(ValueError, match="Invalid signature"):
            await ConsentService().create_consent(
                agent_id=AGENT_ID,
                scope=SCOPE,
                duration_hours=DURATION_HOURS,
                signature=forged,
                nonce=NONCE,
                public_key=public_key_pem,
            )

    # Rejected before any nonce/consent writes
    assert mock_execute.await_count == 0
