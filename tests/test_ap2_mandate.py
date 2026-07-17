"""
Tests for services/ap2_mandate.py — AP2 verifiable-credential mandates
(ADR-012 authority layer, first slice).

The issuer signs with a real key; its DID is a did:key, so verify_mandate
resolves the issuer offline (no network). Every failure path must raise
MandateError (fail closed).
"""
import base64
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.ap2_mandate import (  # noqa: E402
    INTENT_MANDATE,
    MandateError,
    check_mandate_constraints,
    verify_mandate,
)
from services.crypto_service import crypto_service  # noqa: E402

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
SUBJECT = "did:key:z6MkSubjectAgentPlaceholder"


def _b58(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode("ascii")


def _did_key_es256(priv) -> str:
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return "did:key:z" + _b58(b"\x80\x24" + comp)


def _did_key_ed25519(priv) -> str:
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return "did:key:z" + _b58(b"\xed\x01" + raw)


def _sign(mandate, priv, alg) -> str:
    msg = crypto_service.canonicalize_json(mandate).encode()
    if alg == "ES256":
        sig = priv.sign(msg, ec.ECDSA(hashes.SHA256()))
    else:
        sig = priv.sign(msg)
    return base64.b64encode(sig).decode()


def _intent(issuer_did, **over):
    m = {
        "type": INTENT_MANDATE,
        "issuer": issuer_did,
        "subject": SUBJECT,
        "issued_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        "constraints": {
            "actions": ["create_payment"],
            "max_amount": "100.00",
            "currency": "USD",
            "merchants": ["m_1"],
        },
    }
    m.update(over)
    return m


async def test_valid_intent_mandate_es256():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did)
    sig = _sign(m, priv, "ES256")
    assert await verify_mandate(m, sig, now=NOW) == m


async def test_valid_intent_mandate_ed25519():
    priv = ed25519.Ed25519PrivateKey.generate()
    did = _did_key_ed25519(priv)
    m = _intent(did)
    sig = _sign(m, priv, "Ed25519")
    assert await verify_mandate(m, sig, now=NOW) == m


async def test_tampered_mandate_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did)
    sig = _sign(m, priv, "ES256")
    m["constraints"]["max_amount"] = "999999.00"  # tamper after signing
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW)


async def test_expired_mandate_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did, expires_at=(NOW - timedelta(minutes=1)).isoformat())
    sig = _sign(m, priv, "ES256")
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW)


async def test_not_yet_valid_mandate_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did, issued_at=(NOW + timedelta(hours=1)).isoformat())
    sig = _sign(m, priv, "ES256")
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW)


async def test_subject_binding_mismatch_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did)
    sig = _sign(m, priv, "ES256")
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW, expected_subject="did:key:zSomeoneElse")


async def test_untrusted_issuer_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    m = _intent(did)
    sig = _sign(m, priv, "ES256")
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW, trusted_issuers={"did:key:zOther"})
    # But it passes when the issuer IS trusted.
    assert await verify_mandate(m, sig, now=NOW, trusted_issuers={did}) == m


async def test_non_did_issuer_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    m = _intent("not-a-did")
    sig = _sign(m, priv, "ES256")
    with pytest.raises(MandateError):
        await verify_mandate(m, sig, now=NOW)


async def test_unknown_type_and_missing_fields_rejected():
    priv = ec.generate_private_key(ec.SECP256R1())
    did = _did_key_es256(priv)
    bad_type = _intent(did, type="BogusMandate")
    with pytest.raises(MandateError):
        await verify_mandate(bad_type, _sign(bad_type, priv, "ES256"), now=NOW)
    incomplete = {"type": INTENT_MANDATE, "issuer": did}
    with pytest.raises(MandateError):
        await verify_mandate(incomplete, "AA==", now=NOW)


# ------------------------------- constraints -------------------------------- #

def _c():
    return {"constraints": {"actions": ["create_payment"], "max_amount": "100.00",
                            "currency": "USD", "merchants": ["m_1"]}}


def test_constraints_all_satisfied():
    check_mandate_constraints(_c(), action="create_payment", amount=Decimal("50"),
                              currency="USD", merchant_id="m_1")  # no raise


def test_constraints_action_denied():
    with pytest.raises(MandateError):
        check_mandate_constraints(_c(), action="withdraw")


def test_constraints_amount_exceeded():
    with pytest.raises(MandateError):
        check_mandate_constraints(_c(), amount=Decimal("100.01"))


def test_constraints_currency_mismatch():
    with pytest.raises(MandateError):
        check_mandate_constraints(_c(), currency="EUR")


def test_constraints_merchant_not_allowed():
    with pytest.raises(MandateError):
        check_mandate_constraints(_c(), merchant_id="m_evil")


def test_constraints_absent_are_permissive():
    # A mandate with no constraints block does not constrain anything.
    check_mandate_constraints({}, action="anything", amount=Decimal("1000000"),
                              currency="XYZ", merchant_id="whatever")  # no raise


# ---- fail-closed on malformed input (review B1-B4) -------------------------- #

async def test_non_string_type_is_mandateerror_not_typeerror():
    # B1: an unhashable `type` must raise MandateError, not TypeError, at the
    # pre-signature boundary.
    priv = ec.generate_private_key(ec.SECP256R1())
    m = _intent(_did_key_es256(priv), type=[])
    with pytest.raises(MandateError):
        await verify_mandate(m, "AA==", now=NOW)


async def test_issued_after_expiry_rejected():
    # N3: issued_at strictly after expires_at is structurally invalid.
    priv = ec.generate_private_key(ec.SECP256R1())
    m = _intent(
        _did_key_es256(priv),
        issued_at=(NOW - timedelta(hours=1)).isoformat(),
        expires_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    with pytest.raises(MandateError):
        await verify_mandate(m, "AA==", now=NOW)


def test_constraints_non_dict_mandate_rejected():
    # B3: a non-dict mandate must raise MandateError, not AttributeError.
    with pytest.raises(MandateError):
        check_mandate_constraints(["not", "a", "dict"], action="x")


def test_constraints_non_iterable_actions_rejected():
    # B2: a non-iterable allowlist must raise MandateError, not TypeError.
    with pytest.raises(MandateError):
        check_mandate_constraints({"constraints": {"actions": 123}}, action="x")


def test_constraints_non_iterable_merchants_rejected():
    with pytest.raises(MandateError):
        check_mandate_constraints({"constraints": {"merchants": 5}}, merchant_id="m")


def test_constraints_string_actions_does_not_substring_permit():
    # B4 (fail-open guard): "pay" is a substring of "create_payment" but a bare
    # string allowlist must NOT permit it — malformed constraints never widen.
    with pytest.raises(MandateError):
        check_mandate_constraints(
            {"constraints": {"actions": "create_payment"}}, action="pay"
        )


async def test_resolver_non_valueerror_fails_closed_as_mandateerror():
    # N1: a did:web issuer resolves over the network; a non-ValueError transport
    # failure must fail closed as MandateError, not escape as a raw 500-class error.
    async def boom(did):
        raise RuntimeError("network down")

    priv = ec.generate_private_key(ec.SECP256R1())
    m = _intent(_did_key_es256(priv))
    with pytest.raises(MandateError):
        await verify_mandate(m, "AA==", now=NOW, resolver=boom)
