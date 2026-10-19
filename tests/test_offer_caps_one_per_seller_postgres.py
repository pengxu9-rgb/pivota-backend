"""Both offer caps take ONE OFFER PER SELLER first, on POSTGRES, through the real assembler and route.

THE DEFECT (2026-10-18, after the first cross-merchant M4 apply). Two caps counted offers, not
sellers, and every retailer row carries two offers to the same page: its per-variant offer and its
product-level `<product_key>::canonical` placeholder. On ck_0dc56bdd (O HUI Age Recovery Eye Cream
25 ml: 6 rows on 5 stores, kbeautymakeup the keeper):
  * agent_pdp_view.offers (OFFER_TOP_N = 5) stored kbeautymakeup x2 + nanasbeautyholic x3 -- the
    keeper's twins and the cheapest store's two listings -- so 3 of the 5 stores were missing;
  * offers.resolve's catalog arm ordered by stock then price and cut at `LIMIT :limit` (10) BEFORE
    its host dedupe, so the dearest store's two offers fell off: koolseoul's in-stock US/USD $117
    never reached the dedupe and only 4 hosts did.

The rows below are that product's shape: `ext:` listing keys, `<pk>::v:<id>` and `<pk>::canonical`
SKUs, observed sellers minted by services/seller_identity exactly as ingestion mints them (stylekorean
is a known retailer, keyed on its domain; the others on brand + domain), product_group_members rows
with the keeper primary, and for the resolved-group case the applied M4 proposals whose matcher
verdicts list each listing's per-variant offer. Nothing here hands an offer list to a function: the
assembler reads the rows with its own SQL (build_agent_pdp_view_row) and the route sends its own
statement (_handle_offers_resolve).

Runs in its own scratch schema, alone on the search_path, so it neither reads nor leaves rows in the
shared tables the other gate files use.
"""

from __future__ import annotations

import asyncio
import hashlib
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

_SCHEMA = f"offer_caps_per_seller_{uuid.uuid4().hex[:8]}"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"

CK = "ck_0dc56bdd"
BRAND = "O HUI"
TITLE = "O HUI Age Recovery Eye Cream 25ml"
SLUG = "o-hui-age-recovery-eye-cream-25ml"

# (row, host, url path, price): 6 rows on 5 stores; nanasbeautyholic lists it twice.
LISTINGS = (
    ("kbeauty", "kbeautymakeup.com", f"/products/{SLUG}", 38.00),          # the keeper
    ("nanas", "nanasbeautyholic.com", f"/products/{SLUG}", 36.00),
    ("nanas_1", "nanasbeautyholic.com", f"/products/{SLUG}-1", 40.00),
    ("stylekorean", "www.stylekorean.com", "/shop/o-hui-age-recovery-eye-cream-25ml", 52.00),
    ("jolse", "jolse.com", f"/products/{SLUG}", 61.00),
    ("koolseoul", "koolseoul.com", "/products/ohui-age-recovery-eye-cream-25ml", 117.00),
)
KEEPER = "kbeauty"
STORES = {"kbeautymakeup.com", "nanasbeautyholic.com", "stylekorean.com", "jolse.com", "koolseoul.com"}


def _pk(row: str) -> str:
    return "ext:retailer:" + hashlib.sha256(f"{CK}:{row}".encode()).hexdigest()[:32]


def _variant_sku(row: str) -> str:
    return f"{_pk(row)}::v:4410{len(row)}"


def _placeholder_sku(row: str) -> str:
    return f"{_pk(row)}::canonical"


def _variant_offer(row: str) -> str:
    # The placeholder's id sorts FIRST, so an equal-price tie broken by offer_id (the old dedupe)
    # would keep the placeholder; the per-variant offer must win on what it is, not on its id.
    return f"of_{row}_1variant"


def _placeholder_offer(row: str) -> str:
    return f"of_{row}_0canonical"


