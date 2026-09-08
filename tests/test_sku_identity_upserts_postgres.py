"""Both catalog_skus writers, EXECUTED against a real Postgres.

WHY THIS FILE EXISTS. `catalog_skus` carries TWO unique constraints — the PK
`sku_key` and `idx_catalog_skus_source_identity_v2 (merchant_id, platform,
product_key, source_variant_id)` — and Postgres INFERS one arbiter from an
`ON CONFLICT` clause; it never falls through to the other. Which of the two a
statement names is therefore semantics, and it is semantics NO SQLite test and no
string assertion can see:

  - `services/catalog_enrichment_agent/apply._SKU_UPSERT_SQL` named the PK, so a
    re-ingest of a variant that already exists under `catalog_variant_promoter`'s
    spelling of the SAME identity (`<pk>::v::<vid>` vs ingestion's `<pk>::v:<vid>`)
    raised 23505 on the index it did not name. The row was logged-and-skipped and
    its offers were written against a key that does not exist.

  - `services/catalog_variant_promoter.UPSERT_SKU_SQL` named
    `(merchant_id, platform, source_variant_id)` — the 3-column index migration
    123 DROPPED. Postgres rejects an ON CONFLICT clause matching no unique
    constraint at PARSE time (42P10), so `promote_variants_all` has been
    unexecutable since that migration, and the SQLite suite's
    `assert "ON CONFLICT (...)" in sql` stayed green through the whole outage.

So every test here executes the real statement. The SQLite suites keep the
string assertions; this file is the one that can tell a live index from a dead
one, and a correct arbiter from a plausible-looking wrong one.

FIXTURE DISCIPLINE. The dialect gate runs every `tests/test_*_postgres.py`
against ONE shared database. This file therefore builds its tables from the `db/`
models (`tests.model_schema.ensure_model_tables`), NEVER drops or re-creates a
shared one, and deletes only its own rows on teardown. `ensure_model_tables`'
column-patch step is SQLite-only, so the columns this file depends on are patched
here with `ADD COLUMN IF NOT EXISTS` in case an alphabetically-earlier file left
a narrower table behind.
"""

import json
import os
import re
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "m_sku_identity_gate"
OTHER_MERCHANT = "m_sku_identity_other"
PLATFORM = "external_seed"
PK = "ext:sku-identity-gate::0badc0de"
SPID = "sku-identity-gate"
DEST = "https://brand.example/products/sku-identity-gate"
VID = "51234567890123"
VID2 = "51234567890999"
GROUP_ID = "pg_sku_identity_gate"
GROUP_ID_LONG = "pg_sku_identity_gate_long"

#: The widest key `ingestion.derive_product_key` can mint: 'ext:' + canonical[:200]
#: + '::' + 8 hex = 214 chars, leaving 41 for the promoter's '::v::' + a variant id.
#: catalog_skus.sku_key is varchar(255), so a merchant variant id of any real length
#: overruns it — and an over-long bind is a 22001, NOT a unique violation.
LONG_PK = "ext:" + ("sku-identity-long-" + "z" * 200)[:200] + "::0badc0de"
LONG_VID = "9" * 140

#: What the 2026-09-08 variant-identity backfill stamps on a row it adopts. These
#: two keys are the ones a replacing `sku_payload = EXCLUDED.sku_payload` erases.
BACKFILL_SOURCE_SYSTEM = "variant_identity_backfill_v1"


def _promoter_key(vid: str = VID) -> str:
    from services.catalog_variant_promoter import _derive_sku_key

    return _derive_sku_key(PK, vid)


def _ingestion_key(vid: str = VID) -> str:
    from services.catalog_enrichment_agent.ingestion import derive_variant_sku_key

    return derive_variant_sku_key(PK, vid)


# --- schema -----------------------------------------------------------------

#: `product_group_members` has no db/ model object, so it cannot come from
#: create_all. Created defensively and patched column by column — another gate
#: file may have left a narrower shape behind, and CREATE TABLE IF NOT EXISTS
#: silently inherits whatever is already there.
_PGM_DDL = """
    CREATE TABLE IF NOT EXISTS product_group_members (
      product_group_id text, merchant_id text, platform text,
      platform_product_id text, is_primary boolean)
"""
_PGM_COLUMNS = (
    ("product_group_id", "text"), ("merchant_id", "text"), ("platform", "text"),
    ("platform_product_id", "text"), ("is_primary", "boolean"),
)

#: The promoter's group query LEFT JOINs this table; it must exist even though
#: this file writes no seed rows.
_SEEDS_DDL = """
    CREATE TABLE IF NOT EXISTS external_product_seeds (
      external_product_id text, attached_product_key text, status text, seed_data jsonb)
"""
_SEEDS_COLUMNS = (
    ("external_product_id", "text"), ("attached_product_key", "text"),
    ("status", "text"), ("seed_data", "jsonb"),
)

#: Columns this file reads or writes. `ensure_model_tables` patches only on
#: SQLite (a Postgres test DB is normally a real migrated schema), so on the
#: shared gate database a table another file created narrow is never reconciled.
_PATCH_COLUMNS = {
    "catalog_skus": (
        ("source_domain", "text"), ("barcode", "varchar(128)"), ("image_url", "text"),
        ("sku_payload", "jsonb"), ("ingredient_ids", "jsonb"),
        ("visible_attributes", "jsonb"), ("visible_option_labels", "jsonb"),
        ("currency", "varchar(16)"), ("sku", "varchar(128)"),
        ("suppressed_at", "timestamptz"), ("suppression_reason", "text"),
    ),
    "catalog_offers": (
        ("source_ref", "varchar(255)"), ("source_system", "varchar(64)"),
        ("offer_payload", "jsonb"), ("source_domain", "text"),
    ),
    "catalog_products": (("source_domain", "text"), ("product_payload", "jsonb")),
}

