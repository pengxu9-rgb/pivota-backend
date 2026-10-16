"""M3 (cross-merchant matching phase 1): the identity merge path, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_identity_merge_hardening_postgres.py

Every case runs the real functions on real tables -- the intake resolver
(services.intake_identity.resolve_or_attach_content_identity), the crawl writer's identity step and
PDP upsert (services.catalog_enrichment_agent.apply), and the identity engine
(services.identity_resolution: apply_approved, its dry run and digest, revert_proposal,
merge_content_key):

  1. snap-back: a listing the engine moved stays on its new content_key and group when a row is later
     minted on its OLD key and the listing is re-crawled -- with the identity gate on (the resolver keeps
     it) and off (the writer keeps the stored key);
  2. pre-flight: an attach whose listing has no product_group_members row is marked
     blocked_no_membership and the rest of the run applies, instead of the whole run aborting;
  3. batch cap + digest: a stale digest refuses with nothing written; the cap is respected;
  4. revert_proposal restores the exact before-state, and refuses after drift;
  5. merge_content_key moves every live row and membership, leaves nothing behind, and its CAS refuses a
     changed member set.

ISOLATION as in test_intake_identity_barcode_lookup_postgres.py: a per-process scratch schema reached
through connections whose search_path is that schema ALONE, dropped at teardown. Nothing here touches
any database but the throwaway one DATABASE_URL names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_MORE = ("045_product_groups.sql", "179_identity_resolution_d2.sql", "177_intake_identity_events.sql")
_SCHEMA = f"identity_merge_hardening_test_{os.getpid()}"

SYN = "external_seed"          # the crawl lane's platform
BRAND_M = "merch_obs_missha"   # the brand store's observed seller
RET_M = "merch_obs_koolseoul"  # a retailer's observed seller
STRATEGY = "retailer_listing_brand_prefix"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}; throwaway only")


def _plain_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _async_url() -> str:
    return _plain_url().replace("postgresql://", "postgresql+asyncpg://", 1)


async def _apply_sql(db, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text(encoding="utf-8")):
        code = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--"))
        if code.strip().rstrip(";").upper() in {"", "BEGIN", "COMMIT"}:
            continue
        await db.execute(stmt)


@pytest.fixture()
async def db(monkeypatch):
    import databases
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    import db.database as dbmod
    from db.catalog import catalog_products

    _assert_throwaway_database()
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)
    admin = databases.Database(_async_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_async_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        await scoped.execute(str(CreateTable(catalog_products).compile(dialect=postgresql.dialect())))
        for name in _MORE:
            await _apply_sql(scoped, _MIGRATIONS / name)
        # intake_identity and the writer read `from db.database import database` at call time.
        monkeypatch.setattr(dbmod, "database", scoped)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


@pytest.fixture()
async def conn(db):
    import asyncpg

    c = await asyncpg.connect(_plain_url(), server_settings={"search_path": _SCHEMA})
    try:
        yield c
    finally:
        await c.close()


# --- rows ------------------------------------------------------------------------------------------


def _ck(brand: str, title: str) -> str:
    from services.catalog_identity import make_content_key

    return make_content_key(brand, title)


def _spid(key: str) -> str:
    return key[len("ext:"):] if key.startswith("ext:retailer:") else key


async def _row(db, key, merchant, content_key, *, title, brand="Missha", group=None, url=None,
               suppressed=None):
    """A catalog row shaped as the crawl lane writes it, with its product_group_members row when
    `group` is given (the singleton/curated membership every grouped listing has)."""
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand,"
        " content_key, canonical_url, source_domain, suppression_reason)"
        " VALUES (:k, :m, :p, :s, :t, :b, :ck, :u, 'shop.example', :sup)",
        {"k": key, "m": merchant, "p": SYN, "s": _spid(key), "t": title, "b": brand, "ck": content_key,
         "u": url, "sup": suppressed},
    )
    if group:
        await db.execute(
            "INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id,"
            " is_primary) VALUES (:g, :m, :p, :s, TRUE)",
            {"g": group, "m": merchant, "p": SYN, "s": _spid(key)})


async def _state(db, key):
    """(content_key, product_group_id, live) of one row -- the identity a move or revert changes."""
    row = await db.fetch_one(
        "SELECT cp.content_key, cp.suppression_reason, m.product_group_id FROM catalog_products cp"
        " LEFT JOIN product_group_members m ON m.merchant_id = cp.merchant_id AND m.platform = cp.platform"
        " AND m.platform_product_id = cp.source_product_id WHERE cp.product_key = :k", {"k": key})
    return (row["content_key"], row["product_group_id"], row["suppression_reason"] is None)


async def _status(conn, pid):
    return await conn.fetchval("SELECT status FROM identity_resolution_proposals WHERE proposal_id = $1", pid)


async def _events(conn):
    return [(r["proposal_id"], r["action"]) for r in await conn.fetch(
        "SELECT proposal_id, action FROM identity_resolution_events ORDER BY id")]


BRAND_TITLE = "Artemisia Calming Ampoule"
LISTING_TITLE = "Missha Artemisia Calming Ampoule"
B = _ck("Missha", BRAND_TITLE)      # the brand product's family key
A = _ck("Missha", LISTING_TITLE)    # what the listing mints (brand prefix kept)
KEEPER = "ext:missha-artemisia-calming-ampoule::57eacff8"


def _listing(n: int) -> str:
    return "ext:retailer:" + f"{n:x}" * 32


async def _brand_product(db, *, key=KEEPER, title=BRAND_TITLE, group="pg_brand"):
    await _row(db, key, BRAND_M, _ck("Missha", title), title=title, group=group,
               url=f"https://missha.example/products/{key[-8:]}")


async def _attach_proposal(conn, listing_key, *, from_ck=A, from_group, to_group="pg_brand", keeper=KEEPER,
                           to_ck=B, approve=True):
    """The engine's own attach_membership proposal (identity_brand_link's evidence shape), approved."""
    from services.identity_resolution import approve_proposals, new_proposal, upsert_proposals

    p = new_proposal(
        kind="attach_membership", strategy=STRATEGY, subject_product_keys=[listing_key, keeper],
        keeper_product_key=keeper, merchant_id=RET_M, content_key=to_ck, confidence=0.9,
        evidence={"listing_product_key": listing_key, "from_content_key": from_ck,
                  "from_product_group_id": from_group, "to_product_group_id": to_group})
    await upsert_proposals(conn, [p])
    if approve:
        assert await approve_proposals(conn, [p["proposal_id"]], "test") == [p["proposal_id"]]
    return p["proposal_id"]


# --- 1. snap-back ------------------------------------------------------------------------------------


async def _moved_listing_then_a_row_on_its_old_key(db, conn):
    from services.identity_resolution import apply_approved

    listing = _listing(1)
    url = "https://koolseoul.example/products/missha-artemisia-calming-ampoule"
    await _brand_product(db)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing", url=url)
    await _attach_proposal(conn, listing, from_group="pg_listing")
    out = await apply_approved(conn, run_id="R_MOVE", strategies=[STRATEGY])
    assert len(out["applied"]) == 1 and out["skipped"] == [] and out["blocked"] == []
    assert await _state(db, listing) == (B, "pg_brand", True)
    # Later: another retailer's listing of the same wording mints its own family on the OLD key.
    await _row(db, _listing(2), "merch_obs_other", A, title=LISTING_TITLE, group="pg_other",
               url="https://other.example/products/missha-artemisia-calming-ampoule")
    return listing, url


async def test_a_moved_listing_stays_on_its_new_key_when_re_crawled_with_the_gate_on(db, conn):
    from services.catalog_enrichment_agent import apply as writer
    from services.intake_identity import DOOR_CATALOG_ENRICHMENT, resolve_or_attach_content_identity

    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    ctx = {"merchant_id": RET_M, "platform": SYN, "source_domain": "koolseoul.example",
           "product_key": listing, "strict_group_resolution": True}
    out = await resolve_or_attach_content_identity(
        brand="Missha", title=LISTING_TITLE, canonical_url=url, source_product_id=_spid(listing),
        door=DOOR_CATALOG_ENRICHMENT, merchant_ctx=ctx)
    assert (out["action"], out["content_key"], out["product_group_id"]) == ("ATTACH", B, "pg_brand")
    assert out["evidence"]["matcher"] == "existing_listing_identity"

    # The writer's own store path for this PDP: identity step, real upsert, group persistence.
    stored = dict(await db.fetch_one("SELECT * FROM catalog_products WHERE product_key = :k", {"k": listing}))
    pdp = await _planned_pdp(stored, content_key=A)
    targets, refusals = {}, {}
    assert await writer._apply_pdp_identity_gate(pdp, identity_gate_on=True, group_targets=targets,
                                                 refusals=refusals)
    await db.execute(writer._PDP_UPSERT_SQL, pdp)
    assert await writer._ensure_primary_retailer_group(pdp, database=db, target=targets[listing],
                                                       refusals=refusals)
    assert refusals == {}
    assert await _state(db, listing) == (B, "pg_brand", True)


async def test_a_moved_listing_stays_on_its_new_key_when_re_crawled_with_the_gate_off(db, conn):
    from services.catalog_enrichment_agent import apply as writer

    listing, _ = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    stored = dict(await db.fetch_one("SELECT * FROM catalog_products WHERE product_key = :k", {"k": listing}))
    new_row = {"product_key": _listing(3), "content_key": "ck_new_row"}  # not stored: untouched
    ungrouped = _listing(14)  # stored, but never grouped: the planned key is written as before
    await _row(db, ungrouped, RET_M, "ck_stored_ungrouped", title="Other")
    plan = {"pdps": [await _planned_pdp(stored, content_key=A), new_row,
                     {"product_key": ungrouped, "content_key": "ck_planned"}]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {"pdp_content_key_kept_for_grouped_row": 1}
    assert [p["content_key"] for p in kept["pdps"]] == [B, "ck_new_row", "ck_planned"]
    await db.execute(writer._PDP_UPSERT_SQL, kept["pdps"][0])
    assert await _state(db, listing) == (B, "pg_brand", True)
    # Control: the same upsert carrying the plan's key is exactly the snap-back the guard prevents.
    await db.execute(writer._PDP_UPSERT_SQL, plan["pdps"][0])
    assert (await _state(db, listing))[0] == A


async def test_a_listing_whose_stored_key_is_its_own_still_resolves_through_tier_0b(db):
    from services.intake_identity import DOOR_CATALOG_ENRICHMENT, resolve_or_attach_content_identity

    listing = _listing(4)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing")
    out = await resolve_or_attach_content_identity(
        brand="Missha", title=LISTING_TITLE, source_product_id=_spid(listing), door=DOOR_CATALOG_ENRICHMENT,
        merchant_ctx={"merchant_id": RET_M, "platform": SYN, "product_key": listing,
                      "strict_group_resolution": True})
    assert (out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (A, "pg_listing",
                                                                                        "content_key")


async def test_a_stored_listing_without_membership_is_left_to_the_tiers_below(db):
    """Off the computed key but in no group: nothing to keep, so the guard steps aside (no error) and the
    listing's own row answers through Tier-0d as before."""
    from services.intake_identity import DOOR_CATALOG_ENRICHMENT, resolve_or_attach_content_identity

    listing = _listing(15)
    await _row(db, listing, RET_M, "ck_hand_set", title=LISTING_TITLE)
    out = await resolve_or_attach_content_identity(
        brand="Missha", title=LISTING_TITLE, source_product_id=_spid(listing), door=DOOR_CATALOG_ENRICHMENT,
        merchant_ctx={"merchant_id": RET_M, "platform": SYN, "product_key": listing,
                      "strict_group_resolution": True})
    assert (out["content_key"], out["evidence"]["matcher"]) == ("ck_hand_set", "source_product_id_match")


