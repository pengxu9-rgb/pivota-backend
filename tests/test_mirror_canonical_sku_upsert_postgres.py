"""The external-seed mirror's catalog_skus upsert, EXECUTED against a real Postgres.

WHY THIS FILE EXISTS. `catalog_skus` carries TWO unique constraints — the PK
`sku_key` and `idx_catalog_skus_source_identity_v2 (merchant_id, platform,
product_key, source_variant_id)` — and Postgres INFERS one arbiter from an
`ON CONFLICT` clause; it never falls through to the other. Which one a statement
names is therefore semantics, and it is semantics no SQLite test can see: the
promoter's clause named an index migration 123 had DROPPED and Postgres refused
it at PARSE time (42P10) for weeks while a string assertion stayed green.

Commit f846ad546 moved BOTH sibling statements
(`catalog_enrichment_agent/apply._SKU_UPSERT_SQL`,
`catalog_variant_promoter.UPSERT_SKU_SQL`) onto the identity index.
`scripts/mirror_external_seeds_to_catalog_products._upsert_canonical_sku_for_mirror_row`
deliberately did NOT move, and this file is the executable record of why:

  (a) for THIS writer the two arbiters address the same row. `merchant_id` and
      `platform` are encoded inside `product_key`
      (`prod::{merchant_id}::{platform}::{spid}`), `source_variant_id` IS
      `product_key`, and `sku_key` is `product_key || '::canonical'` — so the
      4-tuple and the PK are in bijection over every row the lane can emit.

  (b) the one shape in which they diverge is a FOREIGN row holding this tuple
      under a different `sku_key`. Under the PK arbiter that raises 23505 —
      loud, and the caller logs it. Under the identity arbiter it upserts onto
      the foreign row and keeps THAT row's key, while the offer written one line
      later is keyed on `derive_mirror_sku_key(product_key)` and would hang on a
      `::canonical` key that does not exist. Repointing without an adoption
      helper trades a loud failure for a silent orphan; both halves are executed
      below rather than argued.

THE PROD CENSUS, taken 2026-09-08, is what settles it. Of 24,558
`platform='external_seed'` SKUs, 11,908 carry `source_variant_id = product_key`
and ALL of them are held under `product_key || '::canonical'` — rows holding
that tuple under any other key: ZERO. So case (b) has never fired here.

The same census found the OPPOSITE shape live: three rows hold
`<pk>::canonical` while carrying a `source_variant_id` that is not the
product_key. Those refresh correctly under the PK arbiter and would raise 23505
on the PRIMARY KEY under the identity arbiter, every run. Repointing would break
working rows, not fix broken ones. Both directions are executed below.

FIXTURE DISCIPLINE. The dialect gate runs every `tests/test_*_postgres.py`
against ONE shared database. This file builds its tables from the `db/` models
(`tests.model_schema.ensure_model_tables`), NEVER drops or re-creates a shared
one, and deletes only its own rows on teardown. `ensure_model_tables`' column
patch step is SQLite-only, so the columns depended on here are patched with
`ADD COLUMN IF NOT EXISTS` in case an alphabetically-earlier file left a
narrower table behind.
"""

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    not str(os.getenv("DATABASE_URL") or "").startswith("postgres"),
    reason="needs a real Postgres DATABASE_URL — production-dialect gate",
)

MERCHANT = "merch_obs_mirrorgate"
PLATFORM = "external_seed"
EPID = "ext_mirror_canonical_gate"
#: Storage format, because the bijection claim in (a) rests on merchant_id and
#: platform being recoverable FROM the product key rather than supplied beside it.
PK = f"prod::{MERCHANT}::{PLATFORM}::{EPID}"
DEST = "https://brand.example/products/mirror-canonical-gate"

#: The identity arbiter the two SIBLING statements moved to. Named here so the
#: divergence test drives the real alternative rather than a paraphrase of it.
IDENTITY_ARBITER = "ON CONFLICT (merchant_id, platform, product_key, source_variant_id)"

#: Keys the 2026-09-08 variant-identity backfill stamps on a row it adopts, and
#: the ones a replacing `sku_payload = EXCLUDED.sku_payload` erased.
FOREIGN_STAMP = {
    "variant_id_provenance": "merchant_issued",
    "source_system": "variant_identity_backfill_v1",
}

_PATCH_COLUMNS = (
    ("source_domain", "text"), ("barcode", "varchar(128)"), ("image_url", "text"),
    ("sku_payload", "jsonb"), ("ingredient_ids", "jsonb"),
    ("visible_attributes", "jsonb"), ("visible_option_labels", "jsonb"),
    ("currency", "varchar(16)"), ("sku", "varchar(128)"),
)

