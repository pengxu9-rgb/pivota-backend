"""The backfill script's own behaviour, which had no test at all.

Its two headline safety properties -- the PDP fetch is STRICTLY a fallback, and it is OPT-IN --
were prose in a docstring, and mutation proved it: making the fetch unconditional, or ignoring
`--pdp-fallback` entirely, left the whole 15,520-test suite green. Those are exactly the
properties that decide whether a routine re-run quietly starts crawling merchant hosts and
preferring meta copy over real body copy, so they are asserted here directly.
"""

from __future__ import annotations

import asyncio

import pytest

import scripts.backfill_brand_official_descriptions as bf


def _spy_fetch():
    """A fetch that RECORDS every call, so 'no request happened' is an assertion and not a hope."""
    calls = []

    async def _fetch(domain, handle):
        calls.append((domain, handle))
        return "A genuinely long brand-authored product description taken from the PDP itself."

    return _fetch, calls


def test_usable_body_copy_wins_and_costs_no_request():
    """STRICTLY A FALLBACK. A row whose body_html clears the floor keeps it and issues NO merchant
    request -- byte-identical to before the fallback existed. A fetch fired for a row that does
    not need one is real outbound traffic and a behaviour change.

    An earlier version of this test asserted `calls == []` without ever invoking the code, which
    is an assertion that cannot fail. This drives the real decision function.
    """
    fetch, calls = _spy_fetch()
    body = "Real brand body copy that is comfortably over the fifty character floor."

    out, from_pdp = asyncio.run(
        bf.resolve_description(body, "jsmbeauty.sg", "h", pdp_fallback=True, fetch=fetch)
    )

    assert out == body
    assert from_pdp is False
    assert calls == [], "a row with usable body copy must not touch the merchant"


def test_the_fallback_is_opt_in_and_silent_when_off():
    """OPT-IN. Without the flag an empty-body row stays unfilled and issues no request. A mutant
    ignoring the flag left the entire 15,520-test suite green."""
    fetch, calls = _spy_fetch()

    out, from_pdp = asyncio.run(
        bf.resolve_description("tiny", "jsmbeauty.sg", "h", pdp_fallback=False, fetch=fetch)
    )

    assert out is None and from_pdp is False
    assert calls == [], "the fallback must not crawl unless asked"


def test_the_fallback_fills_and_reports_its_provenance_when_on():
    fetch, calls = _spy_fetch()

    out, from_pdp = asyncio.run(
        bf.resolve_description("tiny", "jsmbeauty.sg", "h", pdp_fallback=True, fetch=fetch)
    )

    assert out and len(out) >= bf.MIN_DESC_LEN
    assert from_pdp is True, "the caller needs this to record the distinct provenance"
    assert calls == [("jsmbeauty.sg", "h")]


def test_the_same_floor_applies_to_what_the_pdp_returns():
    """A 9-character meta description is how these rows got blocked in the first place; accepting
    any non-empty string would re-admit exactly that."""
    async def _short(domain, handle):
        return "Lip Tint"

    out, from_pdp = asyncio.run(
        bf.resolve_description("tiny", "jsmbeauty.sg", "h", pdp_fallback=True, fetch=_short)
    )

    assert out is None and from_pdp is False


def test_a_pdp_that_returns_nothing_leaves_the_row_unfilled():
    async def _none(domain, handle):
        return None

    out, from_pdp = asyncio.run(
        bf.resolve_description("tiny", "jsmbeauty.sg", "h", pdp_fallback=True, fetch=_none)
    )

    assert out is None and from_pdp is False


def test_pdp_filled_rows_carry_a_DISTINCT_provenance_marker():
    """Before the fallback, `description` on this lane had exactly one possible origin. It now has
    two, and ADR-001 asks for provenance tagging on each canonical field -- so a later reader must
    be able to tell which source filled a row, both to audit meta copy and to find it again if the
    meta-description route is ever judged a lower tier than body copy."""
    assert bf.REFRESH_SOURCE_PDP_META != bf.REFRESH_SOURCE
    assert "pdp_meta" in bf.REFRESH_SOURCE_PDP_META


# ---------------------------------------------------------------------------
# The whole-domain boilerplate guard.
# ---------------------------------------------------------------------------

