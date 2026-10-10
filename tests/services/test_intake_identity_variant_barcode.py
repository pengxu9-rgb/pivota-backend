"""F2 (2026-10-10): Tier-0a over variant barcodes, and the barcode-reuse guard.

Census 2026-10-10 (reports/coverage_census_2026_10_10/matching_diagnosis.md):
  - beautyencounter lists "Shalimar by Guerlain for Women" as ONE product with 10 size variants; its
    barcodes live on the SKUs only (product gtin NULL), so Tier-0a never linked it to perfumania's
    "Shalimar Perfume" (product gtin 03346470113541). 88 such cross-seller keys in 46 brands.
  - universalnailsupplies puts barcode 00619828139641 on 4 different OPI shades; Tier-0a merged them
    into one key and only raised gtin_match_brand_title_drift.

Every catalog row here is what the REAL producer emits: a Shopify product JSON through
curated_brand_feed.shopify_product_to_record -> ingestion.ingest_validated_jsonl (the crawl lane's
plan: a multi-variant product's gtin is None, its SKUs carry the barcode as the store gave it, plus a
synthetic `::canonical` SKU). The fake catalog answers the lookups from those planned rows the way
barcode_lookup_sql does (the SQL itself runs on Postgres in
tests/test_intake_identity_barcode_lookup_postgres.py). Incoming listings go through
apply._apply_pdp_identity_gate with the plan's SKUs, as the drain does.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.intake_identity as ii
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion as ing
from services.catalog_enrichment_agent.apply import _apply_pdp_identity_gate, _plan_variants_by_product
from services.catalog_identity import make_content_key
from services.product_group_autogrouper import make_singleton_product_group_id


def gtin13(stem12: str) -> str:
    """A check-digit-valid EAN-13 from 12 digits."""
    weighted = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(stem12)))
    return stem12 + str((10 - weighted % 10) % 10)


SHALIMAR = "3346470113541"          # perfumania's product gtin in the census (03346470113541)
SHALIMAR_16 = gtin13("334647011360")
SHALIMAR_30 = gtin13("334647011370")
OPI_REUSED = "619828139641"          # 00619828139641: on 4 OPI shades at universalnailsupplies
assert ii.canonical_gtin(SHALIMAR) == "03346470113541"
assert ii.canonical_gtin(OPI_REUSED) == "00619828139641"

_next_variant_id = [43603819690000]


def _variant(title: str, barcode: Optional[str], price: str = "25.00") -> Dict[str, Any]:
    _next_variant_id[0] += 1
    return {"id": _next_variant_id[0], "title": title, "barcode": barcode, "sku": f"S{_next_variant_id[0]}",
            "price": price, "available": True}


def plan_for(domain: str, title: str, vendor: str, variants: List[Dict[str, Any]], *,
             source_role: str = "retailer") -> Dict[str, Any]:
    """The crawl lane's plan for one Shopify product (the real producer, no DB)."""
    handle = "-".join(title.lower().replace("#", "").replace("'", "").split())
    product = {"id": 8100000000000 + len(handle), "title": title, "handle": handle, "vendor": vendor,
               "product_type": "Beauty", "variants": variants}
    record = feed.shopify_product_to_record(
        product, domain=domain, category_path="beauty", currency="USD",
        source_role=source_role, emit_native_variants=True,
    )
    plan = ing.ingest_validated_jsonl([record])
    assert plan["skipped"] == 0 and len(plan["pdps"]) == 1
    return plan


