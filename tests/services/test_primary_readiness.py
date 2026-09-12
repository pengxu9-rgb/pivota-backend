"""Handoff failure semantics; actual SQL/assembly is covered by the PG suite."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from services.catalog_enrichment_agent import primary_readiness as pr
from services import index_pipeline_state_service as ips
from services import product_quality_service as quality


class SourceDB:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=1)
        self.missing_source = False
        self.wrong_seed = False
        self.stale = None
        self.plan = {"pdps": [{"product_key": p} for p in ("listing-a", "listing-b")],
                     "seeds": [{"id": f"seed-{p}", "attached_product_key": p} for p in ("listing-a", "listing-b")]}
        self.sources = [{"product_key": p, "content_key": "shared-ck", "merchant_id": f"real-{p}",
            "platform": "external_seed", "source_product_id": f"native-{p}",
            "canonical_url": f"https://{p}.test/products/lip-oil", "title": "Brand Lip Oil",
            "description": "A source description with sufficient detail for the actual deterministic scorer.",
            "brand": "Brand", "product_type": "lip_oil", "category": "lip_oil", "category_path": "beauty/makeup/lip_oil",
            "image_url": "https://cdn.test/lip-oil.jpg", "price": 5, "raw_inci": None} for p in ("listing-a", "listing-b")]

    async def fetch_all(self, sql, values):
        if "FROM catalog_products" in sql:
            assert values["product_keys"] == ["listing-a", "listing-b"]
            return self.sources[:1] if self.missing_source else self.sources
        if "FROM external_product_seeds" in sql:
            return [{"id": s["id"], "attached_product_key": "unrelated" if self.wrong_seed else s["attached_product_key"],
                     "seed_data": {}, "status": "active"} for s in self.plan["seeds"]]
        raise AssertionError(sql)

    async def fetch_one(self, sql, values=None):
        if sql == "SELECT NOW() AS started_at, LOCALTIMESTAMP AS started_local_at":
            return {"started_at": self.now, "started_local_at": self.now.replace(tzinfo=None)}
        if "FROM product_quality_snapshot" in sql:
            assert values["merchant_id"].startswith("real-listing-")
            assert values["source_id"].startswith("native-listing-")
            return {"id": 1, "snapshot_date": self.old if self.stale == "quality" else self.now,
                    "content_quality_score": 50., "rules_version": "v3-six-components"}
        if "FROM agent_pdp_view" in sql:
            return {"refreshed_at": self.old if self.stale == "apv" else self.now}
        if "FROM index_pipeline_state" in sql:
            return {"serving_eligible": False, "index_eligible": False, "blocker_code": "low_quality",
                    "pipeline_stage": "extracted", "last_consolidated_at": self.old if self.stale == "index_state" else self.now}
        if "FROM catalog_row_trust" in sql:
            return {"serving_decision": "blocked", "updated_at": self.old}
        raise AssertionError(sql)


@pytest.fixture
def handoff(monkeypatch):
    db, events = SourceDB(), []

    async def score(**kwargs):
        assert kwargs["db"] is db and kwargs["recompute_eligibility"] is False
        assert kwargs["score_source_backed_components"] is True
        assert "seed_data" not in kwargs["payload"]  # No fabricated INCI/detail sections.
        events.append(("quality", kwargs["merchant_id"]))
        return {"content_quality_score": 50.}

    async def apv(ck, **kwargs):
        assert kwargs["db"] is db
        events.append(("apv", ck))
        return True

    async def recompute(ck, **kwargs):
        assert kwargs["db"] is db and kwargs["strict"] is True
        events.append(("index", ck))
        return False

    async def trust(**kwargs):
        assert kwargs["db"] is db
        events.append(("trust", kwargs["product_key"]))
        return True

    monkeypatch.setattr(pr, "full_quality_eval", AsyncMock(side_effect=score))
    monkeypatch.setattr(pr, "refresh_agent_pdp_view_for_content_key", AsyncMock(side_effect=apv))
    monkeypatch.setattr(pr, "recompute_serving_eligibility", AsyncMock(side_effect=recompute))
    monkeypatch.setattr(pr, "upsert_catalog_row_trust", AsyncMock(side_effect=trust))
    return db, events


@pytest.mark.asyncio
async def test_actual_identity_order_and_legitimate_policy_rejection(handoff):
    db, events = handoff
    result = await pr.materialize_primary_readiness(db.plan, db=db)
    assert result["status"] == "complete"
    assert result["serving_eligible_count"] == 0
    assert events == [("quality", "real-listing-a"), ("quality", "real-listing-b"),
                      ("apv", "shared-ck"), ("index", "shared-ck"),
                      ("trust", "listing-a"), ("trust", "listing-b")]
    assert all(p["policy_status"] == "blocked" for p in result["products"])
    assert all(p["stages"]["trust"]["write_outcome"] == "unchanged_policy" for p in result["products"])
    json.dumps(result)  # Queue/CLI require direct JSON serialization.


@pytest.mark.asyncio
@pytest.mark.parametrize("field,error", [("missing_source", "persisted_product_set_incomplete"),
                                        ("wrong_seed", "attached_source_seed_mismatch")])
async def test_no_identity_or_attached_seed_fallback(handoff, field, error):
    db, events = handoff
    setattr(db, field, True)
    with pytest.raises(pr.PrimaryReadinessIncomplete, match=error):
        await pr.materialize_primary_readiness(db.plan, db=db)
    assert events == []


@pytest.mark.asyncio
async def test_second_source_identity_error_keeps_correct_stage(handoff):
    db, events = handoff
    db.sources[1]["merchant_id"] = None
    with pytest.raises(pr.PrimaryReadinessIncomplete) as exc:
        await pr.materialize_primary_readiness(db.plan, db=db)
    assert exc.value.report["failed_stage"] == "source_readback"
    assert exc.value.report["failed_product_key"] == "listing-b"
    assert events == [("quality", "real-listing-a")]


@pytest.mark.asyncio
async def test_index_eligible_does_not_claim_public_when_trust_blocks(handoff, monkeypatch):
    db, _ = handoff
    original_fetch = db.fetch_one

    async def fetch(sql, values=None):
        row = await original_fetch(sql, values)
        if "FROM index_pipeline_state" in sql:
            row.update(serving_eligible=True, index_eligible=True, blocker_code="none")
        return row

    monkeypatch.setattr(db, "fetch_one", fetch)
    monkeypatch.setattr(pr, "recompute_serving_eligibility", AsyncMock(return_value=True))
    result = await pr.materialize_primary_readiness(db.plan, db=db)
    assert result["status"] == "complete"
    assert result["serving_eligible_count"] == 2
    assert result["public_policy_count"] == 0
    assert all(p["policy_status"] == "blocked" for p in result["products"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,func", [("quality", "full_quality_eval"),
    ("apv", "refresh_agent_pdp_view_for_content_key"), ("index_state", "recompute_serving_eligibility"),
    ("trust", "upsert_catalog_row_trust")])
async def test_stage_error_is_incomplete_not_policy_rejection(handoff, monkeypatch, stage, func):
    db, _ = handoff
    monkeypatch.setattr(pr, func, AsyncMock(side_effect=RuntimeError("database stage failed")))
    with pytest.raises(pr.PrimaryReadinessIncomplete) as exc:
        await pr.materialize_primary_readiness(db.plan, db=db)
    assert exc.value.report["failed_stage"] == stage
    assert exc.value.report["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["quality", "apv", "index_state"])
async def test_old_artifact_cannot_mask_failed_stage(handoff, stage):
    db, _ = handoff
    db.stale = stage
    with pytest.raises(pr.PrimaryReadinessIncomplete, match=f"{stage}_readback_stale"):
        await pr.materialize_primary_readiness(db.plan, db=db)


@pytest.mark.asyncio
async def test_trust_false_is_execution_failure_even_with_old_readback(handoff, monkeypatch):
    db, _ = handoff
    monkeypatch.setattr(pr, "upsert_catalog_row_trust", AsyncMock(return_value=False))
    with pytest.raises(pr.PrimaryReadinessIncomplete, match="trust_not_written"):
        await pr.materialize_primary_readiness(db.plan, db=db)


@pytest.mark.asyncio
async def test_quality_optional_db_does_not_touch_global_connection(monkeypatch):
    target, global_db = AsyncMock(), AsyncMock()
    monkeypatch.setattr(quality, "database", global_db)
    result = await quality.full_quality_eval("seller", "external_seed", "native", "default", {},
                                             db=target, recompute_eligibility=False)
    assert result["content_quality_score"] == 0
    target.execute.assert_awaited_once()
    global_db.execute.assert_not_awaited()
    target.fetch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_ips_raises_missing_inputs_legacy_still_returns_false(monkeypatch):
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(ips, "_fetch_eligibility_inputs", fetch)
    db = object()
    with pytest.raises(ValueError, match="eligibility_inputs_missing"):
        await ips.recompute_serving_eligibility("missing", db=db, strict=True)
    fetch.assert_awaited_with("missing", db=db)
    assert await ips.recompute_serving_eligibility("missing") is False


def test_timestamp_freshness_uses_actual_session_wall_clock():
    instant = datetime(2026, 9, 12, 20, tzinfo=timezone.utc)
    local = datetime(2026, 9, 12, 13)
    pr._fresh(local, instant, "quality", local)
    pr._fresh(instant, instant, "apv", local)
    with pytest.raises(ValueError, match="quality_readback_stale"):
        pr._fresh(local - timedelta(seconds=1), instant, "quality", local)


@pytest.mark.asyncio
async def test_optional_notification_failure_preserves_strict_core_result(monkeypatch):
    from services import indexnow
    from unittest.mock import Mock
    db = AsyncMock()
    db.fetch_one.return_value = None
    monkeypatch.setattr(ips, "_fetch_eligibility_inputs", AsyncMock(return_value=[{"source": "actual"}]))
    monkeypatch.setattr(ips, "_fetch_regression_domains", AsyncMock(return_value=set()))
    monkeypatch.setattr(ips, "_classify_content_key_rows", Mock(return_value={
        "serving_eligible": True, "pivota_signature_id": "sig_actual", "blocker_code": "none"}))
    upsert = AsyncMock()
    monkeypatch.setattr(ips, "_upsert_index_pipeline_state", upsert)
    monkeypatch.setattr(indexnow, "schedule_submit_url", Mock(side_effect=RuntimeError("task scheduling failed")))
    assert await ips.recompute_serving_eligibility("actual", db=db, strict=True) is True
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["db"] is db
