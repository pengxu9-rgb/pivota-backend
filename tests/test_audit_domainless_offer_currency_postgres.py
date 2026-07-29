"""Real-Postgres gate for the domain-less offer currency audit + backfill.

The scan and backfill are Postgres-only on four counts — ``= ANY(:sources)``
array binds, ``offer_payload->>'domain'`` JSONB extraction, ``LEFT JOIN
LATERAL`` with ``array_agg(DISTINCT ...)``, and ``jsonb_to_recordset`` — none of
which the SQLite suite can even parse. Per the standing lesson (#1588/#1593:
green SQLite is not evidence a statement PREPAREs on Postgres), this file
EXECUTES both statements, seeded with real provenance fixtures so the derivation
precedence and the fill-only guard are proven on the engine that runs in prod.

Also executes the dual-write's offer upsert (``INSERT ... ON CONFLICT ... CAST(
:offer_payload AS jsonb)``) end-to-end, asserting the new ``source_domain``
stamp lands and stays fill-only on conflict.

Discovered automatically by the ``tests/test_*_postgres.py`` CI job:

    createdb pivota_dialect_check
    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        pytest tests/test_audit_domainless_offer_currency_postgres.py

Never point this at prod: it INSERTs and UPDATEs fixture rows (all keyed under
the ``dla-gate-`` prefix, deleted in teardown).
"""

from __future__ import annotations

import json
import os

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason=(
        "needs a Postgres DATABASE_URL — this is the production-dialect gate; "
        "see the module docstring for the one-line setup"
    ),
)

# external_product_seeds is not on the shared MetaData, and the gate workflow
# runs every tests/test_*_postgres.py file against ONE database — whichever file
# CREATEs the table first wins and the others' CREATE silently no-ops. So, per
# the hard-won convention in test_offer_currency_backfill_postgres.py: CREATE
# minimal, then ADD COLUMN IF NOT EXISTS for the UNION of every gate file's
# columns (ours: canonical_url, destination_url, attached_product_key on top of
# the existing union). Additive-safe in every order because all gates insert
# with explicit column lists.
_SEED_COLUMNS = (
    # this gate's
    ("id", "text"),
    ("domain", "text"),
    ("canonical_url", "text"),
    ("destination_url", "text"),
    ("attached_product_key", "text"),
    ("updated_at", "timestamp"),
    # the sibling gates' (test_offer_currency_backfill / test_citation_read_surfaces
    # / test_pdp_content_depth)
    ("external_product_id", "text"),
    ("status", "text"),
    ("merchant_id", "text"),
    ("source", "text"),
    ("product_key", "text"),
    ("source_product_id", "text"),
)

_LIGHTWEIGHT_DDL = "CREATE TABLE IF NOT EXISTS external_product_seeds (id text);\n" + "\n".join(
    f"ALTER TABLE external_product_seeds ADD COLUMN IF NOT EXISTS {name} {typ};"
    for name, typ in _SEED_COLUMNS
)

_PREFIX = "dla-gate-"
_SOURCES_KEY = "external_product_seeds_mirror_v1"


@pytest.fixture(scope="module")
def pg_schema():
    import db.catalog  # noqa: F401  (registers catalog_offers/products on the MetaData)
    from sqlalchemy import create_engine, text

    from db.database import metadata

    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine, checkfirst=True)
    with engine.begin() as conn:
        for statement in filter(None, (s.strip() for s in _LIGHTWEIGHT_DDL.split(";"))):
            conn.execute(text(statement))
    yield engine
    _cleanup(engine)
    engine.dispose()


def _cleanup(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM catalog_offers WHERE offer_id LIKE :p"),
                     {"p": f"{_PREFIX}%"})
        conn.execute(text("DELETE FROM catalog_products WHERE product_key LIKE :p"),
                     {"p": f"{_PREFIX}%"})
        conn.execute(text("DELETE FROM external_product_seeds WHERE id LIKE :p"),
                     {"p": f"{_PREFIX}%"})


