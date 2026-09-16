"""The dry-run/apply plan must disclose WHICH products it contains, not only how many.

A count-only plan line cannot distinguish a stable cohort from one that churned while its
size held steady: a retailer that delists the exact product a canary selected and lists a
different one the same day still plans the same number of PDPs, and an acceptance gate
reading `pdps=17` would pass it. scripts/validate_meitu_canary_evidence.py asserts a
specific GTIN and a `beauty/makeup/lip/` leaf per product, so those two fields in
particular have to be visible before anything is applied.
"""
import argparse
import json

from scripts import onboard_curated_brands as cli
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl


def shopify_product(number, *, gtin, title="Honey & Milk Lip Oil"):
    return {
        "id": 9000000 + number, "vendor": "A'PIEU", "title": title,
        "handle": f"lip-oil-{number}", "product_type": "Lip Oil",
        "body_html": "<p>Ingredients: Water, Glycerin, Panthenol</p>",
        "images": [{"src": "https://cdn.example/item.jpg"}],
        "variants": [{"id": 45000000000000 + number, "price": "19.00", "available": True,
                      "option1": "Peach", "sku": f"LIP{number}", "barcode": gtin}],
    }


def plan_for(*pairs):
    records = [
        feed.shopify_product_to_record(
            shopify_product(n, gtin=gtin, title=title), domain="eyurs.com",
            category_path="beauty/makeup", brand_override="A'PIEU", currency="USD",
            source_role="retailer", retailer_name="eyurs.com",
        )
        for n, (gtin, title) in enumerate(pairs, start=1)
    ]
    return ingest_validated_jsonl(records)


def printed_rows(capsys):
    out = capsys.readouterr().out
    return [json.loads(line.split("pdp ", 1)[1]) for line in out.splitlines() if "pdp {" in line]


def test_every_planned_pdp_discloses_its_own_gtin_and_category(capsys):
    plan = plan_for(("8809530070499", "Honey & Milk Lip Oil"),
                    ("8809530070505", "Juicy Pang Lip Oil"))
    cli._print_plan_identity(plan, limit=50)
    rows = printed_rows(capsys)

    assert len(rows) == len(plan["pdps"]) == 2
    # Values must come from each ROW, not from a shared constant: a printer that emitted a
    # fixed object, or the first row twice, still satisfies "two lines were printed".
    # Plan rows carry the GTIN-14 form: a 13-digit merchant barcode is stored zero-padded
    # ("8809530070499" -> "08809530070499"), which is why docs/meitu_onboarding_acceptance.md
    # records the lip oil as 08809530070499. An equality check against the merchant's own
    # spelling would fail here even though the identity is right; the acceptance validator
    # normalises both sides (validated_source_gtin) before comparing.
    assert {r["gtin"] for r in rows} == {"08809530070499", "08809530070505"}
    assert {r["title"] for r in rows} == {"Honey & Milk Lip Oil", "Juicy Pang Lip Oil"}
    assert {r["product_key"] for r in rows} == {p["product_key"] for p in plan["pdps"]}
    # The fields the acceptance validator asserts are present and populated. Every printed
    # name must exist on a real planned row: a key absent from the row prints as None and
    # would read as a missing/unresolved value rather than as a field this lane never sets.
    for row in rows:
        assert row["category_path"].startswith("beauty/makeup/lip/"), row
        assert row["content_key"]
        assert row["merchant_id"] and row["source_domain"] == "eyurs.com", row
    planned = {p["product_key"]: p for p in plan["pdps"]}
    for row in rows:
        # Derived fields are computed from plan["skus"] and are documented as such; every
        # OTHER printed name must exist on the row, or it prints None and reads as a
        # missing value rather than as a field this lane never sets.
        assert set(row) - set(cli._PLAN_DERIVED_FIELDS) <= set(planned[row["product_key"]]), (
            "printed a field the plan row lacks")


def test_a_truncated_plan_says_how_many_rows_it_withheld(capsys):
    plan = plan_for(*[(f"880953007049{n}", f"Lip Oil {n}") for n in range(4)])
    cli._print_plan_identity(plan, limit=2)
    out = capsys.readouterr().out
    assert out.count("pdp {") == 2
    # Silence about the remainder would read as "this is the whole plan".
    assert "2 further PDP row(s) not printed" in out


def test_limit_zero_prints_every_row(capsys):
    plan = plan_for(*[(f"880953007049{n}", f"Lip Oil {n}") for n in range(4)])
    cli._print_plan_identity(plan, limit=0)
    out = capsys.readouterr().out
    assert out.count("pdp {") == 4
    assert "not printed" not in out


