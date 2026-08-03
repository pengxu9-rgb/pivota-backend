"""A9-4 phase 2 must never detach an `attached_product_key` that already resolves.

WHAT THIS PREVENTS, stated as the incident it is named after.

On 2026-08-01 this phase "repaired" 720 seeds whose `attached_product_key` was
`ext:<slug>::<hash>` rather than the `prod::…` storage format
`docs/IDENTITY_REFERENCE.md` §4 documents. It validated that the key it was about
to WRITE resolved to a real `catalog_products` row. Nothing validated the key it
was about to OVERWRITE.

`ext:` is not a bare or malformed key. It is a LIVE product_key format on ~1,795
unsuppressed rows, every one Path-C (`catalog_enrichment_agent_v1`), and the
gateway routes a minted PDP by matching
`external_product_seeds.attached_product_key = cp.product_key`
(`services/pdp_renderability` seed_route_resolves_sql, minted arm). A seed
pointing at an `ext:` row is therefore CORRECT RELATIVE TO THAT ROW. Conforming
the seed while the row keeps its legacy key breaks the join.

Result: **364 elected, trust-public PDPs returned HTTP 500** — confirmed by
fetching them, not inferred from an invariant. Restored from a reconstructed
content_key mapping (587 exact, 133 by hand).

POSTGRES GATE because the guard is a `catalog_products` lookup — a SQLite-only
test could assert the counter moved without proving the query that decides it.

🚨 THESE GATE FILES SHARE ONE DATABASE. Additive, order-proof DDL only (#1651).
"""

from __future__ import annotations

import json
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — production-dialect gate",
)

_LIGHTWEIGHT_DDL = """
-- NO PRIMARY KEY on id. A PK implies NOT NULL, which is a NARROWING, and these
-- gate modules share one database: this file sorts first alphabetically, so a PK
-- here dictated the shape for every sibling and broke 18 of their inserts (they
-- create seeds without an id). #1651's rule is create-minimal-then-widen, and a
-- constraint is not a widening. The tests below supply ids explicitly.
CREATE TABLE IF NOT EXISTS external_product_seeds (id text);
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS external_product_id text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS attached_product_key text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS canonical_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS destination_url text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_data jsonb;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seller_ref text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS seed_kind text;
ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS updated_at timestamptz;
-- previous_value is DELIBERATELY omitted: in prod this table already exists
-- without it, so the statement that actually matters is the
-- `ALTER TABLE … ADD COLUMN IF NOT EXISTS` in ensure_checkpoint_table. Declaring
-- the column here would make that ALTER a no-op and leave the migration path
-- untested.
CREATE TABLE IF NOT EXISTS a9_4_backfill_checkpoint (
  phase text NOT NULL, ref_id text NOT NULL, observed_id text,
  status text NOT NULL DEFAULT 'done',
  updated_at timestamptz NOT NULL DEFAULT NOW(),
  PRIMARY KEY (phase, ref_id)
);
"""

_EXT_KEY = "ext:ilia-multi-stick::b7cd74d8"
_EPID = "ilia:00158e8d4f673daa"
_MIRROR_KEY = f"prod::external_seed::external_seed::{_EPID}"


@pytest.fixture(scope="module")
def pg_engine():
    import db.catalog  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(stmt))
    yield engine
    engine.dispose()


def _reset(conn):
    from sqlalchemy import text

    for t in ("a9_4_backfill_checkpoint", "external_product_seeds", "catalog_products"):
        conn.execute(text(f"DELETE FROM {t}"))


def _product(conn, *, product_key, source_product_id, source_system):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO catalog_products "
             "(product_key, merchant_id, platform, source_product_id, title, "
             " catalog_track, source_system) "
             "VALUES (:pk, 'external_seed', 'external_seed', :spid, :pk, "
             "        'external_referral', :ss)"),
        {"pk": product_key, "spid": source_product_id, "ss": source_system},
    )


def _seed(conn, *, seed_id, attached, epid=_EPID):
    from sqlalchemy import text

    conn.execute(
        text("INSERT INTO external_product_seeds "
             "(id, external_product_id, attached_product_key, destination_url, "
             " seed_data, status, updated_at) "
             "VALUES (:id, :epid, :apk, 'https://ilia.com/p/x', :sd, 'active', NOW())"),
        {"id": seed_id, "epid": epid, "apk": attached,
         "sd": json.dumps({"snapshot": {"brand": "ILIA"}})},
    )


