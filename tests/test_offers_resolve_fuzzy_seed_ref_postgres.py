"""offers.resolve's external_seed_by_fuzzy_ref statement on POSTGRES, against prod's trigram indexes.

The statement ORs, per product alias, `external_product_id = :pid` with LIKE arms over title, the two
urls and seed_data::text. Until 2026-10-19 its arms read LOWER(COALESCE(title,'')) and the like, which
match no index: every call that reached it scanned the 31k-row, 534 MB table (6.7-7.3 s measured on
prod) and lost the seed lane to the 1.2 s timeout. Prod has status-only trigram GINs on exactly
lower(title), lower(canonical_url), lower(destination_url) and lower(seed_data::text) (migration 270
records them), so the arms are now spelled the way those indexes are keyed, and an id-shaped ref skips
the title arm.

Two things are proved here, through the REAL handler and the statement it sends:
  * an id-shaped ref (sig_ / ext_ / uuid) is planned as bitmap probes on those indexes, never a Seq Scan,
    and never probes the title index;
  * every ref returns the SAME rows main's statement (b86afa82c, frozen below) returns, including the
    NULL-title / NULL-url rows and the all-'%' ref the COALESCE removal could in principle change.

The identity retry's statement (external_seed_by_internal_identity: the internal product's title
terms, LIKE over the seed title and seed_data->>'title', brand terms as a filter) had the same
disease: LOWER(COALESCE(title,'')) and an unindexed seed_data->>'title'. Its title side is now
LOWER(title) and its seed_data side is served by migration 271's index on that exact expression; the
same two proofs (no scan, same rows as main) are made for it below.

Rows are the real seed shape (tests/fixtures/judydoll_silky_matte_lip_ink_seed_2026_09_29.json, a
staging row read 2026-09-29) plus filler built from it, attached as prod's rows are (26,945 of 26,950
active rows carry an attached_product_key, 2026-10-19). Runs in its own scratch schema, alone on the
handler's search_path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG, reason="requires a real Postgres DATABASE_URL (postgres dialect gate)"
)

_SCHEMA = f"offers_fuzzy_ref_{uuid.uuid4().hex[:8]}"
_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "db" / "migrations"
_FIXTURE = json.loads((_ROOT / "tests/fixtures/judydoll_silky_matte_lip_ink_seed_2026_09_29.json").read_text())
_FILLER = 31000  # prod: 26,950 active of ~31k rows (2026-10-19); the planner's choice depends on it

SIG_MISS = "sig_" + "3f" * 16  # the common agent path: a signature with no seed behind it
EXT_HIT = "ext_0f95730ee5ba05a6b7957ada"  # the fixture row's own external id
EXT_CROSS = "ext_" + "c1" * 12  # named only inside ANOTHER seed's seed_data
UUID_REF = "7d0c6a8e-3b9f-4c1a-9e2d-5f4b3a2c1d0e"  # a Wix-style product id, inside one seed's seed_data
VARIANT_ID = _FIXTURE["seed_data"]["variants"][0]["id"]


def _main_fuzzy_clauses(product_id_aliases, sku_id_aliases):
    """main @ b86afa82c, verbatim: the statement this PR replaces. The yardstick for "same rows"."""
    from routes.agent_shop_gateway import _safe_lower

    clauses, params = [], {}
    if product_id_aliases:
        pid_clause = []
        for idx, pid_alias in enumerate(product_id_aliases[:8]):
            pid_key, like_key = f"pid_{idx}", f"pid_like_{idx}"
            params[pid_key] = pid_alias
            params[like_key] = f"%{_safe_lower(pid_alias)}%"
            pid_clause.append(
                "("
                f"external_product_id = :{pid_key}"
                f" OR LOWER(COALESCE(title,'')) LIKE :{like_key}"
                f" OR LOWER(COALESCE(canonical_url,'')) LIKE :{like_key}"
                f" OR LOWER(COALESCE(destination_url,'')) LIKE :{like_key}"
                f" OR LOWER(CAST(seed_data AS TEXT)) LIKE :{like_key}"
                ")"
            )
        clauses.append("(" + " OR ".join(pid_clause) + ")")
    if sku_id_aliases:
        sku_clause = []
        for idx, sku_alias in enumerate(sku_id_aliases[:8]):
            key = f"sku_like_{idx}"
            params[key] = f"%{_safe_lower(sku_alias)}%"
            sku_clause.append(f"LOWER(CAST(seed_data AS TEXT)) LIKE :{key}")
        clauses.append("(" + " OR ".join(sku_clause) + ")")
    return clauses, params


def _async_url() -> str:
    return DATABASE_URL


@pytest.fixture(scope="module")
def schema_engine():
    import db.catalog  # noqa: F401
    import db.products  # noqa: F401
    from sqlalchemy import create_engine, text

    from db.database import metadata
    from db.sql_migrations import split_statements

    admin = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE SCHEMA "{_SCHEMA}"'))
        ext_schema = c.execute(text("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n"
                                    " ON n.oid = e.extnamespace WHERE e.extname = 'pg_trgm'")).scalar()
        if ext_schema is None:
            c.execute(text(f'CREATE EXTENSION pg_trgm SCHEMA "{_SCHEMA}"'))
            ext_schema = _SCHEMA
    engine = create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT",
                           connect_args={"options": f"-csearch_path={_SCHEMA}"})
    try:
        # The lanes that run before the fuzzy statement read these (the sig_ mapping arm reads
        # catalog_products, the canonical prefetch reads products_cache); empty, they answer nothing.
        metadata.create_all(engine, tables=[metadata.tables[t] for t in (
            "catalog_products", "catalog_offers", "catalog_merchants", "catalog_skus", "products_cache")])
        with engine.connect() as c:
            # 045: the internal lane reads product_group_members for the identity retry's product.
            for f in ("044_external_product_seeds.sql", "045_product_groups.sql", "134_catalog_source_quarantine.sql"):
                c.exec_driver_sql((_MIGRATIONS / f).read_text())
        _load_rows(engine)
        with engine.connect() as c:
            # Migration 270's own statements, AFTER the rows (prod built them by hand on a full table),
            # one at a time on autocommit (CONCURRENTLY), as the out-of-band build runs them. The
            # trigram opclass resolves wherever pg_trgm lives.
            for name in ("270_external_seed_active_any_trgm_indexes.sql.disabled",
                         "271_external_seed_active_seed_title_trgm_index.sql.disabled"):
                for stmt in split_statements((_MIGRATIONS / name).read_text()):
                    if "CREATE EXTENSION" in stmt:
                        continue
                    c.exec_driver_sql(stmt.replace("gin_trgm_ops", f'"{ext_schema}".gin_trgm_ops'))
            c.execute(text("ANALYZE external_product_seeds"))
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP SCHEMA "{_SCHEMA}" CASCADE'))
        admin.dispose()


def _insert(c, *, id, epid, title, canonical, destination, seed_data, attached, minutes_ago, status="active",
            domain="judydoll.com", currency="USD"):
    from sqlalchemy import text

    c.execute(text(
        "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url, canonical_url,"
        " domain, title, price_amount, price_currency, availability, seed_data, status, attached_product_key,"
        " created_at, updated_at)"
        " VALUES (:id, :epid, 'US', '*', :dest, :canon, :domain, :title, 13.99, :cur, 'in_stock',"
        "         cast(:sd AS jsonb), :status, :apk, NOW() - make_interval(mins => :m + 60),"
        "         NOW() - make_interval(mins => :m))"
    ), {"id": id, "epid": epid, "dest": destination, "canon": canonical, "title": title, "sd": json.dumps(seed_data),
        "status": status, "apk": attached, "m": minutes_ago, "domain": domain, "cur": currency})


KRAVE_PID = "7777000011112"  # the internal Shopify product the identity retry starts from


def _insert_identity_seeds(c):
    """Seeds for the identity retry, which looks for 'great barrier relief' by title, KraveBeauty by brand."""
    krave = {"brand": "KraveBeauty", "variants": [{"id": "1", "sku": "GBR-50", "price": "28.00"}]}
    url = "https://{}/products/{}"
    for row in (
        # title column hit + brand in seed_data
        dict(id="eps_krave_gbr", title="Great Barrier Relief", sd=krave, domain="kravebeauty.com", slug="gbr"),
        # seed_data.title hit only (the column title has no term); brand via seed_data.vendor
        dict(id="eps_krave_sd_title", title="GBR Serum", domain="stylekorean.com", slug="gbr-serum",
             sd={"title": "KraveBeauty Great Barrier Relief Serum", "vendor": "KraveBeauty"}),
        # seed_data.title with a quote: escaped in seed_data's text form, still a hit
        dict(id="eps_krave_quoted", title=None, domain="yesstyle.com", slug="krave-gbr",
             sd={"title": 'KraveBeauty "Great Barrier Relief" 50ml', "brand": "KraveBeauty"}),
        # same title, another brand everywhere: the brand filter drops it
        dict(id="eps_dupe_brand", title="Great Barrier Relief Dupe", domain="dupe.example", slug="dupe",
             sd={"brand": "Dupeco"}),
        # brand but no title anywhere: never a title hit
        dict(id="eps_krave_untitled", title=None, domain="kravebeauty.com", slug="untitled", sd={"brand": "KraveBeauty"}),
        # the title in another currency: the currency conjunct drops it
        dict(id="eps_krave_eur", title="Great Barrier Relief", domain="kravebeauty.eu", slug="gbr-eu", sd=krave,
             currency="EUR"),
    ):
        _insert(c, id=row["id"], epid="ext_" + row["id"], title=row["title"],
                canonical=url.format(row["domain"], row["slug"]), destination=url.format(row["domain"], row["slug"]),
                seed_data=row["sd"], attached=None, minutes_ago=5, domain=row["domain"],
                currency=row.get("currency", "USD"))


def _internal_product(engine, *, title, vendor):
    """The one products_cache row the internal lane finds for KRAVE_PID: its title and vendor are
    what the identity retry searches seeds by."""
    from sqlalchemy import text

    with engine.connect() as c:
        c.execute(text("DELETE FROM products_cache"))
        c.execute(text(
            "INSERT INTO products_cache (merchant_id, platform, platform_product_id, product_data, expires_at)"
            " VALUES ('merch_krave_test', 'shopify', :pid, cast(:pd AS json), NOW() + interval '1 day')"
        ), {"pid": KRAVE_PID, "pd": json.dumps({
            "id": KRAVE_PID, "title": title, **({"vendor": vendor} if vendor else {}), "currency": "USD",
            "variants": [{"id": "4400000000001", "price": "28.00", "inventory_quantity": 5}],
        })})


def _load_rows(engine):
    from sqlalchemy import text

    base_sd = _FIXTURE["seed_data"]
    with engine.connect() as c:
        # The real row, as read.
        _insert(c, id=_FIXTURE["id"], epid=EXT_HIT, title="Silky Matte Lip Ink",
                canonical=_FIXTURE["canonical_url"], destination=_FIXTURE["destination_url"],
                seed_data={**base_sd, "external_product_id": EXT_HIT},
                attached=_FIXTURE["attached_product_key"], minutes_ago=1)
        # Another seed whose seed_data names EXT_CROSS (a copied attached key) and a uuid product id.
        _insert(c, id="eps_cross", epid="ext_" + "d2" * 12, title="Silky Matte Lip Ink Refill",
                canonical="https://judydoll.com/products/silky-matte-lip-ink-refill",
                destination="https://judydoll.com/products/silky-matte-lip-ink-refill?variant=1",
                seed_data={**base_sd, "attached_product_key": f"prod::external_seed::external_seed::{EXT_CROSS}",
                           "product_id": UUID_REF},
                attached="prod::external_seed::external_seed::" + "ext_" + "d2" * 12, minutes_ago=2)
        # NULL title, NULL canonical_url: the rows a COALESCE removal could in principle change.
        _insert(c, id="eps_null_title", epid="ext_" + "e3" * 12, title=None, canonical=None,
                destination="https://judydoll.com/products/silky-matte-lip-ink-mini",
                seed_data={"brand": "Judydoll"}, attached=None, minutes_ago=3)
        # Inactive rows never answer.
        _insert(c, id="eps_superseded", epid="ext_" + "f4" * 12, title="Silky Matte Lip Ink",
                canonical=_FIXTURE["canonical_url"], destination=_FIXTURE["destination_url"],
                seed_data={**base_sd, "external_product_id": EXT_HIT}, attached=None, minutes_ago=4,
                status="superseded")
        _insert_identity_seeds(c)
        # Filler in the same shape: other products, other ids, so the planner sees a real table. A
        # third carry a top-level seed_data.title, as rows from some writers do.
        template = json.dumps(base_sd)
        c.execute(text(
            "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url,"
            " canonical_url, domain, title, price_amount, price_currency, availability, seed_data, status,"
            " attached_product_key, created_at, updated_at)"
            " SELECT 'eps_fill_' || i, 'ext_' || substr(md5(i::text), 1, 24), 'US', '*',"
            "        'https://brand' || (i % 97) || '.com/products/item-' || i || '?variant=' || (50000000000000 + i),"
            "        'https://brand' || (i % 97) || '.com/products/item-' || i, 'brand' || (i % 97) || '.com',"
            "        'Brand' || (i % 97) || ' Hydrating Toner ' || i, 20, 'USD', 'in_stock',"
            "        cast(replace(replace(replace(replace(:tpl, 'silky-matte-lip-ink', 'item-' || i), 'Silky Matte Lip Ink', 'Hydrating Toner ' || i),"
            "             '49819267301653', (50000000000000 + i)::text), 'BURGUNDY', 'SHADE ' || i) AS jsonb)"
            "        || CASE WHEN i % 3 = 0 THEN jsonb_build_object('title', 'Brand' || (i % 97) || ' Hydrating Toner ' || i)"
            "                ELSE '{}'::jsonb END,"
            "        'active', 'prod::external_seed::external_seed::ext_' || substr(md5(i::text), 1, 24),"
            "        NOW() - make_interval(mins => 100 + i), NOW() - make_interval(mins => 100 + i)"
            " FROM generate_series(1, :n) i"
        ), {"tpl": template, "n": _FILLER})


def _resolve(monkeypatch, product_id, *, sku_id=None, statements=None, identity_statements=None):
    import databases

    import db.database as dbmod
    import db.merchant_purchasability as mp
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write
    from routes.agent_shop_gateway import OffersResolvePayload, _handle_offers_resolve

    monkeypatch.setattr(dual_write, "_price_check_column", {"present": False, "checked": None})
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    monkeypatch.setattr(mp, "human_handoff_allowed", AsyncMock(return_value=True))

    async def gate(row, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", gate)

    class _BT:
        def add_task(self, *a, **k):
            pass

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        real_fetch_all = db.fetch_all

        async def recording_fetch_all(query, values=None):
            if statements is not None and "FROM external_product_seeds" in str(query) and (
                "pid_like_0" in str(query) or "sku_like_0" in str(query)
            ):
                statements.append((str(query), dict(values or {})))
            if identity_statements is not None and ":identity_title_0" in str(query):
                identity_statements.append((str(query), dict(values or {})))
            return await real_fetch_all(query, values)

        db.fetch_all = recording_fetch_all
        monkeypatch.setattr(dbmod, "database", db)  # the handler imports it at call time
        monkeypatch.setattr(dual_write, "database", db)
        try:
            product = {"product_id": product_id, **({"sku_id": sku_id} if sku_id else {})}
            return await _handle_offers_resolve(
                OffersResolvePayload(product=product, limit=10, market="US", tool="*", commerce_surface="agent_api"),
                None, _BT(),
            )
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _db_call(fn):
    import databases

    async def go():
        db = databases.Database(DATABASE_URL, server_settings={"search_path": _SCHEMA})
        await db.connect()
        try:
            return await fn(db)
        finally:
            await db.disconnect()

    return asyncio.run(go())


def _explain(query, values):
    """The planner's own choice, no enable_* toggles: on a table of prod's size it is what prod runs.
    (On a 3k-row table it rightly reads the whole table through idx_external_product_seeds_status.)"""
    async def fn(db):
        return [r[0] for r in await db.fetch_all("EXPLAIN (ANALYZE, BUFFERS) " + query, values)]

    return _db_call(fn)


def _explain_generic(query, values):
    """The GENERIC plan (PREPARE, plan_cache_mode = force_generic_plan): asyncpg prepares every
    statement, and after five executions Postgres may keep a plan built without the bind values. That
    plan cannot read the LIKE patterns' trigrams at plan time, so it must still choose the probes."""
    names = []

    def positional(m):
        if m.group(1) not in names:
            names.append(m.group(1))
        return f"${names.index(m.group(1)) + 1}"

    sql = re.sub(r"(?<!:):([a-z_0-9]+)", positional, query)
    args = [values[n] for n in names]
    types = ", ".join("int" if isinstance(a, int) else "text" for a in args)
    literals = ", ".join(str(a) if isinstance(a, int) else "'" + str(a).replace("'", "''") + "'" for a in args)

    async def fn(db):
        async with db.connection() as conn:
            raw = conn.raw_connection
            await raw.execute("SET plan_cache_mode = force_generic_plan")
            await raw.execute(f"PREPARE fuzzy_generic({types}) AS {sql}")
            try:
                return [r[0] for r in await raw.fetch(f"EXPLAIN (ANALYZE) EXECUTE fuzzy_generic({literals})")]
            finally:
                await raw.execute("DEALLOCATE fuzzy_generic")
                await raw.execute("RESET plan_cache_mode")

    return _db_call(fn)


