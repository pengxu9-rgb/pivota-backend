"""The collector must report what it read, and say so when it read nothing.

Its whole purpose is to remove the gap between a claim and a row. A default emitted for a value it
could not read would be indistinguishable from a measurement once written into the JSON — which is
the failure this file exists to prevent, one layer earlier than the validator.
"""
import json

import pytest

from scripts.collect_curated_canary_evidence import SURFACES, collect
from scripts.validate_meitu_canary_evidence import evaluate

CASE = {
    "case_id": "two_sellers",
    "accepted_brands": ["Pyunkang Yul"],
    "seller_hosts": ["eyurs.com", "ohlolly.com"],
    "market": "US",
    "currency": "USD",
    "inci_source": "reseller_listing",
    "target_gtin": "08809486681497",
    "required_category_prefix": "beauty/skincare/cleanse/",
}


class FakeConn:
    """Answers by the statement's FROM target.

    Matching on a bare table-name substring is what let the original SQL ship broken: the group
    query also mentions catalog_products, so a substring match answered it with product rows. This
    still cannot validate column names — `test_every_selected_column_exists_in_the_schema` and the
    Postgres EXPLAIN test do that.
    """

    FROMS = (("FROM catalog_products", "catalog_products"), ("FROM catalog_skus", "catalog_skus"),
             ("FROM catalog_offers", "catalog_offers"),
             ("FROM beauty_sku_ingredients", "beauty_sku_ingredients"),
             ("FROM product_group_members", "product_group_members"))

    def __init__(self, products=(), skus=(), offers=(), incis=(), groups=(), group_error=None):
        self.rows = {"catalog_products": list(products), "catalog_skus": list(skus),
                     "catalog_offers": list(offers), "beauty_sku_ingredients": list(incis),
                     "product_group_members": list(groups)}
        self.group_error = group_error
        self.seen = []

    async def fetch(self, sql, *args):
        self.seen.append((sql, args))
        for needle, table in self.FROMS:
            if needle in sql:
                if table == "product_group_members" and self.group_error:
                    raise self.group_error
                return self.rows[table]
        raise AssertionError(f"unexpected statement: {sql[:60]}")


def product_row(host, key, **overrides):
    # NOTE: no market/currency — catalog_products has neither column. Supplying them here is
    # what hid a query that could not run at all.
    row = {"product_key": key, "merchant_id": f"merch_{host}", "platform": "external_seed",
           "source_product_id": key.split(":")[-1], "source_domain": host, "gtin": "08809486681497",
           "category_path": "beauty/skincare/cleanse/cleanser", "content_key": "ck_shared",
           "brand": "Pyunkang Yul", "title": "Deep Clear Cleansing Balm",
           "pivota_signature_id": "sig_x"}
    row.update(overrides)
    return row


