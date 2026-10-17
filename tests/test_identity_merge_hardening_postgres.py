"""M3 (cross-merchant matching phase 1): the identity merge path, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_identity_merge_hardening_postgres.py

Every case runs the real functions on real tables -- the intake resolver
(services.intake_identity.resolve_or_attach_content_identity), the crawl writer's identity step and
PDP upsert (services.catalog_enrichment_agent.apply), and the identity engine
(services.identity_resolution: apply_approved, its dry run and digest, revert_proposal,
merge_content_key):

  1. snap-back (INTAKE_IDENTITY_ENGINE_MOVE_GUARD on): a listing the engine moved stays on its new
     content_key and group when a row is later minted on its OLD key, or its GTIN sits on another family
     (FLAGged once per listing), and the listing is re-crawled -- with the identity gate on (the resolver
     keeps it) and off (the writer keeps the stored key). Listings the engine never moved, and reverted
     moves, resolve exactly as before; an unreadable read keeps that one listing's stored identity
     without refusing the row. With the flag OFF the resolver is origin/main's (the parity cases);
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
_MORE = ("045_product_groups.sql", "179_identity_resolution_d2.sql", "177_intake_identity_events.sql",
         "044_external_product_seeds.sql", "085_agent_pdp_view.sql")
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


# --- 1. snap-back guard (INTAKE_IDENTITY_ENGINE_MOVE_GUARD) -----------------------------------------

GUARD_ENV = "INTAKE_IDENTITY_ENGINE_MOVE_GUARD"
GTIN = "08809643062985"  # check digit valid (canonical_gtin)
OTHER_TITLE = "Artemisia Calming Essence Ampoule"   # another family carrying the same GTIN


@pytest.fixture()
def guard_on(monkeypatch):
    monkeypatch.setenv(GUARD_ENV, "1")


@pytest.fixture()
def guard_off(monkeypatch):
    monkeypatch.delenv(GUARD_ENV, raising=False)


async def _resolve(listing, *, title=LISTING_TITLE, url=None, gtin=None):
    from services.intake_identity import DOOR_CATALOG_ENRICHMENT, resolve_or_attach_content_identity

    return await resolve_or_attach_content_identity(
        brand="Missha", title=title, gtin=gtin, canonical_url=url, source_product_id=_spid(listing),
        door=DOOR_CATALOG_ENRICHMENT, merchant_ctx={"merchant_id": RET_M, "platform": SYN,
                                                   "source_domain": "koolseoul.example",
                                                   "product_key": listing, "strict_group_resolution": True})


async def _moved_listing_then_a_row_on_its_old_key(db, conn):
    from services.identity_resolution import apply_approved

    listing = _listing(1)
    url = "https://koolseoul.example/products/missha-artemisia-calming-ampoule"
    await _brand_product(db)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing", url=url)
    pid = await _attach_proposal(conn, listing, from_group="pg_listing")
    out = await apply_approved(conn, run_id="R_MOVE", strategies=[STRATEGY])
    assert out["applied"] == [pid] and out["skipped"] == [] and out.get("blocked", []) == []
    assert await _state(db, listing) == (B, "pg_brand", True)
    # Later: another retailer's listing of the same wording mints its own family on the OLD key.
    await _row(db, _listing(2), "merch_obs_other", A, title=LISTING_TITLE, group="pg_other",
               url="https://other.example/products/missha-artemisia-calming-ampoule")
    return listing, url


async def _gtin_family(db):
    """An older row of ANOTHER seller and family, carrying the GTIN the listing's page now shows."""
    other = "ext:retailer:" + "e" * 32
    await _row(db, other, "merch_obs_gtin_seller", _ck("Missha", OTHER_TITLE), title=OTHER_TITLE,
               group="pg_gtin_family")
    await db.execute("UPDATE catalog_products SET gtin = :g, created_at = '2026-01-01' WHERE product_key = :k",
                     {"g": GTIN, "k": other})
    return other


async def _tasks(db, matcher):
    rows = [dict(r) for r in await db.fetch_all("SELECT id, pdp_id, module_key, status, checklist"
                                                " FROM pdp_review_tasks")]
    for r in rows:
        if isinstance(r["checklist"], str):
            r["checklist"] = json.loads(r["checklist"])
    return [r for r in rows if (r["checklist"] or {}).get("matcher") == matcher]


async def test_a_moved_listing_stays_on_its_new_key_when_re_crawled_with_the_gate_on(db, conn, guard_on):
    from services.catalog_enrichment_agent import apply as writer

    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    out = await _resolve(listing, url=url)
    assert (out["action"], out["content_key"], out["product_group_id"]) == ("ATTACH", B, "pg_brand")
    assert out["evidence"]["matcher"] == "engine_moved_listing_identity"

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