def _rows_read(plan):
    """Rows the plan read off external_product_seeds and threw away: a scan shows the whole table here."""
    return sum(int(m.group(1)) for m in re.finditer(r"Rows Removed by (?:Filter|Index Recheck): (\d+)", "\n".join(plan)))


def _bitmap_rows(plan, index):
    """The ACTUAL row count of the bitmap probe on `index`: a pattern pg_trgm extracts no trigram
    from still shows an Index Cond, but reads the whole index, so the count is the proof."""
    for line in plan:
        if f"Bitmap Index Scan on {index}" in line:
            return int(re.search(r"actual time=\S+ rows=(\d+)", line).group(1))
    return None


def _ids(query, values):
    async def fn(db):
        return [r["id"] for r in await db.fetch_all(query, values)]

    return _db_call(fn)


def _fuzzy_source(res):
    return [s for s in res["metadata"]["sources"] if s["source"] == "external_product_seeds"]


@pytest.mark.parametrize("ref", [SIG_MISS, EXT_HIT, EXT_CROSS, UUID_REF])
def test_an_id_shaped_ref_is_index_probes_never_a_table_scan(schema_engine, monkeypatch, ref):
    statements = []
    res = _resolve(monkeypatch, ref, statements=statements)
    [(query, values)] = statements
    assert "LOWER(title) LIKE" not in query, "an id never matches a title: no title arm"
    assert "COALESCE(canonical_url" not in query and "COALESCE(destination_url" not in query
    plan = _explain(query, values)
    text_plan = "\n".join(plan)
    assert "Seq Scan on external_product_seeds" not in text_plan, text_plan
    assert "idx_external_product_seeds_status" not in text_plan, text_plan
    assert _rows_read(plan) <= 2, text_plan
    for index in ("idx_external_product_seeds_active_external_product_id", "idx_eps_active_any_canonical_url_trgm",
                  "idx_eps_active_any_destination_url_trgm", "idx_eps_active_any_seed_data_text_trgm"):
        rows = _bitmap_rows(plan, index)
        assert rows is not None and rows <= 2, (index, rows, text_plan)
    assert "idx_eps_active_any_title_trgm" not in text_plan, text_plan
    generic = _explain_generic(query, values)
    assert "$1" in "\n".join(generic), generic  # really the generic plan
    assert "Seq Scan on external_product_seeds" not in "\n".join(generic), generic
    assert "BitmapOr" in "\n".join(generic) and _rows_read(generic) <= 2, generic
    # The lane answered inside its budget instead of timing out.
    [src] = _fuzzy_source(res)
    assert src["status"] == "ok" and src["query"] == "external_seed_by_fuzzy_ref", src


