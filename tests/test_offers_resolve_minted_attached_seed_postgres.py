"""offers.resolve for a MINTED catalog row, on POSTGRES (OFFERS_RESOLVE_ATTACHED_SEED_ARM).

A row the enrichment drain mints has a NULL `source_ref` and a title-slug `source_product_id`; its
seed is linked only by the seed's `attached_product_key` = the row's `product_key`. The signature
arm joins on the first two, so `offers.resolve {product_id: sig_...}` found no seed for such a row
while the product page of the same signature served a price. The flag adds the third join.

The minted rows here are NOT hand-built: the drain's own planner
(`catalog_enrichment_agent.ingestion.ingest_validated_jsonl`) plans them and its own writer
(`apply.apply_ingest_plan`) writes them, so the fixture has the shape the producer emits (asserted
in `test_the_fixture_is_the_shape_the_drain_mints`). The MIRROR row is written by hand in the shape
the mirror job leaves (`source_ref` = the seed id): its producer is a batch script over the whole
seed table.

The handler, every statement of the seed lane and the readiness gate are the route's own. The seed
is stamped the way the two refresh jobs stamp one they have just read (`_stamp_fresh`), because a
seed nobody has re-read is held back for a live check by design -- which is its own case below.

Runs in its own scratch schema, alone on the search_path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_SCHEMA = f"offers_minted_seed_{uuid.uuid4().hex[:8]}"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"
FLAG = "OFFERS_RESOLVE_ATTACHED_SEED_ARM"
ARM = "external_seed_by_signature_attached_key"
HOST = "brand-x.example"
URL = f"https://{HOST}/products/calm-barrier-cream"

# The mirror row, as the mirror job leaves one: its key and source_product_id carry the seed's
# external id, its source_ref the seed's row id.
MIRROR_SIG = "sig_" + "5e" * 16
MIRROR_EXT = "ext_" + "ab" * 12
MIRROR_SEED = "eps_" + "cd" * 12
MIRROR_PK = f"prod::external_seed::external_seed::{MIRROR_EXT}"
MIRROR_URL = "https://shop-a.example/products/day-cream"


def _record(*, currency="USD", in_stock=True):
    """One validated enrichment record: a brand store's own listing of one product."""
    return {
        "pdp": {
            "brand": "Brand X",
            "product_name": "Calm Barrier Cream",
            "category_path": "beauty/skincare/moisturizer",
            "currency": currency,
            "source_domain": HOST,
        },
        "offers": [{
            "merchant_inferred": "Brand X",
            "destination_url": URL,
            "canonical_url": URL,
            "image_url": f"https://{HOST}/img/calm-barrier-cream.jpg",
            "price": 21.0,
            "in_stock": in_stock,
            "validated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    }


@pytest.fixture(scope="module")
def schema_engine():
    import db.brand_claims  # noqa: F401 -- the writer's seller-of-record lookup reads it
    import db.catalog  # noqa: F401
    import db.products  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
    engine = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT",
                           connect_args={"options": f"-csearch_path={_SCHEMA}"})
    try:
        metadata.create_all(engine)
        with engine.connect() as c:
            # 045: the writer mints the product's singleton group.
            for f in ("044_external_product_seeds.sql", "045_product_groups.sql",
                      "134_catalog_source_quarantine.sql", "200_external_seed_destination_liveness.sql"):
                c.exec_driver_sql((_MIGRATIONS / f).read_text().replace("%", "%%"))
            # As db/schema_guard heals them onto prod (no migration file carries them).
            c.execute(text("ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seller_ref TEXT,"
                           " ADD COLUMN IF NOT EXISTS seed_kind TEXT"))
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        admin.dispose()


def _run(monkeypatch, body):
    """Run `body(db)` with every module-held handle of the global database pointed at the scratch
    schema: the writer and the handler both reach the database through module globals."""
    import databases

    import db.database as dbmod

    async def go():
        scratch = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await scratch.connect()
        original = dbmod.database
        for module in list(sys.modules.values()):
            for attr, value in list(getattr(module, "__dict__", {}).items()):
                if value is original:
                    monkeypatch.setattr(module, attr, scratch)
        try:
            return await body(scratch)
        finally:
            await scratch.disconnect()

    return asyncio.run(go())


def _clear(engine):
    from sqlalchemy import text

    with engine.connect() as c:
        for t in ("catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants",
                  "external_product_seeds"):
            c.execute(text(f"DELETE FROM {t}"))


def _stamp_fresh(engine, seed_id):
    """What the seed refresh job and the destination sweep leave on a seed they have just read."""
    from sqlalchemy import text

    now = datetime.now(timezone.utc)
    with engine.connect() as c:
        c.execute(text(
            "UPDATE external_product_seeds SET destination_checked_at = :now, destination_verdict = 'live',"
            " destination_http_status = 200, destination_failure_streak = 0,"
            " seed_data = jsonb_set(seed_data, '{snapshot}', cast(:snap AS jsonb), true)"
            " WHERE id = :id"
        ), {"now": now, "id": seed_id, "snap": json.dumps({"extracted_at": now.isoformat()})})


def _mint(schema_engine, monkeypatch, *, fresh=True, **record):
    """The drain's planner and writer, for real. Returns (signature, product_key, seed_id, merchant)."""
    import services.catalog_enrichment_agent.apply as apply_mod
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    plan = ingest_validated_jsonl([_record(**record)])

    async def body(db):
        return await apply_mod.apply_ingest_plan(plan, batch_label="minted-attached-seed-test", db=db)

    counts = _run(monkeypatch, body)
    assert counts["pdps"] == 1 and counts["seeds"] == 1, counts
    pdp, seed = plan["pdps"][0], plan["seeds"][0]
    if fresh:
        _stamp_fresh(schema_engine, seed["id"])
    return pdp["pivota_signature_id"], pdp["product_key"], seed["id"], pdp["merchant_id"]


def _mirror(engine):
    from sqlalchemy import text

    with engine.connect() as c:
        c.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
            " pivota_signature_id, catalog_track, truth_tier, readiness_tier, pdp_scope, pdp_lifecycle_stage,"
            " source_system, source_ref, updated_at)"
            " VALUES (:pk, 'external_seed', 'external_seed', :spid, 'Day Cream', 'Brand Y', :sig,"
            "         'external_referral', 'observed', 'referral_only', 'unverified', 'published',"
            "         'external_product_seeds_mirror_v1', :sref, NOW())"
        ), {"pk": MIRROR_PK, "spid": MIRROR_EXT, "sig": MIRROR_SIG, "sref": MIRROR_SEED})
        c.execute(text(
            "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
            " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
            " attached_product_key)"
            " VALUES (:id, :epid, 'US', '*', :url, :url, 'shop-a.example', 'Day Cream', 18, 'USD', 'in_stock',"
            "         cast(:sd AS jsonb), 'active', :pk)"
        ), {"id": MIRROR_SEED, "epid": MIRROR_EXT, "url": MIRROR_URL, "pk": MIRROR_PK,
            "sd": json.dumps({"title": "Day Cream", "brand": "Brand Y", "variants": [
                {"variant_id": "4400000000001", "price_amount": 18, "price_currency": "USD",
                 "currency": "USD", "availability": "in_stock"}]})})
    _stamp_fresh(engine, MIRROR_SEED)