async def test_a_moved_listing_stays_on_its_new_key_when_re_crawled_with_the_gate_off(db, conn, guard_on):
    from services.catalog_enrichment_agent import apply as writer

    listing, _ = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    stored = dict(await db.fetch_one("SELECT * FROM catalog_products WHERE product_key = :k", {"k": listing}))
    new_row = {"product_key": _listing(3), "content_key": "ck_new_row"}  # not stored: untouched
    ungrouped = _listing(14)  # stored, never grouped
    await _row(db, ungrouped, RET_M, "ck_stored_ungrouped", title="Other")
    never_moved = _listing(20)  # stored AND grouped, but the engine never moved it: re-keys as today
    await _row(db, never_moved, RET_M, "ck_stored_grouped", title="Other", group="pg_never_moved")
    plan = {"pdps": [await _planned_pdp(stored, content_key=A), new_row,
                     {"product_key": ungrouped, "content_key": "ck_planned"},
                     {"product_key": never_moved, "content_key": "ck_planned_too"}]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {"pdp_content_key_kept_for_grouped_row": 1}
    assert [p["content_key"] for p in kept["pdps"]] == [B, "ck_new_row", "ck_planned", "ck_planned_too"]
    await db.execute(writer._PDP_UPSERT_SQL, kept["pdps"][0])
    assert await _state(db, listing) == (B, "pg_brand", True)
    # Control: the same upsert carrying the plan's key is exactly the snap-back the guard prevents.
    await db.execute(writer._PDP_UPSERT_SQL, plan["pdps"][0])
    assert (await _state(db, listing))[0] == A


async def test_a_grouped_listing_the_engine_never_moved_still_re_keys(db, guard_on):
    """Nearly every crawled listing is grouped (the writer ensures a primary group per listing): the
    guard must leave them all to the tiers, so a title or brand fix still re-keys the listing."""
    listing = _listing(21)
    await _row(db, listing, RET_M, "ck_before_title_fix", title="Missha Artemisia Calming Ampule",
               group="pg_listing")
    await _row(db, _listing(22), "merch_obs_other", A, title=LISTING_TITLE, group="pg_other")
    out = await _resolve(listing)
    assert (out["content_key"], out["evidence"]["matcher"]) == (A, "content_key")


async def test_a_reverted_move_is_not_guarded(db, conn, guard_on):
    """revert_run marks a proposal reverted even when it left a listing that had moved since: such a
    listing is no longer the engine's, and resolves as today."""
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await conn.execute("UPDATE identity_resolution_proposals SET status = 'reverted'")
    out = await _resolve(listing, url=url)
    assert (out["content_key"], out["evidence"]["matcher"]) == (A, "content_key")


async def test_the_writer_does_not_keep_a_reverted_move(db, conn, guard_on):
    from services.catalog_enrichment_agent import apply as writer

    listing, _ = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await conn.execute("UPDATE identity_resolution_proposals SET status = 'reverted'")
    plan = {"pdps": [{"product_key": listing, "content_key": A}]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {} and kept["pdps"][0]["content_key"] == A


async def test_a_listing_whose_stored_key_is_its_own_still_resolves_through_tier_0b(db, guard_on):
    listing = _listing(4)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing")
    out = await _resolve(listing)
    assert (out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (A, "pg_listing",
                                                                                        "content_key")


@pytest.mark.parametrize("broken,reason", [("_existing_pg_for_listing", "group_unreadable"),
                                           ("_engine_moved_onto", "engine_move_lookup_failed")])
async def test_an_unreadable_read_keeps_the_stored_identity_for_that_listing_only(db, conn, guard_on, monkeypatch,
                                                                                 broken, reason):
    """Fail closed for THIS listing: keep its stored key and its existing membership and record why --
    never refuse the row, which fails the store's whole apply (require_primary_apply)."""
    from services import intake_identity as ii
    from services.catalog_enrichment_agent import apply as writer

    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    real = getattr(ii, broken)

    async def flaky(*args, **kwargs):
        raise ConnectionError("read failed")

    monkeypatch.setattr(ii, broken, flaky)
    out = await _resolve(listing, url=url)
    assert (out["action"], out["content_key"], out["product_group_id"]) == ("ATTACH", B, None)
    assert out["evidence"]["matcher"] == "engine_moved_listing_kept_unverified"
    assert out["evidence"]["evidence"]["unverified"] == reason
    event = await db.fetch_one("SELECT matcher FROM intake_identity_events WHERE product_key = :k", {"k": listing})
    assert event["matcher"] == "engine_moved_listing_kept_unverified"

    stored = dict(await db.fetch_one("SELECT * FROM catalog_products WHERE product_key = :k", {"k": listing}))
    pdp = await _planned_pdp(stored, content_key=A)
    targets, refusals = {}, {}
    assert await writer._apply_pdp_identity_gate(pdp, identity_gate_on=True, group_targets=targets,
                                                 refusals=refusals)
    assert listing not in targets  # no group target: the existing membership is kept, not refused
    monkeypatch.setattr(ii, broken, real)
    await db.execute(writer._PDP_UPSERT_SQL, pdp)
    assert await writer._ensure_primary_retailer_group(pdp, database=db, target=None, refusals=refusals)
    assert refusals == {} and await _state(db, listing) == (B, "pg_brand", True)


async def test_a_moved_listing_is_not_moved_by_gtin_and_the_conflict_is_flagged(db, conn, guard_on):
    """Tier-0a would attach the moved listing to another family (its GTIN is there, older): it keeps its
    stored key and group instead, and the conflict goes to review -- never a silent move."""
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    other = await _gtin_family(db)
    out = await _resolve(listing, url=url, gtin=GTIN)
    assert (out["action"], out["content_key"], out["product_group_id"]) == ("FLAG", B, "pg_brand")
    assert out["evidence"]["matcher"] == "grouped_listing_gtin_conflict"
    detail = out["evidence"]["evidence"]
    assert (detail["reason"], detail["conflict_product_key"], detail["gtin_content_key"]) == (
        "grouped_listing_gtin_conflict", other, _ck("Missha", OTHER_TITLE))
    [task] = await _tasks(db, "grouped_listing_gtin_conflict")
    assert (task["pdp_id"], task["module_key"], task["status"]) == (listing, "identity", "needs_review")
    assert task["checklist"]["candidate_product_key"] == other
    event = await db.fetch_one("SELECT action, matcher, content_key, product_group_id FROM intake_identity_events"
                               " WHERE product_key = :k", {"k": listing})
    assert (event["action"], event["matcher"], event["content_key"], event["product_group_id"]) == (
        "FLAG", "grouped_listing_gtin_conflict", B, "pg_brand")
    await _resolve(listing, url=url, gtin=GTIN)  # a re-crawl does not stack a second task
    assert len(await _tasks(db, "grouped_listing_gtin_conflict")) == 1


async def test_a_moved_listing_is_not_pulled_back_by_a_gtin_on_its_old_family(db, conn, guard_on):
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await db.execute("UPDATE catalog_products SET gtin = :g, created_at = '2026-01-01' WHERE product_key = :k",
                     {"g": GTIN, "k": _listing(2)})  # the row on the OLD key carries the GTIN
    out = await _resolve(listing, url=url, gtin=GTIN)
    assert (out["action"], out["content_key"], out["product_group_id"]) == ("FLAG", B, "pg_brand")
    assert out["evidence"]["matcher"] == "grouped_listing_gtin_conflict"


async def test_a_grouped_listing_the_engine_never_moved_still_resolves_by_gtin(db, guard_on):
    await _gtin_family(db)
    listing = _listing(16)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing")
    out = await _resolve(listing, gtin=GTIN)
    assert (out["content_key"], out["evidence"]["matcher"]) == (_ck("Missha", OTHER_TITLE),
                                                               "gtin_match_brand_title_drift")
    assert await _tasks(db, "grouped_listing_gtin_conflict") == []


async def test_an_ungrouped_listing_still_resolves_by_gtin_as_before(db, guard_on):
    await _gtin_family(db)
    stored = _listing(17)  # stored, never grouped
    await _row(db, stored, RET_M, A, title=LISTING_TITLE)
    new = _listing(18)     # never seen
    for listing in (stored, new):
        out = await _resolve(listing, gtin=GTIN)
        assert (out["action"], out["content_key"], out["evidence"]["matcher"]) == (
            "FLAG", _ck("Missha", OTHER_TITLE), "gtin_match_brand_title_drift")
        assert out["product_group_id"] == "pg_gtin_family"


async def test_the_review_task_dedupe_is_a_primary_key_upsert_not_a_read(db, conn):
    """Two transactions racing to flag one listing: the second waits on the first's key and inserts
    nothing. A read-then-insert would let both insert."""
    import asyncpg

    from services.intake_identity import REVIEW_TASK_UPSERT_SQL, review_task_id

    sql = REVIEW_TASK_UPSERT_SQL
    for n, name in enumerate((":id", ":pdp_id", ":checklist", ":policy_labels"), start=1):
        sql = sql.replace(name, f"${n}")
    args = (review_task_id("grouped_listing_gtin_conflict", "ext:retailer:x"), "ext:retailer:x",
            json.dumps({"matcher": "grouped_listing_gtin_conflict"}), json.dumps([]))
    other = await asyncpg.connect(_plain_url(), server_settings={"search_path": _SCHEMA})
    try:
        first, second = conn.transaction(), other.transaction()
        await first.start()
        await second.start()
        assert await conn.execute(sql, *args) == "INSERT 0 1"
        import asyncio

        racing = asyncio.ensure_future(other.execute(sql, *args))
        await asyncio.sleep(0.2)
        assert not racing.done()  # blocked on the first transaction's key
        await first.commit()
        assert await racing == "INSERT 0 0"
        await second.commit()
    finally:
        await other.close()
    assert await conn.fetchval("SELECT count(*) FROM pdp_review_tasks") == 1


# --- 1b. flag OFF: exactly origin/main ---------------------------------------------------------------


async def test_parity_flag_off_a_moved_listing_resolves_as_before(db, conn, guard_off):
    """With INTAKE_IDENTITY_ENGINE_MOVE_GUARD off the resolver is origin/main's: the re-crawl of a moved
    listing resolves through Tier-0b to the row on its old key (the snap-back the flag exists for)."""
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    out = await _resolve(listing, url=url)
    assert (out["action"], out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (
        "ATTACH", A, "pg_other", "content_key")


async def test_parity_flag_off_a_gtin_conflict_resolves_as_before(db, conn, guard_off):
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await _gtin_family(db)
    out = await _resolve(listing, url=url, gtin=GTIN)
    assert (out["content_key"], out["evidence"]["matcher"]) == (_ck("Missha", OTHER_TITLE),
                                                               "gtin_match_brand_title_drift")
    assert await _tasks(db, "grouped_listing_gtin_conflict") == []


async def test_a_grouped_listing_whose_gtin_agrees_resolves_through_tier_0a_as_before(db, guard_on):
    listing = _listing(19)
    await _row(db, listing, RET_M, A, title=LISTING_TITLE, group="pg_listing")
    await db.execute("UPDATE catalog_products SET gtin = :g WHERE product_key = :k", {"g": GTIN, "k": listing})
    out = await _resolve(listing, gtin=GTIN)
    assert (out["action"], out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (
        "ATTACH", A, "pg_listing", "gtin_match")
    assert await db.fetch_all("SELECT id FROM pdp_review_tasks") == []


async def test_a_moved_listing_whose_gtin_agrees_is_kept_without_a_flag(db, conn, guard_on):
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await db.execute("UPDATE catalog_products SET gtin = :g, created_at = '2026-01-01' WHERE product_key = :k",
                     {"g": GTIN, "k": KEEPER})  # the GTIN sits on the family the engine moved it onto
    out = await _resolve(listing, url=url, gtin=GTIN)
    assert (out["action"], out["content_key"], out["evidence"]["matcher"]) == (
        "ATTACH", B, "engine_moved_listing_identity")
    assert await _tasks(db, "grouped_listing_gtin_conflict") == []


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
    await upsert_proposals(conn, [p], module_writer=M4)
    if approve:
        await approve_proposals(conn, [p["proposal_id"]], "test")
    return p["proposal_id"], loser, rows


async def _digested_apply(conn, strategy, run_id):
    """The governed path: a dry run's digest, then the apply that must match it."""
    from services.identity_resolution import apply_approved

    plan = await apply_approved(conn, strategies=[strategy], dry_run=True)
    return await apply_approved(conn, run_id=run_id, strategies=[strategy], expect_digest=plan["digest"])


async def test_merge_content_key_moves_every_live_row_and_membership(db, conn):
    from services.identity_resolution import revert_proposal

    pid, loser, rows = await _merge_fixture(db, conn)
    before = {k: await _state(db, k) for k in [*rows, _listing(12)]}
    out = await _digested_apply(conn, "cross_merchant_same_size", "R_MERGE")
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
    pid, loser, rows = await _merge_fixture(db, conn)
    await _row(db, _listing(13), "merch_obs_late", loser, title="Artemisia Ampoule", group="pg_late")
    out = await _digested_apply(conn, "cross_merchant_same_size", "R_CAS")
    assert out["applied"] == [] and out["skipped"] == [(pid, "member_set_drift")]
    for key, group in rows.items():
        assert await _state(db, key) == (loser, group, True)
    assert await _status(conn, pid) == "approved"


async def test_merge_content_key_cas_refuses_a_moved_membership(db, conn):
    pid, loser, rows = await _merge_fixture(db, conn)
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_elsewhere'"
                     " WHERE platform_product_id = :s", {"s": _spid(_listing(11))})
    out = await _digested_apply(conn, "cross_merchant_same_size", "R_CAS2")
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
    await upsert_proposals(conn, [p], module_writer=M4)
    await approve_proposals(conn, [p["proposal_id"]], "test")
    out = await apply_approved(conn, run_id="R_FP", strategies=["cross_merchant_same_size"])
    assert out["blocked"] == [(p["proposal_id"], "blocked_unguarded_writer",
                               f"row_rekeyed_by_its_own_sync:{first_party}")]
    assert (await _state(db, first_party))[0] == loser


async def test_the_cli_builds_the_merge_proposal_from_live_state(db, conn):
    from scripts.identity_engine import build_merge

    pid, loser, rows = await _merge_fixture(db, conn, approve=False)
    p = await build_merge(conn, from_ck=loser, to_ck=B, keeper=KEEPER, strategy="coordinator_merge")
    written = await conn.fetchrow("SELECT evidence, keeper_product_key, content_key FROM identity_resolution_proposals"
                                  " WHERE proposal_id = $1", pid)
    ev = json.loads(written["evidence"]) if isinstance(written["evidence"], str) else written["evidence"]
    assert p["evidence"] == ev  # the same move as the module-written proposal, from live state
    assert (p["keeper_product_key"], p["content_key"]) == (written["keeper_product_key"], written["content_key"])
    assert p["evidence"]["from_groups"] == dict(sorted(rows.items()))



# --- M3 review: governance and phase-1 merge-path gaps ---------------------------------------------


async def test_a_merge_never_applies_without_a_digest_whatever_its_strategy_name(db, conn):
    """A merge approved under a name a strategy-filtered apply uses (the link script's, the sweep's) is
    refused by that apply: it applies only as part of a digested batch."""
    from services.identity_resolution import apply_approved

    pid, loser, rows = await _merge_fixture(db, conn)
    await conn.execute("UPDATE identity_resolution_proposals SET strategy = 'retailer_listing_brand_prefix'")
    out = await apply_approved(conn, run_id="R_LINK", strategies=["retailer_listing_brand_prefix"])
    assert out["applied"] == [] and out["skipped"] == [(pid, "merge_content_key_needs_expect_digest")]
    assert await _status(conn, pid) == "approved"
    for key, group in rows.items():
        assert await _state(db, key) == (loser, group, True)


async def test_the_sweep_allowlist_approves_only_dedup_proposals(db, conn):
    from services.identity_reconcile_sweep import APPROVE_ALLOWLIST_SQL, HELD_AUTO_APPROVE_SQL

    pid, _, _ = await _merge_fixture(db, conn, approve=False)
    await conn.execute("UPDATE identity_resolution_proposals SET strategy = 'same_url_dup'")
    assert await conn.fetch(APPROVE_ALLOWLIST_SQL, ["same_url_dup", "junk_url"]) == []
    assert (await conn.fetchrow(HELD_AUTO_APPROVE_SQL, ["same_url_dup", "junk_url"]))["n"] == 0
    assert await _status(conn, pid) == "proposed"


async def test_revert_run_keeps_a_move_it_left_in_place_applied_and_guarded(db, conn, guard_on):
    """revert_run skips a listing that moved since the run; its proposal stays 'applied', so the guard
    still protects the listing on the engine's key."""
    from services.identity_resolution import revert_run

    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    pids = [r["proposal_id"] for r in await conn.fetch("SELECT proposal_id FROM identity_resolution_proposals")]
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_curated' WHERE platform_product_id = :s",
                     {"s": _spid(listing)})  # re-curated: still on B, in another group
    back = await revert_run(conn, "R_MOVE")
    assert back["proposals_reverted"] == [] and back["detached"][0]["why"] == "moved_since_run"
    assert [await _status(conn, p) for p in pids] == ["applied"]
    out = await _resolve(listing, url=url)
    assert (out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (
        B, "pg_curated", "engine_moved_listing_identity")


async def test_revert_run_marks_what_it_moved_back_reverted(db, conn):
    from services.identity_resolution import apply_approved, revert_run

    pids = await _three_approved(db, conn)
    await apply_approved(conn, run_id="R_BACK", strategies=[STRATEGY])
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_curated' WHERE platform_product_id = :s",
                     {"s": _spid(_listing(8))})
    back = await revert_run(conn, "R_BACK")
    assert sorted(back["proposals_reverted"]) == sorted([pids[0], pids[2]])
    assert [await _status(conn, p) for p in pids] == ["reverted", "applied", "reverted"]


async def _merged(db, conn):
    pid, loser, rows = await _merge_fixture(db, conn)
    out = await _digested_apply(conn, "cross_merchant_same_size", "R_MERGED")
    assert out["applied"] == [pid]
    # Later: a new listing of the loser's wording mints on the loser key again.
    await _row(db, _listing(23), "merch_obs_new", loser, title="Artemisia Ampoule", group="pg_new")
    return pid, loser, rows


async def test_a_merge_moved_listing_is_guarded_in_the_resolver_and_the_writer(db, conn, guard_on):
    from services.catalog_enrichment_agent import apply as writer

    _, loser, rows = await _merged(db, conn)
    moved = _listing(10)
    out = await _resolve(moved, title="Artemisia Ampoule")
    assert (out["content_key"], out["product_group_id"], out["evidence"]["matcher"]) == (
        B, "pg_brand", "engine_moved_listing_identity")
    plan = {"pdps": [{"product_key": k, "content_key": loser} for k in rows]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {"pdp_content_key_kept_for_grouped_row": 3}
    assert {p["content_key"] for p in kept["pdps"]} == {B}


async def test_the_keeper_and_a_never_moved_sibling_on_the_engine_key_still_re_key(db, conn, guard_on):
    """Only the listings the engine MOVED are guarded: the keeper (the product they joined) and a sibling
    that reached the key by itself re-key on a title fix exactly as today."""
    from services.catalog_enrichment_agent import apply as writer
    from services.intake_identity import DOOR_CATALOG_ENRICHMENT, resolve_or_attach_content_identity

    await _merged(db, conn)
    sibling = _listing(24)
    await _row(db, sibling, RET_M, B, title="Missha Artemisia Calming Ampule", group="pg_brand")
    fixed = "Artemisia Calming Ampoule Fixed"
    await _row(db, _listing(25), "merch_obs_other", _ck("Missha", fixed), title=fixed, group="pg_fixed")
    await _row(db, _listing(26), "merch_obs_other2", A, title=LISTING_TITLE, group="pg_a")
    keeper_out = await resolve_or_attach_content_identity(
        brand="Missha", title=fixed, source_product_id=_spid(KEEPER), door=DOOR_CATALOG_ENRICHMENT,
        merchant_ctx={"merchant_id": BRAND_M, "platform": SYN, "product_key": KEEPER,
                      "strict_group_resolution": True})
    assert (keeper_out["content_key"], keeper_out["evidence"]["matcher"]) == (_ck("Missha", fixed), "content_key")
    sib_out = await _resolve(sibling)
    assert (sib_out["content_key"], sib_out["evidence"]["matcher"]) == (A, "content_key")
    plan = {"pdps": [{"product_key": KEEPER, "content_key": "ck_keeper_planned"},
                     {"product_key": sibling, "content_key": "ck_sibling_planned"}]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {} and [p["content_key"] for p in kept["pdps"]] == ["ck_keeper_planned", "ck_sibling_planned"]


async def test_a_new_gtin_conflict_gets_a_new_task(db, conn, guard_on):
    listing, url = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    other = await _gtin_family(db)
    await _resolve(listing, url=url, gtin=GTIN)
    await _resolve(listing, url=url, gtin=GTIN)
    assert len(await _tasks(db, "grouped_listing_gtin_conflict")) == 1
    await db.execute("UPDATE catalog_products SET content_key = 'ck_another_family' WHERE product_key = :k",
                     {"k": other})  # the GTIN now points at another family: a new conflict
    await _resolve(listing, url=url, gtin=GTIN)
    tasks = await _tasks(db, "grouped_listing_gtin_conflict")
    assert sorted(t["checklist"]["evidence"]["gtin_content_key"] for t in tasks) == sorted(
        [_ck("Missha", OTHER_TITLE), "ck_another_family"])


async def test_the_writer_keeps_stored_keys_when_its_lookup_fails(db, conn, guard_on):
    """The guard's own read failing (here: no proposals table) never fails the store's apply: every
    stored row keeps its key, new rows are written as planned, and it is counted."""
    from services.catalog_enrichment_agent import apply as writer

    listing, _ = await _moved_listing_then_a_row_on_its_old_key(db, conn)
    await conn.execute("DROP TABLE identity_resolution_proposals CASCADE")
    plan = {"pdps": [{"product_key": listing, "content_key": A},
                     {"product_key": _listing(27), "content_key": "ck_brand_new"}]}
    kept, counts = await writer._keep_grouped_content_keys(plan, db)
    assert counts == {"engine_move_lookup_failed": 1, "pdp_content_key_kept_unverified": 1}
    assert [p["content_key"] for p in kept["pdps"]] == [B, "ck_brand_new"]


async def test_the_blocked_set_and_the_proposals_themselves_are_in_the_digest(db, conn):
    from services.identity_resolution import DigestMismatch, apply_approved

    pids = await _three_approved(db, conn)
    first = await apply_approved(conn, strategies=[STRATEGY], dry_run=True)
    orphan = _listing(28)
    await _row(db, orphan, "merch_obs_orphan", "ck_orphan", title="Orphan")
    await _attach_proposal(conn, orphan, from_ck="ck_orphan", from_group=None)
    second = await apply_approved(conn, strategies=[STRATEGY], dry_run=True)
    assert second["planned"] == first["planned"] == pids and len(second["blocked"]) == 1
    assert second["digest"] != first["digest"]
    with pytest.raises(DigestMismatch):
        await apply_approved(conn, strategies=[STRATEGY], expect_digest=first["digest"])
    # Same ids, same before-state, but a proposal's evidence changed: a different apply.
    await conn.execute("UPDATE identity_resolution_proposals SET evidence = evidence || '{\"note\": 1}'::jsonb"
                       " WHERE proposal_id = $1", pids[0])
    third = await apply_approved(conn, strategies=[STRATEGY], dry_run=True)
    assert third["planned"] == pids and third["digest"] != second["digest"]
    assert await _events(conn) == []


async def _dedup_pair(db, conn):
    """Two same-merchant rows on one key, each backed by an active seed, and an approved suppress_dup."""
    from services.identity_resolution import approve_proposals, new_proposal, upsert_proposals

    for key in ("ext:dup-keeper::1", "ext:dup-loser::2"):
        await _row(db, key, BRAND_M, "ck_dup", title="Dup", group=f"pg_{key[-1]}")
        await db.execute("INSERT INTO external_product_seeds (id, market, destination_url, attached_product_key)"
                         " VALUES (:i, 'US', :u, :k)", {"i": f"eps_{key[-1]}", "u": f"https://x.example/{key[-1]}",
                                                         "k": key})
        await db.execute("UPDATE catalog_products SET source_ref = :i WHERE product_key = :k",
                         {"i": f"eps_{key[-1]}", "k": key})
    p = new_proposal(kind="suppress_dup", strategy="m3_test_dedup",
                     subject_product_keys=["ext:dup-keeper::1", "ext:dup-loser::2"],
                     keeper_product_key="ext:dup-keeper::1", merchant_id=BRAND_M, content_key="ck_dup")
    await upsert_proposals(conn, [p])
    await approve_proposals(conn, [p["proposal_id"]], "test")
    return p["proposal_id"]


async def _seed_status(db, sid):
    return await db.fetch_val("SELECT status FROM external_product_seeds WHERE id = :i", {"i": sid})


async def test_revert_proposal_restores_a_suppress_dup_and_its_seed(db, conn):
    from services.identity_resolution import apply_approved, revert_proposal

    pid = await _dedup_pair(db, conn)
    out = await apply_approved(conn, run_id="R_DUP", strategies=["m3_test_dedup"])
    assert out["applied"] == [pid]
    assert (await _state(db, "ext:dup-loser::2"))[2] is False and await _seed_status(db, "eps_2") == "inactive"
    back = await revert_proposal(conn, pid, dry_run=False)
    assert back["restored_rows"] == ["ext:dup-loser::2"] and back["reactivated_seeds"] == ["eps_2"]
    assert (await _state(db, "ext:dup-loser::2"))[2] is True and await _seed_status(db, "eps_2") == "active"
    assert await _status(conn, pid) == "reverted"


async def test_revert_proposal_refuses_a_suppress_dup_whose_tombstone_moved(db, conn):
    from services.identity_resolution import apply_approved, revert_proposal

    pid = await _dedup_pair(db, conn)
    await apply_approved(conn, run_id="R_DUP2", strategies=["m3_test_dedup"])
    await db.execute("UPDATE catalog_products SET suppression_reason = NULL, suppression_metadata = NULL"
                     " WHERE product_key = 'ext:dup-loser::2'")  # someone revived it by hand
    back = await revert_proposal(conn, pid, dry_run=False)
    assert back["refused"] == "moved_since_apply"
    assert await _seed_status(db, "eps_2") == "inactive" and await _status(conn, pid) == "applied"


async def test_revert_proposal_refuses_a_merge_whose_member_moved(db, conn):
    from services.identity_resolution import revert_proposal

    pid, loser, rows = await _merge_fixture(db, conn)
    await _digested_apply(conn, "cross_merchant_same_size", "R_MREV")
    await db.execute("UPDATE product_group_members SET product_group_id = 'pg_elsewhere'"
                     " WHERE platform_product_id = :s", {"s": _spid(_listing(11))})
    events = await _events(conn)
    back = await revert_proposal(conn, pid, dry_run=False)
    assert back["refused"] == "moved_since_apply"
    assert await _state(db, _listing(10)) == (B, "pg_brand", True) and await _events(conn) == events


# --- module-only strategy: approval of one M4 proposal run -------------------------------------------

M4 = "cross_merchant_same_size"


def _verdict(**over):
    v = {"strategy": M4, "rule_version": "t0.v1", "tier": "T0b", "same_size": True,
         "size": {"value": 30, "unit": "ml", "pack": 1},
         # `{listing}` is the listing the proposal moves (_m4_proposal fills it in): a matched offer
         # must be a variant of a moved row.
         "matched_offers": [{"offer_id": "off_1", "sku_key": "{listing}::v:4500001"}],
         "matched_seeds": [{"seed_id": "eps_m4", "variant_id": "4500001"}]}
    v.update(over)
    return v


async def _m4_proposal(conn, n, *, run="m4run_1", kind="attach_membership", proposer=M4, verdict=None,
                       drop_verdict=False):
    from services.identity_resolution import merge_content_key_proposal, new_proposal, upsert_proposals

    extra = {"proposer": proposer, "proposal_run_id": run}
    if not drop_verdict:
        extra["matcher_verdict"] = verdict if verdict is not None else _verdict(
            **({"from_key_single_size": True} if kind == "merge_content_key" else {}))
    listing = _listing(40 + n)
    for o in (extra.get("matcher_verdict") or {}).get("matched_offers") or []:
        if isinstance(o, dict) and isinstance(o.get("sku_key"), str):
            o["sku_key"] = o["sku_key"].replace("{listing}", listing)
    if kind == "merge_content_key":
        p = merge_content_key_proposal(strategy=M4, from_content_key=f"ck_m4_{n}", to_content_key=B,
                                       from_product_keys=[listing], from_groups={listing: f"pg_m4_{n}"},
                                       keeper_product_key=KEEPER, to_product_group_id="pg_brand", evidence=extra)
    else:
        p = new_proposal(kind="attach_membership", strategy=M4, subject_product_keys=[listing, KEEPER],
                         keeper_product_key=KEEPER, merchant_id=RET_M, content_key=B,
                         evidence={"listing_product_key": listing, "from_content_key": f"ck_m4_{n}",
                                   "from_product_group_id": f"pg_m4_{n}", "to_product_group_id": "pg_brand",
                                   **extra})
    await upsert_proposals(conn, [p], module_writer=M4)
    return p["proposal_id"]


async def test_an_m4_run_is_approved_exactly_under_its_digest(db, conn):
    from services.identity_resolution import DigestMismatch, approve_module_run

    run = [await _m4_proposal(conn, 1), await _m4_proposal(conn, 2), await _m4_proposal(conn, 3,
                                                                                         kind="merge_content_key")]
    other_run = await _m4_proposal(conn, 4, run="m4run_2")
    dry = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    assert dry["dry_run"] is True and sorted(dry["proposals"]) == sorted(run) and dry["problems"] == {}
    assert [await _status(conn, p) for p in run] == ["proposed"] * 3

    refused = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                                       dry_run=False)
    assert refused["refused"] == "expect_digest_required"
    late = await _m4_proposal(conn, 5)  # the run grew after the dry run: a different set
    with pytest.raises(DigestMismatch):
        await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                                 expect_digest=dry["digest"], dry_run=False)
    assert [await _status(conn, p) for p in [*run, late]] == ["proposed"] * 4

    again = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    out = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                                   expect_digest=again["digest"], dry_run=False)
    assert sorted(out["approved"]) == sorted([*run, late])
    assert [await _status(conn, p) for p in [*run, late]] == ["approved"] * 4
    assert await _status(conn, other_run) == "proposed"


@pytest.mark.parametrize("make,needle", [
    (dict(drop_verdict=True), "matcher_verdict missing"),
    (dict(verdict=_verdict(tier="fuzzy")), "tier"),
    (dict(verdict=_verdict(same_size=False)), "same_size"),
    (dict(verdict=_verdict(matched_offers=[])), "matched_offers"),
    (dict(verdict=_verdict(matched_offers=[{"offer_id": "off_1", "sku_key": "ext:x::canonical"}])), "per-variant"),
    (dict(verdict=_verdict(matched_seeds=None)), "matched_seeds"),
    (dict(kind="merge_content_key", verdict=_verdict()), "from_key_single_size"),
    (dict(proposer="hand_made"), "evidence.proposer"),
])
async def test_an_m4_run_with_one_bad_proposal_approves_nothing(db, conn, make, needle):
    from services.identity_resolution import approve_module_run

    good = await _m4_proposal(conn, 1)
    bad = await _m4_proposal(conn, 2, **make)
    out = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    assert out["refused"] == "invalid_proposals" and list(out["problems"]) == [bad]
    assert any(needle in p for p in out["problems"][bad])
    written = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                                       expect_digest=out["digest"], dry_run=False)
    assert written["refused"] == "invalid_proposals"
    assert [await _status(conn, p) for p in (good, bad)] == ["proposed", "proposed"]


async def test_approve_module_run_refuses_without_a_run_or_for_another_strategy(db, conn):
    from services.identity_resolution import approve_module_run

    with pytest.raises(ValueError):
        await approve_module_run(conn, strategy=M4, proposal_run_id="", decided_by="peng")
    with pytest.raises(ValueError):
        await approve_module_run(conn, strategy="retailer_listing_brand_prefix", proposal_run_id="r",
                                 decided_by="peng")
    out = await approve_module_run(conn, strategy=M4, proposal_run_id="no_such_run", decided_by="peng")
    assert out["refused"] == "no_proposed_proposals_in_run"


async def test_an_m4_approval_is_recorded_per_proposal_with_its_digest(db, conn):
    from services.identity_resolution import approve_module_run

    run = [await _m4_proposal(conn, 1), await _m4_proposal(conn, 2)]
    dry = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    assert await _events(conn) == []  # a dry run records nothing
    await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                             expect_digest=dry["digest"], dry_run=False)
    rows = await conn.fetch("SELECT proposal_id, action, run_id, detail FROM identity_resolution_events ORDER BY id")
    assert sorted(r["proposal_id"] for r in rows) == sorted(run)
    for r in rows:
        detail = json.loads(r["detail"]) if isinstance(r["detail"], str) else r["detail"]
        assert (r["action"], r["run_id"]) == ("approved", "m4run_1")
        assert (detail["digest"], detail["decided_by"], detail["proposal_run_id"], detail["strategy"]) == (
            dry["digest"], "peng", "m4run_1", M4)
        assert detail["approved_at"]


