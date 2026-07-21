"""Convergence P1.1 — audit-door catalog rows carry the honest tier triple.

The audit intake previously relied on DB server-defaults for
catalog_track/truth_tier/readiness_tier, which stamp the FIRST-PARTY label
(internal_merchant/primary/commerce_ready) onto OBSERVED audit seeds. Covers:
  - INSERT values carry external_referral/observed/referral_only;
  - the ON CONFLICT set_ does NOT touch the tier columns (a re-audit must
    never downgrade a row a future graduation ladder advanced);
  - backfill script: provenance-keyed WHERE (platform='url_audit' + full
    untouched default triple), dry-run writes nothing, apply issues the UPDATE.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.audit_index_intake as intake  # noqa: E402


def _audit_product() -> Dict[str, Any]:
    # NB: audit_product_to_index_fields reads the brand from `vendor`.
    return {
        "title": "Gentle Cleanser",
        "vendor": "TestBrand",
        "pdp_url": "https://brand.example/products/gentle-cleanser",
        "description": "A cleanser observed during a URL audit.",
    }


@pytest.mark.asyncio
async def test_insert_carries_honest_tier_triple(monkeypatch: pytest.MonkeyPatch):
    executed: List[Any] = []

    async def fake_execute(stmt, *a, **kw):
        executed.append(stmt)
        return None

    async def fake_fetch_one(*a, **kw):
        return None

    async def fake_fetch_all(*a, **kw):
        return []

    from db.database import database

    monkeypatch.setattr(database, "execute", fake_execute)
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)
    # keep the unit test off the network-y/side paths
    monkeypatch.setattr(intake, "apply_audit_er_gate", _none_gate)
    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", _none_guard)

    await intake.upsert_audited_sku_to_index("merch_test", _audit_product())

    insert_stmt = _find_catalog_products_insert(executed)
    assert insert_stmt is not None, "catalog_products insert was not executed"

    compiled = insert_stmt.compile()
    params = compiled.params
    assert params.get("catalog_track") == "external_referral"
    assert params.get("truth_tier") == "observed"
    assert params.get("readiness_tier") == "referral_only"

    # ON CONFLICT must not re-assert tiers (no downgrade on re-audit).
    sql_text = str(compiled)
    on_conflict = sql_text.split("ON CONFLICT", 1)[1]
    for col in ("catalog_track", "truth_tier", "readiness_tier"):
        assert col not in on_conflict, f"{col} must not appear in ON CONFLICT set_"


async def _none_gate(fields):
    return {"action": "none", "content_key": fields.get("content_key")}


async def _none_guard(merchant_id, fields):
    return {"action": "proceed"}


def _find_catalog_products_insert(executed: List[Any]) -> Optional[Any]:
    for stmt in executed:
        table = getattr(stmt, "table", None)
        if getattr(table, "name", "") == "catalog_products":
            return stmt
    return None


@pytest.mark.asyncio
async def test_insert_carries_seller_identity_cross_for_third_party(monkeypatch: pytest.MonkeyPatch):
    """P1.2: seller resolved from the DESTINATION (claim-aware
    ensure_observed_seller), NOT from the auditing merchant. A merchant
    auditing a domain it has NOT verified-claimed → observed seller, seed_kind
    'cross' (the anti-mis-attribution guarantee — regression test for the
    tautological-'self' bug where the audit clobbers catalog_merchants.source_ref).
    ON CONFLICT is existing-first (write-once)."""
    executed: List[Any] = []

    async def fake_execute(stmt, *a, **kw):
        executed.append(stmt)
        return None

    async def fake_fetch_one(*a, **kw):
        return None

    async def fake_fetch_all(*a, **kw):
        return []

    from db.database import database
    import services.seller_identity as si

    async def fake_ensure(**kwargs):
        assert kwargs["brand"] == "TestBrand"
        assert kwargs["domain"] == "brand.example"
        # domain not claimed by the auditing merchant → observed seller id
        return "merch_obs_testbrand_example"

    monkeypatch.setattr(database, "execute", fake_execute)
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(si, "ensure_observed_seller", fake_ensure)
    monkeypatch.setattr(intake, "apply_audit_er_gate", _none_gate)
    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", _none_guard)

    await intake.upsert_audited_sku_to_index("merch_test", _audit_product())

    insert_stmt = _find_catalog_products_insert(executed)
    assert insert_stmt is not None
    params = insert_stmt.compile().params
    assert params.get("seller_ref") == "merch_obs_testbrand_example"
    assert params.get("seed_kind") == "cross"  # NOT 'self' — the key guarantee

    on_conflict = str(insert_stmt.compile()).lower().split("on conflict", 1)[1]
    assert "coalesce(catalog_products.seller_ref, excluded.seller_ref)" in on_conflict
    assert "coalesce(catalog_products.seed_kind, excluded.seed_kind)" in on_conflict


@pytest.mark.asyncio
async def test_seller_self_only_when_resolved_seller_is_the_merchant(monkeypatch: pytest.MonkeyPatch):
    """seed_kind='self' ONLY when the claim-aware resolver returns the auditing
    merchant itself (i.e. it verified-claimed the destination domain)."""
    executed: List[Any] = []

    async def fake_execute(stmt, *a, **kw):
        executed.append(stmt)
        return None

    async def fake_none(*a, **kw):
        return None

    async def fake_all(*a, **kw):
        return []

    from db.database import database
    import services.seller_identity as si

    async def fake_ensure(**kwargs):
        return "merch_test"  # merchant claimed this domain → resolver returns it

    monkeypatch.setattr(database, "execute", fake_execute)
    monkeypatch.setattr(database, "fetch_one", fake_none)
    monkeypatch.setattr(database, "fetch_all", fake_all)
    monkeypatch.setattr(si, "ensure_observed_seller", fake_ensure)
    monkeypatch.setattr(intake, "apply_audit_er_gate", _none_gate)
    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", _none_guard)

    await intake.upsert_audited_sku_to_index("merch_test", _audit_product())

    params = _find_catalog_products_insert(executed).compile().params
    assert params.get("seller_ref") == "merch_test"
    assert params.get("seed_kind") == "self"


@pytest.mark.asyncio
async def test_seller_derivation_failure_leaves_null_and_still_seeds(monkeypatch: pytest.MonkeyPatch):
    """No-fallback (ADR-009 D3): derivation failure → NULL/NULL, and the seed
    insert still proceeds — seller derivation must never break an audit."""
    executed: List[Any] = []

    async def fake_execute(stmt, *a, **kw):
        executed.append(stmt)
        return None

    async def fake_fetch(*a, **kw):
        return None

    async def fake_fetch_all(*a, **kw):
        return []

    from db.database import database
    import services.seller_identity as si

    async def boom(**kwargs):
        raise RuntimeError("identity service down")

    monkeypatch.setattr(database, "execute", fake_execute)
    monkeypatch.setattr(database, "fetch_one", fake_fetch)
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(si, "ensure_observed_seller", boom)
    monkeypatch.setattr(intake, "apply_audit_er_gate", _none_gate)
    monkeypatch.setattr(intake, "apply_audit_brand_fragmentation_guard", _none_guard)

    await intake.upsert_audited_sku_to_index("merch_test", _audit_product())

    insert_stmt = _find_catalog_products_insert(executed)
    assert insert_stmt is not None
    params = insert_stmt.compile().params
    assert params.get("seller_ref") is None
    assert params.get("seed_kind") is None


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch):
    from scripts import backfill_audit_seed_tier_labels as bf

    calls: Dict[str, List[str]] = {"execute": [], "fetch": []}

    class FakeRow(dict):
        pass

    async def fake_fetch_one(sql, *a, **kw):
        calls["fetch"].append(" ".join(str(sql).split()))
        return FakeRow(n=7)

    async def fake_fetch_all(sql, *a, **kw):
        calls["fetch"].append(" ".join(str(sql).split()))
        return []

    async def fake_execute(sql, *a, **kw):
        calls["execute"].append(" ".join(str(sql).split()))

    monkeypatch.setattr(bf, "database", type("D", (), {
        "fetch_one": staticmethod(fake_fetch_one),
        "fetch_all": staticmethod(fake_fetch_all),
        "execute": staticmethod(fake_execute),
    }))

    report = await bf.run_backfill(apply=False)

    assert report["apply"] is False
    assert report["mislabeled_rows"] == 7
    assert calls["execute"] == []  # dry-run: no UPDATE issued
    # provenance-keyed WHERE present in the count query
    assert any("platform = 'url_audit'" in q for q in calls["fetch"])
    assert any("catalog_track = 'internal_merchant'" in q for q in calls["fetch"])


@pytest.mark.asyncio
async def test_backfill_apply_issues_update_with_guarded_where(monkeypatch: pytest.MonkeyPatch):
    from scripts import backfill_audit_seed_tier_labels as bf

    executed: List[str] = []
    counts = iter([3, 0])  # pre-count, post-count

    class FakeRow(dict):
        pass

    async def fake_fetch_one(sql, *a, **kw):
        return FakeRow(n=next(counts))

    async def fake_fetch_all(sql, *a, **kw):
        return []

    async def fake_execute(sql, *a, **kw):
        executed.append(" ".join(str(sql).split()))

    monkeypatch.setattr(bf, "database", type("D", (), {
        "fetch_one": staticmethod(fake_fetch_one),
        "fetch_all": staticmethod(fake_fetch_all),
        "execute": staticmethod(fake_execute),
    }))

    report = await bf.run_backfill(apply=True)

    assert report["rows_updated"] == 3
    assert report["remaining_mislabeled"] == 0
    assert len(executed) == 1
    update_sql = executed[0]
    assert "SET catalog_track = 'external_referral'" in update_sql
    assert "truth_tier = 'observed'" in update_sql
    assert "readiness_tier = 'referral_only'" in update_sql
    # guarded: provenance + untouched-default triple only
    assert "platform = 'url_audit'" in update_sql
    assert "catalog_track = 'internal_merchant'" in update_sql
    assert "truth_tier = 'primary'" in update_sql
    assert "readiness_tier = 'commerce_ready'" in update_sql
    # and never touches a claimed / graduated row (would downgrade it)
    assert "claim_state = 'unclaimed'" in update_sql
    assert "pdp_scope = 'unverified'" in update_sql
