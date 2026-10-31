"""The attached lane's offer read and guarded UPDATE, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_offer_attached_listing_postgres.py

tests/test_external_offer_attached_listing.py proves the plan on dicts shaped like the producers'
rows. This file proves what a fake cannot: the seed SELECT's jsonb projections, the offer read's
jsonb `->>` and its catalog_skus join, and the UPDATE's currency/suppression guard and RETURNING,
all through `sync_offer_for_seed` on real Postgres against the tables the real migrations build.

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches
`public`: a per-process scratch schema, the real migrations applied there through a connection
whose search_path is that schema ALONE, dropped at teardown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    # catalog_offers.source_domain: the mirror upsert (the mirror tests below) writes it.
    "133_catalog_source_domain.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "149_catalog_offers_agent_decision_fields.sql",
    "246_catalog_offers_price_checked_at.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"attached_listing_offer_test_{os.getpid()}"

PK = "ext:missha-pdrn-peel-shot::3595c15f"
DEST = "https://missha.us/products/pdrn-peel-shot"
SEED_ID = "seed:catalog_enrichment_agent_v1:2bed56bf52daa7d4"
SELLER = "agent_seed::missha"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def _load(db, *, variant_refresh_status="all_re_read"):
    seed_data = {
        "variants": [
            {"variant_id": "47761881301179", "price_amount": 22.7},
            {"variant_id": "47761881301180", "price_amount": 38.0},
        ],
        "snapshot": {"variant_refresh": {"status": variant_refresh_status}},
    }
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, status, attached_product_key, seed_data)"
        " VALUES (:id, 'missha:2bed56bf', 'US', :dest, 'missha.us', 'PDRN Peel Shot', 22.7, 'USD',"
        "         'active', :pk, CAST(:sd AS jsonb))",
        {"id": SEED_ID, "dest": DEST, "pk": PK, "sd": json.dumps(seed_data)},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
        " title, catalog_track, truth_tier, readiness_tier, source_system, source_ref)"
        " VALUES (:pk, :m, 'external_seed', :pk, 'PDRN Peel Shot', 'external_referral',"
        "         'observed', 'referral_only', 'catalog_enrichment_agent_v1', 'agent-run-1')",
        {"pk": PK, "m": SELLER},
    )
    for sku, vid in ((f"{PK}::canonical", "canonical"), (f"{PK}::v:47761881301179", "47761881301179"),
                     (f"{PK}::v:47761881301180", "47761881301180"), (f"{PK}::v:999", "999")):
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency)"
            " VALUES (:sk, :pk, :m, 'external_seed', :pk, :vid, 'x', 'USD')",
            {"sk": sku, "pk": PK, "m": SELLER, "vid": vid},
        )

    async def offer(oid, sku, price, *, merchant=SELLER, dest=DEST, currency="USD", suppressed=False):
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, list_price, merchant_effective_price, estimated_best_price,"
            " source_system, source_ref, offer_payload, suppressed_at, updated_at)"
            " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only',"
            "         'external_referral', 'default', 'in_stock', :cur, :p, :p, :p,"
            "         'catalog_enrichment_agent_v1', :dest, CAST(:payload AS jsonb),"
            "         CASE WHEN :sup THEN NOW() END, TIMESTAMP '2026-09-12 01:37:40')",
            {"oid": oid, "sk": sku, "pk": PK, "m": merchant, "cur": currency, "p": price,
             "dest": dest, "payload": json.dumps({"destination_url": dest}), "sup": suppressed},
        )

    await offer("of_canon", f"{PK}::canonical", 29.00)
    await offer("of_v1", f"{PK}::v:47761881301179", 29.00)
    await offer("of_v2", f"{PK}::v:47761881301180", 40.00)
    await offer("of_v999", f"{PK}::v:999", 11.00)                  # not on the seed
    await offer("of_ulta", f"{PK}::canonical", 31.00, merchant="agent_seed::retailer::ulta.com",
                dest="https://www.ulta.com/p/pdrn")                 # another seller's listing


async def _offers(db):
    rows = await db.fetch_all(
        "SELECT offer_id, list_price, merchant_effective_price, estimated_best_price, currency,"
        " updated_at > TIMESTAMP '2026-09-12 01:37:40' AS touched,"
        " price_checked_at IS NOT NULL AS checked FROM catalog_offers"
    )
    return {r["offer_id"]: dict(r) for r in rows}


async def _sync(monkeypatch, db, source="refresh"):
    from services import external_offer_dual_write as mod

    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})
    return await mod.sync_offer_for_seed(SEED_ID, attached_price_source=source, currency_read=source == "refresh")


async def test_the_listing_rows_take_the_re_read_prices_and_nothing_else_moves(scoped_db, monkeypatch):
    await _load(scoped_db)
    result = await _sync(monkeypatch, scoped_db)

    assert result["status"] == "synced" and result["target"] == "attached", result
    assert result["offers_written"] == 3
    assert result["offer_skips"] == {"variant_not_on_seed": 1}
    rows = await _offers(scoped_db)
    for oid, price in (("of_canon", 22.7), ("of_v1", 22.7), ("of_v2", 38.0)):
        row = rows[oid]
        assert float(row["list_price"]) == price, oid
        assert float(row["merchant_effective_price"]) == price
        assert float(row["estimated_best_price"]) == price
        assert row["currency"] == "USD" and row["touched"], oid
        # The read dates the price it wrote (mig 246), which is what the gateway's as-of reads.
        assert row["checked"], oid
    for oid, price in (("of_v999", 11.0), ("of_ulta", 31.0)):
        assert float(rows[oid]["list_price"]) == price and not rows[oid]["touched"], oid
        assert not rows[oid]["checked"], oid


@pytest.mark.parametrize("snapshot_sql", [
    # skintific.com's shape: column USD, page read AUD.
    "jsonb_set(seed_data, '{snapshot,price_currency}', '\"AUD\"')",
    # a snapshot stored as a JSON string hides the page read: the readers price nothing off it.
    "jsonb_set(seed_data, '{snapshot}', to_jsonb('{\"price_currency\": \"USD\"}'::text))",
])
async def test_a_seed_the_readers_cannot_price_reprices_and_stamps_nothing(scoped_db, monkeypatch, snapshot_sql):
    """PR controller review of #2657 (H1): the attached lane compared only the column to each row, so a
    seed whose column and page read disagree re-priced its USD rows and stamped them freshly checked
    every night, rows every reader withholds. Through the real sync it is refused, counted as
    `refused_currency`, and no row moves. (The other tests here are the agreeing control.)"""
    await _load(scoped_db)
    # The listing's rows as the variant backfill copied them off the seed: the rows the readers judge.
    await scoped_db.execute("UPDATE catalog_offers SET source_system = 'variant_identity_backfill_v1' WHERE offer_id <> 'of_ulta'")
    await scoped_db.execute(f"UPDATE external_product_seeds SET seed_data = {snapshot_sql} WHERE id = :id", {"id": SEED_ID})
    result = await _sync(monkeypatch, scoped_db)
    assert result["status"] == "refused_currency", result
    rows = await _offers(scoped_db)
    for oid in ("of_canon", "of_v1", "of_v2", "of_v999", "of_ulta"):
        assert not rows[oid]["touched"] and not rows[oid]["checked"], oid


_AUD_PAGE = "UPDATE external_product_seeds SET seed_data = jsonb_set(seed_data, '{snapshot,price_currency}', '\"AUD\"') WHERE id = :id"


async def test_a_read_of_such_a_seed_reprices_no_enrichment_row(scoped_db, monkeypatch):
    """Peng 2026-10-22, after #2657 (M2): a refresh of a seed whose column and page read disagree
    re-prices and stamps none of the enrichment lane's rows (this fixture's own shape). Nothing is
    suppressed: every row keeps its last price."""
    await _load(scoped_db)
    before = await _offers(scoped_db)
    await scoped_db.execute(_AUD_PAGE, {"id": SEED_ID})
    result = await _sync(monkeypatch, scoped_db, source="refresh")
    assert result["status"] == "refused_currency", result
    rows = await _offers(scoped_db)
    for oid, row in rows.items():
        assert not row["touched"] and not row["checked"], oid
        assert float(row["list_price"]) == float(before[oid]["list_price"]), oid


async def test_an_employee_edit_of_the_same_seed_reprices_them_as_main(scoped_db, monkeypatch):
    """Peng 2026-10-22: the same seed under an explicit employee edit re-prices and stamps the enrichment
    lane's product row, attributed `price_read_source = employee_edit`."""
    await _load(scoped_db)
    await scoped_db.execute(_AUD_PAGE, {"id": SEED_ID})
    result = await _sync(monkeypatch, scoped_db, source="employee_edit")
    assert result["status"] == "synced" and result["offers_written"] >= 1, result
    rows, sources = await _offers(scoped_db), await _read_sources(scoped_db)
    assert rows["of_canon"]["touched"] and rows["of_canon"]["checked"]
    assert sources["of_canon"] == "employee_edit"


