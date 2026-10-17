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
                 market="US", currency=None):  # price None: an offer the market lists without a price
    from services.region_pricing import pricing_currency_for_region

    currency = currency or pricing_currency_for_region(market)  # the market's own (market_currency_mismatch)
    sku_key = f"{key}::v:{offer_id}"  # a per-variant SKU, as the crawl lane keys it
    await c.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id,"
        " title, sku_payload) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)",
        sku_key, key, merchant, SYN, _spid(key), offer_id, sku_title, json.dumps({"option1": sku_title}))
    await c.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, offer_type, market, availability,"
        " currency, merchant_effective_price, source_domain) VALUES ($1, $2, $3, $4, $5, $6, 'in_stock', $9, $7, $8)",
        offer_id, sku_key, key, merchant, offer_type, market, price, host, currency)


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
    # (priced in SGD: an SG offer in USD would hold Ulta as market_currency_mismatch)
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

    scope = [] if "--brands" in extra else ["--all-brands"]   # M3: a full scan only when named
    return script.parse_args(["--out", str(tmp_path), *scope, *extra])


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
    assert [line["level"] for line in out if line["section"] == "warning"] == ["WARN"]  # --all-brands
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
    # #2637 L3: the engine's writer refuses this strategy from anyone but its module (nothing written)
    from services.identity_resolution import upsert_proposals
    stray = {**rows[0], "proposal_id": "irp_stray", "proposal_key": "irk_stray"}
    for token in (None, "coordinator_merge"):
        with pytest.raises(ValueError):
            await upsert_proposals(c, [stray], module_writer=token)
    assert await c.fetchval("SELECT count(*) FROM identity_resolution_proposals WHERE proposal_id = 'irp_stray'") == 0
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
    assert all(o["size"] == {"value": 20.0, "unit": "g", "pack": 1} for o in mv["matched_offers"])  # "0.7 oz"
    seph_mv = next(r for r in by_kind["attach_membership"]
                   if r["evidence"]["listing_product_key"] == SEPH)["evidence"]["matcher_verdict"]
    assert [sorted(o) for o in seph_mv["matched_offers"]] == [["currency", "market", "offer_id", "price", "sku_key"]]
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
    from services.identity_cross_merchant_match import brand_preselect_regexes

    chunks = len(brand_preselect_regexes(["laneige"])["laneige"])
    assert [line["statement"] for line in lines] == ["brand_spellings"] * chunks + [
        "brand_blind_spot", *(n for n, _sql in script.PHASE1 + script.PHASE2)]
    assert all(line["section"] == "explain" and line["plan"] for line in lines)
    assert not any(p.name.endswith(".jsonl") for p in tmp_path.iterdir())


async def test_the_brand_filter_and_phase_two_read_only_the_candidate_keys(conn, tmp_path):
    from scripts.ops import propose_cross_merchant_matches as script

    await _catalog(conn)
    await _product(conn, "ext:retailer:" + "9" * 32, merchant="m_other", title="Snail Mucin Essence 100ml",
                   brand="COSRX", ck="ck_cosrx")
    await _offer(conn, "ext:retailer:" + "9" * 32, "o_cosrx", merchant="m_other", price=20, host="c.example")
    # M1: a COSRX row carrying the anchor's GTIN is read in phase 2 although the run is Laneige-only
    await conn.execute("UPDATE catalog_products SET gtin = '8809643066389' WHERE product_key = ANY($1::text[])",
                       [ANCHOR, "ext:retailer:" + "9" * 32])
    snap, counts = await script.export_snapshot(conn, "US", brands=["Laneige"])
    assert {p["brand"] for p in snap.products} == {"Laneige", "LANEIGE"}       # COSRX filtered in SQL
    assert {r["product_key"] for r in snap.gtin_rows} == {ANCHOR, "ext:retailer:" + "9" * 32}
    assert "ck_cosrx" not in snap.ck_rows and counts["candidate_content_keys"] == len(snap.ck_rows)
    assert counts["brand_spellings"] == ["LANEIGE", "Laneige"]
    assert {o["market"] for o in snap.offers} == {"US", "SG"}                  # every market, candidate keys only
    none, counts = await script.export_snapshot(conn, "US", brands=["Nobrand Cosmetics Example"])
    assert none.products == [] and counts["candidate_content_keys"] == 0


# --- M2 (controller 10-17): --brands finds every stored spelling ------------------------------------------

