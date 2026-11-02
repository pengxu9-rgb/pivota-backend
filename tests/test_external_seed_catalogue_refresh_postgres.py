"""The catalogue price lane end to end on the production dialect: the real batch, the real refresh.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_catalogue_refresh_postgres.py

tests/test_external_seed_catalogue_refresh.py proves the lane's rules on a stubbed DB. This file proves
what a stub cannot, on Postgres, with every row written by its real producer:

  * the seed is the retailer-ingest drain's (`curated_brand_feed.shopify_product_to_record` ->
    `ingest_validated_jsonl`), written by the drain's own upsert (`apply._SEED_UPSERT_SQL`);
  * `run_external_referral_refresh_batch` runs with the lane ON: its candidate query, host and
    destination lookups, the catalogue read (the real `read_host_catalogue` over an httpx MockTransport
    that serves the same products.json product the drain ingested, now re-priced), and the job's own
    refresh callback;
  * the refresh's UPDATE runs for real: `seed_data` jsonb with the re-priced variants and the
    `extracted_at` stamp, `price_amount` / `price_currency`, `last_crawled_at` through
    `CASE WHEN CAST(:read_the_served_product AS BOOLEAN)`, `last_crawl_attempt_at`, and
    `title = COALESCE(title, :title)` keeping the stored title against the lane's NULL;
  * the liveness writer's CASE over `destination_verdict` / `destination_checked_at` /
    `destination_http_status` for the sweep's "listed in products.json" observation (no HTTP status).

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches `public`:
a per-process scratch schema, the real migrations applied there through a connection whose search_path
is that schema alone, dropped at teardown.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql",
    "202_external_seed_content_freshness.sql",
    "266_external_seed_destination_corroborated_dead_at.sql",
    # Hand-applied before the lane is switched on: the page read the lane carries across its own reads.
    "274_external_seed_last_page_read_at.sql.disabled",
    # catalog_products: the candidate query's suppressed-product anti-join and the liveness writer's
    # mirror lookup read it.
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    # The candidate query skips quarantined sources.
    "134_catalog_source_quarantine.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"catalogue_refresh_test_{os.getpid()}"

HOST = "brand.example"
HANDLE = "example-eau-de-toilette"
V50, V15 = "37169662034071", "41430446375063"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


def _product(*, p50: str = "80.00", p15: str = "32.00", a15: bool = True) -> Dict[str, Any]:
    """The products.json product: what the drain ingests and what the catalogue serves back."""
    return {
        "id": 6644068500001, "vendor": "Example", "title": "Example Eau De Toilette", "handle": HANDLE,
        "product_type": "Fragrance", "body_html": "<p>A fragrance.</p>",
        "images": [{"src": "https://cdn.brand.example/a.jpg"}], "options": [{"name": "Size"}],
        "variants": [
            {"id": int(V50), "title": "1.7 fl. oz. / 50 mL", "price": p50, "available": True, "sku": "EX50",
             "option1": "1.7 fl. oz. / 50 mL"},
            {"id": int(V15), "title": "0.5 fl. oz. / 15 mL", "price": p15, "available": a15, "sku": "EX15",
             "option1": "0.5 fl. oz. / 15 mL"},
        ],
    }


def _other(i: int) -> Dict[str, Any]:
    return {"id": 9000 + i, "title": f"Other {i}", "handle": f"other-{i}",
            "variants": [{"id": 8000000000 + i, "title": "Default Title", "price": "10.00", "available": True}]}


def _drain_seed(product: Dict[str, Any], *, handle: str = HANDLE) -> Dict[str, Any]:
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl

    record = feed.shopify_product_to_record(
        dict(product, handle=handle), domain=HOST, category_path="beauty", brand_override="Example",
        currency="USD", source_role="brand_official", retailer_name=None, emit_native_variants=True)
    seed = dict(ingest_validated_jsonl([record])["seeds"][0])
    seed.setdefault("seller_ref", None)
    seed.setdefault("seed_kind", None)
    if not isinstance(seed["seed_data"], str):
        seed["seed_data"] = json.dumps(seed["seed_data"])
    return seed


async def _write_seed(db, seed: Dict[str, Any]) -> None:
    from services.catalog_enrichment_agent.apply import _SEED_UPSERT_SQL

    await db.execute(_SEED_UPSERT_SQL, {k: seed.get(k) for k in (
        "id", "external_product_id", "market", "tool", "title", "image_url", "price_amount", "price_currency",
        "destination_url", "canonical_url", "domain", "attached_product_key", "status", "availability",
        "seed_data", "seller_ref", "seed_kind")})


def _store(pages):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/meta.json":
            return httpx.Response(200, json={"name": "Example", "currency": "USD"})
        if path == "/products.json":
            page = int(request.url.params.get("page") or 1)
            products = pages[page - 1] if page <= len(pages) else []
            return httpx.Response(200, json={"products": products},
                                  headers=[("set-cookie", "cart_currency=USD; path=/")])
        return httpx.Response(404, text="")

    return handler


async def _row(db, seed_id: str) -> Dict[str, Any]:
    row = dict(await db.fetch_one("SELECT * FROM external_product_seeds WHERE id = :id", {"id": seed_id}))
    if isinstance(row.get("seed_data"), str):
        row["seed_data"] = json.loads(row["seed_data"])
    return row


async def _page_read_days_ago(db, seed_ids, days: int) -> None:
    """The stamp a refresh's page read left (every refresh before the lane): `last_crawled_at`."""
    for seed_id in seed_ids:
        await db.execute(
            "UPDATE external_product_seeds SET last_crawled_at = NOW() - make_interval(days => :days) WHERE id = :id",
            {"id": seed_id, "days": days})