async def test_an_m4_approval_digest_covers_each_proposals_content_not_only_its_id(db, conn):
    """Same ids, but one proposal's evidence was edited between the dry run and --execute: refused."""
    from services.identity_resolution import DigestMismatch, approve_module_run

    run = [await _m4_proposal(conn, 1), await _m4_proposal(conn, 2)]
    dry = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    await conn.execute(
        "UPDATE identity_resolution_proposals SET evidence = jsonb_set(evidence, '{matcher_verdict,rule_version}',"
        " '\"t0.v2\"') WHERE proposal_id = $1", run[0])
    again = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    assert again["proposals"] == dry["proposals"] and again["problems"] == {}  # still valid, same ids
    with pytest.raises(DigestMismatch):
        await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                                 expect_digest=dry["digest"], dry_run=False)
    assert [await _status(conn, p) for p in run] == ["proposed", "proposed"] and await _events(conn) == []


async def test_a_module_run_proposal_of_another_kind_is_not_approvable(db, conn):
    from services.identity_resolution import approve_module_run, new_proposal, upsert_proposals

    good = await _m4_proposal(conn, 1)
    p = new_proposal(kind="suppress_dup", strategy=M4, subject_product_keys=["a", "b"], keeper_product_key="a",
                     merchant_id=RET_M, content_key="ck_x",
                     evidence={"proposer": M4, "proposal_run_id": "m4run_1", "matcher_verdict": _verdict()})
    await upsert_proposals(conn, [p], module_writer=M4)
    out = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    assert out["refused"] == "invalid_proposals" and list(out["problems"]) == [p["proposal_id"]]
    assert any("kind is 'suppress_dup'" in x for x in out["problems"][p["proposal_id"]])
    assert await _status(conn, good) == "proposed"