async def _read_sources(db):
    rows = await db.fetch_all("SELECT offer_id, offer_payload->>'price_read_source' AS src FROM catalog_offers")
    return {r["offer_id"]: r["src"] for r in rows}


@pytest.mark.parametrize("source", ["refresh", "employee_edit"])
async def test_the_lane_records_who_vouched_for_the_read(scoped_db, monkeypatch, source):
    """Through the real lane, with migration 246's column present: every row the lane prices is dated AND
    says who vouched for it; a row it does not price carries no marker."""
    from services import external_offer_dual_write as mod

    await _load(scoped_db)
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", scoped_db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})  # re-probe: present
    assert await mod.price_check_column_present() is True
    result = await mod.sync_offer_for_seed(SEED_ID, attached_price_source=source, currency_read=True)
    assert result["status"] == "synced", result
    rows, sources = await _offers(scoped_db), await _read_sources(scoped_db)
    for oid, row in rows.items():
        assert sources[oid] == (source if row["touched"] else None), oid
        assert row["checked"] == bool(row["touched"]), oid
    assert sources["of_canon"] == source and rows["of_canon"]["checked"]


async def test_variant_rows_hold_until_every_variant_was_re_read(scoped_db, monkeypatch):
    await _load(scoped_db, variant_refresh_status="not_all_re_read")
    result = await _sync(monkeypatch, scoped_db)
    assert result["offers_written"] == 1
    rows = await _offers(scoped_db)
    assert float(rows["of_canon"]["list_price"]) == 22.7
    assert float(rows["of_v2"]["list_price"]) == 40.0 and not rows["of_v2"]["touched"]
    assert rows["of_canon"]["checked"] and not rows["of_v2"]["checked"]