async def _planned_pdp(stored, *, content_key):
    """A plan row for the stored listing (every bind the upsert names), as a re-crawl plans it."""
    import re

    from services.catalog_enrichment_agent import apply as writer
    from services.catalog_enrichment_agent.bulk_writer import split_upsert_sql

    _, values, _ = split_upsert_sql(writer._PDP_UPSERT_SQL)
    row = {k: stored.get(k) for k in re.findall(r":(\w+)", values)}
    for k in ("product_payload", "tags", "use_case_tags", "lifestyle_tags"):
        row[k] = json.dumps(row[k]) if row.get(k) is not None else None
    row["content_key"] = content_key
    return row


# --- 2. pre-flight -----------------------------------------------------------------------------------


async def test_an_attach_without_membership_is_blocked_and_the_run_still_applies(db, conn):
    from services.identity_resolution import apply_approved

    await _brand_product(db)
    good, orphan = _listing(5), _listing(6)
    await _row(db, good, RET_M, A, title=LISTING_TITLE, group="pg_good")
    await _row(db, orphan, "merch_obs_orphan", _ck("Missha", "Missha Artemisia Calming Ampoule 2"),
               title="Missha Artemisia Calming Ampoule 2")  # no product_group_members row
    pid_good = await _attach_proposal(conn, good, from_group="pg_good")
    pid_orphan = await _attach_proposal(conn, orphan, from_ck=_ck("Missha", "Missha Artemisia Calming Ampoule 2"),
                                        from_group=None)

    out = await apply_approved(conn, run_id="R_PRE", strategies=[STRATEGY])
    assert out["applied"] == [pid_good] and out["skipped"] == []
    assert out["blocked"] == [(pid_orphan, "blocked_no_membership", f"listing_has_no_membership:{orphan}")]
    assert await _state(db, good) == (B, "pg_brand", True)
    assert (await _state(db, orphan))[1] is None
    assert await _status(conn, pid_orphan) == "blocked_no_membership"
    ev = await conn.fetchval("SELECT evidence FROM identity_resolution_proposals WHERE proposal_id = $1", pid_orphan)
    blocked = (json.loads(ev) if isinstance(ev, str) else ev)["apply_blocked"]
    assert (blocked["status"], blocked["reason"], blocked["run_id"]) == (
        "blocked_no_membership", f"listing_has_no_membership:{orphan}", "R_PRE")
    assert (pid_orphan, "blocked") in await _events(conn)


