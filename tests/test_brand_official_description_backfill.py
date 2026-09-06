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
