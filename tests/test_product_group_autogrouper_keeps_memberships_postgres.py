"""ADR-032 M11 item 3 on POSTGRES: the autogrouper's real read and writes on real tables. A listing in
another group stays there (also when it is claimed between the read and the write), the group's own
members and unclaimed listings join, and a group never loses its primary. The identity-merge Postgres
file's scratch schema is reused (throwaway database only, dropped at teardown).

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_product_group_autogrouper_keeps_memberships_postgres.py
"""
from __future__ import annotations

from services import product_group_autogrouper as autogrouper
from tests.test_identity_merge_hardening_postgres import SYN, _row, db, pytestmark  # noqa: F401 (db: the fixture)

CK = "ck_" + "d" * 32
PG = "pg_" + "d" * 32


async def _members(db):
    rows = await db.fetch_all("SELECT platform_product_id, product_group_id, is_primary FROM product_group_members"
                              " ORDER BY platform_product_id")
    return {r["platform_product_id"]: (r["product_group_id"], r["is_primary"]) for r in rows}


async def _cluster(db):
    await _row(db, "ext:retailer:kept", "merch_obs_a", CK, title="Example Serum", group="pg_curated")
    await _row(db, "ext:retailer:member", "merch_obs_b", CK, title="Example Serum", group=PG)
    await _row(db, "ext:retailer:free", "merch_obs_c", CK, title="Example Serum")
    await db.execute("UPDATE catalog_products SET pdp_lifecycle_stage = 'published'"
                     " WHERE product_key = 'ext:retailer:kept'")  # it would win the primary pick


async def test_a_listing_in_another_group_stays_there_on_postgres(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(autogrouper, "database", db)
    await _cluster(db)
    report = await autogrouper.autogroup_clusters(content_key=CK, apply=True)
    [out] = report.per_cluster
    # The pick among the free rows ties on stage and signature: the lower product_key wins.
    assert (out.member_count, out.primary_product_key, out.kept_product_keys) == (
        2, "ext:retailer:free", ["ext:retailer:kept"])
    assert await _members(db) == {
        "retailer:kept": ("pg_curated", True),
        "retailer:member": (PG, False),
        "retailer:free": (PG, True),
    }


async def test_a_listing_claimed_after_the_read_is_not_taken_and_the_primary_stays_on_postgres(db, monkeypatch):  # noqa: F811
    """The read saw `free` unclaimed and picked it as primary; another writer puts it in its own group
    before the upsert. The guarded upsert leaves it there, and the clear keeps the group's primary."""
    monkeypatch.setattr(autogrouper, "database", db)
    await _row(db, "ext:retailer:free", "merch_obs_c", CK, title="Example Serum")
    await _row(db, "ext:retailer:member", "merch_obs_b", CK, title="Example Serum", group=PG)
    await db.execute("UPDATE catalog_products SET pdp_lifecycle_stage = 'published'"
                     " WHERE product_key = 'ext:retailer:free'")
    clusters = await autogrouper._fetch_clusters(content_key=CK, merchant_id=None, limit=10)
    await db.execute("INSERT INTO product_group_members (product_group_id, merchant_id, platform,"
                     " platform_product_id, is_primary) VALUES ('pg_late', 'merch_obs_c', :p, 'retailer:free', FALSE)",
                     {"p": SYN})
    out = await autogrouper._apply_cluster(content_key=CK, rows=clusters[CK])
    assert out.primary_product_key == "ext:retailer:free"
    # pg_late's row is not touched at all (its flag stays FALSE), and PG keeps its primary.
    assert await _members(db) == {"retailer:free": ("pg_late", False), "retailer:member": (PG, True)}
