"""
Unit tests for services/ap2_did.py — did:key resolution (issue #1442, ADR-012).

Round-trips a generated keypair through did:key encode -> resolve, and proves the
resolved PEM actually verifies a real signature via the SAME code path the AP2
grant flow uses (crypto_service.verify_agent_signature). No network, no DB.
"""
import base64
import sys
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.ap2_did import is_did_key, resolve_did_key  # noqa: E402
from services.crypto_service import crypto_service  # noqa: E402

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_MC_ED25519 = b"\xed\x01"      # multicodec varint for ed25519-pub
_MC_P256 = b"\x80\x24"          # multicodec varint for p256-pub (0x1200)


def _b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58_ALPHABET[rem]]) + out
    n_leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * n_leading + out.decode("ascii")


def _did_key(multicodec: bytes, key_bytes: bytes) -> str:
    return "did:key:z" + _b58encode(multicodec + key_bytes)


def _ed25519_did_key():
    priv = ed25519.Ed25519PrivateKey.generate()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, _did_key(_MC_ED25519, raw)


def _p256_did_key():
    priv = ec.generate_private_key(ec.SECP256R1())
    compressed = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return priv, _did_key(_MC_P256, compressed)


def _payload_and_sig_ed25519(priv):
    payload = {"agent_id": "did-agent", "scope": ["read"], "nonce": "n1"}
    message = crypto_service.canonicalize_json(payload).encode("utf-8")
    sig = base64.b64encode(priv.sign(message)).decode("ascii")
    return payload, sig


def _payload_and_sig_es256(priv):
    payload = {"agent_id": "did-agent", "scope": ["read"], "nonce": "n1"}
    message = crypto_service.canonicalize_json(payload).encode("utf-8")
    sig = base64.b64encode(
        priv.sign(message, ec.ECDSA(hashes.SHA256()))
    ).decode("ascii")
    return payload, sig


# --------------------------------------------------------------------------- #
# is_did_key
# --------------------------------------------------------------------------- #

def test_is_did_key_true():
    _, did = _ed25519_did_key()
    assert is_did_key(did)


@pytest.mark.parametrize("value", [
    "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----",
    "did:web:example.com",
    "did:key:Qmsomething",   # not base58btc multibase ('z')
    "",
    None,
    12345,
])
def test_is_did_key_false(value):
    assert not is_did_key(value)


# --------------------------------------------------------------------------- #
# resolve + real signature verification (the point of the module)
# --------------------------------------------------------------------------- #

def test_ed25519_did_key_resolves_and_verifies():
    priv, did = _ed25519_did_key()
    pem, algorithm = resolve_did_key(did)

    assert algorithm == "Ed25519"
    # PEM must parse as an Ed25519 public key.
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    assert isinstance(key, ed25519.Ed25519PublicKey)

    payload, sig = _payload_and_sig_ed25519(priv)
    assert crypto_service.verify_agent_signature(pem, sig, payload, "Ed25519") is True


def test_p256_did_key_resolves_and_verifies():
    priv, did = _p256_did_key()
    pem, algorithm = resolve_did_key(did)

    assert algorithm == "ES256"
    key = serialization.load_pem_public_key(pem.encode("utf-8"))
    assert isinstance(key, ec.EllipticCurvePublicKey)

    payload, sig = _payload_and_sig_es256(priv)
    assert crypto_service.verify_agent_signature(pem, sig, payload, "ES256") is True


def test_resolved_key_rejects_tampered_payload():
    priv, did = _p256_did_key()
    pem, _ = resolve_did_key(did)
    payload, sig = _payload_and_sig_es256(priv)

    tampered = {**payload, "scope": ["read", "create_payment"]}
    assert crypto_service.verify_agent_signature(pem, sig, tampered, "ES256") is False


def test_resolution_is_deterministic():
    _, did = _ed25519_did_key()
    assert resolve_did_key(did) == resolve_did_key(did)


# --------------------------------------------------------------------------- #
# malformed / unsupported
# --------------------------------------------------------------------------- #

def test_not_a_did_key_raises():
    with pytest.raises(ValueError):
        resolve_did_key("-----BEGIN PUBLIC KEY-----")


def test_invalid_base58_raises():
    # '0' (zero) is not in the base58btc alphabet.
    with pytest.raises(ValueError):
        resolve_did_key("did:key:z0OIl")


def test_unsupported_multicodec_raises():
    # secp256k1-pub is 0xe7 -> varint 0xe7 0x01; not supported here.
    did = _did_key(b"\xe7\x01", b"\x02" + b"\x11" * 32)
    with pytest.raises(ValueError):
        resolve_did_key(did)


def test_ed25519_wrong_length_raises():
    did = _did_key(_MC_ED25519, b"\x11" * 31)  # 31 bytes, not 32
    with pytest.raises(ValueError):
        resolve_did_key(did)


def test_p256_malformed_point_raises():
    # Truncated compressed point (20 bytes, not the required 33).
    did = _did_key(_MC_P256, b"\x02" + b"\x11" * 19)
    with pytest.raises(ValueError):
        resolve_did_key(did)
