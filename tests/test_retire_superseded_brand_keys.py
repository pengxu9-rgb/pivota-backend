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
    out = select_retirable(_C, _rows(), new_live={"new0", "new1"}, domain="stilacosmetics.com", searchable=set(), serving=set())
    assert [c["stale_key"] for c in out["live"]] == ["old0", "old1"]
    assert [c["stale_key"] for c in out["waiting_for_new_key"]] == ["old2", "old3", "old4"]


def test_another_sources_row_under_the_same_key_is_never_retired():
    rows = _rows(old0={"source_domain": "someretailer.com"}, old1={"source_domain": None})
    out = select_retirable(_C, rows, new_live={f"new{i}" for i in range(5)}, domain="www.stilacosmetics.com",
                           searchable=set(), serving=set())
    assert {c["stale_key"] for c in out["foreign"]} == {"old0", "old1"}
    assert {c["stale_key"] for c in out["live"]} == {"old2", "old3", "old4"}   # www. is the same store


def test_absent_and_already_suppressed_keys_are_left_alone():
    rows = _rows(old0={"suppression_reason": "x"})
    del rows["old1"]
    out = select_retirable(_C, rows, new_live={f"new{i}" for i in range(5)}, domain="stilacosmetics.com",
                           searchable=set(), serving=set())
    assert {c["stale_key"] for c in out["live"]} == {"old2", "old3", "old4"}
    assert {c["stale_key"] for c in out["present"]} == {"old0", "old2", "old3", "old4"}


def test_the_old_order_is_an_explicit_choice():
    out = select_retirable(_C, _rows(), new_live=set(), domain="stilacosmetics.com", searchable=set(), serving=set(),
                           before_rewrite=True)
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


def _plan_env(monkeypatch, *, new_rows, serving_cks=(), searchable_keys=()):
    """plan() against a fake DB: two stale rows on the store, new keys as given; seeds/offers recorded.
    Stale row oldN carries content_key ck_oldN; `serving_cks` are the serving_eligible content_keys."""
    import scripts.retire_superseded_brand_keys as tool
    cohort = [{"stale_key": "old0", "new_key": "new0", "brand": "Stila", "title": "A"},
              {"stale_key": "old1", "new_key": "new1", "brand": "Stila", "title": "B"}]
    stale_rows = [{"product_key": k, "source_domain": "stilacosmetics.com", "suppression_reason": None,
                   "suppressed_at": None, "suppression_metadata": None, "merchant_id": "m", "brand": "Stila Cosmetics",
                   "title": t, "content_key": f"ck_{k}"} for k, t in (("old0", "A"), ("old1", "B"))]
    asked = {"seeds": None, "offers": None, "serving": []}

    async def fake_cohort(*a, **k):
        return cohort

    async def fetch_all(sql, values=None):
        keys = (values or {}).get("keys") or []
        if "external_product_seeds" in sql:
            asked["seeds"] = list(keys)
            return [{"id": f"seed_{k}", "status": "active"} for k in keys]
        if "catalog_row_trust" in sql:
            asked.setdefault("searchable", []).append(list(keys))
            return [{"product_key": k} for k in keys if k in searchable_keys]
        if "index_pipeline_state" in sql:
            asked["serving"].append(list(keys))
            # Every key has a state row; the flag says which serve. Unserved flags alternate FALSE / NULL.
            return [{"content_key": ck, "serving_eligible": True if ck in serving_cks else (False if i % 2 else None)}
                    for i, ck in enumerate(keys)]
        if keys and keys[0].startswith("old"):
            return [r for r in stale_rows if r["product_key"] in keys]
        return [r for r in new_rows if r["product_key"] in keys]

    async def cascade(keys, apply=False):
        asked["offers"] = list(keys)
        return [f"off_{k}" for k in keys]

    monkeypatch.setattr(tool, "build_cohort", fake_cohort)
    monkeypatch.setattr(tool.database, "fetch_all", fetch_all)
    monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", cascade)
    return tool, asked