# --- 3. batch cap + digest ---------------------------------------------------------------------------


async def _three_approved(db, conn):
    await _brand_product(db)
    pids = []
    for n in (7, 8, 9):
        title = f"Missha Artemisia Calming Ampoule {n}"
        await _row(db, _listing(n), RET_M, _ck("Missha", title), title=title, group=f"pg_l{n}")
        pids.append(await _attach_proposal(conn, _listing(n), from_ck=_ck("Missha", title), from_group=f"pg_l{n}"))
    return pids


async def test_a_stale_digest_refuses_with_nothing_written(db, conn):
    from services.identity_resolution import DigestMismatch, apply_approved

    pids = await _three_approved(db, conn)
    plan = await apply_approved(conn, run_id="R_DRY", strategies=[STRATEGY], max_proposals=2, dry_run=True)
    assert plan["planned"] == pids[:2] and plan["deferred"] == pids[2:] and plan["applied"] == []
    assert await _events(conn) == [] and [await _status(conn, p) for p in pids] == ["approved"] * 3

    # Something moved under the dry run: listing 8's group was curated elsewhere.
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_curated' WHERE platform_product_id = :s",
                     {"s": _spid(_listing(8))})
    before = [await _state(db, _listing(n)) for n in (7, 8, 9)]
    with pytest.raises(DigestMismatch):
        await apply_approved(conn, run_id="R_STALE", strategies=[STRATEGY], max_proposals=2,
                             expect_digest=plan["digest"])
    assert [await _state(db, _listing(n)) for n in (7, 8, 9)] == before
    assert await _events(conn) == [] and [await _status(conn, p) for p in pids] == ["approved"] * 3


