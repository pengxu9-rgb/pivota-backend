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
    "evidence_items_skipped_unidentified",
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
    assert canonical["evidence_persistence_failed_or_skipped_total"] == 1
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


def test_a_non_json_probe_run_id_still_yields_an_observation_id():
    """`_probe_run_id` is whatever the probe layer put there — a UUID and a
    datetime are both routine, and json.dumps serializes neither. The raise
    escaped response_observations into the caller's try, which also stamps
    run_facts, so ONE such id silently dropped run_facts for the whole run."""
    import uuid
    from datetime import datetime, timezone

    for probe_run_id in (uuid.uuid4(), datetime.now(timezone.utc)):
        rows = observations([{
            "query": "best serum", "axis_metadata": {"axis": "category"},
            "_provider": "gemini", "_probe_run_id": probe_run_id,
            "grounding_sources": [], "parsed": {"brand_mentioned": True},
        }])
        assert len(rows) == 1
        assert len(rows[0]["observation_id"]) == 64

    # Distinct probe runs must still get distinct ids — `default=str` must not
    # collapse them into one.
    a, b = uuid.uuid4(), uuid.uuid4()
    ids = {
        observations([{"query": "q", "axis_metadata": {"axis": "category"},
                       "_provider": "gemini", "_probe_run_id": pid,
                       "grounding_sources": []}])[0]["observation_id"]
        for pid in (a, b)
    }
    assert len(ids) == 2


def test_a_failed_observation_stamp_does_not_take_run_facts_with_it():
    """STRUCTURAL, because the assembly path this guards is a single 400-line
    function with no seam. The selection-observation loop shared one try with
    the run_facts stamp; they have no dependency on each other, so a failure in
    one must not erase the other.

    PARSED, NOT GREPPED. The first cut of this test asserted that
    "_selection_observation_failures += 1" appeared in the source text between
    the observation loop and the run_facts stamp — and the INNER per-SKU
    `except` supplies that string, so re-merging the OUTER try back into the
    run_facts try left the substring exactly where it was and the test green.
    The claim is about block structure, so it is made against the syntax tree:
    no `try` that guards the observation call may also contain the run_facts
    stamp.
    """
    import ast

    import services.agent_center_bd_report_service as acbd

    tree = ast.parse(inspect.getsource(acbd))
    parent_of = {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }

    def enclosing_tries(node):
        found = set()
        while node in parent_of:
            node = parent_of[node]
            if isinstance(node, ast.Try):
                found.add(node)
        return found

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "response_observations"
    ]
    assert calls, "the selection-observation call is gone from the assembly path"

    stamps = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "brand_rollup"
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == "run_facts"
            for target in node.targets
        )
    ]
    assert len(stamps) == 1, "expected exactly one brand_rollup run_facts stamp"

    observation_tries = set().union(*(enclosing_tries(c) for c in calls))
    assert observation_tries, (
        "the observation loop has no try of its own — one bad observation "
        "would escape into whatever try encloses it"
    )
    shared = observation_tries & enclosing_tries(stamps[0])
    assert not shared, (
        "the run_facts stamp sits inside a try that also guards the "
        "observation loop: one bad observation drops run_facts for the run"
    )


def test_the_recovery_postgres_step_runs_after_the_gate_it_must_not_mask():
    """The new step creates its own database and runs its own pytest. Placed
    before the preflight/discover/execute/assert chain, a failure in it aborts
    the job before the dialect gate those four steps exist to enforce has run
    — a red build that proves nothing about the gate and points elsewhere."""
    import yaml
    from pathlib import Path

    workflow = yaml.safe_load(
        Path(".github/workflows/postgres-dialect-gate.yml").read_text()
    )
    names = [s.get("name") for s in
             workflow["jobs"]["postgres-dialect-gate"]["steps"]]
    assert names.index("Assert the gate actually gated") < names.index(
        "Recovery report JSONB to authenticated HTTP"
    )
    step = next(s for s in workflow["jobs"]["postgres-dialect-gate"]["steps"]
                if s.get("name") == "Recovery report JSONB to authenticated HTTP")
    # It keeps its own junit assertion: a SKIPPED recovery test is a failure.
    assert "pytest-recovery-postgres.xml" in step["run"]
    assert "did not execute" in step["run"]


