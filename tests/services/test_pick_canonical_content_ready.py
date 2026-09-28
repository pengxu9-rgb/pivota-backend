"""The served PDP comes from a row the serving gate would accept, when the content_key has one.

Measured 2026-09-28 on prod (backend fbd2b9cec): 348 content_keys were blocked short_description, and
38 of them held a sibling row with real copy. The lowest product_key -- koolseoul.com's listing, whose
Shopify body_html is the product title plus three images -- supplied the served description (41
chars), so index_pipeline_state blocked the whole key and hid dodoskin.com's and coscorea.com's offers
beside their 1,475- and 1,117-char descriptions. 135 serving keys took their title, image, id and
URL from a SUPPRESSED row -- the Stila/Tarte/Tower 28 old-spelling rows the stale-brand retire had
just withdrawn, arencia's JP seeds. The rows below are shaped like those.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.agent_pdp_view_assembler as assembler
from services.agent_pdp_view_assembler import fetch_products_for_key, pick_canonical

TITLE_ONLY = "MISSHA Artemisia Pack Foam Cleanser 150ml"  # koolseoul's body_html text, 41 chars
REAL_COPY = ("A gentle foam cleanser infused with artemisia to calm, hydrate and purify; its creamy "
             "lather lifts impurities while soothing sensitive skin.")
SIG = "sig_" + "a" * 32
WITHDRAWN = datetime(2026, 9, 28, 0, 29, tzinfo=timezone.utc)


def _row(product_key, *, host, description=REAL_COPY, image="https://cdn.example/i.jpg", sig=SIG,
         primary=True, sync_status="live", suppressed_at=None, platform="external_seed", brand="MISSHA"):
    return {"product_key": product_key, "source_domain": host, "brand": brand, "description": description,
            "image_url": image, "pivota_signature_id": sig, "group_is_primary": primary,
            "sync_status": sync_status, "suppressed_at": suppressed_at, "platform": platform,
            "canonical_url": f"https://{host}/products/x", "has_brand_direct_offer": False}


KOOLSEOUL = _row("ext:retailer:07fab0742666118e7d43dcdc8f487da7", host="koolseoul.com", description=TITLE_ONLY)
DODOSKIN = _row("ext:retailer:8e1f0c2a9b3d4e5f60718293a4b5c6d7", host="dodoskin.com")


def test_a_thin_lowest_key_loses_to_a_sibling_with_real_copy():
    assert KOOLSEOUL["product_key"] < DODOSKIN["product_key"]  # the old ladder's tiebreak
    assert pick_canonical([KOOLSEOUL, DODOSKIN]) is DODOSKIN
    assert pick_canonical([DODOSKIN, KOOLSEOUL]) is DODOSKIN


def test_a_winner_that_already_passes_is_never_replaced():
    """Only a content_key whose current winner fails the bar can change: longer copy wins nothing."""
    longer = _row("ext:retailer:ffff", host="coscorea.com", description=REAL_COPY * 10)
    assert pick_canonical([longer, DODOSKIN]) is DODOSKIN


def test_a_suppressed_row_loses_to_the_live_row_with_the_same_copy():
    """Tower 28: the retired old-spelling row sorts first and carries identical copy."""
    old = _row("ext:retailer:1111", host="tower28beauty.com", suppressed_at=WITHDRAWN)
    new = _row("ext:retailer:2222", host="www.tower28beauty.com")
    assert pick_canonical([old, new]) is new


@pytest.mark.parametrize("status,passes", [
    ("live", True), ("stale", False), ("archived", False),
    # the gate refuses only a NON-EMPTY status other than 'live' (index_pipeline_state_service)
    ("", True), (None, True),
])
def test_sync_status_is_read_the_way_the_gate_reads_it(status, passes):
    row = _row("ext:retailer:0000", host="a.example", sync_status=status)
    other = _row("ext:retailer:9999", host="b.example")
    assert (pick_canonical([row, other]) is row) is passes


@pytest.mark.parametrize("description,passes", [
    ("x" * 50, True),                 # MIN_DESCRIPTION_LENGTH
    ("x" * 49, False),
    ("   " + "x" * 49 + "   ", False),  # measured stripped, like the served description
])
def test_the_description_bar_is_the_gates_minimum_measured_stripped(description, passes):
    row = _row("ext:retailer:0000", host="a.example", description=description)
    other = _row("ext:retailer:9999", host="b.example")
    assert (pick_canonical([row, other]) is row) is passes


@pytest.mark.parametrize("sibling", [
    _row("ext:retailer:9999", host="b.example", image=None),
    _row("ext:retailer:9999", host="b.example", image="  "),
    _row("ext:retailer:9999", host="b.example", sig=None),
    _row("ext:retailer:9999", host="b.example", sync_status="stale"),
    _row("ext:retailer:9999", host="b.example", suppressed_at=WITHDRAWN),
])
def test_a_sibling_that_would_itself_be_refused_does_not_win(sibling):
    assert pick_canonical([KOOLSEOUL, sibling]) is KOOLSEOUL


def test_when_no_row_passes_the_old_ladder_decides():
    a = _row("ext:retailer:0000", host="a.example", description=TITLE_ONLY, primary=False)
    b = _row("ext:retailer:1111", host="b.example", description=TITLE_ONLY)
    c = _row("ext:retailer:2222", host="c.example", description=TITLE_ONLY, sig=None)
    assert pick_canonical([a, b, c]) is b  # primary, then signature, then lowest key


def test_a_caller_that_loads_none_of_the_fields_keeps_todays_order():
    """identity_resolution_strategies / identity_reconcile_sweep pass rows without description or image."""
    rows = [{"product_key": "k2", "pivota_signature_id": SIG, "platform": "shopify"},
            {"product_key": "k1", "pivota_signature_id": SIG, "platform": "shopify"},
            {"product_key": "k0", "pivota_signature_id": None, "platform": "shopify"}]
    assert pick_canonical(rows)["product_key"] == "k1"


def test_an_audit_seed_still_sorts_last_even_with_real_copy():
    audit = _row("ext:retailer:0000", host="a.example", platform="url_audit")
    assert pick_canonical([audit, KOOLSEOUL]) is KOOLSEOUL


def test_a_suppressed_brand_store_row_is_not_the_brand_store():
    """The brand rule reads the same bar: a withdrawn brand row never outranks a live retailer's copy."""
    brand = _row("ext:missha-artemisia::1", host="misshaus.com", suppressed_at=WITHDRAWN)
    brand["has_brand_direct_offer"] = True
    assert pick_canonical([brand, DODOSKIN]) is DODOSKIN
    brand["suppressed_at"] = None
    assert pick_canonical([brand, DODOSKIN]) is brand


def test_both_rules_use_one_definition_of_the_bar(monkeypatch):
    """Review ask: brand_rank and content_rank must not carry two copies of the bar that can drift."""
    brand = _row("ext:missha-artemisia::1", host="misshaus.com")
    brand["has_brand_direct_offer"] = True
    assert assembler._is_brand_store_row(brand)
    monkeypatch.setattr(assembler, "_passes_serving_content_bar", lambda row: False)
    assert not assembler._is_brand_store_row(brand)
    assert pick_canonical([DODOSKIN, KOOLSEOUL]) is KOOLSEOUL  # the old ladder once nothing passes


class _FakeDB:
    sql = None

    async def fetch_all(self, sql, params=None):
        self.sql = sql
        return []


@pytest.mark.asyncio
async def test_the_loader_selects_the_fields_the_bar_reads():
    db = _FakeDB()
    await fetch_products_for_key("ck_x", db=db)
    for column in ("cp.description", "cp.image_url", "cp.pivota_signature_id", "cp.sync_status",
                   "cp.suppressed_at"):
        assert column in db.sql
