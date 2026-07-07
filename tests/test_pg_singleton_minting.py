"""ADR-009 ratified decision 1 (no-fallback) — singleton pg minting, unit tests.

Proves, without a database:
  - `make_singleton_product_group_id` is deterministic (same content_key → same
    pg, forever), collision-free across distinct content_keys, and BYTE-IDENTICAL
    to `derive_product_group_id` — the singleton namespace IS the autogroup
    namespace, so a singleton that later gains a second listing grows into a real
    multi-member group under the SAME pg (no re-key, no fork);
  - it REFUSES to mint from nothing: None / "" / whitespace / non-`ck_` input
    raises ValueError (content_key is a derivation INPUT, never a runtime
    alternative — store-less rows stay pg-NULL, handled honestly downstream);
  - `ensure_singleton_group_membership` writes exactly one INSERT INTO
    product_group_members with ON CONFLICT DO NOTHING (never overwrites a
    real/curated group — no auto-merge), stamps is_primary TRUE, uses the
    provided `db` handle when given and the module-level `database` otherwise,
    and for a NULL/blank content_key returns None with ZERO writes + the
    observable `pg_singleton.skip.no_content_key` log;
  - all 5 ingestion writers are wired to the helper (source-level call-site
    assertions), and the brand-authored writer actually FIRES the membership
    INSERT during an ingest.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import product_group_autogrouper as autogrouper  # noqa: E402
from services.product_group_autogrouper import (  # noqa: E402
    derive_product_group_id,
    ensure_singleton_group_membership,
    make_singleton_product_group_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# make_singleton_product_group_id — deterministic derivation
# ---------------------------------------------------------------------------


def test_singleton_pg_is_deterministic_across_repeated_calls():
    """Same content_key → same pg, every time. Re-ingest, backfill re-run and
    autogroup must all converge on ONE group id — otherwise groups would fork."""
    ck = "ck_32de31827aded89c8d0339895b6a2786"
    first = make_singleton_product_group_id(ck)
    for _ in range(5):
        assert make_singleton_product_group_id(ck) == first
    assert first == "pg_32de31827aded89c8d0339895b6a2786"


def test_singleton_pg_distinct_content_keys_yield_distinct_pgs():
    """NON-MERGE property: two DIFFERENT content_keys (distinct physical
    products) always map to DIFFERENT pgs — a singleton can never merge
    distinct products (mis-merge is worse than fragmentation)."""
    cks = [
        "ck_a363cbe4bc721b724168df4282713e6c",
        "ck_32de31827aded89c8d0339895b6a2786",
        "ck_00000000000000000000000000000001",
        "ck_00000000000000000000000000000002",
    ]
    pgs = {make_singleton_product_group_id(ck) for ck in cks}
    assert len(pgs) == len(cks)


def test_singleton_pg_byte_identical_to_autogroup_derivation():
    """Namespace convergence: the singleton pg is EXACTLY what the multi-member
    autogrouper (`derive_product_group_id`) would mint for the same content_key.
    A singleton is just an autogroup of size 1 — when a second listing shows up,
    the cluster lands under the SAME pg with no re-key."""
    for ck in (
        "ck_32de31827aded89c8d0339895b6a2786",
        "ck_a363cbe4bc721b724168df4282713e6c",
    ):
        assert make_singleton_product_group_id(ck) == derive_product_group_id(ck)


def test_singleton_pg_strips_whitespace_then_derives():
    """A padded-but-valid content_key derives from its stripped form."""
    assert (
        make_singleton_product_group_id("  ck_32de31827aded89c8d0339895b6a2786  ")
        == "pg_32de31827aded89c8d0339895b6a2786"
    )


def test_singleton_pg_refuses_to_mint_from_nothing():
    """ADR-009 decision 1: never mint a pg from nothing. Absent / blank /
    malformed content_key is a ValueError, not a silent synthetic group."""
    for bad in (None, "", "   ", "\t\n"):
        with pytest.raises(ValueError):
            make_singleton_product_group_id(bad)  # type: ignore[arg-type]
    for not_ck in ("pg_deadbeef", "sig_deadbeef", "not_a_content_key", "CK_UPPER"):
        with pytest.raises(ValueError):
            make_singleton_product_group_id(not_ck)


# ---------------------------------------------------------------------------
# ensure_singleton_group_membership — the write
# ---------------------------------------------------------------------------


class _FakeDB:
    """Records (sql, values) for every execute; no real I/O."""

    def __init__(self) -> None:
        self.executed: List[Tuple[str, Dict[str, Any]]] = []

    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.executed.append((" ".join(str(query).split()), dict(values or {})))
        return None


async def test_ensure_membership_mints_and_returns_pg_via_insert_on_conflict():
    db = _FakeDB()
    ck = "ck_32de31827aded89c8d0339895b6a2786"
    pg = await ensure_singleton_group_membership(
        merchant_id="merch_a",
        platform="shopify",
        source_product_id="p-1",
        content_key=ck,
        db=db,
    )
    assert pg == "pg_32de31827aded89c8d0339895b6a2786"
    assert len(db.executed) == 1
    sql, values = db.executed[0]
    assert "INSERT INTO product_group_members" in sql
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    # never an UPDATE / DO UPDATE — an existing (real or curated) membership
    # must NEVER be overwritten by a singleton (no auto-merge).
    assert "DO UPDATE" not in sql
    assert not sql.lstrip().upper().startswith("UPDATE")
    assert values == {
        "product_group_id": pg,
        "merchant_id": "merch_a",
        "platform": "shopify",
        "platform_product_id": "p-1",
    }


async def test_ensure_membership_stamps_is_primary_true():
    """A singleton's only member IS the primary — the SQL hard-codes TRUE."""
    db = _FakeDB()
    await ensure_singleton_group_membership(
        merchant_id="m",
        platform="shopify",
        source_product_id="p",
        content_key="ck_a363cbe4bc721b724168df4282713e6c",
        db=db,
    )
    sql, _ = db.executed[0]
    # column list carries is_primary and the VALUES tuple carries literal TRUE
    assert "is_primary" in sql
    assert re.search(r"VALUES\s*\([^)]*\bTRUE\b", sql, flags=re.IGNORECASE), sql


