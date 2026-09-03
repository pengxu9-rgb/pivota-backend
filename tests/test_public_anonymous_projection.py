"""C2: the two new projection audiences.

`public_anonymous` is the ONLY projection an unauthenticated visitor may read,
so most of this file is about what must NOT come out of it. The tests are
written as a recursive sweep over the built payload rather than key-by-key
assertions: a new field added to a builder later is exactly how a leak ships,
and a per-key test would not notice one.

`revenue_recovery` is merchant-gated and mostly a rearrangement, so its tests
are about the one claim it makes that cannot be measured — CONVERT SALES.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from db import audit_evidence as ae
from services import audit_projection_builder as apb


# ---- fixtures ---------------------------------------------------------------

def _evidence() -> List[Dict[str, Any]]:
    """The shapes the REAL writers produce — copied from the deposit sites,
    not invented.

    This matters more than it looks. An earlier cut of the builder read
    `detected` and `evidence_level` out of payload_jsonb, and this fixture
    invented both keys there, so the tests passed while every real store came
    out as `detected: false`. evidence_level is a COLUMN
    (db/audit_evidence.py); the acceptance_signal payload is
    {verifier_id, observed_at, probe_id, signal} (routes/
    store_audit_probe_internal.py); the commerce payloads carry `status` /
    `platform` (routes/store_audit_commerce_probe_internal.py). No writer
    anywhere emits a boolean `detected`.
    """
    return [
        {"evidence_type": "acceptance_signal", "evidence_level": "tested",
         "payload_jsonb": {"verifier_id": "ucp_probe",
                           "observed_at": "2026-09-03T00:00:00Z",
                           "probe_id": "SECRET-PROBEID",
                           "signal": {"endpoint": "SECRET-ENDPOINT"}}},
        {"evidence_type": "commerce_platform", "evidence_level": "detected",
         "payload_jsonb": {"platform": "shopify",
                           "checkout_provider": "SECRET-PROVIDER"}},
        {"evidence_type": "commerce_checkout_route", "evidence_level": "tested",
         "payload_jsonb": {"audit_scope": "guest", "status": "SECRET-STATUS",
                           "challenge_stage": "SECRET-STAGE"}},
        # --- model-derived, must be dropped ---
        {"evidence_type": "grounding_chunk", "confidence": 95,
         "evidence_level": "tested",
         "payload_jsonb": {
             "host": "competitor.example",
             "excerpt_text": "SECRET-EXCERPT the model wrote",
             "query": "SECRET-QUERY best niacinamide serum"}},
        {"evidence_type": "competitor_mention",
         "payload_jsonb": {"competitor": "SECRET-RIVAL"}},
        {"evidence_type": "url_match",
         "payload_jsonb": {"url": "https://SECRET-URL.example/p/1"}},
        {"evidence_type": "missing_signal",
         "payload_jsonb": {"signal": "SECRET-MISSING"}},
        {"evidence_type": "industry_stat",
         "payload_jsonb": {"stat": "SECRET-STAT"}},
    ]


def _findings() -> List[Dict[str, Any]]:
    """The four types extract_findings can actually emit — it is the ONLY
    writer of readiness_findings. An earlier cut mapped stages to
    selection_gap / no_destination / authority_gap / citation_absent, none of
    which any producer emits, so a whole stage reported a false all-clear."""
    return [
        {"finding_id": "f-cat", "finding_type": "category_visibility_low",
         "severity": "high", "short_summary": "SECRET-SUMMARY category"},
        {"finding_id": "f-int", "finding_type": "integration_state_incomplete",
         "severity": "critical", "short_summary": "SECRET-SUMMARY integration"},
        {"finding_id": "f-ret",
         "finding_type": "merchant_visible_via_retailers_only",
         "severity": "high", "short_summary": "SECRET-SUMMARY retailers"},
        {"finding_id": "f-pdp", "finding_type": "first_party_pdp_indexing_gap",
         "severity": "medium", "short_summary": "SECRET-SUMMARY pdp"},
        {"finding_id": "f-low", "finding_type": "category_visibility_low",
         "severity": "low", "short_summary": "SECRET-SUMMARY low"},
    ]


def _actions() -> List[Dict[str, Any]]:
    """action_plan_items has NO finding_type column — parent_finding_id is the
    only link to a finding, and routing by finding_type silently put every
    action in the default stage."""
    return [
        {"parent_finding_id": "f-cat", "severity": "critical", "phase": 1,
         "title": "SECRET-ACTION rewrite the PDP", "lever": "content"},
        {"parent_finding_id": "f-ret", "severity": "high", "phase": 2,
         "title": "SECRET-ACTION pitch the retailer", "lever": "outreach"},
        {"parent_finding_id": None, "severity": "medium", "phase": 3,
         "title": "SECRET-ACTION unattached", "lever": "content"},
    ]


def _public(row: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return apb.build_projection(
        audience=ae.AUDIENCE_PUBLIC_ANONYMOUS,
        evidence=_evidence(), findings=_findings(), actions=_actions(),
        audit_run_row=row if row is not None else {"run_id": "r-1"},
    )


def _blob(payload: Any) -> str:
    import json
    return json.dumps(payload, default=str)


# ---- public_anonymous: what must not come out ------------------------------

def test_no_model_derived_content_reaches_an_anonymous_reader():
    """One sweep over the WHOLE payload. Every model-derived string in the
    fixture is tagged SECRET-; none may appear anywhere at any nesting."""
    blob = _blob(_public())
    leaked = [tok for tok in (
        "SECRET-EXCERPT", "SECRET-QUERY", "SECRET-RIVAL", "SECRET-URL",
        "SECRET-MISSING", "SECRET-STAT", "SECRET-SUMMARY", "SECRET-ACTION",
        # ...and the extra fields on the DETERMINISTIC payloads: being allowed
        # to report a signal is not being allowed to republish its payload.
        "SECRET-ENDPOINT", "SECRET-RUNID", "SECRET-ADMIN", "SECRET-ROUTE",
    ) if tok in blob]
    assert leaked == [], f"public projection leaked: {leaked}"


def test_no_owner_identity_reaches_an_anonymous_reader():
    """A claimed run must not become publicly attributable by being read
    here, and an unclaimed one has no owner to leak in the first place."""
    blob = _blob(_public({"run_id": "r-1", "merchant_id": "merch-secret"}))
    assert "merch-secret" not in blob
    assert "merchant_id" not in _public({"run_id": "r-1"})


def test_no_model_derived_score_reaches_an_anonymous_reader():
    """A headline number on an unregistered store is the fabrication risk in
    its purest form — and visibility_score_avg is model-derived."""
    row = {"run_id": "r-1", "visibility_score_avg": 73,
           "attribution_score_avg": 41, "verdict_labels": ["NEEDS_WORK"]}
    blob = _blob(_public(row))
    assert "73" not in blob and "41" not in blob
    assert "NEEDS_WORK" not in blob


def test_the_observed_signals_DO_come_through():
    """The positive counterpart. Without it, a builder returning {} would pass
    every refusal above.

    PRESENCE is the claim, and evidence_level comes from the COLUMN. There is
    no `detected` field: no writer records one, so emitting `detected: false`
    would tell a store that PASSED the probe that it has no agent checkout."""
    signals = _public()["observed_signals"]
    assert signals == [
        {"signal": "acceptance_signal", "evidence_level": "tested"},
        {"signal": "commerce_platform", "evidence_level": "detected"},
        {"signal": "commerce_checkout_route", "evidence_level": "tested"},
    ]


def test_evidence_level_is_read_from_the_column_not_the_payload():
    """The bug this file exists to prevent a second time. A builder reading
    evidence_level out of payload_jsonb gets None for every real row."""
    out = apb.build_projection(
        audience=ae.AUDIENCE_PUBLIC_ANONYMOUS,
        evidence=[{"evidence_type": "acceptance_signal",
                   "evidence_level": "tested",
                   "payload_jsonb": {"evidence_level": "SECRET-PAYLOAD-LEVEL"}}],
        findings=[], actions=[], audit_run_row={"run_id": "r-1"},
    )
    assert out["observed_signals"] == [
        {"signal": "acceptance_signal", "evidence_level": "tested"}
    ]


def test_an_unknown_evidence_level_is_dropped_not_passed_through():
    """The one field taken from a row rather than computed. Anything outside
    the documented enum must not reach an anonymous reader."""
    for bad in ("SECRET-PROSE", {"note": "SECRET-DICT"}, 7, True, ""):
        out = apb.build_projection(
            audience=ae.AUDIENCE_PUBLIC_ANONYMOUS,
            evidence=[{"evidence_type": "acceptance_signal",
                       "evidence_level": bad, "payload_jsonb": {}}],
            findings=[], actions=[], audit_run_row={"run_id": "r-1"},
        )
        assert out["observed_signals"] == [
            {"signal": "acceptance_signal", "evidence_level": None}
        ], f"leaked {bad!r}"


def test_the_allowlist_names_only_types_something_actually_writes():
    """Pre-approving a payload shape that does not exist is the same failure
    a denylist makes, one level up: whoever implements the writer later will
    not know their payload became public. Three commerce_* types were
    allowlisted with zero write sites; adding one back means reading its
    writer first."""
    assert apb._DETERMINISTIC_EVIDENCE_TYPES == {
        "acceptance_signal", "commerce_platform",
        "commerce_checkout_route", "commerce_cartability",
    }


def test_the_locked_counts_say_how_much_without_saying_what():
    locked = _public()["locked"]
    assert locked == {"findings": 5, "actions": 3}


def test_no_severity_histogram_reaches_an_anonymous_reader():
    """A severity breakdown looks like metadata and is not. extract_findings
    assigns every severity by a fixed threshold on the scores this projection
    refuses (avg_attribution < 30, avg_category < 20/40, phase_0_complete is
    False), so the histogram is an invertible encoding of them — and a
    `critical` discloses that a merchant's Pivota onboarding is incomplete."""
    locked = _public()["locked"]
    assert "by_severity" not in locked
    blob = _blob(_public())
    for level in ("critical", "high", "medium", "low"):
        assert level not in blob