async def test_it_reports_the_stored_ingredient_row_it_found():
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "ext:retailer:a::v:1", "source_variant_id": "45001",
               "sku_payload": json.dumps({"variant_id": "45001", "variant_id_provenance": "merchant_issued"})}],
        offers=[{"product_key": "ext:retailer:a", "merchant_id": "merch_eyurs.com", "currency": "USD",
                 "market": "US", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": "eyurs.com", "destination_url": "https://eyurs.com/products/x"}],
        incis=[{"product_key": "ext:retailer:a", "source_system": "reseller_listing", "raw_inci_chars": 868}],
        groups=[{"product_key": "ext:retailer:a", "product_group_id": "pg_1"}],
    )
    out = await collect(conn, CASE)
    product = out["products"][0]
    assert product["inci_row"] == {"present": True, "source_system": "reseller_listing", "raw_inci_chars": 868}
    # Deliberately NOT accompanied by a sibling inci_source: the validator compares the stored
    # row to the CASE, because a collector that writes both proves only its own consistency.
    assert "inci_source" not in product
    assert product["product_group_id"] == "pg_1"
    assert product["market"] == "US" and product["currency"] == "USD"
    # Provenance is read out of sku_payload JSON, which arrives as TEXT from asyncpg.
    assert product["variant_id"] == "45001"
    assert product["variant_id_provenance"] == "merchant_issued"


async def test_a_product_with_no_stored_ingredients_says_so_rather_than_claiming_the_case_value():
    """The A'PIEU lip oil's real shape: seller publishes no INCI, so no row exists. The collector
    must NOT fill inci_source from the case just because the case declares one."""
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")])
    product = (await collect(conn, CASE))["products"][0]
    assert product["inci_row"] == {"present": False, "source_system": None, "raw_inci_chars": 0}
    assert "inci_source" not in product
    assert product["variant_id"] is None and product["variant_id_provenance"] is None


async def test_live_surfaces_are_null_and_never_empty_lists():
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")])
    out = await collect(conn, CASE)
    for surface in SURFACES:
        assert out[surface] is None, f"{surface} must be null, not a measured-looking []"
        assert surface in out["evidence_provenance"]["not_collected"]
    for field in ("second_ingest_added_product_keys", "identity_failures", "crawl"):
        assert out[field] is None


async def test_collected_evidence_does_not_pass_validation_on_its_own():
    """The collector deliberately produces an INCOMPLETE file: the live-door surfaces, the crawl
    report and the second ingest are not database facts. It must not look acceptable until those
    are measured and merged."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a"), product_row("ohlolly.com", "ext:retailer:b")],
        incis=[{"product_key": "ext:retailer:a", "source_system": "reseller_listing", "raw_inci_chars": 868},
               {"product_key": "ext:retailer:b", "source_system": "reseller_listing", "raw_inci_chars": 869}],
    )
    out = await collect(conn, CASE)
    result = evaluate({"cases": [CASE]}, {"two_sellers": out})
    assert result["passed"] == 0
    assert any("not collected" in r for r in result["cases"][0]["reasons"])


async def test_the_target_gtin_filters_what_is_collected():
    """A host's other products are not this case's evidence, however well they converge."""
    conn = FakeConn(products=[
        product_row("eyurs.com", "ext:retailer:a"),
        product_row("eyurs.com", "ext:retailer:other", gtin="08809530070499"),
    ])
    out = await collect(conn, CASE)
    assert [p["product_key"] for p in out["products"]] == ["ext:retailer:a"]


async def test_a_missing_group_table_is_reported_not_defaulted():
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")],
                    group_error=RuntimeError('relation "product_group_members" does not exist'))
    out = await collect(conn, CASE)
    assert out["products"][0]["product_group_id"] is None
    assert any("product_group_id not collected" in note
               for note in out["evidence_provenance"]["notes"])


async def test_the_canonical_sku_stub_is_not_offered_as_a_merchant_variant():
    """source_variant_id == product_key is the canonical stub; the validator rejects such a
    variant, so reporting it as the merchant's would manufacture a passing-looking field."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "ext:retailer:a::canonical",
               "source_variant_id": "ext:retailer:a", "sku_payload": json.dumps({})}],
    )
    product = (await collect(conn, CASE))["products"][0]
    assert product["variant_id"] is None


def test_every_selected_column_exists_in_the_schema():
    """The fake connection cannot see a column name, and that is how the first version shipped
    selecting catalog_products.market/currency — columns that table does not have. This compares
    each statement's `alias.column` tokens against the model metadata."""
    import re

    from db.catalog import beauty_sku_ingredients, catalog_offers, catalog_products, catalog_skus
    from scripts.collect_curated_canary_evidence import INCI_SQL, OFFER_SQL, PRODUCT_SQL, SKU_SQL

    for sql, alias, table in ((PRODUCT_SQL, "p", catalog_products), (SKU_SQL, "s", catalog_skus),
                              (OFFER_SQL, "o", catalog_offers), (INCI_SQL, "b", beauty_sku_ingredients)):
        referenced = set(re.findall(rf"\b{alias}\.([a-z_]+)", sql))
        known = set(table.c.keys())
        assert referenced <= known, (
            f"{table.name}: {sorted(referenced - known)} not in the schema — "
            f"this raises UndefinedColumnError against the real database")
        assert referenced, f"no {alias}.column tokens found in the statement for {table.name}"