def _seller(host: str):
    from services.seller_identity import resolve_seed_seller_identity

    return resolve_seed_seller_identity(brand=BRAND, domain=host)


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
            for f in ("044_external_product_seeds.sql", "045_product_groups.sql",
                      "134_catalog_source_quarantine.sql", "179_identity_resolution_d2.sql"):
                c.exec_driver_sql((_MIGRATIONS / f).read_text())
            # mig 246, as db/schema_guard heals it onto prod (the shared Table does not carry it).
            c.execute(text("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at TIMESTAMPTZ NULL"))
            # Read by the assembler's source-quarantine anti-join; no model or migration in this repo
            # declares either (the gateway writes pdp_identity_listing). Only the columns it reads.
            c.execute(text("CREATE TABLE pdp_identity_listing (product_id text, merchant_id text)"))
            c.execute(text(
                "CREATE TABLE merchant_stores (store_id text PRIMARY KEY, merchant_id text, platform text,"
                " domain text, status text, is_primary boolean, last_sync timestamptz, created_at timestamptz)"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        admin.dispose()


def _insert_product(c, *, pk, ck, merchant, url, title=TITLE, sig=None, source_domain=None):
    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO catalog_merchants (merchant_id, merchant_name, primary_platform, status)"
        " VALUES (:m, :m, 'external_seed', 'observed') ON CONFLICT DO NOTHING"), {"m": merchant})
    c.execute(text(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " content_key, pivota_signature_id, canonical_url, source_domain, catalog_track, truth_tier,"
        " readiness_tier, pdp_scope, pdp_lifecycle_stage, source_system, updated_at)"
        " VALUES (:pk,:m,'external_seed',:pk,:title,:brand,:ck,:sig,:url,:sd,'citation','primary',"
        "         'referral_only','multi_merchant_canonical','published','catalog_enrichment_agent_v1',NOW())"
    ), {"pk": pk, "m": merchant, "title": title, "brand": BRAND, "ck": ck,
        "sig": sig or "sig_" + hashlib.sha256(pk.encode()).hexdigest()[:24], "url": url, "sd": source_domain})


def _insert_offer(c, *, offer_id, sku, pk, merchant, price, url, market="US", currency="USD",
                  availability="in_stock", checked=None):
    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id,"
        " source_variant_id, title, currency, updated_at)"
        " VALUES (:sk,:pk,:m,'external_seed',:pk,:vid,:title,:cur,NOW()) ON CONFLICT DO NOTHING"
    ), {"sk": sku, "pk": pk, "m": merchant, "vid": sku.rsplit(":", 1)[-1], "title": TITLE, "cur": currency})
    c.execute(text(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, catalog_track, truth_tier,"
        " readiness_tier, offer_mode, channel, availability, market, currency, list_price,"
        " merchant_effective_price, offer_type, is_first_party, source_ref, offer_payload, updated_at,"
        " price_checked_at)"
        " VALUES (:oid,:sk,:pk,:m,'external_referral','observed','referral_only','redirect',"
        "         'external_referral',:avail,:market,:cur,:p,:p,'retailer',false,:url,"
        "         cast(:payload AS jsonb), NOW(), :checked)"
    ), {"oid": offer_id, "sk": sku, "pk": pk, "m": merchant, "avail": availability, "market": market,
        "cur": currency, "p": price, "url": url, "payload": json.dumps({"destination_url": url}),
        "checked": checked})


