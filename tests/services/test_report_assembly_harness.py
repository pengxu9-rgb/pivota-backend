"""A harness that drives run_brand_report far enough to assert what it ATTACHES.

WHY THIS EXISTS. Four times in this workstream a correct, well-tested mechanism
shipped connected by a line nothing asserted:

  * #2004 — `destination_rank` / `is_primary_destination` could be zeroed at the
    persist boundary with every test green (closed by extracting a helper).
  * #2008 — `_basis_pair_for_delta` returning (None, None), i.e. the whole
    comparability feature inert, left 166 tests green.
  * #2008 again — `record_audit_basis` returned None for the empty run id the
    caller passes, so the feature shipped doing nothing.
  * #2009 — `brand_rollup["selection_gap"]` is set inside run_brand_report and
    no test in the repo reached that branch, including the neighbouring
    section's.

Every one is the same shape: the logic is covered, the SEAM is not. Unit tests
stop at the function boundary, and the attach lines live inside a 19k-line
module's assembly function that nothing drove with the guards satisfied.

WHAT THIS DOES. `assemble_report` fakes exactly the external I/O — SKU context,
probe runs, the LLM client, the catalogue read, prior-run fetches — and returns
a real assembled report. Tests then assert on what ARRIVED, not on what a
function returned in isolation.

WHAT IT BUYS, MEASURED. A 16-mutant battery against these attach sites kills
14: deleting an attach line, pointing it at the wrong key, replacing it with a
constant or an empty-shaped dict, feeding it the wrong merchant's catalogue or
empty per-SKU reports, swapping the lost/won inputs or the payload fields, and
every way of breaking the basis pair. The two it does NOT kill are named at the
bottom of this file rather than left for the next reader to rediscover.
"""
from typing import Any, Dict, List, Optional

import pytest

from services.selection_gap import SELECTION_GAP_VERSION

from tests.services.test_agent_center_bd_per_sku import (
    _base_sku_ctx,
    _failing_run,
    _positive_probe_runs,
)

pytestmark = pytest.mark.asyncio

CATALOG = [
    {
        "product_key": "anua-niacinamide-serum",
        "title": "Anua Niacinamide 10 TXA 4 Serum",
        "brand": "TestBrand",
        "product_type": "Serum",
        "category": None,
        "category_label": None,
        "category_path": None,
        "tags": [],
        "use_case_tags": [],
    },
]


