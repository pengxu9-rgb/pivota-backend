"""
Tests for services/ap2_mandate.py::verify_mandate_chain (ADR-012 authority
layer — Intent→Cart→Payment chain-linkage). Each mandate is signed by the
issuer's did:key (resolved offline); the "bad" cases sign a VALID signature over
the malformed mandate so the individual verify passes and the CHAIN check is what
fails — isolating the linkage/bounding logic.
"""
import base64
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.ap2_mandate import (  # noqa: E402
    CART_MANDATE,
    INTENT_MANDATE,
    PAYMENT_MANDATE,
    MandateError,
    verify_mandate_chain,
)
from services.crypto_service import crypto_service  # noqa: E402

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
AGENT_DID = "did:key:z6MkAgentSubjectPlaceholder"
_DEFAULT = object()


def _b58(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = b""
    while num > 0:
        num, rem = divmod(num, 58)
        out = bytes([_B58[rem]]) + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out.decode("ascii")


def _issuer():
    priv = ec.generate_private_key(ec.SECP256R1())
    comp = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.CompressedPoint,
    )
    return priv, "did:key:z" + _b58(b"\x80\x24" + comp)


def _sign(mandate, priv) -> str:
    msg = crypto_service.canonicalize_json(mandate).encode()
    return base64.b64encode(priv.sign(msg, ec.ECDSA(hashes.SHA256()))).decode()


def _env(mtype, issuer_did, **extra):
    return {
        "type": mtype,
        "issuer": issuer_did,
        "subject": AGENT_DID,
        "issued_at": (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        **extra,
    }


def _intent(issuer_did, **over):
    m = _env(INTENT_MANDATE, issuer_did, id="intent-1", constraints={
        "actions": ["create_payment"], "max_amount": "100.00",
        "currency": "USD", "merchants": ["m_1"]})
    m.update(over)
    return m


def _cart(issuer_did, **over):
    m = _env(CART_MANDATE, issuer_did, id="cart-1", intent_ref="intent-1",
             total="50.00", currency="USD", merchant_id="m_1")
    m.update(over)
    return m


def _payment(issuer_did, **over):
    m = _env(PAYMENT_MANDATE, issuer_did, cart_ref="cart-1", amount="50.00",
             currency="USD")
    m.update(over)
    return m


async def _verify(intent, cart, payment, priv, issuer_did, *,
                  trusted=_DEFAULT, now=NOW, agent=AGENT_DID):
    ti = {issuer_did} if trusted is _DEFAULT else trusted
    return await verify_mandate_chain(
        intent, _sign(intent, priv),
        cart, _sign(cart, priv),
        payment, _sign(payment, priv),
        agent_did=agent, trusted_issuers=ti, now=now,
    )


# ------------------------------- happy path --------------------------------- #

async def test_valid_chain_verifies():
    priv, did = _issuer()
    out = await _verify(_intent(did), _cart(did), _payment(did), priv, did)
    assert out["intent"]["id"] == "intent-1"
    assert out["cart"]["id"] == "cart-1"
    assert out["payment"]["cart_ref"] == "cart-1"


# ------------------------------- deny-by-default ---------------------------- #

async def test_missing_trusted_issuers_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did), _payment(did), priv, did, trusted=None)


async def test_untrusted_issuer_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did), _payment(did), priv, did,
                      trusted={"did:key:zSomeoneElse"})


async def test_intent_without_constraints_rejected():
    priv, did = _issuer()
    intent = _intent(did)
    del intent["constraints"]
    with pytest.raises(MandateError):
        await _verify(intent, _cart(did), _payment(did), priv, did)


# ------------------------------- type + linkage ----------------------------- #

async def test_wrong_type_in_slot_rejected():
    priv, did = _issuer()
    # A second IntentMandate handed in the cart slot.
    with pytest.raises(MandateError):
        await _verify(_intent(did), _intent(did, id="intent-1"), _payment(did), priv, did)


async def test_cart_intent_ref_mismatch_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, intent_ref="other"), _payment(did), priv, did)


async def test_payment_cart_ref_mismatch_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did), _payment(did, cart_ref="other"), priv, did)


# ------------------------------- amount + currency -------------------------- #

async def test_payment_amount_not_equal_cart_total_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, total="50.00"),
                      _payment(did, amount="60.00"), priv, did)


async def test_cart_total_over_intent_max_amount_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, total="150.00"),
                      _payment(did, amount="150.00"), priv, did)


async def test_currency_mismatch_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, currency="USD"),
                      _payment(did, currency="EUR"), priv, did)


async def test_merchant_not_in_intent_allowlist_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, merchant_id="m_evil"),
                      _payment(did), priv, did)


# ------------------------------- binding + temporal ------------------------- #

async def test_subject_not_agent_rejected():
    priv, did = _issuer()
    cart = _cart(did, subject="did:key:zDifferentAgent")
    with pytest.raises(MandateError):
        await _verify(_intent(did), cart, _payment(did), priv, did)


async def test_expired_mandate_in_chain_rejected():
    priv, did = _issuer()
    payment = _payment(did, expires_at=(NOW - timedelta(minutes=1)).isoformat())
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did), payment, priv, did)


async def test_tampered_mandate_rejected():
    priv, did = _issuer()
    # Sign a valid cart, then tamper the total AFTER signing → signature breaks.
    intent = _intent(did)
    cart = _cart(did)
    payment = _payment(did)
    isig, csig, psig = _sign(intent, priv), _sign(cart, priv), _sign(payment, priv)
    cart["total"] = "5.00"  # tamper post-signing
    with pytest.raises(MandateError):
        await verify_mandate_chain(
            intent, isig, cart, csig, payment, psig,
            agent_did=AGENT_DID, trusted_issuers={did}, now=NOW,
        )


# ------------------------- hardening (review B1/B2) ------------------------- #

async def test_agent_did_none_rejected():
    # agent_did=None must NOT skip subject binding — fail closed (B1).
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did), _payment(did), priv, did, agent=None)


async def test_action_none_rejected():
    priv, did = _issuer()
    intent, cart, payment = _intent(did), _cart(did), _payment(did)
    with pytest.raises(MandateError):
        await verify_mandate_chain(
            intent, _sign(intent, priv), cart, _sign(cart, priv),
            payment, _sign(payment, priv),
            agent_did=AGENT_DID, trusted_issuers={did}, now=NOW, action=None,
        )


async def test_intent_without_max_amount_rejected():
    # A signed intent with no ceiling is a blank cheque — deny (B2).
    priv, did = _issuer()
    intent = _intent(did, constraints={"currency": "USD", "merchants": ["m_1"]})
    with pytest.raises(MandateError):
        await _verify(intent, _cart(did), _payment(did), priv, did)


async def test_intent_without_currency_rejected():
    # A ceiling without a currency is meaningless — deny (B2).
    priv, did = _issuer()
    intent = _intent(did, constraints={"max_amount": "100.00", "merchants": ["m_1"]})
    with pytest.raises(MandateError):
        await _verify(intent, _cart(did), _payment(did), priv, did)


async def test_infinity_amount_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, total="Infinity"),
                      _payment(did, amount="Infinity"), priv, did)


async def test_negative_amount_rejected():
    priv, did = _issuer()
    with pytest.raises(MandateError):
        await _verify(_intent(did), _cart(did, total="-50.00"),
                      _payment(did, amount="-50.00"), priv, did)
