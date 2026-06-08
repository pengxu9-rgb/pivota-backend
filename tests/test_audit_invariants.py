"""Layer 1 invariant tests. Keystone = the exact URL-audit P0 shape (the
merchant's own domain in top_controllers with merchant_owned_count == 0)."""
import json
from pathlib import Path

import pytest

from services.audit_invariants import (
    check_audit_invariants,
    resolve_merchant_identity,
    MerchantIdentity,
    SEVERITY_CRITICAL,
    SEVERITY_WARN,
)


def _codes(report):
    return sorted(v.code for v in report.violations)


def _critical_codes(report):
    return sorted(v.code for v in report.critical())


def _base_payload(*, owned=2, controllers=None, state="mixed", third=12, prompts=14):
    """A clean wedge payload skeleton (no contradictions)."""
    return {
        "audited_url": "https://bblab.shop/products/good-night-collagen",
        "audited_products": [{"pdp_url": "https://bblab.shop/products/good-night-collagen"}],
        "brand_report": {
            "merchant_name": "BB LAB",
            "merchant_domain": "bblab.shop",
            "aggregate": {
                "buyer_path_verdict": {
                    "state": state,
                    "merchant_owned_count": owned,
                    "third_party_controlled_count": third,
                    "prompt_count": prompts,
                    "top_controllers": controllers if controllers is not None
                    else ["reddit.com", "yesstyle.com", "iherb.com"],
                }
            },
        },
        "sku_intelligence": {
            "hero_sku": {"pdp_url": "https://bblab.shop/products/good-night-collagen"},
            "prompt_matrix": [],
            "top_open_lanes": [],
            "substitution_alert": {"present": False},
            "intent_ladder": {},
        },
    }


# --- identity -------------------------------------------------------------
def test_identity_resolves_from_pdp_url_only():
    """The P0 cold-start shape carries the merchant URL as pdp_url, not
    canonical_url — identity must still resolve the merchant host."""
    payload = _base_payload()
    payload["brand_report"].pop("merchant_domain", None)
    ident = resolve_merchant_identity(payload)
    assert "bblab.shop" in ident.hosts


def test_identity_subdomain_is_first_party_but_not_overmatched():
    ident = MerchantIdentity(hosts=frozenset({"bblab.shop"}))
    from services.audit_invariants import _is_own_host
    assert _is_own_host("shop.bblab.shop", ident) is True
    assert _is_own_host("bblab.shop", ident) is True
    assert _is_own_host("notbblab.shop", ident) is False
    assert _is_own_host("yesstyle.com", ident) is False


# --- KEYSTONE: the exact P0 -----------------------------------------------
def test_keystone_p0_own_host_in_top_controllers_is_critical():
    """The bug: merchant's own domain listed as a third-party controller with
    merchant_owned_count == 0. The invariant must fire CRITICAL A1."""
    payload = _base_payload(
        owned=0, state="third_party_controlled",
        controllers=["iherb.com", "bblab.shop", "yesstyle.com"],
    )
    report = check_audit_invariants(payload)
    assert "OWN_HOST_AS_CONTROLLER" in _critical_codes(report)
    v = next(v for v in report.critical() if v.code == "OWN_HOST_AS_CONTROLLER")
    assert v.surface == "brand_report"
    assert "bblab.shop" in str(v.evidence)


def test_keystone_corrected_payload_is_clean():
    """Post-fix: own host removed from top_controllers, a lane owned -> no
    critical violations."""
    payload = _base_payload(owned=2, state="mixed",
                            controllers=["iherb.com", "yesstyle.com"])
    report = check_audit_invariants(payload)
    assert report.critical() == ()


# --- per-invariant --------------------------------------------------------
def test_a2_own_host_as_per_lane_controller_is_critical():
    payload = _base_payload()
    payload["sku_intelligence"]["prompt_matrix"] = [{
        "query": "halal korean collagen sticks",
        "ownership_state": "retailer-owned",
        "who_owns": "m.stylekorean.com",
        "buyer_path_action": {"controllers": ["m.stylekorean.com", "bblab.shop"]},
    }]
    report = check_audit_invariants(payload)
    assert "OWN_HOST_AS_COMPETITOR_SKU" in _critical_codes(report)


def test_d1_nonhost_who_owns_is_critical():
    payload = _base_payload()
    payload["sku_intelligence"]["prompt_matrix"] = [{
        "query": "low molecular collagen sticks",
        "who_owns": "Good Night Collagen (Low-Molecular Weight Collagen) Halal 30 sticks",
    }]
    report = check_audit_invariants(payload)
    assert "REDIRECTOR_OR_NONHOST_CONTROLLER" in _critical_codes(report)


def test_d1_redirector_host_as_controller_is_critical():
    payload = _base_payload(
        controllers=["vertexaisearch.cloud.google.com", "yesstyle.com"])
    report = check_audit_invariants(payload)
    assert "REDIRECTOR_OR_NONHOST_CONTROLLER" in _critical_codes(report)


def test_c1_merchant_controlled_but_zero_owned_is_critical():
    payload = _base_payload(owned=0, state="merchant_controlled")
    report = check_audit_invariants(payload)
    assert "STATE_VS_OWNED_COUNT" in _critical_codes(report)


def test_c1_third_party_controlled_but_no_external_controller_is_critical():
    payload = _base_payload(owned=0, state="third_party_controlled",
                            controllers=["bblab.shop"])
    report = check_audit_invariants(payload)
    # bblab.shop is own -> A1 fires AND state has no non-own controller -> C1.
    assert "STATE_VS_CONTROLLERS" in _critical_codes(report)
    assert "OWN_HOST_AS_CONTROLLER" in _critical_codes(report)