#: Asserted rather than assumed — every adoption test below would still pass by
#: inserting a fresh row if this index were missing, and the conflict path would
#: go entirely untested. (Test `..._actually_exists` is the guard.)
_IDENTITY_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2
    ON catalog_skus (merchant_id, platform, product_key, source_variant_id)
"""


async def _ddl(database):
    from db.catalog import (
        catalog_offers,
        catalog_products,
        catalog_skus,
        writer_audit_log,
    )
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables(
        [catalog_products, catalog_skus, catalog_offers, writer_audit_log]
    )
    for table, columns in _PATCH_COLUMNS.items():
        for name, coltype in columns:
            await database.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {coltype}"
            )
    await database.execute(_PGM_DDL)
    for name, coltype in _PGM_COLUMNS:
        await database.execute(
            f"ALTER TABLE product_group_members ADD COLUMN IF NOT EXISTS {name} {coltype}"
        )
    await database.execute(_SEEDS_DDL)
    for name, coltype in _SEEDS_COLUMNS:
        await database.execute(
            f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {coltype}"
        )
    await database.execute(_IDENTITY_INDEX)


async def _clear(database):
    """Remove this fixture's ROWS. Never its tables — the gate shares one database
    and a dropped table poisons whichever file collects next."""
    for pk in (PK, LONG_PK):
        await database.execute("DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": pk})
        await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": pk})
        await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": pk})
    await database.execute(
        "DELETE FROM product_group_members WHERE product_group_id = ANY(:g)",
        {"g": [GROUP_ID, GROUP_ID_LONG]},
    )
    await database.execute(
        "DELETE FROM writer_audit_log WHERE writer_name LIKE :w",
        {"w": "catalog_enrichment_agent%"},
    )


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)
    from db.database import database

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await _ddl(database)
    await _clear(database)
    try:
        yield database
    finally:
        await _clear(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


# --- row builders ------------------------------------------------------------


def _full_row(single_sql: str, **overrides):
    """A row carrying EVERY key the statement binds (None-defaulted), so the bulk
    path's pre-bind validation sees the same completeness a real plan row has."""
    from services.catalog_enrichment_agent.bulk_writer import split_upsert_sql

    _, values_tuple, _ = split_upsert_sql(single_sql)
    row = {k: None for k in re.findall(r":(\w+)", values_tuple)}
    row.update(overrides)
    return row


def _planned_sku(*, vid=VID, merchant=MERCHANT, **overrides):
    import services.catalog_enrichment_agent.apply as apply_mod

    row = _full_row(
        apply_mod._SKU_UPSERT_SQL,
        sku_key=_ingestion_key(vid),
        product_key=PK,
        merchant_id=merchant,
        platform=PLATFORM,
        source_product_id=SPID,
        source_variant_id=vid,
        source_domain="brand.example",
        barcode="BC-FRESH",
        title="Ruby (re-ingest)",
        currency="USD",
        image_url="https://img.example/ruby-fresh.jpg",
        visible_attributes=json.dumps({"shade": "Ruby"}),
        visible_option_labels=json.dumps(["shade_ruby"]),
        ingredient_ids=json.dumps([]),
        sku_payload=json.dumps({"agent_version": "ingest_v_test", "variant_id": vid}),
        readiness_tier="referral_only",
    )
    row.update(overrides)
    return row


def _planned_offer(*, sku_key, price=19.0, dest=DEST):
    import services.catalog_enrichment_agent.apply as apply_mod
    from services.catalog_enrichment_agent.ingestion import derive_offer_id

    return _full_row(
        apply_mod._OFFER_UPSERT_SQL,
        offer_id=derive_offer_id(PK, sku_key, dest),
        sku_key=sku_key,
        product_key=PK,
        merchant_id=MERCHANT,
        catalog_track="external_referral",
        truth_tier="primary",
        readiness_tier="referral_only",
        offer_mode="external_referral",
        channel="default",
        availability="in_stock",
        inventory_quantity=999,
        currency="USD",
        list_price=price,
        merchant_effective_price=price,
        estimated_best_price=price,
        price_confidence=0.7,
        source_system="ingest_v_test",
        # Ingestion stores the DESTINATION here (`_build_offer_inserts`), which is
        # the third term of derive_offer_id — the whole basis of the claim that an
        # adopted key lands on the backfill's own offer_id.
        source_ref=dest,
        source_domain="brand.example",
        offer_payload=json.dumps({"destination_url": dest}),
    )


async def _seed_product(database, *, merchant=MERCHANT, payload=None, pk=PK, spid=SPID):
    await database.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform,
             source_product_id, source_domain, title, product_payload)
           VALUES (:pk,:m,:p,:spid,'brand.example','Gate Lipstick',
                   CAST(:pl AS jsonb))""",
        {"pk": pk, "m": merchant, "p": PLATFORM, "spid": spid,
         "pl": json.dumps(payload or {})},
    )


async def _seed_sku(
    database, *, sku_key, vid=VID, merchant=MERCHANT, payload, title="Ruby",
    pk=PK, suppressed=False, readiness_tier="commerce_ready",
):
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload,
             readiness_tier, suppressed_at, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:v,:t,CAST(:pl AS jsonb),
                   :rt,:sup,NOW())""",
        {"sk": sku_key, "pk": pk, "m": merchant, "p": PLATFORM, "spid": SPID,
         "v": vid, "t": title, "pl": json.dumps(payload), "rt": readiness_tier,
         "sup": datetime.now(timezone.utc) if suppressed else None},
    )