def test_an_observation_with_no_id_is_skipped_not_a_KeyError():
    """`payload["observation_id"]` in _evidence_signature raised KeyError, and
    persist_canonical_evidence computes the signature OUTSIDE the try that
    guards the insert — so one malformed observation aborted the whole run's
    evidence persistence rather than losing one row."""
    from services.audit_evidence_builder import _evidence_signature

    assert _evidence_signature(
        {"evidence_type": "selection_response", "payload": {}}
    ) == ""
    assert _evidence_signature(
        {"evidence_type": "selection_response", "payload": {"observation_id": "abc"}}
    ) == "selection_response:abc"
    # Every other evidence type still signs from its own fields.
    assert _evidence_signature(
        {"evidence_type": "grounding_chunk", "product_key": "p",
         "payload": {"host": "h", "excerpt_text": "e", "matched_url": "u"}}
    ) == "grounding_chunk|p|h|e|u"


async def test_a_signature_less_evidence_row_is_counted_and_the_rest_persist(
    monkeypatch,
):
    import services.audit_evidence_builder as eb

    rows = [
        {"evidence_type": "selection_response", "payload": {}, "confidence": 50},
        {"evidence_type": "selection_response",
         "payload": {"observation_id": "ok"}, "confidence": 50},
    ]
    inserted = []

    async def fake_extract(brand_report, content_key_map=None):
        return rows

    async def fake_insert(**kw):
        inserted.append(kw["payload"])
        return "evidence-1"

    monkeypatch.setattr(eb, "extract_evidence_items",
                        lambda *a, **kw: rows)
    monkeypatch.setattr("db.audit_evidence.insert_evidence_item", fake_insert)
    monkeypatch.setattr(eb, "extract_findings", lambda *a, **kw: [])
    monkeypatch.setattr(eb, "extract_actions", lambda *a, **kw: [])

    summary = await eb.persist_canonical_evidence(
        audit_run_id="r", merchant_id="m", brand_report={},
    )
    assert summary["evidence_items_skipped_unidentified"] == 1
    assert [p.get("observation_id") for p in inserted] == ["ok"]


# ---------------------------------------------------------------------------
# NOT-COMPARABLE MEANS NOT COMPARABLE — categorical movements included.
#
# The degrade loop used to run BEFORE the four categorical movements were
# appended, so on a pair whose basis had changed the payload shipped
# `verdict: changed / is_material: True / "Not yet visible -> Agent-ready"`
# directly beside three score movements reading `unknown` / `not_comparable`,
# and `material_movements: 1` under a headline saying no movement can be
# claimed either way. A categorical label is DERIVED from the scores under the
# run's methodology, so a methodology change makes it exactly as unclaimable.
# ---------------------------------------------------------------------------


def _delta_side(*, verdict: str, visibility: int) -> dict:
    """A report in the shape build_reaudit_delta reads, pinned to one prompt
    set so the pair fails comparability on the MEASUREMENT BASIS (the model)
    and not on the question set."""
    from tests.basis_fixtures import with_provider_models

    return with_provider_models({
        "prompt_basis": {"selected_set_id": "sel_pinned"},
        "verdict": {
            "label": verdict,
            "visibility_score": visibility,
            "attribution_score": 40,
            "category_visibility_score": 50,
        },
        "merchant_view": {
            "headline": {
                "verdict_label": verdict,
                "scores": {"visibility": visibility, "attribution": 40,
                           "category_visibility": 50},
            },
            "next_best_action": {
                "primary_gap": "get_indexed",
                "tracking_metrics": ["First-party citation rate."],
            },
        },
    })


