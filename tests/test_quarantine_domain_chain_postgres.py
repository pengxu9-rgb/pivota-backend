"""The two domain chains must resolve the SAME domain. Postgres only.

`catalog_products` has two chain implementations by necessity:

  * `source_quarantine.CATALOG_PRODUCT_DOMAIN_SQL` — lateral scalar subqueries,
    for the selective per-`content_key` assembler lookup;
  * `catalog_row_trust_upserter._PRODUCT_JOIN_CTES` — `DISTINCT ON` CTEs, for
    the bulk scan.

Two SHAPES are unavoidable. Two RULES are not — and a first version of this PR
shipped two rules. Review measured them resolving different domains on **6 of
11 shapes**, producing opposite quarantine verdicts in BOTH directions:

  * over-block: trust says `public`, the assembler drops the row from the
    canonical pick, and the content_key never gets an `agent_pdp_view` — a
    public product with no PDP;
  * under-block: the quarantined storefront keeps the canonical pick and
    shadows the real merchant's PDP, which is bug #1643 verbatim.

Neither showed up in the SQLite suite, because `DISTINCT ON` does not exist
there and every fixture had ONE row per table — with one seed and one store
there is no tie to break, so no ORDER BY leg is observable. This file exists to
close both gaps: real Postgres, multi-row groups, differential assertion.

Runs in the `Postgres Dialect Gate` workflow, which supplies DATABASE_URL and
asserts this module executed at least one test — so it cannot silently skip.
The shared-constant design in
`services/source_quarantine` matters more than this test does: the constants
make divergence impossible to express, and this proves the constants are wired.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

# DATABASE_URL, matching every sibling `*_postgres.py` gate file — NOT a
# bespoke env var. The "Postgres Dialect Gate" workflow auto-discovers these
# modules and supplies DATABASE_URL, then asserts each discovered module
# actually EXECUTED something. A first version of this file gated on
# PIVOTA_TEST_PG_URL, so all 16 tests skipped inside the one job designed to
# run them and the gate failed with "green while testing nothing" — which is
# precisely the failure this file exists to catch, turned on the file itself.
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _IS_PG,
        reason="needs a Postgres DATABASE_URL — production-dialect gate",
    ),
]

PG_URL = DATABASE_URL

# ISOLATED IN ITS OWN POSTGRES SCHEMA.
#
# The dialect gate runs every `*_postgres.py` module in ONE pytest invocation
# against ONE database, and five sibling gate files also use `catalog_products`.
# Dropping it in the public schema would destroy their fixtures depending on
# collection order — a cross-test failure that would look like a flake and be
# blamed on them. `search_path` is per-connection, so this is airtight without
# coordinating with any other file.
SCHEMA = """
DROP SCHEMA IF EXISTS quarantine_chain_test CASCADE;
CREATE SCHEMA quarantine_chain_test;
SET search_path TO quarantine_chain_test;
CREATE TABLE catalog_products (
  product_key text PRIMARY KEY, content_key text, merchant_id text, platform text,
  source_product_id text, source_system text, source_domain text, source_ref text);
CREATE TABLE external_product_seeds (
  id text PRIMARY KEY, external_product_id text, domain text, attached_product_key text,
  status text, updated_at timestamptz, created_at timestamptz, seed_kind text);
CREATE TABLE merchant_stores (
  store_id text PRIMARY KEY, merchant_id text, platform text, domain text,
  status text, is_primary boolean, last_sync timestamptz, created_at timestamptz);