async def _seed_backfill_offer(database, *, sku_key, price=24.0, dest=DEST):
    """The offer the 2026-09-08 backfill hung off the ADOPTED sku_key, with its
    offer_id derived from (product_key, adopted key, destination)."""
    from services.catalog_enrichment_agent.ingestion import derive_offer_id

    offer_id = derive_offer_id(PK, sku_key, dest)
    await database.execute(
        """INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,
             catalog_track, truth_tier, readiness_tier, offer_mode, channel,
             availability, currency, list_price, source_system, source_ref,
             offer_payload, created_at, updated_at)
           VALUES (:oid,:sk,:pk,:m,'external_referral','primary','referral_only',
             'external_referral','default','in_stock','USD',:price,:ss,:ref,
             CAST(:pl AS jsonb),NOW(),NOW())""",
        {"oid": offer_id, "sk": sku_key, "pk": PK, "m": MERCHANT, "price": price,
         "ss": BACKFILL_SOURCE_SYSTEM, "ref": dest,
         "pl": json.dumps({"destination_url": dest})},
    )
    return offer_id


def _backfill_payload(vid=VID):
    return {
        "variant_id": vid,
        "variant_id_provenance": "merchant_issued",
        "source_system": BACKFILL_SOURCE_SYSTEM,
        "agent_version": "promoter_v1",
    }


async def _apply(plan, *, batch):
    import services.catalog_enrichment_agent.apply as apply_mod
    from db.database import database

    return await apply_mod.apply_ingest_plan(
        plan, batch_label="sku-identity-gate", db=database, batch=batch
    )


def _plan(skus, offers):
    return {"merchants": [], "pdps": [], "skus": skus, "offers": offers,
            "seeds": [], "incis": []}


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


# --- preconditions -----------------------------------------------------------


async def test_the_identity_index_the_conflict_target_needs_actually_exists(db):
    """Both fixes rest on catalog_skus carrying TWO unique constraints. Without
    the identity index every adoption test would pass by inserting a fresh row
    and the conflict path would never run."""
    assert await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE tablename='catalog_skus' "
        "AND indexname='idx_catalog_skus_source_identity_v2'"
    ) == 1


async def test_the_two_writers_really_do_spell_the_same_identity_differently(db):
    """The premise. If the two key derivations ever converge, the adoption helper
    becomes dead code and these tests would silently stop testing anything."""
    assert _promoter_key() != _ingestion_key()
    assert _promoter_key() == PK + "::v::" + VID
    assert _ingestion_key() == PK + "::v:" + VID


# --- (a) the per-row ingest path --------------------------------------------


async def test_a_reingest_adopts_the_promoter_row_and_updates_its_backfill_offer(db):
    """THE LIVE BUG, executed. The identity already exists under the promoter's
    `::v::` spelling and carries a live offer the backfill hung off it. Before
    this fix the ingest inserted a rival `::v:` row, the identity index raised
    23505, the SKU was skipped, and its offer was written against a key that does
    not exist."""
    promoter_key = _promoter_key()
    await _seed_product(db)
    await _seed_sku(db, sku_key=promoter_key, payload=_backfill_payload())
    backfill_offer_id = await _seed_backfill_offer(db, sku_key=promoter_key)

    planned = _planned_sku()
    counts = await _apply(
        _plan([planned], [_planned_offer(sku_key=planned["sku_key"])]), batch=False
    )

    assert counts["skus_adopted_existing_identity"] == 1
    assert counts["offers_rekeyed_to_adopted_sku"] == 1
    assert counts["skus_identity_conflict"] == 0
    assert counts["skus"] == 1
    assert counts["offers"] == 1

    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK}
    )
    assert len(rows) == 1, "a rival spelling of the same variant was inserted"
    sku = dict(rows[0])
    assert sku["sku_key"] == promoter_key, "the stored primary key was renamed"

    # descriptive columns REFRESHED by the DO UPDATE
    assert sku["title"] == "Ruby (re-ingest)"
    assert sku["image_url"] == "https://img.example/ruby-fresh.jpg"
    assert sku["barcode"] == "BC-FRESH"
    assert sku["source_domain"] == "brand.example"

    # payload MERGED: the backfill's stamps survive, the fresh keys land
    payload = _jsonb(sku["sku_payload"])
    assert payload["variant_id_provenance"] == "merchant_issued"
    assert payload["source_system"] == BACKFILL_SOURCE_SYSTEM
    assert payload["agent_version"] == "ingest_v_test"

    # THE OFFER LANDED ON THE BACKFILL'S OWN ID — updated, not doubled.
    offers = await db.fetch_all(
        "SELECT * FROM catalog_offers WHERE product_key = :pk", {"pk": PK}
    )
    assert len(offers) == 1
    offer = dict(offers[0])
    assert offer["offer_id"] == backfill_offer_id
    assert offer["sku_key"] == promoter_key
    assert float(offer["list_price"]) == 19.0


# --- (b) the bulk path -------------------------------------------------------


async def test_the_bulk_path_adopts_the_same_identity(db):
    """`batch=True` runs the same SQL through `bulk_upsert`. The adoption helper
    is called from both executors, so this must land exactly where (a) did."""
    promoter_key = _promoter_key()
    await _seed_product(db)
    await _seed_sku(db, sku_key=promoter_key, payload=_backfill_payload())
    backfill_offer_id = await _seed_backfill_offer(db, sku_key=promoter_key)

    planned = _planned_sku()
    counts = await _apply(
        _plan([planned], [_planned_offer(sku_key=planned["sku_key"])]), batch=True
    )

    assert counts["skus_adopted_existing_identity"] == 1
    assert counts["skus_identity_conflict"] == 0
    assert counts["skus"] == 1 and counts["offers"] == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk", {"pk": PK}) == 1
    assert await db.fetch_val(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK}
    ) == promoter_key
    offer_ids = [dict(r)["offer_id"] for r in await db.fetch_all(
        "SELECT offer_id FROM catalog_offers WHERE product_key = :pk", {"pk": PK})]
    assert offer_ids == [backfill_offer_id]


