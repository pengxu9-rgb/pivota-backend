"""
did:key resolution for AP2 agent identity (issue #1442, ADR-012).

First (self-sovereign) slice of the DID/VC identity model: resolve a `did:key`
DID to the agent's verification public key **offline** — no network, fully
deterministic. Trust roots in the DID itself (the public key *is* the
identifier), so there is no platform-issued secret and no key upload — which is
the whole point of moving off API-key rooting.

Supported key types (matching services/crypto_service.verify_agent_signature):
- Ed25519  (multicodec 0xed)   -> algorithm "Ed25519"
- P-256    (multicodec 0x1200) -> algorithm "ES256"

did:key format: ``did:key:z<base58btc( <multicodec-varint> <public-key-bytes> )>``
where the leading ``z`` is the multibase prefix for base58btc.
See https://w3c-ccg.github.io/did-method-key/.

base58btc and the multicodec varint are decoded inline to avoid a new
third-party dependency.
"""
from __future__ import annotations

from typing import Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

# Multicodec code points for the key types we support (decoded from the
# unsigned-varint prefix; 0xed -> varint 0xed 0x01, 0x1200 -> varint 0x80 0x24).
_MULTICODEC_ED25519 = 0xED
_MULTICODEC_P256 = 0x1200

# base58btc (Bitcoin) alphabet.
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_DID_KEY_PREFIX = "did:key:"


def is_did_key(value: object) -> bool:
    """True iff ``value`` is a base58btc did:key string."""
    return isinstance(value, str) and value.startswith(_DID_KEY_PREFIX + "z")


def _b58decode(s: str) -> bytes:
    """Decode a base58btc (Bitcoin alphabet) string to bytes."""
    num = 0
    for ch in s:
        idx = _B58_ALPHABET.find(ch.encode("ascii", "ignore") or b"\xff")
        if idx == -1:
            raise ValueError(f"invalid base58 character: {ch!r}")
        num = num * 58 + idx
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Each leading '1' in base58 encodes one leading zero byte.
    n_leading_zero = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_leading_zero + body


def _read_varint(data: bytes) -> Tuple[int, int]:
    """Read an unsigned LEB128 varint; return (value, bytes_consumed)."""
    result = 0
    shift = 0
    for i, byte in enumerate(data):
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, i + 1
        shift += 7
    raise ValueError("truncated multicodec varint")


def decode_multibase_base58btc(multibase: str) -> bytes:
    """Decode a base58btc multibase string (leading ``z``) to raw bytes."""
    if not multibase.startswith("z"):
        raise ValueError("expected base58btc multibase ('z' prefix)")
    return _b58decode(multibase[1:])


def multicodec_bytes_to_pem(data: bytes) -> Tuple[str, str]:
    """
    Decode ``<multicodec-varint><public-key-bytes>`` to ``(pem, algorithm)``.

    Shared by did:key (its method-specific id) and did:web (a verification
    method's ``publicKeyMultibase``) — both use the identical encoding.

    Raises ValueError on empty, malformed, or unsupported input.
    """
    if not data:
        raise ValueError("empty multicodec payload")

    code, consumed = _read_varint(data)
    key_bytes = data[consumed:]

    if code == _MULTICODEC_ED25519:
        if len(key_bytes) != 32:
            raise ValueError(f"Ed25519 key must be 32 bytes, got {len(key_bytes)}")
        return _to_pem(ed25519.Ed25519PublicKey.from_public_bytes(key_bytes)), "Ed25519"

    if code == _MULTICODEC_P256:
        # Compressed EC point (33 bytes for P-256).
        try:
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key_bytes)
        except Exception as exc:  # invalid/short point, wrong length, etc.
            raise ValueError(f"invalid P-256 point: {exc}")
        return _to_pem(key), "ES256"

    raise ValueError(f"unsupported multicodec: 0x{code:x}")


def resolve_did_key(did: str) -> Tuple[str, str]:
    """
    Resolve a did:key to ``(public_key_pem, algorithm)``.

    ``public_key_pem`` is SubjectPublicKeyInfo PEM, accepted by
    ``crypto_service.verify_agent_signature`` for both ES256 and Ed25519.
    ``algorithm`` is derived from the DID's multicodec and is authoritative
    (the DID, not the caller, states the key type).

    Raises ValueError on a malformed or unsupported DID.
    """
    if not is_did_key(did):
        raise ValueError("not a did:key")

    decoded = decode_multibase_base58btc(did[len(_DID_KEY_PREFIX):])
    return multicodec_bytes_to_pem(decoded)


def _to_pem(key) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
