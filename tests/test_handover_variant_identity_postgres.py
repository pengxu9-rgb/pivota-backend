"""The hand-over resolver's REAL query, against a real `catalog_skus`.

WHY A POSTGRES FILE. The decision logic is pinned on SQLite in
`tests/test_handover_variant_identity.py`; what cannot be pinned there is everything the
DATABASE decides, and that is the half that has bitten this lane before:

  - `sku_payload` is a `jsonb` COLUMN, and a raw-SQL read through `databases` + asyncpg
    hands it back as a JSON **string** anyway (the SQLAlchemy type is not applied to a text
    query) — the opposite of what a reader assumes, and the branch that carries the stamp veto
    and the truncation check;
  - `source_variant_id` is `String(128)`, so the truncation case the unit test constructs by
    hand is a real column bound here, and the identity index
    `(merchant_id, platform, product_key, source_variant_id)` makes two ids that differ only
    past character 128 ONE row;
  - the statement joins `catalog_products` and filters `suppressed_at` on both tables, and a
    string assertion about SQL cannot tell a correct filter from a plausible-looking wrong one
    (the lesson `test_backfill_variant_identity_skus_postgres` was written for: five reverted
    fixes all survived a green 27-test baseline).

GATE HYGIENE. Every `tests/test_*_postgres.py` runs against ONE database in alphabetical order.
This file therefore builds its tables through `ensure_model_tables` (production's exact DDL,
patching a narrower table another file may have created first), NEVER drops a shared table, and
deletes only rows carrying its own `_PREFIX`. The module-end fixture then asserts no row with
that prefix survives, so a miss turns THIS file red instead of poisoning the next one.
"""

import datetime as dt
import json
import os

import pytest

DATABASE_URL = str(os.getenv("DATABASE_URL") or "")
_IS_PG = DATABASE_URL.startswith("postgres")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="needs a real Postgres DATABASE_URL — production-dialect gate"
)

#: Every row this file writes carries this prefix, and nothing else in the gate does. It is the
#: unit of both the teardown and the residue check.
_PREFIX = "handoverpg"
MERCHANT = "m_" + _PREFIX
PLATFORM = "external_seed"
PK = f"prod::{MERCHANT}::{PLATFORM}::{_PREFIX}-serum"
PK_MULTI = f"prod::{MERCHANT}::{PLATFORM}::{_PREFIX}-lipstick"
PK_SUPPRESSED = f"prod::{MERCHANT}::{PLATFORM}::{_PREFIX}-withdrawn"
SPID = _PREFIX + "-serum"

VID = "43062643884185"
VID_B = "43062643884999"
#: Differs from VID_LONG only past character 128 — one `catalog_skus` identity, and the shape
#: the payload-agreement check exists for.
VID_LONG = "9" * 128 + "77"

_SUPPRESSED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


async def _ddl(database):
    from db.catalog import catalog_products, catalog_skus
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([catalog_products, catalog_skus])
    await database.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_skus_source_identity_v2 "
        "ON catalog_skus (merchant_id, platform, product_key, source_variant_id)"
    )


async def _clear(database):
    """This file's ROWS only. Scoped by merchant_id, which carries `_PREFIX` and which no other
    gate file uses — a product_key LIKE would also match a neighbour that happened to embed the
    same substring."""
    await database.execute(
        "DELETE FROM catalog_skus WHERE merchant_id = :m", {"m": MERCHANT})
    await database.execute(
        "DELETE FROM catalog_products WHERE merchant_id = :m", {"m": MERCHANT})


@pytest.fixture(scope="module", autouse=True)
def _nothing_outlives_this_module():
    """Runs after the last teardown. A row still carrying `_PREFIX` was written here and missed
    by `_clear`; failing here is how a neighbour on the next gate run is not poisoned."""
    yield
    if not _IS_PG:
        return
    from sqlalchemy import create_engine, text

    engine = create_engine(DATABASE_URL)
    try:
        with engine.begin() as conn:
            residue = {}
            for table in ("catalog_skus", "catalog_products"):
                if not conn.execute(
                    text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
                ).scalar():
                    continue
                residue[table] = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE merchant_id = :m"),
                    {"m": MERCHANT},
                ).scalar()
    finally:
        engine.dispose()
    assert not any(residue.values()), (
        f"this module left rows behind for the next gate file: {residue}")


@pytest.fixture
async def db():
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


def _spid(pk):
    """`idx_catalog_products_source_identity` is UNIQUE on (merchant_id, platform,
    source_product_id), so two fixture products sharing one source id is a 23505, not a
    fixture. Derived from the key so every product in this file has its own."""
    return pk.rsplit("::", 1)[-1]