async def test_the_batch_cap_applies_exactly_the_digested_proposals(db, conn):
    from services.identity_resolution import apply_approved

    pids = await _three_approved(db, conn)
    plan = await apply_approved(conn, run_id="R_DRY", strategies=[STRATEGY], max_proposals=2, dry_run=True)
    again = await apply_approved(conn, run_id="R_DRY2", strategies=[STRATEGY], max_proposals=2, dry_run=True)
    assert again["digest"] == plan["digest"]  # deterministic over an unchanged state
    out = await apply_approved(conn, run_id="R_CAP", strategies=[STRATEGY], max_proposals=2,
                               expect_digest=plan["digest"])
    assert out["applied"] == pids[:2] and out["deferred"] == pids[2:]
    assert [await _status(conn, p) for p in pids] == ["applied", "applied", "approved"]
    assert (await _state(db, _listing(9)))[1] == "pg_l9"
    with pytest.raises(ValueError):
        await apply_approved(conn, strategies=[STRATEGY], max_proposals=0)


# --- 4. revert_proposal ------------------------------------------------------------------------------


async def test_revert_proposal_restores_one_proposal_exactly_and_leaves_the_run_alone(db, conn):
    from services.identity_resolution import apply_approved, revert_proposal

    pids = await _three_approved(db, conn)
    before = {n: await _state(db, _listing(n)) for n in (7, 8, 9)}
    await apply_approved(conn, run_id="R_REV", strategies=[STRATEGY])
    assert [await _status(conn, p) for p in pids] == ["applied"] * 3

    dry = await revert_proposal(conn, pids[0])  # dry run by default
    assert dry["dry_run"] is True and (await _state(db, _listing(7)))[0] == B
    out = await revert_proposal(conn, pids[0], dry_run=False, revert_run_id="RV1")
    assert out["detached"][0]["reverted"] is True
    assert await _state(db, _listing(7)) == before[7]
    assert await _state(db, _listing(8)) == (B, "pg_brand", True)
    assert [await _status(conn, p) for p in pids] == ["reverted", "applied", "applied"]
    assert (pids[0], "reverted") in await _events(conn)
    assert (await revert_proposal(conn, pids[0], dry_run=False))["refused"] == "status_reverted"