async def _model_generation_changed_bases(report, monkeypatch):
    """(current, prior) bases that differ ONLY in the model that answered —
    the 2026-09-01 condition the contract was written for. Built by the real
    writer, then the prior side's model rolled back by hand, so the pair is
    non-comparable for one nameable reason."""
    import db.merchant_official_domains as mod
    from tests.basis_fixtures import writer_basis

    async def _domains(merchant_id):
        return []

    monkeypatch.setattr(mod, "list_official_domains", _domains)
    current = await writer_basis(report)
    prior = dict(current)
    prior["providers_and_models"] = {
        provider: {**spec, "model_id": f"{spec.get('model_id')}-previous"}
        for provider, spec in (current.get("providers_and_models") or {}).items()
    }
    assert prior["providers_and_models"], (
        "the fixture has no providers_and_models, so this pair would be "
        "non-comparable for a reason the test does not intend"
    )
    return current, prior


async def test_a_non_comparable_pair_claims_no_categorical_movement_either(
    monkeypatch,
):
    from services.audit_delta import build_reaudit_delta

    current = _delta_side(verdict="Agent-ready", visibility=62)
    prior = _delta_side(verdict="Not yet visible", visibility=41)
    current_basis, prior_basis = await _model_generation_changed_bases(
        current, monkeypatch
    )

    delta = build_reaudit_delta(
        current_report=current, prior_report=prior, prior_row={"run_id": "p"},
        days_since=30, current_basis=current_basis, prior_basis=prior_basis,
    )

    # The reason is the measurement basis, not the question set: the prompt
    # set id is identical on both sides.
    assert delta["measurement_basis"]["reason"] == "measurement_basis_changed"
    # The verdict genuinely changed, and it is still not claimable.
    by_signal = {m["signal"]: m for m in delta["movements"]}
    assert (by_signal["verdict"]["from"], by_signal["verdict"]["to"]) == (
        "Not yet visible", "Agent-ready",
    )
    for signal, movement in by_signal.items():
        assert movement["is_material"] is False, signal
        assert movement["direction"] == "unknown", signal
        assert movement["detection"]["verdict"] == "not_comparable", signal
    # ...and the headline agrees with every movement beside it.
    assert delta["headline"].startswith("Not comparable to your last audit")
    assert "Material change" not in delta["headline"]

    # The RETAINED path re-derives the same payload from the persisted delta.
    retained = _since_last_audit({"reaudit_delta": delta})
    assert retained["material_movements"] == 0
    assert retained["headline"].startswith("Not comparable to your last audit")
    assert all(m["is_material"] is False for m in retained["movements"])
    assert all(m["direction"] == "unknown" for m in retained["movements"])


def test_a_retained_categorical_change_on_a_stale_basis_is_not_a_movement():
    """The counter and the headline are produced by the SAME function and had
    to be made to agree there: a delta persisted under an older contract
    carries `is_material: True` on its categoricals, and `_since_last_audit`
    skipped every non-score signal."""
    persisted = {
        "is_first_audit": False,
        "days_since_last": 30,
        "headline": "Material change since your last audit 30 days ago: "
                    "changed: Verdict.",
        "measurement_basis": {"same": False,
                              "reason": "measurement_basis_changed"},
        "movements": [
            {"signal": "verdict", "label": "Verdict", "from": "Not yet visible",
             "to": "Agent-ready", "direction": "changed", "is_material": True},
        ],
    }
    out = _since_last_audit({"reaudit_delta": persisted})
    assert out["material_movements"] == 0
    assert out["movements"][0]["direction"] == "unknown"
    assert out["movements"][0]["detection"]["verdict"] == "not_comparable"
    assert out["headline"].startswith("Not comparable to your last audit")