def test_the_evidence_allowlist_is_an_allowlist_not_a_denylist():
    """A future evidence type must be EXCLUDED by default."""
    ev = _evidence() + [{
        "evidence_type": "some_future_type_nobody_reviewed",
        "evidence_level": "tested",
        "payload_jsonb": {"secret": "SECRET-FUTURE"},
    }]
    out = apb.build_projection(
        audience=ae.AUDIENCE_PUBLIC_ANONYMOUS, evidence=ev,
        findings=[], actions=[], audit_run_row={"run_id": "r-1"},
    )
    assert "SECRET-FUTURE" not in _blob(out)
    assert all(
        sig["signal"] != "some_future_type_nobody_reviewed"
        for sig in out["observed_signals"]
    )


def test_claimable_tracks_whether_the_run_has_an_owner():
    assert _public({"run_id": "r-1"})["claimable"] is True
    assert _public({"run_id": "r-1", "merchant_id": "m-1"})["claimable"] is False


def test_the_tier_says_so_out_loud():
    """Absence of GEO numbers must not read as a store that scored zero."""
    assert _public()["deterministic_only"] is True


# ---- who may read it --------------------------------------------------------

def test_public_anonymous_is_the_only_unauthenticated_audience():
    assert ae.PUBLIC_ALLOWED_AUDIENCES == {ae.AUDIENCE_PUBLIC_ANONYMOUS}