async def test_an_approved_m4_attach_never_applies_without_a_digest(db, conn):
    """Whatever its kind: a module-only strategy's proposals apply only in a digested batch."""
    from services.identity_resolution import apply_approved, approve_module_run

    pid = await _m4_proposal(conn, 1)
    dry = await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng")
    await approve_module_run(conn, strategy=M4, proposal_run_id="m4run_1", decided_by="peng",
                             expect_digest=dry["digest"], dry_run=False)
    out = await apply_approved(conn, run_id="R_M4", strategies=[M4])
    assert out["applied"] == [] and out["skipped"] == [(pid, "module_only_strategy_needs_expect_digest")]
    assert await _status(conn, pid) == "approved"



async def test_only_the_module_may_write_its_strategys_proposals(db, conn):
    """upsert_proposals refuses a module-only strategy's proposal unless the caller is that module
    (module_writer=<its strategy>), and writes nothing when it refuses."""
    from services.identity_resolution import CROSS_MERCHANT_SAME_SIZE, new_proposal, upsert_proposals

    def attach(listing, strategy):
        return new_proposal(kind="attach_membership", strategy=strategy, subject_product_keys=[listing, KEEPER],
                            keeper_product_key=KEEPER, content_key=B,
                            evidence={"listing_product_key": listing, "from_content_key": "ck_x",
                                      "from_product_group_id": "pg_x", "to_product_group_id": "pg_brand"})

    m4, plain = attach(_listing(60), CROSS_MERCHANT_SAME_SIZE), attach(_listing(61), "coordinator_merge")
    for token in (None, "retailer_listing_brand_prefix", "coordinator_merge"):
        with pytest.raises(ValueError, match="module-only"):
            await upsert_proposals(conn, [plain, m4], module_writer=token)
    assert await conn.fetchval("SELECT count(*) FROM identity_resolution_proposals") == 0  # not even `plain`
    assert (await upsert_proposals(conn, [plain]))["inserted"] == 1  # other strategies need no token
    out = await upsert_proposals(conn, [m4], module_writer=CROSS_MERCHANT_SAME_SIZE)
    assert out["inserted"] == 1 and await _status(conn, m4["proposal_id"]) == "proposed"
