"""P-T2.3.2 — scoped test-mode capture decision (readiness bypass + amount cap).

The bypass must engage ONLY for a guarded, kill-switch-permitted charge when the
flag is on, must refuse when over the cap, and must never touch a non-protocol or
production (flag-off) charge.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")

from services.agent_checkout_kill_switch import resolve_acp_test_capture  # noqa: E402


def test_not_engaged_when_flag_off():
    d = resolve_acp_test_capture("acp", amount_cents=100, kill_switch_allowed=True, enabled=False)
    assert d.engaged is False
    assert d.bypass_live_readiness is False
    assert d.reason == "not_engaged"


def test_not_engaged_for_non_protocol():
    d = resolve_acp_test_capture("rest", amount_cents=100, kill_switch_allowed=True, enabled=True)
    assert d.engaged is False
    assert d.bypass_live_readiness is False


def test_not_engaged_when_kill_switch_blocked():
    # If the kill-switch already blocks, the test lane is moot.
    d = resolve_acp_test_capture("acp", amount_cents=100, kill_switch_allowed=False, enabled=True)
    assert d.engaged is False
    assert d.bypass_live_readiness is False


def test_engaged_and_within_cap_bypasses_readiness():
    d = resolve_acp_test_capture("acp", amount_cents=100, kill_switch_allowed=True, enabled=True, max_cents=500)
    assert d.engaged is True
    assert d.within_cap is True
    assert d.bypass_live_readiness is True
    assert d.reason == "engaged"


def test_engaged_over_cap_refuses_bypass():
    # Over the cap: engaged but NOT within_cap → caller must refuse the charge,
    # and bypass_live_readiness is False so it never silently downgrades.
    d = resolve_acp_test_capture("acp", amount_cents=999, kill_switch_allowed=True, enabled=True, max_cents=500)
    assert d.engaged is True
    assert d.within_cap is False
    assert d.bypass_live_readiness is False
    assert d.reason == "over_cap"


def test_at_cap_boundary_is_within():
    d = resolve_acp_test_capture("acp", amount_cents=500, kill_switch_allowed=True, enabled=True, max_cents=500)
    assert d.within_cap is True
    assert d.bypass_live_readiness is True


def test_none_amount_is_not_within_cap_when_engaged():
    d = resolve_acp_test_capture("acp", amount_cents=None, kill_switch_allowed=True, enabled=True)
    assert d.engaged is True
    assert d.within_cap is False


def test_live_settings_default_is_off_and_capped():
    from config.settings import settings

    assert settings.agent_acp_test_capture is False
    assert settings.agent_acp_test_max_cents == 500
    # With the live default (flag off), an ACP charge never engages the test lane.
    d = resolve_acp_test_capture("acp", amount_cents=100, kill_switch_allowed=True)
    assert d.engaged is False
    assert d.bypass_live_readiness is False


# --- P-T2.3.5: LIVE-money capture resolution (separate gate, required allowlist) ---
from services.agent_checkout_kill_switch import resolve_acp_live_capture  # noqa: E402

_LIVE_M = frozenset({"merch_x"})


def _live(**kw):
    base = dict(protocol="acp", merchant_id="merch_x", amount_cents=100,
               kill_switch_allowed=True, enabled=True, merchants=_LIVE_M, max_cents=200)
    base.update(kw)
    return resolve_acp_live_capture(base.pop("protocol"), **base)


def test_live_not_engaged_when_flag_off():
    assert _live(enabled=False).allow_live is False


def test_live_not_engaged_when_merchant_not_allowlisted():
    assert _live(merchants=frozenset({"other"})).engaged is False


def test_live_not_engaged_with_empty_allowlist_even_if_flag_on():
    # Master switch ON but no allowlist → inert by construction (can't go live).
    assert _live(merchants=frozenset()).engaged is False


def test_live_not_engaged_when_kill_switch_blocked():
    assert _live(kill_switch_allowed=False).engaged is False


def test_live_not_engaged_for_non_protocol():
    assert _live(protocol="rest").engaged is False


def test_live_engaged_within_cap_allows_live():
    d = _live(amount_cents=150, max_cents=200)
    assert d.engaged is True and d.allow_live is True and d.reason == "engaged"


def test_live_engaged_over_cap_is_refused():
    d = _live(amount_cents=500, max_cents=200)
    assert d.engaged is True and d.within_cap is False and d.allow_live is False and d.reason == "over_cap"
