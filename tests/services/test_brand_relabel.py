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
                       "merchant_id", "platform", "spid", "seed_ids"}


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
        self.seeds = dict(seeds or {})  # seed id -> [attached product_key, brand]

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
        assert "external_product_seeds" in sql and "id = ANY(:ids)" in sql
        assert all(isinstance(i, str) for i in v["ids"])  # TEXT ids (mig 044), never cast
        done = []
        for sid in v["ids"]:
            pk, brand = self.seeds.get(sid, (None, None))
            if pk == v["pk"] and brand == v["from_brand"]:
                self.seeds[sid] = [pk, v["to_brand"]]
                done.append({"id": sid})
        return done


async def test_the_write_moves_brand_key_group_and_seed_and_the_revert_puts_them_back():
    a = row("ext:retailer:a")
    db = DB([a], seeds={"seed:catalog_enrichment_agent_v1:aa01": ["ext:retailer:a", "ETUDE HOUSE"], "seed:catalog_enrichment_agent_v1:bb02": ["ext:retailer:a", "ETUDE"]})
    m = rl.manifest_for(rl.plan_relabel([a], [a], CANON, {"ext:retailer:a": ["seed:catalog_enrichment_agent_v1:aa01"]}))
    counts = await rl.write_moves(db, m["moves"])
    assert counts == {"products": 1, "groups": 1, "seeds": 1}
    mv = m["moves"][0]
    assert db.rows["ext:retailer:a"] == {"brand": "ETUDE", "content_key": mv["to_ck"]}
    assert db.groups[(a["merchant_id"], "external_seed", a["source_product_id"])] == mv["to_pg"]
    assert db.seeds["seed:catalog_enrichment_agent_v1:aa01"] == ["ext:retailer:a", "ETUDE"]
    await rl.write_moves(db, m["moves"], reverse=True)
    assert db.rows["ext:retailer:a"] == {"brand": "ETUDE HOUSE", "content_key": a["content_key"]}
    assert db.groups[(a["merchant_id"], "external_seed", a["source_product_id"])] == a["product_group_id"]
    assert db.seeds["seed:catalog_enrichment_agent_v1:aa01"] == ["ext:retailer:a", "ETUDE HOUSE"]
    assert db.seeds["seed:catalog_enrichment_agent_v1:bb02"] == ["ext:retailer:a", "ETUDE"]  # never read under the old spelling: untouched both ways


async def test_a_seed_that_changed_since_the_plan_aborts_the_write():
    a = row("ext:retailer:a")
    db = DB([a], seeds={"seed:catalog_enrichment_agent_v1:aa01": ["ext:retailer:a", "re-crawled to something else"]})
    m = rl.manifest_for(rl.plan_relabel([a], [a], CANON, {"ext:retailer:a": ["seed:catalog_enrichment_agent_v1:aa01"]}))
    with pytest.raises(RuntimeError, match="seed"):
        await rl.write_moves(db, m["moves"])


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
            if "external_product_seeds" in sql:
                return []
            rows = [a, c1, c2]
            return [r for r in rows if r["content_key"] in v["cks"] or (r["gtin"] and r["gtin"] in v["gtins"])]
    rows, neighbours, _ = await rl.load(LoadDB(), ["etudehouse", "etude"], CANON)
    assert rows == [a] and {n["product_key"] for n in neighbours} == {"ext:retailer:a", "ext:retailer:c1",
                                                                      "ext:retailer:c2"}
    assert only(rl.plan_relabel(rows, neighbours, CANON))["hold"] == "target_in_several_groups"