async def _run_batch(monkeypatch, db, seed_ids, pages) -> Dict[str, Any]:
    import jobs.external_referral_refresh as job
    import routes.employee_products as routes_mod
    import services.external_referral_readiness as err
    from services import crawl_identity
    from services import crawl_politeness as cp
    from services import external_seed_destination_liveness as liveness

    for mod in (routes_mod, err, liveness):
        monkeypatch.setattr(mod, "database", db)

    async def no_ddl() -> None:
        return None

    async def allow(url, *, user_agent, max_wait=None):
        return None

    monkeypatch.setattr(routes_mod, "_ensure_external_seeds_table", no_ddl)
    monkeypatch.setattr(cp, "before_request", allow)
    monkeypatch.setattr(cp, "note_response", lambda *a, **k: None)
    transport = httpx.MockTransport(_store(pages))
    monkeypatch.setattr(crawl_identity, "transport_kwargs", lambda inner=None: {"transport": transport})
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED", "true")
    monkeypatch.setenv("CRAWL_GLOBAL_PACER_DISABLED", "true")
    monkeypatch.delenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", raising=False)
    cp.reset_for_tests()
    return await err.run_external_referral_refresh_batch(
        refresh_seed_by_id=job._refresh_unbounded, candidate_seed_ids=seed_ids, market="US", limit=len(seed_ids),
    )


async def test_the_batch_prices_the_drains_seeds_off_the_catalogue_on_postgres(scoped_db, monkeypatch):
    seeds = [_drain_seed(_product(), handle=h) for h in (HANDLE, "other-a", "other-b")]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    target = seeds[0]["id"]
    # Page-read 20 days ago (inside the 30-day maximum), live then, with an HTTP status from that read.
    await _page_read_days_ago(scoped_db, [s["id"] for s in seeds], 20)
    await scoped_db.execute(
        "UPDATE external_product_seeds SET destination_verdict = 'live', destination_http_status = 200,"
        " destination_checked_at = NOW() - interval '20 days' WHERE id = :id", {"id": target})
    before = await _row(scoped_db, target)
    assert [v["variant_id"] for v in before["seed_data"]["variants"]] == [V50, V15]

    pages = [[_other(1), _product(p50="85.00", p15="34.00", a15=False)],
             [dict(_product(p50="61.00"), handle="other-a"), dict(_product(p50="62.00"), handle="other-b")]]
    summary = await _run_batch(monkeypatch, scoped_db, [s["id"] for s in seeds], pages)

    cat = summary["catalogue"]
    assert (cat["hosts_read"], cat["rows_read"], cat["pages_read"]) == (1, 3, 2), cat
    assert cat["host_status"] == {"ok": 1} and summary["refreshed"] == 3 and summary["failed"] == 0

    after = await _row(scoped_db, target)
    assert (float(after["price_amount"]), after["price_currency"]) == (85.0, "USD")
    by_id = {v["variant_id"]: v for v in after["seed_data"]["variants"]}
    assert (by_id[V50]["price_amount"], by_id[V15]["price_amount"]) == (85.0, 34.0)
    assert (by_id[V15]["availability"], by_id[V15]["in_stock"]) == ("out_of_stock", False)
    snapshot = after["seed_data"]["snapshot"]
    assert snapshot["variant_refresh"]["status"] == "all_re_read"
    assert snapshot["read_source"] == "products_json"
    # The page read 20 days ago, carried out of last_crawled_at as the catalogue read replaced it.
    assert after["last_page_read_at"] == before["last_crawled_at"]
    assert datetime.fromisoformat(snapshot["extracted_at"]) > datetime.now(timezone.utc) - timedelta(minutes=5)
    # The stamps the queue and the freshness gate read.
    now = datetime.now(timezone.utc)
    assert now - after["last_crawled_at"] < timedelta(minutes=5)
    assert now - after["last_crawl_attempt_at"] < timedelta(minutes=5)
    # COALESCE kept the drain's title and image against the lane's NULLs; the served URL did not move.
    for col in ("title", "image_url", "canonical_url", "destination_url", "domain"):
        assert after[col] == before[col], col
    for key in ("title", "image_urls", "product_name", "brand"):
        assert after["seed_data"].get(key) == before["seed_data"].get(key), key
    # The sweep's listed observation: live, checked now, no HTTP status invented.
    assert after["destination_verdict"] == "live"
    assert after["destination_http_status"] is None
    assert after["destination_failure_streak"] == 0
    assert now - after["destination_checked_at"] < timedelta(minutes=5)