def _seed_catalog(engine, *, koolseoul_availability="in_stock"):
    """ck_0dc56bdd as M4 left it, plus three single-store keys for the twin rule's edges."""
    from sqlalchemy import text

    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    with engine.begin() as c:
        for t in ("catalog_offers", "catalog_skus", "catalog_products", "catalog_merchants",
                  "product_group_members", "identity_resolution_proposals", "external_product_seeds"):
            c.execute(text(f"DELETE FROM {t}"))
        for row, host, path, price in LISTINGS:
            merchant = _seller(host)["merchant_id"]
            url = f"https://{host}{path}"
            _insert_product(c, pk=_pk(row), ck=CK, merchant=merchant, url=url)
            c.execute(text(
                "INSERT INTO product_group_members (product_group_id, merchant_id, platform,"
                " platform_product_id, is_primary) VALUES ('pg_0dc56bdd', :m, 'external_seed', :pk, :primary)"
            ), {"m": merchant, "pk": _pk(row), "primary": row == KEEPER})
            avail = koolseoul_availability if row == "koolseoul" else "in_stock"
            # The twin pair: the variant's own offer and the listing's product-level placeholder,
            # same page, same price -- what the retailer lane writes for a one-size listing.
            _insert_offer(c, offer_id=_variant_offer(row), sku=_variant_sku(row), pk=_pk(row),
                          merchant=merchant, price=price, url=url, availability=avail, checked=fresh)
            _insert_offer(c, offer_id=_placeholder_offer(row), sku=_placeholder_sku(row), pk=_pk(row),
                          merchant=merchant, price=price, url=url, availability=avail, checked=fresh)
            if row != KEEPER:
                # The applied M4 move that joined this listing to the keeper, as identity_resolution
                # writes it: the verdict lists the matched size's PER-VARIANT offer, never a placeholder.
                c.execute(text(
                    "INSERT INTO identity_resolution_proposals (proposal_id, proposal_key, kind, strategy,"
                    " resolver_version, content_key, subject_product_keys, keeper_product_key,"
                    " member_fingerprint, status, evidence)"
                    " VALUES (:id, :id, 'attach_membership', 'cross_merchant_same_size', 'm4', :ck, :subjects,"
                    "         :keeper, 'fp', 'applied', cast(:ev AS jsonb))"
                ), {"id": f"irp_{row}", "ck": CK, "subjects": [_pk(row), _pk(KEEPER)], "keeper": _pk(KEEPER),
                    "ev": json.dumps({
                        "listing_product_key": _pk(row), "from_content_key": f"ck_old_{row}",
                        "proposer": "cross_merchant_same_size",
                        "matcher_verdict": {
                            "strategy": "cross_merchant_same_size", "same_size": True, "rule_version": "m4",
                            "tier": "T0c", "size": {"value": 25, "unit": "ml", "pack": 1},
                            "matched_offers": [{"offer_id": _variant_offer(row), "sku_key": _variant_sku(row)}],
                            "matched_seeds": [],
                        },
                    })})

        # ONE STORE, SEVEN SHADES and the placeholder: the per-seller pass changes nothing, and the
        # placeholder no longer takes one of the five slots from a shade.
        shades_pk, shades_url = "ext:retailer:shades", "https://kbeautymakeup.com/products/o-hui-lip-shades"
        shades_m = _seller("kbeautymakeup.com")["merchant_id"]
        _insert_product(c, pk=shades_pk, ck="ck_shades", merchant=shades_m, url=shades_url, title="O HUI Lip")
        for n in range(7):
            _insert_offer(c, offer_id=f"of_shade_{n}", sku=f"{shades_pk}::v:{n}", pk=shades_pk, merchant=shades_m,
                          price=20 + n, url=shades_url)
        _insert_offer(c, offer_id="of_shade_placeholder", sku=f"{shades_pk}::canonical", pk=shades_pk,
                      merchant=shades_m, price=19, url=shades_url)

        # A PLACEHOLDER ALONE (a mirror row with no per-variant offer): it is the row's offer, and stays.
        mirror_pk, mirror_url = "ext:retailer:mirror", "https://jolse.com/products/o-hui-toner"
        mirror_m = _seller("jolse.com")["merchant_id"]
        _insert_product(c, pk=mirror_pk, ck="ck_mirror", merchant=mirror_m, url=mirror_url, title="O HUI Toner")
        _insert_offer(c, offer_id="of_mirror", sku=f"{mirror_pk}::canonical", pk=mirror_pk, merchant=mirror_m,
                      price=30, url=mirror_url)

        # A TWIN IS SAME MARKET AND CURRENCY: a KR/KRW placeholder beside a US/USD variant offer is
        # another market's price, not a twin, and stays.
        kr_pk, kr_url = "ext:retailer:kr", "https://nanasbeautyholic.com/products/o-hui-cushion"
        kr_m = _seller("nanasbeautyholic.com")["merchant_id"]
        _insert_product(c, pk=kr_pk, ck="ck_kr", merchant=kr_m, url=kr_url, title="O HUI Cushion")
        _insert_offer(c, offer_id="of_kr_usd", sku=f"{kr_pk}::v:1", pk=kr_pk, merchant=kr_m, price=42, url=kr_url)
        _insert_offer(c, offer_id="of_kr_krw", sku=f"{kr_pk}::canonical", pk=kr_pk, merchant=kr_m, price=52000,
                      url=kr_url, market="KR", currency="KRW")
        # ...and a placeholder in another market at the SAME currency (PR, a USD market) is not one.
        _insert_offer(c, offer_id="of_kr_pr", sku=f"{kr_pk}::canonical", pk=kr_pk, merchant=kr_m, price=43,
                      url=kr_url, market="PR")

        # A VARIANT WHOSE KEY MERELY CONTAINS `::canonical` is a variant: only the suffix names the
        # placeholder. Its row's placeholder is then a twin and goes.
        tone_pk, tone_url = "ext:retailer:tone", "https://jolse.com/products/o-hui-tone-up"
        _insert_product(c, pk=tone_pk, ck="ck_tone", merchant=mirror_m, url=tone_url, title="O HUI Tone Up")
        _insert_offer(c, offer_id="of_tone_v", sku=f"{tone_pk}::v::canonical-beige", pk=tone_pk, merchant=mirror_m,
                      price=33, url=tone_url)
        _insert_offer(c, offer_id="of_tone_c", sku=f"{tone_pk}::canonical", pk=tone_pk, merchant=mirror_m,
                      price=31, url=tone_url)

        # THE VARIANT DECIDES STOCK TOO: a sold-out variant beside its in-stock placeholder is sold out.
        so_pk, so_url = "ext:retailer:soldout", "https://koolseoul.com/products/o-hui-soldout"
        so_m = _seller("koolseoul.com")["merchant_id"]
        _insert_product(c, pk=so_pk, ck="ck_soldout", merchant=so_m, url=so_url, title="O HUI Sold Out")
        _insert_offer(c, offer_id="of_so_v", sku=f"{so_pk}::v:1", pk=so_pk, merchant=so_m, price=50, url=so_url,
                      availability="out_of_stock")
        _insert_offer(c, offer_id="of_so_c", sku=f"{so_pk}::canonical", pk=so_pk, merchant=so_m, price=50,
                      url=so_url)

        # ONE MERCHANT ON TWO HOSTS, TWO MERCHANTS ON ONE HOST. The retired `external_seed` bucket holds
        # two stores (hosts read from source_domain; no canonical URL), and a store-brand observed seller
        # shares the first bucket store's host. Five sellers; the first bucket store has two more
        # (cheap) shades that would fill the slots if its stores were one seller.
        bucket = [
            ("bk_a", "external_seed", "beautynetkorea.com", (10, 11, 12)),
            ("bk_b", "external_seed", "yesstyle.com", (40,)),
            ("ob_a", _seller("beautynetkorea.com")["merchant_id"], "beautynetkorea.com", (45,)),
            ("bk_j", _seller("jolse.com")["merchant_id"], "jolse.com", (50,)),
            ("bk_k", _seller("koolseoul.com")["merchant_id"], "koolseoul.com", (55,)),
        ]
        for row, merchant, host, prices in bucket:
            pk = f"ext:retailer:{row}"
            _insert_product(c, pk=pk, ck="ck_bucket", merchant=merchant, url=None, source_domain=host)
            for n, price in enumerate(prices):
                _insert_offer(c, offer_id=f"of_{row}_{n}", sku=f"{pk}::v:{n}", pk=pk, merchant=merchant,
                              price=price, url=f"https://{host}/p/{row}-{n}")

        # THE REPRESENTATIVE IS THE ONE THE BUY PICK WOULD TAKE. Six sellers. `gb` has a cheaper GBP
        # offer in GB beside its in-stock USD one; `unk` has a cheaper offer of unknown stock beside
        # its in-stock one. Their USD in-stock offers must stand for them, not be cut.
        reps = [
            ("gb", "stylekorean.com", ((9, "GB", "GBP", "in_stock"), (20, "US", "USD", "in_stock"))),
            ("unk", "jolse.com", ((18, "US", "USD", "unknown"), (21, "US", "USD", "in_stock"))),
            ("r3", "koolseoul.com", ((25, "US", "USD", "in_stock"),)),
            ("r4", "kbeautymakeup.com", ((26, "US", "USD", "in_stock"),)),
            ("r5", "nanasbeautyholic.com", ((27, "US", "USD", "in_stock"),)),
            ("r6", "beautynetkorea.com", ((28, "US", "USD", "in_stock"),)),
        ]
        for row, host, offers in reps:
            pk, url = f"ext:retailer:rep-{row}", f"https://{host}/p/rep"
            merchant = _seller(host)["merchant_id"]
            _insert_product(c, pk=pk, ck="ck_rep", merchant=merchant, url=url)
            for n, (price, market, currency, avail) in enumerate(offers):
                _insert_offer(c, offer_id=f"of_rep_{row}_{n}", sku=f"{pk}::v:{n}", pk=pk, merchant=merchant,
                              price=price, url=url, market=market, currency=currency, availability=avail)

        # A DESTINATION THE ROUTE CANNOT SERVE never takes its host's slot: `HTTPS://` (the loop keeps
        # only `http://` / `https://` as spelled). And an EMPTY host is a seller of its own.
        for row, host, offers in (
            ("sch1", "jolse.com", ((5, "HTTPS://jolse.com/p/x"), (30, "https://jolse.com/p/x"))),
            ("sch2", "koolseoul.com", ((10, "https://koolseoul.com/p/x"),)),
            ("sch3", "kbeautymakeup.com", ((12, "https://kbeautymakeup.com/p/x"),)),
            ("sch4", "stylekorean.com", ((40, "https://stylekorean.com/p/x"),)),
        ):
            pk = f"ext:retailer:{row}"
            merchant = _seller(host)["merchant_id"]
            _insert_product(c, pk=pk, ck="ck_scheme", merchant=merchant, url=f"https://{host}/p/x")
            for n, (price, url) in enumerate(offers):
                _insert_offer(c, offer_id=f"of_{row}_{n}", sku=f"{pk}::v:{n}", pk=pk, merchant=merchant,
                              price=price, url=url)
        for row, offers in (("at1", ((1, "https://@/a"),)), ("at2", ((2, "https://@/b"),)),
                            ("at3", ((10, "https://koolseoul.com/p/y"), (11, "https://koolseoul.com/p/z")))):
            pk = f"ext:retailer:{row}"
            merchant = _seller("koolseoul.com")["merchant_id"]
            _insert_product(c, pk=pk, ck="ck_empty_host", merchant=merchant, url="https://koolseoul.com/p/y")
            for n, (price, url) in enumerate(offers):
                _insert_offer(c, offer_id=f"of_{row}_{n}", sku=f"{pk}::v:{n}", pk=pk, merchant=merchant,
                              price=price, url=url)

        # ONE ROW, TWO SPELLINGS OF ITS STORE: the variant offer names the mobile host, the placeholder
        # the bare one. Still one merchant's twins, as the assembler reads them: the placeholder goes.
        sd_pk = "ext:retailer:subdomain"
        sd_m = _seller("jolse.com")["merchant_id"]
        _insert_product(c, pk=sd_pk, ck="ck_subdomain", merchant=sd_m, url="https://jolse.com/p/sd")
        _insert_offer(c, offer_id="of_sd_v", sku=f"{sd_pk}::v:1", pk=sd_pk, merchant=sd_m, price=20,
                      url="https://m.jolse.com/p/sd")
        _insert_offer(c, offer_id="of_sd_c", sku=f"{sd_pk}::canonical", pk=sd_pk, merchant=sd_m, price=20,
                      url="https://jolse.com/p/sd")

        # A STATED CURRENCY BEATS NONE, with no market declared too: one store, a $10 offer that states no
        # currency and a $12 one in USD (two variants, so no twin). The USD one represents the store.
        bl_pk, bl_url = "ext:retailer:blank", "https://koolseoul.com/p/blank"
        bl_m = _seller("koolseoul.com")["merchant_id"]
        _insert_product(c, pk=bl_pk, ck="ck_blank", merchant=bl_m, url=bl_url)
        _insert_offer(c, offer_id="of_bl_blank", sku=f"{bl_pk}::v:1", pk=bl_pk, merchant=bl_m, price=10,
                      url=bl_url, currency=None)
        _insert_offer(c, offer_id="of_bl_usd", sku=f"{bl_pk}::v:2", pk=bl_pk, merchant=bl_m, price=12, url=bl_url)

        # ...and across stores: at limit 1 with no market, a store stating USD comes before a cheaper
        # store stating none.
        for row, host, price, currency in (("bx_a", "jolse.com", 9, None), ("bx_b", "stylekorean.com", 14, "USD")):
            pk, url = f"ext:retailer:{row}", f"https://{host}/p/blank"
            merchant = _seller(host)["merchant_id"]
            _insert_product(c, pk=pk, ck="ck_blank_x", merchant=merchant, url=url)
            _insert_offer(c, offer_id=f"of_{row}", sku=f"{pk}::v:1", pk=pk, merchant=merchant, price=price,
                          url=url, currency=currency)

        # ANOTHER RETAILER'S OFFER ATTACHED TO A ROW (scripts/attach_retailer_offer.py, below): the row
        # is a kbeautymakeup listing with its own variant offer and placeholder twin.
        at_pk, at_url = "ext:retailer:attach", "https://kbeautymakeup.com/products/o-hui-first-serum"
        at_m = _seller("kbeautymakeup.com")["merchant_id"]
        _insert_product(c, pk=at_pk, ck="ck_attach", merchant=at_m, url=at_url, title="O HUI First Serum")
        _insert_offer(c, offer_id="of_at_v", sku=f"{at_pk}::v:1", pk=at_pk, merchant=at_m, price=48, url=at_url)
        _insert_offer(c, offer_id="of_at_c", sku=f"{at_pk}::canonical", pk=at_pk, merchant=at_m, price=48,
                      url=at_url)