# --- (c) the canonical SKU ---------------------------------------------------


async def test_the_canonical_sku_is_untouched_by_adoption(db):
    """`<pk>::canonical` carries `source_variant_id = product_key` — a storage
    token no variant row can collide with. It must upsert onto itself, keep its
    own key, and not be dragged onto a variant row by the new arbiter."""
    from services.catalog_enrichment_agent.ingestion import derive_sku_key

    canonical_key = derive_sku_key(PK)
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=canonical_key, vid=PK, payload={"agent_version": "old"},
        title="Gate Lipstick",
    )
    await _seed_sku(db, sku_key=_promoter_key(), payload=_backfill_payload())

    planned = _planned_sku(
        vid=PK, sku_key=canonical_key, title="Gate Lipstick (fresh)",
        source_variant_id=PK,
    )
    counts = await _apply(_plan([planned], []), batch=False)

    assert counts["skus_adopted_existing_identity"] == 0
    assert counts["skus_identity_conflict"] == 0
    assert counts["skus"] == 1
    keys = sorted(dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK}))
    assert keys == sorted([canonical_key, _promoter_key()])
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": canonical_key}
    ) == "Gate Lipstick (fresh)"


# --- (e) the residual dual-unique trap ---------------------------------------


async def test_catalog_skus_title_is_not_null_here(db):
    """Precondition for the next test, which needs a NON-unique failure. On a
    table an earlier gate file created narrow this column can be nullable, and
    the test would then pass while asserting nothing."""
    nullable = await db.fetch_val(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='catalog_skus' AND column_name='title'"
    )
    assert nullable == "NO"


async def _seed_case_c(db):
    """Case (c): TWO existing rows, one holding the plan's KEY under a foreign
    tuple, the other holding the plan's IDENTITY under a foreign key. No move
    satisfies both unique constraints — adopting the identity holder leaves the key
    holder still claiming a key this product derives, healing the key holder's
    tuple collides with the identity holder."""
    await _seed_product(db)
    # holds the plan's sku_key, under another merchant's tuple
    await _seed_sku(
        db, sku_key=_ingestion_key(), merchant=OTHER_MERCHANT,
        payload={"agent_version": "key-holder"},
    )
    # holds the plan's identity tuple, under the promoter's key
    await _seed_sku(
        db, sku_key=_promoter_key(), payload=_backfill_payload(), title="Ruby (held)",
    )


async def test_the_residual_conflict_is_counted_and_the_batch_continues(db):
    """The trap NEITHER remedy closes. The row is counted, logged with both rows,
    and skipped — and the rows behind it in the same batch still land.

    With `ON CONFLICT (sku_key)` this row would SILENTLY succeed, updating a row
    whose merchant_id disagrees with the plan's."""
    await _seed_case_c(db)

    trapped = _planned_sku()                      # key held by one row, identity by another
    clean = _planned_sku(vid=VID2)                # must still land
    counts = await _apply(
        _plan([trapped, clean], [_planned_offer(sku_key=clean["sku_key"])]),
        batch=False,
    )

    assert counts["skus_identity_conflict"] == 1
    assert counts["skus"] == 1
    assert counts["skus_identity_healed"] == 0, "healing here would collide"
    assert counts["skus_adopted_existing_identity"] == 0
    # the batch CONTINUED: the offer stage ran after the refusal, inside the same
    # transaction a 23505 would otherwise have aborted (25P02).
    assert counts["offers"] == 1
    landed = await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE sku_key = :k AND merchant_id = :m",
        {"k": _ingestion_key(vid=VID2), "m": MERCHANT})
    assert landed == 1
    assert await db.fetch_val(
        "SELECT merchant_id FROM catalog_skus WHERE sku_key = :k",
        {"k": _ingestion_key()}) == OTHER_MERCHANT, "the key holder was overwritten"
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k",
        {"k": _promoter_key()}) == "Ruby (held)", "the identity holder was overwritten"


async def test_a_refused_sku_leaves_no_offer_behind_on_the_per_row_path(db):
    """catalog_offers has NO foreign key to catalog_skus, so an offer written for a
    SKU the write refused is not a pending offer — it is a fake one, indexed and
    servable, pointing at a sku_key that does not exist. Pre-fix the conflict was
    counted and the offers went in anyway."""
    await _seed_case_c(db)

    trapped = _planned_sku()
    counts = await _apply(
        _plan([trapped], [_planned_offer(sku_key=trapped["sku_key"])]), batch=False
    )

    assert counts["skus_identity_conflict"] == 1
    assert counts["offers"] == 0
    assert counts["offers_dropped_for_refused_sku"] == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0


async def test_a_refused_sku_leaves_no_offer_behind_on_the_bulk_path(db):
    """Same rule through `bulk_upsert`."""
    await _seed_case_c(db)

    trapped = _planned_sku()
    counts = await _apply(
        _plan([trapped], [_planned_offer(sku_key=trapped["sku_key"])]), batch=True
    )

    assert counts["skus_identity_conflict"] == 1
    assert counts["offers"] == 0
    assert counts["offers_dropped_for_refused_sku"] == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0