async def _product(database, pk, *, suppressed=None):
    await database.execute(
        """INSERT INTO catalog_products (product_key, merchant_id, platform,
             source_product_id, source_domain, title, suppressed_at)
           VALUES (:pk,:m,:p,:spid,'brand.example','Handover Fixture',:sup)""",
        {"pk": pk, "m": MERCHANT, "p": PLATFORM, "spid": _spid(pk), "sup": suppressed},
    )


async def _sku(database, pk, source_variant_id, *, payload=None, suppressed=None, suffix=""):
    await database.execute(
        """INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform,
             source_product_id, source_variant_id, source_domain, title, currency,
             sku_payload, readiness_tier, suppressed_at)
           VALUES (:sk,:pk,:m,:p,:spid,:svid,'brand.example','Fixture','USD',
             CAST(:pl AS jsonb),'referral_only',:sup)""",
        {
            "sk": f"{pk}::v:{source_variant_id}{suffix}",
            "pk": pk, "m": MERCHANT, "p": PLATFORM, "spid": _spid(pk),
            "svid": source_variant_id,
            "pl": json.dumps(payload) if payload is not None else None,
            "sup": suppressed,
        },
    )


def _resolver():
    from services.handover_variant_identity import HandoverVariantResolver

    # No timeout cap: a shared gate database under a full run is slower than a serving path,
    # and a 0.5s ceiling here would make this file flake on the gate's own load rather than
    # test anything. The wall-clock bound itself is exercised in
    # `tests/test_handover_variant_identity.py::test_the_lookup_is_bounded_by_a_wall_clock_timeout`,
    # against a deliberately slow `_fetch`. (An earlier version of this comment claimed that
    # pin already existed when it did not — review caught it by deleting `asyncio.wait_for`
    # and watching 250 tests stay green.)
    return HandoverVariantResolver(timeout_s=30.0)


# ---------------------------------------------------------------------------------------------

async def test_the_real_statement_returns_the_merchant_issued_row(db):
    """The whole seam in one test: identity written where the backfill writes it, read by the
    resolver's own SQL, through asyncpg, out of a jsonb column."""
    from services.handover_variant_identity import R_SOLE, SOURCE_CATALOG_SKU

    await _product(db, PK)
    await _sku(db, PK, VID, payload={"variant_id": VID,
                                     "variant_id_provenance": "merchant_issued"})

    r = _resolver()
    await r.prime([PK])
    got = r.choose(product_key=PK)
    assert got.variant_id == VID
    assert got.reason == R_SOLE and got.source == SOURCE_CATALOG_SKU
    assert got.sku_key == f"{PK}::v:{VID}"


async def test_a_jsonb_payload_arrives_as_TEXT_and_the_stamp_still_vetoes(db):
    """MEASURED, not assumed — and it is the opposite of what a reader expects.

    A raw-SQL read through `databases` + asyncpg does NOT apply the SQLAlchemy JSONB type, so
    `sku_payload` comes back as a JSON **string** on Postgres, exactly as it does on SQLite. An
    earlier draft of this file asserted `isinstance(..., dict)` and failed here; the assertion
    was wrong, not the code. The reason it matters is that a payload reader written for a dict
    would find no `variant_id_provenance` in production, and a veto that cannot find its key
    does not veto — a fail-OPEN that no SQLite test could ever see. So the shape is pinned, and
    the veto is exercised on the value the driver actually hands over.

    (`sku_payload` is genuinely `jsonb` in the column — the INSERT casts to it and the
    truncation test below reads a value only jsonb could have stored. This is about the read
    path, not the storage.)
    """
    from services.handover_variant_identity import candidate_from_row

    await _product(db, PK)
    await _sku(db, PK, VID, payload={"variant_id": VID,
                                     "variant_id_provenance": "product_derived"})

    assert (await db.fetch_val(
        "SELECT pg_typeof(sku_payload)::text FROM catalog_skus WHERE merchant_id = :m",
        {"m": MERCHANT})) == "jsonb", "the column really is jsonb"

    row = dict(await db.fetch_one(
        "SELECT sku_key, product_key, source_product_id, source_variant_id, sku_payload "
        "  FROM catalog_skus WHERE merchant_id = :m", {"m": MERCHANT}))
    assert isinstance(row["sku_payload"], str), (
        "the driver's shape changed; re-read the payload reader before trusting the veto")
    assert candidate_from_row(row) is None, "and the stamp vetoes on that shape"