async def test_the_guard_refuses_a_row_whose_currency_moved_after_the_read(scoped_db, monkeypatch):
    """The UPDATE re-checks currency and suppression: a concurrent writer wins, and RETURNING
    tells the lane it wrote nothing."""
    from services import external_offer_dual_write as mod

    await _load(scoped_db)
    read = dict(next(r for r in await scoped_db.fetch_all(mod.ATTACHED_LISTING_OFFERS_SQL, {"product_key": PK})
                     if r["offer_id"] == "of_canon"))
    values = {"offer_id": "of_canon", "price": 22.7, "currency": "USD",
              **{k: read[k] for k in ("read_offer", "read_sku", "read_product")}}
    await scoped_db.execute("UPDATE catalog_offers SET currency = 'GBP' WHERE offer_id = 'of_canon'")
    row = await scoped_db.fetch_one(
        mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, values
    )
    assert row is None
    await scoped_db.execute("UPDATE catalog_offers SET currency = 'USD', suppressed_at = NOW() WHERE offer_id = 'of_canon'")
    row = await scoped_db.fetch_one(
        mod.ATTACHED_LISTING_OFFER_UPDATE_SQL, values
    )
    assert row is None
    assert float((await _offers(scoped_db))["of_canon"]["list_price"]) == 29.0


@pytest.mark.parametrize('mutation', [
    "UPDATE catalog_offers SET source_ref='https://missha.us/products/other', offer_payload='{}' WHERE offer_id='of_v1'",
    "UPDATE catalog_offers SET merchant_id='other_seller' WHERE offer_id='of_v1'",
    "UPDATE catalog_offers SET market='CA' WHERE offer_id='of_v1'",
    "UPDATE catalog_offers SET merchant_effective_price=33 WHERE offer_id='of_v1'",
    "UPDATE catalog_offers SET offer_payload=jsonb_build_object('price_repair',NULL) WHERE offer_id='of_v1'",
    "UPDATE catalog_skus SET source_variant_id='47761881309999' WHERE source_variant_id='47761881301179'",
    "UPDATE catalog_skus SET currency='EUR' WHERE source_variant_id='47761881301179'",
    "UPDATE catalog_skus SET suppression_reason='intentional' WHERE source_variant_id='47761881301179'",
    "UPDATE catalog_products SET source_ref='other-source' WHERE product_key='ext:missha-pdrn-peel-shot::3595c15f'",
])
async def test_changed_listing_or_identity_is_not_repriced_or_stamped(scoped_db,mutation):
    from services import external_offer_dual_write as mod
    await _load(scoped_db)
    read = next(r for r in await scoped_db.fetch_all(mod.ATTACHED_LISTING_OFFERS_SQL,{'product_key':PK})
                if r['offer_id']=='of_v1')
    values = {'offer_id':'of_v1','price':22.7,'currency':'USD',
              **{k:read[k] for k in ('read_offer','read_sku','read_product')}}
    await scoped_db.execute(mutation)
    before = dict(await scoped_db.fetch_one("SELECT to_jsonb(o)::text AS state FROM catalog_offers o WHERE offer_id='of_v1'"))
    assert await scoped_db.fetch_one(mod.ATTACHED_LISTING_OFFER_UPDATE_SQL,values) is None
    after = dict(await scoped_db.fetch_one("SELECT to_jsonb(o)::text AS state FROM catalog_offers o WHERE offer_id='of_v1'"))
    assert before == after


async def test_reviewed_repair_stays_untouched_through_real_projection(scoped_db,monkeypatch):
    await _load(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET offer_payload=offer_payload || jsonb_build_object('price_repair',jsonb_build_object('evidence_hash','reviewed')) WHERE offer_id='of_v1'")
    before = dict(await scoped_db.fetch_one("SELECT to_jsonb(o)::text AS state FROM catalog_offers o WHERE offer_id='of_v1'"))
    result = await _sync(monkeypatch,scoped_db)
    assert result['offers_written'] == 2
    assert result['offer_skips'] == {'reviewed_price_repair':1,'variant_not_on_seed':1}
    after = dict(await scoped_db.fetch_one("SELECT to_jsonb(o)::text AS state FROM catalog_offers o WHERE offer_id='of_v1'"))
    assert before == after


async def test_variant_currency_conflict_never_gets_a_fresh_price_clock(scoped_db,monkeypatch):
    await _load(scoped_db)
    await scoped_db.execute("UPDATE external_product_seeds SET seed_data=jsonb_set(seed_data,'{variants,1,price_currency}',to_jsonb('EUR'::text)) WHERE id=:id",{'id':SEED_ID})
    result = await _sync(monkeypatch,scoped_db)
    assert result['offer_skips']['variant_currency_mismatch'] == 1
    row = (await _offers(scoped_db))['of_v2']
    assert float(row['list_price']) == 40 and not row['checked'] and not row['touched']


@pytest.mark.parametrize("payload", ['"double-encoded"', '[1, 2]'])
async def test_a_non_object_payload_passes_through_while_the_price_and_its_date_update(scoped_db, monkeypatch, payload):
    """jsonb_set raises on a scalar or an array payload. The marker is written only on an object (or absent)
    payload; any other shape passes through untouched, and the row is still re-priced and dated."""
    from services import external_offer_dual_write as mod

    await _load(scoped_db)
    await scoped_db.execute(
        "UPDATE catalog_offers SET offer_payload = CAST(:p AS jsonb) WHERE offer_id = 'of_canon'", {"p": payload})
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setattr(mod, "database", scoped_db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})  # re-probe: present
    assert await mod.price_check_column_present() is True
    result = await mod.sync_offer_for_seed(SEED_ID, attached_price_source="employee_edit", currency_read=True)
    assert result["status"] == "synced", result
    row = await scoped_db.fetch_one(
        "SELECT list_price, price_checked_at IS NOT NULL AS checked, offer_payload::text AS payload,"
        " jsonb_typeof(offer_payload) AS kind FROM catalog_offers WHERE offer_id = 'of_canon'")
    assert float(row["list_price"]) == 22.7 and row["checked"]
    assert row["kind"] in ("string", "array") and row["payload"] == payload