async def test_an_offer_reports_its_own_skus_variant_not_the_products():
    """Copying the product's variant into the offer makes the validator's
    (product_key, variant_id, ...) tuple check true by construction."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "sku_one", "source_variant_id": "45001",
               "sku_payload": json.dumps({"variant_id_provenance": "merchant_issued"})},
              {"product_key": "ext:retailer:a", "sku_key": "sku_two", "source_variant_id": "45002",
               "sku_payload": json.dumps({"variant_id_provenance": "merchant_issued"})}],
        offers=[{"product_key": "ext:retailer:a", "sku_key": "sku_two", "merchant_id": "m",
                 "currency": "USD", "market": "US", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": "eyurs.com", "destination_url": "https://eyurs.com/products/x"}],
    )
    out = await collect(conn, CASE)
    assert out["offers"][0]["variant_id"] == "45002", "the offer must report the SKU it references"


async def test_more_than_one_merchant_variant_is_ambiguous_not_arbitrary():
    """Taking variants[0] from an unordered result reports an arbitrary id as THE merchant
    variant — a measurement nobody made."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        skus=[{"product_key": "ext:retailer:a", "sku_key": "sku_one", "source_variant_id": "45001",
               "sku_payload": json.dumps({})},
              {"product_key": "ext:retailer:a", "sku_key": "sku_two", "source_variant_id": "45002",
               "sku_payload": json.dumps({})}],
    )
    out = await collect(conn, CASE)
    assert out["products"][0]["variant_id"] is None
    assert any("merchant variants" in note for note in out["evidence_provenance"]["notes"])


