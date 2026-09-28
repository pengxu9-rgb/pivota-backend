"""Relabel existing retailer rows to a family's canonical spelling (services/brand_relabel.py).

Measured 2026-09-28: ETUDE's retailer rows split 293 "ETUDE HOUSE" / 174 "ETUDE" / 10 "Etude" across 20 stores,
so the same product at two retailers never grouped. A crawl never updates a stored brand; this moves the rows.
"""
import pytest

from services import brand_relabel as rl
from services.catalog_identity import make_content_key
from services.product_group_autogrouper import derive_product_group_id

CANON = "ETUDE"


def row(pk, brand="ETUDE HOUSE", title="Fixing Tint", ck=None, gtin=None, pg="auto", host="luxiface.com"):
    ck = ck if ck is not None else make_content_key(brand, title)
    return {"product_key": pk, "brand": brand, "title": title, "gtin": gtin, "content_key": ck,
            "merchant_id": f"m_{host}", "platform": "external_seed", "source_product_id": f"retailer:{pk}",
            "source_domain": host, "product_group_id": derive_product_group_id(ck) if pg == "auto" else pg}


def plan(rows, extra=()):
    rows = list(rows)
    neighbours = [*rows, *extra]
    return rl.plan_relabel([r for r in rows if r["brand"] != CANON], neighbours, CANON)


def only(p):
    assert len(p["moves"]) + len(p["holds"]) == 1
    return (p["moves"] or p["holds"])[0]


def test_a_row_joins_the_canonical_rows_of_the_same_product_by_title():
    canon = row("ext:retailer:c1", brand="ETUDE", host="dodoskin.com")
    m = only(plan([row("ext:retailer:a")], extra=[canon]))
    assert m["reason"] == "joined_by_title"
    assert m["to_ck"] == canon["content_key"] and m["to_pg"] == canon["product_group_id"]
    assert m["to_brand"] == "ETUDE" and m["from_brand"] == "ETUDE HOUSE"


def test_with_no_canonical_row_the_key_is_the_one_intake_mints():
    m = only(plan([row("ext:retailer:a")]))
    assert m["reason"] == "minted"
    assert m["to_ck"] == make_content_key("ETUDE", "Fixing Tint")
    assert m["to_pg"] == derive_product_group_id(m["to_ck"])


def test_two_retailers_of_one_product_move_together_into_one_group():
    a, b = row("ext:retailer:a"), row("ext:retailer:b", host="holiholic.com")
    b["product_group_id"] = a["product_group_id"]  # already grouped under the old spelling
    p = plan([a, b])
    assert {m["to_ck"] for m in p["moves"]} == {make_content_key("ETUDE", "Fixing Tint")}
    assert len({m["to_pg"] for m in p["moves"]}) == 1 and p["holds"] == []


def test_a_gtin_match_joins_that_product_even_when_titles_differ():
    canon = row("ext:retailer:c1", brand="ETUDE", title="Fixing Tint 4g #03", gtin="8809667988", host="dodoskin.com")
    m = only(plan([row("ext:retailer:a", gtin="8809667988")], extra=[canon]))
    assert m["reason"] == "joined_by_gtin" and m["to_ck"] == canon["content_key"]


def test_a_gtin_on_two_canonical_products_is_held():
    c1 = row("ext:retailer:c1", brand="ETUDE", title="Tint A", gtin="880", host="dodoskin.com")
    c2 = row("ext:retailer:c2", brand="ETUDE", title="Tint B", gtin="880", host="moidaus.com")
    assert only(plan([row("ext:retailer:a", gtin="880")], extra=[c1, c2]))["hold"] == "gtin_matches_several_products"


def test_a_target_split_across_groups_is_held():
    c1 = row("ext:retailer:c1", brand="ETUDE", host="dodoskin.com")
    c2 = row("ext:retailer:c2", brand="ETUDE", host="moidaus.com", pg="pg_other")
    assert only(plan([row("ext:retailer:a")], extra=[c1, c2]))["hold"] == "target_in_several_groups"


def test_a_row_already_on_a_canonical_product_changes_only_its_brand():
    """Intake identity attached it to an ETUDE product before: keep that key and group."""
    canon = row("ext:retailer:c1", brand="ETUDE", title="Fixing Tint", host="dodoskin.com")
    attached = row("ext:retailer:a", title="Etude House Fixing Tint 4g", ck=canon["content_key"],
                   pg=canon["product_group_id"])
    m = only(plan([attached], extra=[canon]))
    assert m["reason"] == "brand_only_already_grouped"
    assert m["to_ck"] == m["from_ck"] and m["to_pg"] == m["from_pg"] and m["to_brand"] == "ETUDE"