async def test_a_seed_due_its_page_read_is_page_read_on_postgres(scoped_db, monkeypatch):
    """Review P2-2(a): a destination with a failure streak, and one whose last PAGE read is older than the
    maximum, get the page read the catalogue cannot replace; the rest are priced off the catalogue."""
    import routes.employee_products as routes_mod

    handles = (HANDLE, "other-a", "other-b", "dying", "long-unread")
    seeds = [_drain_seed(_product(), handle=h) for h in handles]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    await _page_read_days_ago(scoped_db, [s["id"] for s in seeds], 20)
    dying, unread = seeds[3]["id"], seeds[4]["id"]
    await scoped_db.execute(
        "UPDATE external_product_seeds SET destination_verdict = 'dead_404', destination_http_status = 404,"
        " destination_failure_streak = 1 WHERE id = :id", {"id": dying})
    # The lane read it last night; its last PAGE read was 40 days ago.
    await scoped_db.execute(
        "UPDATE external_product_seeds SET last_crawled_at = NOW(),"
        " last_page_read_at = NOW() - interval '40 days' WHERE id = :id", {"id": unread})
    page_reads = []

    async def page_read(*, url, **_kw):
        page_reads.append(url)
        raise RuntimeError("page read (stubbed)")

    monkeypatch.setattr(routes_mod, "resolve_external_offer", page_read)
    pages = [[dict(_product(), handle=h) for h in handles]]
    summary = await _run_batch(monkeypatch, scoped_db, [s["id"] for s in seeds], pages)

    cat = summary["catalogue"]
    assert cat["rows_read"] == 3 and cat["rows"]["read"] == 3, cat
    assert cat["rows"]["page_read_due:destination_failure_streak"] == 1
    assert cat["rows"]["page_read_due:page_read_too_old"] == 1
    assert sorted(page_reads) == sorted(s["destination_url"] for s in seeds[3:])
    after = await _row(scoped_db, dying)
    assert (after["destination_verdict"], after["destination_failure_streak"]) == ("dead_404", 1), \
        "a listing never resets a dead streak"