def test_main_scanned_the_table_for_the_same_id(schema_engine, monkeypatch):
    """The yardstick: main's statement, same binds, same table, IS a Seq Scan (no index matches
    LOWER(COALESCE(title,''))). Without this the test above could pass on a table too small to scan."""
    from routes.agent_shop_gateway import _expand_ref_aliases

    from routes.agent_shop_gateway import _fuzzy_seed_ref_clauses

    statements = []
    _resolve(monkeypatch, SIG_MISS, statements=statements)
    [(query, values)] = statements
    [new_clause], _ = _fuzzy_seed_ref_clauses(_expand_ref_aliases(SIG_MISS), [])
    [main_clause], main_params = _main_fuzzy_clauses(_expand_ref_aliases(SIG_MISS), [])
    assert new_clause in query
    main_query = query.replace(new_clause, main_clause, 1)
    assert "LOWER(COALESCE(title,'')) LIKE :pid_like_0" in main_query
    main_plan = _explain(main_query, {**values, **main_params})
    assert "Seq Scan on external_product_seeds" in "\n".join(main_plan), main_plan
    assert _rows_read(main_plan) >= _FILLER // 4, main_plan  # per-worker average under a parallel scan


@pytest.mark.parametrize("product_id, sku_id", [
    (SIG_MISS, None),
    (EXT_HIT, None),
    (EXT_CROSS, None),
    (UUID_REF, None),
    ("Silky Matte", None),                      # a title ref: the title arm runs
    ("silky-matte-lip-ink", None),              # a handle: urls + seed_data
    (VARIANT_ID, None),                         # a Shopify variant id (+ its gid aliases)
    (f"gid://shopify/Product/{VARIANT_ID}", None),
    ("lip-ink-mini", None),                     # the NULL-title / NULL-canonical row, by destination_url
    ("%", None),                                # all-'%': '' LIKE '%%%' -- the COALESCE edge
    ("item-17", None),                          # filler, many rows (item-17, item-170.., item-1700..)
    (SIG_MISS, _FIXTURE["seed_data"]["variants"][0]["sku"]),  # pid AND sku
])
def test_every_ref_returns_the_rows_main_returns(schema_engine, monkeypatch, product_id, sku_id):
    from routes.agent_shop_gateway import _expand_ref_aliases, _fuzzy_seed_ref_clauses

    statements = []
    _resolve(monkeypatch, product_id, sku_id=sku_id, statements=statements)
    [(query, values)] = statements
    pid_aliases, sku_aliases = _expand_ref_aliases(product_id), _expand_ref_aliases(sku_id)
    new_clauses, _ = _fuzzy_seed_ref_clauses(pid_aliases, sku_aliases)
    main_clauses, main_params = _main_fuzzy_clauses(pid_aliases, sku_aliases)
    main_query = query
    for new, old in zip(new_clauses, main_clauses):
        assert new in main_query
        main_query = main_query.replace(new, old, 1)
    assert main_query != query
    got, want = _ids(query, values), _ids(main_query, {**values, **main_params})
    assert got == want, (product_id, got, want)