# ── a seed WITH a mirror: the refresh re-prices the mirror's variant rows (2026-10-31) ───────────
#
# The rows are the producers' shapes on a live mirror product:
#   * the mirror's product-level row (`upsert_catalog_offer_from_seed_row`): sku `<pk>::canonical`;
#   * `scripts/backfill_variant_identity_skus`: sku `<pk>::v:<vid>`, source_ref = the listing URL,
#     payload {destination_url}, source_system variant_identity_backfill_v1 (prod: written 09-08);
#   * the mirror's older per-variant row: source_variant_id `<source_product_id>:<vid>`, source_ref =
#     the seed id, payload {external_seed_id}.
# Prod 2026-10-31 (Centella Teca Ampoule): product-level row $15.30 checked 10-20, variant row $13.60
# from 09-08 never checked, and the PDP served the variant row.

MIRROR_PK = "prod::external_seed::external_seed::ext_74e14bd64686117a6145542a"
MIRROR_SEED = "eps_6c74aab66d1ecda52b73fa80"
MIRROR_SELLER = "merch_obs_bb9261c0db7aef08"
MIRROR_SOURCE_PRODUCT = "ext_74e14bd64686117a6145542a"
MIRROR_DEST = "https://www.skin1004.com/products/centella-teca-ampoule"


async def _load_mirror(db, *, variant_refresh_status="all_re_read"):
    seed_data = {
        "variants": [
            {"variant_id": "49884244017398", "price": 15.3, "price_amount": 15.3, "price_currency": "USD"},
            {"variant_id": "49884244017399", "price": 26.0, "price_amount": 26.0, "price_currency": "USD"},
        ],
        # The snapshot list is the ingest-time copy (its own defect); the planner reads the top-level list.
        "snapshot": {"variants": [{"variant_id": "49884244017398", "price": "13.60"}],
                     "variant_refresh": {"status": variant_refresh_status}},
    }
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, availability, status, attached_product_key, seed_data)"
        " VALUES (:id, :ext, 'US', :dest, 'www.skin1004.com', 'Centella Teca Ampoule', 15.3, 'USD',"
        "         'in_stock', 'active', :pk, CAST(:sd AS jsonb))",
        {"id": MIRROR_SEED, "ext": MIRROR_SOURCE_PRODUCT, "dest": MIRROR_DEST, "pk": MIRROR_PK,
         "sd": json.dumps(seed_data)},
    )
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id,"
        " title, catalog_track, truth_tier, readiness_tier, source_system, source_ref)"
        " VALUES (:pk, :m, 'external_seed', :spid, 'Centella Teca Ampoule', 'external_referral',"
        "         'observed', 'referral_only', 'external_product_seeds_mirror_v1', :seed)",
        {"pk": MIRROR_PK, "m": MIRROR_SELLER, "spid": MIRROR_SOURCE_PRODUCT, "seed": MIRROR_SEED},
    )
    for sku, vid in ((f"{MIRROR_PK}::canonical", MIRROR_PK),
                     (f"{MIRROR_PK}::v:49884244017398", "49884244017398"),
                     (f"{MIRROR_SOURCE_PRODUCT}:49884244017399", f"{MIRROR_SOURCE_PRODUCT}:49884244017399"),
                     (f"{MIRROR_PK}::v:49884244017400", "49884244017400")):
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency)"
            " VALUES (:sk, :pk, :m, 'external_seed', :spid, :vid, 'x', 'USD')",
            {"sk": sku, "pk": MIRROR_PK, "m": MIRROR_SELLER, "spid": MIRROR_SOURCE_PRODUCT, "vid": vid},
        )

    async def offer(oid, sku, price, *, source_system, source_ref, payload):
        await db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
            " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
            " currency, market, list_price, merchant_effective_price, estimated_best_price,"
            " source_system, source_ref, offer_payload, offer_type, updated_at)"
            " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only',"
            "         'redirect', 'external_referral', 'in_stock', 'USD', 'US', :p, :p, :p,"
            "         :ss, :ref, CAST(:payload AS jsonb), 'brand_direct', TIMESTAMP '2026-09-08 06:07:17')",
            {"oid": oid, "sk": sku, "pk": MIRROR_PK, "m": MIRROR_SELLER, "p": price, "ss": source_system,
             "ref": source_ref, "payload": json.dumps(payload)},
        )

    from services.external_offer_dual_write import derive_mirror_offer_id

    await offer(derive_mirror_offer_id(MIRROR_PK), f"{MIRROR_PK}::canonical", 13.60,
                source_system="external_product_seeds_mirror_v1", source_ref=MIRROR_SEED,
                payload={"external_seed_id": MIRROR_SEED, "destination_url": MIRROR_DEST})
    await offer("of_backfill", f"{MIRROR_PK}::v:49884244017398", 13.60,
                source_system="variant_identity_backfill_v1", source_ref=MIRROR_DEST,
                payload={"source_system": "variant_identity_backfill_v1", "price_from": "variant",
                         "destination_url": MIRROR_DEST})
    await offer("of_old_mirror_variant", f"{MIRROR_SOURCE_PRODUCT}:49884244017399", 24.00,
                source_system="external_product_seeds_mirror_v1", source_ref=MIRROR_SEED,
                payload={"external_seed_id": MIRROR_SEED, "destination_url": MIRROR_DEST})
    await offer("of_not_on_seed", f"{MIRROR_PK}::v:49884244017400", 11.00,
                source_system="variant_identity_backfill_v1", source_ref=MIRROR_DEST,
                payload={"destination_url": MIRROR_DEST})