class FakeCatalog:
    """catalog_products + catalog_skus + product_group_members, held as the planned rows."""

    def __init__(self) -> None:
        self.products: Dict[str, Dict[str, Any]] = {}
        self.skus: List[Dict[str, Any]] = []
        self.groups: Dict[tuple, str] = {}
        self.barcode_calls: List[List[str]] = []
        self._clock = 0

    def add(self, plan: Dict[str, Any], *, content_key: Optional[str] = None) -> Dict[str, Any]:
        """Store a plan as the apply would have: the product row (oldest first), its SKUs, and its
        singleton group membership. `content_key` overrides the row's key (a past Tier-0a attach)."""
        pdp = dict(plan["pdps"][0])
        self._clock += 1
        pdp["created_at"] = f"2026-09-{self._clock:02d} 00:00:00+00:00"
        pdp["suppression_reason"] = None
        # The plan's own key is the gate-OFF writer's (it folds the GTIN in); with the gate on, as in
        # prod, a first sighting is MINTed on the GTIN-less family key -- what was stored.
        pdp["content_key"] = content_key or make_content_key(pdp["brand"], pdp["title"])
        self.products[pdp["product_key"]] = pdp
        self.skus.extend(dict(s, suppression_reason=None) for s in plan["skus"])
        self.groups[(pdp["merchant_id"], pdp["platform"], pdp["source_product_id"])] = (
            make_singleton_product_group_id(pdp["content_key"]))
        return pdp

    def _order(self, rows: List[Dict[str, Any]], prefer: Optional[str]) -> List[Dict[str, Any]]:
        return sorted(rows, key=lambda r: (r["merchant_id"] != (prefer or ""), r["created_at"], r["product_key"]))

    _COLS = ("product_key", "merchant_id", "platform", "source_product_id", "canonical_url", "title", "brand",
             "content_key", "gtin", "pivota_signature_id", "pivota_canonical_url", "source_domain", "created_at")

    async def rows_by_gtin(self, gtin14: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        live = [p for p in self.products.values() if p.get("gtin") == gtin14 and p["suppression_reason"] is None]
        return [{k: p.get(k) for k in self._COLS} for p in self._order(live, prefer)[:5]]

    async def rows_by_barcodes(self, barcodes: List[str], prefer: Optional[str]) -> List[Dict[str, Any]]:
        """barcode_lookup_sql's semantics: IN over the spellings, both arms, live rows only."""
        self.barcode_calls.append(list(barcodes))
        spellings = {s for b in barcodes for s in ii.gtin_spellings(b)}
        hits = []
        for p in self.products.values():
            if p["suppression_reason"] is None and p.get("gtin") in spellings:
                hits.append((p, {"matched_barcode": p["gtin"], "match_source": "product_gtin",
                                 "sku_key": None, "sku_title": None}))
        for s in self.skus:
            p = self.products.get(s["product_key"])
            if (p and p["suppression_reason"] is None and s["suppression_reason"] is None
                    and s.get("barcode") in spellings):
                hits.append((p, {"matched_barcode": s["barcode"], "match_source": "sku_barcode",
                                 "sku_key": s["sku_key"], "sku_title": s["title"]}))
        hits.sort(key=lambda h: (h[0]["merchant_id"] != (prefer or ""), h[0]["created_at"],
                                 h[0]["product_key"], h[1]["match_source"]))
        out = []
        for p, hit in hits[:ii.BARCODE_LOOKUP_LIMIT]:
            row = {k: p.get(k) for k in self._COLS}
            row.update(hit)
            row["matched_gtin"] = ii.canonical_gtin(hit["matched_barcode"])
            out.append(row)
        return out

    async def rows_by_content_key(self, ck: str, prefer: Optional[str]) -> List[Dict[str, Any]]:
        live = [p for p in self.products.values() if p.get("content_key") == ck and p["suppression_reason"] is None]
        return [{k: p.get(k) for k in self._COLS} for p in self._order(live, prefer)[:5]]

    async def existing_pg(self, row: Dict[str, Any]) -> Optional[str]:
        return self.groups.get((str(row.get("merchant_id") or ""), str(row.get("platform") or ""),
                                str(row.get("source_product_id") or "")))


@pytest.fixture()
def catalog(monkeypatch: pytest.MonkeyPatch):
    cat = FakeCatalog()
    cat.provenance: List[Dict[str, Any]] = []
    cat.reviews: List[Dict[str, Any]] = []

    async def none_rows(*a: Any, **k: Any) -> List[Dict[str, Any]]:
        return []

    async def capture_provenance(p: Dict[str, Any]) -> None:
        cat.provenance.append(p)

    async def capture_review(door, ctx, ck, matcher, detail) -> None:
        cat.reviews.append({"product_key": ctx.get("product_key"), "matcher": matcher, "detail": detail})

    async def open_review(product_key, matcher) -> bool:
        return any(r["product_key"] == product_key and r["matcher"] == matcher for r in cat.reviews)

    async def guard_proceed(merchant_id, fields, **kw):
        return {"action": "proceed", "reason": "no_conflict"}

    import services.audit_index_intake as intake

    monkeypatch.setenv(ii.VARIANT_BARCODE_MATCH_ENV, "1")
    monkeypatch.setattr(ii, "_rows_by_gtin", cat.rows_by_gtin)
    monkeypatch.setattr(ii, "_rows_by_barcodes", cat.rows_by_barcodes)
    monkeypatch.setattr(ii, "_rows_by_content_key", cat.rows_by_content_key)
    monkeypatch.setattr(ii, "_candidates_by_canonical_url", none_rows)
    monkeypatch.setattr(ii, "_candidates_by_source_id", none_rows)
    monkeypatch.setattr(ii, "_existing_pg_for_listing", cat.existing_pg)
    monkeypatch.setattr(ii, "_write_provenance", capture_provenance)
    monkeypatch.setattr(ii, "_flag_review", capture_review)
    monkeypatch.setattr(ii, "_open_identity_review_exists", open_review)
    monkeypatch.setattr(intake, "apply_intake_brand_fragmentation_guard", guard_proceed)
    return cat


async def crawl(plan: Dict[str, Any]) -> Dict[str, Any]:
    """The drain's identity gate for the plan's one product, with the plan's SKUs as variants."""
    pdp = plan["pdps"][0]
    targets: Dict[str, str] = {}
    assert await _apply_pdp_identity_gate(
        pdp, identity_gate_on=True, group_targets=targets,
        variants=_plan_variants_by_product(plan["skus"]).get(pdp["product_key"]),
    )
    return {"pdp": pdp, "group": targets.get(pdp["product_key"])}


def _last(cat: FakeCatalog) -> Dict[str, Any]:
    return cat.provenance[-1]


SHALIMAR_FAMILY = [_variant("1.0 oz EDP Spray", SHALIMAR), _variant("1.6 oz EDP Spray", SHALIMAR_16),
                   _variant("3.0 oz EDP Spray", SHALIMAR_30)]


# --- the real producer's shape -------------------------------------------------------------------


def test_the_crawl_plan_keeps_a_familys_barcodes_on_its_skus_only():
    """What the matcher has to read: product gtin None, raw barcodes on native SKUs, canonical SKU bare."""
    plan = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", SHALIMAR_FAMILY)
    assert plan["pdps"][0]["gtin"] is None
    by_kind = {s["sku_key"].endswith("::canonical"): s for s in plan["skus"]}
    assert by_kind[True]["barcode"] is None
    native = [s for s in plan["skus"] if "::v:" in s["sku_key"]]
    assert sorted(s["barcode"] for s in native) == sorted([SHALIMAR, SHALIMAR_16, SHALIMAR_30])
    barcodes, reused = ii.incoming_barcodes(None, _plan_variants_by_product(plan["skus"])[plan["pdps"][0]["product_key"]])
    assert barcodes == [ii.canonical_gtin(b) for b in (SHALIMAR, SHALIMAR_16, SHALIMAR_30)]
    assert reused == {}


def test_a_single_variant_plans_canonical_and_native_sku_are_not_a_reuse():
    """The ::canonical SKU repeats the product barcode under the product title beside the native
    "Default Title" variant: the guard must not read that as one barcode on two variants."""
    plan = plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)])
    skus = plan["skus"]
    assert {s["barcode"] for s in skus} == {SHALIMAR} and len(skus) == 2
    barcodes, reused = ii.incoming_barcodes(plan["pdps"][0]["gtin"], skus)
    assert barcodes == ["03346470113541"] and reused == {}
    matches = [{**plan["pdps"][0], "match_source": "sku_barcode", "sku_key": s["sku_key"], "sku_title": s["title"]}
               for s in skus]
    assert ii.barcode_reuse_reason(matches) is None


