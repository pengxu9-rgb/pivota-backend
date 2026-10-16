"""The group-sibling SQL of offers.resolve (OFFERS_RESOLVE_GROUP_SEED_SIBLINGS) on POSTGRES.

tests/test_offers_resolve_group_siblings.py proves the behaviour with the reads faked by SQL shape,
so none of its statements reaches an engine. This file sends the three new statements through the
REAL handler to a real Postgres: the group's member read (`= ANY(:aliases)` list binds, ORDER BY a
boolean), the sibling seed read (three ANY arms padded with a typed "", the shared quarantine
anti-join), and the catalog arm's FRESH-PRICE term (`price_checked_at < CAST(:bind AS TIMESTAMPTZ)`,
ordered before price and before the LIMIT).

Runs in its own scratch schema, alone on the search_path, so it neither reads nor leaves rows in the
shared tables the other gate files use. The readiness gate is faked (it has its own suite); every
SQL statement of the lane is the route's own.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_SCHEMA = f"offers_group_siblings_{uuid.uuid4().hex[:8]}"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
CK = "ck_pg_grp"
BRAND_PK, BRAND_SIG = "prod::m_brand::shopify::serum", "sig_pg_grp_brand"
RETAILER_PK, RETAILER_SIG = "ext:retailer:pg-serum-sk", "sig_pg_grp_retailer"
RETAILER2_PK = "ext:retailer:pg-serum-ys"


@pytest.fixture(scope="module")
def schema_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    admin = create_engine(DATABASE_URL)
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    engine = create_engine(DATABASE_URL, connect_args={"options": f"-csearch_path={_SCHEMA}"})
    try:
        metadata.create_all(engine, tables=[metadata.tables[t] for t in (
            "catalog_products", "catalog_offers", "catalog_merchants", "catalog_skus")])
        with engine.begin() as c:
            for f in ("044_external_product_seeds.sql", "134_catalog_source_quarantine.sql",
                      "179_identity_resolution_d2.sql"):
                c.exec_driver_sql((_MIGRATIONS / f).read_text())
            # mig 246, as db/schema_guard heals it onto prod (the shared Table does not carry it).
            c.execute(text("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at TIMESTAMPTZ NULL"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        admin.dispose()


def _rows(engine, *, retailer_checked, retailer2_checked=None, brand_seed=True, resolved=True):
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    with engine.begin() as c:
        for t in ("catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants", "external_product_seeds",
                  "identity_resolution_proposals"):
            c.execute(text(f"DELETE FROM {t}"))
        # The engine joined both retailer listings to the brand's product (applied, same-size
        # strategy). Unresolved: the same two listings, one move REVERTED and one by another
        # strategy -- neither counts.
        moves = (
            ((RETAILER_PK, "applied", "cross_merchant_same_size"), (RETAILER2_PK, "applied", "cross_merchant_same_size"))
            if resolved else
            ((RETAILER_PK, "reverted", "cross_merchant_same_size"), (RETAILER2_PK, "applied", "same_url_dup"))
        )
        for i, (listing, status, strategy) in enumerate(moves):
            c.execute(text(
                "INSERT INTO identity_resolution_proposals (proposal_id, proposal_key, kind, strategy,"
                " resolver_version, content_key, subject_product_keys, keeper_product_key, member_fingerprint, status,"
                " evidence)"
                " VALUES (:id, :id, :kind, :strategy, 'test', :ck, :subjects, :keeper, 'fp', :status, cast(:ev AS jsonb))"
            ), {"id": f"irp_{i}", "strategy": strategy, "ck": CK, "subjects": [listing, BRAND_PK],
                "keeper": BRAND_PK, "status": status,
                # attach: evidence.listing_product_key; merge: evidence.from_product_keys (be#2637).
                "kind": "merge_content_key" if i == 1 else "attach_membership",
                "ev": (json.dumps({"from_product_keys": [listing], "from_content_key": "ck_old", "to_content_key": CK})
                       if i == 1 else json.dumps({"listing_product_key": listing, "from_content_key": "ck_old"}))})

        def product(pk, sig, merchant, source_ref, source_product_id):
            c.execute(text(
                "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
                " content_key, pivota_signature_id, catalog_track, truth_tier, readiness_tier, pdp_scope,"
                " pdp_lifecycle_stage, source_system, source_ref, updated_at)"
                " VALUES (:pk,:m,'external_seed',:spid,'Serum','Brand',:ck,:sig,'citation','primary',"
                "         'referral_only','multi_merchant_canonical','published','test',:sref,NOW())"
            ), {"pk": pk, "m": merchant, "spid": source_product_id, "ck": CK, "sig": sig, "sref": source_ref})
            c.execute(text(
                "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
                " source_variant_id, title, currency, updated_at)"
                " VALUES (:sk,:pk,:m,'external_seed',:spid,'v1','Serum','USD',NOW())"
            ), {"sk": f"{pk}::canonical", "pk": pk, "m": merchant, "spid": source_product_id})

        def offer(oid, pk, merchant, price, host, checked):
            c.execute(text(
                "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
                " VALUES (:m, :m, 'external_seed', 'active') ON CONFLICT DO NOTHING"), {"m": merchant})
            c.execute(text(
                "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
                " readiness_tier, offer_mode, channel, availability, currency, list_price, merchant_effective_price,"
                " offer_type, is_first_party, source_ref, offer_payload, updated_at, price_checked_at)"
                " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only','redirect',"
                "         'external_referral','in_stock','USD',:p,:p,'retailer',false,:dest,"
                "         cast(:payload AS jsonb), NOW(), :checked)"
            ), {"oid": oid, "sk": f"{pk}::canonical", "pk": pk, "m": merchant, "p": price,
                "dest": f"https://{host}/p/serum", "payload": '{"destination_url": "https://%s/p/serum"}' % host,
                "checked": checked})

        product(BRAND_PK, BRAND_SIG, "m_brand", "eps_pg_brand", "ext_pg_brand")
        product(RETAILER_PK, RETAILER_SIG, "agent_seed::retailer::stylekorean.com",
                "https://stylekorean.com/p/serum", RETAILER_PK)
        product(RETAILER2_PK, "sig_pg_grp_retailer_2", "agent_seed::retailer::yesstyle.com",
                "https://yesstyle.com/p/serum", RETAILER2_PK)
        # The retailer is CHEAPER and its price is the one that may be stale.
        offer("of_pg_sk", RETAILER_PK, "agent_seed::retailer::stylekorean.com", 12.0, "stylekorean.com",
              retailer_checked)
        offer("of_pg_ys", RETAILER2_PK, "agent_seed::retailer::yesstyle.com", 24.0, "yesstyle.com",
              retailer2_checked if retailer2_checked is not None else now - timedelta(days=1))
        if brand_seed:
            c.execute(text(
                "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
                " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
                " attached_product_key)"
                " VALUES ('eps_pg_brand','ext_pg_brand','US','*','https://brand.com/products/serum',"
                "         'https://brand.com/products/serum','brand.com','Serum',19,'USD','in_stock',"
                "         cast(:sd AS jsonb),'active',NULL)"
            ), {"sd": '{"variants": [{"variant_id": "SKU-30ML", "price_amount": 19, "price_currency": "USD",'
                      ' "availability": "in_stock"}]}'})
            # A SUPERSEDED seed of the same listing (a tombstone): never read, never served.
            c.execute(text(
                "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
                " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
                " attached_product_key)"
                " VALUES ('eps_pg_tomb','ext_pg_tomb','US','*','https://brand-old.com/products/serum',"
                "         'https://brand-old.com/products/serum','brand-old.com','Serum',5,'USD','in_stock',"
                "         '{}'::jsonb,'superseded',:pk)"
            ), {"pk": BRAND_PK})


def _resolve(monkeypatch, product_id, *, limit=10, flag="1", seed_reasons=()):
    import databases

    import db.database as dbmod
    import db.merchant_purchasability as mp
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write
    from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

    monkeypatch.setenv("OFFERS_RESOLVE_GROUP_SEED_SIBLINGS", flag)
    monkeypatch.setattr(dual_write, "_price_check_column", {"present": False, "checked": None})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(mp, "human_handoff_allowed", AsyncMock(return_value=True))

    async def gate(row, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": list(seed_reasons)})()

    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", gate)

    class _BT:
        def add_task(self, *a, **k):
            pass

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        monkeypatch.setattr(dbmod, "database", db)  # the handler imports it at call time
        monkeypatch.setattr(dual_write, "database", db)  # the price_checked_at column probe
        try:
            return await _handle_offers_resolve(
                OffersResolvePayload(product={"product_id": product_id}, limit=limit, market="US", tool="*",
                                     commerce_surface="agent_api"),
                None, _BT(),
            )
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _sources(res, name):
    return [s for s in res["metadata"]["sources"] if s["source"] == name]


def test_a_retailer_listing_reaches_the_brand_seed_on_postgres(schema_engine, monkeypatch):
    now = datetime.now(timezone.utc)
    _rows(schema_engine, retailer_checked=now - timedelta(days=1))
    for ident in (RETAILER_SIG, RETAILER_PK, BRAND_SIG, CK):
        res = _resolve(monkeypatch, ident)
        [grp] = _sources(res, "external_product_seeds_group_siblings")
        assert grp["status"] in ("ok", "empty") and grp["members"] == 3 and grp["resolved"] == 3, (ident, grp)
        hosts = sorted((o.get("execution_spec") or {}).get("merchant_domain") or o["url"].split("/")[2]
                       for o in res["offers"])
        assert hosts == ["brand.com", "stylekorean.com", "yesstyle.com"], (ident, hosts, res["metadata"]["sources"])
        assert _sources(res, "catalog_offers")[0]["status"] == "ok"
        assert res["best_price_offer_id"] == "of:catalog_offer:of_pg_sk", "fresh and cheapest"
        marked = sorted(o["offer_id"] for o in res["offers"] if o.get("price_comparable") is True)
        assert marked == ["of:catalog_offer:of_pg_sk", "of:catalog_offer:of_pg_ys",
                          "of:external_seed:eps_pg_brand:SKU-30ML"], marked
        assert all(o["price_checked_at"].endswith("+00:00") for o in res["offers"] if o.get("price_checked_at"))


def test_flag_off_sends_none_of_it(schema_engine, monkeypatch):
    _rows(schema_engine, retailer_checked=None)
    res = _resolve(monkeypatch, RETAILER_SIG, flag="0")
    assert not _sources(res, "external_product_seeds_group_siblings")
    assert "best_price_offer_id" not in res
    assert sorted(o["source"]["offer_id"] for o in res["offers"]) == ["of_pg_sk", "of_pg_ys"]


def test_the_limit_never_cuts_a_fresh_seller_for_a_cheaper_stale_one(schema_engine, monkeypatch):
    """ORDER BY fresh-before-price runs BEFORE the LIMIT. The brand seed is gated out, so the catalog
    arm alone answers; at limit 1 the never-checked $12 must lose to the fresh $24."""
    _rows(schema_engine, retailer_checked=None)
    res = _resolve(monkeypatch, RETAILER_SIG, limit=1, seed_reasons=("currency_mismatch",))
    assert [o["source"]["offer_id"] for o in res["offers"]] == ["of_pg_ys"]
    assert res["best_price_offer_id"] == "of:catalog_offer:of_pg_ys"
    # A price read 30 days ago is as stale as one never read.
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc) - timedelta(days=30))
    res = _resolve(monkeypatch, RETAILER_SIG, limit=1, seed_reasons=("currency_mismatch",))
    assert [o["source"]["offer_id"] for o in res["offers"]] == ["of_pg_ys"]
    # Both stale: price decides again, and the stale winner is listed but not best price.
    _rows(schema_engine, retailer_checked=None, retailer2_checked=datetime.now(timezone.utc) - timedelta(days=9))
    res = _resolve(monkeypatch, RETAILER_SIG, limit=1, seed_reasons=("currency_mismatch",))
    assert [o["source"]["offer_id"] for o in res["offers"]] == ["of_pg_sk"]
    assert res["offers"][0]["price_stale"] is True and res["best_price_offer_id"] is None


def test_the_statement_still_runs_before_the_column_is_healed(schema_engine, monkeypatch):
    from sqlalchemy import text

    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc))
    with schema_engine.begin() as c:
        c.execute(text("ALTER TABLE catalog_offers DROP COLUMN price_checked_at"))
    try:
        res = _resolve(monkeypatch, RETAILER_SIG)
        assert _sources(res, "catalog_offers")[0]["status"] == "ok"
        assert all(o.get("price_stale") for o in res["offers"] if o["offer_id"].startswith("of:catalog_offer:"))
        assert res["best_price_offer_id"] == "of:external_seed:eps_pg_brand:SKU-30ML"
    finally:
        with schema_engine.begin() as c:
            c.execute(text("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at TIMESTAMPTZ NULL"))


def test_a_withdrawn_listing_lends_its_seed_to_no_sibling(schema_engine, monkeypatch):
    """`scripts/withdraw_catalog_rows.py` takes a listing down by its suppression pair; its seed must
    not come back through a live sibling, and a withdrawn listing id widens to nothing."""
    from sqlalchemy import text

    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc))
    with schema_engine.begin() as c:
        c.execute(text("UPDATE catalog_products SET suppressed_at = NOW(), suppression_reason = 'test_takedown'"
                       " WHERE product_key = :pk"), {"pk": BRAND_PK})
    res = _resolve(monkeypatch, RETAILER_SIG)
    [grp] = _sources(res, "external_product_seeds_group_siblings")
    assert grp["members"] == 2 and grp["row_count"] == 0
    assert not any(o["offer_id"].startswith("of:external_seed:") for o in res["offers"])
    # The CALLER's listing withdrawn, its two siblings live: the withdrawn id answers no group.
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc))
    with schema_engine.begin() as c:
        c.execute(text("UPDATE catalog_products SET suppressed_at = NOW(), suppression_reason = 'test_takedown'"
                       " WHERE product_key = :pk"), {"pk": RETAILER_PK})
    res = _resolve(monkeypatch, RETAILER_SIG)
    assert not _sources(res, "external_product_seeds_group_siblings"), "no live anchor: no group"
    assert not any(o["offer_id"].startswith("of:external_seed:") for o in res["offers"])


def test_an_unresolved_group_reads_no_sibling_seed_on_postgres(schema_engine, monkeypatch):
    """Only a reverted move and another strategy's move on the key: not a group. The member and
    proposal reads run (and prepare on Postgres); no seed is read, nothing is best price."""
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc), resolved=False)
    for ident in (RETAILER_SIG, "sig_pg_grp_retailer_2", BRAND_SIG):
        res = _resolve(monkeypatch, ident)
        assert not _sources(res, "external_product_seeds_group_siblings"), ident
        assert "best_price_offer_id" not in res


def test_a_superseded_seed_is_never_served_through_a_sibling(schema_engine, monkeypatch):
    _rows(schema_engine, retailer_checked=datetime.now(timezone.utc))
    res = _resolve(monkeypatch, RETAILER_SIG)
    assert _sources(res, "external_product_seeds_group_siblings")[0]["row_count"] == 1
    assert not any("brand-old.com" in str(o.get("source")) for o in res["offers"])