async def test_load_never_offers_a_row_already_spelt_canonically():
    class LoadDB:
        async def fetch_all(self, sql, v):
            if "regexp_replace" in sql:
                assert set(v["keys"]) == {"etudehouse", "etude"}
                return [row("ext:retailer:a"), row("ext:retailer:c", brand="ETUDE")]
            if "external_product_seeds" in sql:
                assert v["pks"] == ["ext:retailer:a"]
                return [{"id": "seed:catalog_enrichment_agent_v1:aa01", "attached_product_key": "ext:retailer:a", "brand": "ETUDE HOUSE"},
                        {"id": "seed:catalog_enrichment_agent_v1:bb02", "attached_product_key": "ext:retailer:a", "brand": "ETUDE"}]
            return []
    rows, _, seeds = await rl.load(LoadDB(), ["etudehouse", "etude"], CANON)
    assert seeds == {"ext:retailer:a": ["seed:catalog_enrichment_agent_v1:aa01"]}  # only the seed under the row's own (old) spelling
    assert [r["product_key"] for r in rows] == ["ext:retailer:a"]


def test_the_candidate_query_is_live_retailer_rows_of_the_family():
    sql = " ".join(rl.CANDIDATES_SQL.split())
    assert "cp.suppression_reason IS NULL AND cp.product_key LIKE 'ext:retailer:%'" in sql
    assert "lower(regexp_replace(cp.brand, '[^[:alnum:]]', '', 'g')) = ANY(:keys)" in sql
    move = " ".join(rl.MOVE_ROW_SQL.split())
    assert "WHERE product_key = :pk AND brand = :from_brand AND content_key IS NOT DISTINCT FROM :from_ck" in move


# --- review of #2434 ---------------------------------------------------------------------------------------

def test_a_row_intake_half_moved_takes_its_key_s_group_instead_of_being_certified():
    """Re-crawled under the family: new key M (the canonical rows' key), old group O. The relabel must put it in
    M's group -- never report the split as "already grouped"."""
    canon = row("ext:retailer:c1", brand="ETUDE", host="dodoskin.com")
    half = row("ext:retailer:a", ck=canon["content_key"], pg="pg_old_spelling")
    m = only(plan([half], extra=[canon]))
    assert m["to_ck"] == canon["content_key"] and m["from_pg"] == "pg_old_spelling"
    assert m["to_pg"] == canon["product_group_id"] and m["reason"] == "brand_only_already_grouped_regrouped"


def test_a_row_whose_own_key_is_split_across_groups_is_held():
    c1 = row("ext:retailer:c1", brand="ETUDE", host="dodoskin.com")
    c2 = {**row("ext:retailer:c2", brand="ETUDE", host="moidaus.com"), "product_group_id": "pg_x"}
    half = row("ext:retailer:a", ck=c1["content_key"], pg="pg_old_spelling")
    assert only(plan([half], extra=[c1, c2]))["hold"] == "own_key_in_several_groups"


def test_two_moving_rows_on_one_key_never_swap_groups():
    """Re-review of #2434: e1 correctly in pg_K, e2 half-moved into the old spelling's pg_O. Reading each other's
    group, they swapped. Both belong in the key's own group."""
    title = "Fixing Tint #01"
    k = make_content_key("Etude", title)
    e1 = row("ext:retailer:e1", brand="Etude", title=title, host="theglowbeautyshop.com")
    e2 = row("ext:retailer:e2", brand="Etude", title=title, host="kbeautymakeup.com",
             pg=derive_product_group_id(make_content_key("ETUDE HOUSE", title)))
    assert e1["content_key"] == e2["content_key"] == k
    p = plan([e1, e2])
    got = {m["product_key"]: (m["to_pg"], m["reason"]) for m in p["moves"]}
    assert got == {"ext:retailer:e1": (derive_product_group_id(k), "case_only"),
                   "ext:retailer:e2": (derive_product_group_id(k), "case_only_regrouped")}


def test_moving_rows_on_one_key_in_two_foreign_groups_are_held():
    title = "Fixing Tint #01"
    e1 = row("ext:retailer:e1", brand="Etude", title=title, pg="pg_x")
    e2 = row("ext:retailer:e2", brand="Etude", title=title, host="kbeautymakeup.com", pg="pg_y")
    assert {h["hold"] for h in plan([e1, e2])["holds"]} == {"own_key_in_several_groups"}


def test_a_case_only_row_alone_on_its_key_keeps_its_own_group():
    e = row("ext:retailer:e", brand="Etude", host="theglowbeautyshop.com", pg="pg_custom")
    m = only(plan([e]))
    assert m["reason"] == "case_only" and m["to_pg"] == "pg_custom"


