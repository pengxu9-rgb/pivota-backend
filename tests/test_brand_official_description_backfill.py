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
    """A single candidate carries no repetition signal, so mechanism 1 alone decides it -- and
    that is why the blurb fetch is worth its one request per domain.

    The earlier rationale here ("dropping it would make the guard depend on batch size") is now
    contradicted by the code: when the blurb is UNAVAILABLE a singleton IS dropped. This case
    passes only because a real blurb is supplied, which is exactly the distinction being pinned.
    """
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    assert bf.drop_shared_boilerplate({"pk1": real}, _BLURB) == {"pk1": real}


def test_an_unavailable_shop_blurb_still_drops_a_REPEATED_value():
    """fetch_shop_description returns None on any failure. Mechanism 2 does not depend on it, so
    repetition keeps working -- but a SINGLETON has nothing left checking it (next test)."""
    assert bf.drop_shared_boilerplate({"a": _BOGOS, "b": _BOGOS}, None) == {}


def test_an_unavailable_shop_blurb_REFUSES_an_unrepeated_value():
    """FAIL CLOSED. With no blurb to compare against, a value with no sibling is simply
    uncheckable -- and this lane auto-publishes, so 'uncheckable' must mean refused. An earlier
    version kept it, which let a homepage 403 silently re-open the hole."""
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    assert bf.drop_shared_boilerplate({"a": real}, None) == {}
    assert bf.drop_shared_boilerplate({"a": real}, _BLURB) == {"a": real}


def test_one_product_behind_TWO_rows_is_not_mistaken_for_a_shared_string():
    """The repetition unit is the PRODUCT PAGE, not the row. Two catalog rows can share one
    canonical_url; they fetch the same PDP and get the same value, and counting rows would drop
    both as 'shared' when it is one product represented twice."""
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    kept = bf.drop_shared_boilerplate(
        {"pk1": real, "pk2": real}, _BLURB, handles={"pk1": "same-handle", "pk2": "same-handle"})
    assert kept == {"pk1": real, "pk2": real}

    # ...and two DIFFERENT pages carrying it are still shared boilerplate.
    assert bf.drop_shared_boilerplate(
        {"pk1": real, "pk2": real}, _BLURB, handles={"pk1": "a", "pk2": "b"}) == {}


def test_a_brand_plus_title_echo_is_dropped():
    """MECHANISM 3, invisible to the other two: kyliecosmetics.com renders its name tag from a
    template, "Kylie Cosmetics - {title}". Per-product so repetition never fires, not the shop
    blurb so the comparison never fires, and admitted purely by whether the title is long enough
    to clear the 50-char floor -- 2 of 3 accepted values on that storefront."""
    echo = "Kylie Cosmetics - Glossy Pink Makeup Bag + Deluxe Samples"
    kept = bf.drop_shared_boilerplate(
        {"pk1": echo}, _BLURB, titles={"pk1": "Glossy Pink Makeup Bag + Deluxe Samples"})
    assert kept == {}


def test_a_real_description_that_merely_MENTIONS_the_title_survives():
    """The echo rule must key on 'there is nothing here but the title', not on 'the title appears'
    -- brand-authored copy routinely opens with the product's own name."""
    title = "Glossy Pink Makeup Bag"
    real = (f"The {title} is a compact vegan-leather pouch with a wipe-clean lining, sized for a "
            "full brush set and a cushion compact.")
    assert bf.drop_shared_boilerplate({"pk1": real}, _BLURB, titles={"pk1": title}) == {"pk1": real}


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


def _row(pk, ck, handle, title="Artist Eye Palette Core Mood"):
    # A REAL title, not "t": `_is_title_echo` is fed this on the delivery path, and a degenerate
    # one-character title can never demonstrate the mechanism it is supposed to exercise.
    return {
        "product_key": pk, "content_key": ck,
        "canonical_url": f"https://jsmbeauty.sg/products/{handle}",
        "source_domain": "jsmbeauty.sg", "title": title, "image_url": "i",
        "description": "tiny", "category_path": None, "tags": None, "demographic": None,
        "use_case_tags": None, "lifestyle_tags": None, "pdp_scope": None,
        "source_system": "catalog_enrichment_agent_v1", "pdp_lifecycle_stage": "draft",
    }