def _seed_fixtures(engine):
    """Five offers exercising every derivation rule + the not-scanned case."""
    from sqlalchemy import text

    _cleanup(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO external_product_seeds (id, domain, canonical_url, attached_product_key) VALUES "
            "(:a, 'oiad.com', NULL, NULL),"
            "(:b, NULL, 'https://www.viaseed.example/p/1', NULL),"
            "(:c1, 'sib-a.example', NULL, :att_pk),"
            "(:c2, 'sib-b.example', NULL, :att_pk)"),
            {"a": f"{_PREFIX}seed-a", "b": f"{_PREFIX}seed-b",
             "c1": f"{_PREFIX}seed-c1", "c2": f"{_PREFIX}seed-c2",
             "att_pk": f"{_PREFIX}pk-ambiguous"})
        conn.execute(text(
            "INSERT INTO catalog_products (product_key, merchant_id, platform,"
            " source_product_id, title, source_system, source_ref) VALUES "
            "(:pk, 'external_seed_gate', 'external_seed', :spid, 'gate fixture', :src, :ref)"),
            {"pk": f"{_PREFIX}pk-via-product", "spid": f"{_PREFIX}spid-b",
             "src": _SOURCES_KEY, "ref": f"{_PREFIX}seed-b"})

        def offer(offer_id, product_key, source_ref=None, payload=None,
                  source_domain=None, suppressed=False):
            conn.execute(text(
                "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id,"
                " currency, list_price, source_system, source_ref, source_domain,"
                " offer_payload, suppressed_at) VALUES "
                "(:o, :sk, :pk, 'external_seed_gate', 'USD', 400000.0, :ss, :ref,"
                " :dom, CAST(:pl AS jsonb), CASE WHEN CAST(:sup AS boolean) THEN NOW() ELSE NULL END)"),
                {"o": offer_id, "sk": f"{offer_id}::sku", "pk": product_key,
                 "ss": _SOURCES_KEY, "ref": source_ref, "dom": source_domain,
                 "pl": json.dumps(payload) if payload is not None else None,
                 "sup": suppressed})

        # rule 1: offer_seed via catalog_offers.source_ref (suppressed — in scope)
        offer(f"{_PREFIX}offer-own-seed", f"{_PREFIX}pk-own-seed",
              source_ref=f"{_PREFIX}seed-a", suppressed=True)
        # rule 2: offer_payload snapshot only
        offer(f"{_PREFIX}offer-payload", f"{_PREFIX}pk-payload",
              payload={"domain": "snapshot.example"})
        # rule 3: product_seed via catalog_products.source_ref (URL host, not domain col)
        offer(f"{_PREFIX}offer-via-product", f"{_PREFIX}pk-via-product")
        # ambiguous: two attached seeds, two domains
        offer(f"{_PREFIX}offer-ambiguous", f"{_PREFIX}pk-ambiguous")
        # already has source_domain -> must NOT appear in the scan
        offer(f"{_PREFIX}offer-has-domain", f"{_PREFIX}pk-has-domain",
              source_domain="already.example")


def _mod():
    import importlib

    return importlib.import_module("scripts.audit_domainless_offer_currency")


async def _fetch_scan(mod):
    from db.database import database

    await database.connect()
    try:
        rows = [dict(r) for r in await database.fetch_all(
            mod._SCAN_SQL, {"sources": list(mod._SEED_SOURCES)})]
        # the out-of-scope note query must PREPARE too (ANY + IS NULL branch)
        await database.fetch_all(
            mod._OUT_OF_SCOPE_SQL, {"sources": list(mod._SEED_SOURCES)})
    finally:
        await database.disconnect()
    return {r["offer_id"]: r for r in rows if str(r["offer_id"]).startswith(_PREFIX)}


@pytest.mark.asyncio
async def test_scan_executes_and_derives_every_rule(pg_schema):
    mod = _mod()
    _seed_fixtures(pg_schema)
    rows = await _fetch_scan(mod)

    assert set(rows) == {
        f"{_PREFIX}offer-own-seed", f"{_PREFIX}offer-payload",
        f"{_PREFIX}offer-via-product", f"{_PREFIX}offer-ambiguous",
    }, "a row already carrying source_domain must be invisible to the scan"

    derived = {oid: mod.derive_offer_domain(row) for oid, row in rows.items()}
    assert derived[f"{_PREFIX}offer-own-seed"] == ("oiad.com", "offer_seed")
    assert derived[f"{_PREFIX}offer-payload"] == ("snapshot.example", "offer_payload")
    assert derived[f"{_PREFIX}offer-via-product"] == ("viaseed.example", "product_seed")
    assert derived[f"{_PREFIX}offer-ambiguous"] == (None, "attached_seed_ambiguous")

    # the suppressed row is IN scope and says so
    assert rows[f"{_PREFIX}offer-own-seed"]["is_suppressed"] is True


