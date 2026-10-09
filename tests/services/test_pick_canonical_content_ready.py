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

import asyncio
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
    return {"product_key": product_key, "source_domain": host, "brand": brand, "title": TITLE_ONLY,
            "description": description,
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


def test_a_failing_primary_row_loses_to_a_passing_non_primary_one():
    """Rung 0c sits ABOVE is_primary and the signature rung: the gate blocks the key whatever the group says."""
    primary_thin = _row("ext:retailer:0000", host="a.example", description=TITLE_ONLY, primary=True)
    other = _row("ext:retailer:9999", host="b.example", primary=False)
    assert pick_canonical([primary_thin, other]) is other


def test_a_row_without_a_title_is_not_content_ready():
    """assemble_row builds nothing without a title, so such a winner would leave the key unbuilt."""
    titled = _row("ext:retailer:9999", host="b.example")
    for blank in ("", "   ", None):
        untitled = dict(titled, product_key="ext:retailer:0000", title=blank)
        assert pick_canonical([untitled, titled]) is titled


# -- what the row would SERVE: its overlay, then its own column (review #2423) ------------------------

OVERLAY_COPY = "Pivota enrichment copy. " * 30  # an executor overlay, 720 chars


def _titled(pk, host, **kw):
    r = _row(pk, host=host, **kw)
    r["title"] = "MISSHA Artemisia Pack Foam Cleanser 150ml"
    r.update(merchant_id=f"m-{host}", platform="shopify", source_product_id=f"sp-{pk[-4:]}")
    return r


def _overlays(*pairs):
    return {(r["merchant_id"], r["platform"], r["source_product_id"]): o for r, o in pairs}


def _thin_and_sixty():
    thin = _titled("ext:retailer:0000", "a.example", description="Brand serum 30ml.")  # 17 chars raw
    sixty = _titled("ext:retailer:9999", "b.example", description="x" * 60)
    return thin, sixty


def test_a_thin_row_whose_overlay_serves_real_copy_keeps_the_pick():
    thin, sixty = _thin_and_sixty()
    rows = [thin, sixty]
    assembler.annotate_served_copy(rows, _overlays((thin, {"description_markdown": OVERLAY_COPY})), False)
    assert pick_canonical(rows) is thin


def test_without_its_overlay_the_same_thin_row_loses():
    thin, sixty = _thin_and_sixty()
    rows = [thin, sixty]
    assembler.annotate_served_copy(rows, {}, False)
    assert pick_canonical(rows) is sixty


def test_a_brand_attested_overlay_serves_whichever_row_wins_so_it_reorders_nothing():
    thin, sixty = _thin_and_sixty()
    rows = [thin, sixty]
    attested = {"description_markdown": OVERLAY_COPY, "updated_by_employee_id": "brand_attestation"}
    assembler.annotate_served_copy(rows, _overlays((sixty, attested)), False)
    assert pick_canonical(rows) is thin  # both serve the attested copy: the old ladder decides


def test_the_attested_overlay_outranks_a_rows_own_overlay_as_assemble_row_does():
    """A THIN attested overlay serves on every row -- a row's own long overlay never reaches the PDP."""
    first, second = _thin_and_sixty()
    second["description"] = "Brand serum 30ml."
    rows = [first, second]
    thin_attested = {"description_markdown": "Brand copy.", "updated_by_employee_id": "brand_attestation"}
    assembler.annotate_served_copy(
        rows, _overlays((first, thin_attested), (second, {"description_markdown": OVERLAY_COPY})), False)
    assert pick_canonical(rows) is first  # nobody passes: the old ladder, not the own overlay


def test_a_title_override_counts_as_the_title():
    thin, sixty = _thin_and_sixty()
    thin["title"] = ""
    thin["description"] = "x" * 60
    rows = [thin, sixty]
    assembler.annotate_served_copy(rows, _overlays((thin, {"title_override": "MISSHA Artemisia"})), False)
    assert pick_canonical(rows) is thin
    assembler.annotate_served_copy(rows, {}, False)
    assert pick_canonical(rows) is sixty


def test_the_rows_own_description_outranks_nothing_but_an_overlay():
    """The overlay first, then the row's column -- never the other way round."""
    thin, sixty = _thin_and_sixty()
    thin["description"] = "x" * 200
    rows = [thin, sixty]
    assembler.annotate_served_copy(rows, _overlays((thin, {"description_markdown": "Short overlay."})), False)
    assert pick_canonical(rows) is sixty  # the overlay is what serves, and it is thin


def test_the_seed_never_decides_the_pick():
    """Which seed a rebuild holds depends on its caller; a pick that read it would flip the served
    signature between two refreshes of unchanged rows (review #2423)."""
    empty = _titled("ext:retailer:0000", "a.example", description="")
    sixty = _titled("ext:retailer:9999", "b.example", description="x" * 60)
    rows = [empty, sixty]
    assembler.annotate_served_copy(rows, {}, False)
    assert pick_canonical(rows) is sixty
    import inspect
    assert "seed" not in inspect.signature(assembler.annotate_served_copy).parameters
    assert "seed" not in inspect.signature(assembler.load_served_copy).parameters


def test_a_failed_overlay_read_picks_what_a_read_with_no_overlays_picks():
    """Not the old ladder: the rebuild preserves the published copy on a failed read, and a winner
    that differed from the successful read's would write another row's signature under it."""
    thin, sixty = _thin_and_sixty()
    rows = [thin, sixty]
    assembler.annotate_served_copy(rows, {}, False)
    no_overlays = pick_canonical(rows)
    partial = _overlays((thin, {"description_markdown": OVERLAY_COPY}))  # read before the failure
    assembler.annotate_served_copy(rows, partial, True)
    assert pick_canonical(rows) is no_overlays is sixty
    # ...while the read itself still rides on the rows for the enrichment pick, which says it failed
    assert asyncio.run(assembler._fetch_enrichment_for_canonical(rows)) is assembler.FETCH_FAILED


def test_the_image_is_the_rows_own():
    no_image = _titled("ext:retailer:0000", "a.example", image=None)
    other = _titled("ext:retailer:9999", "b.example")
    rows = [no_image, other]
    assembler.annotate_served_copy(rows, {}, True)
    assert pick_canonical(rows) is other


def test_the_enrichment_pick_reuses_the_overlays_it_is_handed(monkeypatch):
    import db.product_enrichment

    async def _no_read(*a, **kw):
        raise AssertionError("overlays were already read")

    monkeypatch.setattr(db.product_enrichment, "get_enrichments_for_products", _no_read)
    thin = _titled("ext:retailer:0000", "a.example", description="Brand serum 30ml.")
    overlay = {"description_markdown": OVERLAY_COPY}
    assembler.annotate_served_copy([thin], _overlays((thin, overlay)), False)
    assert asyncio.run(assembler._fetch_enrichment_for_canonical([thin])) is overlay
    assembler.annotate_served_copy([thin], {}, True)  # the read failed: say so, as before
    assert asyncio.run(assembler._fetch_enrichment_for_canonical([thin])) is assembler.FETCH_FAILED


def test_the_rebuild_serves_the_overlay_row_and_reads_overlays_once(monkeypatch):
    """End to end through refresh_agent_pdp_view_for_content_key: every pick in the rebuild (the
    evidence scope, assemble_row, the enrichment pick) sees the annotated rows."""
    import db.product_enrichment

    thin = _titled("ext:retailer:0000", "a.example", description="Brand serum 30ml.")
    sixty = _titled("ext:retailer:9999", "b.example", description="y" * 60)
    reads = []

    async def _bulk(merchant_id, *, product_keys=None, geo_code="default"):
        reads.append(merchant_id)
        if merchant_id == thin["merchant_id"]:
            return {(thin["platform"], thin["source_product_id"]): {"description_markdown": OVERLAY_COPY}}
        return {}

    async def _rows(*a, **kw):
        return [dict(thin), dict(sixty)]

    async def _empty(*a, **kw):
        return []

    async def _none(*a, **kw):
        return None

    async def _no_evidence(*a, **kw):
        return {}

    monkeypatch.setattr(db.product_enrichment, "get_enrichments_for_products", _bulk)
    monkeypatch.setattr(assembler, "fetch_products_for_key", _rows)
    monkeypatch.setattr(assembler, "fetch_skus_for_keys", _empty)
    monkeypatch.setattr(assembler, "fetch_offers_for_keys", _empty)
    monkeypatch.setattr(assembler, "fetch_external_seed_for_keys", _none)
    monkeypatch.setattr(assembler, "fetch_evidence_for_keys", _no_evidence)

    class _DB:
        params = None

        async def execute(self, sql, params):
            self.params = params

        async def fetch_one(self, sql, params):
            return None

    db = _DB()
    assert asyncio.run(assembler.refresh_agent_pdp_view_for_content_key("ck_x", refresh_source="t", db=db))
    assert db.params["description"].startswith("Pivota enrichment copy.")
    assert sorted(reads) == sorted({thin["merchant_id"], sixty["merchant_id"]})  # once per merchant


def test_the_identity_keepers_load_what_the_bar_reads():
    """same_url_dup / junk_url / the tier-3 judge pick their keeper with pick_canonical and are
    auto-approved: a keeper the bar cannot see would suppress the row that serving picked."""
    from scripts.step5_lane2_same_url_dedup import DETAIL_SQL
    from services.identity_reconcile_sweep import JUDGE_ROWS_SQL

    for sql in (DETAIL_SQL, JUDGE_ROWS_SQL):
        for column in ("title", "description", "image_url", "sync_status", "suppressed_at", "pivota_signature_id"):
            assert column in sql


def test_the_dedup_sweep_never_auto_suppresses_the_served_row():
    """The keeper reads raw columns; serving reads overlays. Where they differ, an unreviewed apply
    would suppress the row being served -- so that proposal is held for review (review #2423)."""
    from services.identity_reconcile_sweep import (
        APPROVE_ALLOWLIST_SQL, HELD_AUTO_APPROVE_SQL, REVIEW_HELD_SQL, _A_LOSER_IS_SERVED)

    assert "cp.pivota_signature_id = av.pivota_signature_id" in _A_LOSER_IS_SERVED
    assert "cp.product_key = ANY(p.subject_product_keys)" in _A_LOSER_IS_SERVED
    assert "cp.product_key <> p.keeper_product_key" in _A_LOSER_IS_SERVED
    assert "AND NOT EXISTS (" + _A_LOSER_IS_SERVED + ")" in APPROVE_ALLOWLIST_SQL
    for sql in (HELD_AUTO_APPROVE_SQL, REVIEW_HELD_SQL):
        assert "AND EXISTS (" + _A_LOSER_IS_SERVED + ")" in sql


def test_a_held_proposal_is_counted_warned_and_sent_to_review(monkeypatch, caplog):
    import json
    import logging

    import services.identity_reconcile_sweep as sweep

    held = {"proposal_id": "irp_held", "kind": "suppress_dup", "strategy": "same_url_dup", "merchant_id": "m",
            "content_key": "ck_x", "subject_product_keys": ["a", "b"], "keeper_product_key": "a",
            "confidence": 0.99, "evidence": "{}"}

    class _Conn:
        summary = None

        async def fetchval(self, sql, *a):
            return 0

        async def fetch(self, sql, *a):
            return [held] if sql is sweep.REVIEW_HELD_SQL else []

        async def fetchrow(self, sql, *a):
            if sql is sweep.HELD_AUTO_APPROVE_SQL:
                return {"n": 1}
            if sql is sweep.ENQUEUE_REVIEW_TASK_SQL:
                return {"id": a[0]}
            return None

        async def execute(self, sql, *a):
            if sql is sweep.INSERT_EVENT_SQL:
                _Conn.summary = json.loads(a[3])

        async def close(self):
            pass

    async def _connect(*a, **kw):
        return _Conn()

    monkeypatch.setattr(sweep, "_connect_with_retry", _connect)
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.delenv("ENABLE_TIER3_JUDGE", raising=False)
    with caplog.at_level(logging.WARNING, logger="identity_reconcile_sweep"):
        out = asyncio.run(sweep.run_identity_reconcile_sweep_tick(force=True))
    assert out["auto_approve_held_loser_served"] == 1
    assert _Conn.summary["auto_approve_held_loser_served"] == 1
    assert out["review_tasks_enqueued"] == ["pdptask_ir_irp_held"]
    assert any("held for review" in r.getMessage() for r in caplog.records)


def test_a_partial_overlay_read_still_hands_the_enrichment_pick_what_it_read():
    """A failed read judges the PICK as if no overlay existed, but the overlays that were read still
    reach _fetch_enrichment_for_canonical, which serves the canonical's own as before."""
    passing = _titled("ext:retailer:0000", "a.example", description="x" * 80)
    other = _titled("ext:retailer:9999", "b.example", description="y" * 80)
    rows = [passing, other]
    overlay = {"description_markdown": OVERLAY_COPY}
    assembler.annotate_served_copy(rows, _overlays((passing, overlay)), True)
    assert pick_canonical(rows) is passing
    assert asyncio.run(assembler._fetch_enrichment_for_canonical(rows)) is overlay


def test_the_scripts_that_pick_the_served_winner_annotate_first(monkeypatch):
    """repair_external_seed_offer_mainline and author_decision_intelligence must pick the winner the
    rebuild serves, so they annotate the rows before any pick."""
    import scripts.author_decision_intelligence as adi
    import scripts.repair_external_seed_offer_mainline as repair

    thin, sixty = _thin_and_sixty()
    seen = []

    async def _rows(*a, **kw):
        return [dict(thin), dict(sixty)]

    async def _empty(*a, **kw):
        return []

    async def _none(*a, **kw):
        return None

    async def _annotate(products):
        assembler.annotate_served_copy(products, _overlays((thin, {"description_markdown": OVERLAY_COPY})), False)

    def _capture_assemble(**kw):
        seen.append(pick_canonical(kw["products"])["product_key"])
        return None

    monkeypatch.setattr(repair, "fetch_products_for_key", _rows)
    monkeypatch.setattr(repair, "fetch_skus_for_keys", _empty)
    monkeypatch.setattr(repair, "fetch_offers_for_keys", _empty)
    monkeypatch.setattr(repair, "fetch_external_seed_for_keys", _none)
    monkeypatch.setattr(repair, "load_served_copy", _annotate)
    monkeypatch.setattr(repair, "assemble_row", _capture_assemble)
    asyncio.run(repair._build_apv_offer_field_update("ck_x", db=object()))
    assert seen == [thin["product_key"]]  # the overlay row, as the rebuild serves it

    class _DB:
        async def fetch_one(self, *a, **kw):
            raise _Stop()

    class _Stop(Exception):
        pass

    def _capture_pick(products):
        seen.append(pick_canonical(products)["product_key"])
        raise _Stop()

    monkeypatch.setattr(adi, "fetch_products_for_key", _rows)
    monkeypatch.setattr(adi, "load_served_copy", _annotate)
    monkeypatch.setattr(adi, "pick_canonical", _capture_pick)
    with pytest.raises(_Stop):
        asyncio.run(adi._prepare("ck_x"))
    assert seen[-1] == thin["product_key"]