def test_the_rows_cover_the_cases_they_claim(schema_engine, monkeypatch):
    """Guards the fixture: each case above must actually hit what its comment says it hits."""
    from routes.agent_shop_gateway import _expand_ref_aliases, _fuzzy_seed_ref_clauses

    def rows(ref):
        statements = []
        _resolve(monkeypatch, ref, statements=statements)
        [(query, values)] = statements
        return _ids(query, values)

    assert rows(SIG_MISS) == []
    assert rows(EXT_HIT) == [_FIXTURE["id"]]
    assert rows(EXT_CROSS) == ["eps_cross"], "an ext_ id named only in another seed's seed_data still answers"
    assert rows(UUID_REF) == ["eps_cross"]
    assert rows("lip-ink-mini") == ["eps_null_title"]
    assert "eps_null_title" in rows("%")
    assert "eps_superseded" not in rows("Silky Matte")
    assert set(rows("Silky Matte")) == {_FIXTURE["id"], "eps_cross"}
    assert len(rows("item-17")) > 10
    # A title ref keeps its title arm.
    clauses, _ = _fuzzy_seed_ref_clauses(_expand_ref_aliases("Silky Matte"), [])
    assert "LOWER(title) LIKE :pid_like_0" in clauses[0]


# --- the identity retry (external_seed_by_internal_identity) -------------------------------------