async def test_a_product_derived_id_stored_in_the_real_column_is_refused(db):
    """39.4% of the live table. Written here exactly as `ingestion.py:841` writes it."""
    from services.handover_variant_identity import R_NO_IDENTITY

    await _product(db, PK)
    await _sku(db, PK, PK[:128], payload=None)

    r = _resolver()
    await r.prime([PK])
    got = r.choose(product_key=PK)
    assert got.variant_id is None and got.reason == R_NO_IDENTITY


async def test_two_live_merchant_issued_rows_refuse_and_an_exact_name_resolves(db):
    """Both halves against the SAME two real rows, so neither can pass by accident."""
    from services.handover_variant_identity import R_AMBIGUOUS, R_EXACT

    await _product(db, PK_MULTI)
    await _sku(db, PK_MULTI, VID, payload=None)
    await _sku(db, PK_MULTI, VID_B, payload=None)

    r = _resolver()
    await r.prime([PK_MULTI])
    assert r.choose(product_key=PK_MULTI).reason == R_AMBIGUOUS
    named = r.choose(product_key=PK_MULTI, offer_variant_id=VID_B)
    assert named.variant_id == VID_B and named.reason == R_EXACT


async def test_a_suppressed_sku_is_not_a_candidate(db):
    """MUTANT: drop `s.suppressed_at IS NULL`.

    Suppression is a human withdrawal. Handing a withdrawn SKU to a cart builder republishes
    it as buyable supply — the same defect `backfill_variant_identity_skus` had to fix when its
    ORDER BY turned out to be a preference rather than a filter.
    """
    from services.handover_variant_identity import R_NO_IDENTITY

    await _product(db, PK)
    await _sku(db, PK, VID, payload=None, suppressed=_SUPPRESSED_AT)

    r = _resolver()
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_NO_IDENTITY


async def test_a_suppressed_product_takes_its_live_skus_with_it(db):
    """MUTANT: drop the `catalog_products` join, or its `cp.suppressed_at IS NULL`.

    2,171 suppressed products carry unsuppressed offers on prod (2026-09-08), so this is not a
    hypothetical row shape — it is a measured population, and the SKU-side filter alone does
    not catch it.
    """
    from services.handover_variant_identity import R_NO_IDENTITY

    await _product(db, PK_SUPPRESSED, suppressed=_SUPPRESSED_AT)
    await _sku(db, PK_SUPPRESSED, VID, payload=None)

    r = _resolver()
    await r.prime([PK_SUPPRESSED])
    assert r.choose(product_key=PK_SUPPRESSED).reason == R_NO_IDENTITY


async def test_an_id_truncated_by_the_column_bound_is_refused(db):
    """`source_variant_id` is String(128) and `sku_payload.variant_id` keeps the full id (#2148).

    Postgres enforces the bound; SQLite does not, so only here is the stored value genuinely
    the cut one. A cut variant id names a variant the merchant does not have, and the
    classifier — handed one string — cannot possibly see it.
    """
    from services.handover_variant_identity import R_NO_IDENTITY

    await _product(db, PK)
    await _sku(db, PK, VID_LONG[:128], payload={"variant_id": VID_LONG})

    stored = await db.fetch_val(
        "SELECT source_variant_id FROM catalog_skus WHERE merchant_id = :m", {"m": MERCHANT})
    assert len(stored) == 128 and stored != VID_LONG, "the bound must really have cut it"

    r = _resolver()
    await r.prime([PK])
    assert r.choose(product_key=PK).reason == R_NO_IDENTITY


async def test_one_statement_answers_several_product_keys(db):
    """The batching contract, over real bound parameters rather than a fake. asyncpg binds an
    IN-list of generated names; a driver that could not would fail here and nowhere else."""
    await _product(db, PK)
    await _product(db, PK_MULTI)
    await _sku(db, PK, VID, payload=None)
    await _sku(db, PK_MULTI, VID_B, payload=None)

    r = _resolver()
    await r.prime([PK, PK_MULTI])
    assert r.choose(product_key=PK).variant_id == VID
    assert r.choose(product_key=PK_MULTI).variant_id == VID_B
    assert r.stats["handover_rows_scanned"] == 2


async def test_another_products_row_is_never_borrowed(db):
    """The join is on `product_key`, which already carries the merchant — but the statement
    also has to return the merchant's OWN rows and nothing else, and a `product_key` is exactly
    the kind of value a mis-scoped join has borrowed across products before (the backfill's
    round-1 defect 3: 652 seeds whose attached key names a different product)."""
    await _product(db, PK)
    await _sku(db, PK, VID, payload=None)
    await _product(db, PK_MULTI)
    await _sku(db, PK_MULTI, VID_B, payload=None)

    r = _resolver()
    await r.prime([PK])
    assert [c.variant_id for c in r.candidates_for(PK)] == [VID]
    assert r.candidates_for(PK_MULTI) == []