def test_a_row_joins_a_case_only_sibling_s_group_even_though_that_sibling_is_moving_too():
    """"Etude" and "ETUDE" share a content_key (normalize_brand lowercases); an "Etude" row identity put in its own
    group already sits on the key an "ETUDE HOUSE" row of the same product moves to -- join THAT group."""
    e = row("ext:retailer:e", brand="Etude", host="theglowbeautyshop.com", pg="pg_custom")
    p = plan([row("ext:retailer:a"), e])
    a = next(m for m in p["moves"] if m["product_key"] == "ext:retailer:a")
    assert a["to_ck"] == e["content_key"] and a["to_pg"] == "pg_custom" and a["reason"] == "joined_by_title"


def test_a_case_only_spelling_keeps_its_key():
    m = only(plan([row("ext:retailer:a", brand="Etude", host="theglowbeautyshop.com")]))
    assert m["reason"] == "case_only" and m["to_ck"] == m["from_ck"] and m["to_brand"] == "ETUDE"


def test_moving_one_member_of_a_product_that_another_brand_shares_is_held():
    """A row of an unrelated brand on the same content_key would be left alone on it: the product would split."""
    a = row("ext:retailer:a")
    stranger = {**row("ext:retailer:x", brand="Some Other Brand", host="x.com"), "content_key": a["content_key"],
                "product_group_id": a["product_group_id"]}
    assert only(plan([a], extra=[stranger]))["hold"] == "would_split_its_product"


def test_members_of_one_product_that_would_land_on_different_keys_are_held():
    a = row("ext:retailer:a", title="Fixing Tint")
    b = {**row("ext:retailer:b", title="Fixing Tint 4g", host="holiholic.com"), "content_key": a["content_key"],
         "product_group_id": a["product_group_id"]}
    p = plan([a, b])
    assert {h["hold"] for h in p["holds"]} == {"would_split_its_product"} and p["moves"] == []


def test_the_manifest_carries_every_from_and_to_value():
    p = plan([row("ext:retailer:a")])
    m = rl.manifest_for(p)
    assert m["run_id"].startswith("relabel_") and m["canonical"] == "ETUDE"
    [mv] = m["moves"]
    assert set(mv) == {"product_key", "from_brand", "to_brand", "from_ck", "to_ck", "from_pg", "to_pg",
                       "merchant_id", "platform", "spid"}


# --- the write, against a fake database --------------------------------------------------------------------

class Tx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class DB:
    """product rows by key; group members by (merchant, platform, spid); seed brands by attached key."""
    def __init__(self, rows, seeds=None):
        self.rows = {r["product_key"]: {"brand": r["brand"], "content_key": r["content_key"]} for r in rows}
        self.groups = {(r["merchant_id"], r["platform"], r["source_product_id"]): r["product_group_id"]
                       for r in rows if r.get("product_group_id")}
        self.seeds = dict(seeds or {})

    def transaction(self): return Tx()

    async def fetch_one(self, sql, v):
        if "UPDATE catalog_products" in sql:
            r = self.rows.get(v["pk"])
            if r and r["brand"] == v["from_brand"] and r["content_key"] == v["from_ck"]:
                r.update(brand=v["to_brand"], content_key=v["to_ck"])
                return {"product_key": v["pk"]}
            return None
        key = (v["merchant_id"], v["platform"], v["spid"])
        if "UPDATE product_group_members" in sql:
            if self.groups.get(key) == v["from_pg"]:
                self.groups[key] = v["to_pg"]
                return {"product_group_id": v["to_pg"]}
            return None
        if "INSERT INTO product_group_members" in sql:
            if key in self.groups:
                return None
            self.groups[key] = v["to_pg"]
            return {"product_group_id": v["to_pg"]}
        if "DELETE FROM product_group_members" in sql:
            if self.groups.get(key) == v["pg"]:
                del self.groups[key]
                return {"product_group_id": v["pg"]}
            return None
        raise AssertionError(sql)

    async def fetch_all(self, sql, v):
        assert "external_product_seeds" in sql
        if self.seeds.get(v["pk"]) == v["from_brand"]:
            self.seeds[v["pk"]] = v["to_brand"]
            return [{"id": 1}]
        return []