async def test_an_offer_is_dropped_when_the_sku_write_ITSELF_fails_per_row(db):
    """The refusal the pre-resolve cannot foresee: the SKU row is fine by both
    unique constraints and fails at write time anyway (here a NOT NULL violation).
    The per-row path collects the refused keys from the loop; without that filter
    the offer lands against a sku_key nothing wrote."""
    await _seed_product(db)

    broken = _planned_sku(title=None)
    counts = await _apply(
        _plan([broken], [_planned_offer(sku_key=broken["sku_key"])]), batch=False
    )

    assert counts["skus"] == 0
    assert counts["offers"] == 0
    assert counts["offers_dropped_for_refused_sku"] == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}) == 0


async def test_an_offer_is_dropped_when_the_sku_write_ITSELF_fails_bulk(db):
    """Same, through `bulk_upsert`'s THIRD return (`skipped_rows`) — the same
    channel the PDP stage already feeds into `_filter_children_of_skipped`."""
    await _seed_product(db)

    broken = _planned_sku(title=None)
    clean = _planned_sku(vid=VID2)
    counts = await _apply(
        _plan(
            [broken, clean],
            [_planned_offer(sku_key=broken["sku_key"]),
             _planned_offer(sku_key=clean["sku_key"], dest=DEST + "?v=2")],
        ),
        batch=True,
    )

    assert counts["skus"] == 1
    assert counts["offers"] == 1
    assert counts["offers_dropped_for_refused_sku"] == 1
    keys = [dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_offers WHERE product_key = :pk", {"pk": PK})]
    assert keys == [clean["sku_key"]]


# --- (g) the drifted stored tuple -------------------------------------------


async def test_a_drifted_source_variant_id_is_healed_not_refused_forever(db):
    """THE REGRESSION THIS PR INTRODUCED, executed. The stored row is the one
    INGESTION's own key names, but its identity tuple has drifted — a legacy
    `source_variant_id = 'default'` from before variant ids were captured. Pre-PR
    `ON CONFLICT (sku_key) DO UPDATE` refreshed it whatever its tuple said. With
    the identity arbiter and no healing the INSERT hits the PK, raises 23505, and
    the row is counted-and-skipped on EVERY run for ever, while its offers are
    written onto the stale row.

    The plan's tuple is the truth (`_prepare_seller_of_record` pins it to the
    catalog_products row that exists), so the stored row's tuple is re-pointed at
    it IN PLACE — key kept, so the live offers keyed on it stay attached."""
    key = _ingestion_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=key, vid="default", payload={"agent_version": "legacy"},
        title="Ruby (stale)",
    )
    offer_id = await _seed_backfill_offer(db, sku_key=key)

    counts = await _apply(
        _plan([_planned_sku()], [_planned_offer(sku_key=key)]), batch=False
    )

    assert counts["skus_identity_healed"] == 1
    assert counts["skus"] == 1
    assert counts["skus_identity_conflict"] == 0
    assert counts["offers_dropped_for_refused_sku"] == 0

    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert len(rows) == 1, "a rival row was inserted beside the drifted one"
    sku = dict(rows[0])
    assert sku["sku_key"] == key, "the healed row was re-keyed"
    assert sku["source_variant_id"] == VID, "the identity tuple was not healed"
    assert sku["title"] == "Ruby (re-ingest)", "the row was not refreshed"
    # the offer that was already hanging off that key is still attached to it
    assert await db.fetch_val(
        "SELECT sku_key FROM catalog_offers WHERE offer_id = :o", {"o": offer_id}
    ) == key


async def test_a_drifted_merchant_id_is_healed_too(db):
    """The other drift the review named: `catalog_skus.merchant_id` no longer
    agrees with its product's (a W2 re-resolution, a claimed-attach). The sku_key
    is derived from the product_key, so a row holding it under a foreign merchant
    is drift by construction — and the plan's merchant comes off the
    catalog_products row itself."""
    key = _ingestion_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=key, merchant=OTHER_MERCHANT, payload={"agent_version": "drift"},
        title="Ruby (stale)",
    )

    counts = await _apply(_plan([_planned_sku()], []), batch=False)

    assert counts["skus_identity_healed"] == 1
    assert counts["skus"] == 1
    assert counts["skus_identity_conflict"] == 0
    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert len(rows) == 1
    assert dict(rows[0])["merchant_id"] == MERCHANT
    assert dict(rows[0])["title"] == "Ruby (re-ingest)"


async def test_the_bulk_path_heals_the_same_drift(db):
    """ONE helper, both executors."""
    key = _ingestion_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=key, vid="default", payload={"agent_version": "legacy"},
        title="Ruby (stale)",
    )

    counts = await _apply(
        _plan([_planned_sku()], [_planned_offer(sku_key=key)]), batch=True
    )

    assert counts["skus_identity_healed"] == 1
    assert counts["skus"] == 1 and counts["offers"] == 1
    sku = dict((await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK}))[0])
    assert sku["sku_key"] == key and sku["source_variant_id"] == VID
    assert sku["title"] == "Ruby (re-ingest)"


# --- (h) suppressed rows -----------------------------------------------------