def _main_identity_arm(key, term):
    """main @ b86afa82c, verbatim."""
    return ("(" f"LOWER(COALESCE(title,'')) LIKE :{key}"
            f" OR LOWER(COALESCE(seed_data->>'title','')) LIKE :{key}" ")")


def _identity(monkeypatch, engine, *, title, vendor):
    from routes.agent_shop_gateway import _identity_title_arm

    _internal_product(engine, title=title, vendor=vendor)
    identity_statements = []
    res = _resolve(monkeypatch, KRAVE_PID, identity_statements=identity_statements)
    assert identity_statements, res["metadata"]["sources"]
    [(query, values)] = identity_statements
    main_query = query
    for key in sorted((k for k in values if k.startswith("identity_title_")), key=lambda k: -int(k.rsplit("_", 1)[1])):
        term = values[key].strip("%")
        new = _identity_title_arm(key, term)
        assert new in main_query, (key, new)
        main_query = main_query.replace(new, _main_identity_arm(key, term), 1)
    assert main_query != query
    return res, query, main_query, values


def _identity_source(res):
    return [s for s in res["metadata"]["sources"] if s["source"] == "external_product_seeds_identity_retry"]


def test_the_identity_retry_probes_the_title_indexes_and_never_scans(schema_engine, monkeypatch):
    res, query, main_query, values = _identity(monkeypatch, schema_engine, title="KraveBeauty Great Barrier Relief",
                                               vendor="KraveBeauty")
    assert values["identity_title_0"] == "%kravebeauty great barrier relief%"
    assert values["identity_title_1"] == "%great barrier relief%"
    assert "LOWER(title) LIKE :identity_title_0" in query
    plan = _explain(query, values)
    text_plan = "\n".join(plan)
    assert "Seq Scan on external_product_seeds" not in text_plan, text_plan
    assert "idx_external_product_seeds_status" not in text_plan, text_plan
    for index in ("idx_eps_active_any_title_trgm", "idx_eps_active_any_seed_title_trgm"):
        assert f"Bitmap Index Scan on {index}" in text_plan, (index, text_plan)
    assert _rows_read(plan) <= 10, text_plan
    generic = _explain_generic(query, values)
    assert "$1" in "\n".join(generic), generic
    assert "Seq Scan on external_product_seeds" not in "\n".join(generic), generic
    assert "Bitmap Index Scan on idx_eps_active_any_seed_title_trgm" in "\n".join(generic), generic
    # The yardstick: main's arms, same binds, same table, scan it.
    main_plan = _explain(main_query, values)
    assert "Seq Scan on external_product_seeds" in "\n".join(main_plan), main_plan
    assert _rows_read(main_plan) >= _FILLER // 4, main_plan
    # And the lane answered: the column-title hit, the seed_data.title hit, the quoted one.
    [src] = _identity_source(res)
    assert src["status"] == "ok" and src["row_count"] == 3, src
    assert sorted(_ids(query, values)) == ["eps_krave_gbr", "eps_krave_quoted", "eps_krave_sd_title"]