# --- Tier-0a over variant barcodes ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_family_listing_attaches_to_another_sellers_product_by_a_variant_barcode(catalog):
    """beautyencounter's 3-size family (barcodes on SKUs only) joins perfumania's Shalimar, which
    carries one of them as product gtin. Wording differs, so it is the drift FLAG -- attached."""
    perfumania = catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain",
                                      [_variant("Default Title", SHALIMAR)]))
    incoming = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", SHALIMAR_FAMILY)
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == perfumania["content_key"]
    assert out["group"] == make_singleton_product_group_id(perfumania["content_key"])
    p = _last(catalog)
    assert (p["action"], p["matcher"]) == (ii.ACTION_FLAG, "variant_barcode_match_brand_title_drift")
    assert p["evidence"]["matched_product_key"] == perfumania["product_key"]
    assert p["evidence"]["matched_barcodes"] == ["03346470113541"]
    assert [r["matcher"] for r in catalog.reviews] == ["variant_barcode_match_brand_title_drift"]


@pytest.mark.asyncio
async def test_same_wording_on_another_seller_is_a_clean_variant_barcode_attach(catalog):
    """A second seller's row of the same family wording: ATTACH, no review."""
    first = catalog.add(plan_for("perfumania.com", "Shalimar Eau de Parfum", "Guerlain",
                                 [_variant("1.6 oz", SHALIMAR_16), _variant("3.0 oz", SHALIMAR_30)]))
    # Shown by wording alone too, but the barcode tier runs first and must carry it.
    incoming = plan_for("beautyencounter.com", "Shalimar Eau de Parfum", "Guerlain", SHALIMAR_FAMILY)
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == first["content_key"]
    assert (_last(catalog)["action"], _last(catalog)["matcher"]) == (ii.ACTION_ATTACH, "variant_barcode_match")
    assert _last(catalog)["evidence"]["match_source"] == "sku_barcode"
    assert catalog.reviews == []