async def _sync_mirror(monkeypatch, db, source="refresh"):
    from services import external_offer_dual_write as mod

    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    # The missing-variant projection has its own suite; here the rows above are the whole mirror.
    monkeypatch.setenv("CATALOG_VARIANT_OFFER_PROJECTION_ENABLED", "0")
    monkeypatch.setattr(mod, "database", db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})
    return await mod.sync_offer_for_seed(MIRROR_SEED, attached_price_source=source, currency_read=source == "refresh")


async def _mirror_offers(db):
    rows = await db.fetch_all(
        "SELECT offer_id, list_price, merchant_effective_price, estimated_best_price,"
        " updated_at > TIMESTAMP '2026-09-08 06:07:17' AS touched,"
        " price_checked_at IS NOT NULL AS checked, offer_payload ->> 'price_read_source' AS read_source"
        " FROM catalog_offers WHERE product_key = :pk",
        {"pk": MIRROR_PK},
    )
    return {r["offer_id"]: dict(r) for r in rows}


async def test_a_refresh_reprices_the_mirrors_variant_rows_not_only_its_product_row(scoped_db, monkeypatch):
    from services.external_offer_dual_write import derive_mirror_offer_id

    await _load_mirror(scoped_db)
    result = await _sync_mirror(monkeypatch, scoped_db)

    assert result["status"] == "synced" and result["target"] == "mirror", result
    assert result["variant_status"] == "synced" and result["variant_offers_written"] == 2, result
    assert result["offer_skips"] == {"mirror_variants_variant_not_on_seed": 1}
    rows = await _mirror_offers(scoped_db)
    # The mirror upsert still owns the product-level row.
    assert float(rows[derive_mirror_offer_id(MIRROR_PK)]["list_price"]) == 15.3
    # The row the PDP prices the variant from now holds the re-read, dated, with who vouched for it.
    for oid, price in (("of_backfill", 15.3), ("of_old_mirror_variant", 26.0)):
        row = rows[oid]
        assert float(row["list_price"]) == price, oid
        assert float(row["merchant_effective_price"]) == price
        assert float(row["estimated_best_price"]) == price
        assert row["touched"] and row["checked"] and row["read_source"] == "refresh", oid
    # A variant the page did not list keeps its price and its clock.
    assert float(rows["of_not_on_seed"]["list_price"]) == 11.0
    assert not rows["of_not_on_seed"]["touched"] and not rows["of_not_on_seed"]["checked"]


async def test_mirror_variant_rows_hold_until_every_variant_was_re_read(scoped_db, monkeypatch):
    await _load_mirror(scoped_db, variant_refresh_status="not_all_re_read")
    result = await _sync_mirror(monkeypatch, scoped_db)

    assert result["status"] == "synced" and result["variant_status"] == "listing_offer_not_written", result
    assert result["offer_skips"] == {"mirror_variants_variant_not_re_read": 2, "mirror_variants_variant_not_on_seed": 1}
    rows = await _mirror_offers(scoped_db)
    assert float(rows["of_backfill"]["list_price"]) == 13.6 and not rows["of_backfill"]["checked"]


async def test_an_employee_edit_moves_no_mirror_variant_row(scoped_db, monkeypatch):
    await _load_mirror(scoped_db)
    result = await _sync_mirror(monkeypatch, scoped_db, source="employee_edit")

    assert result["status"] == "synced" and "variant_status" not in result, result
    rows = await _mirror_offers(scoped_db)
    assert float(rows["of_backfill"]["list_price"]) == 13.6 and not rows["of_backfill"]["touched"]


# ── review of #2684: what the planner cannot see on a mirror, on real Postgres ───────────────────


async def _add_mirror_offer(db, oid, sku, vid, price, *, merchant=MIRROR_SELLER, market="US", currency="USD",
                            new_sku=True, payload=None):
    if new_sku:
        await db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,"
            " source_product_id, source_variant_id, title, currency)"
            " VALUES (:sk, :pk, :m, 'external_seed', :spid, :vid, 'x', :cur)",
            {"sk": sku, "pk": MIRROR_PK, "m": merchant, "spid": MIRROR_SOURCE_PRODUCT, "vid": vid, "cur": currency},
        )
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
        " catalog_track, truth_tier, readiness_tier, offer_mode, channel, availability,"
        " currency, market, list_price, merchant_effective_price, estimated_best_price,"
        " source_system, source_ref, offer_payload, offer_type, updated_at)"
        " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only',"
        "         'redirect', 'external_referral', 'in_stock', :cur, :mkt, :p, :p, :p,"
        "         'variant_identity_backfill_v1', :ref, CAST(:payload AS jsonb), 'brand_direct',"
        "         TIMESTAMP '2026-09-08 06:07:17')",
        {"oid": oid, "sk": sku, "pk": MIRROR_PK, "m": merchant, "p": price, "ref": MIRROR_DEST,
         "cur": currency, "mkt": market, "payload": json.dumps(payload or {"destination_url": MIRROR_DEST})},
    )


