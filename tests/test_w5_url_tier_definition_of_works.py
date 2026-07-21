"""W5 P9 — URL-tier "definition of works" verification.

Makes the W5 URL-tier promise a CHECKED FACT, not a manual eyeball. The
invariant this suite pins:

    On a URL-tier (synthetic ``url_audit``) run, every rendered report CTA
    produces an observable side effect or an honest error — ZERO silent no-ops —
    and the audited SKU gets a live canonical PDP identity + an indexing request
    within one run cycle.

The pieces W5 shipped that this asserts against:
  - P3 (services/audit_index_intake.py): url_audit seeds mint a deterministic
    ``pivota_signature_id`` + ``pivota_canonical_url`` on the catalog_products row.
  - P4.1 (services/agent_center_bd_report_service.py ~L6495): the per-SKU report's
    indexing CTA is repointed off the ephemeral ``urlwedge:*`` key to the seed's
    REAL catalog product_key, which ``resolve_canonical_pdp_url`` resolves.
  - P4.2 (services/audit_run_worker.py): the synthetic completion path auto-submits
    the seed's stored canonical URL (self-gated on gsc_pivota_submit_enabled).
  - P6 (services/executor_agents/dispatcher.py): URL_AUDIT_EXECUTORS dispatched
    for synthetic runs; catalog-dependent executors (sitemap_freshness) excluded.

Style: fixture-style, deterministic, DB-less. The heavy per-SKU report builder
(build_per_sku_report) makes an LLM call (attach_sku_strategic_brief) and reads
many DB rows, so the CTA-resolvability assertions exercise the REAL load-bearing
helpers the report uses to stamp the CTA (_url_audit_seed_report_identity +
make_catalog_product_key — bd_report L6495-6515) plus the REAL resolver / real
public request_sku_indexing surface, rather than faking the whole builder.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from services.audit_index_intake import PLATFORM_URL_AUDIT, stable_source_id
from services.catalog_sync_service import (
    make_catalog_product_key,
    make_pivota_signature_id,
    pivota_canonical_pdp_url,
)

# --- Deterministic fixture identity for one pasted brand-surface URL ----------
MERCHANT = "m_p9_defworks"
BRAND_URL = "https://brand.example/products/heartleaf-toner"

SOURCE_ID = stable_source_id(BRAND_URL)
SEED_PK = make_catalog_product_key(MERCHANT, PLATFORM_URL_AUDIT, SOURCE_ID)
SIG = make_pivota_signature_id(MERCHANT, PLATFORM_URL_AUDIT, SOURCE_ID)
CANON_URL = pivota_canonical_pdp_url(SIG)


def _synthetic_item() -> Dict[str, Any]:
    """The synthetic product shape the url_audit route mints per pasted URL
    (routes/merchant_audit_routes.py step 4): sku_key == product_key ==
    ``urlwedge:<digest>`` (the EPHEMERAL key), plus the brand-surface
    canonical_url the seed is keyed on."""
    from routes.merchant_audit_routes import _synthetic_url_sku_key

    key = _synthetic_url_sku_key(MERCHANT, BRAND_URL)
    return {
        "sku_key": key,
        "product_key": key,
        "title": "Heartleaf 77% Soothing Toner",
        "raw_title": "Anua Heartleaf 77% Soothing Toner 250ml",
        "vendor": "Anua",
        "product_type": "Toner",
        "pdp_url": BRAND_URL,
        "canonical_url": BRAND_URL,
        "attributes_raw": {"size": "250ml"},
    }


def _wire_resolver_db(monkeypatch, *, product_row: Optional[Dict[str, Any]]):
    """Fake db.database.database.fetch_one for resolve_canonical_pdp_url:
    a url_audit seed has NO variant sku row (catalog_skus -> None), and its
    catalog_products row is returned only when keyed by the seed product_key."""
    import db.database as dbmod

    async def _fake_fetch_one(query, values=None):
        q = str(query)
        if "catalog_skus" in q:
            return None  # url_audit seed: no variant sku indirection
        if "catalog_products" in q:
            if values and values.get("product_key") == SEED_PK:
                return product_row
            return None
        return None

    monkeypatch.setattr(dbmod.database, "fetch_one", _fake_fetch_one)


# =====================================================================
# 1. Seed identity — the audited SKU's seed row carries sig + canonical
# =====================================================================


@pytest.mark.asyncio
async def test_seed_row_carries_pivota_signature_and_canonical_url(monkeypatch):
    """After the synthetic run's discovery hook seeds the index (intake ON), the
    audited SKU's seed carries a non-null pivota_signature_id + pivota_canonical_url,
    deterministic from make_pivota_signature_id(merchant, url_audit, source_id).
    Proves: the worker seeds on the brand-surface URL and the fields the intake
    persists carry the minted identity."""
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")
    import services.audit_index_intake as intake
    import services.audit_run_worker as worker

    captured: List[Any] = []

    async def _spy_upsert(merchant_id, product):
        captured.append((merchant_id, product))
        return "ck_x"

    monkeypatch.setattr(intake, "upsert_audited_sku_to_index", _spy_upsert)

    await worker._seed_url_audit_index(
        merchant_id=MERCHANT, synthetic_items=[_synthetic_item()],
    )

    assert len(captured) == 1
    cm, cp = captured[0]
    assert cm == MERCHANT
    # Seeded on the BRAND surface (canonical_url), threaded in as pdp_url.
    assert cp["pdp_url"] == BRAND_URL

    fields = intake.audit_product_to_index_fields(MERCHANT, cp)
    assert fields["pivota_signature_id"] == SIG
    assert fields["pivota_canonical_url"] == CANON_URL
    assert fields["product_key"] == SEED_PK


@pytest.mark.asyncio
async def test_seed_hook_seeds_unconditionally(monkeypatch):
    """W5 P2: url_audit seeding is the unconditional main line — the discovery
    seed hook mints a seed regardless of any env flag (ENABLE_AUDIT_INDEX_INTAKE
    is gone). The positive restatement of the old flag-off negative: with no flag
    set, the hook still calls the intake."""
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE_MERCHANT_IDS", raising=False)
    import services.audit_index_intake as intake
    import services.audit_run_worker as worker

    called: List[Any] = []

    async def _spy_upsert(merchant_id, product):
        called.append((merchant_id, product))
        return "ck_x"

    monkeypatch.setattr(intake, "upsert_audited_sku_to_index", _spy_upsert)
    await worker._seed_url_audit_index(
        merchant_id=MERCHANT, synthetic_items=[_synthetic_item()],
    )
    assert len(called) == 1
    assert called[0][0] == MERCHANT


# =====================================================================
# 2. CTA resolvability — the CORE invariant
# =====================================================================


@pytest.mark.asyncio
async def test_indexing_cta_targets_real_seed_and_resolves(monkeypatch):
    """With a seed (intake ON): the per-SKU report's indexing CTA target is the
    REAL seed product_key (NOT urlwedge:*), and resolve_canonical_pdp_url returns
    the seed's canonical URL — i.e. the rendered CTA actually resolves. Also the
    no-raw-key-leak guard (assertion 5): the CTA target a portal would POST
    carries no ``urlwedge:`` string.

    Reproduces the report's P4.1 CTA-stamping exactly (bd_report L6495-6515):
    _url_audit_seed_report_identity() -> pipe key -> make_catalog_product_key()."""
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")
    import services.agent_center_bd_report_service as bd

    item = _synthetic_item()
    sku_ctx = bd.build_synthetic_sku_context(item, MERCHANT)  # synthetic_url_audit=True
    product = sku_ctx["product"]

    # build_sku_next_best_action stamps the EPHEMERAL urlwedge key first.
    cta: Dict[str, Any] = {
        "action": "request_indexing",
        "target_sku_key": item["sku_key"],
    }
    assert cta["target_sku_key"].startswith("urlwedge:")

    # P4.1 repoint (the exact report logic): seed exists -> 3-part pipe key ->
    # real catalog product_key.
    seed_pk, _seed_ck = bd._url_audit_seed_report_identity(MERCHANT, product, sku_ctx)
    assert seed_pk == f"{MERCHANT}|{PLATFORM_URL_AUDIT}|{SOURCE_ID}"
    parts = seed_pk.split("|")
    assert len(parts) == 3
    cta["target_sku_key"] = make_catalog_product_key(*parts)

    # (2) CTA target is the REAL seed product_key, not the ephemeral key.
    assert cta["target_sku_key"] == SEED_PK
    # (5) no raw urlwedge key leaks into what the portal would POST.
    assert "urlwedge:" not in cta["target_sku_key"]

    # ...and it RESOLVES to the seed's canonical URL through the real resolver.
    _wire_resolver_db(
        monkeypatch,
        product_row={
            "pivota_canonical_url": CANON_URL,
            "pivota_signature_id": SIG,
            "content_key": "ck_x",
        },
    )
    from services.pivota_indexing_request import resolve_canonical_pdp_url

    url = await resolve_canonical_pdp_url(cta["target_sku_key"], MERCHANT)
    assert url == CANON_URL


@pytest.mark.asyncio
async def test_cta_without_seed_keeps_urlwedge_and_honest_errors(monkeypatch):
    """The honest-error half of the invariant: when a seed genuinely can't be
    keyed (no merchant_id / no resolvable seed URL — a real boundary, NOT a flag;
    W5 P2 made seeding unconditional), _url_audit_seed_report_identity returns
    None, so the CTA keeps its ephemeral urlwedge key, and the resolver honestly
    returns None -> request_sku_indexing yields ``no_canonical_url`` (not a
    crash). This is the 'honest error, zero silent no-op' half of the invariant."""
    import services.agent_center_bd_report_service as bd

    item = _synthetic_item()
    sku_ctx = bd.build_synthetic_sku_context(item, MERCHANT)
    product = sku_ctx["product"]

    # Genuine no-seed: an empty merchant_id can't key a seed (the real boundary
    # P2 preserved at bd L6313 `if not merchant_id`), so there's nothing to
    # repoint the CTA to.
    seed_pk, _seed_ck = bd._url_audit_seed_report_identity("", product, sku_ctx)
    assert seed_pk is None  # no merchant_id -> no seed to repoint to

    cta_target = item["sku_key"]  # stays the ephemeral key
    assert cta_target.startswith("urlwedge:")

    # Resolver + the real public endpoint surface honestly no-op (no crash).
    _wire_resolver_db(monkeypatch, product_row=None)
    from services.pivota_indexing_request import (
        request_sku_indexing,
        resolve_canonical_pdp_url,
    )

    assert await resolve_canonical_pdp_url(cta_target, MERCHANT) is None
    out = await request_sku_indexing(cta_target, MERCHANT)
    assert out["status"] == "no_canonical_url"  # honest error, not a raise


# =====================================================================
# 3. Executor dispatch — exactly URL_AUDIT_EXECUTORS, each an observable row
# =====================================================================


@pytest.mark.asyncio
async def test_synthetic_run_dispatches_exactly_url_audit_executor_set(monkeypatch):
    """A synthetic dispatch enqueues an executor_run row for EACH agent in
    URL_AUDIT_EXECUTORS (observable side effect) and — even with every agent's
    should_run forced True — does NOT enqueue the catalog-dependent
    sitemap_freshness_monitor. Proves the allowlist (not should_run) is what
    excludes catalog-coupled executors from the URL tier."""
    import db.executor_runs as er
    import db.merchant_portal_preferences as prefs
    import services.executor_agents.dispatcher as disp
    from services.executor_agents.base import ExecutorContext
    from services.executor_agents.canonical_pdp_enrichment import (
        CanonicalPdpEnrichmentAgent,
    )
    from services.executor_agents.competitor_insights import CompetitorInsightsAgent
    from services.executor_agents.content_brief import ContentBriefGeneratorAgent
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    from services.executor_agents.sitemap_freshness import SitemapFreshnessAgent

    enqueued: List[str] = []

    async def _fake_enqueue(
        *, agent_name, merchant_id, parent_audit_run_id, payload_jsonb,
        idempotency_key, max_retries, stage,
    ):
        enqueued.append(agent_name)
        return f"run::{agent_name}"

    async def _fake_find(*, idempotency_key):
        return None

    def _fake_idem(*, agent_name, merchant_id, parent_audit_run_id):
        return f"idem::{agent_name}"

    async def _fake_auto(mid):
        return True

    monkeypatch.setattr(er, "enqueue_executor_run", _fake_enqueue)
    monkeypatch.setattr(er, "find_in_flight_executor_run_by_idempotency", _fake_find)
    monkeypatch.setattr(er, "compute_executor_idempotency_key", _fake_idem)
    monkeypatch.setattr(prefs, "get_merchant_executor_auto_execute", _fake_auto)

    # Force EVERY agent's should_run True so nothing is excluded by self-gating —
    # the ONLY thing that may keep sitemap out is the allowlist.
    async def _yes(self, context):
        return True

    for cls in (
        GscUrlSubmissionAgent, SitemapFreshnessAgent, ContentBriefGeneratorAgent,
        CanonicalPdpEnrichmentAgent, CompetitorInsightsAgent,
    ):
        monkeypatch.setattr(cls, "should_run", _yes)

    ctx = ExecutorContext(
        merchant_id=MERCHANT, parent_audit_run_id="run_dw",
        audit_report={"per_sku_reports": []},
    )
    summary = await disp.dispatch_agents(ctx, agent_names=disp.URL_AUDIT_EXECUTORS)

    assert set(enqueued) == set(disp.URL_AUDIT_EXECUTORS)
    assert "sitemap_freshness_monitor" not in enqueued
    assert summary["dispatched_count"] == len(disp.URL_AUDIT_EXECUTORS)
    # each dispatched executor produced an executor_run row id (observable).
    assert all(r.get("run_id") for r in summary["runs"])


# =====================================================================
# 4. + worker wiring — indexing submission within one run cycle (end-to-end)
# =====================================================================


def _patch_synthetic_worker(monkeypatch, *, submit_flag: bool, seed_rows):
    """Drive process_one_audit_run down the synthetic (url_audit) path with
    record-only fakes for every heavy seam, letting the REAL
    _submit_url_audit_seed_canonical_urls run (with faked DB + GSC seams) so the
    submit fires as part of the actual completion path, not in isolation."""
    import db.database as dbmod
    import services.agent_center_bd_report_service as bd
    import services.audit_run_worker as worker
    import services.gsc_integration as gsc
    from config.settings import settings
    from db import merchant_audit_runs as mar
    from services.executor_agents.dispatcher import URL_AUDIT_EXECUTORS

    monkeypatch.setattr(
        settings, "gsc_pivota_submit_enabled", submit_flag, raising=False,
    )

    state: Dict[str, Any] = {
        "transitions": [], "materialize_calls": [], "submitted": [],
        "final_fields": 0, "url_executors": URL_AUDIT_EXECUTORS,
    }

    claim = {
        "run_id": "run_dw", "merchant_id": MERCHANT,
        "product_keys": [], "stage": "queued",
        "partial_result_jsonb": {
            "launch": {"synthetic_products": [_synthetic_item()]},
        },
    }

    async def _claim(*, worker_id):
        return claim

    async def _transition(*, run_id, from_stage, to_stage, worker_id, **kw):
        state["transitions"].append((from_stage, to_stage))
        return True

    async def _partial(**kw):
        return True

    async def _lease(**kw):
        return True

    async def _persist(**kw):
        return True

    async def _fetch_by_id(*, run_id):
        return {"cancelled_at": None}

    async def _recent(**kw):
        return []

    monkeypatch.setattr(mar, "claim_next_pending_run", _claim)
    monkeypatch.setattr(mar, "transition_stage", _transition)
    monkeypatch.setattr(mar, "record_partial_result", _partial)
    monkeypatch.setattr(mar, "extend_lease", _lease)
    monkeypatch.setattr(mar, "persist_report_jsonb", _persist)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", _fetch_by_id)
    monkeypatch.setattr(mar, "recent_runs_for_merchant", _recent)

    async def _resolve_syn(*, launch_options, merchant_id):
        return (
            "Anua", "brand.example",
            [{
                "title": "Heartleaf 77% Soothing Toner",
                "product_key": _synthetic_item()["product_key"],
                "canonical_url": BRAND_URL,
            }],
            [], None,
        )

    monkeypatch.setattr(worker, "_resolve_synthetic_url_products", _resolve_syn)

    async def _seed(**kw):
        return None  # seed identity is asserted in test 1; stub here

    monkeypatch.setattr(worker, "_seed_url_audit_index", _seed)

    async def _run_brand_report(**kw):
        # No mock markers -> _detect_mock_audit_output returns [].
        return {
            "aggregate": {"products_succeeded": 1, "products_failed": 0},
            "per_product": [], "per_sku_reports": [],
        }

    monkeypatch.setattr(bd, "run_brand_report", _run_brand_report)

    async def _materialize(**kw):
        state["materialize_calls"].append(kw)
        return {
            "tasks_materialized": 0,
            "executors_dispatched": len(URL_AUDIT_EXECUTORS),
        }

    monkeypatch.setattr(worker, "_materialize_tasks_and_executors", _materialize)

    async def _final(**kw):
        state["final_fields"] += 1

    monkeypatch.setattr(worker, "_record_final_report_fields", _final)

    async def _cost(**kw):
        return {}

    monkeypatch.setattr(worker, "_aggregate_cost_summary_for_run", _cost)

    # REAL _submit_url_audit_seed_canonical_urls runs; fake only its DB + GSC.
    async def _fetch_all(query):
        return seed_rows

    monkeypatch.setattr(dbmod.database, "fetch_all", _fetch_all)

    async def _submit(*, merchant_id, urls, audit_run_id=None):
        state["submitted"].append(
            {"merchant_id": merchant_id, "urls": urls, "audit_run_id": audit_run_id},
        )
        return [{"status": "submitted", "url": u} for u in urls]

    monkeypatch.setattr(gsc, "submit_pivota_canonical_urls", _submit)
    return state


@pytest.mark.asyncio
async def test_end_to_end_synthetic_run_submits_seed_url_when_flag_on(monkeypatch):
    """A full synthetic run: materializing dispatches the URL-tier executor set
    (dispatch_only), and within the SAME cycle the verifying completion path
    submits the seed's STORED canonical URL. The 'indexing request within one
    run cycle' half of the invariant."""
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    from services.executor_agents.dispatcher import URL_AUDIT_EXECUTORS

    state = _patch_synthetic_worker(
        monkeypatch, submit_flag=True,
        seed_rows=[{"pivota_canonical_url": CANON_URL}],
    )
    from services.audit_run_worker import process_one_audit_run

    processed = await process_one_audit_run()
    assert processed is True
    assert ("verifying", "completed") in state["transitions"]

    # materializing dispatched exactly the URL-tier set in dispatch_only mode.
    assert len(state["materialize_calls"]) == 1
    mc = state["materialize_calls"][0]
    assert mc["dispatch_only"] is True
    assert set(mc["agent_names"]) == set(URL_AUDIT_EXECUTORS)

    # ...and the seed's stored canonical URL was submitted within the run cycle.
    assert len(state["submitted"]) == 1
    assert state["submitted"][0]["urls"] == [CANON_URL]
    assert state["submitted"][0]["audit_run_id"] == "run_dw"


