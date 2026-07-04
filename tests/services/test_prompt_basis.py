"""W2 — pinned measurement basis: the LLM-generated prompt lists become an
asset of the SKU, reused on re-runs so scores are comparable by construction.

The core contract under test:
  - a prior completed run's stamped basis is RELOADED (no LLM calls);
  - only a first audit / explicit refresh / PROMPT_BASIS_VERSION bump generates;
  - an empty prior basis (e.g. an LLM-outage run) is never pinned;
  - the re-audit delta states basis identity honestly (same / changed / unknown).
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.prompt_basis import (
    PROMPT_BASIS_VERSION,
    build_prompt_set_id,
    build_selected_set_id,
    clean_selected_specs,
    harvest_prompt_basis,
    resolve_prompt_basis,
    set_selected_specs_on_meta,
)


def _report_with_basis(sku_key: str, basis: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "brand_report": {
            "per_sku_reports": [
                {"sku_key": "other-sku", "prompt_basis": {"basis_version": 99}},
                {"sku_key": sku_key, "prompt_basis": basis},
            ],
        },
    }


_GOOD_BASIS = {
    "prompt_set_id": "ps_abc123",
    "basis_version": PROMPT_BASIS_VERSION,
    "source": "fresh",
    "winnable": ["vegan shampoo", "shampoo with japanese shiso for healthy scalp"],
    "scenario": ["best shampoo for postpartum hair loss"],
    "created_at": "2026-07-03T12:00:00+00:00",
}


# ---- identity ----------------------------------------------------------------

def test_prompt_set_id_is_stable_and_content_sensitive():
    a = build_prompt_set_id(["p1", "p2"], ["s1"])
    assert a == build_prompt_set_id(["p1", "p2"], ["s1"])
    assert a != build_prompt_set_id(["p1"], ["s1"])
    assert a != build_prompt_set_id(["p1", "p2"], [])
    assert a.startswith("ps_")


# ---- harvest -----------------------------------------------------------------

def test_harvest_finds_the_right_sku_basis():
    basis = harvest_prompt_basis(
        _report_with_basis("sku-1", _GOOD_BASIS), sku_key="sku-1",
    )
    assert basis is not None
    assert basis["winnable"] == _GOOD_BASIS["winnable"]
    assert basis["scenario"] == _GOOD_BASIS["scenario"]
    assert basis["prompt_set_id"] == "ps_abc123"
    assert basis["created_at"] == "2026-07-03T12:00:00+00:00"


def test_harvest_rejects_version_mismatch():
    stale = dict(_GOOD_BASIS, basis_version=PROMPT_BASIS_VERSION + 1)
    assert harvest_prompt_basis(
        _report_with_basis("sku-1", stale), sku_key="sku-1",
    ) is None


def test_harvest_rejects_empty_basis():
    empty = dict(_GOOD_BASIS, winnable=[], scenario=[])
    assert harvest_prompt_basis(
        _report_with_basis("sku-1", empty), sku_key="sku-1",
    ) is None


def test_harvest_absent_sku_or_malformed_returns_none():
    assert harvest_prompt_basis(
        _report_with_basis("sku-1", _GOOD_BASIS), sku_key="missing",
    ) is None
    assert harvest_prompt_basis({}, sku_key="sku-1") is None
    assert harvest_prompt_basis(
        {"brand_report": {"per_sku_reports": "not-a-list"}}, sku_key="sku-1",
    ) is None


# ---- resolve: pinned vs fresh --------------------------------------------------

class _Generators:
    """Counting fakes — the pinned path must make ZERO generator calls."""

    def __init__(self, winnable: List[str], scenario: List[str]):
        self.calls = 0
        self._w, self._s = winnable, scenario

    async def winnable(self) -> List[str]:
        self.calls += 1
        return self._w

    async def scenario(self) -> List[str]:
        self.calls += 1
        return self._s


@pytest.mark.asyncio
async def test_resolve_pins_prior_basis_without_llm_calls(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return {
            "winnable": ["pinned w1"], "scenario": ["pinned s1"],
            "prompt_set_id": "ps_prior", "created_at": "2026-07-01T00:00:00+00:00",
            "pinned_from_run_id": "run-prior",
        }

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["fresh w"], ["fresh s"])

    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    assert gen.calls == 0, "pinned path must not call the LLM generators"
    assert out["winnable"] == ["pinned w1"]
    assert out["scenario"] == ["pinned s1"]
    meta = out["meta"]
    assert meta["source"] == "pinned"
    assert meta["prompt_set_id"] == "ps_prior"
    assert meta["pinned_from_run_id"] == "run-prior"
    # basis birthdate carries through the chain
    assert meta["created_at"] == "2026-07-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_resolve_generates_fresh_when_no_prior(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return None

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["fresh w1", "fresh w1", " fresh w2 "], ["fresh s1"])

    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    assert gen.calls == 2
    # cleaned: deduped + stripped
    assert out["winnable"] == ["fresh w1", "fresh w2"]
    meta = out["meta"]
    assert meta["source"] == "fresh"
    assert meta["basis_version"] == PROMPT_BASIS_VERSION
    assert meta["prompt_set_id"] == build_prompt_set_id(
        ["fresh w1", "fresh w2"], ["fresh s1"],
    )
    assert meta["created_at"]  # stamped now


@pytest.mark.asyncio
async def test_resolve_refresh_skips_prior_and_marks_source(monkeypatch):
    import services.prompt_basis as pb

    load_calls = []

    async def fake_load(**kwargs):
        load_calls.append(1)
        return {"winnable": ["old"], "scenario": [], "prompt_set_id": "ps_old"}

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["new w"], ["new s"])

    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
        refresh=True,
    )
    assert load_calls == [], "refresh=True must not even consult the prior basis"
    assert gen.calls == 2
    assert out["meta"]["source"] == "refreshed"


@pytest.mark.asyncio
async def test_resolve_generator_failure_yields_empty_never_raises(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return None

    async def exploding() -> List[str]:
        raise RuntimeError("LLM down")

    async def working() -> List[str]:
        return ["s1"]

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=exploding, generate_scenario=working,
    )
    assert out["winnable"] == []
    assert out["scenario"] == ["s1"]
    assert out["meta"]["source"] == "fresh"


# ---- round trip: this run's stamp is the next run's pin -----------------------

@pytest.mark.asyncio
async def test_stamped_meta_round_trips_through_harvest(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return None

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["w1"], ["s1"])
    first = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    # The meta is stamped verbatim as per-SKU report `prompt_basis`…
    prior_report = _report_with_basis("sku-1", first["meta"])
    # …and the next run harvests exactly the same basis + identity.
    harvested = harvest_prompt_basis(prior_report, sku_key="sku-1")
    assert harvested is not None
    assert harvested["winnable"] == first["winnable"]
    assert harvested["scenario"] == first["scenario"]
    assert harvested["prompt_set_id"] == first["meta"]["prompt_set_id"]


# ---- durable carrier: the meta must ride the PERSISTED probe payload ----------
# The probing phase and the report phase use DIFFERENT sku_ctx instances
# (run_brand_report resets the context cache and reloads), so a ctx stash alone
# silently vanishes by report time — the 2026-07-04 prod validation pair caught
# exactly that (run #1 stamped prompt_basis=None; run #2 regenerated fresh).

def test_meta_rides_probe_runs_round_trip():
    from services.prompt_basis import (
        attach_basis_meta_to_probe_runs,
        basis_meta_from_probe_runs,
    )

    runs = [
        {"query": "best shampoo", "raw": "..."},
        {"query": "vegan shampoo", "raw": "..."},
    ]
    attach_basis_meta_to_probe_runs(runs, dict(_GOOD_BASIS))
    # attached to exactly one run; run fields untouched
    assert runs[0]["prompt_basis_meta"]["prompt_set_id"] == "ps_abc123"
    assert "prompt_basis_meta" not in runs[1]
    assert runs[0]["query"] == "best shampoo"
    recovered = basis_meta_from_probe_runs(runs)
    assert recovered == dict(_GOOD_BASIS)


def test_meta_helpers_tolerate_garbage():
    from services.prompt_basis import (
        attach_basis_meta_to_probe_runs,
        basis_meta_from_probe_runs,
    )

    attach_basis_meta_to_probe_runs(None, _GOOD_BASIS)      # no runs
    attach_basis_meta_to_probe_runs([], _GOOD_BASIS)        # empty
    attach_basis_meta_to_probe_runs(["not-a-dict"], _GOOD_BASIS)
    attach_basis_meta_to_probe_runs([{"query": "q"}], None)  # no meta
    assert basis_meta_from_probe_runs(None) is None
    assert basis_meta_from_probe_runs([{"query": "q"}]) is None


def test_report_stamp_reads_probe_runs_not_only_ctx():
    """The report assembly must source prompt_basis from the persisted probe
    payload (basis_meta_from_probe_runs) — a ctx-only read is the exact
    dataflow break that shipped. Source-level pin, mirrors the W6 pattern."""
    from pathlib import Path

    src = Path("services/agent_center_bd_report_service.py").read_text()
    stamp = src[src.index('"prompt_basis": ('):]
    stamp = stamp[:stamp.index('),')]
    assert "basis_meta_from_probe_runs(probe_runs)" in stamp, (
        "per-SKU report prompt_basis no longer reads the durable probe-run "
        "carrier — the probing-phase ctx stash does not survive to report time"
    )


# ---- delta basis identity -----------------------------------------------------

def _delta_report(prompt_set_id):
    return {
        "per_product": [{
            "prompt_basis": {"prompt_set_id": prompt_set_id},
            "merchant_view": {"headline": {"scores": {
                "visibility": 50, "attribution": 40, "category_visibility": 30,
            }}},
        }],
    }


def test_delta_states_same_basis():
    from services.audit_delta import build_reaudit_delta

    delta = build_reaudit_delta(
        current_report=_delta_report("ps_x"),
        prior_report=_delta_report("ps_x"),
        prior_row={}, days_since=7,
    )
    basis = delta["measurement_basis"]
    assert basis["same"] is True
    assert basis["prompt_set_id"] == "ps_x"
    assert "same prompt set" in basis["note"]


def test_delta_states_changed_basis():
    from services.audit_delta import build_reaudit_delta

    delta = build_reaudit_delta(
        current_report=_delta_report("ps_new"),
        prior_report=_delta_report("ps_old"),
        prior_row={}, days_since=7,
    )
    assert delta["measurement_basis"]["same"] is False
    assert "changed" in delta["measurement_basis"]["note"]


def test_delta_honest_when_prior_predates_pinning():
    from services.audit_delta import build_reaudit_delta

    prior = _delta_report(None)
    prior["per_product"][0].pop("prompt_basis")
    delta = build_reaudit_delta(
        current_report=_delta_report("ps_x"),
        prior_report=prior,
        prior_row={}, days_since=7,
    )
    assert delta["measurement_basis"]["same"] is None


def test_delta_first_audit_declares_baseline_basis():
    from services.audit_delta import build_reaudit_delta

    delta = build_reaudit_delta(
        current_report=_delta_report("ps_x"),
        prior_report=None, prior_row=None, days_since=None,
    )
    assert delta["is_first_audit"] is True
    assert delta["measurement_basis"]["same"] is None
    assert delta["measurement_basis"]["prompt_set_id"] == "ps_x"


# ---- W2.1: pin the FULL selected probe set, not just the LLM lists ----------

_SPECS = [
    {"query": "vegan shampoo", "axis": "category", "source": "llm_winnable"},
    {"query": "best shampoo for shiso", "axis": "sidewalk",
     "attribute_basis": ["ingredient:shiso"], "evidence": ["shiso extract"],
     "intent_weight": 0.7},
    {"query": "DAMDAM CLASSIC SHAMPOO - Shiso reviews", "axis": "review"},
]


def test_clean_selected_specs_sanitizes_dedupes_caps():
    dirty = [
        {"query": "  Vegan Shampoo  ", "axis": "category", "source": "x", "junk": 1},
        {"query": "vegan shampoo", "axis": "category"},   # dup (case/space)
        {"axis": "category"},                              # no query -> dropped
        {"query": "q2", "attribute_basis": ["a", "", None], "evidence": list("abcdefghij"),
         "intent_weight": 3},
    ]
    out = clean_selected_specs(dirty)
    assert [s["query"] for s in out] == ["Vegan Shampoo", "q2"]
    assert "junk" not in out[0]
    assert out[0]["axis"] == "category"
    # evidence capped at 6; attribute_basis drops falsy; weight coerced to float
    assert out[1]["attribute_basis"] == ["a"]
    assert len(out[1]["evidence"]) == 6
    assert out[1]["intent_weight"] == 3.0
    assert clean_selected_specs("not-a-list") == []


def test_selected_set_id_is_query_and_axis_sensitive():
    a = build_selected_set_id(_SPECS)
    assert a == build_selected_set_id(_SPECS)
    assert a.startswith("sel_")
    # order matters
    assert a != build_selected_set_id(list(reversed(_SPECS)))
    # axis matters (same queries, different axis)
    shifted = [dict(s, axis="other") for s in _SPECS]
    assert a != build_selected_set_id(shifted)
    # extra metadata (evidence/weight) does NOT change identity — only query+axis
    stripped = [{"query": s["query"], "axis": s["axis"]} for s in _SPECS]
    assert a == build_selected_set_id(stripped)


def test_set_selected_specs_on_meta_folds_and_ids():
    meta = {"prompt_set_id": "ps_x", "source": "fresh"}
    out = set_selected_specs_on_meta(meta, _SPECS)
    assert out["prompt_set_id"] == "ps_x"          # untouched
    assert [s["query"] for s in out["selected_specs"]] == [s["query"] for s in _SPECS]
    assert out["selected_set_id"] == build_selected_set_id(_SPECS)
    # no-op when nothing to record (an all-empty probe set never overwrites)
    assert set_selected_specs_on_meta(meta, []) == meta
    assert "selected_set_id" not in set_selected_specs_on_meta(meta, [])


def test_harvest_reads_selected_specs_version_gated():
    basis = dict(_GOOD_BASIS, selected_specs=_SPECS,
                 selected_set_id=build_selected_set_id(_SPECS))
    got = harvest_prompt_basis(_report_with_basis("sku-1", basis), sku_key="sku-1")
    assert [s["query"] for s in got["selected_specs"]] == [s["query"] for s in _SPECS]
    assert got["selected_set_id"] == build_selected_set_id(_SPECS)
    # a W2-only basis (no selected_specs) still pins the LLM lists; selected empty
    got2 = harvest_prompt_basis(_report_with_basis("sku-1", _GOOD_BASIS), sku_key="sku-1")
    assert got2["selected_specs"] == []
    assert got2["selected_set_id"] is None


@pytest.mark.asyncio
async def test_resolve_pins_selected_specs_for_reprobe(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return {
            "winnable": ["w"], "scenario": ["s"], "selected_specs": _SPECS,
            "selected_set_id": build_selected_set_id(_SPECS),
            "prompt_set_id": "ps_prior", "created_at": "2026-07-01T00:00:00+00:00",
            "pinned_from_run_id": "run-prior",
        }

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["fresh"], ["fresh"])
    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    assert gen.calls == 0
    assert [s["query"] for s in out["selected_specs"]] == [s["query"] for s in _SPECS]


@pytest.mark.asyncio
async def test_resolve_fresh_has_empty_selected_specs(monkeypatch):
    import services.prompt_basis as pb

    async def fake_load(**kwargs):
        return None

    monkeypatch.setattr(pb, "load_prior_prompt_basis", fake_load)
    gen = _Generators(["w"], ["s"])
    out = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    assert out["selected_specs"] == []  # established after probing, not here


@pytest.mark.asyncio
async def test_selected_specs_round_trip_fresh_then_pin(monkeypatch):
    """Fresh run records the probed set -> stamped on report -> next run pins &
    reprobes it. The end-to-end W2.1 contract in one test."""
    import services.prompt_basis as pb

    async def no_prior(**kwargs):
        return None

    monkeypatch.setattr(pb, "load_prior_prompt_basis", no_prior)
    gen = _Generators(["w1"], ["s1"])
    fresh = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen.winnable, generate_scenario=gen.scenario,
    )
    # the fan-out folds what was probed into the meta before persisting
    final_meta = set_selected_specs_on_meta(fresh["meta"], _SPECS)
    prior_report = _report_with_basis("sku-1", final_meta)

    # next run harvests + resolves -> pins the same selected set
    async def load_from_report(**kwargs):
        got = harvest_prompt_basis(prior_report, sku_key="sku-1")
        got["pinned_from_run_id"] = "run-1"
        return got

    monkeypatch.setattr(pb, "load_prior_prompt_basis", load_from_report)
    gen2 = _Generators(["should-not-run"], ["should-not-run"])
    pinned = await resolve_prompt_basis(
        merchant_id="m1", sku_key="sku-1",
        generate_winnable=gen2.winnable, generate_scenario=gen2.scenario,
    )
    assert gen2.calls == 0
    assert [s["query"] for s in pinned["selected_specs"]] == [s["query"] for s in _SPECS]
    assert pinned["meta"]["source"] == "pinned"


def test_probe_path_reprobes_pinned_selected_specs():
    """Source-level pin: _probe_per_sku_ctx must reprobe _pinned_selected_specs
    directly (no spec rebuild) and record _selected_specs_out — the exact
    dataflow W2.1 depends on. Mirrors the W6/W2 source-scan pattern."""
    from pathlib import Path

    src = Path("services/agent_center_bd_report_service.py").read_text()
    fn = src[src.index("async def _probe_per_sku_ctx"):]
    fn = fn[:fn.index("\nasync def ", 1)] if "\nasync def " in fn[1:] else fn[:8000]
    assert '_pinned_selected_specs' in fn
    assert '_selected_specs_out' in fn


def test_delta_prefers_selected_set_id_over_prompt_set_id():
    """When the FULL probed set matches, the delta says same=True even if the
    LLM-list prompt_set_id differs; when the probed set differs, same=False even
    if prompt_set_id matches."""
    from services.audit_delta import build_reaudit_delta

    def rep(prompt_id, sel_id):
        return {"per_product": [{
            "prompt_basis": {"prompt_set_id": prompt_id, "selected_set_id": sel_id},
            "merchant_view": {"headline": {"scores": {
                "visibility": 50, "attribution": 40, "category_visibility": 30}}},
        }]}

    # same selected set, different LLM-list id -> same measurement
    d1 = build_reaudit_delta(
        current_report=rep("ps_A", "sel_X"), prior_report=rep("ps_B", "sel_X"),
        prior_row={}, days_since=7)
    assert d1["measurement_basis"]["same"] is True
    assert d1["measurement_basis"]["prompt_set_id"] == "sel_X"

    # different selected set, same LLM-list id -> NOT the same measurement
    d2 = build_reaudit_delta(
        current_report=rep("ps_A", "sel_X"), prior_report=rep("ps_A", "sel_Y"),
        prior_row={}, days_since=7)
    assert d2["measurement_basis"]["same"] is False