async def test_revert_proposal_refuses_a_listing_that_moved_since(db, conn):
    from services.identity_resolution import apply_approved, revert_proposal

    pids = await _three_approved(db, conn)
    await apply_approved(conn, run_id="R_DRIFT", strategies=[STRATEGY])
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_curated_later'"
                     " WHERE platform_product_id = :s", {"s": _spid(_listing(8))})
    events = await _events(conn)
    out = await revert_proposal(conn, pids[1], dry_run=False)
    assert out["refused"] == "moved_since_apply"
    assert await _state(db, _listing(8)) == (B, "pg_curated_later", True)  # not half-reverted
    assert await _status(conn, pids[1]) == "applied" and await _events(conn) == events


# --- 5. merge_content_key ----------------------------------------------------------------------------


async def _merge_fixture(db, conn, *, approve=True):
    from services.identity_resolution import approve_proposals, merge_content_key_proposal, upsert_proposals

    await _brand_product(db)
    loser = _ck("Missha", "Artemisia Ampoule")
    rows = {_listing(10): "pg_a", _listing(11): "pg_a", "ext:missha-artemisia-ampoule::0badc0de": "pg_a_brandish"}
    for key, group in rows.items():
        merchant = RET_M if key.startswith("ext:retailer:") else BRAND_M
        await _row(db, key, merchant if key != _listing(11) else "merch_obs_two", loser,
                   title="Artemisia Ampoule", group=group)
    await _row(db, _listing(12), "merch_obs_gone", loser, title="Artemisia Ampoule", group="pg_gone",
               suppressed="d2_same_url_dup")  # not live: it does not move and holds nothing back
    p = merge_content_key_proposal(strategy="cross_merchant_same_size", from_content_key=loser, to_content_key=B,
                                   from_product_keys=list(rows), from_groups=rows, keeper_product_key=KEEPER,
                                   to_product_group_id="pg_brand")
    await upsert_proposals(conn, [p])
    if approve:
        await approve_proposals(conn, [p["proposal_id"]], "test")
    return p["proposal_id"], loser, rows