async def test_ensure_membership_null_or_blank_content_key_no_write(caplog):
    """Honest absence: no content_key → no pg, ZERO writes, observable log.
    Never a force-mint from nothing."""
    for blank in (None, "", "   "):
        db = _FakeDB()
        with caplog.at_level(logging.INFO, logger=autogrouper.__name__):
            caplog.clear()
            result = await ensure_singleton_group_membership(
                merchant_id="merch_a",
                platform="shopify",
                source_product_id="p-1",
                content_key=blank,
                db=db,
            )
        assert result is None
        assert db.executed == []
        assert any(
            r.message == "pg_singleton.skip.no_content_key" for r in caplog.records
        ), f"missing observable skip log for content_key={blank!r}"


async def test_ensure_membership_uses_provided_db_handle(monkeypatch):
    """When a `db` handle is passed (e.g. the backfill's transaction), the write
    goes THROUGH IT — the module-level database is untouched."""
    module_db = _FakeDB()
    handle_db = _FakeDB()
    monkeypatch.setattr(autogrouper, "database", module_db)
    pg = await ensure_singleton_group_membership(
        merchant_id="m",
        platform="shopify",
        source_product_id="p",
        content_key="ck_32de31827aded89c8d0339895b6a2786",
        db=handle_db,
    )
    assert pg is not None
    assert len(handle_db.executed) == 1
    assert module_db.executed == []


async def test_ensure_membership_defaults_to_module_database(monkeypatch):
    """No handle → the module-level `database` singleton is used (ingestion path)."""
    module_db = _FakeDB()
    monkeypatch.setattr(autogrouper, "database", module_db)
    pg = await ensure_singleton_group_membership(
        merchant_id="m",
        platform="shopify",
        source_product_id="p",
        content_key="ck_32de31827aded89c8d0339895b6a2786",
    )
    assert pg == "pg_32de31827aded89c8d0339895b6a2786"
    assert len(module_db.executed) == 1
    assert "INSERT INTO product_group_members" in module_db.executed[0][0]


# ---------------------------------------------------------------------------
# Wiring — every ingestion writer stamps the singleton pg
# ---------------------------------------------------------------------------

