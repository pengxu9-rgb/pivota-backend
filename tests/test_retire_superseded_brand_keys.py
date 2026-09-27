"""The cohort this retire step acts on must be exactly the set #2173 re-keyed.

Pure: the storefront fetch is monkeypatched, no DB and no network. The SQL paths are
exercised in the staged run against prod, not here — what is testable in isolation, and
what actually decides which live rows get tombstoned, is the cohort.
"""

import pytest

import services.curated_brand_feed as cbf
from scripts.retire_superseded_brand_keys import build_cohort
from services.catalog_enrichment_agent.ingestion import derive_product_key


def _product(vendor, title, handle):
    return {
        "title": title, "handle": handle, "vendor": vendor, "product_type": "Cleanser",
        "images": [{"src": "https://cdn.x/img.jpg"}],
        "variants": [{"id": 111, "price": "12.00", "available": True}],
    }


@pytest.fixture
def misshaus(monkeypatch):
    """The real vendor mix measured on misshaus.com 2026-09-11, in miniature."""
    feed = [
        _product("MISSHA", "Time Revolution Essence", "tr-essence"),
        _product("MISSHA US", "Artemisia Ampoule", "artemisia"),
        _product("APIEU", "A'pieu Honey & Milk Lip Oil", "lip-oil"),
        _product("Apieu", "A pieu Juicy Pang", "juicy-pang"),
        _product("CHOGONGJIN", "Chogongjin Cream", "cho-cream"),
    ]

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return feed

    async def fake_locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", fake_locale)
    return feed


@pytest.mark.asyncio
async def test_cohort_is_exactly_the_rows_whose_brand_moved(misshaus):
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    titles = sorted(c["title"] for c in cohort)
    # The two MISSHA-vendor rows keep brand "Missha" and therefore keep their key.
    assert titles == ["A pieu Juicy Pang", "A'pieu Honey & Milk Lip Oil", "Chogongjin Cream"]
    assert all(c["stale_key"] != c["new_key"] for c in cohort)


@pytest.mark.asyncio
async def test_the_stale_key_is_the_one_the_old_code_would_have_written(misshaus):
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    lip = next(c for c in cohort if c["title"] == "A'pieu Honey & Milk Lip Oil")
    # Exactly what `brand_override or vendor` produced — this is the row in prod today.
    assert lip["stale_key"] == derive_product_key("Missha", "A'pieu Honey & Milk Lip Oil")
    assert lip["new_key"] == derive_product_key("APIEU", "A'pieu Honey & Milk Lip Oil")
    assert lip["brand"] == "APIEU"


@pytest.mark.asyncio
async def test_a_row_that_kept_its_brand_is_never_in_the_cohort(misshaus):
    """The retire step must not touch the 98 rows the fix left alone."""
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    stale = {c["stale_key"] for c in cohort}
    for title in ("Time Revolution Essence", "Artemisia Ampoule"):
        assert derive_product_key("Missha", title) not in stale


@pytest.mark.asyncio
async def test_the_spelling_fold_does_not_split_one_brand_across_two_keys(misshaus):
    """`APIEU` and `Apieu` fold to one spelling, and the key normalises case anyway —
    so both A'pieu rows retire onto keys under the same brand prefix."""
    cohort = await build_cohort("misshaus.com", "Missha", "beauty/skincare")
    brands = {c["brand"] for c in cohort if "pieu" in c["title"].lower()}
    assert brands == {"APIEU"}


@pytest.mark.asyncio
async def test_an_empty_cohort_when_nothing_was_re_keyed(monkeypatch):
    """A single-brand storefront: the fix changes nothing, so there is nothing to retire."""
    feed = [_product("COSRX", "Snail Mucin Gel Cleanser", "snail")]

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return feed

    async def fake_locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", fake_locale)
    assert await build_cohort("cosrx.com", "COSRX", "beauty/skincare") == []