def test_no_internal_audience_became_publicly_readable():
    for internal in (
        ae.AUDIENCE_EMPLOYEE_BD, ae.AUDIENCE_INTERNAL_OPS,
        ae.AUDIENCE_PIVOTA_PDP_FEED, ae.AUDIENCE_FRONTEND_AGENT_FEED,
        ae.AUDIENCE_MERCHANT, ae.AUDIENCE_REVENUE_RECOVERY,
    ):
        assert internal not in ae.PUBLIC_ALLOWED_AUDIENCES


def test_revenue_recovery_is_merchant_readable_and_public_anonymous_is_not():
    assert ae.AUDIENCE_REVENUE_RECOVERY in ae.MERCHANT_ALLOWED_AUDIENCES
    assert ae.AUDIENCE_PUBLIC_ANONYMOUS not in ae.MERCHANT_ALLOWED_AUDIENCES


def test_the_two_mirrored_audience_lists_agree():
    """services/audit_projection_builder.py MIRRORS the audience constants as
    its own strings rather than importing them (deliberately, per its comment),
    so the two VALID_AUDIENCES sets can silently drift — and a builder
    registered under a name db/audit_evidence.py rejects is a projection that
    can be built and never read."""
    assert apb.VALID_AUDIENCES == ae.VALID_AUDIENCES
    assert set(apb._BUILDERS) == ae.VALID_AUDIENCES