def _k(tag: str) -> str:
    return "ext:retailer:" + (tag * 32)[:32]


async def _spelling_catalog(c):
    """Three brands stored under the spellings a literal-token filter missed, brand stores included."""
    rows = [
        # Dr.Jart+: one store anchor + three retailers, each spelled differently
        ("ext:drjart-cicapair::s1", "Dr.Jart+", "Cicapair Tiger Grass Color Correcting Treatment 50ml", "ck_dj", True),
        (_k("a"), "DR JART", "Cicapair Tiger Grass Color Correcting Treatment 50 ml", "ck_dj_a", False),
        (_k("b"), "Dr. Jart+", "Cicapair Tiger Grass Color Correcting Treatment 50ml", "ck_dj_b", False),
        (_k("c"), "Dr.Jart", "Cicapair Tiger Grass Color Correcting Treatment 50ml", "ck_dj_c", False),
        # Dr.Jart+ Ceramidin: TWO store keys under odd spellings -> the full run holds multi_anchor
        ("ext:drjart-ceramidin::s1", "Dr. Jart+", "Ceramidin Cream 50ml", "ck_cer_1", True),
        ("ext:drjart-ceramidin::s2", "DR JART", "Ceramidin Cream 50ml", "ck_cer_2", True),
        (_k("d"), "Dr.Jart+", "Ceramidin Cream 50ml", "ck_cer_r", False),
        # L'Oréal: the store spelled with the accent, the retailer without
        ("ext:loreal-revitalift::s1", "L'Oréal", "Revitalift Night Cream 50ml", "ck_lo", True),
        (_k("e"), "L'Oreal", "Revitalift Night Cream 50ml", "ck_lo_e", False),
        # SK-II: the store "SK II", the retailer "SK-II"
        ("ext:skii-fte::s1", "SK II", "Facial Treatment Essence 230ml", "ck_sk", True),
        (_k("f"), "SK-II", "Facial Treatment Essence 230ml", "ck_sk_f", False),
        # a look-alike that must stay out of every scoped run
        (_k("g"), "Dr Jart Plus", "Cicapair Tiger Grass Color Correcting Treatment 50ml", "ck_djp", False),
        # short-run spellings behind ONE leading character: found by the pre-select (_PG_LEAD)
        (_k("i"), "(SK-II)", "Facial Treatment Essence 230ml", "ck_sk_i", False),
        (_k("j"), " SK-II", "Facial Treatment Essence 230ml", "ck_sk_j", False),
        ("ext:elf-primer::s1", "e.l.f.", "Power Grip Primer 24ml", "ck_elf_s", True),
        (_k("h"), "(e.l.f.)", "Power Grip Primer 24ml", "ck_elf", False),
    ]
    for n, (key, brand, title, ck, store) in enumerate(rows):
        merchant = f"m_store_{n}" if store else f"m_ret_{n}"
        await _product(c, key, merchant=merchant, title=title, brand=brand, ck=ck,
                       seed_kind="self" if store else None, created="2026-01-01" if store else "2026-05-01")
        await _offer(c, key, f"o_sp_{n}", merchant=merchant, price=30, host=f"h{n}.example",
                     offer_type="brand_direct" if store else "retailer")
    return rows


def _restricted(plan, family):
    from services.product_match_normalizer import canonical_brand

    keep = [p for p in plan.proposals if canonical_brand(p["evidence"]["anchor"]["brand"]) == family]
    return ([json.dumps(p, sort_keys=True, default=str) for p in keep],
            [o for o in plan.outcomes if o["brand"] == family])


