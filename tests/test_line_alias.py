"""Line-alias layer tests — certified vocabulary drift becomes deterministic,
under strict additive-only + ambiguity-guard invariants.

Real-world shapes come from the judge-certified drift pairs mined 2026-07-17:
SKIN1004 drops "Madagascar (Centella)" on the official side (both classes:
whole-phrase and Madagascar-only), COSRX's "Full Fit" line appears official-
side only, and SPF/PA suffixes drift on sunscreen titles.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./pivota_test.db")

from services.pdp_matcher.line_alias import alias_match_keys  # noqa: E402
from services.pdp_matcher.retailer_match import (  # noqa: E402
    build_match_index,
    match_record,
    retailer_match_key,
)


def _row(pk: str, brand: str, title: str, scope: str = "merchant_owned") -> Dict[str, Any]:
    return {"product_key": pk, "content_key": f"ck_{pk}", "brand": brand,
            "title": title, "pdp_scope": scope}


# --- alias_match_keys ----------------------------------------------------------

def test_alias_keys_cover_both_certified_skin1004_classes():
    keys = alias_match_keys("SKIN1004", "Madagascar Centella Poremizing Fresh Ampoule 100ml")
    # whole-phrase class (64x): official title has neither token
    assert "skin1004::poremizing fresh ampoule" in keys
    # madagascar-only class (27x): official title keeps "centella"
    assert "skin1004::centella poremizing fresh ampoule" in keys
    # never the primary key itself
    assert retailer_match_key("SKIN1004", "Madagascar Centella Poremizing Fresh Ampoule 100ml") not in keys


def test_alias_keys_empty_without_certified_vocabulary():
    assert alias_match_keys("COSRX", "Advanced Snail 96 Mucin Power Essence") == []
    assert alias_match_keys(None, "anything") == []
    assert alias_match_keys("SKIN1004", "") == []


def test_alias_keys_strip_suncare_noise_only_for_certified_brands():
    keys = alias_match_keys("Beauty of Joseon", "Relief Sun Rice Probiotics SPF50 PA")
    assert "beauty of joseon::relief sun rice probiotics" in keys
    # NOT certified for suncare drift -> no suncare variants (an uncurated
    # global strip cross-attached different products; 90be1574 review blocker)
    assert alias_match_keys("Kiehls", "Ultra Facial Cream SPF30") == []


def test_cross_rating_alias_attach_refused():
    # catalog carries ONLY the SPF50 variant; an SPF30 listing must not
    # alias-attach to it (certified drift = rating DROPPED, never CHANGED)
    index = build_match_index([_row("pk50", "Beauty of Joseon", "Relief Sun SPF50")])
    assert match_record(index, "Beauty of Joseon", "Relief Sun SPF30 50ml") is None
    # one-sided rating (the certified class) still bridges
    hit = match_record(index, "Beauty of Joseon", "Relief Sun 50ml")
    assert hit and hit["product_key"] == "pk50"


def test_alias_vs_primary_cross_product_collision_blocks_alias_lane():
    # pk_spf's suncare alias key equals pk_plain's PRIMARY key — two different
    # products one strip apart. Alias-lane lookups on that key must refuse;
    # direct primary queries keep working.
    index = build_match_index([
        _row("pk_plain", "Beauty of Joseon", "Glow Serum"),
        _row("pk_spf", "Beauty of Joseon", "Glow Serum SPF50"),
    ])
    # query alias variant lands on the blocked key -> None, not pk_plain
    assert match_record(index, "Beauty of Joseon", "Glow Serum SPF30 30ml") is None
    # direct primary queries unaffected
    assert match_record(index, "Beauty of Joseon", "Glow Serum")["product_key"] == "pk_plain"
    assert match_record(index, "Beauty of Joseon", "Glow Serum SPF50")["product_key"] == "pk_spf"


# --- index integration ---------------------------------------------------------

def test_query_with_line_prefix_matches_official_titled_row():
    index = build_match_index([_row("pk1", "SKIN1004", "Poremizing Fresh Ampoule")])
    hit = match_record(index, "SKIN1004", "Madagascar Centella Poremizing Fresh Ampoule 100ml")
    assert hit and hit["product_key"] == "pk1" and hit.get("_alias_matched") is True


def test_row_with_line_prefix_matches_plain_query():
    # reverse direction: OUR row carries the line name, the query does not
    index = build_match_index([_row("pk1", "COSRX", "Full Fit Propolis Synergy Toner 280ml")])
    hit = match_record(index, "COSRX", "Propolis Synergy Toner")
    assert hit and hit["product_key"] == "pk1"


def test_primary_match_never_reports_alias():
    index = build_match_index([_row("pk1", "SKIN1004", "Centella Ampoule")])
    hit = match_record(index, "SKIN1004", "Centella Ampoule 100ml")
    assert hit and "_alias_matched" not in hit


def test_alias_never_shadows_primary_key():
    # pk2's alias form ("centella ampoule") collides with pk1's PRIMARY key —
    # the primary must win for direct queries.
    index = build_match_index([
        _row("pk1", "SKIN1004", "Centella Ampoule"),
        _row("pk2", "SKIN1004", "Madagascar Centella Ampoule"),
    ])
    hit = match_record(index, "SKIN1004", "Centella Ampoule")
    assert hit and hit["product_key"] == "pk1" and "_alias_matched" not in hit


def test_ambiguous_alias_key_is_dropped():
    # two DIFFERENT products collapse onto the same alias key -> neither may
    # win it (a wrong deterministic attach is worse than a residue row).
    index = build_match_index([
        _row("pk1", "SKIN1004", "Madagascar Centella Soothing Cream"),
        _row("pk2", "SKIN1004", "Madagascar Soothing Cream"),
    ])
    # both alias to "skin1004::soothing cream"; the key must not resolve
    assert index.get("skin1004::soothing cream") is None
    assert match_record(index, "SKIN1004", "Soothing Cream") is None
    # primaries still work; pk1's UNIQUE alias variant survives the drop
    assert match_record(index, "SKIN1004", "Madagascar Centella Soothing Cream")["product_key"] == "pk1"
    assert match_record(index, "SKIN1004", "Madagascar Soothing Cream")["product_key"] == "pk2"
    assert match_record(index, "SKIN1004", "Centella Soothing Cream")["product_key"] == "pk1"


def test_spf_variants_of_one_product_collapse_and_are_guarded():
    # SPF30 and SPF50 variants are distinct products sharing an alias key —
    # the guard must drop it so neither gets a wrong deterministic attach,
    # even for a rating-less query that can't be refused by rating conflict.
    index = build_match_index([
        _row("pk30", "Beauty of Joseon", "Relief Sun SPF30"),
        _row("pk50", "Beauty of Joseon", "Relief Sun SPF50"),
    ])
    assert match_record(index, "Beauty of Joseon", "Relief Sun") is None


def test_scope_rank_still_applies_to_primary_collisions():
    index = build_match_index([
        _row("pk_a", "COSRX", "Snail Essence", scope="merchant_owned"),
        _row("pk_b", "COSRX", "Snail Essence 100ml", scope="multi_merchant_canonical"),
    ])
    hit = match_record(index, "COSRX", "Snail Essence")
    assert hit and hit["product_key"] == "pk_b"


# --- HITL emit grouping (pure parts) -------------------------------------------

@pytest.mark.asyncio
async def test_hitl_emit_groups_no_targets_and_detects_dups(tmp_path, monkeypatch):
    import json

    import scripts.stylekorean_hitl as hitl

    # judge proposals: 2 SK size-variants certify to ONE un-minted official +
    # 1 verdict whose official IS in catalog (must not become a proposal)
    pfile = tmp_path / "judge_proposals_anua.jsonl"
    def _auto(sk_title: str, off_name: str) -> Dict[str, Any]:
        return {"bucket": "auto",
                "item": {"brand": "Anua", "title": sk_title, "url": "https://sk/x",
                         "price": "10.0", "currency": "USD",
                         "record": {"offers": [{"availability": "in_stock"}]}},
                "official": {"pdp": {"brand": "Anua", "product_name": off_name,
                                     "source_domain": "anua.us"}},
                "verdict": {"confidence": 0.95}}
    with open(pfile, "w") as f:
        f.write(json.dumps(_auto("PDRN Capsule Mask (1ea)", "PDRN Capsule 100 Serum Mask")) + "\n")
        f.write(json.dumps(_auto("PDRN Capsule Mask (10ea)", "PDRN Capsule 100 Serum Mask")) + "\n")
        f.write(json.dumps(_auto("Heartleaf Toner 250ml", "Heartleaf Toner")) + "\n")

    our_rows = [
        _row("pk_toner", "Anua", "Heartleaf Toner"),
        # duplicate canonicals: same match key, different content_keys
        _row("pk_dup1", "Missha", "Night Repair Ampoule 5X"),
        {**_row("pk_dup2", "Missha", "Night Repair Ampoule 5X 75ml"), "content_key": "ck_other"},
    ]

    class EmitFakeDB:
        async def connect(self): pass
        async def disconnect(self): pass

    async def fake_load(brands: Any) -> Any:
        return our_rows

    monkeypatch.setattr(hitl, "database", EmitFakeDB())
    monkeypatch.setattr(hitl, "_load_our_rows", fake_load)

    out = tmp_path / "hitl.jsonl"
    import argparse
    rc = await hitl._emit(argparse.Namespace(proposals=str(pfile), out=str(out)))
    assert rc == 0
    rows = [json.loads(l) for l in open(out)]
    mints = [r for r in rows if r["kind"] == "mint_and_attach"]
    merges = [r for r in rows if r["kind"] == "merge_duplicate"]
    # the two PDRN variants group onto ONE official proposal; the in-catalog
    # official produces none
    assert len(mints) == 1 and len(mints[0]["items"]) == 2
    assert mints[0]["official"]["pdp"]["product_name"] == "PDRN Capsule 100 Serum Mask"
    # the missha pair is detected exactly once
    assert len(merges) == 1
    assert {r["product_key"] for r in merges[0]["rows"]} == {"pk_dup1", "pk_dup2"}
    # everything emitted pending — nothing pre-approved
    assert all(r["status"] == "pending" for r in rows)


@pytest.mark.asyncio
async def test_hitl_apply_positive_path(tmp_path, monkeypatch, capsys):
    """An APPROVED mint_and_attach must mint the net-new official and attach
    the certified SK offers — the write lane's happy path."""
    import argparse
    import contextlib
    import json

    import scripts.stylekorean_hitl as hitl

    official = {"pdp": {"brand": "Anua", "product_name": "PDRN Capsule 100 Serum Mask",
                        "source_domain": "anua.us"},
                "offers": [{"canonical_url": "https://anua.us/products/pdrn", "price": 24.0}]}
    reviewed = tmp_path / "reviewed.jsonl"
    with open(reviewed, "w") as f:
        f.write(json.dumps({
            "kind": "mint_and_attach", "status": "approved", "official": official,
            "items": [{"brand": "Anua", "title": "PDRN Capsule Mask (1ea)",
                       "url": "https://sk/x", "price": "10.0", "currency": "USD",
                       "record": {"offers": [{"availability": "in_stock"}]}}],
        }) + "\n")

    calls: list = []
    minted_row = _row("pk_new", "Anua", "PDRN Capsule 100 Serum Mask")

    class ApplyFakeDB:
        async def connect(self): pass
        async def disconnect(self): pass

    async def fake_load(brands: Any) -> Any:
        # before mint: empty; after mint: the new canonical appears
        return [minted_row] if "ingest" in [c[0] for c in calls] else []

    def fake_filter(officials: Any, our_rows: Any):
        return list(officials), [], []  # net-new

    async def fake_ingest(**kw: Any):
        calls.append(("ingest", kw["official_records"][0]["pdp"]["product_name"]))
        return {"applied": {"pdps": 1}}

    async def fake_attach(items: Any):
        calls.append(("attach", [i["matched_product_key"] for i in items]))
        return len(items), 0

    @contextlib.asynccontextmanager
    async def fake_lock(db: Any):
        yield True

    monkeypatch.setattr(hitl, "database", ApplyFakeDB())
    monkeypatch.setattr(hitl, "_load_our_rows", fake_load)
    monkeypatch.setattr(hitl, "_attach_offer_items", fake_attach)
    monkeypatch.setattr(hitl, "retailer_ingest_lock", fake_lock)
    monkeypatch.setattr(hitl.bo, "filter_net_new", fake_filter)
    monkeypatch.setattr(hitl.bo, "ingest_brand_official", fake_ingest)

    rc = await hitl._apply(argparse.Namespace(reviewed=str(reviewed)))
    assert rc == 0
    assert calls == [("ingest", "PDRN Capsule 100 Serum Mask"), ("attach", ["pk_new"])]