def _resolve(monkeypatch, product_id, *, flag, merchant_id=None, market="US", live_opt_in=False):
    import db.merchant_purchasability as mp
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write
    from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    monkeypatch.setattr(dual_write, "_price_check_column", {"present": False, "checked": None})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(mp, "human_handoff_allowed", AsyncMock(return_value=True))

    class _BT:
        def add_task(self, *a, **k):
            pass

    product = {"product_id": product_id, **({"merchant_id": merchant_id} if merchant_id else {})}

    async def body(db):
        monkeypatch.setattr(dual_write, "database", db)
        return await _handle_offers_resolve(
            OffersResolvePayload(product=product, limit=10, market=market, tool="*",
                                 commerce_surface="agent_api",
                                 **({"include_live_verification_offers": True} if live_opt_in else {})),
            None, _BT(),
        )

    return _run(monkeypatch, body)


def _seed_source(res):
    [src] = [s for s in res["metadata"]["sources"] if s["source"] == "external_product_seeds"]
    return src


def _seed_offers(res):
    return [o for o in res["offers"] if str(o.get("offer_id") or "").startswith("of:external_seed:")]


def _stable(res):
    """The answer without its per-call values (timings, ids minted per request)."""
    volatile = {"latency_ms", "request_id", "trace_id", "generated_at", "resolved_at", "as_of", "elapsed_ms"}

    def scrub(v):
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items() if k not in volatile}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        return v

    return json.dumps(scrub(res), sort_keys=True, default=str)