#: Asserted rather than assumed. Without this index every test below would pass
#: by inserting a second row and the conflict path would never run at all.
_IDENTITY_INDEX = """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2
    ON catalog_skus (merchant_id, platform, product_key, source_variant_id)
"""


def _row_dict():
    return {
        "external_product_id": EPID,
        "title": "Mirror Gate Serum",
        "image_url": "https://img.example/mirror-gate.jpg",
        "price_currency": "USD",
        "destination_url": DEST,
    }


async def _clear(database):
    """Remove this fixture's ROWS. Never its table — the gate shares one database
    and a dropped table poisons whichever file collects next."""
    await database.execute(
        "DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK}
    )


@pytest.fixture
async def db():
    from db.catalog import catalog_skus
    from db.database import database
    from tests.model_schema import ensure_model_tables

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables([catalog_skus])
    for name, coltype in _PATCH_COLUMNS:
        await database.execute(
            f"ALTER TABLE catalog_skus ADD COLUMN IF NOT EXISTS {name} {coltype}"
        )
    await database.execute(_IDENTITY_INDEX)
    await _clear(database)
    try:
        yield database
    finally:
        await _clear(database)
        if not was_connected and database.is_connected:
            await database.disconnect()


async def _mirror_upsert(product_key=PK, merchant_id=MERCHANT, **overrides):
    """Drive the REAL writer against the REAL database — no recorder, no copy of
    the statement. This is the call the mirror's `_apply` makes."""
    import scripts.mirror_external_seeds_to_catalog_products as mirror

    row = _row_dict()
    row.update(overrides)
    await mirror._upsert_canonical_sku_for_mirror_row(
        product_key, row, merchant_id=merchant_id
    )


async def _capture_statement(monkeypatch):
    """The (sql, params) the writer actually emits, so the divergence test can
    re-run the SAME text with only the ON CONFLICT target swapped."""
    import scripts.mirror_external_seeds_to_catalog_products as mirror

    seen = []

    class _Recorder:
        async def execute(self, sql, params):
            seen.append((str(sql), dict(params)))

    monkeypatch.setattr(mirror, "database", _Recorder())
    row = _row_dict()
    await mirror._upsert_canonical_sku_for_mirror_row(PK, row, merchant_id=MERCHANT)
    monkeypatch.undo()
    assert len(seen) == 1
    return seen[0]


