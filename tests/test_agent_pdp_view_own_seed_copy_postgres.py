"""fetch_own_seed_copy_for_keys and fetch_shared_section_lines against REAL Postgres.

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs
`tests/test_*_postgres.py`).

The query is on the served path (build_agent_pdp_view_row -> compose_brand_section_description),
and everything that matters about it is Postgres semantics no fake can check: DISTINCT ON with its
ORDER BY picking the NEWEST active seed per product, `= ANY(:keys)` binding a Python list through
`databases`, the `->` / `->>` reads of the origin (top level, else the snapshot's) and of the
reviewed-rollback record, and jsonb coming back for the sections; and for the storefront repetition
check, unnest over a text[] bind, jsonb_array_elements over a column that is not always an array,
the edition cut done by regexp_replace, and count(DISTINCT) of other products.

Runs in its own scratch schema, alone on the search_path, with exactly the columns the query reads:

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \
        .venv/bin/python -m pytest tests/test_agent_pdp_view_own_seed_copy_postgres.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"apv_own_seed_copy_test_{os.getpid()}"

SECTIONS = [{"heading": "Details", "body": "The brand's own copy.", "source_kind": "details_summary"}]


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _async_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture
async def scoped():
    import databases

    _assert_throwaway_database()
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    db = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await db.connect()
    try:
        await db.execute(
            """
            CREATE TABLE external_product_seeds (
              id text PRIMARY KEY,
              attached_product_key text,
              status text NOT NULL,
              domain text,
              canonical_url text,
              title text,
              seed_data jsonb,
              updated_at timestamptz NOT NULL
            )
            """
        )
        yield db
    finally:
        await db.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def _seed(db, seed_id, key, *, status="active", updated="2026-09-01", seed_data=None, domain="brand.example",
                title="Product", url=None):
    await db.execute(
        "INSERT INTO external_product_seeds (id, attached_product_key, status, domain, canonical_url, title, "
        "seed_data, updated_at) "
        "VALUES (:id, :key, :status, :domain, :url, :title, CAST(:seed_data AS jsonb), :updated)",
        {"id": seed_id, "key": key, "status": status, "domain": domain, "title": title,
         "url": url or f"https://{domain}/products/{seed_id}", "seed_data": json.dumps(seed_data or {}),
         "updated": datetime.fromisoformat(updated).replace(tzinfo=timezone.utc)},
    )


async def test_each_product_gets_its_own_newest_active_seed(scoped):
    from services.agent_pdp_view_assembler import fetch_own_seed_copy_for_keys

    await _seed(scoped, "eps_old", "pk_a", updated="2026-09-01",
                seed_data={"seed_description_origin": "pdp_product_description"})
    await _seed(scoped, "eps_new", "pk_a", updated="2026-09-20",
                seed_data={"seed_description_origin": "pdp_variant_description", "pdp_details_sections": SECTIONS})
    # newer still, but inactive: never the row's seed
    await _seed(scoped, "eps_inactive", "pk_a", status="inactive", updated="2026-09-28",
                seed_data={"seed_description_origin": "aurora_product_intel_kb_reviewed_rollback"})
    await _seed(scoped, "eps_b", "pk_b", seed_data={"seed_description_origin": "pdp_product_description"})
    await _seed(scoped, "eps_other", "pk_not_asked", seed_data={"seed_description_origin": "pdp_product_description"})

    got = await fetch_own_seed_copy_for_keys(["pk_a", "pk_b", "pk_missing"], db=scoped)

    assert set(got) == {"pk_a", "pk_b"}
    assert got["pk_a"]["description_origin"] == "pdp_variant_description"
    sections = got["pk_a"]["details_sections"]
    assert (json.loads(sections) if isinstance(sections, str) else sections) == SECTIONS
    assert got["pk_b"]["description_origin"] == "pdp_product_description"
    assert got["pk_b"]["details_sections"] is None


async def test_the_origin_falls_back_to_the_snapshot_then_to_empty(scoped):
    from services.agent_pdp_view_assembler import fetch_own_seed_copy_for_keys

    await _seed(scoped, "eps_snap", "pk_snap",
                seed_data={"snapshot": {"seed_description_origin": "aurora_product_intel_kb_reviewed_rollback"}})
    await _seed(scoped, "eps_none", "pk_none", seed_data={"description": "Tagline."})

    got = await fetch_own_seed_copy_for_keys(["pk_snap", "pk_none"], db=scoped)

    assert got["pk_snap"]["description_origin"] == "aurora_product_intel_kb_reviewed_rollback"
    assert got["pk_none"]["description_origin"] == ""


async def test_the_reviewed_rollback_record_is_read(scoped):
    from services.agent_pdp_view_assembler import fetch_own_seed_copy_for_keys

    await _seed(scoped, "eps_rb", "pk_rb", seed_data={
        "seed_description_origin": "pdp_variant_description",
        "snapshot_quarantine": {"pivota_description_rollback_v1": {"reason": "rollback_catalog_backfill_low_quality"}},
    })
    await _seed(scoped, "eps_q", "pk_q", seed_data={"snapshot_quarantine": {"pdp_review_summary": {}}})
    await _seed(scoped, "eps_plain", "pk_plain", seed_data={})

    got = await fetch_own_seed_copy_for_keys(["pk_rb", "pk_q", "pk_plain"], db=scoped)

    assert got["pk_rb"]["description_reviewed"] is True
    assert got["pk_q"]["description_reviewed"] is False
    assert got["pk_plain"]["description_reviewed"] is False


async def test_no_keys_reads_nothing(scoped):
    from services.agent_pdp_view_assembler import fetch_own_seed_copy_for_keys

    assert await fetch_own_seed_copy_for_keys([], db=scoped) == {}


async def test_an_updated_at_tie_is_broken_by_id(scoped):
    from services.agent_pdp_view_assembler import fetch_own_seed_copy_for_keys

    await _seed(scoped, "eps_a", "pk_tie", updated="2026-09-20", seed_data={"seed_description_origin": "a"})
    await _seed(scoped, "eps_b", "pk_tie", updated="2026-09-20", seed_data={"seed_description_origin": "b"})
    for _ in range(3):
        got = await fetch_own_seed_copy_for_keys(["pk_tie"], db=scoped)
        assert got["pk_tie"]["description_origin"] == "b"
    assert got["pk_tie"]["domain"] == "brand.example"
    assert got["pk_tie"]["canonical_url"] == "https://brand.example/products/eps_b"


STORE_LINE = "Fenty Beauty is 100% cruelty free."
OWN_LINE = "The buildable cream-to-powder formula is weightless and easy to blend."


def _secs(*bodies):
    return {"pdp_details_sections": [{"heading": "Details", "body": b} for b in bodies]}


async def test_a_line_on_two_other_products_of_the_storefront_is_shared(scoped):
    from services.agent_pdp_view_assembler import fetch_shared_section_lines

    # the product itself and its own shades: never "other products"
    # the row itself -- its seed title differs from the catalog one, so only its key excludes it
    await _seed(scoped, "eps_own", "pk_own", title="Match Stix Contour Skinstick", seed_data=_secs(STORE_LINE, OWN_LINE))
    await _seed(scoped, "eps_shade", "pk_shade", title="Match Stix — Truffle", seed_data=_secs(STORE_LINE, OWN_LINE))
    await _seed(scoped, "eps_shade2", None, title="Match Stix – Espresso", seed_data=_secs(OWN_LINE))
    # ONE other product carries the product's own line: its shades above must not make that two
    await _seed(scoped, "eps_set", "pk_set", title="Match Stix Set", seed_data=_secs(OWN_LINE))
    # two other products carry the store line; whitespace differs in one of them
    await _seed(scoped, "eps_p1", "pk_p1", title="Gloss Bomb — Fenty Glow", seed_data=_secs(f"Shiny.\n{STORE_LINE}"))
    await _seed(scoped, "eps_p2", "pk_p2", title="Pro Filt'r — 110",
                seed_data=_secs("Fenty  Beauty is 100%\ncruelty free."))
    # neither another storefront, nor an inactive seed, nor a non-array value counts
    await _seed(scoped, "eps_x", "pk_x", domain="other.example", title="X", seed_data=_secs(STORE_LINE, OWN_LINE))
    await _seed(scoped, "eps_dead", "pk_dead", status="inactive", title="Dead", seed_data=_secs(OWN_LINE))
    await _seed(scoped, "eps_str", "pk_str", title="Str", seed_data={"pdp_details_sections": "Details"})

    shared = await fetch_shared_section_lines(
        "brand.example", [STORE_LINE, OWN_LINE], exclude_product_keys=["pk_own"],
        own_base_title="match stix", db=scoped)

    assert shared == {STORE_LINE}


async def test_one_other_product_is_not_enough(scoped):
    from services.agent_pdp_view_assembler import fetch_shared_section_lines

    await _seed(scoped, "eps_p1", "pk_p1", title="Gloss Bomb", seed_data=_secs(STORE_LINE))
    await _seed(scoped, "eps_p1b", "pk_p1b", title="Gloss Bomb — Mini", seed_data=_secs(STORE_LINE))  # same product
    shared = await fetch_shared_section_lines(
        "brand.example", [STORE_LINE], exclude_product_keys=["pk_own"], own_base_title="match stix", db=scoped)
    assert shared == set()


async def test_no_lines_reads_nothing_and_no_domain_refuses(scoped):
    from services.agent_pdp_view_assembler import fetch_shared_section_lines

    assert await fetch_shared_section_lines("brand.example", [], exclude_product_keys=[], own_base_title="", db=scoped) == set()
    with pytest.raises(ValueError):
        await fetch_shared_section_lines("", [STORE_LINE], exclude_product_keys=[], own_base_title="", db=scoped)