async def test_a_mirror_variant_row_in_another_market_keeps_its_price(scoped_db, monkeypatch):
    """Reviewer's reproduction: a US/USD seed wrote 15.30 onto its mirror's EC (USD) variant row."""
    await _load_mirror(scoped_db)
    await _add_mirror_offer(scoped_db, "of_ec", f"{MIRROR_PK}::v:49884244017398", "49884244017398", 13.6,
                            market="EC", new_sku=False)
    result = await _sync_mirror(monkeypatch, scoped_db)
    rows = await _mirror_offers(scoped_db)
    assert float(rows["of_ec"]["list_price"]) == 13.6 and not rows["of_ec"]["checked"], result
    assert result["offer_skips"]["mirror_variants_mirror_market_mismatch"] == 1
    assert float(rows["of_backfill"]["list_price"]) == 15.3  # the US row on the same sku still moves


async def test_mirror_variant_rows_of_another_seller_keep_their_price(scoped_db, monkeypatch):
    """Reviewer's reproduction: variant rows under another merchant took the seed's price (13.60 → 15.30)."""
    await _load_mirror(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET merchant_id = 'merch_obs_OTHER'"
                            " WHERE offer_id IN ('of_backfill', 'of_old_mirror_variant', 'of_not_on_seed')")
    result = await _sync_mirror(monkeypatch, scoped_db)
    rows = await _mirror_offers(scoped_db)
    assert float(rows["of_backfill"]["list_price"]) == 13.6 and not rows["of_backfill"]["checked"], result
    assert result["variant_status"] == "listing_offer_not_written"
    assert result["offer_skips"] == {"mirror_variants_mirror_other_seller": 3}


async def test_the_mirrors_own_offer_on_a_variant_sku_takes_only_the_upserts_price(scoped_db, monkeypatch):
    from services.external_offer_dual_write import derive_mirror_offer_id

    await _load_mirror(scoped_db)
    oid = derive_mirror_offer_id(MIRROR_PK)
    await scoped_db.execute("DELETE FROM catalog_offers WHERE offer_id = 'of_old_mirror_variant'")
    await scoped_db.execute("UPDATE catalog_offers SET sku_key = :sk WHERE offer_id = :oid",
                            {"sk": f"{MIRROR_SOURCE_PRODUCT}:49884244017399", "oid": oid})
    result = await _sync_mirror(monkeypatch, scoped_db)
    rows = await _mirror_offers(scoped_db)
    assert float(rows[oid]["list_price"]) == 15.3  # the upsert's price (the seed's), not the variant's 26.0
    assert result["variant_offers_written"] == 1  # of_backfill only


async def test_a_reviewed_price_repair_and_another_seeds_row_never_move(scoped_db, monkeypatch):
    await _load_mirror(scoped_db)
    await scoped_db.execute("UPDATE catalog_offers SET offer_payload = offer_payload"
                            " || CAST('{\"price_repair\": {\"x\": 1}}' AS jsonb) WHERE offer_id = 'of_backfill'")
    await scoped_db.execute("UPDATE catalog_offers SET offer_payload = offer_payload"
                            " || CAST('{\"external_seed_id\": \"eps_other\"}' AS jsonb) WHERE offer_id = 'of_old_mirror_variant'")
    result = await _sync_mirror(monkeypatch, scoped_db)
    rows = await _mirror_offers(scoped_db)
    assert float(rows["of_backfill"]["list_price"]) == 13.6, result
    assert float(rows["of_old_mirror_variant"]["list_price"]) == 24.0
    assert result["offer_skips"]["mirror_variants_reviewed_price_repair"] == 1


async def test_a_variant_update_that_fails_in_postgres_leaves_the_mirror_row_written(scoped_db, monkeypatch):
    from services import external_offer_dual_write as mod

    await _load_mirror(scoped_db)
    bad = ("UPDATE catalog_offers SET list_price = 1/0 WHERE offer_id = :offer_id AND :price > 0"
           " AND :currency <> '' AND :read_offer <> '' AND :read_sku <> '' AND :read_product <> ''"
           " AND :read_source <> '' RETURNING offer_id")
    monkeypatch.setattr(mod, "ATTACHED_LISTING_OFFER_UPDATE_SQL", bad)
    monkeypatch.setattr(mod, "ATTACHED_LISTING_OFFER_UPDATE_SQL_WITHOUT_PRICE_CHECK", bad)
    result = await _sync_mirror(monkeypatch, scoped_db)
    rows = await _mirror_offers(scoped_db)
    assert result["status"] == "synced" and result["variant_status"] == "error", result
    assert float(rows[mod.derive_mirror_offer_id(MIRROR_PK)]["list_price"]) == 15.3


async def test_a_row_in_the_variant_projections_shape_is_repriced_with_the_projection_on(scoped_db, monkeypatch):
    """The third producer's row shape (`catalog_variant_offer_projection.plan_offers`: offer_id
    `offer:variant-projection:<hash>`, source_ref = the listing URL, payload external_seed_id + variant_id
    + price_freshness), with the projection itself ON inside the mirror upsert, as prod runs it."""
    from services import external_offer_dual_write as mod

    await _load_mirror(scoped_db)
    await scoped_db.execute(
        "UPDATE catalog_offers SET offer_id = 'offer:variant-projection:0123abcd',"
        " source_system = 'variant_offer_projection_v1', offer_payload = CAST(:payload AS jsonb)"
        " WHERE offer_id = 'of_backfill'",
        {"payload": json.dumps({"source_system": "variant_offer_projection_v1", "external_seed_id": MIRROR_SEED,
                                "variant_id": "49884244017398", "variant_id_provenance": "merchant_issued",
                                "price_from": "variant", "destination_url": MIRROR_DEST,
                                "price_freshness": "stored_snapshot_only"})},
    )
    monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
    monkeypatch.setenv("CATALOG_VARIANT_OFFER_PROJECTION_ENABLED", "1")
    monkeypatch.setattr(mod, "database", scoped_db)
    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})
    result = await mod.sync_offer_for_seed(MIRROR_SEED, attached_price_source="refresh", currency_read=True)
    rows = await _mirror_offers(scoped_db)
    row = rows["offer:variant-projection:0123abcd"]
    assert result["status"] == "synced" and result["variant_status"] == "synced", result
    assert float(row["list_price"]) == 15.3 and row["checked"] and row["read_source"] == "refresh"