async def test_a_suppressed_row_holding_the_identity_is_never_adopted(db):
    """A suppressed row is one a withdrawal took OUT of supply. Adopting its key
    would resurrect it under a fresh title and hang live offers off it — and it
    could not have worked anyway: both unique constraints cover suppressed rows, so
    the INSERT is refused by the identity index regardless.

    So the identity lookup excludes suppressed rows (`AND suppressed_at IS NULL`),
    the planned row is refused and counted separately from a real identity
    conflict, and its offers are dropped rather than left pointing at a withdrawn
    SKU."""
    promoter_key = _promoter_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=promoter_key, payload=_backfill_payload(),
        title="Ruby (withdrawn)", suppressed=True,
    )

    planned = _planned_sku()
    counts = await _apply(
        _plan([planned], [_planned_offer(sku_key=planned["sku_key"])]), batch=False
    )

    assert counts["skus_skipped_suppressed_identity"] == 1
    assert counts["skus_adopted_existing_identity"] == 0, "a suppressed row was adopted"
    assert counts["skus_identity_conflict"] == 0, "misreported as an identity conflict"
    assert counts["skus"] == 0
    assert counts["offers"] == 0
    assert counts["offers_dropped_for_refused_sku"] == 1

    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert len(rows) == 1, "a rival row was inserted beside the suppressed one"
    sku = dict(rows[0])
    assert sku["sku_key"] == promoter_key
    assert sku["title"] == "Ruby (withdrawn)", "the suppressed row was refreshed"
    assert sku["suppressed_at"] is not None, "the suppressed row was resurrected"
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_offers WHERE product_key = :pk", {"pk": PK}
    ) == 0, "an offer was re-keyed onto a withdrawn SKU"


async def test_the_bulk_path_refuses_a_suppressed_identity_too(db):
    promoter_key = _promoter_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=promoter_key, payload=_backfill_payload(),
        title="Ruby (withdrawn)", suppressed=True,
    )

    planned = _planned_sku()
    counts = await _apply(
        _plan([planned], [_planned_offer(sku_key=planned["sku_key"])]), batch=True
    )

    assert counts["skus_skipped_suppressed_identity"] == 1
    assert counts["skus"] == 0 and counts["offers"] == 0
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": promoter_key}
    ) == "Ruby (withdrawn)"


# --- (i) readiness_tier ------------------------------------------------------


async def test_adoption_never_downgrades_the_readiness_tier(db):
    """`readiness_tier` is INSERT-only. This lane's plan rows carry
    'referral_only'; the row it now lands on may be a promoter/backfill row written
    'commerce_ready'. A `readiness_tier = EXCLUDED.readiness_tier` in the DO UPDATE
    downgrades a purchasable SKU on every content re-sync — a tier is promoted by
    the lane that can prove the checkout, never by a description refresh."""
    promoter_key = _promoter_key()
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=promoter_key, payload=_backfill_payload(),
        readiness_tier="commerce_ready",
    )

    planned = _planned_sku(readiness_tier="referral_only")
    counts = await _apply(_plan([planned], []), batch=False)

    assert counts["skus"] == 1
    assert counts["skus_adopted_existing_identity"] == 1
    assert await db.fetch_val(
        "SELECT readiness_tier FROM catalog_skus WHERE sku_key = :k",
        {"k": promoter_key}) == "commerce_ready"
    # ...and the descriptive columns still refresh, so this is not a dead DO UPDATE
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k",
        {"k": promoter_key}) == "Ruby (re-ingest)"


# --- (j) two planned rows, one identity --------------------------------------