@pytest.mark.asyncio
async def test_hitl_apply_skips_merges_and_unapproved(tmp_path, monkeypatch, capsys):
    import argparse
    import json

    import scripts.stylekorean_hitl as hitl

    reviewed = tmp_path / "reviewed.jsonl"
    with open(reviewed, "w") as f:
        f.write(json.dumps({"kind": "merge_duplicate", "status": "approved", "rows": []}) + "\n")
        f.write(json.dumps({"kind": "mint_and_attach", "status": "pending",
                            "official": {}, "items": []}) + "\n")

    called = []
    monkeypatch.setattr(hitl, "database", type("D", (), {
        "connect": staticmethod(lambda: _noop()), "disconnect": staticmethod(lambda: _noop())})())

    async def _noop(): return None

    async def fake_ingest(**kw: Any):
        called.append("ingest")
        return {"applied": {}}

    monkeypatch.setattr(hitl.bo, "ingest_brand_official", fake_ingest)
    rc = await hitl._apply(argparse.Namespace(reviewed=str(reviewed)))
    assert rc == 0
    assert called == []  # merge skipped, pending mint skipped — zero writes
    out = capsys.readouterr().out
    assert "SKIPPING 1 merge_duplicate" in out


# --- wave3 orchestrator pure helpers ------------------------------------------