# ---- revenue_recovery -------------------------------------------------------

def _recovery() -> Dict[str, Any]:
    return apb.build_projection(
        audience=ae.AUDIENCE_REVENUE_RECOVERY,
        evidence=_evidence(), findings=_findings(), actions=_actions(),
        audit_run_row={"run_id": "r-1", "visibility_score_avg": 40},
    )


def test_convert_sales_is_always_unverified():
    """Not an oversight — the browser commerce lane has produced zero
    production observations, so no run can carry evidence for this stage. A
    stage that claimed otherwise would fabricate the thing being measured."""
    stage = next(
        s for s in _recovery()["stages"] if s["stage"] == "convert_sales"
    )
    assert stage["status"] == "UNVERIFIED"
    assert stage["findings"] == [] and stage["actions"] == []
    assert "browser commerce lane" in stage["unverified_reason"]


def test_convert_sales_stays_unverified_even_with_findings_routed_at_it():
    """The status is structural, not a consequence of an empty list."""
    out = apb.build_projection(
        audience=ae.AUDIENCE_REVENUE_RECOVERY, evidence=[],
        findings=[{"finding_type": "convert_sales", "severity": "critical",
                   "short_summary": "x"}],
        actions=[], audit_run_row={"run_id": "r-1"},
    )
    stage = next(s for s in out["stages"] if s["stage"] == "convert_sales")
    assert stage["status"] == "UNVERIFIED"


def test_the_stages_route_the_finding_types_producers_actually_emit():
    stages = {s["stage"]: s for s in _recovery()["stages"]}
    assert [s["stage"] for s in _recovery()["stages"]] == [
        "get_selected", "get_cited", "convert_sales",
    ]
    assert {f["type"] for f in stages["get_selected"]["findings"]} == {
        "category_visibility_low", "integration_state_incomplete",
    }
    assert {f["type"] for f in stages["get_cited"]["findings"]} == {
        "merchant_visible_via_retailers_only", "first_party_pdp_indexing_gap",
    }
    assert stages["get_selected"]["status"] == "MEASURED"
    assert stages["get_cited"]["status"] == "MEASURED"