_BLURB = ("Discover JUNGSAEMMOOL, the epitome of Korean makeup and cosmetic products, "
          "blending artistry with skincare for every day.")
_BOGOS = ("This product is used for the app BOGOS.io Free Gift BOGO Bundle to work. "
          "Please do not delete/edit it, or email us at: help@bogos.io.")


def test_a_value_equal_to_the_shops_own_blurb_is_dropped():
    """MECHANISM 1. A theme renders og:description as `page_description | default:
    shop.description`, so a product with no SEO description serves the STORE's blurb: over the
    floor, zero product information, and identical across the storefront.

    The earlier guard tried to spot this from one page's markup (a present-but-empty name tag)
    and so fired only on the storefront it was derived from; a 60-PDP sweep found the blurb
    served 9 times under an ABSENT name tag, which that rule waved through. Comparing against the
    blurb ITSELF is the mechanism rather than a symptom of it.
    """
    kept = bf.drop_shared_boilerplate({"pk1": _BLURB}, _BLURB)
    assert kept == {}, "the shop's own blurb must never become a product description"


def test_the_shop_blurb_is_matched_despite_case_and_whitespace():
    """The homepage and the PDP render the same string through different templates; a newline or
    a capital must not be the difference between dropping and publishing it."""
    on_pdp = "  DISCOVER JUNGSAEMMOOL, the epitome of Korean\n  makeup and cosmetic products, " \
             "blending artistry with skincare for every day.  "
    assert bf.drop_shared_boilerplate({"pk1": on_pdp}, _BLURB) == {}


def test_a_value_repeated_across_products_is_dropped_even_with_no_shop_blurb():
    """MECHANISM 2, which no shop-blurb comparison can reach. App vendors write operational text
    into the name tag -- 231 characters of BOGOS.io's "do not delete/edit it" on 5 of 5 sampled
    jsmbeauty.sg products. It is nobody's shop blurb, and the ONLY thing wrong with it is that it
    is not about any one product. Repetition is the tell.
    """
    kept = bf.drop_shared_boilerplate({"pk1": _BOGOS, "pk2": _BOGOS, "pk3": _BOGOS}, None)
    assert kept == {}


def test_real_per_product_copy_survives_alongside_the_boilerplate():
    """The guard must not be a blanket refusal -- the whole point of the lane is to fill rows."""
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    kept = bf.drop_shared_boilerplate(
        {"pk1": _BOGOS, "pk2": real, "pk3": _BOGOS}, _BLURB
    )
    assert kept == {"pk2": real}


def test_a_lone_candidate_is_kept_when_it_is_not_the_shop_blurb():
    """A single candidate carries NO repetition signal. Dropping it would make the guard depend on
    batch size -- the same row kept in a 2-row run and dropped in a 1-row run."""
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    assert bf.drop_shared_boilerplate({"pk1": real}, _BLURB) == {"pk1": real}


def test_an_unavailable_shop_blurb_leaves_the_repetition_guard_armed():
    """fetch_shop_description returns None on any failure. That must disarm only the comparison it
    feeds, never the repetition rule beside it."""
    assert bf.drop_shared_boilerplate({"a": _BOGOS, "b": _BOGOS}, None) == {}
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    assert bf.drop_shared_boilerplate({"a": real}, None) == {"a": real}


# ---------------------------------------------------------------------------
# The delivery path. resolve_description is only a decision; run() is what writes.
# ---------------------------------------------------------------------------

class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    async def fetch_all(self, *a, **k):
        return list(self._rows)

    async def execute(self, sql, values=None):
        self.updates.append(values)

    async def disconnect(self):
        pass


def _row(pk, ck, handle):
    return {
        "product_key": pk, "content_key": ck,
        "canonical_url": f"https://jsmbeauty.sg/products/{handle}",
        "source_domain": "jsmbeauty.sg", "title": "t", "image_url": "i",
        "description": "tiny", "category_path": None, "tags": None, "demographic": None,
        "use_case_tags": None, "lifestyle_tags": None, "pdp_scope": None,
        "source_system": "catalog_enrichment_agent_v1", "pdp_lifecycle_stage": "draft",
    }


