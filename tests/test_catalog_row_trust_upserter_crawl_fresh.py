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

    def __init__(self, *, joined_rows, reads=None, column_present=True):
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
        if query == upserter.CRAWL_LAST_PRICE_READ_SQL:
            assert self.column_present, "the statement naming price_checked_at was sent to a DB without it"
            keys = values["product_keys"]
            return [
                {"product_key": k, "last_price_read_at": self.reads[k]}
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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == []


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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == [{"product_keys": [RETAILER_PK]}]


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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == []


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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == []


async def test_on_a_batch_with_no_retailer_listing_sends_nothing_extra(monkeypatch):
    _switch_on(monkeypatch)
    db = CrawlFakeDb(joined_rows=[make_joined_row()])
    await _write_one(db, pk="pk_internal_1")
    assert db.sent(PRICE_CHECK_COLUMN_SQL) == []
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == []


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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == [
        {"product_keys": sorted([RETAILER_PK, OTHER_RETAILER_PK, BRAND_PK])}
    ]


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
    assert db.sent(upserter.CRAWL_LAST_PRICE_READ_SQL) == [
        {"product_keys": [RETAILER_PK]},
        {"product_keys": [OTHER_RETAILER_PK]},
    ]


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
    assert upserter.CRAWL_LAST_PRICE_READ_SQL == (
        "SELECT k.product_key, r.last_price_read_at\n"
        "  FROM unnest(CAST(:product_keys AS text[])) AS k(product_key)\n"
        " CROSS JOIN LATERAL (\n"
        "   SELECT max(co.price_checked_at) AS last_price_read_at\n"
        "     FROM catalog_offers co\n"
        "    WHERE co.product_key = k.product_key\n"
        "      AND co.source_system = 'catalog_enrichment_agent_v1'\n"
        "      AND co.suppressed_at IS NULL\n"
        "      AND coalesce(co.merchant_effective_price, co.list_price) > 0\n"
        "      AND co.price_checked_at IS NOT NULL\n"
        "      AND btrim(coalesce(co.offer_payload->>'destination_url', co.source_ref),"
        " E' \\t\\n\\r\\f\\v') ~ '^https?://\\S+$'\n"
        "      AND coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%'\n"
        " ) r\n"
        " WHERE r.last_price_read_at IS NOT NULL\n"
    )


def test_the_statement_binds_only_the_key_list():
    from sqlalchemy import text

    assert set(text(upserter.CRAWL_LAST_PRICE_READ_SQL)._bindparams) == {"product_keys"}