def test_a_jsonb_value_is_bound_as_text_however_the_driver_returned_it():
    """`CAST(:metadata AS jsonb)` wants TEXT. asyncpg hands a jsonb column back as a
    dict and the manifest round-trips it as one; binding that to a text cast fails in
    `revert` — the one path that must not fail."""
    from scripts.retire_superseded_brand_keys import _as_json_text
    assert _as_json_text(None) is None
    assert _as_json_text('{"a": 1}') == '{"a": 1}'
    assert _as_json_text({"run_id": "x", "n": 2}) == '{"run_id": "x", "n": 2}'
    assert _as_json_text([1, 2]) == "[1, 2]"


@pytest.fixture
def tarte(monkeypatch):
    """tartecosmetics.com, 2026-09-27: one crawl wrote 176 rows as "Tarte" and 73 as "Tarte Cosmetics"
    (vendor spellings), no title in common. Re-run as "Tarte", every row resolves to "Tarte"."""
    feed = [
        _product("tarte", "Shape Tape Concealer", "shape-tape"),
        _product("Tarte Cosmetics", "Maracuja Juicy Lip Balm", "juicy-lip"),
        _product("Tarte Cosmetics", "Amazonian Clay Blush", "clay-blush"),
    ]

    async def fake_fetch(domain, *, max_products=500, timeout_s=15.0):
        return feed

    async def fake_locale(domain, **kw):
        return {"currency": "USD"}

    monkeypatch.setattr(cbf, "fetch_shopify_products", fake_fetch)
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", fake_locale)
    return feed


@pytest.mark.asyncio
async def test_without_the_stale_spelling_a_respelled_store_retires_nothing(tarte):
    """The Sand & Sky case: re-run brand == record brand, so derive(brand) == derive(record brand)."""
    assert await build_cohort("tartecosmetics.com", "Tarte", "beauty") == []


@pytest.mark.asyncio
async def test_the_stale_spelling_maps_each_old_key_to_its_new_one(tarte):
    cohort = await build_cohort("tartecosmetics.com", "Tarte", "beauty", stale_brand="Tarte Cosmetics")
    assert {c["title"] for c in cohort} == {"Shape Tape Concealer", "Maracuja Juicy Lip Balm", "Amazonian Clay Blush"}
    for c in cohort:
        assert c["brand"] == "Tarte"
        assert c["stale_key"] == derive_product_key("Tarte Cosmetics", c["title"])
        assert c["new_key"] == derive_product_key("Tarte", c["title"])
    # Only the 73 rows actually written as "Tarte Cosmetics" exist under a stale key in prod; `plan` retires
    # present keys only, so "Shape Tape Concealer" (stored as "Tarte") is never touched.


def test_the_cli_takes_the_stale_spelling():
    import scripts.retire_superseded_brand_keys as tool
    captured = {}

    async def fake_run(a):
        captured.update(vars(a))
        return 0

    orig = tool.run
    tool.run = fake_run
    try:
        tool.main(["--domain", "stilacosmetics.com", "--brand", "Stila", "--stale-brand", "Stila Cosmetics",
                   "--category", "beauty"])
    finally:
        tool.run = orig
    assert captured["stale_brand"] == "Stila Cosmetics" and captured["brand"] == "Stila"


# --- review of #2397: retire only what the re-run actually rewrote, and only this store's rows ---------

from scripts.retire_superseded_brand_keys import select_retirable  # noqa: E402

_C = [{"stale_key": f"old{i}", "new_key": f"new{i}", "brand": "Stila", "title": f"T{i}"} for i in range(5)]


def _rows(**over):
    base = {f"old{i}": {"product_key": f"old{i}", "source_domain": "stilacosmetics.com", "suppression_reason": None}
            for i in range(5)}
    for k, v in over.items():
        base[k] = {**base[k], **v}
    return base


def test_a_stale_key_is_retired_only_once_its_new_key_is_live():
    """Measured: 72 of stilacosmetics.com's 125 records were unresolved, and the drain drops those; retiring
    their stale keys first would have hidden the products."""
    out = select_retirable(_C, _rows(), new_live={"new0", "new1"}, domain="stilacosmetics.com")
    assert [c["stale_key"] for c in out["live"]] == ["old0", "old1"]
    assert [c["stale_key"] for c in out["waiting_for_new_key"]] == ["old2", "old3", "old4"]