@pytest.mark.asyncio
async def test_plan_retires_only_rewritten_keys_and_scopes_seeds_and_offers_to_them(monkeypatch):
    new_rows = [
        {"product_key": "new0", "source_domain": "www.stilacosmetics.com", "suppression_reason": None},  # rewritten
        {"product_key": "new1", "source_domain": "stilacosmetics.com", "suppression_reason": "x"},        # suppressed
    ]
    tool, asked = _plan_env(monkeypatch, new_rows=new_rows)
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert [c["stale_key"] for c in p["live"]] == ["old0"]
    assert [c["stale_key"] for c in p["waiting_for_new_key"]] == ["old1"]
    assert asked["seeds"] == ["old0"] and asked["offers"] == ["old0"]
    assert [s["id"] for s in p["active_seeds"]] == ["seed_old0"]


@pytest.mark.asyncio
async def test_another_sources_new_key_does_not_count_as_the_rewrite(monkeypatch):
    new_rows = [{"product_key": "new0", "source_domain": "someretailer.com", "suppression_reason": None}]
    tool, asked = _plan_env(monkeypatch, new_rows=new_rows)
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert p["live"] == [] and len(p["waiting_for_new_key"]) == 2 and p["seeds"] == [] and p["offers"] == []


# --- a live new key is not a served one: never retire a served row onto a blocked replacement -----------
# 2026-09-28 this was hand-checked per store (old rows joined to new by host + lower(title), serving_eligible
# compared). The tool now refuses on its own.

_ALL_NEW = {f"new{i}" for i in range(5)}


def _keys(out, bucket):
    return [c["stale_key"] for c in out[bucket]]


def test_a_served_old_row_is_kept_while_its_new_row_is_not_served():
    out = select_retirable(_C[:1], _rows(), new_live=_ALL_NEW, domain="stilacosmetics.com", searchable=set(), serving={"old0"})
    assert _keys(out, "live") == []
    assert _keys(out, "new_not_serving") == ["old0"]


def test_an_unserved_old_row_is_retired_even_though_its_new_row_is_not_served():
    """Nothing is on the storefront to lose."""
    out = select_retirable(_C[:1], _rows(), new_live=_ALL_NEW, domain="stilacosmetics.com", searchable=set(), serving=set())
    assert _keys(out, "live") == ["old0"] and out["new_not_serving"] == []


def test_a_served_old_row_is_retired_once_its_new_row_serves():
    out = select_retirable(_C[:1], _rows(), new_live=_ALL_NEW, domain="stilacosmetics.com",
                           searchable=set(), serving={"old0", "new0"})
    assert _keys(out, "live") == ["old0"] and out["new_not_serving"] == []


def test_the_serving_check_splits_a_mixed_store_per_key():
    """old0 served/new0 blocked (kept), old1 dark/new1 blocked, old2 served/new2 served, old3 waiting, old4 x."""
    rows = _rows(old4={"suppression_reason": "x"})
    out = select_retirable(_C, rows, new_live={"new0", "new1", "new2"}, domain="stilacosmetics.com",
                           searchable=set(), serving={"old0", "old2", "new2", "old3"})
    assert _keys(out, "new_not_serving") == ["old0"]
    assert _keys(out, "live") == ["old1", "old2"]
    assert _keys(out, "waiting_for_new_key") == ["old3"]
    assert _keys(out, "already_suppressed") == ["old4"]


def test_the_serving_map_is_required():
    with pytest.raises(TypeError):
        select_retirable(_C, _rows(), new_live=_ALL_NEW, domain="stilacosmetics.com")


def test_the_old_order_retires_regardless_of_serving():
    """--before-rewrite retires before the new key exists at all; the serving gap is what it opts into."""
    out = select_retirable(_C, _rows(), new_live=set(), domain="stilacosmetics.com",
                           searchable=set(), serving={f"old{i}" for i in range(5)}, before_rewrite=True)
    assert len(out["live"]) == 5 and out["new_not_serving"] == []


def _both_new_rows_live():
    return [{"product_key": f"new{i}", "source_domain": "stilacosmetics.com", "suppression_reason": None,
             "content_key": f"ck_new{i}"} for i in range(2)]


