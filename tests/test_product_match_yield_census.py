"""scripts/ops/product_match_yield_census.py: the read-only T0a/T0b/T0c yield census.

The grouping is pure (compute_census over already-loaded rows), so it is tested here on fake
rows with no database. The loader is tested against a fake asyncpg connection for its safety
contract: one READ ONLY transaction, SET LOCAL statement_timeout '60s', SELECTs only, and the
optional variant-title statement in a savepoint.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "product_match_yield_census.py"
_spec = importlib.util.spec_from_file_location("product_match_yield_census", _PATH)
census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census)


def _gtin(body: str) -> str:
    weighted = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - weighted % 10) % 10)


SNAIL_GTIN = _gtin("880941647000")
POLLUTED_GTIN = _gtin("880941647111")
RESTRICTED_GTIN = _gtin("21234567890")  # UPC starting 2: restricted circulation


def _p(pk, brand, title, ck, gtin=None):
    return {"product_key": pk, "brand": brand, "title": title, "gtin": gtin, "content_key": ck,
            "pivota_signature_id": f"sig_{pk}", "source_system": "test"}


PRODUCTS = [
    # COSRX essence: brand store (multi-size variants) + 3 retailers at 100ml, one 50ml.
    _p("prod::cosrx::essence", "COSRX", "Advanced Snail 96 Mucin Power Essence", "ck_ess_brand"),
    _p("ext:retailer:r1", "COSRX Official", "COSRX Advanced Snail 96 Mucin Power Essence 3.38 fl oz / 100ml",
       "ck_ess_r1", SNAIL_GTIN),
    _p("ext:retailer:r2", "Cosrx", "Advanced Snail 96 Mucin Power Essence 100ml", "ck_ess_r2", SNAIL_GTIN),
    _p("ext:retailer:r3", "COSRX", "Advanced Snail 96 Mucin Power Essence 100ml", "ck_ess_r3"),
    _p("ext:retailer:r4", "COSRX", "Advanced Snail 96 Mucin Power Essence 50ml", "ck_ess_r4"),
    # Round Lab toner: two spellings, same size -> T0c join (not T0b: titles differ).
    _p("ext:retailer:rl1", "ROUND LAB", "1025 Dokdo Toner 200ml", "ck_rl1"),
    _p("ext:retailer:rl2", "Round Lab", "Roundlab 1025 Dokdo Toner 6.76 fl oz", "ck_rl2"),
    # Dior: EDT vs EDP at the same size -> one T0c key, VETOED (two-sided concentration).
    _p("ext:retailer:d1", "Dior", "Sauvage Eau de Toilette 100ml", "ck_d1"),
    _p("ext:retailer:d2", "Christian Dior", "Sauvage Eau de Parfum Spray 3.4 oz", "ck_d2"),
    # Beauty of Joseon: SPF stated on one side only -> VETOED (one-sided).
    _p("ext:retailer:b1", "Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml", "ck_b1"),
    _p("ext:retailer:b2", "Beauty of Joseon", "Relief Sun Rice + Probiotics 50ml", "ck_b2"),
    # A GTIN carried on two sizes: polluted, never a T0a key.
    _p("ext:retailer:g1", "Anua", "Heartleaf 77% Soothing Toner 250ml", "ck_g1", POLLUTED_GTIN),
    _p("ext:retailer:g2", "Anua", "Heartleaf 77% Soothing Toner 500ml", "ck_g2", POLLUTED_GTIN),
    # A restricted-circulation GTIN shared by two unrelated listings: never a T0a key.
    _p("ext:retailer:x1", "Torriden", "DIVE-IN Serum 50ml", "ck_x1", RESTRICTED_GTIN),
    _p("ext:retailer:x2", "Torriden", "DIVE-IN Cream 100ml", "ck_x2", RESTRICTED_GTIN),
    # Already one content_key: a key group, but nothing to join.
    _p("ext:retailer:s1", "Laneige", "Water Sleeping Mask 70ml", "ck_same"),
    _p("ext:retailer:s2", "Laneige", "Water Sleeping Mask 70ml", "ck_same"),
    # SG only.
    _p("ext:retailer:sg1", "Anua", "Heartleaf Pore Control Cleansing Oil 200ml", "ck_sg1"),
    _p("ext:retailer:sg2", "ANUA", "Heartleaf Pore Control Cleansing Oil 200ml", "ck_sg2"),
    # No live offer in any market: ignored.
    _p("ext:retailer:none", "Anua", "Heartleaf Pore Control Cleansing Oil 200ml", "ck_none"),
]
_HOSTS = {
    "prod::cosrx::essence": ("US", "www.cosrx.com", True),
    "ext:retailer:r1": ("US", "www.yesstyle.com", False),
    "ext:retailer:r2": ("US", "stylevana.com", False),
    "ext:retailer:r3": ("US", "yesstyle.com", False),  # same store as r1 once www is folded
    "ext:retailer:r4": ("US", "sokoglam.com", False),
    "ext:retailer:rl1": ("US", "sokoglam.com", False),
    "ext:retailer:rl2": ("US", "roundlab.com", True),
    "ext:retailer:d1": ("US", "sephora.com", False),
    "ext:retailer:d2": ("US", "perfumania.com", False),
    "ext:retailer:b1": ("US", "stylevana.com", False),
    "ext:retailer:b2": ("US", "beautyofjoseon.com", True),
    "ext:retailer:g1": ("US", "a.com", False),
    "ext:retailer:g2": ("US", "b.com", False),
    "ext:retailer:x1": ("US", "a.com", False),
    "ext:retailer:x2": ("US", "b.com", False),
    "ext:retailer:s1": ("US", "a.com", False),
    "ext:retailer:s2": ("US", "a.com", False),
    "ext:retailer:sg1": ("SG", "cocomo.sg", False),
    "ext:retailer:sg2": ("SG", "hermo.sg", False),
}
HOSTS = [{"product_key": pk, "market": m, "host": h, "brand_direct": bd} for pk, (m, h, bd) in _HOSTS.items()]
SKU_TITLES = {"prod::cosrx::essence": ["100ml", "50ml"], "ext:retailer:rl2": ["Default Title"]}


def _lines(**kw):
    return census.compute_census(PRODUCTS, HOSTS, SKU_TITLES, **kw)


def _get(lines, section, market=None, tier=None):
    hits = [l for l in lines if l["section"] == section and (market is None or l.get("market") == market)
            and (tier is None or l.get("tier") == tier)]
    assert len(hits) == 1, (section, market, tier, hits)
    return hits[0]


def test_every_line_is_json_and_tagged():
    lines = _lines()
    for line in lines:
        assert line["census"] == "product_match_yield" and line["section"]
        assert json.loads(json.dumps(line, default=str)) is not None
    assert [l["section"] for l in lines][0] == "loaded"


def test_inputs_count_market_membership_hosts_and_gtin_quality():
    lines = _lines()
    us = _get(lines, "inputs", "US")
    assert us["products"] == 17  # every product with a live US offer; SG-only and offer-less excluded
    assert us["products_by_host_count"]["1"] == 17
    assert us["products_with_brand_direct_offer"] == 3
    assert us["gtins_polluted"] == 1
    assert us["gtin_restricted_by_reason"] == {"restricted_circulation": 2}
    assert us["products_with_multi_size_variants"] == 1  # the brand store's 100ml + 50ml variants
    assert us["content_keys_multi_host_today"] == 0
    assert _get(lines, "inputs", "SG")["products"] == 2


def test_t0a_needs_a_clean_gtin_and_equal_size():
    t0a = _get(_lines(), "tier", "US", "T0a")
    # Only the snail GTIN qualifies: r1 + r2, same brand, same 100ml. The polluted and restricted
    # GTINs make no key at all.
    assert t0a["counts"]["split_key_groups"] == 1
    assert t0a["counts"]["joinable_groups"] == 1
    assert t0a["counts"]["joinable_multi_host_groups"] == 1
    assert t0a["joined_products_distinct"] == 2


def test_t0b_is_exact_title_and_counts_already_joined_groups_apart():
    t0b = _get(_lines(), "tier", "US", "T0b")
    c = t0b["counts"]
    # r2 + r3 (same title, brand spelling folds) join; s1 + s2 already share a content_key.
    assert c["key_groups_already_one_ck"] == 1
    assert c["joinable_groups"] == 1
    assert c["joinable_multi_host_groups"] == 1  # stylevana + yesstyle


def test_t0c_joins_vetoes_and_defers():
    lines = _lines()
    t0c = _get(lines, "tier", "US", "T0c")
    c = t0c["counts"]
    # Snail 100ml (r1, r2, r3) and Round Lab join; Dior and BoJ are vetoed; Laneige is one ck.
    assert c["joinable_groups"] == 2
    assert c["vetoed_key_groups"] == 2
    assert t0c["veto_two_sided_by_token"] == {"edp": 1, "edt": 1}
    assert t0c["veto_one_sided_by_token"] == {"pa++++": 1, "spf50+": 1}
    assert c["joinable_with_brand_direct"] == 1  # roundlab.com
    # r3 shares yesstyle.com with r1 once www. is folded: 2 hosts, not 3.
    samples = _get(lines, "tier_samples", "US", "T0c")["joinable"]
    snail = next(g for g in samples if any("Snail" in m["title"] for m in g["members"]))
    assert {h for m in snail["members"] for h in m["hosts"]} == {"yesstyle.com", "stylevana.com"}


def test_deferred_when_a_member_has_more_than_one_size():
    products = PRODUCTS + [
        _p("ext:retailer:brandstore100", "COSRX", "Advanced Snail 96 Mucin Power Essence 100ml", "ck_ess_brand"),
    ]
    hosts = HOSTS + [{"product_key": "ext:retailer:brandstore100", "market": "US", "host": "cosrx.com",
                      "brand_direct": True}]
    # ck_ess_brand now holds a 100ml listing and a product whose variants are 100ml + 50ml.
    lines = census.compute_census(products, hosts, SKU_TITLES, ["US"])
    t0c = _get(lines, "tier", "US", "T0c")["counts"]
    assert t0c["deferred_multi_size_groups"] == 1
    assert t0c["joinable_groups"] == 1  # Round Lab only


def test_any_tier_union_and_top_brands():
    lines = _lines()
    any_tier = _get(lines, "any_tier", "US")
    assert any_tier["groups"] == 2  # snail (T0a + T0b + T0c overlap into one) and round lab
    assert any_tier["multi_host_groups"] == 2
    brands = _get(lines, "top_brands", "US")["brands"]
    assert [b["brand"] for b in brands] == ["cosrx", "roundlab"]
    assert brands[0]["T0c_multi_host_groups"] == 1


def test_sg_is_computed_separately():
    lines = _lines()
    sg = _get(lines, "tier", "SG", "T0b")["counts"]
    assert sg["joinable_groups"] == 1 and sg["joinable_multi_host_groups"] == 1


def test_samples_can_be_turned_off():
    assert not [l for l in _lines(samples=0) if l["section"] == "tier_samples"]


def test_fold_host():
    assert census.fold_host("WWW.Sephora.com.") == "sephora.com"
    assert census.fold_host(None) == ""


# ---- loader safety contract -------------------------------------------------------------

class _FakeConn:
    def __init__(self, fail_skus=False):
        self.calls = []
        self.fail_skus = fail_skus

    @asynccontextmanager
    async def _tx(self, **kw):
        self.calls.append(("begin", kw))
        try:
            yield
        finally:
            self.calls.append(("end", kw))

    def transaction(self, **kw):
        return self._tx(**kw)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql))

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        if "catalog_skus" in sql:
            if self.fail_skus:
                raise RuntimeError("canceling statement due to statement timeout")
            return [{"product_key": "p1", "titles": ["50ml", None]}]
        if "GROUP BY 1, 2, 3" in sql:
            return [{"product_key": "p1", "market": "US", "host": "a.com", "brand_direct": False}]
        return [{"product_key": "p1", "brand": "Anua", "title": "Toner 50ml", "gtin": None,
                 "content_key": "ck", "pivota_signature_id": "sig", "source_system": "x"}]


def test_load_is_one_read_only_transaction_with_a_timeout():
    conn = _FakeConn()
    products, hosts, skus, err = asyncio.run(census.load(conn))
    assert conn.calls[0] == ("begin", {"readonly": True})
    assert conn.calls[1] == ("execute", "SET LOCAL statement_timeout = '60s'")
    fetches = [c for c in conn.calls if c[0] == "fetch"]
    assert len(fetches) == 3
    assert [f[2] for f in fetches] == [(["US", "SG"],)] * 3  # every statement is market-filtered
    assert len(products) == 1 and len(hosts) == 1 and skus == {"p1": ["50ml"]} and err is None


def test_load_survives_a_failed_variant_statement():
    conn = _FakeConn(fail_skus=True)
    products, hosts, skus, err = asyncio.run(census.load(conn))
    assert skus == {} and "statement timeout" in err and len(products) == 1


def test_every_statement_filters_to_live_signed_products_with_a_market_offer():
    for sql in (census.PRODUCTS_SQL, census.HOSTS_SQL, census.SKU_TITLES_SQL):
        assert "p.pivota_signature_id IS NOT NULL" in sql and "p.sync_status = 'live'" in sql
        assert "o.market = ANY($1::text[])" in sql


def test_the_sql_only_reads():
    for sql in (census.PRODUCTS_SQL, census.HOSTS_SQL, census.SKU_TITLES_SQL):
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT)\b", sql, re.I)


def test_the_program_is_free_of_the_at_sign():
    # run_oneoff_job.sh picks gcloud's --args delimiter from a fixed list that starts with the at-sign.
    assert "@" not in _PATH.read_text()


# ---- review of be#2636 ---------------------------------------------------------------------

OPI_GTIN = "00619828139641"


def _row(pk, brand, title, ck, gtin=None, merchant=None, domain=None):
    r = _p(pk, brand, title, ck, gtin)
    r.update(merchant_id=merchant, source_domain=domain)
    return r


def _us_hosts(rows, host_of):
    return [{"product_key": r["product_key"], "market": "US", "host": host_of(r), "brand_direct": False} for r in rows]


def test_h1_a_gtin_one_seller_reuses_across_shades_never_joins():
    assert census.gtin_canonical(OPI_GTIN) == OPI_GTIN
    rows = [
        _row("ext:retailer:o1", "OPI", "Nail Lacquer Big Apple Red 15ml", "ck_o1", OPI_GTIN, "m_uns", "universalnailsupplies.com"),
        _row("ext:retailer:o2", "OPI", "Nail Lacquer Bubble Bath 15ml", "ck_o2", OPI_GTIN, "m_uns", "universalnailsupplies.com"),
        _row("ext:retailer:o3", "OPI", "Nail Lacquer Malaga Wine 0.5 fl oz", "ck_o3", OPI_GTIN, "m_ulta", "ulta.com"),
    ]
    lines = census.compute_census(rows, _us_hosts(rows, lambda r: r["source_domain"]), {}, ["US"])
    inputs = _get(lines, "inputs", "US")
    assert inputs["gtins_polluted"] == 1
    assert inputs["gtins_polluted_by_reason"] == {"titles_differ_within_seller": 1}
    t0a = _get(lines, "tier", "US", "T0a")
    assert t0a["counts"].get("joinable_groups", 0) == 0 and t0a["counts"].get("key_groups", 0) == 0


def test_h1_t0a_also_needs_the_title_core_to_agree():
    # Three sellers, one shade each: no seller reuses the GTIN, yet the titles name three shades.
    rows = [
        _row("ext:retailer:o1", "OPI", "Nail Lacquer Big Apple Red 15ml", "ck_o1", OPI_GTIN, "m_a", "a.com"),
        _row("ext:retailer:o2", "OPI", "Nail Lacquer Bubble Bath 15ml", "ck_o2", OPI_GTIN, "m_b", "b.com"),
        _row("ext:retailer:o3", "OPI", "Nail Lacquer Malaga Wine 0.5 fl oz", "ck_o3", OPI_GTIN, "m_c", "c.com"),
    ]
    lines = census.compute_census(rows, _us_hosts(rows, lambda r: r["source_domain"]), {}, ["US"])
    assert _get(lines, "inputs", "US")["gtins_polluted"] == 0
    assert _get(lines, "tier", "US", "T0a")["counts"].get("joinable_groups", 0) == 0
    # T0a_size_not_required (GTIN + brand only) shows what the core requirement withheld, and says
    # that it includes wrong merges.
    info = _get(lines, "tier", "US", "T0a_size_not_required")
    assert info["counts"]["split_key_groups"] == 1 and "WRONG MERGES" in info["note"]
    assert "WRONG MERGES" in _get(lines, "tier", "US", "T0c_token_set")["note"]
    assert "note" not in _get(lines, "tier", "US", "T0a")


def test_h2_t0b_defers_a_group_whose_members_are_different_sizes_or_no_size():
    title = "Clinique Moisture Surge 100H Auto-Replenishing Hydrator"
    rows = [
        _row("ext:retailer:c1", "Clinique", title, "ck_c1", None, "m_ulta", "ulta.com"),
        _row("ext:retailer:c2", "Clinique", title, "ck_c2", None, "m_nord", "nordstrom.com"),
        _row("ext:retailer:c3", "Clinique", "Moisture Surge Hydrating Lotion", "ck_c3", None, "m_ulta", "ulta.com"),
        _row("ext:retailer:c4", "Clinique", "Moisture Surge Hydrating Lotion", "ck_c4", None, "m_nord", "nordstrom.com"),
        _row("ext:retailer:c5", "Clinique", "Moisture Surge Eye 96-Hour", "ck_c5", None, "m_ulta", "ulta.com"),
        _row("ext:retailer:c6", "Clinique", "Moisture Surge Eye 96-Hour", "ck_c6", None, "m_nord", "nordstrom.com"),
    ]
    skus = {"ext:retailer:c1": ["1.7 oz"], "ext:retailer:c2": ["Jumbo 4.2 oz"],
            "ext:retailer:c3": ["50 ml"], "ext:retailer:c4": ["15 ml"]}
    lines = census.compute_census(rows, _us_hosts(rows, lambda r: r["source_domain"]), skus, ["US"])
    c = _get(lines, "tier", "US", "T0b")["counts"]
    assert c["accepted_groups"] == 3
    assert c["deferred_multi_size_groups"] == 2   # 1.7 oz vs 4.2 oz; 50 ml vs 15 ml
    assert c["deferred_size_unknown_groups"] == 1  # neither title nor variants state a size
    assert c.get("joinable_groups", 0) == 0


def test_t0c_keys_on_word_order_and_reports_what_that_costs():
    rows = [
        _row("ext:retailer:w1", "Clinique", "Cream to Powder Foundation 30ml", "ck_w1", None, "m_a", "a.com"),
        _row("ext:retailer:w2", "Clinique", "Powder to Cream Foundation 30ml", "ck_w2", None, "m_b", "b.com"),
        _row("ext:retailer:w3", "COSRX", "Snail 96 Mucin Power Essence 100ml", "ck_w3", None, "m_a", "a.com"),
        _row("ext:retailer:w4", "COSRX", "96 Snail Mucin Power Essence 100ml", "ck_w4", None, "m_b", "b.com"),
    ]
    lines = census.compute_census(rows, _us_hosts(rows, lambda r: r["source_domain"]), {}, ["US"])
    assert _get(lines, "tier", "US", "T0c")["counts"].get("joinable_groups", 0) == 0
    # The token set joins both: the foundation pair WRONGLY (same words, one format word, so no
    # format_order token tells them apart) and the snail pair rightly -- the snail pair is the
    # recall the ordered key gives up.
    t0c_set = _get(lines, "tier", "US", "T0c_token_set")
    assert t0c_set["counts"]["joinable_groups"] == 2