async def _seed_rival(database, *, sku_key, payload):
    """A row holding the mirror's identity tuple under a DIFFERENT primary key —
    the only shape in which the two arbiters disagree."""
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, sku_payload,
             readiness_tier, created_at, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,:pk,'Rival Spelling',CAST(:pl AS jsonb),
                   'commerce_ready',NOW(),NOW())""",
        {"sk": sku_key, "pk": PK, "m": MERCHANT, "p": PLATFORM, "spid": EPID,
         "pl": json.dumps(payload)},
    )


async def _keys(database):
    rows = await database.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": PK}
    )
    return sorted(dict(r)["sku_key"] for r in rows)


def _sqlstate(exc):
    return getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)


# --- preconditions -----------------------------------------------------------


async def test_the_identity_index_this_file_reasons_about_actually_exists(db):
    """Every conflict test below would pass by inserting a fresh row if the index
    were absent, and would prove nothing about arbiter choice."""
    assert await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE tablename='catalog_skus' "
        "AND indexname='idx_catalog_skus_source_identity_v2'"
    ) == 1


async def test_the_three_column_predecessor_the_old_comments_cited_is_gone(db):
    """The comments this change corrects justified the convention by citing
    `idx_catalog_skus_source_identity (merchant_id, platform, source_variant_id)`.
    Migration 123 dropped it. If it ever came back, the prose would need
    rewriting again — and the promoter's 42P10 outage says a dead index name is
    not a harmless one."""
    assert await db.fetch_val(
        "SELECT count(*) FROM pg_indexes WHERE tablename='catalog_skus' "
        "AND indexname='idx_catalog_skus_source_identity'"
    ) == 0


# --- (a) the two arbiters address the same row -------------------------------


async def test_the_statement_executes_and_a_rerun_lands_on_its_own_row(db):
    """The promoter lesson: a string assertion cannot tell a live index from a
    dead one. Execute it, twice, and count rows."""
    await _mirror_upsert()
    await _mirror_upsert(title="Mirror Gate Serum (refreshed)")

    assert await _keys(db) == [f"{PK}::canonical"]
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": f"{PK}::canonical"}
    ) == "Mirror Gate Serum (refreshed)"


async def test_the_pk_and_the_identity_tuple_select_the_same_row(db):
    """THE REASON THE ARBITER DOES NOT NEED TO MOVE. `sku_key` is a total,
    injective function of `product_key`, and `product_key` determines the whole
    4-tuple — merchant_id and platform are encoded in it and source_variant_id
    IS it. So the PK arbiter and the identity arbiter cannot pick different rows
    for anything this writer emits."""
    await _mirror_upsert()

    by_pk = await db.fetch_val(
        "SELECT sku_key FROM catalog_skus WHERE sku_key = :k", {"k": f"{PK}::canonical"}
    )
    by_identity = await db.fetch_val(
        """SELECT sku_key FROM catalog_skus
           WHERE merchant_id = :m AND platform = :p
             AND product_key = :pk AND source_variant_id = :pk""",
        {"m": MERCHANT, "p": PLATFORM, "pk": PK},
    )
    assert by_pk == by_identity == f"{PK}::canonical"

    # And the tuple is recoverable from the key alone — the bijection, not a
    # coincidence of this fixture's inputs.
    assert PK.split("::")[1] == MERCHANT
    assert PK.split("::")[2] == PLATFORM


async def test_two_products_never_collide_on_either_constraint(db):
    """The original comment's stated fear — 'every row past the first collides' —
    was true only of the 3-column index. Under v2 two products differ in
    product_key, so they differ in BOTH the PK and the identity tuple."""
    other_pk = f"prod::{MERCHANT}::{PLATFORM}::ext_mirror_canonical_gate_two"
    await _mirror_upsert()
    try:
        await _mirror_upsert(product_key=other_pk, external_product_id="two")
        rows = await db.fetch_all(
            "SELECT sku_key FROM catalog_skus WHERE product_key IN (:a, :b)",
            {"a": PK, "b": other_pk},
        )
        assert len(rows) == 2
    finally:
        await db.execute(
            "DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": other_pk}
        )


# --- (b) where the two arbiters diverge --------------------------------------


async def test_a_foreign_row_on_this_tuple_fails_LOUDLY_under_the_pk_arbiter(db):
    """The divergence shape, half one. A rival spelling of the same identity makes
    the insert violate the index the clause did NOT name. 23505 propagates, and
    `_apply` logs 'chain write failed' — the SKU is stale but nothing is silently
    wrong about it."""
    rival_key = f"{PK}::v:{PK}"
    await _seed_rival(db, sku_key=rival_key, payload=FOREIGN_STAMP)

    with pytest.raises(Exception) as caught:
        await _mirror_upsert()
    assert _sqlstate(caught.value) == "23505", (
        f"expected a unique violation, got {caught.value!r}"
    )
    assert await _keys(db) == [rival_key]


async def test_the_identity_arbiter_would_absorb_that_row_and_orphan_the_offer(
    db, monkeypatch
):
    """The divergence shape, half two — and the reason repointing this statement
    would be a REGRESSION rather than the sibling's fix.

    Run the writer's own statement with nothing changed but the ON CONFLICT
    target. It succeeds, which is what makes it dangerous: it upserts onto the
    rival row and keeps THAT row's key. `<pk>::canonical` never comes into
    existence — and `upsert_catalog_offer_from_seed_row` derives its `sku_key`
    from `derive_mirror_sku_key(product_key)`, not from anything this statement
    returns, so the offer written immediately afterwards points at a row that is
    not there. The siblings could move only because f846ad546 gave them
    `_adopt_existing_sku_identities` to re-key the offer first."""
    from services.external_offer_dual_write import derive_mirror_sku_key

    sql, params = await _capture_statement(monkeypatch)
    assert "ON CONFLICT (sku_key)" in sql, "the shipped statement no longer names the PK"
    repointed = sql.replace("ON CONFLICT (sku_key)", IDENTITY_ARBITER)

    rival_key = f"{PK}::v:{PK}"
    await _seed_rival(db, sku_key=rival_key, payload=FOREIGN_STAMP)
    await db.execute(repointed, params)

    assert await _keys(db) == [rival_key]
    # The row the offer will be written against, which the adoption did not create.
    assert await db.fetch_val(
        "SELECT count(*) FROM catalog_skus WHERE sku_key = :k",
        {"k": derive_mirror_sku_key(PK)},
    ) == 0
    # The update did land — so nothing raises, nothing is logged, and the orphan
    # is invisible until recall drops the product.
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": rival_key}
    ) == "Mirror Gate Serum"


async def test_a_canonical_key_carrying_a_foreign_identity_still_refreshes(db):
    """THE OTHER LIVE SHAPE, and the second reason not to repoint. The prod
    census of 2026-09-08 found three rows holding `<pk>::canonical` as their key
    while carrying a `source_variant_id` that is NOT the product_key — one key
    under an identity this lane does not spell.

    The PK arbiter finds them by key and refreshes them, which is what happens
    today. The identity arbiter would match no tuple, attempt an INSERT, and hit
    the PRIMARY KEY instead — 23505 on every one of them, every run. Repointing
    would break rows that currently work, which is the opposite of the sibling's
    situation."""
    canonical_key = f"{PK}::canonical"
    await db.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, readiness_tier,
             created_at, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,'legacy-default','Stale Title',
                   'commerce_ready',NOW(),NOW())""",
        {"sk": canonical_key, "pk": PK, "m": MERCHANT, "p": PLATFORM, "spid": EPID},
    )

    await _mirror_upsert(title="Refreshed By The PK Arbiter")

    assert await _keys(db) == [canonical_key]
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": canonical_key}
    ) == "Refreshed By The PK Arbiter"
    # The DO UPDATE touches no identity column, so the row keeps the identity it
    # arrived with. Healing that is a backfill's job, not this statement's.
    assert await db.fetch_val(
        "SELECT source_variant_id FROM catalog_skus WHERE sku_key = :k",
        {"k": canonical_key},
    ) == "legacy-default"