async def test_the_drains_rewrite_never_hides_a_page_read_the_lane_owes_on_postgres(scoped_db, monkeypatch):
    """Review round 2: the drain's upsert replaces seed_data whole about every five days. The page read a
    catalogue read carried forward lives in a column that upsert never names, so it still comes due."""
    import routes.employee_products as routes_mod
    from services import external_seed_catalogue_refresh as lane

    handles = (HANDLE, "other-a", "other-b", "other-c")   # one more than the minimum, so one can come due
    seeds = [_drain_seed(_product(), handle=h) for h in handles]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    ids = [s["id"] for s in seeds]
    await _page_read_days_ago(scoped_db, ids, 20)
    pages = [[dict(_product(p50="85.00"), handle=h) for h in handles]]
    assert (await _run_batch(monkeypatch, scoped_db, ids, pages))["catalogue"]["rows_read"] == 4
    # The drain re-applies its cohort: same rows, its own seed_data, no snapshot.
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    row = await _row(scoped_db, ids[0])
    assert "snapshot" not in row["seed_data"]
    now = datetime.now(timezone.utc)
    assert now - row["last_crawled_at"] < timedelta(minutes=5), "the catalogue read set last_crawled_at"
    assert lane.page_read_due(row, now=now, max_age_days=30) is None
    assert lane.page_read_due(row, now=now + timedelta(days=11), max_age_days=30) == lane.DUE_TOO_OLD

    # Eleven days on, the next run reads that seed's page and stamps it.
    await scoped_db.execute(
        "UPDATE external_product_seeds SET last_crawled_at = last_crawled_at - interval '11 days',"
        " last_page_read_at = last_page_read_at - interval '11 days' WHERE id = :id", {"id": ids[0]})
    page_reads = []
    real_resolve = routes_mod.resolve_external_offer

    async def page_read(*, url, **kw):
        page_reads.append(url)
        raise RuntimeError("page read (stubbed)")

    monkeypatch.setattr(routes_mod, "resolve_external_offer", page_read)
    try:
        summary = await _run_batch(monkeypatch, scoped_db, ids, pages)
    finally:
        monkeypatch.setattr(routes_mod, "resolve_external_offer", real_resolve)
    assert summary["catalogue"]["rows"].get("page_read_due:page_read_too_old") == 1, summary["catalogue"]
    assert summary["catalogue"]["rows_read"] == 3
    assert page_reads == [seeds[0]["destination_url"]]


async def test_switched_on_without_the_column_the_lane_stays_off_on_postgres(scoped_db, monkeypatch):
    import routes.employee_products as routes_mod

    await scoped_db.execute("ALTER TABLE external_product_seeds DROP COLUMN last_page_read_at")
    seeds = [_drain_seed(_product(), handle=h) for h in (HANDLE, "other-a", "other-b")]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    await _page_read_days_ago(scoped_db, [s["id"] for s in seeds], 20)
    page_reads = []

    async def page_read(*, url, **_kw):
        page_reads.append(url)
        raise RuntimeError("page read (stubbed)")

    monkeypatch.setattr(routes_mod, "resolve_external_offer", page_read)
    summary = await _run_batch(monkeypatch, scoped_db, [s["id"] for s in seeds], [[_product()]])
    cat = summary["catalogue"]
    assert cat["page_read_column_missing"] is True and cat["enabled"] is False and cat["hosts_read"] == 0
    assert sorted(page_reads) == sorted(s["destination_url"] for s in seeds)


async def test_flag_off_the_batch_never_reads_the_catalogue_on_postgres(scoped_db, monkeypatch):
    seeds = [_drain_seed(_product(), handle=h) for h in (HANDLE, "other-a", "other-b")]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    asked = []

    async def page_read(seed_id, **kwargs):
        asked.append((seed_id, kwargs))
        return {"status": "success", "price_refresh": {"status": "unchanged"}, "projection": {}}

    import services.external_referral_readiness as err

    await _run_batch(monkeypatch, scoped_db, [s["id"] for s in seeds], [[_product()]])
    monkeypatch.setenv("EXTERNAL_REFERRAL_REFRESH_CATALOGUE_ENABLED", "false")
    summary = await err.run_external_referral_refresh_batch(
        refresh_seed_by_id=page_read, candidate_seed_ids=[s["id"] for s in seeds], market="US", limit=3)
    assert summary["catalogue"]["hosts_planned"] == 0 and summary["catalogue"]["hosts_read"] == 0
    assert sorted(a[0] for a in asked) == sorted(s["id"] for s in seeds) and all(a[1] == {} for a in asked)


