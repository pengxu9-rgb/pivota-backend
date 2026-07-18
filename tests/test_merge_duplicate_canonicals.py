"""Merge executor decision-logic tests — the destructive-critical path.

Winner selection must NEVER guess: the KEEP ck from the adjudicator note is
the decision, cross-checked against scope rank; any disagreement/ambiguity
skips the pair rather than risk collapsing two real products.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./pivota_test.db")

from scripts.merge_duplicate_canonicals import (  # noqa: E402
    _target_offer_id,
    resolve_winner_loser,
)


def _pair(ck_a, scope_a, ck_b, scope_b, note):
    return {"kind": "merge_duplicate", "recommendation": "merge", "review_note": note,
            "rows": [{"product_key": "pk_a", "content_key": ck_a, "pdp_scope": scope_a},
                     {"product_key": "pk_b", "content_key": ck_b, "pdp_scope": scope_b}]}


def test_keep_note_picks_winner():
    w, l, note = resolve_winner_loser(_pair(
        "ck_aaa", "merchant_owned", "ck_bbb", "multi_merchant_canonical",
        "same product; KEEP ck_bbb (multi_merchant, 2 offers)"))
    assert w["content_key"] == "ck_bbb" and l["content_key"] == "ck_aaa"


def test_equal_scope_gift_pair_ok():
    w, l, note = resolve_winner_loser(_pair(
        "ck_aaa", "multi_merchant_canonical", "ck_bbb", "multi_merchant_canonical",
        "gift-label drift; KEEP ck_aaa"))
    assert w["content_key"] == "ck_aaa"


def test_refuses_to_retire_higher_scope():
    # note keeps the merchant_owned row over a multi_merchant_canonical sibling
    w, l, note = resolve_winner_loser(_pair(
        "ck_1010", "merchant_owned", "ck_beef", "multi_merchant_canonical",
        "KEEP ck_1010"))
    assert w is None and "lower scope" in note


def test_missing_keep_note_skips():
    w, l, note = resolve_winner_loser(_pair(
        "ck_aaa", "merchant_owned", "ck_bbb", "multi_merchant_canonical",
        "these look the same to me"))
    assert w is None and "KEEP" in note


def test_keep_matches_neither_row_skips():
    w, l, note = resolve_winner_loser(_pair(
        "ck_aaa", "merchant_owned", "ck_bbb", "multi_merchant_canonical",
        "KEEP ck_ffff"))
    assert w is None and "neither" in note


def test_wrong_row_count_skips():
    row = {"kind": "merge_duplicate", "recommendation": "merge",
           "review_note": "KEEP ck_aaa",
           "rows": [{"product_key": "pk_a", "content_key": "ck_aaa", "pdp_scope": "merchant_owned"}]}
    w, l, note = resolve_winner_loser(row)
    assert w is None and "2 rows" in note


def test_retailer_offer_rekeys_to_winner():
    off = {"offer_id": "offer:retailer:stylekorean_global:abc123", "merchant_id": "stylekorean_global"}
    tid = _target_offer_id(off, "prod::winner")
    assert tid.startswith("offer:retailer:stylekorean_global:") and tid != off["offer_id"]


def test_non_retailer_offer_keeps_id():
    off = {"offer_id": "offer:mirror:xyz", "merchant_id": "merch_obs_boj"}
    assert _target_offer_id(off, "prod::winner") == "offer:mirror:xyz"


import pytest  # noqa: E402


class _FakeDB:
    """Minimal fake for _classify_offers: winner + loser offer rows."""
    def __init__(self, winner_rows, loser_active):
        self._winner = winner_rows
        self._loser = loser_active

    async def fetch_all(self, sql, params=None):
        pk = params["pk"]
        if "suppressed_at IS NULL" in sql:   # _active_offers(loser)
            return [r for r in self._loser if r["product_key"] == pk]
        return [r for r in self._winner if r["product_key"] == pk]  # _winner_offers


@pytest.mark.asyncio
async def test_classify_reactivates_winner_suppressed_offer(monkeypatch):
    import scripts.merge_duplicate_canonicals as m
    # winner has a SUPPRESSED stylekorean_global offer; loser has an ACTIVE one.
    wpk, lpk = "prod::win", "prod::lose"
    win_tid = m._offer_id(wpk, "stylekorean_global")
    winner_rows = [{"offer_id": win_tid, "product_key": wpk, "merchant_id": "stylekorean_global",
                    "suppressed_at": "2026-01-01", "sku_key": wpk + "::canonical"}]
    loser_active = [{"offer_id": m._offer_id(lpk, "stylekorean_global"), "product_key": lpk,
                     "merchant_id": "stylekorean_global", "suppressed_at": None,
                     "sku_key": lpk + "::canonical", "list_price": 12.0}]
    monkeypatch.setattr(m, "database", _FakeDB(winner_rows, loser_active))
    out = await m._classify_offers(wpk, lpk)
    # winner's suppressed offer is reactivated; loser's is retired; nothing moved
    assert [r["offer_id"] for r in out["reactivate"]] == [win_tid]
    assert len(out["suppress"]) == 1 and len(out["move"]) == 0


@pytest.mark.asyncio
async def test_classify_moves_unique_seller(monkeypatch):
    import scripts.merge_duplicate_canonicals as m
    wpk, lpk = "prod::win", "prod::lose"
    # winner has NO offers; loser has an active retailer offer -> it moves
    loser_active = [{"offer_id": m._offer_id(lpk, "stylekorean_global"), "product_key": lpk,
                     "merchant_id": "stylekorean_global", "suppressed_at": None,
                     "sku_key": lpk + "::canonical", "list_price": 12.0}]
    monkeypatch.setattr(m, "database", _FakeDB([], loser_active))
    out = await m._classify_offers(wpk, lpk)
    assert len(out["move"]) == 1 and out["move"][0]["_target_offer_id"] == m._offer_id(wpk, "stylekorean_global")
    assert len(out["suppress"]) == 0 and len(out["reactivate"]) == 0