def test_apply_discloses_the_rows_it_is_about_to_write(monkeypatch, capsys):
    """--apply re-crawls and re-plans, so the rows it is about to WRITE are not necessarily
    the reviewed ones. Asserted by RUNNING the apply path, not by reading its source: a
    source-order check passes against `_print_plan_identity(...) if not args.apply else None`,
    which prints nothing on apply.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from db import database as db_module
    from services.catalog_enrichment_agent import ingestion

    # Native variants, as the canary runs with --emit-real-variants: without them a
    # retailer plan is refused for having no commerce chain, which is itself covered below.
    records = [feed.shopify_product_to_record(
        shopify_product(1, gtin="8809530070499"), domain="eyurs.com",
        category_path="beauty/makeup", brand_override="A'PIEU", currency="USD",
        source_role="retailer", retailer_name="eyurs.com", emit_native_variants=True,
    )]
    # The CLI refuses a plan whose crawl completeness was not proven, so the stub must
    # carry a real crawl_report — the same shape fetch_shopify_products returns.
    fetch = AsyncMock(return_value=feed.ShopifyProductBatch(records, scanned_products=1, pages=1))
    fetch.last_vendor_filter_report = None
    fetch.last_brand_census = None
    fetch.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    plan = ingestion.ingest_validated_jsonl(records)
    monkeypatch.setattr(cli, "apply_ingest_plan", AsyncMock(
        return_value={name: len(plan[name]) for name in ("pdps", "skus", "offers")}))
    monkeypatch.setattr(db_module, "database",
                        SimpleNamespace(is_connected=True, disconnect=AsyncMock()))

    assert cli.main(["--domain", "eyurs.com", "--category", "beauty", "--brand", "A'PIEU",
                     "--only-vendor", "A'PIEU", "--source-role", "retailer", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "pdp {" in out, "--apply wrote rows without disclosing which ones"
    assert "08809530070499" in out
    assert "DRY-RUN" not in out
    # Disclosure precedes the write, so it is a record of what was ABOUT to be written —
    # and it survives a plan the readiness preflight then refuses.
    assert out.index("pdp {") < out.index("primary ingestion: {\"applied\"") if \
        "primary ingestion: {\"applied\"" in out else True


def test_apply_defaults_to_disclosing_every_row(capsys):
    """A truncated apply leaves most of the write unrecorded; an explicit flag still wins."""
    plan = plan_for(*[(f"880953007049{n}", f"Lip Oil {n}") for n in range(4)])
    args = argparse.Namespace(apply=True, plan_print_limit=None)
    cli._print_plan_identity(plan, limit=cli._effective_print_limit(args))
    assert capsys.readouterr().out.count("pdp {") == 4

    assert cli._effective_print_limit(argparse.Namespace(apply=True, plan_print_limit=2)) == 2
    assert cli._effective_print_limit(
        argparse.Namespace(apply=False, plan_print_limit=None)) == cli._PLAN_PRINT_DEFAULT


def test_non_ascii_titles_stay_greppable(capsys):
    """A gate greps these lines; \\uXXXX escaping hides exactly the K-beauty cohort."""
    plan = plan_for(("8809530070499", "A'PIEU 허니 앤 밀크 립 오일"))
    cli._print_plan_identity(plan, limit=0)
    out = capsys.readouterr().out
    assert "허니 앤 밀크" in out and "\\u" not in out


def test_merchant_variant_ids_are_disclosed_and_the_canonical_stub_is_not(capsys):
    """The validator rejects a variant_id that is really the product key (ext:/sig_), so a
    plan that carries only the canonical stub must not look like it has variant identity."""
    two_variants = [
        {"id": 45000000000001, "price": "19.00", "available": True, "option1": "5g",
         "sku": "LIP1", "barcode": "8809530070499"},
        {"id": 45000000000002, "price": "21.00", "available": True, "option1": "10g",
         "sku": "LIP2", "barcode": "8809530070505"},
    ]
    record = feed.shopify_product_to_record(
        {**shopify_product(1, gtin="8809530070499"), "variants": two_variants},
        domain="eyurs.com", category_path="beauty/makeup", brand_override="A'PIEU",
        currency="USD", source_role="retailer", retailer_name="eyurs.com",
        emit_native_variants=True,
    )
    plan = ingest_validated_jsonl([record])
    cli._print_plan_identity(plan, limit=0)
    row = printed_rows(capsys)[0]
    assert row["variant_count"] == 2
    assert row["variant_ids"] == ["45000000000001", "45000000000002"]
    assert not any(str(v).startswith(("ext:", "sig_")) for v in row["variant_ids"])

    # Without native variants only the canonical stub SKU exists: disclose nothing rather
    # than restate the product key as if it were a merchant variant.
    capsys.readouterr()
    cli._print_plan_identity(plan_for(("8809530070499", "Lip Oil")), limit=0)
    stub_row = printed_rows(capsys)[0]
    assert stub_row["variant_ids"] == [] and stub_row["variant_count"] == 0

