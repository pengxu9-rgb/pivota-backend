"""Concrete adversarial failures from the pre-rollout review; no network or DB."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import onboard_curated_brands as cli
from services import catalog_onboard_worker as worker, curated_brand_feed as feed
from services.catalog_enrichment_agent import apply as writer, ingestion as ing
from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan


def product():
    return {
        "id": 9000001, "handle": "honey-milk-lip-oil", "title": "Honey Milk Lip Oil",
        "vendor": "A'PIEU", "product_type": "Lip Oil", "images": [{"src": "https://cdn.example/oil.jpg"}],
        "variants": [{"id": 45000000000001, "barcode": "8809530070499", "price": "10.00", "available": True}],
    }


def record(host="first.example", raw=None, *, native=True):
    return feed.shopify_product_to_record(
        raw or product(), domain=host, category_path="beauty", currency="USD",
        source_role="retailer", emit_native_variants=native,
    )


def batch(records):
    return feed.CuratedRecordBatch(records, crawl_report={"status": "complete", "pages": 1})


@pytest.mark.asyncio
async def test_same_title_native_product_and_variant_ids_keep_two_listing_chains_on_replay():
    records = [record(host) for host in ("first.example", "second.example")]
    plan = ing.ingest_validated_jsonl(records)
    assert {k: len(plan[k]) for k in ("pdps", "skus", "offers", "seeds")} == {
        "pdps": 2, "skus": 4, "offers": 4, "seeds": 2,
    }
    assert not plan["skipped_reasons"]
    assert len({p["pivota_signature_id"] for p in plan["pdps"]}) == 2
    assert inspect_primary_plan(plan)["status"] == "ready_to_apply"
    existing = copy.deepcopy(plan["pdps"])

    class DB:
        async def fetch_all(self, *args, **kwargs):
            return existing
        async def fetch_one(self, *args, **kwargs):
            return None
        async def execute(self, *args, **kwargs):
            return None

    before = [(s["sku_key"], s["merchant_id"], s["source_variant_id"]) for s in plan["skus"]]
    await writer._prepare_seller_of_record(plan, DB())
    assert [(s["sku_key"], s["merchant_id"], s["source_variant_id"]) for s in plan["skus"]] == before
    assert len({s["merchant_id"] for s in plan["skus"]}) == 2
    for row in records:
        row["pdp"]["product_name"] = "Revised Display Title"
    replay = ing.ingest_validated_jsonl(records)
    assert {p["product_key"] for p in replay["pdps"]} == {p["product_key"] for p in plan["pdps"]}
    assert {p["pivota_signature_id"] for p in replay["pdps"]} == {p["pivota_signature_id"] for p in plan["pdps"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_legacy_same_storefront_listing_blocks_before_any_write(batch_mode):
    plan = ing.ingest_validated_jsonl([record()])
    legacy = dict(plan["pdps"][0], product_key="ext:legacy-title::12345678")
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = [legacy]
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer.apply_ingest_plan(plan, batch_label="review", db=database, batch=batch_mode)
    database.execute.assert_not_awaited()


def test_apex_www_listing_identity_is_equal_and_other_store_is_refused():
    assert ing.retailer_listing_identity("www.first.example", "https://first.example/products/oil/") == (
        ing.retailer_listing_identity("first.example", "http://www.first.example/products/oil?tracking=1")
    )
    with pytest.raises(ValueError, match="identity_unproven"):
        ing.retailer_listing_identity("first.example", "https://second.example/products/oil")


@pytest.mark.parametrize("other_barcode", ["4006381333931", "8809530070499", None])
def test_native_variant_order_or_price_never_promotes_one_barcode_to_whole_line(other_barcode):
    raw = product()
    raw["variants"].append(dict(raw["variants"][0], id=45000000000002, barcode=other_barcode))
    rows = []
    for _ in range(2):
        mapped = record(raw=raw)
        plan = ing.ingest_validated_jsonl([mapped])
        assert mapped["pdp"]["barcode"] is None
        assert plan["pdps"][0]["gtin"] is None
        assert {s["barcode"] for s in plan["skus"] if "::v:" in s["sku_key"]} == {"8809530070499", other_barcode}
        rows.append((plan["pdps"][0]["product_key"], plan["pdps"][0]["content_key"]))
        raw["variants"].reverse()
    assert rows[0] == rows[1]
    raw["variants"][0]["price"] = "0.00"
    assert record(raw=raw)["pdp"]["barcode"] is None


@pytest.mark.parametrize("vendor,host", [(None, "store.example"), ("VC-B004", "sukoshi.com"),
    ("Metro Singapore Departmental Store - Celebrating 69 Years in SG", "metro.com.sg")])
def test_store_or_supplier_vendor_cannot_become_operator_brand(vendor, host):
    raw = product()
    raw["vendor"] = vendor
    with pytest.raises(ValueError, match="retailer_maker_unproven"):
        feed.shopify_product_to_record(raw, domain=host, category_path="beauty", currency="USD",
                                      source_role="retailer", brand_override="Elizabeth Arden")


@pytest.mark.parametrize("vendor", ["珂润", "설화수", "3CE", "A'PIEU Plus"])
def test_real_distinct_vendor_cannot_be_overridden(vendor):
    raw = product()
    raw["vendor"] = vendor
    mapped = feed.shopify_product_to_record(raw, domain="store.example", category_path="beauty", currency="USD",
                                          source_role="retailer", brand_override="A'PIEU")
    assert mapped["pdp"]["brand"] == vendor


def test_empty_and_cross_product_only_chains_are_not_primary_ready():
    assert inspect_primary_plan({})["status"] == "blocked"
    plan = ing.ingest_validated_jsonl([record("first.example"), record("second.example")])
    missing = plan["pdps"][1]["product_key"]
    plan["offers"] = [o for o in plan["offers"] if o["product_key"] != missing]
    report = inspect_primary_plan(plan)
    assert report["status"] == "blocked"
    assert report["missing_commerce_product_keys"] == [missing]
    assert inspect_primary_plan(ing.ingest_validated_jsonl([record(native=False)]))["status"] == "blocked"


def test_cli_rejects_unsupported_market_before_fetching_any_roster_row(monkeypatch, tmp_path):
    roster = tmp_path / "roster.jsonl"
    rows = [{"domain": "first.example", "category_path": "beauty", "source_role": "retailer",
             "only_vendors": ["A'PIEU"], "emit_real_variants": True},
            {"domain": "second.example", "category_path": "beauty", "market": "SG"}]
    roster.write_text("\n".join(map(json.dumps, rows)))
    fetch = AsyncMock()
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--file", str(roster), "--apply"]) == 2
    fetch.assert_not_awaited()


def test_cli_row_controls_reach_fetch_and_real_native_plan(monkeypatch, tmp_path):
    roster = tmp_path / "roster.jsonl"
    row = {"domain": "first.example", "category_path": "beauty", "market": "US", "source_role": "retailer",
           "only_vendors": ["A'PIEU"], "emit_real_variants": True, "max_products": 7,
           "base_listings_only": True, "max_scan_products": 12, "require_currency": "USD"}
    roster.write_text(json.dumps(row))
    calls = []
    async def fetch(**kwargs):
        calls.append(kwargs)
        return batch([record(native=kwargs["emit_real_variants"])])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--file", str(roster)]) == 0
    assert calls[0]["emit_real_variants"] is True
    assert calls[0]["base_listings_only"] is True
    assert calls[0]["max_products"] == 7
    assert calls[0]["max_scan_products"] == 12


@pytest.mark.asyncio
async def test_empty_complete_crawl_cannot_finish_queue_apply(monkeypatch):
    monkeypatch.setattr(worker, "records_for_brand", AsyncMock(return_value=batch([])))
    apply = AsyncMock()
    monkeypatch.setattr(worker, "apply_ingest_plan", apply)
    with pytest.raises(ValueError, match="no products enumerated"):
        await worker._process_curated_brand({"domain": "first.example"}, apply=True, db=None)
    apply.assert_not_awaited()


def test_retailer_cli_requires_exact_maker_subset_before_fetch(monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(["--domain", "store.example", "--category", "beauty", "--source-role", "retailer"]) == 2
    fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("outcome", [None, {}, {"action": "MINT"},
    {"action": "MINT", "content_key": "ck_substitute", "evidence": {"evidence": {"reason": "error", "error": "db unavailable"}}}])
async def test_primary_identity_failure_never_writes_a_minted_substitute(monkeypatch, batch_mode, outcome):
    from services import intake_identity as identity
    plan = ing.ingest_validated_jsonl([record()])
    monkeypatch.setattr(identity, "intake_identity_enabled", lambda door: True)
    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", AsyncMock(return_value=outcome))
    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = []
    database.fetch_one.return_value = None
    database.transaction = MagicMock(return_value=AsyncMock())
    counts = await writer.apply_ingest_plan(plan, batch_label="review", db=database, batch=batch_mode)
    assert counts["pdps"] == counts["skus"] == counts["offers"] == counts["seeds"] == 0
    assert counts["pdps_skipped_identity"] == 1
    for call in database.execute.await_args_list:
        sql = str(call.args[0] if call.args else call.kwargs.get("query", ""))
        assert not any("INSERT INTO " + table in sql for table in ("catalog_products", "catalog_skus", "catalog_offers", "external_product_seeds"))


@pytest.mark.asyncio
async def test_legacy_guard_ignores_unrelated_missing_source_metadata():
    plan = ing.ingest_validated_jsonl([record()])
    database = AsyncMock()
    database.fetch_all.return_value = [{"product_key": "legacy", "source_domain": None,
                                       "canonical_url": "https://first.example/products/unrelated"}]
    await writer._refuse_parallel_retailer_listings(plan, database)
    database.execute.assert_not_awaited()


@pytest.mark.parametrize("stock,expected,quantity", [(True, "in_stock", None), (False, "out_of_stock", 0), (None, "unknown", None)])
def test_observed_stock_reaches_native_and_canonical_offers_without_invented_quantity(stock, expected, quantity):
    raw = product()
    if stock is None:
        raw["variants"][0].pop("available")
    else:
        raw["variants"][0]["available"] = stock
    mapped = record(raw=raw)
    assert mapped["pdp"]["variants"][0]["in_stock"] is stock
    plan = ing.ingest_validated_jsonl([mapped])
    assert len(plan["offers"]) == 2
    assert {offer["availability"] for offer in plan["offers"]} == {expected}
    assert {offer["inventory_quantity"] for offer in plan["offers"]} == {quantity}
    assert {seed["availability"] for seed in plan["seeds"]} == {expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, {"product_group_id": ""}, {"product_group_id": "pg_different"}])
async def test_primary_group_requires_persisted_matching_membership(stored):
    pdp = ing.ingest_validated_jsonl([record()])["pdps"][0]
    database = AsyncMock()
    database.fetch_one.return_value = stored
    assert not await writer._ensure_primary_retailer_group(pdp, database=database, target="pg_expected")
    database.execute.assert_awaited_once()
    assert database.execute.await_args.args[1]["product_group_id"] == "pg_expected"


@pytest.mark.asyncio
async def test_primary_group_preserves_existing_curated_membership_without_resolution():
    pdp = ing.ingest_validated_jsonl([record()])["pdps"][0]
    database = AsyncMock()
    database.fetch_one.return_value = {"product_group_id": "pg_curated"}
    assert await writer._ensure_primary_retailer_group(pdp, database=database)
    assert "DO NOTHING" in database.execute.await_args.args[0]


def test_primary_apply_group_failure_is_incomplete_even_with_all_row_counts():
    from services.catalog_enrichment_agent.primary_ingestion import PrimaryIngestionIncomplete, require_primary_apply
    counts = {"pdps": 1, "skus": 2, "offers": 2}
    with pytest.raises(PrimaryIngestionIncomplete):
        require_primary_apply({"planned": counts}, {**counts, "product_groups_failed": 1})


# --- Dry-run legacy listing preflight (--check-legacy-listings) --------------------------------
# Wave 1 (2026-09-18): every dry run said `ready_to_apply`, then the apply refused haruharu wonder
# at ohlolly.com on a legacy `prod::external_seed::...` row that owned the same listing URL.
# These pin that the dry run now reports what the apply refuses on, from the same finder, while
# writing nothing and leaving the apply guard's decision (suppressed rows still block) unchanged.

_SUPPRESSED_AT = "2026-09-01T00:00:00+00:00"
_RETAILER_ARGS = ["--domain", "first.example", "--category", "beauty", "--source-role", "retailer",
                  "--only-vendor", "A'PIEU", "--emit-real-variants"]


def second_product():
    raw = product()
    raw.update(id=9000002, handle="honey-milk-lip-balm", title="Honey Milk Lip Balm")
    raw["variants"] = [dict(raw["variants"][0], id=45000000000009, barcode="4006381333931")]
    return raw


def legacy_owner(pdp, key, *, suppressed=None, reason=None, url=None):
    return {"product_key": key, "source_domain": pdp["source_domain"],
            "canonical_url": url or pdp["canonical_url"],
            "suppressed_at": suppressed, "suppression_reason": reason}


class ReadOnlyCatalog:
    """Records every call; any write-shaped call fails the test at the call site."""

    def __init__(self, rows, *, connected=True, error=None):
        self.rows, self.is_connected, self.error = rows, connected, error
        self.queries, self.connects, self.disconnects = [], 0, 0

    async def fetch_all(self, query, values=None):
        self.queries.append((query, values))
        if self.error:
            raise self.error
        return self.rows

    async def connect(self):
        self.connects += 1
        self.is_connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False

    def __getattr__(self, name):  # execute / execute_many / transaction / fetch_one ...
        raise AssertionError(f"legacy preflight touched database.{name}")


def run_dry(monkeypatch, capsys, database, raws=None, extra=()):
    raws = raws or [product()]
    async def fetch(**kwargs):
        return batch([record(raw=raw) for raw in raws])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    apply = AsyncMock()
    monkeypatch.setattr(cli, "apply_ingest_plan", apply)
    if database is not None:
        monkeypatch.setattr(cli, "_preflight_database", lambda: (database, None))
    rc = cli.main(_RETAILER_ARGS + list(extra))
    out, err = capsys.readouterr()
    lines = [line for line in out.splitlines() if line.startswith(cli.LEGACY_LISTINGS_MARKER)]
    assert len(lines) == 1, out
    apply.assert_not_awaited()
    return rc, json.loads(lines[0][len(cli.LEGACY_LISTINGS_MARKER):]), out, err


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("suppressed", [None, _SUPPRESSED_AT])
async def test_suppressed_legacy_owner_still_blocks_apply_before_any_write(batch_mode, suppressed):
    """Decision pinned, not changed: a suppressed legacy owner refuses exactly like a live one."""
    plan = ing.ingest_validated_jsonl([record()])
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = [legacy_owner(plan["pdps"][0], "ext:legacy-title::12345678",
                                                    suppressed=suppressed, reason="withdrawn")]
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        await writer.apply_ingest_plan(plan, batch_label="review", db=database, batch=batch_mode)
    database.execute.assert_not_awaited()


@pytest.mark.parametrize("suppressed", [None, _SUPPRESSED_AT])
def test_dry_run_reports_the_legacy_owner_apply_refuses_and_writes_nothing(monkeypatch, capsys, suppressed):
    pdp = ing.ingest_validated_jsonl([record()])["pdps"][0]
    legacy_key = "prod::external_seed::external_seed::ext_bf55156550aa86a7eb921ff2"
    database = ReadOnlyCatalog([legacy_owner(pdp, legacy_key, suppressed=suppressed, reason="stale")])
    rc, report, out, err = run_dry(monkeypatch, capsys, database, extra=["--check-legacy-listings"])

    assert rc == 2
    assert "legacy_listing_preflight_conflicts" in err
    assert "DRY-RUN" not in out
    assert report["status"] == "conflicts" and report["apply_would_refuse"] is True
    assert report["planned_listings"] == 1
    assert report["conflict_count"] == 1 and report["listings_with_conflicts"] == 1
    assert report["suppressed_conflict_count"] == (1 if suppressed else 0)
    assert report["conflicts"] == [{
        "listing": "first.example/products/honey-milk-lip-oil",
        "planned_product_key": pdp["product_key"],
        "legacy_owners": [{"product_key": legacy_key, "suppressed": bool(suppressed),
                           "suppression_reason": "stale"}],
    }]
    # The plan verdict line is untouched: curated_apply_gate parses it, the worker computes it too.
    assert 'primary ingestion: {' in out and '"status": "ready_to_apply"' in out
    # Strictly SELECT-only: one fetch_all (any other attribute access raises in ReadOnlyCatalog).
    assert len(database.queries) == 1
    assert database.queries[0][0].lstrip().upper().startswith("SELECT")
    assert database.queries[0][1] == {"hosts": ["first.example"]}


@pytest.mark.parametrize("suppressed", [None, _SUPPRESSED_AT])
def test_dry_run_refusal_is_the_exact_message_the_apply_raises(monkeypatch, capsys, suppressed):
    plan = ing.ingest_validated_jsonl([record()])
    rows = [legacy_owner(plan["pdps"][0], "ext:legacy-title::12345678", suppressed=suppressed)]
    _, report, _, _ = run_dry(monkeypatch, capsys, ReadOnlyCatalog(rows), extra=["--check-legacy-listings"])
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = rows
    with pytest.raises(ValueError) as refused:
        asyncio.run(writer.apply_ingest_plan(plan, batch_label="review", db=database))
    assert str(refused.value) == report["apply_refusal"]


def test_dry_run_counts_every_owner_per_listing_suppressed_or_not(monkeypatch, capsys):
    """ohlolly.com shape: one listing held by many legacy rows, most suppressed, plus a second listing."""
    pdps = ing.ingest_validated_jsonl([record(raw=product()), record(raw=second_product())])["pdps"]
    oil, balm = sorted(pdps, key=lambda p: p["canonical_url"], reverse=True)
    assert oil["canonical_url"].endswith("/honey-milk-lip-oil")
    rows = [legacy_owner(oil, f"legacy::oil::{i}", suppressed=_SUPPRESSED_AT if i else None) for i in range(3)]
    rows += [legacy_owner(balm, "legacy::balm", suppressed=_SUPPRESSED_AT),
             legacy_owner(oil, oil["product_key"]),  # the planned key itself is not a conflict
             legacy_owner(oil, "legacy::other", url="https://first.example/products/unrelated")]
    rc, report, _, _ = run_dry(monkeypatch, capsys, ReadOnlyCatalog(rows),
                               raws=[product(), second_product()], extra=["--check-legacy-listings"])
    assert rc == 2
    assert report["planned_listings"] == 2
    assert report["conflict_count"] == 4
    assert report["suppressed_conflict_count"] == 3
    assert report["listings_with_conflicts"] == 2
    owners = {c["listing"]: [o["product_key"] for o in c["legacy_owners"]] for c in report["conflicts"]}
    assert owners == {"first.example/products/honey-milk-lip-oil": ["legacy::oil::0", "legacy::oil::1", "legacy::oil::2"],
                      "first.example/products/honey-milk-lip-balm": ["legacy::balm"]}


def test_dry_run_is_clear_when_only_the_planned_key_and_unrelated_rows_exist(monkeypatch, capsys):
    pdp = ing.ingest_validated_jsonl([record()])["pdps"][0]
    rows = [legacy_owner(pdp, pdp["product_key"]),
            legacy_owner(pdp, "legacy::other", url="https://first.example/products/unrelated", suppressed=_SUPPRESSED_AT)]
    rc, report, out, _ = run_dry(monkeypatch, capsys, ReadOnlyCatalog(rows), extra=["--check-legacy-listings"])
    assert rc == 0 and "DRY-RUN" in out
    assert report["status"] == "clear" and report["apply_would_refuse"] is False
    assert report["conflict_count"] == 0 and report["conflicts"] == [] and report["apply_refusal"] is None


def test_dry_run_reports_an_unproven_legacy_row_in_the_order_apply_meets_it(monkeypatch, capsys):
    """A same-host row with no listing path makes the apply raise identity_unproven; say so."""
    plan = ing.ingest_validated_jsonl([record()])
    pdp = plan["pdps"][0]
    rows = [legacy_owner(pdp, "legacy::home", url="https://first.example/"),
            legacy_owner(pdp, "legacy::oil")]
    rc, report, _, _ = run_dry(monkeypatch, capsys, ReadOnlyCatalog(rows), extra=["--check-legacy-listings"])
    assert rc == 2 and report["status"] == "conflicts"
    assert report["unproven_legacy_rows"] == [{"product_key": "legacy::home", "canonical_url": "https://first.example/"}]
    assert report["conflict_count"] == 1
    assert report["apply_refusal"].startswith("retailer_listing_identity_unproven")
    database = AsyncMock()
    database.is_connected = True
    database.fetch_all.return_value = rows
    with pytest.raises(ValueError, match="retailer_listing_identity_unproven"):
        asyncio.run(writer.apply_ingest_plan(plan, batch_label="review", db=database))
    database.fetch_all.return_value = list(reversed(rows))
    with pytest.raises(ValueError, match="retailer_listing_migration_required"):
        asyncio.run(writer.apply_ingest_plan(plan, batch_label="review", db=database))


def test_dry_run_without_the_flag_says_unchecked_and_never_opens_the_db(monkeypatch, capsys):
    def no_db():
        raise AssertionError("dry run opened the DB without --check-legacy-listings")
    monkeypatch.setattr(cli, "_preflight_database", no_db)
    rc, report, out, _ = run_dry(monkeypatch, capsys, None)
    assert rc == 0 and "DRY-RUN" in out
    assert report["status"] == "unchecked" and report["planned_listings"] == 1
    assert "conflict_count" not in report  # an unchecked run never claims zero conflicts


def test_preflight_connects_and_disconnects_a_db_it_opened(monkeypatch, capsys):
    database = ReadOnlyCatalog([], connected=False)
    rc, report, _, _ = run_dry(monkeypatch, capsys, database, extra=["--check-legacy-listings"])
    assert rc == 0 and report["status"] == "clear"
    assert (database.connects, database.disconnects, database.is_connected) == (1, 1, False)


def test_preflight_error_exits_2_and_never_prints_the_driver_message(monkeypatch, capsys):
    database = ReadOnlyCatalog([], error=RuntimeError("connect failed postgresql://u:hunter2@10.0.0.1/db"))
    rc, report, out, err = run_dry(monkeypatch, capsys, database, extra=["--check-legacy-listings"])
    assert rc == 2
    assert report["status"] == "error" and report["error"] == "RuntimeError"
    assert "hunter2" not in out + err and "conflict_count" not in report


def test_preflight_refuses_the_sqlite_fallback_instead_of_reporting_clear(monkeypatch, capsys):
    import db.database as db_module
    monkeypatch.setattr(db_module, "DATABASE_URL", "sqlite+aiosqlite:///./pivota.db")
    database, reason = cli._preflight_database()
    assert database is None and reason == "no_postgres_database_url"
    async def fetch(**kwargs):
        return batch([record()])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    assert cli.main(_RETAILER_ARGS + ["--check-legacy-listings"]) == 2
    out = capsys.readouterr().out
    assert '"error": "no_postgres_database_url"' in out and '"status": "error"' in out


@pytest.mark.asyncio
async def test_select_only_handle_cannot_write():
    inner = AsyncMock()
    handle = cli._SelectOnlyDatabase(inner)
    for sql in ("UPDATE catalog_products SET suppressed_at = NULL", "  delete from catalog_products",
                "WITH x AS (DELETE FROM catalog_products RETURNING 1) SELECT * FROM x"):
        with pytest.raises(PermissionError):
            await handle.fetch_all(sql, {})
    assert not hasattr(handle, "execute") and not hasattr(handle, "transaction")
    inner.fetch_all.assert_not_awaited()
    await handle.fetch_all("  select 1", {})
    inner.fetch_all.assert_awaited_once()


def test_brand_official_plan_has_no_retailer_listings_to_check(monkeypatch, capsys):
    raw = product()
    async def fetch(**kwargs):
        return batch([feed.shopify_product_to_record(raw, domain="first.example", category_path="beauty",
                                                     currency="USD", source_role="brand_official")])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    def no_db():
        raise AssertionError("no retailer listing planned; nothing to read")
    monkeypatch.setattr(cli, "_preflight_database", no_db)
    assert cli.main(["--domain", "first.example", "--category", "beauty", "--check-legacy-listings"]) == 0
    out = capsys.readouterr().out
    assert 'legacy listings: {"planned_listings": 0, "status": "not_applicable"}' in out
