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
    await database.execute("DELETE FROM catalog_offers WHERE product_key = :pk", {"pk": PK})
    await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})
    await database.execute(
        "DELETE FROM product_group_members WHERE product_group_id = :g", {"g": GROUP_ID}
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


async def _seed_product(database, *, merchant=MERCHANT, payload=None):
    await database.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform,
             source_product_id, source_domain, title, product_payload)
           VALUES (:pk,:m,:p,:spid,'brand.example','Gate Lipstick',
                   CAST(:pl AS jsonb))""",
        {"pk": PK, "m": merchant, "p": PLATFORM, "spid": SPID,
         "pl": json.dumps(payload or {})},
    )


async def _seed_sku(database, *, sku_key, vid=VID, merchant=MERCHANT, payload, title="Ruby"):
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload,
             readiness_tier, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:v,:t,CAST(:pl AS jsonb),
                   'commerce_ready',NOW())""",
        {"sk": sku_key, "pk": PK, "m": merchant, "p": PLATFORM, "spid": SPID,
         "v": vid, "t": title, "pl": json.dumps(payload)},
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


async def test_the_same_key_under_a_different_identity_is_counted_and_the_batch_continues(db):
    """The trap the arbiter swap does NOT close, from the other direction: this
    row's `sku_key` is already held by a DIFFERENT identity tuple (a product whose
    merchant_id was re-resolved). Neither constraint can be satisfied, so the row
    is classified by SQLSTATE, counted, logged, and skipped — and the rows behind
    it in the same batch still land.

    With `ON CONFLICT (sku_key)` this row would SILENTLY succeed, updating a row
    whose merchant_id disagrees with the plan's."""
    await _seed_product(db)
    # same sku_key ingestion would derive, but under another merchant
    await _seed_sku(
        db, sku_key=_ingestion_key(), merchant=OTHER_MERCHANT,
        payload={"agent_version": "other"},
    )

    trapped = _planned_sku()                      # merchant MERCHANT, key already taken
    clean = _planned_sku(vid=VID2)                # must still land
    counts = await _apply(
        _plan([trapped, clean], [_planned_offer(sku_key=clean["sku_key"])]),
        batch=False,
    )

    assert counts["skus_identity_conflict"] == 1
    assert counts["skus"] == 1
    # the batch CONTINUED: the offer stage ran after the failure, inside the same
    # transaction the 23505 would otherwise have aborted (25P02).
    assert counts["offers"] == 1
    landed = await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE product_key = :pk AND merchant_id = :m",
        {"pk": PK, "m": MERCHANT})
    assert landed == 1
    assert await db.fetch_val(
        "SELECT merchant_id FROM catalog_skus WHERE sku_key = :k",
        {"k": _ingestion_key()}) == OTHER_MERCHANT, "the trapped row was overwritten"


async def test_a_non_unique_failure_is_not_counted_as_an_identity_conflict(db):
    """The classification must key on SQLSTATE 23505, never on message text. A
    NOT NULL violation (23502) is a write failure like any other and must not be
    reported as an identity conflict — the count is what an operator would read to
    decide whether the two lanes are still fighting over a key."""
    await _seed_product(db)
    counts = await _apply(_plan([_planned_sku(title=None)], []), batch=False)

    assert counts["skus"] == 0
    assert counts["skus_identity_conflict"] == 0


async def test_the_bulk_path_classifies_the_same_trap_rather_than_failing_the_chunk(db):
    """`bulk_upsert` treats a 23505 as a DATA error (not transport), so the chunk
    replays row by row and only the bad row is skipped. `on_row_error` is what
    lets the caller classify it — without the hook the exception never leaves
    bulk_writer and the count could only be inferred."""
    await _seed_product(db)
    await _seed_sku(
        db, sku_key=_ingestion_key(), merchant=OTHER_MERCHANT,
        payload={"agent_version": "other"},
    )
    counts = await _apply(
        _plan([_planned_sku(), _planned_sku(vid=VID2)], []), batch=True
    )
    assert counts["skus_identity_conflict"] == 1
    assert counts["skus"] == 1


# --- (d) the promoter --------------------------------------------------------


async def _seed_group(database):
    await database.execute(
        """INSERT INTO product_group_members
             (product_group_id, merchant_id, platform, platform_product_id, is_primary)
           VALUES (:g,:m,:p,:spid,TRUE)""",
        {"g": GROUP_ID, "m": MERCHANT, "p": PLATFORM, "spid": SPID},
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
        ("promoter.UPSERT_SKU_SQL", promoter.UPSERT_SKU_SQL),
    ):
        assert _norm(sql) in collected, f"{label} is not collected by the PREPARE gate"