async def test_the_identity_arbiter_would_break_that_row_on_the_primary_key(
    db, monkeypatch
):
    """The same row, driven through the repointed statement: neither constraint
    can be satisfied, so it raises rather than upserting. Three prod rows are in
    exactly this state."""
    canonical_key = f"{PK}::canonical"
    await db.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, title, readiness_tier,
             created_at, updated_at)
           VALUES (:sk,:pk,:m,:p,:spid,'legacy-default','Stale Title',
                   'commerce_ready',NOW(),NOW())""",
        {"sk": canonical_key, "pk": PK, "m": MERCHANT, "p": PLATFORM, "spid": EPID},
    )

    sql, params = await _capture_statement(monkeypatch)
    repointed = sql.replace("ON CONFLICT (sku_key)", IDENTITY_ARBITER)

    with pytest.raises(Exception) as caught:
        await db.execute(repointed, params)
    assert _sqlstate(caught.value) == "23505", (
        f"expected a primary-key violation, got {caught.value!r}"
    )
    assert await db.fetch_val(
        "SELECT title FROM catalog_skus WHERE sku_key = :k", {"k": canonical_key}
    ) == "Stale Title"


# --- the sku_payload merge ---------------------------------------------------


async def test_a_foreign_stamp_survives_a_mirror_rerun(db):
    """`sku_payload = EXCLUDED.sku_payload` replaced the whole document, so a
    mirror pass over a row another writer had stamped erased
    `variant_id_provenance` / `source_system` outright. The merge keeps them."""
    canonical_key = f"{PK}::canonical"
    await _mirror_upsert()
    await db.execute(
        """UPDATE catalog_skus
           SET sku_payload = COALESCE(sku_payload, CAST('{}' AS jsonb))
                             || CAST(:stamp AS jsonb)
           WHERE sku_key = :k""",
        {"k": canonical_key, "stamp": json.dumps(FOREIGN_STAMP)},
    )

    await _mirror_upsert(title="Mirror Gate Serum (refreshed)")

    payload = await db.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE sku_key = :k", {"k": canonical_key}
    )
    payload = json.loads(payload) if isinstance(payload, str) else payload
    for key, value in FOREIGN_STAMP.items():
        assert payload.get(key) == value, f"{key} was erased by the mirror re-run"


async def test_the_mirrors_own_keys_still_win_over_a_stale_value(db):
    """A merge must not turn the row read-only to its own writer. Every key this
    lane sets is present in EXCLUDED on every pass, so `||` overwrites them."""
    canonical_key = f"{PK}::canonical"
    await _mirror_upsert()
    await db.execute(
        """UPDATE catalog_skus
           SET sku_payload = COALESCE(sku_payload, CAST('{}' AS jsonb))
                             || CAST(:stale AS jsonb)
           WHERE sku_key = :k""",
        {"k": canonical_key,
         "stale": json.dumps({"destination_url": "https://stale.example/gone"})},
    )

    await _mirror_upsert()

    payload = await db.fetch_val(
        "SELECT sku_payload FROM catalog_skus WHERE sku_key = :k", {"k": canonical_key}
    )
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload["destination_url"] == DEST
    assert payload["synthetic_canonical_variant"] is True