@pytest.mark.parametrize("title, vendor", [
    ("KraveBeauty Great Barrier Relief", "KraveBeauty"),   # brand-prefixed title: two title terms
    ("Great Barrier Relief", None),                        # no brand: the brand filter is TRUE
    ("Great Barrier Relief", "Dupeco"),                     # the other brand's seed only
    ("Silky Matte Lip Ink", "Judydoll"),                    # the real-shape fixture row
    ("Brand3 Hydrating Toner 1749", "Brand3"),              # filler 1749: column title and seed_data.title
    ("Hydrating Toner", None),                              # many rows, cut by the LIMIT
    ("Nothing Sells This Product", "Nobody"),               # no hit at all
])
def test_the_identity_retry_returns_the_rows_main_returns(schema_engine, monkeypatch, title, vendor):
    _res, query, main_query, values = _identity(monkeypatch, schema_engine, title=title, vendor=vendor)
    assert _ids(query, values) == _ids(main_query, values), (title, vendor)


def test_the_identity_cases_hit_what_they_claim(schema_engine, monkeypatch):
    def ids(title, vendor):
        _res, query, _main, values = _identity(monkeypatch, schema_engine, title=title, vendor=vendor)
        return sorted(_ids(query, values))

    assert ids("Great Barrier Relief", "Dupeco") == ["eps_dupe_brand"]
    assert ids("Silky Matte Lip Ink", "Judydoll") == sorted([_FIXTURE["id"], "eps_cross"])
    assert ids("Nothing Sells This Product", "Nobody") == []
    assert len(ids("Hydrating Toner", None)) == 60  # the LIMIT (attached_seed_limit) cuts it
    # Filler 1749 (brand 1749 % 97 = 3; 1749 % 3 == 0, so it also carries a seed_data.title).
    assert "eps_fill_1749" in ids("Brand3 Hydrating Toner 1749", "Brand3")