# ── the dry-run script, end to end against the same rows ───────────────────────────────────────


def _run_plan_script(env_over):
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
    env.update(env_over)
    return subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "scripts/ops/plan_mirror_variant_reprice.py")],
                          env=env, capture_output=True, text=True, timeout=120)


def _plan_lines(out):
    lines = out.stdout.splitlines()
    assert out.returncode == 0, out.stderr[-2000:]
    assert lines[0] == "GATE_OPEN", lines
    data = [l for l in lines[1:] if l.startswith("MVR_PLAN ")]
    assert lines[-1] == f"MVR_PLAN DONE lines={len(data) - 1}" and len(data) == len(lines) - 1, lines
    kinds = {}
    for line in data[:-1]:
        _, kind, payload = line.split(" ", 2)
        kinds.setdefault(kind, []).append(json.loads(payload))
    return kinds


def _plan_env(timeout="30"):
    sep = "&" if "?" in _url() else "?"
    return {"PIVOTA_ENV": "production", "DB_STATEMENT_TIMEOUT_SECONDS": timeout,
            "DATABASE_URL": f"{_url()}{sep}search_path={_SCHEMA}"}


async def test_the_dry_run_plans_exactly_what_the_lane_writes_and_writes_nothing(scoped_db):
    await _load_mirror(scoped_db)
    before = await _mirror_offers(scoped_db)
    kinds = _plan_lines(_run_plan_script(_plan_env()))
    summary = kinds["summary"][0]
    assert summary["products"] == 1 and summary["variant_rows"] == 3 and summary["planned_writes"] == 2
    assert summary["price_moves"] == 2 and summary["ours_lower_now"] == 2 and summary["assumes_currency_read"] is True
    assert kinds["statuses"] == [{"planned": 1}] and kinds["skips"] == [{"variant_not_on_seed": 1}]
    assert kinds["excluded"] == [{}] and kinds["variant_refresh_status"] == [{"all_re_read": 1}]
    assert sorted((s["before"], s["after"]) for s in kinds["sample"][0]) == [(13.6, 15.3), (24.0, 26.0)]
    # Every chunk ran under the bound it was given, read back inside its own transaction.
    assert [p["statement_timeout"] for p in kinds["progress"]] == ["30s"]
    assert await _mirror_offers(scoped_db) == before, "a dry run wrote"


async def test_the_dry_run_counts_the_mirrors_own_offer_on_a_variant_sku_as_product_level(scoped_db):
    """Only the offer-id exclusion applies to this row (its sku is a variant sku), in the scan and the plan."""
    from services.external_offer_dual_write import derive_mirror_offer_id

    await _load_mirror(scoped_db)
    await scoped_db.execute("DELETE FROM catalog_offers WHERE offer_id = 'of_old_mirror_variant'")
    await scoped_db.execute("UPDATE catalog_offers SET sku_key = :sk WHERE offer_id = :oid",
                            {"sk": f"{MIRROR_SOURCE_PRODUCT}:49884244017399", "oid": derive_mirror_offer_id(MIRROR_PK)})
    summary = _plan_lines(_run_plan_script(_plan_env()))["summary"][0]
    assert summary["variant_rows"] == 2 and summary["planned_writes"] == 1, summary


