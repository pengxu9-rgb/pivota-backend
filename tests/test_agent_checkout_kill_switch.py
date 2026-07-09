"""P-T2.2 — Tier-2 protocol checkout kill-switch decision matrix.

Verifies the fail-closed posture: protocol-tier charges are blocked by default
(strict ON, submit OFF), the live redirect/REST floor is never gated, and the
ceiling only opens when submit_payment is explicitly enabled.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services.agent_checkout_kill_switch import (  # noqa: E402
    GUARDED_PROTOCOLS,
    REASON_ALLOWED_STRICT_DISABLED,
    REASON_ALLOWED_STRICT_ENABLED,
    REASON_BLOCKED_SUBMIT_DISABLED,
    REASON_NOT_PROTOCOL,
    evaluate_tier2_charge,
    is_guarded_protocol,
)


@pytest.mark.parametrize("proto", ["acp", "ucp", "ap2", "ACP", " Ap2 "])
def test_guarded_protocols_recognized(proto):
    assert is_guarded_protocol(proto) is True


@pytest.mark.parametrize("proto", ["rest", "mcp", "", None, "redirect", "hosted"])
def test_non_protocol_lanes_not_guarded(proto):
    assert is_guarded_protocol(proto) is False


@pytest.mark.parametrize("proto", ["rest", "", None, "mcp"])
def test_non_protocol_charge_always_allowed(proto):
    # Even with the strictest posture, a non-protocol charge is never gated.
    d = evaluate_tier2_charge(proto, strict=True, submit_payment_enabled=False)
    assert d.allowed is True
    assert d.guarded is False
    assert d.reason == REASON_NOT_PROTOCOL


@pytest.mark.parametrize("proto", sorted(GUARDED_PROTOCOLS))
def test_protocol_charge_blocked_by_default_posture(proto):
    # Default: strict ON, submit OFF → fail-closed block.
    d = evaluate_tier2_charge(proto, strict=True, submit_payment_enabled=False)
    assert d.allowed is False
    assert d.guarded is True
    assert d.reason == REASON_BLOCKED_SUBMIT_DISABLED
    assert d.as_error_detail()["error"] == "TIER2_CHARGE_DISABLED"


@pytest.mark.parametrize("proto", sorted(GUARDED_PROTOCOLS))
def test_protocol_charge_allowed_when_submit_enabled(proto):
    # The canary flip: strict stays ON, submit_payment turned on → allowed.
    d = evaluate_tier2_charge(proto, strict=True, submit_payment_enabled=True)
    assert d.allowed is True
    assert d.guarded is True
    assert d.reason == REASON_ALLOWED_STRICT_ENABLED


@pytest.mark.parametrize("proto", sorted(GUARDED_PROTOCOLS))
def test_protocol_charge_allowed_when_strict_disabled(proto):
    # Dev bypass: strict explicitly off → allowed regardless of submit.
    d = evaluate_tier2_charge(proto, strict=False, submit_payment_enabled=False)
    assert d.allowed is True
    assert d.guarded is True
    assert d.reason == REASON_ALLOWED_STRICT_DISABLED


def test_live_settings_default_is_fail_closed():
    # With no env override, the live posture must be strict-ON / submit-OFF, so a
    # protocol charge is blocked. This is the "switch always ON by default" guarantee.
    from config.settings import settings

    assert settings.agent_checkout_strict is True
    assert settings.agent_submit_payment_enabled is False
    d = evaluate_tier2_charge("acp")  # uses live settings
    assert d.allowed is False
    assert d.reason == REASON_BLOCKED_SUBMIT_DISABLED


def test_strict_env_parsing_treats_absent_as_on():
    # An absent/blank AGENT_CHECKOUT_STRICT must parse as ON (fail-closed), and
    # only explicit false-y strings disable it.
    def parse_strict(raw):
        return str(raw).strip().lower() not in {"false", "0", "off", "no"}

    assert parse_strict("") is True
    assert parse_strict("true") is True
    assert parse_strict("anything") is True
    for off in ("false", "0", "off", "no", "FALSE", " Off "):
        assert parse_strict(off) is False
