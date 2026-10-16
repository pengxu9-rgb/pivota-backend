"""M4 cross_merchant_same_size on the production dialect: the export SQL, --write-proposals, and the
M3 engine reading what it wrote.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_identity_cross_merchant_match_postgres.py

Runs the real script entry point (scripts/ops/propose_cross_merchant_matches.run) against real tables:
  1. the dry run's six SELECTs run in ONE read-only transaction and write proposals.jsonl + the audit
     sample; nothing is written to the database;
  2. --write-proposals refuses a stale digest (nothing written), then inserts status 'proposed' rows
     with one proposal run id; the script never approves or applies;
  3. #2637's approve_module_run approves exactly that run (dry run -> ira_ digest -> execute, as
     Peng would), and the M3 engine dry-runs an apply over them: a listing
     whose membership vanished since is blocked_no_membership, the rest plan; an apply without a
     digest skips every proposal (strategy_needs_expect_digest); with the dry run's digest it applies,
     moving content_keys and groups exactly as the evidence says.

ISOLATION as in test_identity_merge_hardening_postgres.py: a per-process scratch schema reached through
connections whose search_path is that schema ALONE, dropped at teardown.
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
_MORE = ("045_product_groups.sql", "179_identity_resolution_d2.sql", "044_external_product_seeds.sql")
_SCHEMA = f"cross_merchant_match_test_{os.getpid()}"
SYN = "external_seed"
STRATEGY = "cross_merchant_same_size"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r}; throwaway only")


def _plain_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _apply_sql(conn, path: Path) -> None:
    from db.sql_migrations import split_statements

    for stmt in split_statements(path.read_text(encoding="utf-8")):
        code = "\n".join(line for line in stmt.splitlines() if not line.strip().startswith("--"))
        if code.strip().rstrip(";").upper() in {"", "BEGIN", "COMMIT"}:
            continue
        await conn.execute(stmt)


@pytest.fixture()
async def conn():
    import asyncpg
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.catalog import catalog_offers, catalog_products, catalog_skus

    _assert_throwaway_database()
    admin = await asyncpg.connect(_plain_url())
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    c = await asyncpg.connect(_plain_url(), server_settings={"search_path": _SCHEMA})
    try:
        for table in (catalog_products, catalog_offers, catalog_skus):
            await c.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
        for name in _MORE:
            await _apply_sql(c, _MIGRATIONS / name)
        yield c
    finally:
        await c.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.close()


def _spid(key: str) -> str:
    return key[len("ext:"):]


async def _product(c, key, *, merchant, title, brand="Laneige", ck, seed_kind=None, signed=True, group="auto",
                   created="2026-05-01"):
    await c.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand, content_key,"
        " pivota_signature_id, seed_kind, created_at, image_url, canonical_url, source_system)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::timestamp, $11, $12, 'crawl')",
        key, merchant, SYN, _spid(key), title, brand, ck, ("sig_" + key) if signed else None, seed_kind,
        __import__("datetime").datetime.fromisoformat(created), f"https://img.example/{_spid(key)}.jpg",
        f"https://{merchant}.example/p/{_spid(key)}")
    if group:
        g = ("pg_" + key.replace(":", "_")) if group == "auto" else group
        await c.execute(
            "INSERT INTO product_group_members (product_group_id, merchant_id, platform, platform_product_id, is_primary)"
            " VALUES ($1, $2, $3, $4, TRUE)", g, merchant, SYN, _spid(key))


async def _offer(c, key, offer_id, *, merchant, price, host, sku_title="Default Title", offer_type="retailer",
                 market="US"):  # price None: an offer the market lists without a price
    sku_key = f"{key}::v:{offer_id}"  # a per-variant SKU, as the crawl lane keys it
    await c.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id,"
        " title, sku_payload) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
        sku_key, key, merchant, SYN, _spid(key), offer_id, sku_title, json.dumps({"option1": sku_title}))
    await c.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, offer_type, market, availability,"
        " currency, merchant_effective_price, source_domain) VALUES ($1, $2, $3, $4, $5, $6, 'in_stock', 'USD', $7, $8)",
        offer_id, sku_key, key, merchant, offer_type, market, price, host)


ANCHOR = "ext:laneige-lip-sleeping-mask::a1"
ULTA = "ext:retailer:" + "1" * 32
SEPH = "ext:retailer:" + "2" * 32
DUP_A = "ext:retailer:" + "3" * 32
DUP_B = "ext:retailer:" + "4" * 32
UNSIGNED = "ext:retailer:" + "5" * 32
SHARED = "ext:retailer:" + "6" * 32
MULTI = "ext:retailer:" + "7" * 32


async def _catalog(c):
    await _product(c, ANCHOR, merchant="m_laneige", title="Lip Sleeping Mask 20g", brand="LANEIGE", ck="ck_anchor",
                   seed_kind="self", created="2026-01-01")
    await _offer(c, ANCHOR, "o_anchor", merchant="m_laneige", price=24, host="us.laneige.com", offer_type="brand_direct")
    await _product(c, ULTA, merchant="m_ulta", title="LANEIGE Lip Sleeping Mask 0.7 oz", ck="ck_ulta")
    await _offer(c, ULTA, "o_ulta_20", merchant="m_ulta", price=24, host="www.ulta.com", sku_title="0.7 oz")
    await _offer(c, ULTA, "o_ulta_sg", merchant="m_ulta", price=30, host="www.ulta.com", sku_title="0.7 oz", market="SG")
    await _product(c, SEPH, merchant="m_seph", title="Lip Sleeping Mask 20g", ck="ck_seph")
    await _offer(c, SEPH, "o_seph", merchant="m_seph", price=23, host="sephora.com")
    # one retailer content_key held by two identical live rows: they move together
    await _product(c, DUP_A, merchant="m_yes", title="Lip Sleeping Mask 20g", ck="ck_dup")
    await _offer(c, DUP_A, "o_dup_a", merchant="m_yes", price=22, host="yesstyle.com", sku_title="20g")
    await _product(c, DUP_B, merchant="m_yes2", title="Lip Sleeping Mask 20g", ck="ck_dup")
    await _offer(c, DUP_B, "o_dup_b", merchant="m_yes2", price=22, host="stylevana.com", sku_title="20g")
    # a content_key whose second live row is unsigned: never judged, so the signed one is held
    await _product(c, SHARED, merchant="m_ob", title="Lip Sleeping Mask 20g", ck="ck_shared")
    await _offer(c, SHARED, "o_shared", merchant="m_ob", price=24, host="oliveyoung.example")
    await _product(c, UNSIGNED, merchant="m_ob2", title="Lip Sleeping Mask 20g", ck="ck_shared", signed=False)
    # a retailer page that also sells a PRICED 8 g: phase 1 never moves it (listing_multi_size)
    await _product(c, MULTI, merchant="m_multi", title="Lip Sleeping Mask 20g", ck="ck_multi")
    await _offer(c, MULTI, "o_multi_20", merchant="m_multi", price=24, host="multi.example", sku_title="20g")
    await _offer(c, MULTI, "o_multi_8", merchant="m_multi", price=None, host="multi.example", sku_title="8g")
    # seeds: the anchor's single-variant 20 g seed is listed (variant_id null); a second seed of the
    # same product in 8 g never is; Ulta's two-variant seed names its 0.7 oz variant
    for sid, attached, variants in (
            ("seed_anchor", ANCHOR, [{"variant_id": "a1", "title": "20g"}]),
            ("seed_anchor_8g", ANCHOR, [{"variant_id": "a8", "title": "8g"}]),
            ("seed_ulta", ULTA, [{"variant_id": "u20", "title": "0.7 oz"}, {"variant_id": "u8", "title": "0.3 oz"}])):
        await c.execute(
            "INSERT INTO external_product_seeds (id, market, destination_url, seed_data, status, attached_product_key)"
            " VALUES ($1, 'US', 'https://x.example/' || $1, $2::jsonb, 'active', $3)",
            sid, json.dumps({"variants": variants}), attached)


def _args(tmp_path, *extra):
    from scripts.ops import propose_cross_merchant_matches as script

    return script.parse_args(["--out", str(tmp_path), *extra])


async def _proposals(c):
    rows = [dict(r) for r in await c.fetch("SELECT * FROM identity_resolution_proposals ORDER BY proposal_id")]
    for r in rows:
        r["evidence"] = json.loads(r["evidence"]) if isinstance(r["evidence"], str) else r["evidence"]
    return rows


async def test_dry_run_write_proposals_and_the_engine_reading_them(conn, tmp_path, capsys):
    from scripts.ops import propose_cross_merchant_matches as script
    from services.identity_cross_merchant_match import evidence_problems
    from services.identity_resolution import DigestMismatch, apply_approved, approve_module_run

    c = conn
    await _catalog(c)

    # 1. dry run: reports, writes files, writes nothing to the database
    assert await script.run(c, _args(tmp_path)) == 0
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    head = next(line for line in out if line["section"] == "proposals")
    held = next(line for line in out if line["section"] == "outcomes")["held"]
    assert head["proposals"] == 3 and head["by_kind"] == {"attach_membership": 2, "merge_content_key": 1}
    assert held == {"key_shared_with_unmatched": 1, "listing_multi_size": 1}  # MULTI: an unpriced 8 g too
    audit_logged = [line for line in out if line["section"] == "audit"]
    assert len(audit_logged) == 4 and {a["anchor"]["product_key"] for a in audit_logged} == {ANCHOR}
    digest = head["digest"]
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["audit_sample.jsonl", "proposals.jsonl"]
    assert len((tmp_path / "proposals.jsonl").read_text().splitlines()) == 3
    audit = [json.loads(line) for line in (tmp_path / "audit_sample.jsonl").read_text().splitlines()]
    assert len(audit) == 4 and {a["anchor"]["product_key"] for a in audit} == {ANCHOR}
    assert await c.fetchval("SELECT count(*) FROM identity_resolution_proposals") == 0

    # 2. a stale digest is refused, nothing written
    assert await script.run(c, _args(tmp_path, "--write-proposals", "--expect-digest", "cmd_stale",
                                     "--max-proposals", "10")) == 2
    assert await c.fetchval("SELECT count(*) FROM identity_resolution_proposals") == 0
    capsys.readouterr()
    # ... and a cap below the set
    assert await script.run(c, _args(tmp_path, "--write-proposals", "--expect-digest", digest,
                                     "--max-proposals", "2")) == 2
    assert await c.fetchval("SELECT count(*) FROM identity_resolution_proposals") == 0
    capsys.readouterr()
    assert await script.run(c, _args(tmp_path, "--write-proposals", "--expect-digest", digest,
                                     "--max-proposals", "3")) == 0
    written = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                   if json.loads(line)["section"] == "written")
    assert written["inserted"] == 3 and written["dry_run_digest"] == digest
    run_id = written["proposal_run_id"]
    assert f"--strategy {STRATEGY} --run {run_id}" in written["approve_dry_run"]
    assert "irad_" not in json.dumps(written) and "approve_digest" not in written

    rows = await _proposals(c)
    assert {r["status"] for r in rows} == {"proposed"}            # never approved or applied here
    assert {r["strategy"] for r in rows} == {STRATEGY}
    assert {r["evidence"]["proposal_run_id"] for r in rows} == {run_id}
    assert {r["evidence"]["proposer"] for r in rows} == {STRATEGY}
    assert all(evidence_problems(r) == [] for r in rows)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    ulta = next(r for r in by_kind["attach_membership"] if r["evidence"]["listing_product_key"] == ULTA)
    mv = ulta["evidence"]["matcher_verdict"]
    assert sorted(o["offer_id"] for o in mv["matched_offers"]) == ["o_ulta_20", "o_ulta_sg"]  # every market
    assert {o["market"] for o in mv["matched_offers"]} == {"US", "SG"}
    assert mv["matched_seeds"] == [{"seed_id": "seed_anchor", "variant_id": None},
                                   {"seed_id": "seed_ulta", "variant_id": "u20"}]
    merge = by_kind["merge_content_key"][0]
    assert merge["evidence"]["from_product_keys"] == sorted([DUP_A, DUP_B])
    assert merge["evidence"]["keeper_product_key"] == ANCHOR == merge["keeper_product_key"]
    # a re-run writes nothing new (same proposal_ids) and reports the dedupe
    capsys.readouterr()
    assert await script.run(c, _args(tmp_path, "--write-proposals", "--expect-digest", digest,
                                     "--max-proposals", "3")) == 0
    again = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                 if json.loads(line)["section"] == "written")
    assert again["inserted"] == 0 and again["deduped"] == 3

    # 3. the engine reads them. Approval is Peng's step (here: the test). Then Sephora's membership
    # vanishes before the apply: the pre-flight blocks that one proposal instead of aborting the run.
    ids = [r["proposal_id"] for r in rows]
    dry = await approve_module_run(c, strategy=STRATEGY, proposal_run_id=run_id, decided_by="peng")
    assert dry.get("dry_run") and sorted(dry["proposals"]) == sorted(ids) and dry["problems"] == {}
    assert {r["status"] for r in await _proposals(c)} == {"proposed"}
    done_approve = await approve_module_run(c, strategy=STRATEGY, proposal_run_id=run_id, decided_by="peng",
                                            expect_digest=dry["digest"], dry_run=False)
    assert sorted(done_approve["approved"]) == sorted(ids)
    await c.execute("DELETE FROM product_group_members WHERE platform_product_id = $1", _spid(SEPH))
    plan = await apply_approved(c, strategies=[STRATEGY], max_proposals=200, dry_run=True)
    seph = next(r["proposal_id"] for r in rows if r["evidence"].get("listing_product_key") == SEPH)
    assert [b[0] for b in plan["blocked"]] == [seph] and plan["blocked"][0][1] == "blocked_no_membership"
    assert len(plan["planned"]) == 2
    with pytest.raises(DigestMismatch):
        await apply_approved(c, strategies=[STRATEGY], max_proposals=200, expect_digest="ird_stale")
    plan = await apply_approved(c, strategies=[STRATEGY], max_proposals=200, dry_run=True)
    done = await apply_approved(c, strategies=[STRATEGY], max_proposals=200, expect_digest=plan["digest"])
    assert len(done["applied"]) == 2 and done["skipped"] == []
    moved = {r["product_key"]: (r["content_key"], r["g"]) for r in await c.fetch(
        "SELECT cp.product_key, cp.content_key, m.product_group_id AS g FROM catalog_products cp"
        " LEFT JOIN product_group_members m ON m.merchant_id = cp.merchant_id AND m.platform = cp.platform"
        " AND m.platform_product_id = cp.source_product_id WHERE cp.product_key = ANY($1::text[])",
        [ULTA, DUP_A, DUP_B, SEPH, SHARED, MULTI])}
    anchor_group = "pg_" + ANCHOR.replace(":", "_")
    assert moved[ULTA] == moved[DUP_A] == moved[DUP_B] == ("ck_anchor", anchor_group)
    assert moved[SEPH] == ("ck_seph", None) and moved[SHARED][0] == "ck_shared" and moved[MULTI][0] == "ck_multi"


async def test_explain_plans_every_statement_and_executes_none(conn, tmp_path, capsys):
    from scripts.ops import propose_cross_merchant_matches as script

    await _catalog(conn)
    assert await script.run(conn, _args(tmp_path, "--explain", "--brands", "laneige")) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["statement"] for line in lines] == [n for n, _sql in script.PHASE1 + script.PHASE2]
    assert all(line["section"] == "explain" and line["plan"] for line in lines)
    assert not any(p.name.endswith(".jsonl") for p in tmp_path.iterdir())


async def test_the_brand_filter_and_phase_two_read_only_the_candidate_keys(conn, tmp_path):
    from scripts.ops import propose_cross_merchant_matches as script

    await _catalog(conn)
    await _product(conn, "ext:retailer:" + "9" * 32, merchant="m_other", title="Snail Mucin Essence 100ml",
                   brand="COSRX", ck="ck_cosrx")
    await _offer(conn, "ext:retailer:" + "9" * 32, "o_cosrx", merchant="m_other", price=20, host="c.example")
    snap, counts = await script.export_snapshot(conn, "US", brands=["Laneige"])
    assert {p["brand"] for p in snap.products} == {"Laneige", "LANEIGE"}       # COSRX filtered in SQL
    assert "ck_cosrx" not in snap.ck_rows and counts["candidate_content_keys"] == len(snap.ck_rows)
    assert {o["market"] for o in snap.offers} == {"US", "SG"}                  # every market, candidate keys only
    none, counts = await script.export_snapshot(conn, "US", brands=["Nobrand Cosmetics Example"])
    assert none.products == [] and counts["candidate_content_keys"] == 0
