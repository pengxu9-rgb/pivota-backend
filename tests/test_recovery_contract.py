import inspect
import json
from pathlib import Path

import pytest

from services.selection_measurement import response_observations, selection_measurement, selection_tier
from services.revenue_recovery_report import recovery_from_report
from services.audit_delta import measurement_basis_between
from services.report_summary_builder import _since_last_audit


def observations(runs):
    return response_observations(runs, sku_key="shopify|sku-1", merchant_host="anua.com", merchant_brand="Anua")


def test_response_denominator_keeps_replicates_providers_failures_and_unknown_separate():
    common = {"query": "best serum", "axis_metadata": {"axis": "category"}, "grounding_sources": []}
    runs = [{**common, "_provider": "gemini", "parsed": {"brand_mentioned": True}},
            {**common, "_provider": "gemini", "parsed": {"brand_mentioned": False}},
            {**common, "_provider": "chatgpt", "raw": "__error__:timeout"},
            {**common, "_provider": "chatgpt", "parsed": {"correct_sku": False}}]
    rows = observations(runs)
    metric = selection_measurement(rows + rows)
    t = metric["tiers"]["unbranded"]
    assert metric["observations"] == 4
    assert t["attempted"] == 4 and t["provider_failed"] == 1
    assert t["brand_mentioned"]["n"] == 2 and t["brand_mentioned"]["positive"] == 1
    assert t["brand_mentioned"]["unknown"] == 1
    assert t["source_visible"]["n"] == 3
    lo, hi = t["brand_mentioned"]["ci95"]
    assert 0 < lo < .5 < hi < 1


def test_source_is_not_answer_and_excerpt_is_not_complete_negative_evidence():
    rows = observations([{"query": "Anua reviews", "axis_metadata": {"axis": "review"},
                          "grounding_sources": [{"uri": "https://anua.com", "title": "Anua"}],
                          "parsed": {"evidence_excerpt": "Anua is available"}}])
    assert rows[0]["source_visible"] is True
    assert rows[0]["brand_mentioned"] is None
    assert "evidence_excerpt" not in rows[0] and "raw" not in rows[0]


@pytest.mark.parametrize("query,axis,expected", [
    ("Anua dupe", "brand", "dupe"), ("alternatives to Anua", "category", "dupe"),
    ("manuka serum", "category", "unbranded"), ("Anua price", "price", "branded"),
    ("strange query", "something_new", "unknown"),
])
def test_three_disjoint_tiers(query, axis, expected):
    assert selection_tier(query, axis, "Anua") == expected


def test_missing_responses_are_unknown_and_never_zero_percent():
    metric = selection_measurement([])
    assert metric["unavailable_reason"]
    assert set(metric["tiers"]) == {"branded", "unbranded", "dupe"}
    assert all(t["brand_mentioned"]["rate"] is None for t in metric["tiers"].values())


def test_real_report_json_round_trip_has_same_recovery_and_no_url_routing_score():
    report = json.loads(Path("tests/fixtures_real_per_sku_brand_report.json").read_text())
    report.setdefault("per_sku_reports", [{}])[0]["selection_observations"] = observations([
        {"query": "Anua dupe", "axis_metadata": {"axis": "category"}, "grounding_sources": [],
         "parsed": {"brand_mentioned": False}}])
    from services.audit_evidence_builder import extract_findings, extract_evidence_items, _evidence_signature
    from services.audit_projection_builder import build_revenue_recovery_projection
    report["catalog_dimensions_available"] = False
    findings = extract_findings(report)
    db_rows = [{**f, "payload_jsonb": json.dumps(f["payload"])} for f in findings]
    for f in db_rows:
        del f["payload"]
    from_db = build_revenue_recovery_projection(evidence=[], findings=db_rows, actions=[], audit_run_row={"run_id": "run"})
    direct = recovery_from_report(report, run_id="run", catalog_available=False)
    assert direct == from_db
    assert from_db["selection"]["tiers"]["dupe"]["brand_mentioned"]["n"] == 1
    assert all(d["dimension"] != "routability" for d in direct["headline"]["dimensions"])
    evidence = [e for e in extract_evidence_items(report) if e["evidence_type"] == "selection_response"]
    assert len(evidence) == 1 and _evidence_signature(evidence[0]).startswith("selection_response:")
    assert direct["stages"][-1]["status"] == "UNVERIFIED"