# ------------------------------------------------------------------ the assembler

def _build(monkeypatch, content_key):
    import databases

    import db.product_enrichment as enrichment
    import services.outcome_aggregation_service as outcomes
    from services import agent_pdp_view_assembler as assembler

    # Overlays only (copy and seller trust); the offers are read by the assembler's own SQL.
    monkeypatch.setattr(enrichment, "get_enrichments_for_products", AsyncMock(return_value={}))
    monkeypatch.setattr(outcomes, "seller_trust_bulk", AsyncMock(return_value={}))
    monkeypatch.setenv(assembler.MARKET_PRICES_FLAG_ENV, "on")

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        try:
            return await assembler.build_agent_pdp_view_row(content_key, refresh_source="test", db=db)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _store_of(offer, hosts_by_merchant):
    return hosts_by_merchant[offer["merchant_id"]]


def _hosts_by_merchant():
    return {_seller(host)["merchant_id"]: host.removeprefix("www.") for _, host, _, _ in LISTINGS}


def test_the_view_stores_every_store_of_a_merged_product(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, CK)
    stores = [_store_of(o, _hosts_by_merchant()) for o in row["offers"]]
    # Was kbeautymakeup x2 + nanasbeautyholic x3.
    assert sorted(stores) == sorted(STORES), stores
    # In the stored order: the keeper leads its stock group, then price ascending; each store's
    # cheapest offer (nanasbeautyholic's $36 listing, not its $40 one).
    assert [(s, o["price"]) for s, o in zip(stores, row["offers"])] == [
        ("kbeautymakeup.com", 38.0), ("nanasbeautyholic.com", 36.0), ("stylekorean.com", 52.0),
        ("jolse.com", 61.0), ("koolseoul.com", 117.0),
    ]
    assert [o["is_primary"] for o in row["offers"]] == [True, False, False, False, False]
    assert all(o["currency"] == "USD" and o["market"] == "US" for o in row["offers"])
    # The aggregates read the offers without their twins (tests/test_agent_pdp_view_seller_count_postgres.py):
    # 6 listings, 5 stores. Were 12 offers.
    assert (row["offer_count"], row["seller_count"], row["price_min"], row["price_max"]) == (6, 5, 36, 117)
    usd = row["market_prices"]["currencies"]["USD"]
    assert usd["offers"] == row["offers"] and (usd["offer_count"], usd["seller_count"]) == (6, 5)