async def test_offers_that_disagree_about_market_are_reported():
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a")],
        offers=[{"product_key": "ext:retailer:a", "sku_key": "s1", "merchant_id": "m", "currency": "USD",
                 "market": "US", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": "eyurs.com", "destination_url": "https://eyurs.com/x"},
                {"product_key": "ext:retailer:a", "sku_key": "s2", "merchant_id": "m", "currency": "USD",
                 "market": "KR", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": "eyurs.com", "destination_url": "https://eyurs.com/y"}],
    )
    out = await collect(conn, CASE)
    assert out["products"][0]["market"] is None
    assert any("disagree on market" in note for note in out["evidence_provenance"]["notes"])


async def test_a_complete_file_passes_so_the_incompleteness_test_has_a_control():
    """Without this, 'collected evidence does not pass' would hold for any broken collector."""
    conn = FakeConn(
        products=[product_row("eyurs.com", "ext:retailer:a"), product_row("ohlolly.com", "ext:retailer:b")],
        skus=[{"product_key": f"ext:retailer:{k}", "sku_key": f"sku_{k}", "source_variant_id": f"4500{i}",
               "sku_payload": json.dumps({"variant_id_provenance": "merchant_issued"})}
              for i, k in enumerate("ab", start=1)],
        offers=[{"product_key": f"ext:retailer:{k}", "sku_key": f"sku_{k}", "merchant_id": f"merch_{h}",
                 "currency": "USD", "market": "US", "offer_type": "retailer", "offer_mode": "redirect",
                 "source_domain": h, "destination_url": f"https://{h}/products/x"}
                for k, h in (("a", "eyurs.com"), ("b", "ohlolly.com"))],
        incis=[{"product_key": "ext:retailer:a", "source_system": "reseller_listing", "raw_inci_chars": 868},
               {"product_key": "ext:retailer:b", "source_system": "reseller_listing", "raw_inci_chars": 869}],
        groups=[{"product_key": "ext:retailer:a", "product_group_id": "pg"},
                {"product_key": "ext:retailer:b", "product_group_id": "pg"}],
    )
    out = await collect(conn, CASE)
    out["gateway_revision"] = "gw"
    out["source_artifacts"] = ["job.log"]
    out["evidence_provenance"]["backend_revision"] = out["backend_revision"] = "abc"
    out["crawl"] = {"status": "complete", "selected_products": 2}
    keys = [p["product_key"] for p in out["products"]]
    for surface in SURFACES:
        out[surface] = list(keys)
    for field in ("second_ingest_added_product_keys", "second_ingest_added_sku_keys",
                  "second_ingest_added_offer_keys", "identity_failures"):
        out[field] = []
    result = evaluate({"cases": [CASE]}, {"two_sellers": out})
    assert result["passed"] == 1, result["cases"][0]["reasons"]


def test_the_group_statement_is_also_checked_against_the_schema():
    """GROUP_SQL was omitted from the parity check, which is exactly the statement whose broken
    shape (product_group_members.product_key) the first revision shipped."""
    import re

    from db.catalog import catalog_products
    from scripts.collect_curated_canary_evidence import GROUP_SQL

    product_tokens = set(re.findall(r"\bp\.([a-z_]+)", GROUP_SQL))
    assert product_tokens, "no p.column tokens found in GROUP_SQL"
    assert product_tokens <= set(catalog_products.c.keys()), sorted(product_tokens)

    # product_group_members has no Table(); parse its real columns out of the migration.
    from pathlib import Path

    ddl = Path(__file__).resolve().parents[1].joinpath("db/migrations/045_product_groups.sql").read_text()
    create = ddl.split("CREATE TABLE IF NOT EXISTS product_group_members", 1)[1].split(");", 1)[0]
    declared = set(re.findall(r"^\s*([a-z_]+)\s+[A-Z]", create, re.MULTILINE))
    declared |= set(re.findall(r"ADD COLUMN IF NOT EXISTS ([a-z_]+)", ddl))
    member_tokens = set(re.findall(r"\bm\.([a-z_]+)", GROUP_SQL))
    assert member_tokens, "no m.column tokens found in GROUP_SQL"
    assert member_tokens <= declared, (
        f"{sorted(member_tokens - declared)} are not columns of product_group_members "
        f"(declared: {sorted(declared)})")


async def test_the_statements_carry_the_filters_the_collector_depends_on():
    """The fake ignores WHERE clauses and the parity regex only checks that a token exists, so
    dropping a filter is invisible to every other test here. These are the filters whose absence
    changes WHICH rows become evidence."""
    conn = FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")])
    await collect(conn, CASE)
    sent = {sql for sql, _ in conn.seen}
    products_sql = next(s for s in sent if "FROM catalog_products p" in s)
    skus_sql = next(s for s in sent if "FROM catalog_skus" in s)
    offers_sql = next(s for s in sent if "FROM catalog_offers" in s)

    assert "lower(" in products_sql, "host matching must be case-insensitive"
    assert "p.suppressed_at IS NULL" in products_sql, "a withdrawn product is not evidence"
    assert "s.suppressed_at IS NULL" in skus_sql, "a withdrawn SKU is not a merchant variant"
    assert "o.suppressed_at IS NULL" in offers_sql, "a suppressed offer does not resolve"
    assert "ORDER BY" in skus_sql, "variant selection must not depend on result order"


async def test_a_case_declared_variant_disambiguates_but_only_if_it_exists():
    skus = [{"product_key": "ext:retailer:a", "sku_key": "s1", "source_variant_id": "45001",
             "sku_payload": json.dumps({"variant_id_provenance": "merchant_issued"})},
            {"product_key": "ext:retailer:a", "sku_key": "s2", "source_variant_id": "45002",
             "sku_payload": json.dumps({"variant_id_provenance": "merchant_issued"})}]
    case = dict(CASE, observed_source_variants={"eyurs.com": "45002"})
    out = await collect(FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")], skus=skus), case)
    assert out["products"][0]["variant_id"] == "45002"

    # A declaration matching nothing is a stale manifest, not evidence.
    stale = dict(CASE, observed_source_variants={"eyurs.com": "99999"})
    out = await collect(FakeConn(products=[product_row("eyurs.com", "ext:retailer:a")], skus=skus), stale)
    assert out["products"][0]["variant_id"] is None
    assert any("but the stored rows carry" in n for n in out["evidence_provenance"]["notes"])


def _fake_asyncpg(monkeypatch, rows=None):
    """A stand-in for the driver so the entrypoint's own logic is testable offline."""
    import sys
    import types

    class FakeTx:
        async def start(self): return None
        async def rollback(self): return None

    class Conn(FakeConn):
        def transaction(self, **kw): return FakeTx()
        async def close(self): return None

    conn = Conn(**(rows or {"products": []}))
    module = types.ModuleType("asyncpg")

    async def connect(url, **kwargs):
        conn.connect_kwargs = kwargs
        conn.url = url
        return conn

    module.connect = connect
    monkeypatch.setitem(sys.modules, "asyncpg", module)
    return conn


def test_it_refuses_to_emit_when_it_cannot_identify_the_build(monkeypatch, tmp_path, capsys):
    """Emitting anyway produces a file the validator rejects for 'missing revisions' — a reason
    that reads as a data problem rather than 'this did not run on a stamped image'."""
    import json as _json

    from scripts import collect_curated_canary_evidence as collector

    _fake_asyncpg(monkeypatch)
    monkeypatch.setattr(collector, "commit_sha", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/db")
    manifest = tmp_path / "m.json"
    manifest.write_text(_json.dumps({"cases": [CASE]}))

    rc = collector.main(["--manifest", str(manifest), "--case-id", CASE["case_id"]])
    out = capsys.readouterr()
    assert rc == 2
    assert out.out.strip() == "", "nothing may be emitted when the build is unidentifiable"
    assert "REFUSED" in out.err


def test_it_prints_the_evidence_to_stdout_because_the_job_container_is_deleted(monkeypatch, tmp_path, capsys):
    import json as _json

    from scripts import collect_curated_canary_evidence as collector

    conn = _fake_asyncpg(monkeypatch, {"products": [product_row("eyurs.com", "ext:retailer:a")]})
    monkeypatch.setattr(collector, "commit_sha", lambda *a, **k: "abc123")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u@h/db")
    manifest = tmp_path / "m.json"
    manifest.write_text(_json.dumps({"cases": [CASE]}))

    rc = collector.main(["--manifest", str(manifest), "--case-id", CASE["case_id"]])
    out = capsys.readouterr()
    assert rc == 0
    document = _json.loads(out.out)
    assert document[CASE["case_id"]]["evidence_provenance"]["backend_revision"] == "abc123"
    assert "DIGEST " in out.err and "SUMMARY " in out.err
    # The driver URL must lose the SQLAlchemy dialect prefix, and carry timeouts.
    assert conn.url.startswith("postgresql://")
    assert conn.connect_kwargs.get("timeout") and conn.connect_kwargs.get("command_timeout")


def test_the_digest_covers_the_rows_not_the_header(monkeypatch):
    """A digest over provenance alone would change with the clock and say nothing about content."""
    from scripts.collect_curated_canary_evidence import content_digest

    base = {"products": [{"product_key": "a"}], "offers": [], "evidence_provenance": {"collected_at": "t1"}}
    same_rows_later = {"products": [{"product_key": "a"}], "offers": [], "evidence_provenance": {"collected_at": "t2"}}
    changed_rows = {"products": [{"product_key": "b"}], "offers": [], "evidence_provenance": {"collected_at": "t1"}}
    assert content_digest(base) == content_digest(same_rows_later)
    assert content_digest(base) != content_digest(changed_rows)