def test_old_significance_flags_are_not_republished():
    report = {"reaudit_delta": {"measurement_basis": {"same": True}, "movements": [
        {"signal": "attribution", "label": "First-party citation", "from": 51, "to": 40,
         "is_material": True, "direction": "regressed"}]}}
    out = _since_last_audit(report)
    assert out["basis_same"] is None
    assert out["movements"][0]["is_material"] is False
    assert report["reaudit_delta"]["movements"][0]["is_material"] is True


def test_missing_full_basis_fails_closed():
    report = {"per_sku_reports": [{"prompt_basis": {"selected_set_id": "same"}}]}
    assert measurement_basis_between(report, report)["same"] is None


def _url_persistence_stubs(monkeypatch, *, canonical=None, projections=None):
    """Stub the two persistence calls _persist_url_recovery makes."""
    import services.audit_evidence_builder as evidence
    import services.audit_projection_builder as projection
    seen = []

    async def persist(**kw):
        assert kw["brand_report"]["catalog_dimensions_available"] is False
        seen.append("evidence")
        return dict(canonical if canonical is not None else {"evidence_items_failed": 0})

    async def project(**kw):
        assert kw["strict"] is True
        seen.append("projection")
        return dict(projections if projections is not None
                    else {"projections_built": 6, "projections_failed": 0,
                          "readback_mismatches": 0})

    monkeypatch.setattr(evidence, "persist_canonical_evidence", persist)
    monkeypatch.setattr(projection, "build_and_persist_all_projections", project)
    return seen


async def test_url_persistence_happy_path_runs_both_halves(monkeypatch):
    from services.audit_run_worker import _persist_url_recovery
    seen = _url_persistence_stubs(monkeypatch)
    canonical, projections = await _persist_url_recovery(
        run_id="r", merchant_id="m", brand_report={})
    assert seen == ["evidence", "projection"]
    assert canonical["evidence_persistence_degraded"] is False


@pytest.mark.parametrize("counter", [
    "evidence_items_failed", "findings_failed", "actions_failed",
])
async def test_one_failed_evidence_insert_does_not_fail_and_refund_the_run(
    monkeypatch, counter,
):
    """M10. The caller runs _fail_run_and_refund AFTER the report is already
    persisted, so a raise here marks a DELIVERED audit failed, makes it
    unreachable through get_audit_run (completed stage only) and refunds it.
    response_observations deposits one selection_response row per product x
    provider x response, each its own insert — one transient failure out of
    hundreds must not do that. It is counted and returned instead."""
    from services.audit_run_worker import _persist_url_recovery
    _url_persistence_stubs(
        monkeypatch,
        canonical={"evidence_items_inserted": 412, counter: 1},
    )
    canonical, projections = await _persist_url_recovery(
        run_id="r", merchant_id="m", brand_report={})
    assert canonical["evidence_persistence_failed_total"] == 1
    assert canonical["evidence_persistence_degraded"] is True
    assert projections["projections_built"] == 6


async def test_a_partial_projection_failure_still_completes(monkeypatch):
    """Some projections written is a served report; refunding it is worse."""
    from services.audit_run_worker import _persist_url_recovery
    _url_persistence_stubs(
        monkeypatch,
        projections={"projections_built": 5, "projections_failed": 1,
                     "readback_mismatches": 0},
    )
    _, projections = await _persist_url_recovery(
        run_id="r", merchant_id="m", brand_report={})
    assert projections["projections_failed"] == 1


async def test_total_projection_failure_fails_and_refunds(monkeypatch):
    """M9 half one: nothing written, so there is no recovery surface to read."""
    from services.audit_run_worker import _persist_url_recovery
    _url_persistence_stubs(
        monkeypatch,
        projections={"projections_built": 0, "projections_failed": 6,
                     "readback_mismatches": 0},
    )
    with pytest.raises(RuntimeError, match="wrote nothing"):
        await _persist_url_recovery(run_id="r", merchant_id="m", brand_report={})


