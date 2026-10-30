from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.ops.relgraph_freshness_refresh import run, validate_manifest


def manifest(**overrides):
    return {"schema": "relgraph.freshness_refresh.v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "market": "US", "currency": "USD", "max_products": 2, "product_keys": ["P1", "P2"], **overrides}


async def fake_plan(_db, _manifest):
    return {"page_keys": ["P1", "P2"], "seed_ids": ["seed_1", "seed_2"], "matched_products": 2}


def planned_then_settled():
    calls = 0
    async def planner(db, value):
        nonlocal calls
        calls += 1
        return await fake_plan(db, value) if calls == 1 else {"page_keys": [], "seed_ids": [], "matched_products": 2}
    return planner


def test_manifest_refuses_old_future_oversized_and_market_currency_mismatch():
    for value in [manifest(currency="SGD"), manifest(market="ZZ"), manifest(max_products=201),
                  manifest(product_keys=["P1", "P1"]), manifest(url="https://not-accepted.example"),
                  manifest(generated_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat()),
                  manifest(generated_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())]:
        with pytest.raises(ValueError):
            validate_manifest(value)


@pytest.mark.asyncio
async def test_default_no_write_no_origin_and_private_plan_not_in_output():
    async def forbidden(**kwargs):
        raise AssertionError("unexpected validator invocation")
    summary = await run(None, manifest(), plan_fn=fake_plan, batch_fn=forbidden, page_fn=forbidden)
    assert summary["status"] == "dry_run"
    assert summary["dry_run"] is True
    assert summary["page_checks_due"] == summary["origin_checks_due"] == 2
    assert all(secret not in json.dumps(summary) for secret in ("P1", "seed_1", "http"))


@pytest.mark.asyncio
async def test_apply_requires_explicit_authorization_and_dual_projection_before_any_validator():
    async def forbidden(**kwargs):
        raise AssertionError("unexpected validator invocation")
    with pytest.raises(ValueError, match="authorization"):
        await run(None, manifest(), apply=True, batch_fn=forbidden, plan_fn=forbidden)
    with pytest.raises(ValueError, match="dual_write"):
        await run(None, manifest(), apply=True, authorized=True, plan_fn=fake_plan,
                  batch_fn=forbidden, dual_write_fn=lambda: False)


@pytest.mark.asyncio
async def test_owner_calls_are_bounded_and_output_strips_prices_hosts_urls_and_raw_errors():
    calls = []
    async def batch(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "attempted_count": 2, "origin_reads": 2, "projections_attempted": 2,
                "projections_written": 2, "price_unchanged": 2, "errors": [{"url": "secret", "price": 42}],
                "top_degraded_hosts": {"private.example": 3}}
    async def pages(keys, **kwargs):
        calls.append(keys)
        return 2
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=planned_then_settled(),
                        batch_fn=batch, page_fn=pages, dual_write_fn=lambda: True)
    assert calls[0]["candidate_seed_ids"] == ["seed_1", "seed_2"]
    assert calls[0]["budget_seconds"] == 120
    assert calls[1] == ["P1", "P2"]
    assert summary["complete"] is True
    assert summary["status"] == "success"
    assert summary["freshness_rechecked"] is True and summary["remaining_offer_checks_due"] == 0
    assert "secret" not in json.dumps(summary) and "private.example" not in json.dumps(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("residue", [
    {"skipped_for_budget": 1}, {"refreshed_from_cache": 1}, {"skipped_for_host_backoff": 1},
    {"projections_written": 0}, {"projections_errored": 1}, {"origin_reads": 1},
    {"price_skipped_currency_mismatch": 1},
    {"attempted_count": 3}, {"origin_reads": 3}, {"projections_written": 3}, {"projections_attempted": 3},
])
async def test_incomplete_owner_outcome_is_degraded_even_when_owner_says_success(residue):
    async def batch(**kwargs):
        return {"status": "success", "attempted_count": 2, "origin_reads": 2,
                "projections_attempted": 2, "projections_written": 2, **residue}
    async def pages(*args, **kwargs):
        return 2
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=planned_then_settled(),
                        batch_fn=batch, page_fn=pages, dual_write_fn=lambda: True)
    assert summary["status"] == "degraded"
    assert summary["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("post", [
    {"page_keys": ["P1"], "seed_ids": [], "matched_products": 2},
    {"page_keys": [], "seed_ids": ["seed_1"], "matched_products": 2},
    {"page_keys": [], "seed_ids": [], "matched_products": 2, "unrefreshable_products": 1},
    {"page_keys": [], "seed_ids": [], "matched_products": 1},
    {"page_keys": [], "seed_ids": [], "matched_products": 2, "offer_due_products": 1},
])
async def test_success_counters_cannot_substitute_for_post_owner_freshness(post):
    plans = 0
    async def planner(db, value):
        nonlocal plans
        plans += 1
        return await fake_plan(db, value) if plans == 1 else post
    async def batch(**kwargs):
        return {"status": "success", "attempted_count": 2, "origin_reads": 2,
                "projections_attempted": 2, "projections_written": 2}
    async def pages(*args, **kwargs):
        return 2
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=planner,
                        batch_fn=batch, page_fn=pages, dual_write_fn=lambda: True)
    assert summary["complete"] is False and summary["status"] == "degraded" and plans == 2
    assert summary["freshness_rechecked"] is True
    assert all(secret not in json.dumps(summary) for secret in ("P1", "seed_1", "http"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout", "overcount"])
async def test_post_owner_recheck_is_bounded_no_retry_and_unknown_is_degraded(monkeypatch, failure):
    import scripts.ops.relgraph_freshness_refresh as mod
    owner_wait = asyncio.wait_for
    timeouts = []
    def wait(future, timeout):
        timeouts.append(timeout)
        return owner_wait(future, timeout=0.001 if failure == "timeout" and len(timeouts) == 3 else timeout)
    monkeypatch.setattr(mod.asyncio, "wait_for", wait)
    plans = 0
    async def planner(db, value):
        nonlocal plans
        plans += 1
        if plans == 1:
            return await fake_plan(db, value)
        if failure == "timeout":
            await asyncio.sleep(10)
        if failure == "overcount":
            return {"page_keys": [], "seed_ids": [], "matched_products": 3}
        raise RuntimeError("https://private.example?price=42")
    async def batch(**kwargs):
        return {"status": "success", "attempted_count": 2, "origin_reads": 2,
                "projections_attempted": 2, "projections_written": 2}
    async def pages(*args, **kwargs):
        return 2
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=planner,
                        batch_fn=batch, page_fn=pages, dual_write_fn=lambda: True)
    assert summary["complete"] is False and summary["status"] == "degraded" and plans == 2
    assert timeouts == [120, 30, 30]
    assert summary["freshness_rechecked"] is False and summary["remaining_offer_checks_due"] is None
    assert "private" not in json.dumps(summary)


@pytest.mark.asyncio
async def test_owner_failure_is_not_retried_or_logged_as_success():
    calls = 0
    async def batch(**kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("https://private.example?credential=secret")
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=fake_plan,
                        batch_fn=batch, dual_write_fn=lambda: True)
    assert calls == 1
    assert summary["status"] == "failed"
    assert summary["partial_writes_possible"] is True
    assert "private" not in json.dumps(summary)


@pytest.mark.asyncio
async def test_native_or_unowned_offer_cannot_claim_complete_freshness():
    async def work(*args):
        return {"page_keys": [], "seed_ids": [], "matched_products": 1, "unrefreshable_products": 1}
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=work,
                        dual_write_fn=lambda: True)
    assert summary["complete"] is False and summary["status"] == "degraded"


