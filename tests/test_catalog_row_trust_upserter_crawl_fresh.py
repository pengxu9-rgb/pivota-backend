"""The upserter half of CRAWL_FRESH: the switch, N, and the input.

The policy only ever sees ``crawl_fresh``; whether it is supplied at
all is decided HERE (CATALOG_TRUST_CRAWL_FRESH_ENABLED, default off),
so every recompute path -- the single-row hook, the bulk path the 6h backfill
cron and the one-off scripts use, the quarantine refresh, and the read-only
preview -- must supply it the same way. The statement itself runs on Postgres
in tests/test_crawl_fresh_trust_postgres.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services import catalog_row_trust_upserter as upserter
from services.catalog_trust_policy import REASON_CODES
from services.external_offer_dual_write import PRICE_CHECK_COLUMN_SQL
from tests.test_catalog_row_trust_upserter import make_joined_row

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
RETAILER_PK = "ext:retailer:0123456789abcdef0123456789abcdef"
OTHER_RETAILER_PK = "ext:retailer:fedcba9876543210fedcba9876543210"
BRAND_PK = "ext:serum-slug::0123abcd"


@pytest.fixture(autouse=True)
def _switch_off_by_default(monkeypatch):
    monkeypatch.delenv(upserter.CRAWL_FRESH_ENV, raising=False)
    monkeypatch.delenv(upserter.CRAWL_FRESH_DAYS_ENV, raising=False)


def retailer_row(pk=RETAILER_PK, **overrides):
    """A drain-written retailer listing as the product join returns it: no
    identity listing minted yet (the IDENTITY_CONFIDENCE_NULL case)."""
    base = dict(
        product_key=pk,
        content_key=f"ck_{pk[-6:]}",
        merchant_id="merch_obs_0123456789abcdef",
        platform="external_seed",
        source_system="catalog_enrichment_agent_v1",
        source_ref="https://shop.example.com/products/serum",
        source_product_id="serum-slug",
        source_domain="shop.example.com",
        pil_source_listing_ref=None,
        identity_status=None,
        identity_confidence=None,
        live_read_enabled=None,
        review_required=None,
        sellable_item_group_id=None,
        product_line_id=None,
        review_family_id=None,
        eps_id=77,
        eps_status="active",
        eps_domain="shop.example.com",
        eps_attached_product_key=pk,
        eps_last_seen_at=NOW,
        eps_seed_kind="cross",
        ms_merchant_id=None,
        ms_platform=None,
        ms_domain=None,
        ms_status=None,
        ms_last_sync=None,
        row_has_priced_offer=True,
        row_is_elected_canonical=None,
    )
    base.update(overrides)
    return make_joined_row(**base)


class CrawlFakeDb:
    """The product join, the quarantine list, the 246 column probe and the
    freshness statement, each recognised by text the real statement carries."""

    def __init__(self, *, joined_rows, reads=None, column_present=True, read_fails=False):
        self.read_fails = read_fails
        self.joined_rows = list(joined_rows)
        self.reads = dict(reads or {})
        self.column_present = column_present
        self.queries: list[tuple[str, dict]] = []
        self.executes: list[tuple[str, dict]] = []

    async def fetch_one(self, query, values=None):
        values = dict(values or {})
        self.queries.append((query, values))
        if query == PRICE_CHECK_COLUMN_SQL:
            return {"present": self.column_present}
        pk = values.get("product_key")
        return next((r for r in self.joined_rows if r["product_key"] == pk), None)

    async def fetch_all(self, query, values=None):
        values = dict(values or {})
        self.queries.append((query, values))
        if "FROM catalog_source_quarantine" in query:
            return []
        if query == upserter.CRAWL_ROW_PRICE_READ_SQL:
            assert self.column_present, "the statement naming price_checked_at was sent to a DB without it"
            if self.read_fails:
                raise RuntimeError("the freshness read failed")
            assert values["now"].tzinfo is not None, "the batch clock must be UTC-aware"
            keys = values["product_keys"]
            return [
                {"product_key": k, "row_price_read_at": self.reads[k]}
                for k in keys
                if self.reads.get(k) is not None
            ]
        if "product_key = ANY" in query:
            keys = set(values.get("product_keys") or [])
            return sorted((r for r in self.joined_rows if r["product_key"] in keys),
                          key=lambda r: r["product_key"])
        return list(self.joined_rows)

    async def execute(self, query, values=None):
        self.executes.append((query, dict(values or {})))

    def sent(self, sql):
        return [v for q, v in self.queries if q == sql]

    def keys_sent(self):
        return [v["product_keys"] for v in self.sent(upserter.CRAWL_ROW_PRICE_READ_SQL)]

    def written(self):
        return {v["subject_key"]: v for _, v in self.executes}


def _switch_on(monkeypatch, days=None):
    monkeypatch.setenv(upserter.CRAWL_FRESH_ENV, "1")
    if days is not None:
        monkeypatch.setenv(upserter.CRAWL_FRESH_DAYS_ENV, str(days))


async def _write_one(db, pk=RETAILER_PK):
    assert await upserter.upsert_catalog_row_trust(db=db, product_key=pk, now=NOW) is True
    return db.written()[pk]


# ---------------------------------------------------------------------------
# Switch OFF: byte-identical to main
# ---------------------------------------------------------------------------


async def test_off_sends_no_extra_statement_and_writes_today_s_decision():
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW})
    params = await _write_one(db)
    assert params["serving_decision"] == "shadow"
    assert params["serving_reason_codes"] == [REASON_CODES.IDENTITY_CONFIDENCE_NULL]
    assert db.sent(PRICE_CHECK_COLUMN_SQL) == []
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL) == []


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled", "2"])
def test_only_the_truthy_spellings_switch_it_on(monkeypatch, value):
    monkeypatch.setenv(upserter.CRAWL_FRESH_ENV, value)
    assert upserter.crawl_fresh_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(upserter.CRAWL_FRESH_ENV, value)
    assert upserter.crawl_fresh_enabled() is True


def test_off_leaves_the_inputs_dict_exactly_as_main_built_it():
    row = retailer_row()
    built = upserter._joined_row_to_inputs(row, [], NOW)
    assert "crawl_fresh" not in built
    assert upserter._joined_row_to_inputs(row, [], NOW, crawl_fresh=None) == built
    # A key the map does not carry (a non-retailer row) leaves it unset too.
    assert upserter._joined_row_to_inputs(row, [], NOW, crawl_fresh={"other": True}) == built


# ---------------------------------------------------------------------------
# Switch ON
# ---------------------------------------------------------------------------


async def test_on_a_fresh_listing_is_written_public_with_the_trace_code(monkeypatch):
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW - timedelta(days=2)})
    params = await _write_one(db)
    assert params["serving_decision"] == "public"
    assert params["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]
    assert db.keys_sent() == [[RETAILER_PK]]
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL)[0]["now"] == NOW


async def test_on_a_stale_listing_is_today_s_decision(monkeypatch):
    off = await _write_one(CrawlFakeDb(joined_rows=[retailer_row()]))
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW - timedelta(days=8)})
    assert await _write_one(db) == off


async def test_on_a_listing_with_no_qualifying_read_is_today_s_decision(monkeypatch):
    off = await _write_one(CrawlFakeDb(joined_rows=[retailer_row()]))
    _switch_on(monkeypatch)
    assert await _write_one(CrawlFakeDb(joined_rows=[retailer_row()], reads={})) == off


async def test_on_without_the_246_column_nothing_names_it_and_nothing_moves(monkeypatch):
    off = await _write_one(CrawlFakeDb(joined_rows=[retailer_row()]))
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW}, column_present=False)
    assert await _write_one(db) == off
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL) == []


async def test_on_a_failed_column_probe_reads_as_absent(monkeypatch):
    _switch_on(monkeypatch)

    class ProbeFails(CrawlFakeDb):
        async def fetch_one(self, query, values=None):
            if query == PRICE_CHECK_COLUMN_SQL:
                raise RuntimeError("no pg_attribute here")
            return await super().fetch_one(query, values)

    db = ProbeFails(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW})
    params = await _write_one(db)
    assert params["serving_decision"] == "shadow"
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL) == []


async def test_on_a_batch_with_no_retailer_listing_sends_nothing_extra(monkeypatch):
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[make_joined_row()])
    await _write_one(db, pk="pk_internal_1")
    assert db.sent(PRICE_CHECK_COLUMN_SQL) == []
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL) == []


async def test_a_fresh_brand_official_row_is_written_public_with_the_same_code(monkeypatch):
    _switch_on(monkeypatch)
    row = retailer_row(BRAND_PK, merchant_id="merch_brand_0123456789ab",
                       identity_status="approved", identity_confidence=0.9,
                       live_read_enabled=False, pil_source_listing_ref="seed:9")
    db = CrawlFakeDb(joined_rows=[row], reads={BRAND_PK: NOW - timedelta(days=1)})
    params = await _write_one(db, pk=BRAND_PK)
    assert params["serving_decision"] == "public"
    assert params["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]


async def test_on_only_drain_lane_rows_are_looked_up_and_only_they_get_the_input(monkeypatch):
    _switch_on(monkeypatch)
    rows = [
        retailer_row(),
        retailer_row(OTHER_RETAILER_PK),
        make_joined_row(),
        # the drain writer's brand-official / curated slug key: also a crawl row
        retailer_row(BRAND_PK, merchant_id="merch_brand_0123456789ab"),
        # an `ext:` key under another writer: not
        retailer_row("ext:mirror-slug::0123abcd", source_system="external_product_seeds_mirror_v1"),
    ]
    db = CrawlFakeDb(joined_rows=rows, reads={RETAILER_PK: NOW, BRAND_PK: NOW,
                                              "ext:mirror-slug::0123abcd": NOW})
    fresh = await upserter._crawl_fresh_by_key(db, rows, NOW)
    assert fresh == {RETAILER_PK: True, OTHER_RETAILER_PK: False, BRAND_PK: True}
    assert db.keys_sent() == [sorted([RETAILER_PK, OTHER_RETAILER_PK, BRAND_PK])]


@pytest.mark.parametrize("days,age_days,decision", [(3, 4, "shadow"), (5, 4, "public"), (4, 4, "public")])
async def test_on_n_days_comes_from_the_env(monkeypatch, days, age_days, decision):
    _switch_on(monkeypatch, days=days)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW - timedelta(days=age_days)})
    assert (await _write_one(db))["serving_decision"] == decision


@pytest.mark.parametrize("raw", ["", "  ", "x", "0", "-1", "nan", "inf", "-inf"])
def test_an_unusable_n_falls_back_to_seven(monkeypatch, raw):
    monkeypatch.setenv(upserter.CRAWL_FRESH_DAYS_ENV, raw)
    assert upserter.crawl_fresh_days() == 7.0


def test_n_accepts_fractions(monkeypatch):
    monkeypatch.setenv(upserter.CRAWL_FRESH_DAYS_ENV, " 0.5 ")
    assert upserter.crawl_fresh_days() == 0.5
    monkeypatch.delenv(upserter.CRAWL_FRESH_DAYS_ENV)
    assert upserter.crawl_fresh_days() == 7.0


# ---------------------------------------------------------------------------
# Every recompute path supplies it
# ---------------------------------------------------------------------------


def _paths_db():
    return CrawlFakeDb(
        joined_rows=[retailer_row(), retailer_row(OTHER_RETAILER_PK)],
        reads={RETAILER_PK: NOW - timedelta(hours=6), OTHER_RETAILER_PK: NOW - timedelta(days=30)},
    )


async def test_bulk_path_used_by_the_backfill_cron_and_scripts(monkeypatch):
    _switch_on(monkeypatch)
    db = _paths_db()
    wrote = await upserter.upsert_catalog_row_trust_many(
        db=db, product_keys=[RETAILER_PK, OTHER_RETAILER_PK], now=NOW
    )
    assert wrote == 2
    written = db.written()
    assert written[RETAILER_PK]["serving_decision"] == "public"
    assert written[OTHER_RETAILER_PK]["serving_decision"] == "shadow"


async def test_bulk_path_asks_once_per_batch(monkeypatch):
    _switch_on(monkeypatch)
    db = _paths_db()
    await upserter.upsert_catalog_row_trust_many(
        db=db, product_keys=[RETAILER_PK, OTHER_RETAILER_PK], now=NOW, limit=1
    )
    assert db.keys_sent() == [[RETAILER_PK], [OTHER_RETAILER_PK]]


async def test_quarantine_refresh_path(monkeypatch):
    _switch_on(monkeypatch)
    db = _paths_db()
    wrote = await upserter.upsert_catalog_row_trust_for_quarantine_match(
        db=db, match_type="merchant_platform", match_value="merch_obs_0123456789abcdef:external_seed", now=NOW
    )
    assert wrote == 2
    assert db.written()[RETAILER_PK]["serving_decision"] == "public"
    assert db.written()[OTHER_RETAILER_PK]["serving_decision"] == "shadow"


async def test_preview_path(monkeypatch):
    db = _paths_db()
    keys = [RETAILER_PK, OTHER_RETAILER_PK]
    assert await upserter.preview_serving_decisions(db=db, product_keys=keys, now=NOW) == {
        RETAILER_PK: "shadow", OTHER_RETAILER_PK: "shadow"}
    _switch_on(monkeypatch)
    assert await upserter.preview_serving_decisions(db=db, product_keys=keys, now=NOW) == {
        RETAILER_PK: "public", OTHER_RETAILER_PK: "shadow"}
    assert db.executes == []


async def test_primary_readiness_and_the_cron_reach_the_upserter_entry_points():
    """They own no trust SQL of their own: readiness calls the single-row hook,
    the cron the bulk path. If either grew its own join, this would be the
    place that missed the new input."""
    import inspect

    from jobs import catalog_row_trust_backfill_cron as cron
    from services.catalog_enrichment_agent import primary_readiness

    assert "upsert_catalog_row_trust(db=db, product_key=pk)" in inspect.getsource(primary_readiness)
    assert "upsert_catalog_row_trust_many(db=database, product_keys=product_keys)" in inspect.getsource(cron)
    assert "derive_trust" not in inspect.getsource(primary_readiness)
    assert "derive_trust" not in inspect.getsource(cron)


# ---------------------------------------------------------------------------
# The statement, pinned byte for byte (the twin copies it)
# ---------------------------------------------------------------------------


def test_the_statement_is_pinned():
    """Byte for byte: the PIVOTA-Agent twin copies this statement."""
    assert upserter.CRAWL_ROW_PRICE_READ_SQL == (
        'SELECT k.product_key, r.row_price_read_at\n'
        '  FROM unnest(CAST(:product_keys AS text[])) AS k(product_key)\n'
        ' CROSS JOIN LATERAL (\n'
        '   SELECT CASE WHEN count(*) > 0\n'
        "                AND bool_and((CASE WHEN co.source_system = 'catalog_enrichment_agent_v1' AND (upper(btrim(coalesce(co.market, ''))), upper(btrim(coalesce(co.currency, '')))) IN (('SG', 'SGD'), ('US', 'USD')) AND coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%' AND co.price_checked_at <= CAST(:now AS timestamptz) AND upper(btrim(coalesce(co.offer_payload->>'declared_market', ''))) = upper(btrim(coalesce(co.market, ''))) AND coalesce(co.offer_payload->>'price_read_source', '') <> 'employee_edit' THEN co.price_checked_at END) IS NOT NULL)\n"
        "                AND bool_or(lower(btrim(coalesce(co.availability, ''))) NOT IN ('out_of_stock', 'outofstock', 'sold_out', 'soldout', 'unavailable'))\n"
        "               THEN min(CASE WHEN co.source_system = 'catalog_enrichment_agent_v1' AND (upper(btrim(coalesce(co.market, ''))), upper(btrim(coalesce(co.currency, '')))) IN (('SG', 'SGD'), ('US', 'USD')) AND coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%' AND co.price_checked_at <= CAST(:now AS timestamptz) AND upper(btrim(coalesce(co.offer_payload->>'declared_market', ''))) = upper(btrim(coalesce(co.market, ''))) AND coalesce(co.offer_payload->>'price_read_source', '') <> 'employee_edit' THEN co.price_checked_at END) END AS row_price_read_at\n"
        '     FROM catalog_offers co\n'
        '    WHERE co.product_key = k.product_key\n'
        '      AND co.suppressed_at IS NULL\n'
        '      AND coalesce(co.merchant_effective_price, co.estimated_best_price, co.list_price) > 0\n'
        ' ) r\n'
        ' WHERE r.row_price_read_at IS NOT NULL\n'
    )


def test_the_class_is_every_unsuppressed_priced_offer_and_validity_carries_the_rest():
    """Rounds 4-5, fail closed: the WHERE is the key, unsuppressed and priced --
    nothing else. Source, market, currency and affiliate moved into the valid
    read, so an offer that fails one is IN the class and makes the row NULL."""
    where = upserter.CRAWL_ROW_PRICE_READ_SQL.split(" WHERE co.product_key", 1)[1].split(" ) r", 1)[0]
    priced = "coalesce(co.merchant_effective_price, co.estimated_best_price, co.list_price) > 0"
    assert where.split("\n      AND ")[1:] == ["co.suppressed_at IS NULL", priced + "\n"]
    assert upserter.crawl_offer_class_conjuncts("k") == ["co.product_key = k", "co.suppressed_at IS NULL", priced]
    assert upserter.CRAWL_OFFER_CLASS_PRICED_SQL == priced
    for moved in (upserter.CRAWL_OFFER_DRAIN_SOURCE_SQL, upserter.CRAWL_OFFER_SERVED_MARKET_SQL,
                  upserter.CRAWL_OFFER_NOT_AFFILIATE_SQL):
        assert moved not in where
        assert moved in upserter.CRAWL_OFFER_VALID_READ_SQL


def test_the_class_prices_an_offer_like_the_widest_reader():
    """Round 6: mep, else estimated best, else list -- the price migration 246's
    stamp-forgetting trigger watches and the PDP view / offers.resolve /
    purchase readers serve; never the narrower shared gate (mep, else list)."""
    from services.priced_offer_sql import priced_offer_price_expr

    mig = (Path(__file__).resolve().parent.parent
           / "db/migrations/246_catalog_offers_price_checked_at.sql").read_text()
    assert "coalesce(merchant_effective_price, estimated_best_price, list_price)" in mig
    assert upserter.CRAWL_OFFER_CLASS_PRICED_SQL.replace("co.", "").replace(" > 0", "") in mig
    assert priced_offer_price_expr(alias="co") not in upserter.CRAWL_ROW_PRICE_READ_SQL


def test_the_shared_price_gate_is_unchanged():
    """The widening lives in CRAWL_OFFER_CLASS_PRICED_SQL only. The shared helper
    builds row_has_priced_offer for OFFER_PRICE_MISSING, which deliberately leaves
    out estimated_best_price (and the gateway pins that spelling)."""
    from services.priced_offer_sql import priced_offer_exists_sql, priced_offer_row_conjuncts

    assert priced_offer_row_conjuncts(alias="co") == (
        "co.suppressed_at IS NULL", "coalesce(co.merchant_effective_price, co.list_price) > 0")
    assert "estimated_best_price" not in priced_offer_exists_sql("cp.product_key")
    assert priced_offer_exists_sql("cp.product_key") + " AS row_has_priced_offer" in upserter._PRODUCT_JOIN_SELECT


def test_no_link_test_anywhere_in_the_rule():
    """Round 5: not every reader tests the link (the canonical PDP's own-money read
    serves an offer whose destination is '' or a bare path), so the link neither
    takes an offer out of the class nor decides whether its read is valid."""
    for sql in (upserter.CRAWL_ROW_PRICE_READ_SQL, upserter.CRAWL_FRESH_RECHECK_SQL,
                upserter.CRAWL_OFFER_VALID_READ_SQL):
        for token in ("destination_url", "source_ref", "https?", "http"):
            assert token not in sql, (token, sql[:60])
    assert not hasattr(upserter, "CRAWL_OFFER_LINK_SQL")


def test_the_served_pairs_fixture_is_current():
    import json

    from scripts.generate_crawl_fresh_served_markets import served_markets_json

    committed = (Path(__file__).resolve().parent.parent / "config/crawl_fresh_served_markets.json").read_text()
    assert committed == served_markets_json()
    pairs = json.loads(committed)["served_market_currency"]
    assert [tuple(p) for p in pairs] == list(upserter.CRAWL_SERVED_MARKET_CURRENCY_PAIRS)
    assert upserter.CRAWL_SERVED_MARKET_CURRENCY_PAIRS == (("SG", "SGD"), ("US", "USD"))


def test_the_declared_market_key_is_the_drain_s():
    from services.retailer_ingest.pipeline import DECLARED_MARKET_KEY

    assert upserter.CRAWL_DECLARED_MARKET_KEY == DECLARED_MARKET_KEY == "declared_market"
    assert "offer_payload->>'declared_market'" in upserter.CRAWL_OFFER_DECLARED_MARKET_SQL
    assert upserter.CRAWL_OFFER_DECLARED_MARKET_SQL in upserter.CRAWL_OFFER_VALID_READ_SQL


def test_the_market_and_stock_fragments_come_from_the_registries():
    from services.market_registry import served_markets
    from services.offer_buyability import OFFER_UNAVAILABLE_AVAILABILITIES
    from services.region_pricing import pricing_currency_for_region

    for market in served_markets():
        assert f"('{market}', '{pricing_currency_for_region(market)}')" in upserter.CRAWL_OFFER_SERVED_MARKET_SQL
    for value in OFFER_UNAVAILABLE_AVAILABILITIES:
        assert f"'{value}'" in upserter.CRAWL_OFFER_IN_STOCK_SQL


def test_the_read_source_key_is_the_writer_s():
    from services.external_offer_dual_write import ATTACHED_LISTING_OFFER_UPDATE_SQL, PRICE_READ_SOURCE_KEY

    assert upserter.CRAWL_PRICE_READ_SOURCE_KEY == PRICE_READ_SOURCE_KEY == "price_read_source"
    assert "'{price_read_source}'" in ATTACHED_LISTING_OFFER_UPDATE_SQL
    assert ":read_source" in ATTACHED_LISTING_OFFER_UPDATE_SQL


def test_the_recheck_shares_the_input_statement_s_rule():
    inner = upserter.crawl_row_price_read_lateral_sql("crt.subject_key")
    assert inner in upserter.CRAWL_FRESH_RECHECK_SQL
    assert "JOIN catalog_products cp ON cp.product_key = crt.subject_key" in upserter.CRAWL_FRESH_RECHECK_SQL
    assert "JOIN catalog_products cp ON cp.product_key = crt.subject_key" in upserter._CRAWL_FRESH_ROWS_ALL_SQL
    assert upserter.crawl_row_price_read_lateral_sql("k.product_key") in upserter.CRAWL_ROW_PRICE_READ_SQL
    assert "@> CAST(ARRAY['CRAWL_FRESH'] AS text[])" in upserter.CRAWL_FRESH_RECHECK_SQL


def test_the_orphan_demote_re_checks_both_conditions_on_the_row_it_updates():
    """Round 4: under EvalPlanQual only the OUTER quals are re-checked on a row a
    concurrent writer changed (Postgres proof: test_crawl_fresh_trust_postgres)."""
    sql = upserter._CRAWL_FRESH_ORPHAN_DEMOTE_SQL
    outer = sql.split(" RETURNING", 1)[0].split("   AND crt.subject_key IN (", 1)[0]
    assert "   AND crt.serving_reason_codes @> CAST(ARRAY['CRAWL_FRESH'] AS text[])\n" in outer
    assert ("   AND NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.product_key = crt.subject_key)\n"
            in outer)
    assert " WHERE crt.subject_type = 'product'\n" in outer


# ---------------------------------------------------------------------------
# Round 2: the decay recheck
# ---------------------------------------------------------------------------


class RecheckFakeDb(CrawlFakeDb):
    def __init__(self, *, expiring=(), all_crawl_fresh=(), orphans=(), **kw):
        super().__init__(**kw)
        self.orphans = list(orphans)
        self.expiring = list(expiring)
        self.all_crawl_fresh = list(all_crawl_fresh)

    async def fetch_all(self, query, values=None):
        if query == upserter._CRAWL_FRESH_ORPHAN_DEMOTE_SQL:
            self.queries.append((query, dict(values or {})))
            return [{"subject_key": k} for k in self.orphans]
        if query == upserter.CRAWL_FRESH_RECHECK_SQL:
            self.queries.append((query, dict(values or {})))
            assert self.column_present
            return [{"subject_key": k} for k in self.expiring]
        if query == upserter._CRAWL_FRESH_ROWS_ALL_SQL:
            self.queries.append((query, dict(values or {})))
            return [{"subject_key": k} for k in self.all_crawl_fresh]
        return await super().fetch_all(query, values)


async def test_recheck_on_selects_the_rows_that_have_aged_out_and_re_derives_them(monkeypatch):
    _switch_on(monkeypatch, days=7)
    db = RecheckFakeDb(expiring=[RETAILER_PK], joined_rows=[retailer_row()],
                       reads={RETAILER_PK: NOW - timedelta(days=8)})
    keys = await upserter.recheck_crawl_fresh_rows(db=db, now=NOW)
    assert keys == [RETAILER_PK]
    sent = db.sent(upserter.CRAWL_FRESH_RECHECK_SQL)
    assert sent == [{"cutoff": NOW - timedelta(days=7), "now": NOW, "limit": 20_000}]
    assert db.sent(upserter._CRAWL_FRESH_ORPHAN_DEMOTE_SQL) == [{"limit": 20_000}]
    assert db.written()[RETAILER_PK]["serving_decision"] == "shadow"


async def test_recheck_off_re_derives_every_crawl_fresh_row(monkeypatch):
    db = RecheckFakeDb(all_crawl_fresh=[RETAILER_PK], joined_rows=[retailer_row()],
                       reads={RETAILER_PK: NOW})
    keys = await upserter.recheck_crawl_fresh_rows(db=db, now=NOW, limit=5)
    assert keys == [RETAILER_PK]
    assert db.sent(upserter._CRAWL_FRESH_ROWS_ALL_SQL) == [{"limit": 5}]
    assert db.sent(upserter.CRAWL_FRESH_RECHECK_SQL) == []
    # switched off -> back to today's decision
    assert db.written()[RETAILER_PK]["serving_decision"] == "shadow"


async def test_recheck_without_the_246_column_falls_back_to_every_row(monkeypatch):
    _switch_on(monkeypatch)
    db = RecheckFakeDb(all_crawl_fresh=[], joined_rows=[], column_present=False)
    assert await upserter.recheck_crawl_fresh_rows(db=db, now=NOW) == []
    assert db.sent(upserter.CRAWL_FRESH_RECHECK_SQL) == []
    assert db.executes == []


@pytest.mark.parametrize("raw,limit", [("", 20_000), ("50", 50), ("0", 20_000), ("x", 20_000), ("-3", 20_000)])
def test_recheck_limit(monkeypatch, raw, limit):
    monkeypatch.setenv(upserter.CRAWL_FRESH_RECHECK_LIMIT_ENV, raw)
    assert upserter.crawl_fresh_recheck_limit() == limit


async def test_the_cron_rechecks_first_and_leaves_those_keys_out_of_its_pass(monkeypatch):
    from jobs import catalog_row_trust_backfill_cron as cron

    calls = []

    async def fake_recheck(*, db, **kw):
        calls.append(("recheck", kw))
        return ["pk_a"]

    async def fake_many(*, db, product_keys, **kw):
        calls.append(("many", list(product_keys)))
        return len(product_keys)

    class Db:
        async def fetch_all(self, sql, values=None):
            calls.append(("fetch", values))
            return [{"product_key": k} for k in ("pk_a", "pk_b")]

        async def fetch_one(self, sql, values=None):
            return {"missing": 0}

    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", Db())
    monkeypatch.setattr(upserter, "recheck_crawl_fresh_rows", fake_recheck)
    monkeypatch.setattr(upserter, "upsert_catalog_row_trust_many", fake_many)
    await cron.run_catalog_row_trust_backfill_tick()
    assert calls[0] == ("recheck", {})
    assert ("many", ["pk_b"]) in calls


async def test_a_failed_recheck_does_not_cost_the_pass(monkeypatch):
    from jobs import catalog_row_trust_backfill_cron as cron

    calls = []

    async def failing_recheck(**kw):
        raise RuntimeError("boom")

    async def fake_many(*, db, product_keys, **kw):
        calls.append(list(product_keys))
        return len(product_keys)

    class Db:
        async def fetch_all(self, sql, values=None):
            return [{"product_key": "pk_a"}]

        async def fetch_one(self, sql, values=None):
            return {"missing": 0}

    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", Db())
    monkeypatch.setattr(upserter, "recheck_crawl_fresh_rows", failing_recheck)
    monkeypatch.setattr(upserter, "upsert_catalog_row_trust_many", fake_many)
    await cron.run_catalog_row_trust_backfill_tick()
    assert calls == [["pk_a"]]


def test_the_statement_binds_only_the_key_list():
    from sqlalchemy import text

    assert set(text(upserter.CRAWL_ROW_PRICE_READ_SQL)._bindparams) == {"product_keys", "now"}
    assert set(text(upserter.CRAWL_FRESH_RECHECK_SQL)._bindparams) == {"cutoff", "now", "limit"}


# ---------------------------------------------------------------------------
# Round 3
# ---------------------------------------------------------------------------


async def test_a_failed_freshness_read_fails_closed_and_still_writes_the_batch(monkeypatch, caplog):
    """Flag-off verdicts for the batch, the event logged, nothing raised (the twin does the same)."""
    import logging

    off = await _write_one(CrawlFakeDb(joined_rows=[retailer_row()]))
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW}, read_fails=True)
    with caplog.at_level(logging.WARNING, logger=upserter.logger.name):
        assert await _write_one(db) == off
    assert any("catalog_row_trust_crawl_fresh_read_failed" in r.getMessage() for r in caplog.records)


async def test_a_failed_probe_fails_closed_too(monkeypatch):
    _switch_on(monkeypatch)

    class ProbeBoom(CrawlFakeDb):
        async def fetch_one(self, query, values=None):
            if query == PRICE_CHECK_COLUMN_SQL:
                raise RuntimeError("boom")
            return await super().fetch_one(query, values)

    fresh = await upserter._crawl_fresh_by_key(ProbeBoom(joined_rows=[]), [retailer_row()], NOW)
    assert fresh is None


async def test_a_naive_clock_is_read_as_utc(monkeypatch):
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[retailer_row()], reads={RETAILER_PK: NOW - timedelta(days=1)})
    fresh = await upserter._crawl_fresh_by_key(db, [retailer_row()], NOW.replace(tzinfo=None))
    assert fresh == {RETAILER_PK: True}
    assert db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL)[0]["now"] == NOW


async def test_each_batch_binds_its_own_clock(monkeypatch):
    _switch_on(monkeypatch)
    db = _paths_db()
    await upserter.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER_PK, OTHER_RETAILER_PK], limit=1)
    clocks = [v["now"] for v in db.sent(upserter.CRAWL_ROW_PRICE_READ_SQL)]
    assert len(clocks) == 2 and clocks[0] <= clocks[1] and all(c.tzinfo is not None for c in clocks)


def test_the_recheck_clock_is_utc_aware():
    assert upserter._utc(NOW.replace(tzinfo=None)) == NOW
    assert upserter._utc(None).tzinfo is not None


async def test_orphans_are_demoted_and_a_failed_batch_is_not_reported_as_rechecked(monkeypatch):
    _switch_on(monkeypatch)
    db = RecheckFakeDb(expiring=[RETAILER_PK, "ext:retailer:gone"], orphans=["ext:retailer:orphan"],
                       joined_rows=[retailer_row()], reads={})
    keys = await upserter.recheck_crawl_fresh_rows(db=db, now=NOW)
    # "ext:retailer:gone" has no catalog row in the fake: not written, not reported
    assert keys == [RETAILER_PK]


async def test_the_written_keys_out_param(monkeypatch):
    db = _paths_db()
    written = []
    await upserter.upsert_catalog_row_trust_many(db=db, product_keys=[RETAILER_PK, "nope"], written_keys=written)
    assert written == [RETAILER_PK]


async def test_the_hourly_tick_runs_the_recheck_and_honours_the_backfill_switch(monkeypatch):
    from jobs import catalog_row_trust_backfill_cron as cron

    calls = []

    async def fake_recheck(*, db, **kw):
        calls.append(db)
        return []

    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", object())
    monkeypatch.setattr(upserter, "recheck_crawl_fresh_rows", fake_recheck)
    await cron.run_catalog_row_trust_crawl_fresh_recheck_tick()
    assert len(calls) == 1
    monkeypatch.setenv("CATALOG_TRUST_BACKFILL_ENABLED", "false")
    await cron.run_catalog_row_trust_crawl_fresh_recheck_tick()
    assert len(calls) == 1


def test_the_hourly_recheck_is_scheduled():
    import inspect

    from services import audit_scheduler

    source = inspect.getsource(audit_scheduler)
    assert 'id="catalog_row_trust_crawl_fresh_recheck"' in source
    assert "run_catalog_row_trust_crawl_fresh_recheck_tick" in source


def test_the_boot_log_names_the_switch_and_n(monkeypatch):
    import utils.logger as ulog

    lines = []

    class Rec:
        def warning(self, msg, *args):
            lines.append(msg % args)

    monkeypatch.setattr(ulog, "logger", Rec())
    _switch_on(monkeypatch, days=3)
    upserter.log_crawl_fresh_config("worker")
    monkeypatch.delenv(upserter.CRAWL_FRESH_ENV)
    upserter.log_crawl_fresh_config("web")
    assert lines == [
        "catalog_row_trust_crawl_fresh_config component=worker enabled=True days=3.0 "
        "recheck_limit=20000 served_market_currency=SG:SGD,US:USD",
        "catalog_row_trust_crawl_fresh_config component=web enabled=False days=3.0 "
        "recheck_limit=20000 served_market_currency=SG:SGD,US:USD",
    ]


def test_the_boot_log_is_wired_into_every_runtime():
    import inspect

    import jobs.retailer_ingest_drain as drain

    assert 'log_crawl_fresh_config("retailer_ingest_drain")' in inspect.getsource(drain)
    main_source = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    assert "log_crawl_fresh_config(" in main_source