def _sql(engine, statement, **params):
    from sqlalchemy import text

    with engine.connect() as c:
        c.execute(text(statement), params)


def test_the_fixture_is_the_shape_the_drain_mints(schema_engine, monkeypatch):
    """The gap, stated on the rows the producer wrote: the catalog row has no `source_ref`, its
    `source_product_id` is a slug, and the seed answers to neither -- only to the attached key."""
    from sqlalchemy import text

    _clear(schema_engine)
    sig, pk, seed_id, merchant = _mint(schema_engine, monkeypatch)
    with schema_engine.connect() as c:
        cp = c.execute(text("SELECT source_ref, source_product_id, source_system, platform, merchant_id"
                            " FROM catalog_products WHERE pivota_signature_id = :s"), {"s": sig}).mappings().one()
        seed = c.execute(text("SELECT id, external_product_id, attached_product_key, status, market,"
                              " price_currency, availability FROM external_product_seeds")).mappings().one()
    assert sig.startswith("sig_") and pk.startswith("ext:") and merchant.startswith("merch_obs_")
    assert cp["source_ref"] is None and cp["source_system"] == "catalog_enrichment_agent_v1"
    assert cp["platform"] == "external_seed" and cp["merchant_id"] == merchant
    assert cp["source_product_id"] and cp["source_product_id"] != seed["external_product_id"]
    assert seed["id"] == seed_id and seed["id"].startswith("seed:catalog_enrichment_agent_v1:")
    assert seed["id"] != cp["source_product_id"]
    assert seed["attached_product_key"] == pk
    assert (seed["status"], seed["market"], seed["price_currency"], seed["availability"]) == (
        "active", "US", "USD", "in_stock")


@pytest.mark.parametrize("off", [None, "", "0", "false", "off", "shadow", "enforce", "2"])
def test_flag_off_a_minted_row_has_no_seed_offer(schema_engine, monkeypatch, off):
    _clear(schema_engine)
    sig, _pk, _seed_id, merchant = _mint(schema_engine, monkeypatch)
    res = _resolve(monkeypatch, sig, flag=off, merchant_id=merchant)
    assert res["offers"] == [] and res["metadata"]["reason_code"] == "NO_CANDIDATES"
    assert _seed_source(res)["row_count"] == 0 and _seed_source(res)["query"] == "external_seed_by_fuzzy_ref"


@pytest.mark.parametrize("on", ["1", "true", "on", "YES"])
@pytest.mark.parametrize("with_merchant", [True, False])
def test_flag_on_a_minted_row_serves_its_attached_seed_with_a_link(schema_engine, monkeypatch, on, with_merchant):
    _clear(schema_engine)
    sig, _pk, seed_id, merchant = _mint(schema_engine, monkeypatch)
    res = _resolve(monkeypatch, sig, flag=on, merchant_id=merchant if with_merchant else None)
    src = _seed_source(res)
    assert (src["query"], src["row_count"], src["status"]) == (ARM, 1, "ok")
    [offer] = _seed_offers(res)
    assert res["offers"] == [offer]
    assert offer["source"]["seed_id"] == seed_id and offer["source"]["type"] == "external_seed"
    assert (offer["price"], offer["currency"], offer["in_stock"]) == (21.0, "USD", True)
    assert "requires_live_verification" not in offer
    # The link fields: our redirect, the store page behind it, and the click it is credited to.
    assert offer["purchase_route"] == "affiliate_outbound"
    assert offer["affiliate_url"].startswith("https://") and "/r?token=" in offer["affiliate_url"]
    spec = offer["execution_spec"]
    assert spec["merchant_domain"] == HOST and spec["pdp_url"].startswith(URL + "?")
    assert spec["tracking"]["click_id"].startswith("clk_") and spec["tracking"]["click_id"] in spec["pdp_url"]
    assert res["metadata"]["reason_code"] == "OK" and res["metadata"]["has_external"] is True