@pytest.mark.asyncio
async def test_a_product_gtin_finds_another_sellers_sku_barcode(catalog):
    """The other direction: the new listing is single-variant (product gtin set), the existing family
    keeps that barcode on a SKU only -- _rows_by_gtin cannot see it, the widened lookup does."""
    family = catalog.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                                  SHALIMAR_FAMILY))
    incoming = plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)])
    assert incoming["pdps"][0]["gtin"] == "03346470113541"
    assert await catalog.rows_by_gtin("03346470113541", None) == []
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == family["content_key"]
    assert _last(catalog)["matcher"] == "variant_barcode_match_brand_title_drift"


@pytest.mark.asyncio
async def test_a_product_gtin_match_is_unchanged_and_outranks_a_variant_barcode(catalog):
    """Today's Tier-0a (product gtin = product gtin) keeps its outcome even when a variant barcode
    of the same listing points at a different family."""
    same = catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain",
                                [_variant("Default Title", SHALIMAR)]))
    catalog.add(plan_for("microperfumes.com", "Mon Guerlain EDP - Retail Bottle", "Guerlain",
                         [_variant("Default Title", gtin13("334647013140"))]))
    incoming = plan_for("fragrancenet.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)])
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == same["content_key"]
    assert (_last(catalog)["action"], _last(catalog)["matcher"]) == (ii.ACTION_ATTACH, "gtin_match")


@pytest.mark.asyncio
async def test_a_genuine_cross_merchant_gtin_drift_still_attaches_and_flags(catalog):
    """ADR-011 R3, unchanged: same GTIN at another seller under different wording -> attach + FLAG."""
    perfumania = catalog.add(plan_for("perfumania.com", "Mon Guerlain Perfume", "Guerlain",
                                      [_variant("Default Title", gtin13("334647013140"))]))
    incoming = plan_for("microperfumes.com", "Mon Guerlain EDP - Retail Bottle", "Guerlain",
                        [_variant("Default Title", gtin13("334647013140"))])
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == perfumania["content_key"]
    assert (_last(catalog)["action"], _last(catalog)["matcher"]) == (ii.ACTION_FLAG, "gtin_match_brand_title_drift")
    assert _last(catalog)["evidence"]["conflict_product_key"] == perfumania["product_key"]


# --- the reuse guard (the OPI hazard) -------------------------------------------------------------


def _opi(title: str) -> Dict[str, Any]:
    return plan_for("universalnailsupplies.com", title, "OPI", [_variant("Default Title", OPI_REUSED)])


@pytest.mark.asyncio
async def test_a_shade_reusing_its_stores_barcode_flags_and_keeps_its_own_family(catalog):
    """The drift branch's same-merchant case: one shade stored, a DIFFERENT shade at the same store
    with the same barcode. It used to attach (and merge the shades); now FLAG, own key, no attach."""
    first = catalog.add(_opi("OPI GelColor - It's A Girl #H39"))
    incoming = _opi("OPI GelColor - Barefoot In Barcelona #E41")
    out = await crawl(incoming)
    own = make_content_key("OPI", "OPI GelColor - Barefoot In Barcelona #E41")
    assert out["pdp"]["content_key"] == own != first["content_key"]
    assert out["group"] == make_singleton_product_group_id(own)
    p = _last(catalog)
    assert (p["action"], p["matcher"]) == (ii.ACTION_FLAG, "gtin_reused_within_seller")
    assert p["evidence"]["barcode_match"]["untrusted_barcodes"] == {"00619828139641": "titles_differ_within_seller"}
    assert [r["matcher"] for r in catalog.reviews] == ["gtin_reused_within_seller"]
    assert catalog.reviews[0]["detail"]["conflict_product_key"] == first["product_key"]


@pytest.mark.asyncio
async def test_the_census_merge_state_does_not_absorb_another_shade(catalog):
    """Prod today: two shades already merged onto one key (ck_772bcc30...). A third shade at the
    same store, and the same barcode at another seller, both stay out of that key."""
    girl = catalog.add(_opi("OPI GelColor - It's A Girl #H39"))
    catalog.add(_opi("OPI GelColor - Dulce De Leche #A15"), content_key=girl["content_key"])
    out = await crawl(_opi("OPI GelColor - Tickle My France-y #F16"))
    assert out["pdp"]["content_key"] == make_content_key("OPI", "OPI GelColor - Tickle My France-y #F16")
    assert _last(catalog)["matcher"] == "gtin_reused_within_seller"
    ulta = plan_for("ulta.com", "GelColor Dulce De Leche", "OPI", [_variant("Default Title", OPI_REUSED)])
    out = await crawl(ulta)
    assert out["pdp"]["content_key"] == make_content_key("OPI", "GelColor Dulce De Leche")
    assert _last(catalog)["action"] == ii.ACTION_FLAG


@pytest.mark.asyncio
async def test_a_listing_reusing_one_barcode_across_its_own_shades_is_not_matched_on_it(catalog):
    """The incoming side: a family whose store puts one barcode on two shades. Another seller's row
    carries that barcode -- not attached, FLAGged; its other, per-shade barcode still matches."""
    other = catalog.add(plan_for("ulta.com", "Infinite Shine - Big Apple Red", "OPI",
                                 [_variant("Default Title", OPI_REUSED)]))
    incoming = plan_for("universalnailsupplies.com", "Infinite Shine Lacquer", "OPI",
                        [_variant("Big Apple Red", OPI_REUSED), _variant("Malaga Wine", OPI_REUSED)])
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == make_content_key("OPI", "Infinite Shine Lacquer") != other["content_key"]
    p = _last(catalog)
    assert (p["action"], p["matcher"]) == (ii.ACTION_FLAG, "gtin_reused_within_seller")
    assert p["evidence"]["barcode_match"]["untrusted_barcodes"] == {
        "00619828139641": "variant_titles_differ_within_listing"}


@pytest.mark.asyncio
async def test_a_listing_reusing_a_barcode_nobody_else_carries_mints_without_a_review(catalog):
    incoming = plan_for("universalnailsupplies.com", "Infinite Shine Lacquer", "OPI",
                        [_variant("Big Apple Red", OPI_REUSED), _variant("Malaga Wine", OPI_REUSED)])
    await crawl(incoming)
    assert _last(catalog)["action"] == ii.ACTION_MINT
    assert catalog.reviews == []


@pytest.mark.asyncio
async def test_an_open_review_is_not_enqueued_again(catalog):
    catalog.add(_opi("OPI GelColor - It's A Girl #H39"))
    await crawl(_opi("OPI GelColor - Barefoot In Barcelona #E41"))
    await crawl(_opi("OPI GelColor - Barefoot In Barcelona #E41"))
    assert [r["matcher"] for r in catalog.reviews] == ["gtin_reused_within_seller"]
    assert [p["matcher"] for p in catalog.provenance[-2:]] == ["gtin_reused_within_seller"] * 2


# --- what the widened tier refuses to do ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["0", "00000000000000", "0000000000000", "3346470113542", "not-a-barcode", "7"])
async def test_an_invalid_or_all_zero_variant_barcode_is_never_looked_up(catalog, bad):
    catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", bad)]))
    incoming = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                        [_variant("1.0 oz", bad), _variant("1.6 oz", bad)])
    out = await crawl(incoming)
    assert catalog.barcode_calls == []
    assert out["pdp"]["content_key"] == make_content_key("Guerlain", "Shalimar by Guerlain for Women")
    assert _last(catalog)["action"] == ii.ACTION_MINT