async def test_a_producer_that_stamps_no_observation_ids_flips_the_degraded_dial(
    monkeypatch,
):
    """The SYSTEMATIC case, which is the one this dial exists for.

    `evidence_items_skipped_unidentified` counts rows dropped BEFORE the
    insert because the producer gave them no `observation_id`. It is a skip,
    not an insert failure, so it was not one of the counters
    `_persist_url_recovery` summed — and a transient insert error loses one
    row out of hundreds while a producer bug that stops stamping observation
    ids loses EVERY selection_response in the run. The quiet failure read
    `evidence_persistence_failed_or_skipped_total: 0`, `degraded: False`, and completed
    with no selection evidence at all.
    """
    from services.audit_run_worker import _persist_url_recovery

    _url_persistence_stubs(monkeypatch, canonical={
        "evidence_items_inserted": 0,
        "evidence_items_failed": 0,
        "findings_failed": 0,
        "actions_failed": 0,
        "evidence_items_skipped_unidentified": 412,
    })
    canonical, _ = await _persist_url_recovery(
        run_id="r", merchant_id="m", brand_report={})

    assert canonical["evidence_persistence_failed_or_skipped_total"] == 412
    assert canonical["evidence_persistence_degraded"] is True


def _boot_posture_log_call():
    """The `logger.info(...)` node that writes audit_scheduler's boot line.

    Located in the syntax tree rather than by grepping for a sentence: the
    claim is about a specific CALL — its format string and the arguments that
    fill it — and both halves have to be checked together or a placeholder can
    drift away from its value and raise at boot.
    """
    import ast

    import services.audit_scheduler as sched

    tree = ast.parse(inspect.getsource(sched))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "info"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("audit_scheduler: started with")
    ]
    assert len(calls) == 1, "expected exactly one scheduler boot-posture line"
    return calls[0]


@pytest.mark.parametrize("raw,posture", [
    # The value an operator most plausibly sets believing it arms the job.
    ("1", "OFF"),
    ("yes", "OFF"),
    ("True", "ON"),
    ("true", "ON"),
    (None, "OFF"),
])
def test_the_boot_line_shows_the_liveness_flag_raw_and_resolved(
    monkeypatch, raw, posture,
):
    """The boot line is the ONE place an operator can see what posture the
    process actually booted with, and this job's first prod run seeds
    merchant_official_domains for every merchant in the catalog. It named
    catalog_import_drain's resolved posture and said nothing at all about
    OFFICIAL_DOMAIN_LIVENESS_ENABLED, which arms on the literal "true" — so
    `=1` was an operator who believed they had armed a job that was still
    parked, with nothing in the logs to say otherwise.

    Rendered, not grepped: the format string is filled with the real argument
    expressions, so a placeholder that drifts away from its value fails here
    instead of raising at boot.
    """
    import ast
    import os as _os
    import re

    import jobs.official_domain_liveness as job

    if raw is None:
        monkeypatch.delenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("OFFICIAL_DOMAIN_LIVENESS_ENABLED", raw)

    call = _boot_posture_log_call()
    fmt = call.args[0].value
    assert "OFFICIAL_DOMAIN_LIVENESS_ENABLED" in fmt

    namespace = {
        "os": _os,
        "liveness_job_enabled": job.liveness_job_enabled,
        # Not this test's subject; pinned in its own line of the same log.
        "_catalog_import_drain_enabled": lambda: True,
    }
    values = tuple(
        eval(ast.unparse(arg), namespace) for arg in call.args[1:]  # noqa: S307
    )
    assert len(re.findall(r"%[rsd]", fmt)) == len(values), (
        "the boot line's placeholders and arguments have drifted apart — "
        "this raises at boot, in the one log line that says what posture the "
        "process started in"
    )

    line = fmt % values
    assert f"raw {raw!r}" in line
    assert f"OFFICIAL_DOMAIN_LIVENESS_ENABLED is literally 'true' — raw " \
           f"{raw!r}, resolved now as {posture}" in line
    # The parsed posture must agree with the job's own gate, not with a
    # second reading of the environment written into the log line.
    assert (posture == "ON") is job.liveness_job_enabled()