def test_a_mirror_row_is_answered_the_same_either_way(schema_engine, monkeypatch):
    """The existing keys find a mirror row's seed, so the new arm never runs for it: the answer is
    the same with the flag off and on, apart from the click id minted per request."""
    import re

    _clear(schema_engine)
    _mirror(schema_engine)

    def stable(res):
        text_ = _stable(res)
        for offer in res["offers"]:
            click = offer["execution_spec"]["tracking"]["click_id"]
            text_ = text_.replace(click, "clk_X")
            text_ = text_.replace(offer["affiliate_url"], "LINK")
        return re.sub(r'"expires_at": "[^"]+"', '"expires_at": "T"', text_)

    answers = {}
    for flag in (None, "1"):
        res = _resolve(monkeypatch, MIRROR_SIG, flag=flag)
        src = _seed_source(res)
        assert (src["query"], src["row_count"]) == ("external_seed_by_pivota_signature", 1), flag
        [offer] = _seed_offers(res)
        assert offer["source"]["seed_id"] == MIRROR_SEED
        answers[flag] = stable(res)
    assert answers[None] == answers["1"]


def test_an_attached_seed_the_rules_hide_stays_hidden(schema_engine, monkeypatch):
    def none_served(label, **kw):
        res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant, **kw)
        assert _seed_offers(res) == [] and res["offers"] == [], label
        return res

    # Not active (a tombstone of a re-minted listing).
    _clear(schema_engine)
    sig, pk, seed_id, merchant = _mint(schema_engine, monkeypatch)
    _sql(schema_engine, "UPDATE external_product_seeds SET status = 'superseded' WHERE id = :id", id=seed_id)
    assert _seed_source(none_served("inactive"))["row_count"] == 0

    # Priced in another currency than the buyer market's: a US buyer never sees it.
    _clear(schema_engine)
    sig, pk, seed_id, merchant = _mint(schema_engine, monkeypatch, currency="SGD")
    assert _seed_source(none_served("non-USD seed, US buyer"))["row_count"] == 0

    # Out of market: a USD seed for a buyer whose market is priced in another currency, and for a
    # market with no known currency at all.
    _clear(schema_engine)
    sig, pk, seed_id, merchant = _mint(schema_engine, monkeypatch)
    assert _seed_source(none_served("USD seed, SG buyer", market="SG"))["row_count"] == 0
    assert _seed_source(none_served("USD seed, unpriced market", market="DE"))["row_count"] == 0
    # ... while the same rows do answer the US buyer (the cases above are the filter, not a typo).
    assert len(_seed_offers(_resolve(monkeypatch, sig, flag="1", merchant_id=merchant))) == 1

    # Its store is quarantined.
    _sql(schema_engine, "INSERT INTO catalog_source_quarantine (match_type, match_value, created_by)"
                        " VALUES ('domain', :d, 'test')", d=HOST)
    try:
        assert _seed_source(none_served("quarantined"))["row_count"] == 0
    finally:
        _sql(schema_engine, "DELETE FROM catalog_source_quarantine")

    # Its catalog row is withdrawn: by either half of the suppression pair.
    for column, value in (("suppressed_at", "NOW()"), ("suppression_reason", "'test_takedown'")):
        _sql(schema_engine, f"UPDATE catalog_products SET {column} = {value} WHERE product_key = :pk", pk=pk)
        assert _seed_source(none_served(f"withdrawn by {column}"))["row_count"] == 0
        _sql(schema_engine, f"UPDATE catalog_products SET {column} = NULL WHERE product_key = :pk", pk=pk)
    assert len(_seed_offers(_resolve(monkeypatch, sig, flag="1", merchant_id=merchant))) == 1