@pytest.mark.asyncio
async def test_two_families_behind_one_listings_barcodes_flag_instead_of_guessing(catalog):
    """Seller B lists per size what A lists as one family: A's barcodes hit two of B's products."""
    catalog.add(plan_for("perfumania.com", "Shalimar EDP 1 oz", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    catalog.add(plan_for("perfumania.com", "Shalimar EDP 1.6 oz", "Guerlain",
                         [_variant("Default Title", SHALIMAR_16)]))
    incoming = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", SHALIMAR_FAMILY)
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] == make_content_key("Guerlain", "Shalimar by Guerlain for Women")
    p = _last(catalog)
    assert (p["action"], p["matcher"]) == (ii.ACTION_FLAG, "variant_barcode_ambiguous_family")
    assert len(catalog.reviews[0]["detail"]["candidate_content_keys"]) == 2


@pytest.mark.asyncio
async def test_a_stored_listing_is_left_to_the_backfill(catalog):
    """A re-crawl of a listing stored before F2: its own row is among the hits. The crawl never
    moves it (its group would be refused); it resolves to its own key as before."""
    perfumania = catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain",
                                      [_variant("Default Title", SHALIMAR)]))
    stored = catalog.add(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                                  SHALIMAR_FAMILY))
    recrawl = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", SHALIMAR_FAMILY)
    assert recrawl["pdps"][0]["product_key"] == stored["product_key"]
    out = await crawl(recrawl)
    assert out["pdp"]["content_key"] == stored["content_key"] != perfumania["content_key"]
    p = _last(catalog)
    assert (p["action"], p["matcher"]) == (ii.ACTION_ATTACH, "content_key")
    assert p["evidence"]["barcode_match"]["variant_barcode_match"] == "existing_listing_left_to_backfill"
    assert catalog.reviews == []


