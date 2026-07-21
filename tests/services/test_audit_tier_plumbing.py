"""Deep-tier slice 1 (spec 2026-07-21): tier plumbing + per-tier basis pinning.

The tier is a NAMED product unit (standard 40 / deep 80 prompts per SKU per
provider), never a merchant-facing raw count. Contracts under test:
  - tier resolution: unknown → standard; deep is flag-gated (falls back to
    standard with AUDIT_DEEP_TIER_ENABLED off, never errors);
  - the probe budget follows the tier unless an explicit prompts_per_sku
    override (internal/testing knob) is set;
  - basis pinning is scoped PER TIER: a deep run never reuses a standard
    basis (tier switch = deliberate baseline reset), and a pre-tier legacy
    basis reads as standard so already-audited SKUs keep pinning;
  - the deep tier's larger LLM-list cap (18 vs 12) survives generation,
    stamping, and re-harvest without truncation;
  - the selected-spec storage cap (96) holds a full deep probe set — at the
    old 64 a deep set would silently truncate and the re-pinned basis would
    drift from what was actually probed.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from services.prompt_basis import (
    AUDIT_TIER_DEEP,
    AUDIT_TIER_STANDARD,
    PROMPT_BASIS_VERSION,
    clean_selected_specs,
    harvest_prompt_basis,
    max_prompts_per_list_for_tier,
    normalize_audit_tier,
    prompts_per_sku_for_tier,
    resolve_prompt_basis,
)


# ---- tier resolution ---------------------------------------------------------

def test_normalize_audit_tier_defaults_unknown_to_standard():
    assert normalize_audit_tier("deep") == AUDIT_TIER_DEEP
    assert normalize_audit_tier("DEEP ") == AUDIT_TIER_DEEP
    assert normalize_audit_tier("standard") == AUDIT_TIER_STANDARD
    for junk in (None, "", "premium", 42, {"tier": "deep"}):
        assert normalize_audit_tier(junk) == AUDIT_TIER_STANDARD


def test_tier_budgets():
    assert prompts_per_sku_for_tier(AUDIT_TIER_STANDARD) == 40
    assert prompts_per_sku_for_tier(AUDIT_TIER_DEEP) == 80
    assert prompts_per_sku_for_tier("nonsense") == 40
    assert max_prompts_per_list_for_tier(AUDIT_TIER_STANDARD) == 12
    assert max_prompts_per_list_for_tier(AUDIT_TIER_DEEP) == 18


def test_selected_spec_cap_holds_a_full_deep_probe_set():
    records = [{"query": f"prompt {i}", "axis": "category"} for i in range(120)]
    cleaned = clean_selected_specs(records)
    assert len(cleaned) == 96
    # A deep set (80) fits without truncation.
    assert len(clean_selected_specs(records[:80])) == 80


# ---- launch-option plumbing (worker helpers) ---------------------------------

def _set_deep_flag(monkeypatch, value: bool) -> None:
    from config.settings import settings

    monkeypatch.setattr(settings, "audit_deep_tier_enabled", value, raising=False)


def test_launch_audit_tier_is_flag_gated(monkeypatch):
    from services.audit_run_worker import _launch_audit_tier

    _set_deep_flag(monkeypatch, False)
    assert _launch_audit_tier({}) == AUDIT_TIER_STANDARD
    # Flag off: a deep request degrades to a completed standard run.
    assert _launch_audit_tier({"audit_tier": "deep"}) == AUDIT_TIER_STANDARD

    _set_deep_flag(monkeypatch, True)
    assert _launch_audit_tier({"audit_tier": "deep"}) == AUDIT_TIER_DEEP
    assert _launch_audit_tier({"audit_tier": "bogus"}) == AUDIT_TIER_STANDARD


def test_launch_prompts_per_sku_follows_tier_unless_overridden():
    from services.audit_run_worker import _launch_prompts_per_sku

    assert _launch_prompts_per_sku({}) == 40
    assert _launch_prompts_per_sku({}, AUDIT_TIER_DEEP) == 80
    # Explicit internal knob wins over the tier default.
    assert _launch_prompts_per_sku({"prompts_per_sku": 14}, AUDIT_TIER_DEEP) == 14
    # Garbage falls back to the tier default, not a crash and not 40-always.
    assert _launch_prompts_per_sku({"prompts_per_sku": "x"}, AUDIT_TIER_DEEP) == 80


# ---- per-tier basis pinning --------------------------------------------------

def _report_with_basis(sku_key: str, basis: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "brand_report": {
            "per_sku_reports": [{"sku_key": sku_key, "prompt_basis": basis}],
        },
    }


def _basis(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "prompt_set_id": "ps_abc123",
        "basis_version": PROMPT_BASIS_VERSION,
        "source": "fresh",
        "winnable": ["vegan shampoo"],
        "scenario": ["best shampoo for postpartum hair loss"],
        "created_at": "2026-07-20T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_legacy_basis_reads_as_standard():
    report = _report_with_basis("sku-1", _basis())  # no audit_tier key
    pinned = harvest_prompt_basis(report, sku_key="sku-1")
    assert pinned is not None
    assert pinned["audit_tier"] == AUDIT_TIER_STANDARD
    # The same legacy basis must NOT pin a deep run — baseline reset.
    assert (
        harvest_prompt_basis(report, sku_key="sku-1", audit_tier=AUDIT_TIER_DEEP)
        is None
    )


def test_tier_stamped_basis_pins_only_its_own_tier():
    report = _report_with_basis("sku-1", _basis(audit_tier="deep"))
    assert harvest_prompt_basis(report, sku_key="sku-1") is None
    pinned = harvest_prompt_basis(
        report, sku_key="sku-1", audit_tier=AUDIT_TIER_DEEP
    )
    assert pinned is not None and pinned["audit_tier"] == AUDIT_TIER_DEEP


def test_deep_harvest_keeps_18_item_lists():
    winnable = [f"winnable prompt {i}" for i in range(25)]
    report = _report_with_basis(
        "sku-1", _basis(audit_tier="deep", winnable=winnable)
    )
    pinned = harvest_prompt_basis(
        report, sku_key="sku-1", audit_tier=AUDIT_TIER_DEEP
    )
    assert pinned is not None
    assert len(pinned["winnable"]) == 18  # deep cap, not the standard 12


@pytest.mark.asyncio
async def test_resolve_fresh_stamps_tier_and_honors_deep_cap(monkeypatch):
    # Force the fresh path regardless of DB availability.
    import services.prompt_basis as pb

    async def no_prior(**_kwargs):
        return None

    monkeypatch.setattr(pb, "load_prior_prompt_basis", no_prior)

    async def gen_winnable():
        return [f"winnable prompt {i}" for i in range(25)]

    async def gen_scenario():
        return [f"scenario prompt {i}" for i in range(25)]

    deep = await resolve_prompt_basis(
        merchant_id="m1",
        sku_key="sku-1",
        generate_winnable=gen_winnable,
        generate_scenario=gen_scenario,
        audit_tier=AUDIT_TIER_DEEP,
    )
    assert deep["meta"]["audit_tier"] == AUDIT_TIER_DEEP
    assert len(deep["winnable"]) == 18 and len(deep["scenario"]) == 18

    standard = await resolve_prompt_basis(
        merchant_id="m1",
        sku_key="sku-1",
        generate_winnable=gen_winnable,
        generate_scenario=gen_scenario,
    )
    assert standard["meta"]["audit_tier"] == AUDIT_TIER_STANDARD
    assert len(standard["winnable"]) == 12