def test_an_out_of_stock_minted_seed_makes_no_stock_claim(schema_engine, monkeypatch):
    """Stock is a downstream rule on the row, the same for every arm: an out-of-stock seed is a
    referral that says so, never an in-stock offer."""
    _clear(schema_engine)
    sig, _pk, _seed_id, merchant = _mint(schema_engine, monkeypatch, in_stock=False)
    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant)
    [offer] = _seed_offers(res)
    assert offer.get("in_stock") is False and offer["cart_prefilled"] is False
    assert offer["execution_spec"]["cart_url"] is None


def test_a_minted_seed_nobody_has_reread_follows_the_stale_snapshot_rule(schema_engine, monkeypatch):
    """The freshness gate is the lane's, not the arm's. A seed never re-read is withheld from a
    caller that did not opt in, and returned FLAGGED -- no stock claim, stored price marked stale
    -- to one that did (the door that re-reads the store before serving)."""
    _clear(schema_engine)
    sig, _pk, seed_id, merchant = _mint(schema_engine, monkeypatch, fresh=False)
    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant)
    assert res["offers"] == [] and _seed_source(res)["row_count"] == 1
    [held] = [c for c in res["mapping"]["candidates"] if c.get("seed_id") == seed_id]
    assert held["reason"] == "external_seed_requires_live_verification"
    assert "stale_snapshot" in held["blockers"]

    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant, live_opt_in=True)
    [offer] = _seed_offers(res)
    assert "stale_snapshot" in offer["requires_live_verification"]["reasons"]
    assert offer["requires_live_verification"]["price_stale"] is True
    assert "in_stock" not in offer and offer["availability"] == "unknown"
    # Flag off, the opted-in caller still gets nothing for this row.
    assert _resolve(monkeypatch, sig, flag=None, merchant_id=merchant, live_opt_in=True)["offers"] == []


def test_the_arm_probes_the_attached_key_index(schema_engine, monkeypatch):
    """The arm's seed statement, as the handler sent it, planned on a table of prod's order of
    magnitude: an index probe on idx_external_product_seeds_attached (migration 044), no scan."""
    from sqlalchemy import text

    import routes.agent_shop_gateway as gateway

    _clear(schema_engine)
    sig, pk, _seed_id, merchant = _mint(schema_engine, monkeypatch)
    _sql(schema_engine,
         "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
         " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
         " attached_product_key)"
         " SELECT 'eps_fill_' || g, 'ext_fill_' || g, 'US', '*', 'https://shop-b.example/p/' || g,"
         "        'https://shop-b.example/p/' || g, 'shop-b.example', 'Filler ' || g, 9, 'USD', 'in_stock',"
         "        '{}'::jsonb, 'active', 'ext:filler-' || g || '::00000000'"
         " FROM generate_series(1, 30000) g")
    _sql(schema_engine, "ANALYZE external_product_seeds")

    sent = []
    real = gateway._fetch_signature_attached_seed_rows

    async def spy(db, **kw):
        class _Recording:
            async def fetch_all(self, query, values=None):
                sent.append((str(query), dict(values or {})))
                return await db.fetch_all(query, values)

        return await real(_Recording(), **kw)

    monkeypatch.setattr(gateway, "_fetch_signature_attached_seed_rows", spy)
    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant)
    assert len(_seed_offers(res)) == 1
    [(statement, params)] = [s for s in sent if "FROM external_product_seeds" in s[0]]
    assert params["product_keys"] == [pk]
    with schema_engine.connect() as c:
        plan = "\n".join(r[0] for r in c.execute(text("EXPLAIN " + statement), params))
    assert "idx_external_product_seeds_attached" in plan, plan
    assert "Seq Scan on external_product_seeds" not in plan, plan


# ── the arm serves MINTED rows only ───────────────────────────────────────────────────────────
#
# The two rows below are written by hand in the shape their producers leave (a store sync and
# the mirror job: neither is a function a test can call for one row). Each has an ACTIVE, fresh
# seed attached to its `product_key`, so the only thing between it and an offer is the arm's
# `source_system` filter -- which the last step of each test proves by relabelling the row.

