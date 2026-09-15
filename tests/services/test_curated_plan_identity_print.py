"""The dry-run/apply plan must disclose WHICH products it contains, not only how many.

A count-only plan line cannot distinguish a stable cohort from one that churned while its
size held steady: a retailer that delists the exact product a canary selected and lists a
different one the same day still plans the same number of PDPs, and an acceptance gate
reading `pdps=17` would pass it. scripts/validate_meitu_canary_evidence.py asserts a
specific GTIN and a `beauty/makeup/lip/` leaf per product, so those two fields in
particular have to be visible before anything is applied.
"""
import inspect
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
        assert set(row) <= set(planned[row["product_key"]]), "printed a field the plan row lacks"


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


def test_identity_is_printed_before_the_apply_branch():
    """--apply re-crawls and re-plans, so the rows it is about to WRITE are not
    necessarily the reviewed ones. The print must not sit inside the dry-run branch."""
    source = inspect.getsource(cli._run)
    assert "_print_plan_identity(" in source
    assert source.index("_print_plan_identity(") < source.index("if not args.apply")