async def test_the_write_moves_brand_key_group_and_seed_and_the_revert_puts_them_back():
    a = row("ext:retailer:a")
    db = DB([a], seeds={"ext:retailer:a": "ETUDE HOUSE"})
    m = rl.manifest_for(plan([a]))
    counts = await rl.write_moves(db, m["moves"])
    assert counts == {"products": 1, "groups": 1, "seeds": 1}
    mv = m["moves"][0]
    assert db.rows["ext:retailer:a"] == {"brand": "ETUDE", "content_key": mv["to_ck"]}
    assert db.groups[(a["merchant_id"], "external_seed", a["source_product_id"])] == mv["to_pg"]
    assert db.seeds["ext:retailer:a"] == "ETUDE"
    await rl.write_moves(db, m["moves"], reverse=True)
    assert db.rows["ext:retailer:a"] == {"brand": "ETUDE HOUSE", "content_key": a["content_key"]}
    assert db.groups[(a["merchant_id"], "external_seed", a["source_product_id"])] == a["product_group_id"]
    assert db.seeds["ext:retailer:a"] == "ETUDE HOUSE"


async def test_a_row_without_membership_gets_the_target_group_and_the_revert_removes_it():
    a = row("ext:retailer:a", pg=None)
    db = DB([a])
    m = rl.manifest_for(plan([a]))
    await rl.write_moves(db, m["moves"])
    key = (a["merchant_id"], "external_seed", a["source_product_id"])
    assert db.groups[key] == m["moves"][0]["to_pg"]
    await rl.write_moves(db, m["moves"], reverse=True)
    assert key not in db.groups


@pytest.mark.parametrize("drift", ["brand", "content_key", "group"])
async def test_any_drift_since_the_plan_aborts_the_write(drift):
    a = row("ext:retailer:a")
    db = DB([a])
    m = rl.manifest_for(plan([a]))
    if drift == "group":
        db.groups[(a["merchant_id"], "external_seed", a["source_product_id"])] = "pg_moved_by_someone"
    else:
        db.rows["ext:retailer:a"][drift] = "changed"
    with pytest.raises(RuntimeError, match="drift"):
        await rl.write_moves(db, m["moves"])


async def test_a_brand_only_move_touches_no_group():
    a = row("ext:retailer:a", brand="Etude", host="theglowbeautyshop.com")
    db = DB([a])
    counts = await rl.write_moves(db, rl.manifest_for(plan([a]))["moves"])
    assert counts == {"products": 1, "groups": 0, "seeds": 0} and db.rows["ext:retailer:a"]["brand"] == "ETUDE"


async def test_load_finds_the_whole_group_behind_a_gtin_match():
    """Pass one sees the canonical row by GTIN; pass two loads every row on ITS content_key, so a target
    split across groups is visible to the planner."""
    a = row("ext:retailer:a", gtin="880")
    c1 = row("ext:retailer:c1", brand="ETUDE", title="Other Title", gtin="880", host="dodoskin.com")
    c2 = {**row("ext:retailer:c2", brand="ETUDE", title="Other Title", host="moidaus.com"), "product_group_id": "pg_x"}
    calls = []

    class LoadDB:
        async def fetch_all(self, sql, v):
            calls.append(v)
            if "regexp_replace" in sql:
                return [a]
            rows = [a, c1, c2]
            return [r for r in rows if r["content_key"] in v["cks"] or (r["gtin"] and r["gtin"] in v["gtins"])]
    rows, neighbours = await rl.load(LoadDB(), ["etudehouse", "etude"], CANON)
    assert rows == [a] and {n["product_key"] for n in neighbours} == {"ext:retailer:a", "ext:retailer:c1",
                                                                      "ext:retailer:c2"}
    assert only(rl.plan_relabel(rows, neighbours, CANON))["hold"] == "target_in_several_groups"


async def test_load_never_offers_a_row_already_spelt_canonically():
    class LoadDB:
        async def fetch_all(self, sql, v):
            if "regexp_replace" in sql:
                assert set(v["keys"]) == {"etudehouse", "etude"}
                return [row("ext:retailer:a"), row("ext:retailer:c", brand="ETUDE")]
            return []
    rows, _ = await rl.load(LoadDB(), ["etudehouse", "etude"], CANON)
    assert [r["product_key"] for r in rows] == ["ext:retailer:a"]


def test_the_candidate_query_is_live_retailer_rows_of_the_family():
    sql = " ".join(rl.CANDIDATES_SQL.split())
    assert "cp.suppression_reason IS NULL AND cp.product_key LIKE 'ext:retailer:%'" in sql
    assert "lower(regexp_replace(cp.brand, '[^[:alnum:]]', '', 'g')) = ANY(:keys)" in sql
    move = " ".join(rl.MOVE_ROW_SQL.split())
    assert "WHERE product_key = :pk AND brand = :from_brand AND content_key IS NOT DISTINCT FROM :from_ck" in move