@pytest.mark.asyncio
async def test_backfill_writes_returns_and_stays_fill_only(pg_schema):
    from sqlalchemy import text

    from db.database import database

    mod = _mod()
    _seed_fixtures(pg_schema)

    mapping = [
        {"offer_id": f"{_PREFIX}offer-own-seed", "source_domain": "oiad.com"},
        # adversarial: mapping names a row that ALREADY has a domain — the
        # fill-only guard must make it a no-op, not an overwrite
        {"offer_id": f"{_PREFIX}offer-has-domain", "source_domain": "attacker.example"},
    ]
    params = {"mappings": json.dumps(mapping, sort_keys=True),
              "sources": list(mod._SEED_SOURCES)}

    await database.connect()
    try:
        written = await database.fetch_all(mod._BACKFILL_SQL, params)
        again = await database.fetch_all(mod._BACKFILL_SQL, params)
    finally:
        await database.disconnect()

    assert [r["offer_id"] for r in written] == [f"{_PREFIX}offer-own-seed"], (
        "RETURNING must yield exactly the rows written — it is the operator's "
        "only evidence (database.execute() returns None for UPDATEs)"
    )
    assert again == [], "second pass must be a no-op (idempotent fill-only)"

    with pg_schema.connect() as conn:
        got = dict(conn.execute(text(
            "SELECT offer_id, source_domain FROM catalog_offers "
            "WHERE offer_id LIKE :p AND source_domain IS NOT NULL"),
            {"p": f"{_PREFIX}%"}).fetchall())
    assert got[f"{_PREFIX}offer-own-seed"] == "oiad.com"
    assert got[f"{_PREFIX}offer-has-domain"] == "already.example", (
        "fill-only guard breached: an existing source_domain was overwritten"
    )

    # and the backfilled row has left the domain-less cohort for good
    rows = await _fetch_scan(mod)
    assert f"{_PREFIX}offer-own-seed" not in rows


@pytest.mark.asyncio
async def test_dual_write_upsert_stamps_source_domain_fill_only(pg_schema):
    """The go-forward fix: the seed→offer projection must PREPARE on Postgres
    and stamp a normalized source_domain — without ever clobbering one that is
    already present (ingest-written or audit-backfilled)."""
    from sqlalchemy import text

    from db.database import database
    from services.external_offer_dual_write import (
        derive_mirror_offer_id,
        upsert_catalog_offer_from_seed_row,
    )

    _seed_fixtures(pg_schema)
    product_key = f"{_PREFIX}pk-dualwrite"
    offer_id = derive_mirror_offer_id(product_key)
    seed_row = {
        "id": f"{_PREFIX}seed-dw",
        "domain": "https://www.Oiad.com/",   # normalization exercised
        "canonical_url": None,
        "destination_url": None,
        "price_amount": 12.5,
        "price_currency": "USD",
        "availability": "in_stock",
        "market": "US",
    }

    await database.connect()
    try:
        await upsert_catalog_offer_from_seed_row(
            product_key, seed_row, merchant_id="external_seed_gate")
        # conflict path: seed now claims a DIFFERENT domain; fill-only keeps the first
        await upsert_catalog_offer_from_seed_row(
            product_key, {**seed_row, "domain": "changed.example"},
            merchant_id="external_seed_gate")
    finally:
        await database.disconnect()

    try:
        with pg_schema.connect() as conn:
            row = conn.execute(text(
                "SELECT source_domain, source_ref FROM catalog_offers WHERE offer_id = :o"),
                {"o": offer_id}).fetchone()
        assert row is not None, "dual-write INSERT did not land"
        assert row[0] == "oiad.com", "source_domain must be the normalized bare host"
        assert row[1] == f"{_PREFIX}seed-dw"
    finally:
        with pg_schema.begin() as conn:
            conn.execute(text("DELETE FROM catalog_offers WHERE offer_id = :o"),
                         {"o": offer_id})