def _harness(monkeypatch, rows, *, body_map, pdp=None, blurb=None):
    """Drive run() against stubs, recording what each content_key was refreshed AS."""
    db = _FakeDB(rows)
    refreshed = {}
    fetched = []

    async def _noop():
        pass

    async def _load(domain, max_products):
        return dict(body_map)

    async def _pdp(domain, handle, **k):
        fetched.append((domain, handle))
        return (pdp or {}).get(handle)

    async def _blurb(domain, **k):
        return blurb

    async def _refresh(ck, refresh_source=None, **k):
        refreshed[ck] = refresh_source

    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "_reconnect", _noop)
    monkeypatch.setattr(bf, "_load_body_map", _load)
    monkeypatch.setattr(bf, "fetch_pdp_description", _pdp)
    monkeypatch.setattr(bf, "fetch_shop_description", _blurb)
    monkeypatch.setattr(bf, "refresh_agent_pdp_view_for_content_key", _refresh)
    monkeypatch.setattr(bf, "compute_lifecycle_stage", lambda row: "published")
    return db, refreshed, fetched


_LONG_BODY = "Real brand body copy that is comfortably over the fifty character floor here."
_LONG_META = "A watery lip tint that layers into a vivid stain without drying the lips out."


@pytest.mark.parametrize("order", [["body", "pdp"], ["pdp", "body"]])
def test_the_refresh_source_follows_EACH_content_key_not_the_last_row_processed(monkeypatch, order):
    """THE PROVENANCE MARKER, asserted on the path that writes it.

    `refresh_source` is persisted to agent_pdp_view.refresh_source, and an earlier version chose
    it with `r.get("_from_pdp")` inside the REFRESH loop -- where `r` is a leaked variable from
    the fill loop above, holding the last row of the last domain. Every key got one global
    coin-flip, wrong in both orderings; `touched_cks` was a flat list of strings, so per-key
    provenance was not even representable. A test comparing the two module CONSTANTS (below)
    passes happily against that, which is why this drives run() instead.

    Parametrised on order precisely because the leak's value depended on it.
    """
    handles = {"body": "h-body", "pdp": "h-pdp"}
    rows = [_row(f"pk-{k}", f"ck-{k}", handles[k]) for k in order]
    _, refreshed, _ = _harness(
        monkeypatch, rows,
        body_map={"h-body": _LONG_BODY, "h-pdp": "tiny"},
        pdp={"h-pdp": _LONG_META},
    )

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    assert refreshed["ck-body"] == bf.REFRESH_SOURCE, "body copy must not be marked as PDP meta"
    assert refreshed["ck-pdp"] == bf.REFRESH_SOURCE_PDP_META, "PDP meta copy must say so"


def test_the_call_site_honours_the_opt_in_not_just_the_helper(monkeypatch):
    """resolve_description's own opt-in test cannot see a call site that passes pdp_fallback=True
    unconditionally -- a mutant doing exactly that survived the whole suite."""
    rows = [_row("pk1", "ck1", "h1")]
    _, refreshed, fetched = _harness(
        monkeypatch, rows, body_map={"h1": "tiny"}, pdp={"h1": _LONG_META})

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=False)) == 0

    assert fetched == [], "run() must not crawl merchant hosts without --pdp-fallback"
    assert refreshed == {}, "nothing should have been filled"


def test_a_storefront_wide_blurb_never_reaches_the_database(monkeypatch):
    """END TO END, the defect this whole change exists to close: the shop blurb clears the 50-char
    floor, and `is_published_ready` auto-publishes this lane -- so an accepted blurb enters serving
    as brand-official PRODUCT copy. It must not survive as far as an UPDATE."""
    rows = [_row("pk1", "ck1", "h1"), _row("pk2", "ck2", "h2")]
    db, refreshed, _ = _harness(
        monkeypatch, rows,
        body_map={"h1": "tiny", "h2": "tiny"},
        pdp={"h1": _BLURB, "h2": _BLURB},
        blurb=_BLURB,
    )

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    assert db.updates == [], "the shop blurb was written as a product description"
    assert refreshed == {}
    written = " ".join(str(u) for u in db.updates)
    assert "JUNGSAEMMOOL" not in written