async def test_a_readback_mismatch_fails_and_refunds(monkeypatch):
    """M9 half two. The row the database holds is not the row we built, so the
    audit we would serve is not the audit we believe we stored — fatal even
    though five other projections persisted."""
    from services.audit_run_worker import _persist_url_recovery
    _url_persistence_stubs(
        monkeypatch,
        projections={"projections_built": 5, "projections_failed": 1,
                     "readback_mismatches": 1},
    )
    with pytest.raises(RuntimeError, match="read-back"):
        await _persist_url_recovery(run_id="r", merchant_id="m", brand_report={})


async def test_strict_projection_readback_is_counted_separately(monkeypatch):
    """The mismatch counter has to come from the BUILDER, not the stub above:
    deleting the read-back check must break something."""
    import services.audit_projection_builder as pb
    import db.audit_evidence as ae

    async def fetch_run(*, run_id):
        return {"run_id": run_id, "merchant_id": "m"}

    async def rows(**kw):
        return [{"finding_id": "f1", "finding_type": "not_selected",
                 "severity": "high", "short_summary": "s"}]

    async def none_rows(**kw):
        return []

    async def upsert(**kw):
        return True

    async def stored_is_wrong(**kw):
        return {"payload_jsonb": {"audience": "tampered"}}

    monkeypatch.setattr("db.merchant_audit_runs.fetch_audit_run_by_id", fetch_run)
    monkeypatch.setattr(ae, "list_evidence_for_run", none_rows)
    monkeypatch.setattr(ae, "list_findings_for_run", rows)
    monkeypatch.setattr(ae, "list_actions_for_run", none_rows)
    monkeypatch.setattr(ae, "upsert_projection", upsert)
    monkeypatch.setattr(ae, "fetch_projection", stored_is_wrong)

    summary = await pb.build_and_persist_all_projections(
        audit_run_id="r", strict=True)
    assert summary["projections_built"] == 0
    assert summary["readback_mismatches"] == summary["projections_failed"] > 0


async def test_domain_tick_seeds_before_checking_liveness(monkeypatch):
    import jobs.official_domain_liveness as job
    monkeypatch.setenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", "true")
    monkeypatch.setattr(job, "_cursor", "")
    seen = []
    batches = [[{"merchant_id": "merchant"}], []]
    async def fetch(query):
        return batches.pop(0)
    async def seed(merchant, **kw):
        assert kw["strict"] is True
        seen.append("seed")
    async def sweep(**kw):
        seen.append("sweep")
        return {"checked": 1}
    monkeypatch.setattr(job.database, "fetch_all", fetch)
    monkeypatch.setattr(job, "seed_inferred_domains", seed)
    monkeypatch.setattr(job, "refresh_official_domain_liveness", sweep)
    result = await job.run_official_domain_liveness_tick()
    assert seen == ["seed", "sweep"] and result["seed_failed"] == 0


async def test_domain_tick_is_dormant_unless_explicitly_armed(monkeypatch):
    """OPT-IN, not opt-out.

    audit_scheduler registers this tick every 6h on any worker with
    worker_enabled and the prod worker deploys, so a "true" default armed it
    on merge. Its first run seeds merchant_official_domains for EVERY merchant
    and official_domains is a comparability field — it moves attribution and
    makes the next re-audit of each of those merchants non-comparable.
    """
    import jobs.official_domain_liveness as job

    monkeypatch.delenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", raising=False)

    async def never(*a, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("the dormant tick touched the database")

    monkeypatch.setattr(job.database, "fetch_all", never)
    monkeypatch.setattr(job, "seed_inferred_domains", never)
    monkeypatch.setattr(job, "refresh_official_domain_liveness", never)

    assert job.liveness_job_enabled() is False
    assert await job.run_official_domain_liveness_tick() == {"skipped": True}

    # A value that is not "true" is not arming, either.
    for value in ("", "false", "0", "yes"):
        monkeypatch.setenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", value)
        assert job.liveness_job_enabled() is False, value
        assert await job.run_official_domain_liveness_tick() == {"skipped": True}


async def test_the_scheduler_registers_the_flag_checking_tick(monkeypatch):
    """The gate has to be on the callable the scheduler actually holds."""
    import services.audit_scheduler as sched
    import jobs.official_domain_liveness as job

    monkeypatch.delenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", raising=False)
    source = inspect.getsource(sched)
    assert "run_official_domain_liveness_tick" in source
    assert 'id="official_domain_liveness"' in source
    assert await job.run_official_domain_liveness_tick() == {"skipped": True}