async def _run_barekey(execute: bool):
    """Drive the real phase-2 code against this database."""
    from databases import Database

    from services import seller_identity
    from scripts.backfill_seller_of_record import BackfillReport, SellerBackfill

    db = Database(DATABASE_URL)
    await db.connect()
    try:
        bf = SellerBackfill(database=db, si_mod=seller_identity,
                            execute=execute, batch_size=50)
        await bf.ensure_checkpoint_table()
        report = BackfillReport(mode="execute" if execute else "dry_run",
                                started_at="2026-08-01T00:00:00Z",
                                phases=["barekey"], batch_size=50)
        await bf.run_barekey(report)
        return report
    finally:
        await db.disconnect()


def test_a_seed_on_a_resolving_ext_key_is_skipped_not_repaired(pg_engine):
    """THE REGRESSION TEST. The exact prod shape: a Path-C seed attached to an
    `ext:` row that EXISTS, whose derived `prod::` key also exists. Before the
    guard this was "repaired" and the minted PDP lost its route."""
    import asyncio

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, product_key=_EXT_KEY, source_product_id="ilia-multi-stick",
                 source_system="catalog_enrichment_agent_v1")
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:catalog_enrichment_agent_v1:x", attached=_EXT_KEY)

    report = asyncio.run(_run_barekey(execute=True))
    assert report.barekey["scanned"] == 1
    assert report.barekey["already_resolving"] == 1
    assert report.barekey["repaired"] == 0

    from sqlalchemy import text

    with pg_engine.begin() as conn:
        still = conn.execute(text(
            "SELECT attached_product_key FROM external_product_seeds "
            "WHERE id = 'seed:catalog_enrichment_agent_v1:x'")).scalar()
    assert still == _EXT_KEY, "the working link must NOT be detached"


def test_a_genuinely_dangling_key_is_still_repaired(pg_engine):
    """The guard must not neuter the phase. A seed pointing at a key that
    resolves to NOTHING is exactly what this phase exists to fix."""
    import asyncio

    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:dangling",
              attached="ext:points-at-nothing::deadbeef")

    report = asyncio.run(_run_barekey(execute=True))
    assert report.barekey["already_resolving"] == 0
    assert report.barekey["repaired"] == 1

    with pg_engine.begin() as conn:
        now = conn.execute(text(
            "SELECT attached_product_key FROM external_product_seeds "
            "WHERE id = 'seed:dangling'")).scalar()
    assert now == _MIRROR_KEY


def test_the_repair_records_the_previous_value(pg_engine):
    """An UPDATE that cannot be undone from its own audit trail is not
    resumable, it is just fast. Recovery from the incident needed a
    reconstructed content_key join precisely because this was missing."""
    import asyncio

    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:dangling2", attached="ext:gone::cafe1234")

    asyncio.run(_run_barekey(execute=True))

    with pg_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT observed_id, previous_value, status FROM a9_4_backfill_checkpoint "
            "WHERE phase='barekey' AND ref_id='seed:dangling2'")).mappings().first()
    assert row is not None
    assert row["previous_value"] == "ext:gone::cafe1234", "old value must be recoverable"
    assert row["observed_id"] == _MIRROR_KEY
    assert row["status"] == "done"


def test_dry_run_writes_nothing_and_still_reports_the_skip(pg_engine):
    """Dry run must classify identically to execute — the phase-1 lesson was
    that a silent divergence between the two is invisible until the DB fails to
    move."""
    import asyncio

    from sqlalchemy import text

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, product_key=_EXT_KEY, source_product_id="ilia-multi-stick",
                 source_system="catalog_enrichment_agent_v1")
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:dry", attached=_EXT_KEY)

    report = asyncio.run(_run_barekey(execute=False))
    assert report.barekey["already_resolving"] == 1
    assert report.barekey["repaired"] == 0

    with pg_engine.begin() as conn:
        still = conn.execute(text(
            "SELECT attached_product_key FROM external_product_seeds "
            "WHERE id = 'seed:dry'")).scalar()
        ck = conn.execute(text("SELECT count(*) FROM a9_4_backfill_checkpoint")).scalar()
    assert still == _EXT_KEY
    assert ck == 0, "dry run must not checkpoint"


