from __future__ import annotations

from utils.order_track_token import mint_order_track_token, verify_order_track_token


def test_order_track_token_round_trip_tampered_and_expired(monkeypatch):
    monkeypatch.setenv("ORDER_TRACK_TOKEN_SECRET", "test-order-track-secret")

    token = mint_order_track_token("ORD_TOKEN_1")
    assert verify_order_track_token(token) == "ORD_TOKEN_1"

    prefix, payload, sig = token.split(".")
    tampered = f"{prefix}.{payload}.{sig[:-2]}xx"
    assert verify_order_track_token(tampered) is None

    expired = mint_order_track_token("ORD_TOKEN_2", expires_in_seconds=-60)
    assert verify_order_track_token(expired) is None
