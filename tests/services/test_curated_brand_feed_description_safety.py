"""Safety text survives the description cleanup (post-merge review of #2578).

A warning can share a sentence with a store-boilerplate anchor ("contact us", "cannot be shipped") or repeat on
every product of a store ("Avoid contact with eyes"). Both cleanup passes must keep it: the pattern pass keeps a
safety sentence whole, or cuts only a `;`-separated clause that is pure boilerplate with no safety cue of its own,
and the store-template repetition pass never drops a safety sentence. Run through the real producer.
"""
import pytest

from services import curated_brand_feed as cbf
from services.curated_brand_feed import body_html_to_text, drop_repeated_store_sentences, is_safety_text


@pytest.mark.parametrize("body, kept", [
    ("<p>A gentle cleanser.</p><p>Discontinue use if irritation occurs and contact us for advice.</p>",
     "Discontinue use if irritation occurs and contact us for advice."),
    ("<p>A calming balm.</p><p>Do not use on broken skin; if a reaction occurs, call us at 555-0100 or see a doctor.</p>",
     "Do not use on broken skin; if a reaction occurs, call us at 555-0100 or see a doctor."),
    ("<p>Sun mist SPF 50.</p><p>Warning: flammable aerosol; contents under pressure, cannot be shipped by air.</p>",
     "Warning: flammable aerosol; contents under pressure, cannot be shipped by air."),
    ("<p>Nail lacquer.</p><p>Keep out of reach of children. Free shipping on orders over $50.</p>",
     "Keep out of reach of children."),
])
def test_a_warning_beside_a_boilerplate_anchor_is_kept(body, kept):
    out = body_html_to_text(body)
    assert kept in out, out


def test_only_a_pure_boilerplate_clause_is_cut_from_a_safety_sentence():
    out = body_html_to_text("<p>A gel cleanser.</p>"
                            "<p>Avoid contact with eyes; free shipping on orders over $40.</p>")
    assert out == "A gel cleanser. Avoid contact with eyes.", out


def test_plain_boilerplate_is_still_removed():
    assert body_html_to_text("<p>A gel cleanser.</p><p>Free shipping on orders over $40.</p>") == "A gel cleanser."
    assert body_html_to_text("<p>A gel cleanser.</p><p>Please contact us with any questions.</p>") == "A gel cleanser."


@pytest.mark.parametrize("sentence", [
    "Warning: flammable.", "Caution: for external use only.", "Avoid contact with eyes.",
    "Discontinue use if irritation occurs.", "Keep out of reach of children.", "Patch test before use.",
    "Consult a physician if pregnant.", "Contents under pressure.",
])
def test_safety_cues(sentence):
    assert is_safety_text(sentence)


def test_product_copy_without_a_cue_is_not_safety_text():
    assert not is_safety_text("A creamy satin lipstick that glides on in one swipe.")


SAFETY = "Avoid contact with eyes; if contact occurs, rinse thoroughly with water."
FOOTER = "We ship worldwide from our studio every single business day."


def _batch():
    """12 rows of 3 kinds, every row ending with the same safety line and the same store footer."""
    kinds = ["serum", "cleanser", "toner"] * 4
    rows = [[f"Product {i} brightens with a light texture that sinks in fast.", SAFETY, FOOTER] for i in range(12)]
    return rows, kinds


def test_the_repetition_pass_never_drops_a_safety_sentence():
    rows, kinds = _batch()
    out, report = drop_repeated_store_sentences(rows, kinds=kinds)
    assert all(SAFETY in r for r in out), out
    assert all(FOOTER not in r for r in out)  # the store template is still removed
    assert report["rows_changed"] == 12


def test_a_safety_sentence_survives_records_for_brand(monkeypatch):
    """End to end through records_for_brand's batch pass."""
    products = []
    for i in range(12):
        ptype = ["Serum", "Cleanser", "Toner"][i % 3]
        products.append({"id": 9100 + i, "title": f"Example {ptype} No {i}", "handle": f"example-{i}",
                         "vendor": "Example Brand", "product_type": ptype, "images": [{"src": f"https://cdn.example.com/{i}.jpg"}],
                         "variants": [{"id": 91000 + i, "price": "25.00", "available": True}],
                         "body_html": f"<p>Example {ptype.lower()} number {i} with a light, fast-absorbing texture.</p>"
                                      f"<p>{SAFETY}</p><p>{FOOTER}</p>"})

    async def _products(domain, **kw):
        return cbf.ShopifyProductBatch(products, scanned_products=len(products), pages=1)

    async def _locale(domain, **kw):
        return {"currency": "USD"}
    monkeypatch.setattr(cbf, "fetch_shopify_products", _products)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", _locale)
    import asyncio
    recs = asyncio.run(cbf.records_for_brand(domain="store.example", category_path="beauty"))
    summaries = [r["pdp"]["attribute_summary"] for r in recs]
    assert summaries and all("Avoid contact with eyes" in s for s in summaries), summaries
    assert all("We ship worldwide" not in s for s in summaries), summaries  # the pass really ran