def test_a_title_match_with_a_different_barcode_is_held_not_merged():
    canon = row("ext:retailer:c1", brand="ETUDE", gtin="8801111", host="dodoskin.com")
    assert only(plan([row("ext:retailer:a", gtin="8802222")], extra=[canon]))["hold"] == \
        "gtin_conflicts_with_title_match"
    # no barcode on either side, or the same one: joined as before
    assert only(plan([row("ext:retailer:a")], extra=[canon]))["reason"] == "joined_by_title"


def test_the_family_lists_its_accented_spellings():
    from scripts.relabel_retailer_brand import FAMILIES
    canonical, spellings = FAMILIES["etude"]
    keys = {rl.brand_alnum(s) for s in spellings}
    assert canonical == "ETUDE" and {"etudehouse", "etude", "étudehouse", "étude"} <= keys


def test_every_relabel_family_writes_the_ingest_familys_canonical_spelling():
    """One spelling per brand has one owner (curated_brand_feed.RETAILER_BRAND_CANONICAL): a relabel to any
    other spelling would be undone -- in display -- by the next ingest. A relabel family whose spelling family
    is not live yet (identity-changing: relabel FIRST, family after) must at least cover its own spellings."""
    from scripts.relabel_retailer_brand import FAMILIES
    from services import curated_brand_feed as feed
    for name, (canonical, spellings) in FAMILIES.items():
        assert canonical in spellings, name
        family = feed._retailer_brand_family(feed._brand_key(canonical))
        if family is not None:
            assert feed.RETAILER_BRAND_CANONICAL[family] == canonical, name
            assert all(feed._retailer_brand_family(feed._brand_key(s)) == family for s in spellings), name
    # Every identity-changing relabel family is now live as a spelling family, each added only after its rows were
    # relabelled (services/brand_relabel.py ORDER): jungsaemmool (relabel_6239d93b1080), ohui (relabel_fad2cefd5223).


def test_the_losing_side_of_every_move_is_rebuilt_before_any_gaining_side():
    moves = [{"from_ck": "ck_a", "to_ck": "ck_m"}, {"from_ck": "ck_b", "to_ck": "ck_a"}]
    assert rl.touched_keys(moves) == ["ck_a", "ck_b", "ck_m"]
    assert rl.touched_keys(moves, reverse=True) == ["ck_m", "ck_a", "ck_b"]


async def test_refresh_rebuilds_every_key_trusts_every_row_on_them_and_lists_failures(monkeypatch):
    from services import agent_pdp_view_assembler as apv, catalog_row_trust_upserter as tr
    from services import index_pipeline_state_service as ips
    calls = []

    async def refresh(ck, **kw):
        calls.append(("refresh", ck))
        if ck == "ck_bad":
            raise RuntimeError("boom")
        return True

    async def reap(ck, **kw):
        calls.append(("reap", ck))
        return ck == "ck_a"

    async def recompute(ck, **kw):
        calls.append(("recompute", ck))

    async def trust(*, db, product_keys):
        calls.append(("trust", tuple(product_keys)))
        return len(product_keys)
    monkeypatch.setattr(apv, "refresh_agent_pdp_view_for_content_key", refresh)
    monkeypatch.setattr(apv, "delete_agent_pdp_view_if_orphaned", reap)
    monkeypatch.setattr(ips, "recompute_serving_eligibility", recompute)
    monkeypatch.setattr(tr, "upsert_catalog_row_trust_many", trust)

    class DB:
        async def fetch_all(self, sql, v):
            assert "content_key = ANY(:cks)" in sql and v["cks"] == ["ck_a", "ck_bad", "ck_m"]
            return [{"product_key": "p_moved"}, {"product_key": "p_canonical_already_there"}]
    out = await rl.refresh_after(DB(), [{"from_ck": "ck_a", "to_ck": "ck_m"}, {"from_ck": "ck_bad", "to_ck": "ck_m"}],
                                 source="t")
    assert [c for c in calls if c[0] == "refresh"] == [("refresh", "ck_a"), ("refresh", "ck_bad"), ("refresh", "ck_m")]
    assert ("reap", "ck_a") in calls and out["reaped"] == 1
    assert calls[-1] == ("trust", ("p_moved", "p_canonical_already_there")) and out["trust"] == 2
    assert [f["content_key"] for f in out["failed_keys"]] == ["ck_bad"] and out["recomputed"] == 2