async def test_m2_a_scoped_run_equals_the_full_run_restricted_to_the_brand(conn):
    from scripts.ops import propose_cross_merchant_matches as script
    from services.identity_cross_merchant_match import plan_market
    from services.product_match_normalizer import canonical_brand

    rows = await _spelling_catalog(conn)
    await _catalog(conn)  # Laneige too: a scoped run must not read it
    full_snap, _ = await script.export_snapshot(conn, "US")
    full = plan_market(full_snap, "US")
    for requested in ("drjart", "Dr.Jart+", "loreal", "sk-ii", "elf"):
        fam = canonical_brand(requested)
        snap, counts = await script.export_snapshot(conn, "US", brands=[requested])
        stores = {k for k, b, _t, _ck, store in rows if store and canonical_brand(b) == fam}
        assert stores <= {p["product_key"] for p in snap.products}, requested   # odd-spelled anchors read
        assert {canonical_brand(p["brand"]) for p in snap.products} == {fam}     # nothing else
        assert set(counts["brand_spellings"]) == {b for _k_, b, *_x in rows if canonical_brand(b) == fam}
        scoped = plan_market(snap, "US", brands=[requested])
        assert _restricted(scoped, fam) == _restricted(full, fam), requested
        assert scoped.proposals, requested
    dj = plan_market((await script.export_snapshot(conn, "US", brands=["drjart"]))[0], "US", brands=["drjart"])
    held = {o["product_key"]: o["reason"] for o in dj.outcomes}
    assert held[_k("d")] == "multi_anchor_listing"   # both Ceramidin store keys were read, and it matches both
    assert {p["evidence"]["keeper_product_key"] for p in dj.proposals} == {"ext:drjart-cicapair::s1"}
    assert sorted(p["evidence"]["listing_product_key"] for p in dj.proposals) == sorted([_k("a"), _k("b"), _k("c")])
    assert _k("g") not in {p["product_key"] for p in
                           (await script.export_snapshot(conn, "US", brands=["drjart"]))[0].products}


def _bitmap_rows(plan_lines):
    """The Bitmap Index Scan's ACTUAL row count on idx_catalog_products_brand_trgm, or None. A regex
    pg_trgm extracts no trigram from still shows an Index Cond with seq scans disabled -- it scans
    the WHOLE index and returns every row -- so the count, not the plan shape, is the proof."""
    import re as _re

    for line in plan_lines:
        if "Bitmap Index Scan on idx_catalog_products_brand_trgm" in line:
            return int(_re.search(r"actual time=\S+ rows=(\d+)", line).group(1))
    return None


async def test_m2_the_brand_statements_are_served_by_the_trigram_index(conn):
    """Planned and RUN against the index migration 059 defines (created here in the scratch schema,
    never a new index) over 3000 filler brands: every phase-0 family regex and phase 1's brand clause
    must be a bitmap scan on it that returns a handful of rows, never the table."""
    from scripts.ops import propose_cross_merchant_matches as script
    from services.identity_cross_merchant_match import brand_exact_regex, brand_preselect_regexes

    ext_schema = await conn.fetchval("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n"
                                     " ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'")
    if ext_schema is None:
        await conn.execute(f"CREATE EXTENSION pg_trgm SCHEMA {_SCHEMA}")
        ext_schema = _SCHEMA
    ddl = next(stmt for stmt in (_MIGRATIONS / "059_catalog_pivot_search_indexes.sql").read_text().split(";")
               if "idx_catalog_products_brand_trgm" in stmt)
    ddl = ddl.replace("CONCURRENTLY ", "").replace("gin_trgm_ops", f"{ext_schema}.gin_trgm_ops")
    await conn.execute(ddl)
    await _spelling_catalog(conn)
    await conn.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, brand)"
        " SELECT 'filler:' || i, 'm_f', 'p', 'f' || i, 'T', 'qx' || lpad(i::text, 6, '0') || ' wq'"
        " FROM generate_series(1, 3000) i")
    await conn.execute("ANALYZE catalog_products")
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        families = ["drjart", "loreal", "sk-ii", "laneige", "dior", "vt", "elf", "ysl", "cledepeaubeaute",
                    "beautyofjoseon", "decorte", "etude", "esteelauder"]
        for fam, regexes in brand_preselect_regexes(families).items():
            for regex in regexes:
                plan = [r[0] for r in await conn.fetch("EXPLAIN (ANALYZE) " + script.BRAND_SPELLINGS_SQL, regex)]
                rows = _bitmap_rows(plan)
                assert rows is not None and rows < 20, (fam, rows, plan)
        spellings = [("Dr.Jart+", "dr.jart+"), ("SK II", "sk ii"), ("(SK-II)", "(sk-ii)"), ("L'Oréal", "l'oréal")]
        # Phase 1's brand clause on its own (the full statement's join order depends on catalog_offers,
        # whose indexes this scratch schema does not build).
        clause = script._BRAND_CLAUSE.replace("$2", "$1").replace("$3", "$2")
        plan = [r[0] for r in await conn.fetch(
            "EXPLAIN (ANALYZE) SELECT p.product_key FROM catalog_products p WHERE TRUE" + clause,
            brand_exact_regex(lo for _b, lo in spellings), [b for b, _lo in spellings])]
        rows = _bitmap_rows(plan)
        assert rows is not None and rows < 20, (rows, plan)
        assert script._BRAND_CLAUSE in script.PRODUCTS_BRAND_SQL and script._BRAND_CLAUSE in script.HOSTS_BRAND_SQL