@pytest.mark.asyncio
async def test_end_to_end_synthetic_run_inert_when_flag_off(monkeypatch):
    """Flag off: the same run completes but the submit hook is inert (no GSC
    call). Proves the auto-submit lands INERT until gsc_pivota_submit_enabled
    flips, without altering the rest of the completion path."""
    monkeypatch.delenv("ENABLE_AUDIT_INDEX_INTAKE", raising=False)
    state = _patch_synthetic_worker(
        monkeypatch, submit_flag=False,
        seed_rows=[{"pivota_canonical_url": CANON_URL}],
    )
    from services.audit_run_worker import process_one_audit_run

    processed = await process_one_audit_run()
    assert processed is True
    assert ("verifying", "completed") in state["transitions"]
    assert state["submitted"] == []  # inert


# =====================================================================
# 5. CTA-actionability manifest — every rendered CTA action has a live
#    URL-tier backend (else FAIL with the offending action).
# =====================================================================


@pytest.mark.asyncio
async def test_url_tier_cta_action_manifest_all_actionable(monkeypatch):
    """Enumerate the CTA action types a synthetic per-SKU report can render
    (next_best_action.SKU_CTA_ACTIONS) and assert each maps to a live backend
    surface for the URL tier:
        request_indexing  -> request-indexing endpoint resolves (seed -> URL)
                             + a gsc submission executor exists in the URL tier
        request_enrichment-> a real seed product_key exists to enrich/attach
                             proof to + canonical_pdp_enrichment runs for URL runs
        none              -> monitor-only; inert BY DESIGN (honest no-op)
    A rendered CTA action with no live URL-tier backend is exactly the class of
    silent-no-op bug this packet exists to catch -> FAIL and name it."""
    monkeypatch.setenv("ENABLE_AUDIT_INDEX_INTAKE", "1")
    import services.executor_agents.dispatcher as disp
    from services.next_best_action import SKU_CTA_ACTIONS
    from services.pivota_indexing_request import resolve_canonical_pdp_url

    _wire_resolver_db(
        monkeypatch,
        product_row={
            "pivota_canonical_url": CANON_URL,
            "pivota_signature_id": SIG,
            "content_key": "ck_x",
        },
    )

    findings: List[str] = []
    for action in SKU_CTA_ACTIONS:
        if action == "request_indexing":
            url = await resolve_canonical_pdp_url(SEED_PK, MERCHANT)
            if not url:
                findings.append(
                    f"{action}: request-indexing does not resolve a URL-tier seed",
                )
            if "gsc_url_submission_loop" not in disp.URL_AUDIT_EXECUTORS:
                findings.append(
                    f"{action}: no gsc submission executor in URL_AUDIT_EXECUTORS",
                )
        elif action == "request_enrichment":
            if not SEED_PK or SEED_PK.startswith("urlwedge:"):
                findings.append(
                    f"{action}: no real seed product_key to enrich",
                )
            if "canonical_pdp_enrichment" not in disp.URL_AUDIT_EXECUTORS:
                findings.append(
                    f"{action}: no enrichment executor in URL_AUDIT_EXECUTORS",
                )
        elif action == "none":
            pass  # monitor-only: inert by design, not a silent no-op
        else:
            findings.append(
                f"{action}: unknown CTA action with no URL-tier backend mapping",
            )

    assert findings == [], (
        "URL-tier CTA actions with no live backend surface (silent no-op class): "
        f"{findings}"
    )