async def test_apply_stores_the_manifest_before_any_write_and_prints_no_manifest_line(monkeypatch, capsys):
    import scripts.relabel_retailer_brand as cli
    events = []

    class FakeDatabase:
        async def connect(self): events.append("connect")
        async def disconnect(self): events.append("disconnect")

    async def load(db, spellings, canonical):
        return [row("ext:retailer:a")], [row("ext:retailer:a")], {}

    async def store(db, manifest):
        events.append(("store", manifest["run_id"]))

    async def write(db, moves, reverse=False, run_id=None):
        assert run_id and run_id.startswith("relabel_")  # the applied event is written with the moves
        events.append(("write", len(moves)))
        return {"products": len(moves), "groups": 1, "seeds": 0}

    async def refresh(db, moves, **kw):
        events.append("refresh")
        return {}
    monkeypatch.setattr(cli, "database", FakeDatabase())
    monkeypatch.setattr(rl, "load", load)
    monkeypatch.setattr(rl, "store_manifest", store)
    monkeypatch.setattr(rl, "write_moves", write)
    monkeypatch.setattr(rl, "refresh_after", refresh)
    import argparse
    await cli.run(argparse.Namespace(command="plan", family="etude", apply=True, manifest=None, run_id=None,
                                     reverse=False))
    kinds = [e[0] if isinstance(e, tuple) else e for e in events]
    assert kinds == ["connect", "store", "write", "refresh", "disconnect"]
    out = capsys.readouterr().out
    assert "MANIFEST STORED run_id=relabel_" in out and '"moves": [' not in out  # never the whole manifest


async def test_revert_by_run_id_reads_the_stored_manifest(monkeypatch):
    import argparse
    import scripts.relabel_retailer_brand as cli
    seen = []

    class FakeDatabase:
        async def connect(self): pass
        async def disconnect(self): pass

    async def load_manifest(db, run_id):
        seen.append(run_id)
        return {"run_id": run_id, "moves": [{"from_ck": "a", "to_ck": "b"}]}

    async def write(db, moves, reverse=False, run_id=None):
        seen.append(("write", reverse, run_id))
        return {}

    async def refresh(db, moves, **kw):
        seen.append(("refresh", kw["reverse"]))
        return {}
    monkeypatch.setattr(cli, "database", FakeDatabase())
    monkeypatch.setattr(rl, "load_manifest", load_manifest)
    monkeypatch.setattr(rl, "write_moves", write)
    monkeypatch.setattr(rl, "refresh_after", refresh)
    await cli.run(argparse.Namespace(command="revert", family=None, apply=False, manifest=None,
                                     run_id="relabel_x", reverse=False))
    assert seen == ["relabel_x", ("write", True, "relabel_x"), ("refresh", True)]


async def test_store_manifest_raises_when_nothing_was_stored():
    class DB:
        async def fetch_one(self, sql, v):
            assert "identity_resolution_events" in sql and v["action"] == rl.MANIFEST_ACTION
            return None
    with pytest.raises(RuntimeError, match="not stored"):
        await rl.store_manifest(DB(), {"run_id": "relabel_x", "moves": []})



def test_status_lines_are_bounded():
    import scripts.relabel_retailer_brand as cli
    failed = [{"content_key": f"ck_{i}", "error": "x" * 200} for i in range(606)]
    out = cli.bounded({"refreshed": 0, "failed_keys": failed})
    assert out["failed_keys_total"] == 606 and len(out["failed_keys"]) == cli.FAILED_KEYS_SHOWN
    assert len(rl.dumps(out)) < 100_000