CONNECTED_SIG = "sig_" + "c1" * 16
CONNECTED_MERCHANT = "merch_shop_a"
CONNECTED_PID = "7700000000001"
CONNECTED_PK = f"prod::{CONNECTED_MERCHANT}::shopify::{CONNECTED_PID}"
MINTED_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"


def _attached_seed(engine, *, seed_id, external_id, product_key, host, status="active"):
    """A seed attached to `product_key`, read moments ago unless it is not active."""
    url = f"https://{host}/products/day-cream"
    _sql(engine,
         "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
         " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
         " attached_product_key)"
         " VALUES (:id, :epid, 'US', '*', :url, :url, :host, 'Day Cream', 18, 'USD', 'in_stock',"
         "         cast(:sd AS jsonb), :status, :pk)",
         id=seed_id, epid=external_id, url=url, host=host, status=status, pk=product_key,
         sd=json.dumps({"title": "Day Cream", "brand": "Brand Y", "variants": [
             {"variant_id": "4400000000002", "price_amount": 18, "price_currency": "USD",
              "currency": "USD", "availability": "in_stock"}]}))
    _stamp_fresh(engine, seed_id)


def _same_answer_flag_off_and_on(monkeypatch, product_id, **kw):
    """Resolve with the flag off and on; neither may come from the arm. Returns the two answers."""
    answers = {}
    for flag in (None, "1"):
        res = _resolve(monkeypatch, product_id, flag=flag, **kw)
        assert _seed_offers(res) == [], (flag, kw)
        assert _seed_source(res)["query"] != ARM and _seed_source(res)["row_count"] == 0, (flag, kw)
        answers[flag] = _stable(res)
    return answers


@pytest.mark.parametrize("market", ["US", "SG"])
@pytest.mark.parametrize("scoped", [True, False])
def test_flag_on_a_connected_row_with_an_attached_seed_gets_nothing_from_the_arm(
        schema_engine, monkeypatch, market, scoped):
    _clear(schema_engine)
    _sql(schema_engine,
         "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
         " pivota_signature_id, source_system, source_ref, updated_at)"
         " VALUES (:pk, :m, 'shopify', :spid, 'Day Cream', 'Brand Y', :sig, 'products_cache', NULL, NOW())",
         pk=CONNECTED_PK, m=CONNECTED_MERCHANT, spid=CONNECTED_PID, sig=CONNECTED_SIG)
    _attached_seed(schema_engine, seed_id="eps_" + "e1" * 12, external_id="ext_" + "e1" * 12,
                   product_key=CONNECTED_PK, host="shop-c.example")
    kw = {"market": market, "merchant_id": CONNECTED_MERCHANT if scoped else None}
    answers = _same_answer_flag_off_and_on(monkeypatch, CONNECTED_SIG, **kw)
    assert answers["1"] == answers[None]

    # Not vacuous: the same row and seed, labelled as the drain labels its rows, IS served (to the
    # market the seed is priced for).
    _sql(schema_engine, "UPDATE catalog_products SET source_system = :s WHERE product_key = :pk",
         s=MINTED_SOURCE_SYSTEM, pk=CONNECTED_PK)
    res = _resolve(monkeypatch, CONNECTED_SIG, flag="1", **kw)
    assert (_seed_source(res)["query"] == ARM and len(_seed_offers(res)) == 1) is (market == "US")


@pytest.mark.parametrize("market", ["US", "SG"])
def test_flag_on_a_mirror_row_whose_own_seed_is_inactive_gets_nothing_from_the_arm(
        schema_engine, monkeypatch, market):
    """The mirror row's own seed is no longer active; ANOTHER store's active seed is attached to
    the same key. The arm does not hand that store's seed to the mirror row."""
    _clear(schema_engine)
    _mirror(schema_engine)
    _sql(schema_engine, "UPDATE external_product_seeds SET status = 'inactive' WHERE id = :id", id=MIRROR_SEED)
    _attached_seed(schema_engine, seed_id="eps_" + "e2" * 12, external_id="ext_" + "e2" * 12,
                   product_key=MIRROR_PK, host="shop-c.example")
    answers = _same_answer_flag_off_and_on(monkeypatch, MIRROR_SIG, market=market)
    assert answers["1"] == answers[None]

    _sql(schema_engine, "UPDATE catalog_products SET source_system = :s WHERE product_key = :pk",
         s=MINTED_SOURCE_SYSTEM, pk=MIRROR_PK)
    res = _resolve(monkeypatch, MIRROR_SIG, flag="1", market=market)
    assert (_seed_source(res)["query"] == ARM and len(_seed_offers(res)) == 1) is (market == "US")
    assert all(o["source"]["seed_id"] != MIRROR_SEED for o in _seed_offers(res))