async def test_two_planned_rows_with_one_identity_are_deduped_and_counted_once(db):
    """`counts["skus"]` is what an operator reads to decide the lane is healthy. Two
    planned rows carrying the SAME identity tuple are one SKU: through the identity
    arbiter the second silently UPDATEs the first, so the count reported two writes
    where one row exists. The duplicate is dropped and its offers follow the
    survivor's key (same identity == same row, so they are not orphans)."""
    await _seed_product(db)

    first = _planned_sku()                                   # `<pk>::v:<vid>`
    dup = _planned_sku(sku_key=_promoter_key(), title="Ruby (dup)")  # `<pk>::v::<vid>`
    counts = await _apply(
        _plan(
            [first, dup],
            [_planned_offer(sku_key=first["sku_key"]),
             _planned_offer(sku_key=_promoter_key(), dest=DEST + "?v=2")],
        ),
        batch=False,
    )

    assert counts["skus_deduped_same_identity"] == 1
    assert counts["skus"] == 1, "counts['skus'] overstated the rows written"
    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert len(rows) == 1
    assert dict(rows[0])["sku_key"] == first["sku_key"], "the survivor is the first"
    assert dict(rows[0])["title"] == "Ruby (re-ingest)"
    # both offers survive, keyed on the row that actually exists
    keys = sorted(dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_offers WHERE product_key = :pk", {"pk": PK}))
    assert keys == [first["sku_key"], first["sku_key"]]
    assert counts["offers"] == 2


# --- (k) the SQLSTATE classifier ---------------------------------------------


def test_is_unique_violation_keys_on_the_sqlstate_not_the_class_name():
    """The classifier decides whether a failure is reported to operators as "the
    two lanes are still fighting over a key" or as a plain write failure. It must
    read the driver's SQLSTATE — a class-name-only test misses every wrapper, and
    a message-text test folds every other constraint failure into this bucket."""
    import services.catalog_variant_promoter as promoter
    from services.catalog_enrichment_agent.apply import (
        _is_unique_violation as apply_is_uv,
    )

    class SomeDriverError(Exception):
        """A name no substring match would ever catch."""

    class UniqueViolationError(Exception):
        """asyncpg's own class, with no sqlstate attribute set."""

    class NotNullViolationError(Exception):
        pass

    tagged = SomeDriverError("duplicate key value violates unique constraint")
    tagged.sqlstate = "23505"
    by_class = UniqueViolationError("no sqlstate here")
    not_null = NotNullViolationError("null value in column")
    not_null.sqlstate = "23502"
    # a message that LOOKS like a unique violation, under a code that is not one
    liar = SomeDriverError("duplicate key value violates unique constraint")
    liar.sqlstate = "23502"

    for is_uv in (apply_is_uv, promoter._is_unique_violation):
        assert is_uv(tagged) is True, "SQLSTATE 23505 must classify on the code"
        assert is_uv(by_class) is True, "the class-name fallback was dropped"
        assert is_uv(not_null) is False
        assert is_uv(liar) is False, "classified on the message text"
        # the driver error hidden behind a wrapper is still found
        wrapped = RuntimeError("query failed")
        wrapped.__cause__ = tagged
        assert is_uv(wrapped) is True


async def test_a_non_unique_failure_is_not_counted_as_an_identity_conflict(db):
    """The classification must key on SQLSTATE 23505, never on message text. A
    NOT NULL violation (23502) is a write failure like any other and must not be
    reported as an identity conflict — the count is what an operator would read to
    decide whether the two lanes are still fighting over a key."""
    await _seed_product(db)
    counts = await _apply(_plan([_planned_sku(title=None)], []), batch=False)

    assert counts["skus"] == 0
    assert counts["skus_identity_conflict"] == 0


async def test_the_bulk_path_refuses_the_same_residual_and_keeps_the_chunk(db):
    """The residual conflict on the batched executor: the refused row does not take
    the chunk (or the offers stage) with it, and `bulk_upsert` still classifies a
    write-time 23505 through `on_row_error` — the hook exists because the exception
    never leaves bulk_writer otherwise."""
    await _seed_case_c(db)
    counts = await _apply(
        _plan([_planned_sku(), _planned_sku(vid=VID2)], []), batch=True
    )
    assert counts["skus_identity_conflict"] == 1
    assert counts["skus"] == 1


# --- (d) the promoter --------------------------------------------------------


async def _seed_group(database, *, group_id=GROUP_ID, spid=SPID):
    await database.execute(
        """INSERT INTO product_group_members
             (product_group_id, merchant_id, platform, platform_product_id, is_primary)
           VALUES (:g,:m,:p,:spid,TRUE)""",
        {"g": group_id, "m": MERCHANT, "p": PLATFORM, "spid": spid},
    )


def _payload_with_variants():
    return {"variants": [
        {"variant_id": VID, "title": "Ruby", "sku": "SKU-RUBY", "currency": "USD",
         "options": [{"name": "Shade", "value": "Ruby", "axis_kind": "shade"}]},
    ]}


async def test_the_promoter_upsert_executes_and_adopts_without_renaming(db):
    """`promote_variants_all` has been unexecutable since migration 123 (42P10 on
    an ON CONFLICT clause naming a dropped index). This drives the real group path
    against the real index — and the row it lands on is one INGESTION wrote, under
    the `::v:` spelling, carrying the backfill's stamps and a live offer."""
    import services.catalog_variant_promoter as promoter

    ingestion_key = _ingestion_key()
    await _seed_product(db, payload=_payload_with_variants())
    await _seed_group(db)
    await _seed_sku(db, sku_key=ingestion_key, payload=_backfill_payload())
    offer_id = await _seed_backfill_offer(db, sku_key=ingestion_key)

    outcome = await promoter.promote_variants_for_group(group_id=GROUP_ID, apply=True)

    assert outcome.skipped_reason is None
    assert outcome.variants_promoted == 1
    assert outcome.skus_identity_conflict == 0

    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    assert len(rows) == 1, "the promoter minted a rival spelling of the same variant"
    sku = dict(rows[0])
    assert sku["sku_key"] == ingestion_key, "the promoter RENAMED a live primary key"
    assert sku["title"] == "Ruby"
    assert sku["sku"] == "SKU-RUBY"

    payload = _jsonb(sku["sku_payload"])
    assert payload["variant_id_provenance"] == "merchant_issued", "the merge replaced"
    assert payload["source_system"] == BACKFILL_SOURCE_SYSTEM

    # the offer keyed on that sku_key is still attached to a row that exists
    assert await db.fetch_val(
        "SELECT sku_key FROM catalog_offers WHERE offer_id = :o", {"o": offer_id}
    ) == ingestion_key


async def test_a_second_promoter_run_is_idempotent(db):
    """One identity is one row, however many times the promoter runs."""
    import services.catalog_variant_promoter as promoter

    await _seed_product(db, payload=_payload_with_variants())
    await _seed_group(db)

    first = await promoter.promote_variants_for_group(group_id=GROUP_ID, apply=True)
    keys_after_first = sorted(dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK}))
    second = await promoter.promote_variants_for_group(group_id=GROUP_ID, apply=True)
    keys_after_second = sorted(dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK}))

    assert first.variants_promoted == 1 and second.variants_promoted == 1
    assert keys_after_first == keys_after_second == [_promoter_key()]


async def test_the_promoter_counts_the_residual_trap_and_keeps_going(db):
    """Same key, different identity — on the promoter's side. The variant is
    skipped and counted; the group is not taken down with it (a 23505 aborts the
    enclosing transaction, so this only holds because each row runs in its own
    savepoint)."""
    import services.catalog_variant_promoter as promoter

    payload = {"variants": [
        {"variant_id": VID, "title": "Ruby",
         "options": [{"name": "Shade", "value": "Ruby"}]},
        {"variant_id": VID2, "title": "Coral",
         "options": [{"name": "Shade", "value": "Coral"}]},
    ]}
    await _seed_product(db, payload=payload)
    await _seed_group(db)
    # the promoter's own key for VID, held by a DIFFERENT identity tuple
    await _seed_sku(
        db, sku_key=_promoter_key(VID), merchant=OTHER_MERCHANT,
        payload={"agent_version": "other"},
    )

    outcome = await promoter.promote_variants_for_group(group_id=GROUP_ID, apply=True)

    assert outcome.skus_identity_conflict == 1
    assert outcome.variants_promoted == 1
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk AND merchant_id = :m",
        {"pk": PK, "m": MERCHANT}) == 1