async def assemble_report(
    monkeypatch,
    *,
    catalog_rows: Optional[List[Dict[str, Any]]] = None,
    prior_runs: Optional[List[Dict[str, Any]]] = None,
    prior_report: Optional[Dict[str, Any]] = None,
    prior_basis: Optional[Dict[str, Any]] = None,
    catalog_raises: bool = False,
    selected_set_id: Optional[str] = None,
    seen: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the real assembly with only external I/O faked."""
    from config import settings as settings_module
    from services import agent_center_bd_report_service as bd
    import services.prompt_basis as pb

    monkeypatch.setattr(settings_module.settings, "deepseek_api_key", "test-key")

    async def fake_ctx(sku_key, merchant_id):
        ctx = _base_sku_ctx()
        ctx["sku_key"] = sku_key
        ctx["merchant_id"] = merchant_id
        return ctx

    async def fake_runs(sku_key, merchant_id, audit_run_id, include_internal_comparison=False):
        # One UNCITED prompt, so the section under test has a real gap to find
        # rather than the empty-state shape every positive run produces.
        runs = _positive_probe_runs(count=3) + [
            _failing_run("best affordable niacinamide serum", ["Rival Labs"])
        ]
        if selected_set_id:
            # The basis meta rides the PERSISTED probe payload — this is the
            # seam run_brand_report reloads (basis_meta_from_probe_runs at
            # agent_center_bd_report_service.py:7642), which is what stamps
            # per_sku_reports[].prompt_basis and unlocks the comparability
            # branch below. resolve_prompt_basis is NOT on this code path.
            pb.attach_basis_meta_to_probe_runs(runs, {
                "prompt_set_id": "ps_1",
                "selected_set_id": selected_set_id,
                "basis_version": pb.PROMPT_BASIS_VERSION,
                "audit_tier": "standard",
                "source": "pinned",
                "winnable": [],
                "scenario": [],
            })
        return runs

    async def fake_probe(**kw):
        return {
            "scan_mode": kw["scan_mode"], "provider": "deepseek", "role": "verify",
            "raw_runs": [], "usage": {}, "scores": {"visibility_score": 0},
        }

    async def fake_catalog(merchant_id, cap=500):
        # This fake stands in for a wrapper, not the I/O seam, so it deletes the
        # merchant_id -> query binding from coverage. Record it so a caller can
        # assert the section was built from THIS merchant's catalogue.
        if seen is not None:
            seen["catalog_merchant_id"] = merchant_id
        if catalog_raises:
            raise RuntimeError("db down")
        return list(catalog_rows if catalog_rows is not None else CATALOG)

    async def fake_prior_full(run_id=None, **_kw):
        return {"report_jsonb": prior_report} if prior_report else None

    async def fake_basis(run_id):
        return prior_basis

    monkeypatch.setattr(bd, "load_sku_context", fake_ctx)
    monkeypatch.setattr(bd, "load_per_sku_probe_runs", fake_runs)
    monkeypatch.setattr(bd.llm_client, "probe", fake_probe)
    monkeypatch.setattr(bd, "_merchant_catalog_rows_for_selection_gap", fake_catalog)
    # Patched at the SOURCE: run_brand_report imports it locally
    # (`from db.merchant_audit_runs import fetch_audit_run_by_id`), so it is not
    # an attribute of bd. A `raising=False` patch on bd silently did nothing —
    # exactly the missed-seam class this harness exists to catch.
    import db.merchant_audit_runs as mar
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_prior_full)
    import db.audit_basis as ab
    monkeypatch.setattr(ab, "get_basis_for_run", fake_basis)

    return await bd.run_brand_report(
        merchant_name="TestBrand", merchant_domain="merchant.test",
        products=[{"sku_key": "sku-1", "product_key": "prod-1"}],
        coverage_profile="us_shopper", audit_mode="per_sku",
        merchant_id="m-1", audit_run_id="audit-1", prompts_per_sku=4,
        prior_runs=prior_runs,
    )


# --- C1: the selection-gap attach (#2009's admitted gap) -------------------
async def test_the_selection_gap_section_is_attached(monkeypatch):
    seen: Dict[str, Any] = {}
    report = await assemble_report(monkeypatch, seen=seen)
    rollup = report.get("brand_rollup") or {}
    assert "selection_gap" in rollup, (
        "brand_rollup['selection_gap'] is set inside run_brand_report and was "
        "reached by no test in the repo — deleting the line shipped green"
    )
    section = rollup["selection_gap"]
    assert section["version"] == SELECTION_GAP_VERSION

    # Key-presence alone passes on any stub dict. Assert the CONTENT, so a
    # constant, a swapped gaps/won_queries payload, or the wrong lost/won
    # inputs cannot survive.
    assert seen["catalog_merchant_id"] == "m-1", (
        "the section must be built from THIS merchant's catalogue"
    )
    assert section["counts"] == {
        "catalog_products_indexed": 1,
        "lost_queries": 1,
        "lost_queries_with_matched_product": 1,
        "won_queries": 3,
    }
    assert [q["query"] for q in section["won_queries"]] == [
        f"where can I buy Bright Skin Serum query {i}" for i in range(3)
    ], "the won/lost inputs must not be swapped on the way in"

    gaps = section["gaps"]
    assert len(gaps) == 1, f"expected the one uncited prompt to surface: {gaps}"
    gap = gaps[0]
    assert gap["query"] == "best affordable niacinamide serum"
    # The matched-product join IS the section's product claim ("you sell X and
    # lose query Y"). An empty-state section would assert fine without it.
    assert [m["product_key"] for m in gap["matched_products"]] == [
        "anua-niacinamide-serum"
    ]


async def test_an_empty_catalogue_attaches_nothing(monkeypatch):
    """Positive counterpart: the attach is guarded, not unconditional."""
    report = await assemble_report(monkeypatch, catalog_rows=[])
    assert "selection_gap" not in (report.get("brand_rollup") or {})


async def test_a_catalogue_read_failure_never_sinks_the_report(monkeypatch):
    report = await assemble_report(monkeypatch, catalog_raises=True)
    assert report.get("brand_rollup"), "the report must still assemble"
    assert "selection_gap" not in (report.get("brand_rollup") or {})


# --- #2008: the reaudit-delta basis kwargs --------------------------------
# Deleting `current_basis=` / `prior_basis=` at either build_reaudit_delta call
# site survived mutation, because nothing drove an attach site end to end.
def _attached_delta(report: Dict[str, Any]) -> Dict[str, Any]:
    """Pin WHERE the delta lands (the report ROOT, not brand_rollup), so the
    section moving is a failure rather than silently accepted."""
    delta = report.get("reaudit_delta")
    assert delta, "no reaudit_delta attached — the harness is not reaching the site"
    return delta


def _prior_report(prompt_set_id="sel_1"):
    return {
        "prompt_basis": {"selected_set_id": prompt_set_id},
        "scores": {"visibility_score": 50},
        "brand_rollup": {},
        "per_sku_reports": [{"prompt_basis": {"selected_set_id": prompt_set_id}}],
    }


def _basis(**over):
    """A prior basis that MATCHES the current run's in-memory basis.

    The current side is built by record_audit_basis(persist=False) off the
    assembled report, so a prior row invented from plausible-looking values
    diverges on some field no matter what the test is trying to prove — and
    then `same is False` passes for the wrong reason. These values mirror the
    real current basis field for field, which makes the control below assert
    True and the model swap the ONLY thing that moves it.
    """
    row = {
        "methodology_version": "1",
        "providers_and_models": {
            "chatgpt": {"model_id": "chat-latest", "temperature": None},
            "gemini": {"model_id": "gemini-2.5-flash", "temperature": None},
        },
        "primary_destination_version": 1,
        "prompt_set_id": "ps_1",
        "selected_set_id": "sel_1",
        "official_domains": [],
        "tier_mix": {},
        "market": "US",
        "language": "en",
    }
    row.update(over)
    return row


async def _measurement_basis(monkeypatch, prior_basis):
    report = await assemble_report(
        monkeypatch,
        prior_runs=[{"run_id": "prior-1", "status": "succeeded",
                     "requested_at": "2026-08-01T00:00:00Z", "audit_mode": "per_sku"}],
        prior_report=_prior_report(),
        selected_set_id="sel_1",
        prior_basis=prior_basis,
    )
    return _attached_delta(report).get("measurement_basis") or {}


async def test_a_reaudit_delta_is_attached_when_a_prior_run_exists(monkeypatch):
    report = await assemble_report(
        monkeypatch,
        prior_runs=[{"run_id": "prior-1", "status": "succeeded",
                     "requested_at": "2026-08-01T00:00:00Z", "audit_mode": "per_sku"}],
        prior_report=_prior_report(),
        selected_set_id="sel_1",
        prior_basis=_basis(),
    )
    delta = _attached_delta(report)
    assert "measurement_basis" in delta


async def test_an_unchanged_basis_is_reported_as_comparable(monkeypatch):
    """The control. Without it, `same is False` below proves only that SOMETHING
    diverged — which it does even with no model swap, if the fixture basis does
    not match the current one."""
    basis = await _measurement_basis(monkeypatch, _basis())
    assert basis.get("same") is True, (
        f"the control basis must be comparable, else the swap test is "
        f"confounded: {basis}"
    )
    assert not basis.get("basis_divergence")


async def test_a_model_swap_between_runs_is_not_reported_as_movement(monkeypatch):
    """End to end: the basis pair must actually REACH build_reaudit_delta.
    Deleting the current_basis/prior_basis kwargs is invisible to unit tests."""
    basis = await _measurement_basis(
        monkeypatch,
        _basis(providers_and_models={
            "chatgpt": {"model_id": "chat-latest", "temperature": None},
            "gemini": {"model_id": "gemini-3-flash-preview", "temperature": None},
        }),
    )
    assert basis.get("same") is False, (
        "a model swap must not be narrated as the merchant's own movement"
    )
    assert basis.get("basis_divergence") == "measurement_basis"


# --- What this harness does NOT catch -------------------------------------
# Measured, not assumed: a 16-mutant battery against the attach sites and the
# comparability path kills 14. The two survivors, so the next reader does not
# have to rediscover them:
#
#   1. Dropping *_delta_bases from the measurement_basis_between() call that
#      feeds report["outreach_outcomes"] (agent_center_bd_report_service.py
#      :16876). That is a SECOND attach site, and outreach_outcomes surfaces
#      only `comparable` / `basis_note` — both already False/incomparable here
#      for unrelated reasons, so asserting on them would be confounded in
#      exactly the way test_an_unchanged_basis_is_reported_as_comparable
#      exists to prevent. Closing it needs the outreach fixture built out.
#   2. Passing the wrong merchant_name into _selection_gap_section. The name is
#      used to strip own-brand mentions from competitor lists; this fixture's
#      competitors contain no own-brand token, so no assertion moves. Closing
#      it needs a failing run whose competitors include the brand itself.