def test_c2_count_overflow_is_critical():
    payload = _base_payload(owned=10, third=10, prompts=14)
    report = check_audit_invariants(payload)
    assert "COUNT_OVERFLOW" in _critical_codes(report)


def test_c2_negative_count_is_critical():
    payload = _base_payload(owned=-1)
    report = check_audit_invariants(payload)
    assert "NEGATIVE_COUNT" in _critical_codes(report)


def test_c3_self_substitution_by_name_is_critical():
    payload = _base_payload()
    payload["sku_intelligence"]["substitution_alert"] = {
        "present": True, "substituted_by": "BB LAB", "engines": ["gemini"],
    }
    report = check_audit_invariants(payload)
    assert "SELF_SUBSTITUTION" in _critical_codes(report)


def test_c3_legit_competitor_substitution_is_clean():
    payload = _base_payload()
    payload["sku_intelligence"]["substitution_alert"] = {
        "present": True, "substituted_by": "Vital Proteins", "engines": ["gemini"],
    }
    report = check_audit_invariants(payload)
    assert "SELF_SUBSTITUTION" not in _critical_codes(report)


def test_c4_strong_but_unowned_is_warn_not_critical():
    payload = _base_payload(owned=0, state="third_party_controlled",
                            controllers=["yesstyle.com"])
    payload["sku_intelligence"]["intent_ladder"] = {
        "branded_transactional": {"score": 95}}
    report = check_audit_invariants(payload)
    assert "STRONG_BUT_UNOWNED" in _codes(report)
    assert "STRONG_BUT_UNOWNED" not in _critical_codes(report)
    warn = next(v for v in report.warnings() if v.code == "STRONG_BUT_UNOWNED")
    assert warn.severity == SEVERITY_WARN


def test_a3_own_host_as_brief_competitor_is_critical():
    payload = _base_payload()
    payload["sku_intelligence"]["next_best_action"] = {
        "strategic_brief": {"grounding_notes": {
            "competitor_attributes": {"status": "assessed", "competitor": "bblab.shop"},
        }}
    }
    report = check_audit_invariants(payload)
    assert "OWN_AS_COMPETITOR_BRIEF" in _critical_codes(report)


def test_missing_surfaces_never_crash():
    """A payload missing whole surfaces must yield no violations, never an
    exception or INVARIANT_INTERNAL_ERROR."""
    for payload in ({}, {"brand_report": {}}, {"sku_intelligence": {}},
                    {"brand_report": {"aggregate": {}}}):
        report = check_audit_invariants(payload)
        assert "INVARIANT_INTERNAL_ERROR" not in _codes(report)


# --- real-artifact characterization --------------------------------------
_ARTIFACT = Path.home() / "dev/Markato/p0_verify_bblab.json"


def _replay_sku_intelligence(artifact):
    """Re-score the saved live probe runs through the CURRENT pipeline (no keys,
    no network) so the golden reflects today's code, not a stale snapshot."""
    import asyncio
    import services.agent_center_bd_report_service as bd

    data = json.loads(artifact.read_text())
    per_q = {}
    for rec in (data.get("raw_probe_runs") or []):
        for rr in ((rec.get("response") or {}).get("raw_runs") or []):
            if isinstance(rr, dict) and rr.get("query"):
                per_q.setdefault((rec.get("provider"), rr["query"]), rr)
    if not per_q:
        return None

    async def _mock_probe(**kwargs):
        prov = kwargs.get("provider")
        ctx = kwargs.get("context") if isinstance(kwargs.get("context"), dict) else {}
        runs = [per_q[(prov, q)] for q in (ctx.get("queries") or []) if (prov, q) in per_q]
        return {"scan_mode": kwargs.get("scan_mode"), "provider": prov,
                "runs_count": len(runs), "raw_runs": runs, "scores": {},
                "findings": [], "model": "replay", "model_is_override": False}

    original = bd.llm_client.probe
    bd.llm_client.probe = _mock_probe
    try:
        product = {
            "title": "Good Night Collagen Low-Molecular Weight Collagen",
            "raw_title": "Good Night Collagen Low-Molecular Weight Collagen",
            "pdp_url": "https://bblab.shop/products/good-night-collagen-low-molecular-weight-collagen-30-packages",
            "vendor": "BB LAB", "attributes_raw": {},
        }
        return asyncio.new_event_loop().run_until_complete(
            bd.run_wedge_hero_sku_intelligence(
                hero_product=product, merchant_id="external_bd_bb_lab",
                run_id="replay", coverage_profile="us_shopper", prompts_per_sku=14,
            )
        )
    finally:
        bd.llm_client.probe = original


@pytest.mark.skipif(not _ARTIFACT.exists(), reason="live BB Lab artifact not present")
def test_real_data_replay_has_no_per_lane_own_host_leak():
    """Golden on REAL probe data, re-scored through the current pipeline: the
    per-lane controller/who_owns fix must leave the merchant's own host and
    product title out of every controller/owner surface, so the invariant layer
    reports no own-host/non-host criticals in sku_intelligence."""
    sku = _replay_sku_intelligence(_ARTIFACT)
    if sku is None:
        pytest.skip("artifact has no raw_probe_runs to replay")
    payload = {
        "audited_url": "https://bblab.shop/products/good-night-collagen",
        "sku_intelligence": sku,
    }
    report = check_audit_invariants(payload)
    sku_criticals = [v.code for v in report.critical() if v.surface == "sku_intelligence"]
    assert "OWN_HOST_AS_COMPETITOR_SKU" not in sku_criticals
    assert "REDIRECTOR_OR_NONHOST_CONTROLLER" not in sku_criticals