def test_a_padded_key_is_repaired_not_skipped(pg_engine):
    """R1. The guard must compare the RAW value, because the join it protects
    does not trim either side (`_seed_route_minted.attached_product_key =
    cp.product_key`) and product_key is varchar — trailing space is significant.

    A btrim'd comparison declares a padded key "already resolving", skips the
    very row this phase exists to repair, and reports it under a value the
    column does not contain — hiding it from the reviewer who would catch it.
    """
    import asyncio

    from sqlalchemy import text

    padded = f"  {_EXT_KEY}  "
    with pg_engine.begin() as conn:
        _reset(conn)
        # The UNPADDED row exists; the seed's value is padded, so the real join
        # finds nothing and this seed IS broken.
        _product(conn, product_key=_EXT_KEY, source_product_id="ilia-multi-stick",
                 source_system="catalog_enrichment_agent_v1")
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:padded", attached=padded)

        joins = conn.execute(text(
            "SELECT count(*) FROM external_product_seeds s "
            "JOIN catalog_products cp ON cp.product_key = s.attached_product_key "
            "WHERE s.id = 'seed:padded'")).scalar()
    assert joins == 0, "precondition: the padded key must NOT join"

    report = asyncio.run(_run_barekey(execute=True))
    assert report.barekey["already_resolving"] == 0, "a padded key does not resolve"
    assert report.barekey["repaired"] == 1

    with pg_engine.begin() as conn:
        now = conn.execute(text(
            "SELECT attached_product_key FROM external_product_seeds "
            "WHERE id = 'seed:padded'")).scalar()
        prev = conn.execute(text(
            "SELECT previous_value FROM a9_4_backfill_checkpoint "
            "WHERE phase='barekey' AND ref_id='seed:padded'")).scalar()
    assert now == _MIRROR_KEY
    assert prev == padded, "the undo value must be byte-exact, not stripped"


def test_the_update_is_a_compare_and_swap(pg_engine):
    """R2. The guard is a READ. Without a CAS the UPDATE's WHERE never mentions
    the column, so any writer landing between the unbatched fetch_all and the
    write is clobbered with no re-check — a silent detach, the exact failure
    this PR exists to close.

    This drives the REAL run_barekey and injects the concurrent write from
    inside it, via a db proxy that fires once on the guard's first lookup. An
    earlier version of this test ran the CAS statement inline and therefore
    tested its own SQL rather than the code — it passed with the fix reverted.
    """
    import asyncio

    from sqlalchemy import text

    from databases import Database

    from services import seller_identity
    from scripts.backfill_seller_of_record import BackfillReport, SellerBackfill

    with pg_engine.begin() as conn:
        _reset(conn)
        _product(conn, product_key=_MIRROR_KEY, source_product_id=_EPID,
                 source_system="external_product_seeds_mirror_v1")
        _seed(conn, seed_id="seed:raced", attached="ext:stale::0000")

    WINNER = "ext:someone-else-won::ffff"

    class RacingDb:
        """Delegates to the real Database, but lands a competing write once —
        after the phase has snapshotted the row and before it issues its UPDATE."""

        def __init__(self, inner):
            self._inner = inner
            self._fired = False

        async def fetch_one(self, *a, **kw):
            result = await self._inner.fetch_one(*a, **kw)
            if not self._fired:
                self._fired = True
                await self._inner.execute(
                    "UPDATE external_product_seeds SET attached_product_key = :w "
                    "WHERE id = 'seed:raced'", {"w": WINNER})
            return result

        def __getattr__(self, name):
            return getattr(self._inner, name)

    async def race():
        db = Database(DATABASE_URL)
        await db.connect()
        try:
            bf = SellerBackfill(database=RacingDb(db), si_mod=seller_identity,
                                execute=True, batch_size=50)
            await bf.ensure_checkpoint_table()
            report = BackfillReport(mode="execute", started_at="2026-08-01T00:00:00Z",
                                    phases=["barekey"], batch_size=50)
            await bf.run_barekey(report)
            return report
        finally:
            await db.disconnect()

    asyncio.run(race())

    with pg_engine.begin() as conn:
        now = conn.execute(text(
            "SELECT attached_product_key FROM external_product_seeds "
            "WHERE id = 'seed:raced'")).scalar()
    assert now == WINNER, (
        "the CAS must make the stale write a no-op; without it the concurrent "
        "writer is clobbered and the seed is silently detached")


def test_the_checkpoint_column_is_added_by_migration(pg_engine):
    """The gate DDL deliberately omits `previous_value`, so this exercises the
    ALTER in ensure_checkpoint_table — the statement that actually runs in prod,
    where the table already exists without the column."""
    import asyncio

    from sqlalchemy import text

    asyncio.run(_run_barekey(execute=False))
    with pg_engine.begin() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'a9_4_backfill_checkpoint'")).fetchall()}
    assert "previous_value" in cols