-- merchant_id: the identity leg is merchant-scoped (#1665). Without this
-- column the leg cannot be expressed and this harness cannot see the bug.
CREATE TABLE pdp_identity_listing (
  product_id text, source_listing_ref text, merchant_id text);
-- _PRODUCT_JOIN_CTES carries more CTEs than this test exercises; they must
-- still resolve, so their tables exist here as empty stubs.
CREATE TABLE pdp_identity_override (
  id text, source_listing_ref text, action_type text, active boolean,
  updated_at timestamptz, created_at timestamptz);
CREATE TABLE catalog_source_quarantine (
  quarantine_id serial, match_type text, match_value text, state text, expires_at timestamptz);
"""

# Each shape has MULTIPLE rows per group, so every ORDER BY leg is observable.
SHAPES = {
    # (status='active') DESC must outrank last_sync — the leg the first version
    # of the lateral dropped entirely.
    "store: deleted+newer vs active+older": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", None),
        stores=[
            ("st_old", "m1", "shopify", "oldshop.example.com", "deleted", False, "2026-07-25", "2026-01-01"),
            ("st_new", "m1", "shopify", "newshop.example.com", "active", False, "2026-07-01", "2026-01-01"),
        ],
        expect="newshop.example.com",
    ),
    # is_primary sits on a DIFFERENT platform: the uniqueness constraint is per
    # merchant, not per (merchant, platform), so this is a normal shape.
    "store: primary is on another platform": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", None),
        stores=[
            ("st_a", "m1", "wix", "wixshop.example.com", "active", True, "2026-07-25", "2026-01-01"),
            ("st_b", "m1", "shopify", "shopshop.example.com", "active", False, "2026-07-01", "2026-01-01"),
        ],
        expect="shopshop.example.com",
    ),
    "store: tie on everything falls to store_id": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", None),
        stores=[
            ("st_a", "m1", "shopify", "a.example.com", "active", None, None, None),
            ("st_b", "m1", "shopify", "b.example.com", "active", None, None, None),
        ],
        expect="b.example.com",  # store_id DESC
    ),
    # Path-C minted: identity-carrying seed outranks a newer sibling.
    "minted: identity-carrying vs newer sibling": dict(
        product=("pk", "m1", "shopify", "slug", "catalog_enrichment_agent_v1", None),
        seeds=[
            ("s_id", "ext_id", "retailer-a.example.com", "pk", "active", "2026-01-01", "2026-01-01"),
            ("s_new", "ext_new", "retailer-b.example.com", "pk", "active", "2026-07-01", "2026-07-01"),
        ],
        listings=[("ext_id", "external_seed:ext_id", "m1")],
        expect="retailer-a.example.com",
    ),
    # Issue #1665: the identity leg must not be satisfied by a listing at
    # ANOTHER merchant. `source_listing_ref` is merchant_id:product_id
    # (ADR-008), so product_id alone is not unique. `ext_new` is newer and
    # carries a listing — but at m2, not this row's m1 — so it must NOT win the
    # leg. Before the fix it did, and the resolved domain flipped to
    # retailer-b, taking the trust decision and the quarantine verdict with it.
    "minted: a listing at ANOTHER merchant must not win the leg": dict(
        product=("pk", "m1", "shopify", "slug", "catalog_enrichment_agent_v1", None),
        seeds=[
            ("s_id", "ext_id", "retailer-a.example.com", "pk", "active", "2026-01-01", "2026-01-01"),
            ("s_new", "ext_new", "retailer-b.example.com", "pk", "active", "2026-07-01", "2026-07-01"),
        ],
        listings=[("ext_id", "m1:ext_id", "m1"), ("ext_new", "m2:ext_new", "m2")],
        expect="retailer-a.example.com",
    ),
    "mirror: active outranks newer-inactive": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", None),
        seeds=[
            ("s_a", "ext_1", "inactive.example.com", None, "disabled", "2026-07-25", "2026-01-01"),
            ("s_b", "ext_1", "live.example.com", None, "active", "2026-07-01", "2026-01-01"),
        ],
        expect="live.example.com",
    ),
    # created_at must decide BEFORE the id fallback, so the fixture makes the two
    # legs disagree: s_a has the newer created_at, s_b the higher id. Without
    # this opposition, dropping `created_at` from the shared constant leaves
    # both chains agreeing on the same (wrong-for-the-right-reason) answer and
    # the mutant survives — which it did.
    "mirror: created_at decides before the id fallback": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", None),
        seeds=[
            ("s_a", "ext_1", "by-created-at.example.com", None, "active", "2026-07-01", "2026-06-01"),
            ("s_b", "ext_1", "by-id.example.com", None, "active", "2026-07-01", "2026-01-01"),
        ],
        expect="by-created-at.example.com",
    ),
    "cp.source_domain wins over every fallback": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", "own.example.com"),
        seeds=[("s_a", "ext_1", "seed.example.com", None, "active", "2026-07-01", "2026-01-01")],
        stores=[("st_a", "m1", "shopify", "store.example.com", "active", True, "2026-07-01", "2026-01-01")],
        expect="own.example.com",
    ),
    "empty cp.source_domain falls through": dict(
        product=("pk", "m1", "shopify", "ext_1", "external_product_seeds_mirror_v1", ""),
        seeds=[("s_a", "ext_1", "seed.example.com", None, "active", "2026-07-01", "2026-01-01")],
        expect="seed.example.com",
    ),
}


def _ts(v):
    """asyncpg binds by TYPE — a string date raises rather than being cast."""
    if v is None:
        return None
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


async def _load(conn, shape):
    await conn.execute(
        "TRUNCATE catalog_products, external_product_seeds, merchant_stores,"
        " pdp_identity_listing, catalog_source_quarantine"
    )
    pk, mid, plat, spid, ssys, sdom = shape["product"]
    await conn.execute(
        "INSERT INTO catalog_products VALUES ($1,'ck',$2,$3,$4,$5,$6,NULL)",
        pk, mid, plat, spid, ssys, sdom,
    )
    for s in shape.get("seeds", []):
        await conn.execute(
            "INSERT INTO external_product_seeds VALUES ($1,$2,$3,$4,$5,$6,$7,'seed')",
            s[0], s[1], s[2], s[3], s[4], _ts(s[5]), _ts(s[6]),
        )
    for st in shape.get("stores", []):
        await conn.execute(
            "INSERT INTO merchant_stores VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            st[0], st[1], st[2], st[3], st[4], st[5], _ts(st[6]), _ts(st[7]),
        )
    for li in shape.get("listings", []):
        await conn.execute(
            "INSERT INTO pdp_identity_listing VALUES ($1,$2,$3)", *li)


async def _lateral_domain(conn) -> str | None:
    from services.source_quarantine import CATALOG_PRODUCT_DOMAIN_SQL

    return await conn.fetchval(
        f"SELECT {CATALOG_PRODUCT_DOMAIN_SQL} FROM catalog_products cp WHERE cp.product_key='pk'"
    )


async def _cte_domain(conn) -> str | None:
    """The upserter's chain, built from its OWN CTE text — not retyped here."""
    from services.catalog_row_trust_upserter import _PRODUCT_JOIN_CTES

    sql = (
        _PRODUCT_JOIN_CTES
        + """
        SELECT coalesce(nullif(cp.source_domain, ''), nullif(eps.domain, ''),
                        nullif(epm.domain, ''), nullif(ms.domain, ''), '') AS d
        FROM catalog_products cp
        LEFT JOIN external_seed_one eps
          ON cp.source_system = 'external_product_seeds_mirror_v1'
         AND eps.external_product_id = cp.source_product_id
        LEFT JOIN minted_seed_one epm
          ON cp.source_system = 'catalog_enrichment_agent_v1'
         AND epm.attached_product_key = cp.product_key
        LEFT JOIN merchant_store_one ms
          ON ms.merchant_id = cp.merchant_id AND ms.platform = cp.platform
        WHERE cp.product_key = 'pk'
        """
    )
    return await conn.fetchval(sql)


@pytest.mark.parametrize("label", sorted(SHAPES))
async def test_both_chains_resolve_the_same_domain(label):
    """The differential. This is the assertion that would have caught #1644's
    first version, where 6 of 11 shapes disagreed."""
    import asyncpg

    conn = await asyncpg.connect(PG_URL)
    try:
        await conn.execute(SCHEMA)
        await conn.execute("SET search_path TO quarantine_chain_test")
        shape = SHAPES[label]
        await _load(conn, shape)
        lateral = await _lateral_domain(conn)
        cte = await _cte_domain(conn)
        assert lateral == cte, (
            f"[{label}] the two chains resolve different domains — "
            f"lateral={lateral!r} cte={cte!r}. A quarantine on either value now "
            "produces opposite verdicts on the two doors."
        )
        assert lateral == shape["expect"], (
            f"[{label}] expected {shape['expect']!r}, both chains gave {lateral!r}"
        )
    finally:
        await conn.close()


@pytest.mark.parametrize("label", sorted(SHAPES))
async def test_the_anti_join_blocks_exactly_the_resolved_domain(label):
    """End-to-end: quarantine the resolved domain and the row must disappear;
    quarantine anything else and it must stay. A chain that resolves correctly
    but is wired into the predicate wrongly would pass the test above."""
    import asyncpg

    from services.agent_pdp_view_assembler import _SOURCE_QUARANTINE_ANTI_JOIN

    conn = await asyncpg.connect(PG_URL)
    try:
        await conn.execute(SCHEMA)
        await conn.execute("SET search_path TO quarantine_chain_test")
        shape = SHAPES[label]
        await _load(conn, shape)
        sql = (
            "SELECT cp.product_key FROM catalog_products cp "
            f"WHERE cp.content_key = 'ck' {_SOURCE_QUARANTINE_ANTI_JOIN}"
        )
        assert len(await conn.fetch(sql)) == 1, f"[{label}] row blocked with no quarantine"

        await conn.execute(
            "INSERT INTO catalog_source_quarantine (match_type, match_value, state)"
            " VALUES ('domain', $1, 'active')", shape["expect"],
        )
        assert await conn.fetch(sql) == [], (
            f"[{label}] quarantining the resolved domain {shape['expect']!r} did not block the row"
        )

        await conn.execute("UPDATE catalog_source_quarantine SET state = 'revoked'")
        assert len(await conn.fetch(sql)) == 1, f"[{label}] revoke did not release the row"
    finally:
        await conn.close()