@pytest.mark.asyncio
async def test_plan_loads_serving_by_content_key_in_one_query(monkeypatch):
    """old0 + old1 both serve; only new1 serves. old0 is kept, and its seeds/offers are not touched."""
    tool, asked = _plan_env(monkeypatch, new_rows=_both_new_rows_live(),
                            serving_cks={"ck_old0", "ck_old1", "ck_new1"})
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert asked["serving"] == [["ck_new0", "ck_new1", "ck_old0", "ck_old1"]]
    assert [c["stale_key"] for c in p["new_not_serving"]] == ["old0"]
    assert [c["stale_key"] for c in p["live"]] == ["old1"]
    assert asked["seeds"] == ["old1"] and asked["offers"] == ["old1"]


@pytest.mark.asyncio
async def test_plan_asks_serving_only_for_this_stores_live_new_rows(monkeypatch):
    """A suppressed or foreign row under the new key is not the re-run's row; its content_key must not make the
    new key look served."""
    new_rows = [{"product_key": "new0", "source_domain": "someretailer.com", "suppression_reason": None,
                 "content_key": "ck_foreign"},
                {"product_key": "new1", "source_domain": "stilacosmetics.com", "suppression_reason": "x",
                 "content_key": "ck_dead"}]
    tool, asked = _plan_env(monkeypatch, new_rows=new_rows, serving_cks={"ck_foreign", "ck_dead"})
    await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert asked["serving"] == [["ck_old0", "ck_old1"]]


@pytest.mark.asyncio
async def test_the_plan_prints_the_kept_count_and_counts_suppressed_rows_not_present_minus_live(monkeypatch, capsys):
    import scripts.retire_superseded_brand_keys as tool
    # old0 served, new0 live but blocked -> kept; old1's new key not written -> waiting. Nothing is suppressed,
    # but present - live would print 2.
    new_rows = _both_new_rows_live()[:1]
    tool, _ = _plan_env(monkeypatch, new_rows=new_rows, serving_cks={"ck_old0"})
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    tool.print_plan(p)
    out = capsys.readouterr().out
    assert "NEW NOT SERVING (old served): 1  -- never retired" in out
    assert "WAITING (new key not live)  : 1" in out
    assert "already suppressed          : 0" in out


# --- the store scope is an exact host match: a subdomain or look-alike host is another source -----------
# Mutation sweep of #2424: `!=` / `==` on _host() rewritten to a substring test survived every test above.
# Hosts on both sides of a substring test: ones that CONTAIN the store's host and ones CONTAINED IN it.

_LOOKALIKES = ["shop.stilacosmetics.com", "stilacosmetics.com.evil.io", "notstilacosmetics.com",
               "cosmetics.com", "stilacosmetics.co"]


@pytest.mark.parametrize("lookalike", _LOOKALIKES)
def test_a_lookalike_hosts_stale_row_is_foreign_never_retired(lookalike):
    rows = _rows(old0={"source_domain": lookalike}, old1={"source_domain": "www.stilacosmetics.com"})
    out = select_retirable(_C[:2], rows, new_live=_ALL_NEW, domain="stilacosmetics.com", serving=set(),
                           searchable=set())
    assert _keys(out, "foreign") == ["old0"]
    assert _keys(out, "live") == ["old1"]   # www. is the same store


@pytest.mark.parametrize("lookalike", _LOOKALIKES)
@pytest.mark.asyncio
async def test_a_lookalike_hosts_new_row_does_not_count_as_the_rewrite(monkeypatch, lookalike):
    new_rows = [{"product_key": "new0", "source_domain": lookalike, "suppression_reason": None,
                 "content_key": "ck_new0"},
                {"product_key": "new1", "source_domain": "www.stilacosmetics.com", "suppression_reason": None,
                 "content_key": "ck_new1"}]
    tool, asked = _plan_env(monkeypatch, new_rows=new_rows)
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert [c["stale_key"] for c in p["waiting_for_new_key"]] == ["old0"]
    assert [c["stale_key"] for c in p["live"]] == ["old1"]   # www. is the same store
    assert asked["seeds"] == ["old1"] and asked["offers"] == ["old1"]