# ── more than one seed on a minted row ────────────────────────────────────────────────────────

RETAILER_HOST = "shop-b.example"
SG_HOST = "shop-c.example"


def _offer_of(host, *, seller, price):
    url = f"https://{host}/products/calm-barrier-cream"
    return {"merchant_inferred": seller, "destination_url": url, "canonical_url": url,
            "image_url": f"https://{host}/img/calm-barrier-cream.jpg", "price": price, "in_stock": True,
            "validated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _mint_records(schema_engine, monkeypatch, records):
    """The drain's planner and writer over `records` that describe ONE product. Returns
    (signature, product_key, merchant, {seed id: its planned row})."""
    import services.catalog_enrichment_agent.apply as apply_mod
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    plan = ingest_validated_jsonl(records)

    async def body(db):
        return await apply_mod.apply_ingest_plan(plan, batch_label="minted-attached-seed-test", db=db)

    counts = _run(monkeypatch, body)
    [pdp] = plan["pdps"]
    assert counts["pdps"] == 1 and counts["seeds"] == len(plan["seeds"]) >= 2, counts
    assert {s["attached_product_key"] for s in plan["seeds"]} == {pdp["product_key"]}
    for seed in plan["seeds"]:
        _stamp_fresh(schema_engine, seed["id"])
    return pdp["pivota_signature_id"], pdp["product_key"], pdp["merchant_id"], {s["id"]: s for s in plan["seeds"]}


def _two_sellers(schema_engine, monkeypatch):
    record = _record()
    record["offers"].append(_offer_of(RETAILER_HOST, seller="Shop B", price=19.5))
    return _mint_records(schema_engine, monkeypatch, [record])


@pytest.mark.parametrize("scoped", [True, False])
def test_flag_on_a_minted_row_serves_every_seller_attached_to_it(schema_engine, monkeypatch, scoped):
    """The drain writes one seed per validated offer -- the brand store's and a retailer's -- and
    both are the row's sellers."""
    _clear(schema_engine)
    sig, _pk, merchant, seeds = _two_sellers(schema_engine, monkeypatch)
    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant if scoped else None)
    assert (_seed_source(res)["query"], _seed_source(res)["row_count"]) == (ARM, 2)
    offers = _seed_offers(res)
    assert {o["source"]["seed_id"] for o in offers} == set(seeds)
    assert {o["execution_spec"]["merchant_domain"] for o in offers} == {HOST, RETAILER_HOST}
    assert {(o["price"], o["currency"]) for o in offers} == {(21.0, "USD"), (19.5, "USD")}
    # Flag off, neither.
    assert _resolve(monkeypatch, sig, flag=None, merchant_id=merchant if scoped else None)["offers"] == []


def test_flag_on_two_currencies_on_one_key_each_go_to_their_own_market(schema_engine, monkeypatch):
    """One product listed in USD by the brand store and in SGD by another store: two seeds on one
    key. Each buyer market gets the seed priced in its currency and never the other."""
    _clear(schema_engine)
    sgd = _record(currency="SGD")
    sgd["pdp"]["source_domain"] = SG_HOST
    sgd["offers"] = [_offer_of(SG_HOST, seller="Shop C", price=28.0)]
    sig, _pk, merchant, seeds = _mint_records(schema_engine, monkeypatch, [_record(), sgd])
    by_currency = {s["price_currency"]: sid for sid, s in seeds.items()}
    assert sorted(by_currency) == ["SGD", "USD"]

    for market, currency, host, price in (("US", "USD", HOST, 21.0), (None, "USD", HOST, 21.0),
                                          ("SG", "SGD", SG_HOST, 28.0)):
        res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant, market=market)
        assert (_seed_source(res)["query"], _seed_source(res)["row_count"]) == (ARM, 1), market
        [offer] = _seed_offers(res)
        assert offer["source"]["seed_id"] == by_currency[currency], market
        assert (offer["price"], offer["currency"]) == (price, currency), market
        assert offer["execution_spec"]["merchant_domain"] == host, market
    # A market with no known currency gets neither.
    res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant, market="DE")
    assert _seed_offers(res) == [] and _seed_source(res)["row_count"] == 0