async def test_the_dry_run_plans_the_drains_seeds_and_writes_nothing_on_postgres(scoped_db, monkeypatch, capsys):
    """`scripts/ops/plan_shopify_catalogue_refresh.py` end to end: its READ ONLY reads on this schema, the
    lane's reader over the mock storefront, one `host` line, and not a byte written."""
    import importlib.util

    from services import crawl_identity
    from services import crawl_politeness as cp

    seeds = [_drain_seed(_product(), handle=h) for h in (HANDLE, "other-a", "other-b", "gone")]
    for seed in seeds:
        await _write_seed(scoped_db, seed)
    # One the lane read last night, whose last page read (carried in the column) is 40 days old.
    await scoped_db.execute(
        "UPDATE external_product_seeds SET last_crawled_at = NOW(), last_page_read_at = NOW() - interval '40 days'"
        " WHERE id = :id", {"id": seeds[1]["id"]})
    snapshot_before = [dict(r) for r in await scoped_db.fetch_all(
        "SELECT id, price_amount, seed_data::text AS sd, updated_at, last_crawled_at, destination_checked_at"
        " FROM external_product_seeds ORDER BY id")]

    script = Path(__file__).resolve().parent.parent / "scripts/ops/plan_shopify_catalogue_refresh.py"
    spec = importlib.util.spec_from_file_location("plan_scr_pg", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    pages = [[_other(1), _product(p50="90.00")],
             [dict(_product(), handle="other-a"), dict(_product(p50="81.00"), handle="other-b")]]
    transport = httpx.MockTransport(_store(pages))
    monkeypatch.setattr(crawl_identity, "transport_kwargs", lambda inner=None: {"transport": transport})
    # The REAL gate, so every request takes a slot on the script's global pacer and is counted; no robots.txt
    # fetch and no per-host interval, so nothing leaves the mock transport and nothing waits but the pacer.
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0")
    cp.reset_for_tests()
    monkeypatch.setattr(mod, "_now_utc", lambda: datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setenv("PIVOTA_ENV", "production")
    monkeypatch.setenv("CATALOGUE_PLAN_EGRESS", "pivota-crawl")
    monkeypatch.setenv("CLOUD_RUN_JOB", "plan-shopify-catalogue-refresh")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("PLAN_RPS", "1.0")
    # asyncpg passes an unknown DSN parameter as a server setting: the script's own connection reads this schema.
    monkeypatch.setenv("DATABASE_URL", _url() + f"?search_path={_SCHEMA}")
    _ = capsys.readouterr()
    await mod.main()
    out = capsys.readouterr().out.splitlines()

    assert out[0] == "GATE_OPEN"
    lines = [line for line in out if line.startswith("SCR_PLAN ")]
    assert lines[-1] == f"SCR_PLAN DONE lines={len(lines) - 1}"
    payload = {line.split(" ", 2)[1]: json.loads(line.split(" ", 2)[2]) for line in lines[:-1]}
    assert payload["hosts"] == {HOST: 4} and payload["page_read_column"] is True
    host = payload["host"]
    assert (host["status"], host["currency"], host["proof"]) == ("ok", "USD", "base_and_page_cookie")
    assert (host["seeds"], host["matched"]) == (4, 3)
    assert host["reasons"] == {"read": 3, "handle_not_read": 1}
    assert host["price"] == {"agree": 1, "moved_within_2pct": 1, "moved_over_10pct": 1}
    assert host["variants"] == {"all_re_read": 3}
    # Stored 80.00 against 90.00, 80.00 and 81.00: drift on one seed, no constant offset.
    assert host["price_ratio"] == {"n": 3, "median": 1.0125, "p10": 1.0, "p90": 1.125, "offset_suspected": False}
    # The drain's rows were never page-read: the lane would read their pages first.
    assert host["page_read_due"] == {"page_read_unknown": 3, "page_read_too_old": 1}
    summary = payload["summary"]
    assert (summary["hosts_read"], summary["hosts_priceable"], summary["matched"]) == (1, 1, 3)
    # meta.json + 3 catalogue pages (the third, empty, ends it: "gone" is not listed).
    assert summary["origin_requests"] == 4 and summary["requests_per_matched_seed"] == 1.333
    # Every seed here is due its page read, which the job gives it before any guard: nothing is counted guarded.
    assert host["move_guarded"] == 0 and summary["move_guarded"] == 0
    assert summary["go_no_go"] == {"not_all_re_read_share": 0.0, "moved_over_10pct_share": 0.3333,
                                   "move_guarded_share": 0.0,
                                   "offset_suspected_hosts": 0, "hosts_priced_on_cart_proof": 0}

    after = [dict(r) for r in await scoped_db.fetch_all(
        "SELECT id, price_amount, seed_data::text AS sd, updated_at, last_crawled_at, destination_checked_at"
        " FROM external_product_seeds ORDER BY id")]
    assert after == snapshot_before, "a dry run writes nothing"

    # Before migration 274 is applied the dry run still runs, every page read on last_crawled_at.
    await scoped_db.execute("ALTER TABLE external_product_seeds DROP COLUMN last_page_read_at")
    cp.reset_for_tests()
    await mod.main()
    out = capsys.readouterr().out.splitlines()
    payload = {line.split(" ", 2)[1]: json.loads(line.split(" ", 2)[2])
               for line in out if line.startswith("SCR_PLAN ") and not line.startswith("SCR_PLAN DONE")}
    # Without the column the lane's own last_crawled_at would pass for a page read: the column is the check.
    assert payload["page_read_column"] is False and payload["host"]["page_read_due"] == {"page_read_unknown": 3}