def _harness(monkeypatch, rows, *, body_map, pdp=None, blurb=_BLURB, truncated=False):
    """Drive run() against stubs, recording what each content_key was refreshed AS."""
    db = _FakeDB(rows)
    refreshed = {}
    fetched = []

    async def _noop():
        pass

    async def _load(domain, max_products):
        return dict(body_map), truncated

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
    as brand-official PRODUCT copy.

    ONE blurb candidate and ONE genuine candidate, deliberately DIFFERENT strings, so repetition
    cannot fire and only the blurb comparison can explain the drop. An earlier version gave both
    rows the same blurb; that made it a repetition test wearing a shop-blurb label, and a mutant
    deleting the entire `_load_shop_blurb` call at the call site survived the whole suite.
    """
    rows = [_row("pk1", "ck1", "h1"), _row("pk2", "ck2", "h2")]
    db, refreshed, _ = _harness(
        monkeypatch, rows,
        body_map={"h1": "tiny", "h2": "tiny"},
        pdp={"h1": _BLURB, "h2": _LONG_META},
        blurb=_BLURB,
    )

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    written = [u["product_key"] for u in db.updates]
    assert written == ["pk2"], f"expected only the genuine row to be written, got {written}"
    assert _BLURB not in " ".join(str(u) for u in db.updates)
    assert refreshed == {"ck2": bf.REFRESH_SOURCE_PDP_META}


def test_an_unavailable_shop_blurb_refuses_an_unrepeated_candidate(monkeypatch):
    """FAIL CLOSED. `fetch_shop_description` swallows every failure into None, so a homepage 403
    disarms mechanism 1 -- and a lone candidate then has NOTHING checking it. Letting it through
    silently re-opens the hole this change exists to close, printing `boilerplate=0` as though
    nothing had happened."""
    rows = [_row("pk1", "ck1", "h1")]
    db, refreshed, _ = _harness(
        monkeypatch, rows, body_map={"h1": "tiny"}, pdp={"h1": _BLURB}, blurb=None)

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    assert db.updates == [], "an uncheckable candidate must not be written"
    assert refreshed == {}


def test_the_blurb_fetch_is_retried_before_it_is_believed(monkeypatch):
    """`_load_body_map` retries 3x precisely because this module's fetches swallow errors into a
    falsy value. The blurb fetch has the identical swallow and the failure is in the DANGEROUS
    direction, so it retries too -- otherwise one transient 503 refuses a whole domain."""
    calls = []

    async def _flaky(domain, **k):
        calls.append(domain)
        return _BLURB if len(calls) >= 2 else None

    _real_sleep = asyncio.sleep
    monkeypatch.setattr(bf, "fetch_shop_description", _flaky)
    monkeypatch.setattr(bf.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))

    assert asyncio.run(bf._load_shop_blurb("jsmbeauty.sg")) == _BLURB
    assert len(calls) == 2, "a transient failure must not be taken as 'no blurb'"


def test_a_truncated_feed_skips_the_fallback_rather_than_judging_half_a_storefront(monkeypatch):
    """NON-DETERMINISM, proven on real data: the same 24 jsmbeauty.sg candidates kept 13 as one
    batch and 15 as two, and the string that survived the split was a shared one. The census votes
    on whatever it is handed, so a storefront sliced by --max-products gets a verdict that depends
    on the cut -- in the direction that WRITES.

    DRIVES THE REAL `_load_body_map`, because the first version of this guard compared
    `len(body_map) >= max_products` and a single DUPLICATE HANDLE in a capped feed slipped under
    it: the map is keyed on handle, so it shrinks below the cap while the feed was still cut. That
    is routine when a catalog mutates across 250-item page boundaries. The loader now reports the
    truncation itself.
    """
    feed = [{"handle": f"h{i}", "body_html": "<p>tiny</p>"} for i in range(4)]
    feed.append({"handle": "h0", "body_html": "<p>tiny</p>"})   # the duplicate that hid the cut

    async def _feed(domain, max_products=None, **k):
        return feed[:max_products]

    monkeypatch.setattr(bf, "fetch_shopify_products", _feed)
    body_map, truncated = asyncio.run(bf._load_body_map("jsmbeauty.sg", 4))
    assert len(body_map) == 4 and len(body_map) < 5
    assert truncated is True, "a capped feed with a repeated handle is still a partial storefront"

    # ...and a feed that ends on its own is not flagged.
    _, untruncated = asyncio.run(bf._load_body_map("jsmbeauty.sg", 800))
    assert untruncated is False


def test_a_pagination_death_is_truncation_even_though_it_is_UNDER_the_cap(monkeypatch):
    """The second truncation shape, and the one a cap comparison cannot see. `_load_body_map`'s own
    comment says the feed "returns a PARTIAL list when pagination dies mid-feed, which looks like
    an exact multiple of the 250-item page size" -- that partial is far BELOW --max-products, so a
    census over it covers a fraction of the storefront while every cap check stays silent."""
    feed = [{"handle": f"h{i}", "body_html": "<p>x</p>"} for i in range(250)]

    async def _feed(domain, max_products=None, **k):
        return feed[:max_products]

    monkeypatch.setattr(bf, "fetch_shopify_products", _feed)
    # `_load_body_map` retries a page-aligned feed 3x with 10s/20s sleeps -- by design, and the
    # reason this test would otherwise take 30 seconds.
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(bf.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    body_map, truncated = asyncio.run(bf._load_body_map("jsmbeauty.sg", 800))

    assert len(body_map) == 250 < 800, "well under the cap"
    assert truncated is True, "a page-aligned feed is a suspected mid-feed pagination death"


def test_a_truncated_feed_is_not_crawled_at_all(monkeypatch):
    """Skipping the census must also skip the requests it would have needed."""
    rows = [_row("pk1", "ck1", "h1"), _row("pk2", "ck2", "h2")]
    db, _, fetched = _harness(
        monkeypatch, rows,
        body_map={"h1": "tiny", "h2": "tiny"},
        pdp={"h1": _LONG_META, "h2": _LONG_META + " Distinct."},
        truncated=True,
    )

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=2,
                              pdp_fallback=True)) == 0

    assert fetched == [], "a partial storefront must not be judged, nor crawled"
    assert db.updates == []


@pytest.mark.parametrize("order", [["pdp", "body"], ["body", "pdp"]])
def test_two_rows_behind_ONE_content_key_are_marked_if_EITHER_came_from_the_pdp(monkeypatch, order):
    """Several product_keys can share a content_key, and the view is refreshed once per KEY. If
    any contributing row carries PDP meta copy the marker must say so -- OR, never overwrite.

    PARAMETRISED ON ORDER, and that is the whole test. With the PDP row written LAST, plain
    assignment reaches the same answer as the OR, so a single ordering cannot tell them apart --
    a mutant replacing `or` with `=` survived my first attempt at this test for exactly that
    reason. It is the PDP-row-FIRST case where a later body row would erase the marker.
    """
    _h = {"body": ("pk-body", "h-body"), "pdp": ("pk-pdp", "h-pdp")}
    rows = [_row(_h[k][0], "CK", _h[k][1]) for k in order]
    _, refreshed, _ = _harness(
        monkeypatch, rows,
        body_map={"h-body": _LONG_BODY, "h-pdp": "tiny"},
        pdp={"h-pdp": _LONG_META},
        blurb=_BLURB,
    )

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    assert refreshed == {"CK": bf.REFRESH_SOURCE_PDP_META}, \
        "a body row must not erase a sibling's PDP-meta provenance"


def test_the_boilerplate_drop_is_counted_not_just_performed(monkeypatch):
    """The operator reads these counters to decide whether a run did what they asked. A silent
    drop is indistinguishable from a storefront that simply had nothing to fill."""
    rows = [_row("pk1", "ck1", "h1"), _row("pk2", "ck2", "h2")]
    db, _, _ = _harness(
        monkeypatch, rows,
        body_map={"h1": "tiny", "h2": "tiny"},
        pdp={"h1": _BOGOS, "h2": _BOGOS},
        blurb=_BLURB,
    )
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    done = [ln for ln in printed if ln.startswith("[done]")]
    assert done and "'boilerplate': 2" in done[0], done
    assert db.updates == []


def test_the_title_echo_threshold_keeps_its_margin_on_both_sides():
    """PINS THE NUMBER, so a later edit has to argue with the measurement rather than nudge it.

    Observed echoes leave 5-15 characters once the title is removed; observed genuine copy leaves
    76-101. The threshold sits in that gap. It is deliberately not raised to catch every template
    imaginable: a longer SEO scaffold leaves 36 and is knowingly missed, because tuning to an
    unobserved case is how the previous guard overfitted, and because genuine copy just over the
    50-char floor leaves only about `50 - len(title)`.
    """
    title = "Glossy Pink Makeup Bag"
    assert bf._is_title_echo(f"Kylie Cosmetics - {title}", title) is True
    assert bf._is_title_echo(
        f"The {title} is a compact vegan-leather pouch with a wipe-clean lining.", title) is False
    # The knowingly-missed family, asserted as MISSED so the gap is visible, not forgotten.
    assert bf._is_title_echo(f"Buy {title} online at JUNGSAEMMOOL Singapore", title) is False
    assert bf._is_title_echo("Anything at all", None) is False, "no title means no signal"


def test_a_title_echo_is_refused_ON_THE_DELIVERY_PATH(monkeypatch):
    """MECHANISM 3, driven through run() rather than through the pure function.

    Round 3 blocked because deleting the shop-blurb call AT THE CALL SITE left the suite green
    while the pure function stayed well tested. That was fixed for mechanism 1 -- and mechanism 3
    then shipped with the identical gap: changing `titles=` to `{}` at the call site survived
    226/226, and the value below (measured live on kyliecosmetics.com 2026-09-06) went to serving
    as a `published` brand-official description.
    """
    echo = "Kylie Cosmetics - Glossy Pink Makeup Bag + Deluxe Samples"
    rows = [_row("pk1", "ck1", "h1", title="Glossy Pink Makeup Bag + Deluxe Samples")]
    db, refreshed, _ = _harness(
        monkeypatch, rows, body_map={"h1": "tiny"}, pdp={"h1": echo})

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    assert db.updates == [], "a brand+title echo was written as brand-official product copy"
    assert refreshed == {}
    assert echo not in " ".join(str(u) for u in db.updates)


def test_ONE_product_behind_two_rows_is_still_filled_ON_THE_DELIVERY_PATH(monkeypatch):
    """MECHANISM 2's identity unit, driven through run(). `handles={}` at the call site survived
    the whole suite while the pure function was tested -- and it fails CLOSED (both rows dropped as
    'shared' when it is one product twice), so nothing else would have shown it either."""
    rows = [_row("pk1", "ck1", "same-handle"), _row("pk2", "ck2", "same-handle")]
    db, _, _ = _harness(
        monkeypatch, rows, body_map={"same-handle": "tiny"}, pdp={"same-handle": _LONG_META})

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    written = sorted(u["product_key"] for u in db.updates)
    assert written == ["pk1", "pk2"], \
        f"one product behind two rows was mistaken for shared boilerplate: {written}"


def test_an_unavailable_blurb_is_COUNTED_and_announced(monkeypatch):
    """`blurb_unavailable` is what tells an operator that a whole domain was refused for a reason
    other than 'nothing to fill'. The `boilerplate` counter got an assertion in the same commit
    that added this one and left it unasserted."""
    rows = [_row("pk1", "ck1", "h1")]
    _harness(monkeypatch, rows, body_map={"h1": "tiny"}, pdp={"h1": _LONG_META}, blurb=None)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))

    assert asyncio.run(bf.run(apply=True, domains_filter=[], max_products=10,
                              pdp_fallback=True)) == 0

    done = [ln for ln in printed if ln.startswith("[done]")]
    assert done and "'blurb_unavailable': 1" in done[0], done
    assert any("shop blurb unavailable" in ln for ln in printed), "the refusal must be announced"


def test_a_blurb_too_short_to_clear_the_floor_counts_as_NO_blurb(monkeypatch):
    """MEASURED: glossier.com's homepage description is "Glossier" -- 8 characters. Accepting it
    arms mechanism 1 in name only, since no candidate over the 50-char floor can ever equal it,
    AND switches off the fail-closed refusal -- so singletons pass with nothing checking them.
    That is the hole this guard exists to close, reached through a different door."""
    real = "A watery lip tint that layers into a vivid stain without drying the lips out."
    assert bf.drop_shared_boilerplate({"a": real}, "Glossier") == {}, \
        "an 8-char site name must not count as an armed blurb comparison"
    assert bf.drop_shared_boilerplate({"a": real}, _BLURB) == {"a": real}

    async def _short(domain, **k):
        return "Glossier"

    monkeypatch.setattr(bf, "fetch_shop_description", _short)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(bf.asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    assert asyncio.run(bf._load_shop_blurb("glossier.com")) is None


def test_the_title_echo_threshold_is_pinned_at_its_boundary():
    """PINS THE NUMBER. The previous version of this test claimed to and did not: 30 -> 29 and
    30 -> 31 both survived it, so it pinned a wide band. These two assertions straddle the
    boundary exactly, and also kill `<` -> `<=`."""
    title = "Glossy Pink Makeup Bag"

    def value_leaving(n: int) -> str:
        return f"{title} " + ("a" * n)

    # LITERALS, not `bf._ECHO_REMAINDER_MIN - 1`. Deriving the boundary from the constant makes
    # the test move WITH it: 30 -> 29 and 30 -> 31 both survived that version too.
    assert bf._ECHO_REMAINDER_MIN == 30, "the measured margin; move it deliberately, with data"
    assert bf._is_title_echo(value_leaving(29), title) is True
    assert bf._is_title_echo(value_leaving(30), title) is False


def test_a_value_that_does_not_contain_the_title_is_never_an_echo():
    """The `t not in v` precondition, untested until now. Without it the rule would measure the
    length of copy that has nothing to do with the title and refuse short genuine descriptions."""
    assert bf._is_title_echo("A watery lip tint with a glassy finish.", "Totally Different") is False
    assert bf._is_title_echo("", "Some Title") is False