def test_get_cited_can_report_a_REAL_all_clear():
    """It has producible finding types behind it, so NO_FINDINGS there is
    genuine. An earlier cut mapped it to four types nothing emits, making it a
    permanent false all-clear — the exact thing convert_sales is marked
    UNVERIFIED to avoid."""
    out = apb.build_projection(
        audience=ae.AUDIENCE_REVENUE_RECOVERY, evidence=[],
        findings=[{"finding_id": "f1",
                   "finding_type": "category_visibility_low",
                   "severity": "high", "short_summary": "x"}],
        actions=[], audit_run_row={"run_id": "r-1"},
    )
    cited = next(s for s in out["stages"] if s["stage"] == "get_cited")
    assert cited["status"] == "NO_FINDINGS"
    assert "unverified_reason" not in cited


def test_a_stage_with_no_producer_never_reports_an_all_clear():
    """The structural rule behind both: if nothing that runs today can put a
    finding in a stage, its emptiness is not evidence of health."""
    for stage in apb._STAGES:
        if not apb._stage_is_measurable(stage):
            out = next(
                s for s in _recovery()["stages"] if s["stage"] == stage
            )
            assert out["status"] == "UNVERIFIED"
            assert out["unverified_reason"]


def test_actions_are_routed_by_parent_finding_id():
    """action_plan_items has no finding_type column, so routing by one put
    every action in the default stage regardless of what it was about."""
    stages = {s["stage"]: s for s in _recovery()["stages"]}
    assert [a["title"] for a in stages["get_cited"]["actions"]] == [
        "SECRET-ACTION pitch the retailer"
    ]
    titles = [a["title"] for a in stages["get_selected"]["actions"]]
    assert "SECRET-ACTION rewrite the PDP" in titles
    # An action with no parent still has to land somewhere visible.
    assert "SECRET-ACTION unattached" in titles


def test_low_severity_findings_are_dropped_like_the_merchant_projection():
    blob = _blob(_recovery())
    assert "SECRET-SUMMARY low" not in blob


# ---- mutants that survived the first battery -------------------------------

def test_claimable_is_false_for_an_absent_run_not_just_an_owned_one():
    """`bool(row) and not row.get(...)` — the first conjunct had no test, so
    dropping it survived. An absent run is not a claimable one."""
    for row in (None, {}):
        out = apb.build_projection(
            audience=ae.AUDIENCE_PUBLIC_ANONYMOUS, evidence=[],
            findings=[], actions=[], audit_run_row=row,
        )
        assert out["claimable"] is False


def test_the_locked_counts_count_the_right_collections():
    """findings and actions counted from their own lists. Both fixtures having
    the same length is how a swapped pair survives."""
    out = apb.build_projection(
        audience=ae.AUDIENCE_PUBLIC_ANONYMOUS, evidence=[],
        findings=[{"severity": "high"}] * 4,
        actions=[{"title": "a"}] * 7,
        audit_run_row={"run_id": "r-1"},
    )
    assert out["locked"] == {"findings": 4, "actions": 7}


def test_revenue_recovery_reports_the_visibility_score_not_another_one():
    """headline_score was asserted by nothing, so pointing it at
    attribution_score_avg survived."""
    out = apb.build_projection(
        audience=ae.AUDIENCE_REVENUE_RECOVERY, evidence=[], findings=[],
        actions=[],
        audit_run_row={"run_id": "r-1", "visibility_score_avg": 40,
                       "attribution_score_avg": 91},
    )
    assert out["headline_score"] == 40


# ---- what gets PERSISTED ----------------------------------------------------

def test_public_anonymous_is_not_persisted_for_every_run():
    """build_and_persist_all_projections runs for every audit run, including a
    paying merchant's. Persisting a public projection per run leaves the table
    pre-populated with one carrying that merchant_id in its own column, and
    idx_report_projections_merchant_audience makes it cheap to scan. The
    public lane builds it on demand for the unclaimed funnel runs that are its
    only subject."""
    import inspect
    src = inspect.getsource(apb.build_and_persist_all_projections)
    assert "VALID_AUDIENCES - {AUDIENCE_PUBLIC_ANONYMOUS}" in src, (
        "the persist loop must exclude the public audience"
    )
    assert "all 5 projections" not in src

