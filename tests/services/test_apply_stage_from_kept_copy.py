"""An existing row's lifecycle stage is judged by the copy the upsert keeps on it.

`_PDP_UPSERT_SQL` never updates title or description, but it writes the plan's pdp_lifecycle_stage,
which ingestion computed from the CRAWLED body. Measured 2026-09-29: 279 rows filled from their own
product pages that day (koolseoul 125, tartecosmetics.com 33, ...) were one re-crawl away from
published -> draft, with nothing to restore them.
"""
from __future__ import annotations

import asyncio

from services.catalog_enrichment_agent.apply import _KEPT_COPY_SQL, _stage_from_kept_copy

REAL = ("A nourishing lip oil with a glossy finish and a comfortable, non-sticky texture that lasts "
        "for hours.")
THIN = "A'PIEU Honey Milk Lip Oil"


def _pdp(key="ext:apieu-honey-milk-lip-oil::1", *, description=THIN, stage="draft", image="https://c/i.jpg",
         category="beauty/makeup/lip/gloss", title=THIN):
    return {"product_key": key, "title": title, "description": description, "image_url": image,
            "category_path": category, "tags": '["Lip Oil"]', "demographic": None, "use_case_tags": "[]",
            "lifestyle_tags": "[]", "pdp_scope": "single_merchant", "source_system": "catalog_enrichment_agent_v1",
            "pdp_lifecycle_stage": stage}


class _DB:
    def __init__(self, stored):
        self.stored, self.calls = stored, []

    async def fetch_all(self, sql, values=None):
        self.calls.append((sql, values))
        return [r for r in self.stored if r["product_key"] in (values or {}).get("keys", [])]


def _run(pdps, stored):
    db = _DB(stored)
    plan, counts = asyncio.run(_stage_from_kept_copy({"pdps": pdps, "skus": ["kept"]}, db))
    return plan, counts, db


def test_a_thin_recrawl_of_a_row_with_real_copy_keeps_its_published_stage():
    plan, counts, _ = _run([_pdp()], [{"product_key": _pdp()["product_key"], "title": THIN, "description": REAL}])
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "published"
    assert counts == {"pdp_stage_from_kept_copy_planned": {"draft->published": 1}}
    assert plan["skus"] == ["kept"]  # the rest of the plan is untouched


def test_a_row_that_keeps_THIN_copy_is_not_published_on_the_crawls_word():
    """The other direction, for the same reason: the row will hold the stored thin copy."""
    pdp = _pdp(description=REAL, stage="published")
    plan, counts, _ = _run([pdp], [{"product_key": pdp["product_key"], "title": THIN, "description": THIN}])
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "draft"
    assert counts == {"pdp_stage_from_kept_copy_planned": {"published->draft": 1}}


def test_a_new_row_is_untouched():
    pdp = _pdp()
    plan, counts, db = _run([pdp], [])
    assert plan["pdps"][0] is pdp and counts == {}
    assert db.calls[0][0] is _KEPT_COPY_SQL


def test_a_row_whose_stored_copy_is_the_planned_copy_is_untouched():
    pdp = _pdp(description=REAL, stage="published")
    plan, counts, _ = _run([pdp], [{"product_key": pdp["product_key"], "title": THIN, "description": REAL}])
    assert plan["pdps"][0] is pdp and counts == {}


def test_the_ingests_category_rule_still_holds_whatever_the_copy():
    """A planned row whose category does not resolve stays draft (ingestion's early return)."""
    pdp = _pdp(category="beauty/not-a-real-path")
    plan, counts, _ = _run([pdp], [{"product_key": pdp["product_key"], "title": THIN, "description": REAL}])
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "draft" and counts == {}


def test_a_product_that_lost_its_image_still_drops():
    """Only title and description come from the stored row; everything else is the crawl's."""
    pdp = _pdp(image=None)
    plan, counts, _ = _run([pdp], [{"product_key": pdp["product_key"], "title": THIN, "description": REAL}])
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "draft" and counts == {}


def test_a_blank_stored_title_is_judged_as_the_row_will_hold_it():
    pdp = _pdp()
    plan, _, _ = _run([pdp], [{"product_key": pdp["product_key"], "title": "", "description": REAL}])
    assert plan["pdps"][0]["pdp_lifecycle_stage"] == "draft"


def test_an_empty_plan_does_no_lookup():
    plan, counts, db = _run([], [])
    assert counts == {} and db.calls == []


def test_a_new_row_beside_an_existing_one_keeps_its_planned_stage():
    """Review #2438: the lookup finds the existing row; the NEW row in the same plan has no stored copy and
    must keep the stage the crawl earned (not be judged against an empty stored copy)."""
    existing = _pdp()
    new = _pdp("ext:apieu-honey-milk-lip-oil::2", description=REAL, stage="published")
    plan, counts, _ = _run([existing, new], [{"product_key": existing["product_key"], "title": THIN, "description": REAL}])
    assert [p["pdp_lifecycle_stage"] for p in plan["pdps"]] == ["published", "published"]
    assert plan["pdps"][1] is new
    assert counts == {"pdp_stage_from_kept_copy_planned": {"draft->published": 1}}


def test_a_suppressed_row_is_not_looked_up():
    from services.catalog_enrichment_agent.apply import _KEPT_COPY_SQL
    assert "suppressed_at IS NULL" in _KEPT_COPY_SQL