@pytest.mark.parametrize("blind", ["ＳＫ-ＩＩ", "((SK-II))"])
async def test_m2_a_scoped_run_with_a_spelling_the_preselect_cannot_see_is_refused(conn, tmp_path, capsys, blind):
    from scripts.ops import propose_cross_merchant_matches as script

    await _spelling_catalog(conn)
    await _product(conn, _k("9"), merchant="m_blind", title="Facial Treatment Essence 230ml", brand=blind,
                   ck="ck_sk_blind")
    await _offer(conn, _k("9"), "o_blind", merchant="m_blind", price=30, host="jp.example")
    with pytest.raises(script.Refused) as exc:
        await script.export_snapshot(conn, "US", brands=["sk-ii"])
    assert blind in str(exc.value) and "--all-brands" in str(exc.value) and "1 stored spelling" in str(exc.value)
    assert await script.run(conn, _args(tmp_path, "--brands", "sk-ii")) == 2
    refused = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["section"] for line in refused] == ["refused"] and blind in refused[0]["reason"]
    assert not any(p.name.endswith(".jsonl") for p in tmp_path.iterdir())
    # other families, with only spellings the pre-select sees, still run; so does the full run
    snap, _ = await script.export_snapshot(conn, "US", brands=["drjart"])
    assert snap.products and blind not in {p["brand"] for p in snap.products}
    full, _ = await script.export_snapshot(conn, "US")
    assert blind in {p["brand"] for p in full.products}


async def test_m2_the_blind_spot_sql_is_the_python_predicate(conn):
    from scripts.ops import propose_cross_merchant_matches as script
    from services.identity_cross_merchant_match import in_brand_blind_spot

    samples = ["ＳＫ-ＩＩ", "(SK-II)", " SK-II", "'SK II'", "[VT] Cosmetics", "(e.l.f.)", "L'Oréal", "SKⅡ",
               "SK-II", "SK II", "Dr.Jart+", "e.l.f.", "VT Cosmetics", "Laneige", "1004 Lab"]
    for n, b in enumerate(samples):
        await _product(conn, _k(f"{n:x}") + "z", merchant=f"m_s{n}", title="T", brand=b, ck=f"ck_s{n}")
    got = {r["brand"] for r in await conn.fetch(script.BRAND_BLIND_SPOT_SQL)}
    assert got == {b for b in samples if in_brand_blind_spot(b)}


@pytest.mark.parametrize("requested, found, refused", [
    ("sk-ii", [" SK-II", "'SK II'", "(SK-II)", "SK-II"], ["ＳＫ-ＩＩ"]),
    ("vt", ["[VT] Cosmetics", " VT Cosmetics"], []),
    ("elf", [" e.l.f.", "(e.l.f.)", "e.l.f."], []),
    ("shiseido", ["Shiseido"], ["ＳＨＩＳＥＩＤＯ"]),
    ("laneige", ["LANEIGE"], ["Ｌａｎｅｉｇｅ"]),
])
async def test_m2_the_controllers_spellings_are_found_on_postgres_or_refused(conn, requested, found, refused):
    """The real PostgreSQL regexes (not a Python emulation): each listed spelling is found by phase 0,
    or -- full-width, which the pre-select does not index -- refused by the blind-spot guard by name.
    Look-alikes are never chosen."""
    from scripts.ops import propose_cross_merchant_matches as script

    lookalikes = ["SKIN1004", "Skinfood", "VTech", "Elfbar", "(e.l.f. x Disney)", "Shiseido Men", "[Laneige Homme]"]
    for n, b in enumerate([*found, *lookalikes]):
        await _product(conn, f"ext:sp:{n}", merchant=f"m_sp{n}", title="T", brand=b, ck=f"ck_sp{n}")
    async with conn.transaction(readonly=True):
        got = await script.brand_spellings(conn, [requested])
    assert sorted(b for b, _lo in got) == sorted(found)
    for n, b in enumerate(refused):
        await _product(conn, f"ext:spr:{n}", merchant=f"m_spr{n}", title="T", brand=b, ck=f"ck_spr{n}")
    if refused:
        with pytest.raises(script.Refused) as exc:
            async with conn.transaction(readonly=True):
                await script.brand_spellings(conn, [requested])
        assert all(b in str(exc.value) for b in refused)