def test_a_sold_out_store_is_still_listed_but_after_every_store_that_can_sell(schema_engine, monkeypatch):
    _seed_catalog(schema_engine, koolseoul_availability="out_of_stock")
    row = _build(monkeypatch, CK)
    stores = [_store_of(o, _hosts_by_merchant()) for o in row["offers"]]
    assert sorted(stores) == sorted(STORES) and stores[-1] == "koolseoul.com"
    assert row["offers"][-1]["availability"] == "out_of_stock"


def test_one_store_keeps_its_five_cheapest_shades_and_the_placeholder_no_longer_takes_a_slot(
    schema_engine, monkeypatch
):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_shades")
    # The $19 product-level placeholder used to sit first in the five; the shades are the prices.
    assert [o["price"] for o in row["offers"]] == [20.0, 21.0, 22.0, 23.0, 24.0]
    # Seven offers, one seller; the headline is the cheapest shade's, not the placeholder's $19.
    assert (row["offer_count"], row["seller_count"], row["price_min"]) == (7, 1, 20)


def test_a_placeholder_with_no_variant_offer_beside_it_is_the_rows_offer(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_mirror")
    assert [(o["price"], o["currency"]) for o in row["offers"]] == [(30.0, "USD")]


def test_a_placeholder_in_another_market_is_not_a_twin(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_kr")
    assert sorted((o["market"], o["currency"], o["price"]) for o in row["offers"]) == [
        ("KR", "KRW", 52000.0), ("PR", "USD", 43.0), ("US", "USD", 42.0)]
    assert set(row["market_prices"]["currencies"]) == {"KRW", "USD"}


AMAZON_URL = "https://www.amazon.com/dp/B0OHUISERUM"


def _attach_amazon(monkeypatch):
    """Amazon's offer onto the ck_attach row through the attach tool's REAL write path (its row
    builder, its live-SKU precondition, its INSERT). Its view refresh is recorded, not run: the
    tests rebuild the row themselves with build_agent_pdp_view_row."""
    import databases

    import scripts.attach_retailer_offer as attach
    import services.catalog_offer_writer_guard as guard
    from services.seller_identity import make_observed_retailer_id

    refreshed = []

    async def record(content_key, **kwargs):
        refreshed.append(content_key)
        return True

    monkeypatch.setattr(attach, "refresh_agent_pdp_view_for_content_key", record)

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        monkeypatch.setattr(attach, "database", db)
        monkeypatch.setattr(guard, "database", db)
        try:
            row = attach.build_retailer_offer_row(
                product_key="ext:retailer:attach", merchant_id=make_observed_retailer_id("amazon.com"),
                merchant_name="Amazon", retailer_url=AMAZON_URL, price=29.99)
            return await attach.attach_retailer_offer(row)
        finally:
            await db.disconnect()

    assert asyncio.run(go()) == "ck_attach" and refreshed == ["ck_attach"]


def test_an_attached_retailers_offer_is_not_the_rows_twin(schema_engine, monkeypatch):
    """The attach tool writes Amazon's offer as `<the row's product_key>::canonical`. It is Amazon's only
    offer, not a twin of the kbeautymakeup row's variant offer: the twin rule is per seller."""
    from services.seller_identity import make_observed_retailer_id

    _seed_catalog(schema_engine)
    _attach_amazon(monkeypatch)
    row = _build(monkeypatch, "ck_attach")
    amazon, kbeauty = make_observed_retailer_id("amazon.com"), _seller("kbeautymakeup.com")["merchant_id"]
    assert sorted((o["merchant_id"], o["price"]) for o in row["offers"]) == sorted(
        [(amazon, 29.99), (kbeauty, 48.0)])


def test_one_merchant_on_two_hosts_is_two_sellers_and_two_merchants_on_one_host_are_two(
    schema_engine, monkeypatch
):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_bucket")
    obs = _seller("beautynetkorea.com")["merchant_id"]
    assert sorted((o["merchant_id"], o["price"]) for o in row["offers"]) == sorted([
        ("external_seed", 10.0), ("external_seed", 40.0), (obs, 45.0),
        (_seller("jolse.com")["merchant_id"], 50.0), (_seller("koolseoul.com")["merchant_id"], 55.0),
    ])


def test_a_seller_is_represented_by_its_in_stock_offer_in_the_rows_currency(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_rep")
    assert row["currency"] == "USD"
    stored = sorted((_store_of(o, {_seller(h)["merchant_id"]: h for h in (
        "stylekorean.com", "jolse.com", "koolseoul.com", "kbeautymakeup.com", "nanasbeautyholic.com",
        "beautynetkorea.com")}), o["price"], o["currency"], o["availability"]) for o in row["offers"])
    assert stored == sorted([
        ("stylekorean.com", 20.0, "USD", "in_stock"), ("jolse.com", 21.0, "USD", "in_stock"),
        ("koolseoul.com", 25.0, "USD", "in_stock"), ("kbeautymakeup.com", 26.0, "USD", "in_stock"),
        ("nanasbeautyholic.com", 27.0, "USD", "in_stock"),
    ])
    from services.offer_buyability import annotate_offer_buyability

    [pick] = [o for o in annotate_offer_buyability(row["offers"], "US") if o["is_buy_pick"]]
    assert (pick["price"], pick["currency"]) == (20.0, "USD")


def test_a_variant_key_that_contains_canonical_is_a_variant(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_tone")
    assert [o["price"] for o in row["offers"]] == [33.0]


def test_a_sold_out_variant_is_not_hidden_by_its_in_stock_placeholder(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    row = _build(monkeypatch, "ck_soldout")
    assert [(o["price"], o["availability"]) for o in row["offers"]] == [(50.0, "out_of_stock")]


# ------------------------------------------------------------------ offers.resolve's catalog arm

def _resolve(monkeypatch, product_id, *, limit=10, group_flag="0", market="US"):
    import databases

    import db.database as dbmod
    import db.merchant_purchasability as mp
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write
    from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

    monkeypatch.setenv("OFFERS_RESOLVE_GROUP_SEED_SIBLINGS", group_flag)
    monkeypatch.setattr(dual_write, "_price_check_column", {"present": False, "checked": None})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(mp, "human_handoff_allowed", AsyncMock(return_value=True))

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
                OffersResolvePayload(product={"product_id": product_id}, limit=limit, market=market, tool="*",
                                     commerce_surface="agent_api"),
                None, _BT(),
            )
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _catalog(res):
    return [o for o in res["offers"] if (o.get("source") or {}).get("type") == "catalog_offer"]


def _host(offer):
    return offer["url"].split("/")[2].removeprefix("www.")


@pytest.mark.parametrize("ident", [CK, _pk("koolseoul"), _pk(KEEPER)])
def test_every_store_reaches_the_buyer_at_limit_10(schema_engine, monkeypatch, ident):
    _seed_catalog(schema_engine)
    res = _resolve(monkeypatch, ident)
    offers = _catalog(res)
    # Was 4 hosts: the LIMIT cut koolseoul's two offers before the dedupe saw them.
    assert sorted(_host(o) for o in offers) == sorted(STORES), [_host(o) for o in offers]
    [kool] = [o for o in offers if _host(o) == "koolseoul.com"]
    assert (kool["price"], kool["currency"], kool["in_stock"]) == (117.0, "USD", True)
    # Each store is represented by its per-variant offer, never the placeholder twin.
    assert sorted(o["source"]["offer_id"] for o in offers) == sorted(
        _variant_offer(r) for r in ("kbeauty", "nanas", "stylekorean", "jolse", "koolseoul"))
    [nanas] = [o for o in offers if _host(o) == "nanasbeautyholic.com"]
    assert nanas["price"] == 36.0, "the store's cheaper listing"


def test_a_small_limit_is_filled_with_distinct_stores_cheapest_first(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    offers = _catalog(_resolve(monkeypatch, CK, limit=3))
    # Was nanasbeautyholic + kbeautymakeup only: the LIMIT kept 36, 36, 38 and the dedupe ate one.
    assert [(_host(o), o["price"]) for o in offers] == [
        ("nanasbeautyholic.com", 36.0), ("kbeautymakeup.com", 38.0), ("stylekorean.com", 52.0)]


def test_a_sold_out_store_fills_only_after_every_store_that_can_sell(schema_engine, monkeypatch):
    _seed_catalog(schema_engine, koolseoul_availability="out_of_stock")
    offers = _catalog(_resolve(monkeypatch, CK, limit=10))
    assert [_host(o) for o in offers][-1] == "koolseoul.com" and offers[-1]["in_stock"] is False
    assert sorted(_host(o) for o in offers) == sorted(STORES)


def test_a_resolved_group_compares_every_stores_variant_offer(schema_engine, monkeypatch):
    """M4's verdicts list per-variant offers only, so a placeholder twin representing a store would
    take that store out of the comparable set. Each store ships its variant offer, and every one of
    them is price_comparable; the best price is the cheapest store's."""
    _seed_catalog(schema_engine)
    import services.external_offer_dual_write as dual_write

    res = _resolve(monkeypatch, _pk(KEEPER), group_flag="1")
    # The column probe is what makes these prices fresh (mig 246 present in this schema).
    assert dual_write._price_check_column["present"] is True
    offers = _catalog(res)
    assert sorted(_host(o) for o in offers) == sorted(STORES)
    assert sorted(o["source"]["offer_id"] for o in offers) == sorted(
        _variant_offer(r) for r in ("kbeauty", "nanas", "stylekorean", "jolse", "koolseoul"))
    # Every MOVED listing's store compares. (The keeper's own offer is in no verdict -- a verdict lists
    # the listing's matched offers -- so it never did; unchanged here.)
    comparable = sorted(o["source"]["offer_id"] for o in offers if o.get("price_comparable") is True)
    assert comparable == sorted(_variant_offer(r) for r in ("nanas", "stylekorean", "jolse", "koolseoul"))
    assert res["best_price_offer_id"] == f"of:catalog_offer:{_variant_offer('nanas')}"


def test_an_attached_retailers_offer_reaches_the_buyer(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    _attach_amazon(monkeypatch)
    offers = _catalog(_resolve(monkeypatch, "ck_attach"))
    assert sorted((_host(o), o["price"]) for o in offers) == [("amazon.com", 29.99), ("kbeautymakeup.com", 48.0)]
    [kbeauty] = [o for o in offers if _host(o) == "kbeautymakeup.com"]
    assert kbeauty["source"]["offer_id"] == "of_at_v", "the row's own variant offer, not its placeholder"


def test_the_route_sells_a_sold_out_variant_as_sold_out(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    [offer] = _catalog(_resolve(monkeypatch, "ck_soldout"))
    assert (offer["source"]["offer_id"], offer["in_stock"]) == ("of_so_v", False)


def test_an_unservable_destination_never_takes_its_hosts_slot(schema_engine, monkeypatch):
    """jolse's `HTTPS://` $5 offer cannot be served; were it jolse's best, jolse's servable $30 offer
    would rank behind stylekorean's $40 and the LIMIT would cut it."""
    _seed_catalog(schema_engine)
    offers = _catalog(_resolve(monkeypatch, "ck_scheme", limit=3))
    assert sorted((_host(o), o["price"]) for o in offers) == [
        ("jolse.com", 30.0), ("kbeautymakeup.com", 12.0), ("koolseoul.com", 10.0)]


def test_offers_with_no_host_are_not_one_seller(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    offers = _catalog(_resolve(monkeypatch, "ck_empty_host", limit=2))
    assert sorted(o["source"]["offer_id"] for o in offers) == ["of_at1_0", "of_at2_0"]


def test_a_row_with_no_currency_is_withheld_from_a_buyer_in_a_declared_market(schema_engine, monkeypatch):
    from sqlalchemy import text

    _seed_catalog(schema_engine)
    with schema_engine.begin() as c:
        c.execute(text("UPDATE catalog_offers SET currency = NULL WHERE offer_id = 'of_sch2_0'"))
    res = _resolve(monkeypatch, "ck_scheme", limit=4)
    assert "of_sch2_0" not in [o["source"]["offer_id"] for o in _catalog(res)]
    assert res["metadata"]["buyer_currency_filtered"] == {"none": 1}


def test_a_rows_twins_on_two_spellings_of_its_host_are_still_twins(schema_engine, monkeypatch):
    """The view and the route agree: keyed by the merchant, `m.jolse.com` and `jolse.com` on one row
    are one store's variant offer and its placeholder."""
    _seed_catalog(schema_engine)
    assert [o["price"] for o in _build(monkeypatch, "ck_subdomain")["offers"]] == [20.0]
    assert [o["source"]["offer_id"] for o in _catalog(_resolve(monkeypatch, "ck_subdomain"))] == ["of_sd_v"]


@pytest.mark.parametrize("market", [None, "US"])
def test_an_offer_that_states_no_currency_never_stands_for_a_store_that_states_one(
    schema_engine, monkeypatch, market
):
    _seed_catalog(schema_engine)
    [offer] = _catalog(_resolve(monkeypatch, "ck_blank", market=market))
    assert (offer["source"]["offer_id"], offer["currency"]) == ("of_bl_usd", "USD")


def test_a_store_that_states_its_currency_comes_before_one_that_states_none(schema_engine, monkeypatch):
    _seed_catalog(schema_engine)
    [offer] = _catalog(_resolve(monkeypatch, "ck_blank_x", limit=1, market=None))
    assert offer["source"]["offer_id"] == "of_bx_b"