_WRITERS = (
    "services/catalog_sync_service.py",
    "services/audit_index_intake.py",
    "services/brand_authored_intake.py",
    "services/catalog_enrichment_agent/apply.py",
    "scripts/mirror_external_seeds_to_catalog_products.py",
)


@pytest.mark.parametrize("rel_path", _WRITERS)
def test_writer_references_ensure_singleton_group_membership(rel_path: str):
    """Call-site pin: each of the 5 catalog writers imports AND awaits the
    singleton minting helper. If a refactor drops one, products from that path
    would land pg-NULL and fall out of offers.resolve canonical identity —
    exactly the regression this guards."""
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    assert "ensure_singleton_group_membership" in source, (
        f"{rel_path} lost its ADR-009 singleton pg minting call site"
    )
    # It must be an awaited call, not just a stray import.
    assert re.search(r"await\s+(?:\w+\.)?(?:_ensure_singleton_pg|ensure_singleton_group_membership)\s*\(", source), (
        f"{rel_path} references the helper but never awaits it"
    )


async def test_brand_authored_ingest_fires_singleton_membership_insert(monkeypatch):
    """End-to-end (fake db): `upsert_brand_authored_catalog_row` actually FIRES
    the product_group_members INSERT with the deterministic singleton pg derived
    from the row's own content_key."""
    import db.database as dbmod
    from services import brand_authored_intake as bai

    executed: List[Tuple[Any, Dict[str, Any]]] = []

    async def fake_execute(query, values=None):
        executed.append((query, dict(values or {})))
        return None

    # Both the catalog upsert (db.database.database) and the minting helper's
    # module-level database resolve to the same singleton instance — patch its
    # execute method once.
    monkeypatch.setattr(dbmod.database, "execute", fake_execute)
    assert autogrouper.database is dbmod.database

    fields = {
        "merchant_id": "merch_ba",
        "platform": bai.PLATFORM_BRAND_AUTHORED,
        "source_product_id": "ba-serum-abc123",
        "product_key": "prod::merch_ba::brand_authored::ba-serum-abc123",
        "title": "Test Serum",
        "brand": "TestBrand",
        "content_key": "ck_a363cbe4bc721b724168df4282713e6c",
        "description": None,
        "product_type": None,
        "category": None,
        "image_url": None,
        "tags": None,
        "pdp_scope": "unverified",
    }
    pk = await bai.upsert_brand_authored_catalog_row(fields)
    assert pk == fields["product_key"]

    member_inserts = [
        (q, v) for q, v in executed if "INSERT INTO product_group_members" in " ".join(str(q).split())
    ]
    assert len(member_inserts) == 1, "the ingest must stamp exactly one singleton membership"
    _, values = member_inserts[0]
    assert values["product_group_id"] == "pg_a363cbe4bc721b724168df4282713e6c"
    assert values["merchant_id"] == "merch_ba"
    assert values["platform"] == bai.PLATFORM_BRAND_AUTHORED
    assert values["platform_product_id"] == "ba-serum-abc123"


async def test_brand_authored_ingest_null_content_key_writes_no_membership(monkeypatch):
    """Honest-absent through a real writer: a row without content_key ingests
    fine but gets NO membership row (stays pg-NULL)."""
    import db.database as dbmod
    from services import brand_authored_intake as bai

    executed: List[Tuple[Any, Dict[str, Any]]] = []

    async def fake_execute(query, values=None):
        executed.append((query, dict(values or {})))
        return None

    monkeypatch.setattr(dbmod.database, "execute", fake_execute)

    fields = {
        "merchant_id": "merch_ba",
        "platform": bai.PLATFORM_BRAND_AUTHORED,
        "source_product_id": "ba-thin-xyz",
        "product_key": "prod::merch_ba::brand_authored::ba-thin-xyz",
        "title": "Thin Row",
        "brand": None,
        "content_key": None,
        "description": None,
        "product_type": None,
        "category": None,
        "image_url": None,
        "tags": None,
        "pdp_scope": "unverified",
    }
    pk = await bai.upsert_brand_authored_catalog_row(fields)
    assert pk == fields["product_key"]
    assert not any(
        "INSERT INTO product_group_members" in " ".join(str(q).split()) for q, _ in executed
    ), "NULL content_key must never mint a membership row"