# --- (d2) the promoter's own column limits -----------------------------------


def test_a_promoter_sku_key_that_already_fits_is_byte_identical():
    """The truncation below must be unreachable for any key that could already have
    been written. `<pk>::v::<vid>` is the PRIMARY KEY of the 4,286 rows the
    2026-09-08 backfill adopted and hung catalog_offers on — with no FK to catch a
    rename, changing a short key renames live supply out from under its offers."""
    from services.catalog_variant_promoter import _derive_sku_key

    assert _derive_sku_key(PK, VID) == f"{PK}::v::{VID}"
    assert _derive_sku_key("pk_short", "1") == "pk_short::v::1"


def test_a_promoter_sku_key_is_bounded_by_the_column():
    """catalog_skus.sku_key is varchar(255) and product_key alone reaches 214, so a
    real merchant variant id overruns it. An over-long bind is SQLSTATE 22001, not
    a unique violation — it used to be re-raised and abort the whole run."""
    from services.catalog_variant_promoter import _derive_sku_key

    assert len(LONG_PK) == 214
    key = _derive_sku_key(LONG_PK, LONG_VID)
    assert len(key) <= 255
    assert key.startswith(LONG_PK + "::v::")
    # stable across runs: the same id derives the same key
    assert key == _derive_sku_key(LONG_PK, LONG_VID)
    # ...and distinct ids do not collapse onto one key
    assert key != _derive_sku_key(LONG_PK, LONG_VID + "7")


async def test_the_promoter_run_survives_an_over_long_variant_id(db):
    """END TO END on the real column widths: a 214-char product_key and a 140-char
    variant id. Pre-fix the first such variant raised 22001 and `promote_variants_all`
    aborted — every group still queued behind it went unpromoted."""
    import services.catalog_variant_promoter as promoter

    payload = {"variants": [
        {"variant_id": LONG_VID, "title": "Ruby",
         "options": [{"name": "Shade", "value": "Ruby"}]},
    ]}
    await _seed_product(db, payload=payload, pk=LONG_PK, spid=SPID)
    await _seed_group(db, group_id=GROUP_ID_LONG, spid=SPID)

    report = await promoter.promote_variants_all(
        apply=True, product_group_id=GROUP_ID_LONG
    )

    assert report.skus_write_failed_total == 0
    assert report.skus_upserted_total == 1
    rows = await db.fetch_all(
        "SELECT * FROM catalog_skus WHERE product_key = :pk", {"pk": LONG_PK})
    assert len(rows) == 1, "the over-long variant never landed"
    sku = dict(rows[0])
    assert len(sku["sku_key"]) <= 255
    # source_variant_id is varchar(128) and the id is 140 chars
    assert sku["source_variant_id"] == LONG_VID[:128]


async def test_one_unwritable_variant_does_not_abort_the_promoter_run(db):
    """A NON-unique write error on one variant used to be re-raised, taking the
    group — and, through `promote_variants_all`, every group behind it — down. Here
    an over-long `sku` (varchar(128)) raises 22001; the savepoint has already rolled
    that row back, so it is counted and the run finishes."""
    import services.catalog_variant_promoter as promoter

    payload = {"variants": [
        {"variant_id": VID, "title": "Ruby", "sku": "S" * 300,
         "options": [{"name": "Shade", "value": "Ruby"}]},
        {"variant_id": VID2, "title": "Coral", "sku": "SKU-CORAL",
         "options": [{"name": "Shade", "value": "Coral"}]},
    ]}
    await _seed_product(db, payload=payload)
    await _seed_group(db)

    report = await promoter.promote_variants_all(apply=True, product_group_id=GROUP_ID)

    assert report.skus_write_failed_total == 1
    assert report.skus_identity_conflict_total == 0, "22001 is not a unique violation"
    assert report.skus_upserted_total == 1
    keys = [dict(r)["sku_key"] for r in await db.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK})]
    assert keys == [_promoter_key(VID2)]


# --- (f) the PREPARE gate ----------------------------------------------------


def test_both_statements_are_collected_by_the_repo_prepare_gate():
    """The statements must be SEEN by `tests/test_repo_sql_prepare_postgres.py`,
    which only follows module-level literal constants passed by name to a
    `database.*` call. A statement moved into an f-string, a helper's return value
    or a function-local would drop straight out of that sweep — silently, since
    the gate reports only what it collected."""
    from tests.test_repo_sql_prepare_postgres import collect_statements

    def _norm(sql):
        return re.sub(r"\s+", " ", sql).strip()

    import services.catalog_enrichment_agent.apply as apply_mod
    import services.catalog_variant_promoter as promoter

    collected = {_norm(sql) for _, sql in collect_statements()}
    for label, sql in (
        ("apply._SKU_UPSERT_SQL", apply_mod._SKU_UPSERT_SQL),
        ("apply._SKU_IDENTITY_LOOKUP_SQL", apply_mod._SKU_IDENTITY_LOOKUP_SQL),
        ("apply._SKU_KEY_HOLDER_LOOKUP_SQL", apply_mod._SKU_KEY_HOLDER_LOOKUP_SQL),
        ("apply._SKU_SUPPRESSED_IDENTITY_SQL", apply_mod._SKU_SUPPRESSED_IDENTITY_SQL),
        ("apply._SKU_IDENTITY_HEAL_SQL", apply_mod._SKU_IDENTITY_HEAL_SQL),
        ("promoter.UPSERT_SKU_SQL", promoter.UPSERT_SKU_SQL),
    ):
        assert _norm(sql) in collected, f"{label} is not collected by the PREPARE gate"