@pytest.mark.asyncio
async def test_products_excluded_at_apply_are_counted_and_not_reported_as_refreshed():
    async def work(*args):
        return {"page_keys": [], "seed_ids": [], "matched_products": 0}
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=work,
                        dual_write_fn=lambda: True)
    assert summary["complete"] is False and summary["status"] == "degraded"
    assert summary["excluded_products"] == 2


@pytest.mark.asyncio
async def test_total_deadline_cancels_slow_owner_without_retry(monkeypatch):
    import scripts.ops.relgraph_freshness_refresh as mod
    owner = asyncio.wait_for
    monkeypatch.setattr(mod.asyncio, "wait_for", lambda future, timeout: owner(future, timeout=0.001))
    async def slow(**kwargs):
        await asyncio.sleep(10)
    summary = await run(None, manifest(), apply=True, authorized=True, plan_fn=fake_plan,
                        batch_fn=slow, dual_write_fn=lambda: True)
    assert summary["status"] == "timed_out"
    assert summary["partial_writes_possible"] is True
    assert summary["page_written"] == 0


@pytest.mark.asyncio
async def test_targeted_selector_rechecks_currency_market_and_original_guards(monkeypatch):
    import services.external_referral_readiness as owner
    calls = []
    async def fetch(sql, values):
        calls.append((sql, values))
        return [{"id": "seed_1", "market": "US", "price_currency": "USD", "is_fresh": 0}]
    monkeypatch.setattr(owner.database, "fetch_all", fetch)
    assert await owner.get_external_referral_refresh_candidate_seed_ids(
        seed_ids=["seed_1"], market="US", limit=1) == ["seed_1"]
    sql, values = calls[0]
    assert "catalog_source_quarantine" in sql and "suppressed_at" in sql
    assert "upper(trim(market)) = :refresh_market" in sql
    assert values["refresh_currency"] == "USD" and values["refresh_seed_0"] == "seed_1"
    with pytest.raises(ValueError):
        await owner.get_external_referral_refresh_candidate_seed_ids(seed_ids=["seed_1"] * 201, market="ZZ")