@pytest.mark.parametrize("stored_order", ["ASC", "DESC"])
def test_seeds_with_tied_timestamps_come_in_seed_id_order(schema_engine, monkeypatch, stored_order):
    """The drain writes one product's seeds in one transaction, so their timestamps tie. The arm's
    order -- and which seed survives a `limit` -- is then the seed id's, whichever way the rows
    happen to be stored."""
    from sqlalchemy import text

    import routes.agent_shop_gateway as gateway

    _clear(schema_engine)
    sig, pk, _merchant, seeds = _two_sellers(schema_engine, monkeypatch)
    with schema_engine.connect() as c:
        # Re-store the drain's own rows in a chosen physical order, timestamps tied.
        c.execute(text("CREATE TEMP TABLE tied_seeds AS SELECT * FROM external_product_seeds"
                       " WHERE attached_product_key = :pk"), {"pk": pk})
        c.execute(text("UPDATE tied_seeds SET updated_at = '2026-11-10T10:00:00Z',"
                       " created_at = '2026-11-10T10:00:00Z'"))
        c.execute(text("DELETE FROM external_product_seeds WHERE attached_product_key = :pk"), {"pk": pk})
        c.execute(text(f"INSERT INTO external_product_seeds SELECT * FROM tied_seeds ORDER BY id {stored_order}"))
        c.execute(text("DROP TABLE tied_seeds"))
        stored = [r[0] for r in c.execute(text(
            "SELECT id FROM external_product_seeds WHERE attached_product_key = :pk ORDER BY ctid"), {"pk": pk})]
    assert stored == sorted(seeds, reverse=(stored_order == "DESC"))

    def arm(limit):
        async def body(db):
            rows = await gateway._fetch_signature_attached_seed_rows(
                db, sig_aliases=[sig], market_hint="US", limit=limit)
            return [dict(r)["id"] for r in rows]

        return _run(monkeypatch, body)

    assert arm(10) == sorted(seeds)
    assert arm(1) == sorted(seeds)[:1]


# ── the arm's own failure boundary ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error", [asyncio.TimeoutError(), RuntimeError("connection is closed")],
                         ids=["timeout", "database_error"])
def test_flag_on_an_arm_failure_leaves_the_flag_off_answer(schema_engine, monkeypatch, caplog, error):
    """The arm fails; the request goes on through the later lanes and answers what it answers with
    the flag off. The failure is reported under the arm's name, and the seed source is not an
    error: the lanes after the arm did run."""
    import logging

    import routes.agent_shop_gateway as gateway

    _clear(schema_engine)
    sig, _pk, _seed_id, merchant = _mint(schema_engine, monkeypatch)
    off = _resolve(monkeypatch, sig, flag=None, merchant_id=merchant)

    async def failing(db, **kw):
        raise error

    monkeypatch.setattr(gateway, "_fetch_signature_attached_seed_rows", failing)
    with caplog.at_level(logging.WARNING, logger="routes.agent_shop_gateway"):
        res = _resolve(monkeypatch, sig, flag="1", merchant_id=merchant)
    src = _seed_source(res)
    assert (src["status"], src["query"], src["row_count"]) == ("ok", "external_seed_by_fuzzy_ref", 0)
    assert src.pop("attached_seed_arm_error") == type(error).__name__
    assert _stable(res) == _stable(off)
    assert f"offers.resolve.attached_seed_arm.failed error_type={type(error).__name__}" in [
        r.getMessage() for r in caplog.records]