def test_wave3_candidate_domains_and_vendor_verification():
    from scripts.wave3_stylekorean_longtail import candidate_domains, vendor_matches_brand

    cands = candidate_domains("Beauty of Joseon", "beauty-of-joseon")
    assert "beautyofjoseon.com" in cands
    assert cands.index("beautyofjoseon.com") == 0  # display-name flat comes first
    assert any(c.startswith("beauty-of-joseon.") for c in cands)

    # vendor verification: containment either way, never empty, never unrelated
    assert vendor_matches_brand("Anua US", "Anua")
    assert vendor_matches_brand("BOJ", "BOJ")
    assert vendor_matches_brand("Beauty of Joseon", "Beauty of Joseon UK")
    assert not vendor_matches_brand("Glow Recipe", "Abib")
    assert not vendor_matches_brand("", "Abib")


def test_wave3_plan_stats(tmp_path):
    import json as _json

    from scripts.wave3_stylekorean_longtail import _plan_stats

    p = tmp_path / "plan.jsonl"
    with open(p, "w") as f:
        f.write(_json.dumps({"decision": "attach", "brand": "Abib"}) + "\n")
        f.write(_json.dumps({"decision": "mint", "brand": "Abib"}) + "\n")
        f.write(_json.dumps({"decision": "mint", "brand": "Abib"}) + "\n")
    s = _plan_stats(str(p))
    assert s == {"attach": 1, "mint": 2, "brand_display": "Abib"}
    assert _plan_stats(str(tmp_path / "missing.jsonl"))["attach"] == 0