async def test_the_dry_run_plans_only_what_the_refresh_reaches_and_counts_the_rest(scoped_db):
    import hashlib

    await _load_mirror(scoped_db)

    async def mirror_with_a_variant_row(pk, seed_id, *, updated_at=None):
        # (merchant, platform, source_variant_id) is unique across products: one variant id per product.
        vid = "7" + str(int(hashlib.sha256(pk.encode()).hexdigest(), 16) % 10**11).zfill(11)
        await scoped_db.execute(
            "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,"
            " catalog_track, truth_tier, readiness_tier, source_system, source_ref, updated_at)"
            " VALUES (:pk, :m, 'external_seed', :pk, 'x', 'external_referral', 'observed', 'referral_only',"
            "         'external_product_seeds_mirror_v1', :seed, COALESCE(:at, now()))",
            {"pk": pk, "m": MIRROR_SELLER, "seed": seed_id, "at": updated_at},
        )
        await scoped_db.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
            " source_variant_id, title, currency) VALUES (:sk, :pk, :m, 'external_seed', :pk, :vid, 'x', 'USD')",
            {"sk": pk + "::v:" + vid, "pk": pk, "m": MIRROR_SELLER, "vid": vid},
        )
        await scoped_db.execute(
            "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
            " readiness_tier, offer_mode, channel, availability, currency, market, list_price,"
            " merchant_effective_price, estimated_best_price, source_system, source_ref, offer_payload)"
            " VALUES (:oid, :sk, :pk, :m, 'external_referral', 'observed', 'referral_only', 'redirect',"
            "         'external_referral', 'in_stock', 'USD', 'US', 9, 9, 9, 'variant_identity_backfill_v1', :ref,"
            "         CAST(:payload AS jsonb))",
            {"oid": "of_" + pk[-8:], "sk": pk + "::v:" + vid, "pk": pk, "m": MIRROR_SELLER,
             "ref": MIRROR_DEST, "payload": json.dumps({"destination_url": MIRROR_DEST})},
        )

    async def seed(seed_id, *, status, external_product_id):
        await scoped_db.execute(
            "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url, domain, title,"
            " price_amount, price_currency, availability, status, seed_data)"
            " VALUES (:id, :ext, 'US', :dest, 'www.skin1004.com', 'x', 15.3, 'USD', 'in_stock', :st, CAST('{}' AS jsonb))",
            {"id": seed_id, "ext": external_product_id, "dest": MIRROR_DEST, "st": status},
        )

    # An older second mirror of the same seed: resolve_mirror_product picks the newest only.
    from datetime import datetime

    await mirror_with_a_variant_row("prod::older::mirror::aaaaaaa1", MIRROR_SEED, updated_at=datetime(2020, 1, 1))
    await seed("eps_inactive", status="inactive", external_product_id="ext_inactive")
    await mirror_with_a_variant_row("prod::inactive::mirror::bbbbbbb2", "eps_inactive")
    await seed("eps_no_epid", status="active", external_product_id="")
    await mirror_with_a_variant_row("prod::no_epid::mirror::ccccccc3", "eps_no_epid")
    await mirror_with_a_variant_row("prod::orphan::mirror::ddddddd4", "eps_gone")

    kinds = _plan_lines(_run_plan_script(_plan_env()))
    summary = kinds["summary"][0]
    assert summary["products"] == 1 and summary["mirror_products_with_variant_rows"] == 5, summary
    assert summary["variant_rows"] == 3 and summary["planned_writes"] == 2
    assert kinds["excluded"] == [{
        "not_the_resolved_mirror": {"products": 1, "variant_rows": 1},
        "seed_status_inactive": {"products": 1, "variant_rows": 1},
        "seed_no_external_product_id": {"products": 1, "variant_rows": 1},
        "seed_missing": {"products": 1, "variant_rows": 1},
    }]


async def test_the_scan_never_counts_a_mirror_whose_only_extra_row_is_its_own_offer(scoped_db):
    """The scan applies the offer-id exclusion too: a mirror (here on an inactive seed) whose only
    non-canonical-sku row is the mirror's own offer has no variant row, so it is in no bucket at all."""
    from services.external_offer_dual_write import derive_mirror_offer_id

    await _load_mirror(scoped_db)
    await scoped_db.execute("UPDATE external_product_seeds SET status = 'inactive' WHERE id = :id", {"id": MIRROR_SEED})
    await scoped_db.execute("DELETE FROM catalog_offers WHERE offer_id <> :oid", {"oid": derive_mirror_offer_id(MIRROR_PK)})
    await scoped_db.execute("UPDATE catalog_offers SET sku_key = :sk", {"sk": f"{MIRROR_PK}::v:49884244017398"})
    kinds = _plan_lines(_run_plan_script(_plan_env()))
    assert kinds["summary"][0]["mirror_products_with_variant_rows"] == 0 and kinds["excluded"] == [{}], kinds


async def test_the_product_scan_runs_after_the_gate_opens(scoped_db):
    """The gate checks preconditions only; the scan is the first plan query. A scan that fails (here: a
    seeds table without the column it reads) fails after GATE_OPEN, never in place of the gate."""
    await _load_mirror(scoped_db)
    await scoped_db.execute("ALTER TABLE external_product_seeds RENAME COLUMN status TO status_renamed")
    out = _run_plan_script(_plan_env())
    assert out.returncode != 0 and out.stdout.splitlines()[0] == "GATE_OPEN", (out.stdout, out.stderr[-500:])
    assert "MVR_PLAN DONE" not in out.stdout


async def test_the_dry_run_floors_a_sub_second_timeout(scoped_db):
    await _load_mirror(scoped_db)
    kinds = _plan_lines(_run_plan_script(_plan_env(timeout="0.2")))
    assert [p["statement_timeout"] for p in kinds["progress"]] == ["1s"]


async def test_the_dry_run_gate_stays_closed_outside_production():
    out = _run_plan_script({"PIVOTA_ENV": "staging", "DB_STATEMENT_TIMEOUT_SECONDS": "30", "DATABASE_URL": _url()})
    assert out.returncode == 3 and out.stdout.strip() == "GATE_CLOSED PIVOTA_ENV is not production"
    out = _run_plan_script({"PIVOTA_ENV": "production", "DB_STATEMENT_TIMEOUT_SECONDS": "", "DATABASE_URL": _url()})
    assert out.returncode == 3 and out.stdout.startswith("GATE_CLOSED DB_STATEMENT_TIMEOUT_SECONDS unset")
    out = _run_plan_script({"PIVOTA_ENV": "production", "DB_STATEMENT_TIMEOUT_SECONDS": "30s", "DATABASE_URL": _url()})
    assert out.returncode == 3 and out.stdout.strip() == "GATE_CLOSED DB_STATEMENT_TIMEOUT_SECONDS is not a number: 30s"
    assert "Traceback" not in out.stderr
    out = _run_plan_script({"PIVOTA_ENV": "production", "DB_STATEMENT_TIMEOUT_SECONDS": "-5", "DATABASE_URL": _url()})
    assert out.returncode == 3 and out.stdout.strip() == "GATE_CLOSED DB_STATEMENT_TIMEOUT_SECONDS must be positive"