@pytest.mark.asyncio
async def test_a_grouped_listing_whose_row_lacks_the_barcodes_is_left_too(catalog):
    perfumania = catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain",
                                      [_variant("Default Title", SHALIMAR)]))
    incoming = plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain", SHALIMAR_FAMILY)
    pdp = incoming["pdps"][0]
    catalog.groups[(pdp["merchant_id"], pdp["platform"], pdp["source_product_id"])] = "pg_existing"
    out = await crawl(incoming)
    assert out["pdp"]["content_key"] != perfumania["content_key"]
    assert _last(catalog)["evidence"]["barcode_match"]["variant_barcode_match"] == "existing_listing_left_to_backfill"


@pytest.mark.asyncio
async def test_a_cut_short_lookup_is_not_matched_on(catalog, monkeypatch):
    monkeypatch.setattr(ii, "BARCODE_LOOKUP_LIMIT", 1)
    catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    out = await crawl(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                               SHALIMAR_FAMILY))
    assert out["pdp"]["content_key"] == make_content_key("Guerlain", "Shalimar by Guerlain for Women")
    assert _last(catalog)["evidence"]["barcode_match"]["widened_lookup"] == "truncated"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", None])
async def test_the_flag_off_or_unset_is_product_gtin_only_matching(catalog, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ii.VARIANT_BARCODE_MATCH_ENV, raising=False)  # the prod default until 262 lands
    else:
        monkeypatch.setenv(ii.VARIANT_BARCODE_MATCH_ENV, value)
    catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain", [_variant("Default Title", SHALIMAR)]))
    out = await crawl(plan_for("beautyencounter.com", "Shalimar by Guerlain for Women", "Guerlain",
                               SHALIMAR_FAMILY))
    assert catalog.barcode_calls == []
    assert _last(catalog)["action"] == ii.ACTION_MINT
    assert out["pdp"]["content_key"] == make_content_key("Guerlain", "Shalimar by Guerlain for Women")


