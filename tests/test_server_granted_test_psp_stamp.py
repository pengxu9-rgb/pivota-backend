"""The server stamps an allowlisted probe merchant's order itself, so a TEST processor no longer
depends on the caller remembering a URL parameter — but ONLY when that merchant's processors are
actually test-mode.

Production evidence (2026-08-29, merch_c5e24a8d3738d73b, same env both times):
  ORD_9F4C24E73705231D  no allow_test_psp_surfaces  -> "All PSPs blocked: ... configured for test"
  ORD_50C00A24BEADFA78  stamped                     -> paid
A buyer reaching checkout through PDP -> cart -> Checkout never carries the parameter.
"""
from __future__ import annotations

import asyncio

import routes.order_routes as orr


def _run(coro):
    return asyncio.run(coro)


def _probe(monkeypatch, *, enabled=True, merchants="merch_probe"):
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1" if enabled else "0")
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", merchants)


def _psps(monkeypatch, rows):
    async def fake(*, merchant_id, provider=None, database_override=None):
        return rows

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", fake)


TEST_ROW = {"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_test_abc"}
LIVE_ROW = {"provider": "stripe", "environment": "live", "runtime_secret_key": "sk_live_abc"}


# --- the delivering behaviour -------------------------------------------------------------

def test_stamps_an_allowlisted_test_mode_merchant_with_no_caller_stamp(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is True
    assert metadata["allow_test_psp_surfaces"] is True
    assert metadata["test_psp_surfaces_granted_by"] == "server_allowlist"
    # The whole point: the gate now permits the test processor for this order.
    assert orr._resolve_order_live_readiness_requirement(metadata, "merch_probe") is False


def test_without_the_stamp_the_gate_still_enforces(monkeypatch):
    # Guards the premise: if this were already False, the test above would prove nothing.
    _probe(monkeypatch)
    assert orr._resolve_order_live_readiness_requirement({}, "merch_probe") is True


# --- the new safety guard -----------------------------------------------------------------

def test_refuses_when_the_merchant_has_a_LIVE_processor(monkeypatch):
    # The reason this is safer than the caller-supplied stamp: allowlisting a live merchant by
    # mistake must not route its real buyers to a test processor and mark them paid unpaid.
    _probe(monkeypatch)
    _psps(monkeypatch, [LIVE_ROW])
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata


def test_refuses_when_ANY_processor_is_live(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW, LIVE_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_refuses_when_the_merchant_has_no_processors(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_refuses_when_the_psp_lookup_raises(monkeypatch):
    _probe(monkeypatch)

    async def boom(*, merchant_id, provider=None, database_override=None):
        raise RuntimeError("db down")

    import services.merchant_psp_config_service as svc

    monkeypatch.setattr(svc, "fetch_active_merchant_psps", boom)
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_environment_missing_but_test_key_prefix_counts_as_test(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "stripe", "environment": "", "runtime_secret_key": "sk_test_x"}])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is True


def test_a_live_key_is_refused_even_when_environment_claims_test(monkeypatch):
    # The dangerous shape: `environment` is a label someone types, the KEY is what charges a card.
    # A row mislabelled test while holding sk_live_ must never earn a test-mode bypass.
    _probe(monkeypatch)
    _psps(monkeypatch, [{"provider": "stripe", "environment": "test", "runtime_secret_key": "sk_live_x"}])
    assert _run(orr._merchant_active_psp_is_test_mode("merch_probe")) is False
    metadata: dict = {}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata


# --- containment: unchanged from before ---------------------------------------------------

def test_master_switch_off_refuses(monkeypatch):
    _probe(monkeypatch, enabled=False)
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_merchant_not_allowlisted_is_refused(monkeypatch):
    _probe(monkeypatch, merchants="merch_other")
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "merch_probe")) is False


def test_explicit_enforce_live_readiness_true_always_wins(monkeypatch):
    # The stricter choice must remain honourable from the caller, or the allowlist becomes a way to
    # force a test processor onto an order someone deliberately pinned to live.
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    metadata = {"enforce_live_readiness": True}
    assert _run(orr._apply_server_granted_test_psp_stamp(metadata, "merch_probe")) is False
    assert "allow_test_psp_surfaces" not in metadata
    assert orr._resolve_order_live_readiness_requirement(metadata, "merch_probe") is True


def test_non_dict_metadata_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp(None, "merch_probe")) is False


def test_missing_merchant_is_refused(monkeypatch):
    _probe(monkeypatch)
    _psps(monkeypatch, [TEST_ROW])
    assert _run(orr._apply_server_granted_test_psp_stamp({}, "")) is False