def test_another_sources_row_under_the_same_key_is_never_retired():
    rows = _rows(old0={"source_domain": "someretailer.com"}, old1={"source_domain": None})
    out = select_retirable(_C, rows, new_live={f"new{i}" for i in range(5)}, domain="www.stilacosmetics.com")
    assert {c["stale_key"] for c in out["foreign"]} == {"old0", "old1"}
    assert {c["stale_key"] for c in out["live"]} == {"old2", "old3", "old4"}   # www. is the same store


def test_absent_and_already_suppressed_keys_are_left_alone():
    rows = _rows(old0={"suppression_reason": "x"})
    del rows["old1"]
    out = select_retirable(_C, rows, new_live={f"new{i}" for i in range(5)}, domain="stilacosmetics.com")
    assert {c["stale_key"] for c in out["live"]} == {"old2", "old3", "old4"}
    assert {c["stale_key"] for c in out["present"]} == {"old0", "old2", "old3", "old4"}


def test_the_old_order_is_an_explicit_choice():
    out = select_retirable(_C, _rows(), new_live=set(), domain="stilacosmetics.com", before_rewrite=True)
    assert len(out["live"]) == 5 and out["waiting_for_new_key"] == []


def test_the_cli_plumbs_both_flags_into_the_plan(monkeypatch):
    import scripts.retire_superseded_brand_keys as tool
    seen = {}

    async def fake_plan(domain, brand, category, stale_brand=None, *, before_rewrite=False):
        seen.update(domain=domain, brand=brand, stale_brand=stale_brand, before_rewrite=before_rewrite)
        return {"domain": domain, "brand_override": brand, "stale_brand": stale_brand, "cohort": [], "present": [],
                "live": [], "already_new": [], "seeds": [], "active_seeds": [], "offers": []}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(tool, "plan", fake_plan)
    monkeypatch.setattr(tool.database, "connect", noop)
    monkeypatch.setattr(tool.database, "disconnect", noop)
    assert tool.main(["--domain", "stilacosmetics.com", "--brand", "Stila", "--stale-brand", "Stila Cosmetics",
                      "--category", "beauty"]) == 0
    assert seen == {"domain": "stilacosmetics.com", "brand": "Stila", "stale_brand": "Stila Cosmetics",
                    "before_rewrite": False}


@pytest.mark.asyncio
async def test_plan_passes_the_stale_spelling_to_the_cohort(monkeypatch):
    import scripts.retire_superseded_brand_keys as tool
    seen = {}

    async def fake_cohort(domain, brand, category_path, stale_brand=None):
        seen["stale_brand"] = stale_brand
        return []

    async def fetch_all(sql, values=None):
        return []

    async def cascade(keys, apply=False):
        return []

    monkeypatch.setattr(tool, "build_cohort", fake_cohort)
    monkeypatch.setattr(tool.database, "fetch_all", fetch_all)
    monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", cascade)
    p = await tool.plan("tartecosmetics.com", "Tarte", "beauty", "Tarte Cosmetics")
    assert seen["stale_brand"] == "Tarte Cosmetics" and p["stale_brand"] == "Tarte Cosmetics"


def test_the_cli_plumbs_the_old_order_flag(monkeypatch):
    import scripts.retire_superseded_brand_keys as tool
    seen = {}

    async def fake_plan(domain, brand, category, stale_brand=None, *, before_rewrite=False):
        seen["before_rewrite"] = before_rewrite
        return {"domain": domain, "brand_override": brand, "stale_brand": stale_brand, "cohort": [], "present": [],
                "live": [], "already_new": [], "seeds": [], "active_seeds": [], "offers": []}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(tool, "plan", fake_plan)
    monkeypatch.setattr(tool.database, "connect", noop)
    monkeypatch.setattr(tool.database, "disconnect", noop)
    tool.main(["--domain", "x.com", "--brand", "X", "--before-rewrite"])
    assert seen["before_rewrite"] is True