async def test_merge_content_key_moves_every_live_row_and_membership(db, conn):
    from services.identity_resolution import apply_approved, revert_proposal

    pid, loser, rows = await _merge_fixture(db, conn)
    before = {k: await _state(db, k) for k in [*rows, _listing(12)]}
    out = await apply_approved(conn, run_id="R_MERGE", strategies=["cross_merchant_same_size"])
    assert out["applied"] == [pid] and out["skipped"] == [] and out["blocked"] == []
    for key in rows:
        assert await _state(db, key) == (B, "pg_brand", True)
    assert await db.fetch_val("SELECT count(*) FROM catalog_products WHERE content_key = :ck"
                              " AND suppression_reason IS NULL", {"ck": loser}) == 0
    assert await _state(db, _listing(12)) == before[_listing(12)]  # the tombstone stays where it was
    detail = await conn.fetchval("SELECT detail FROM identity_resolution_events WHERE proposal_id = $1"
                                 " AND action = 'applied'", pid)
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert sorted(m["product_key"] for m in detail["members"]) == sorted(rows)

    out = await revert_proposal(conn, pid, dry_run=False)
    assert {k: await _state(db, k) for k in before} == before


async def test_merge_content_key_cas_refuses_a_changed_member_set(db, conn):
    from services.identity_resolution import apply_approved

    pid, loser, rows = await _merge_fixture(db, conn)
    await _row(db, _listing(13), "merch_obs_late", loser, title="Artemisia Ampoule", group="pg_late")
    out = await apply_approved(conn, run_id="R_CAS", strategies=["cross_merchant_same_size"])
    assert out["applied"] == [] and out["skipped"] == [(pid, "member_set_drift")]
    for key, group in rows.items():
        assert await _state(db, key) == (loser, group, True)
    assert await _status(conn, pid) == "approved"


async def test_merge_content_key_cas_refuses_a_moved_membership(db, conn):
    from services.identity_resolution import apply_approved

    pid, loser, rows = await _merge_fixture(db, conn)
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_elsewhere'"
                     " WHERE platform_product_id = :s", {"s": _spid(_listing(11))})
    out = await apply_approved(conn, run_id="R_CAS2", strategies=["cross_merchant_same_size"])
    assert out["skipped"] == [(pid, "member_group_drift")]
    assert (await _state(db, _listing(10)))[0] == loser


async def test_merge_content_key_blocks_a_first_party_row_its_sync_would_rekey(db, conn):
    from services.identity_resolution import (approve_proposals, apply_approved, merge_content_key_proposal,
                                              upsert_proposals)

    await _brand_product(db)
    loser = _ck("Missha", "Artemisia Ampoule")
    first_party = "prod::m_missha::shopify::123"
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, content_key)"
        " VALUES (:k, 'm_missha', 'shopify', '123', 'Artemisia Ampoule', :ck)", {"k": first_party, "ck": loser})
    await db.execute("INSERT INTO product_group_members (product_group_id, merchant_id, platform,"
                     " platform_product_id) VALUES ('pg_fp', 'm_missha', 'shopify', '123')")
    p = merge_content_key_proposal(strategy="cross_merchant_same_size", from_content_key=loser, to_content_key=B,
                                   from_product_keys=[first_party], from_groups={first_party: "pg_fp"},
                                   keeper_product_key=KEEPER, to_product_group_id="pg_brand")
    await upsert_proposals(conn, [p])
    await approve_proposals(conn, [p["proposal_id"]], "test")
    out = await apply_approved(conn, run_id="R_FP", strategies=["cross_merchant_same_size"])
    assert out["blocked"] == [(p["proposal_id"], "blocked_unguarded_writer",
                               f"row_rekeyed_by_its_own_sync:{first_party}")]
    assert (await _state(db, first_party))[0] == loser


async def test_the_cli_builds_the_merge_proposal_from_live_state(db, conn):
    from scripts.identity_engine import build_merge

    pid, loser, rows = await _merge_fixture(db, conn, approve=False)
    p = await build_merge(conn, from_ck=loser, to_ck=B, keeper=KEEPER, strategy="cross_merchant_same_size")
    assert p["proposal_id"] == pid  # the same move dedupes onto the proposal already written
    assert p["evidence"]["from_groups"] == dict(sorted(rows.items()))