@pytest.mark.asyncio
async def test_a_failed_barcode_lookup_keeps_todays_product_gtin_match(catalog, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(ii, "_rows_by_barcodes", boom)
    same = catalog.add(plan_for("perfumania.com", "Shalimar Perfume", "Guerlain",
                                [_variant("Default Title", SHALIMAR)]))
    out = await crawl(plan_for("fragrancenet.com", "Shalimar Perfume", "Guerlain",
                               [_variant("Default Title", SHALIMAR)]))
    assert out["pdp"]["content_key"] == same["content_key"]
    assert _last(catalog)["matcher"] == "gtin_match"
    assert _last(catalog)["evidence"]["barcode_match"] == {"widened_lookup": "error"}


# --- pure helpers ----------------------------------------------------------------------------------


def test_gtin_spellings_are_the_zero_stripped_forms_only():
    assert ii.gtin_spellings("03346470113541") == ["03346470113541", "3346470113541"]
    assert ii.gtin_spellings("00619828139641") == ["00619828139641", "0619828139641", "619828139641"]
    assert ii.gtin_spellings("00000096385074") == ["00000096385074", "0000096385074", "000096385074", "96385074"]
    assert ii.gtin_spellings("13346470113548") == ["13346470113548"]


def test_seller_key_splits_the_legacy_bucket_by_domain():
    assert ii.seller_key({"merchant_id": "merch_obs_1", "source_domain": "a.com"}) == "merch_obs_1"
    assert ii.seller_key({"merchant_id": "external_seed", "source_domain": "www.A.com"}) == "external_seed|a.com"
    assert ii.seller_key({"merchant_id": "external_seed", "product_key": "p1"}) == "external_seed|pk:p1"


def test_the_mirror_passes_every_barcoded_seed_variant():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "mirror_script_f2",
        Path(__file__).resolve().parents[2] / "scripts" / "mirror_external_seeds_to_catalog_products.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seed = {"title": "Shalimar", "variants": [
        {"title": "1.0 oz", "barcode": SHALIMAR}, {"title": "1.6 oz", "barcode": ""},
        {"title": "3.0 oz", "gtin": SHALIMAR_30}, "junk"]}
    assert mod._seed_variants(seed) == [{"barcode": SHALIMAR, "title": "1.0 oz"},
                                        {"barcode": SHALIMAR_30, "title": "3.0 oz"}]
    assert mod._seed_variants(None) == [] and mod._seed_variants({"variants": "x"}) == []
