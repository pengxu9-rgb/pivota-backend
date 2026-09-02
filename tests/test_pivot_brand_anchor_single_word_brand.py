"""A single-word brand must be able to anchor the pivot lane.

`_category_brand_anchor_terms` requires a category prefix AND >= 2 residual
tokens. The second rule keeps a lone descriptor ("brightening blush") from
becoming an accidental brand gate, but a token COUNT cannot tell a descriptor
from a brand — so Murad, CeraVe, NARS and every other single-word brand could
never anchor, and the broad category lane filled the page with whatever else
matched. Measured live on 2026-09-02: "show me Murad products" answered with a
LIZUSH bath bomb ("...bath & body products"), and "I am looking for a Murad
cleanser" answered with twelve cleansers from other brands.

The separating signal is whether the token IS a brand in our catalog, which
`_detect_brand_query` already answers from the catalog-warmed dictionary.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_api as agent_api  # noqa: E402
import routes.agent_shop_gateway as gw  # noqa: E402
from services.pivot_query_service import _category_brand_anchor_terms  # noqa: E402


@pytest.fixture
def catalog_brands(monkeypatch: pytest.MonkeyPatch):
    """Stub the catalog dictionary: 'murad' and 'cerave' are real brands."""

    async def _noop_load() -> None:
        return None

    monkeypatch.setattr(agent_api, "_ensure_brand_dictionary_loaded", _noop_load)

    def _detect(query):
        lowered = str(query or "").lower()
        for brand in ("murad", "cerave", "the ordinary"):
            if brand in lowered:
                return {"brand_like": True, "brand_terms": [brand], "mode": "catalog"}
        return {"brand_like": False, "brand_terms": [], "mode": None}

    monkeypatch.setattr(agent_api, "_detect_brand_query", _detect)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,expected",
    [
        ("show me Murad products", ["murad"]),  # the live repro — no category at all
        ("murad cleanser", ["murad"]),          # category found, ONE residual token
        ("I am looking for a Murad cleanser", ["murad"]),
        ("the ordinary serum", ["the", "ordinary"]),  # multi-word span splits to tokens
    ],
)
async def test_single_word_brand_anchors(catalog_brands, query, expected):
    # The shipped residual heuristic cannot produce these on its own.
    assert _category_brand_anchor_terms(query) != expected
    terms, source = await gw._resolve_brand_anchor_terms(query)
    assert terms == expected
    assert source == "catalog"


@pytest.mark.asyncio
async def test_a_descriptor_is_still_not_a_brand_gate(catalog_brands):
    """The positive counterpart for the rule the residual heuristic was protecting."""
    terms, source = await gw._resolve_brand_anchor_terms("brightening blush")
    assert terms == []
    assert source is None


@pytest.mark.asyncio
async def test_the_category_residual_path_is_unchanged(catalog_brands):
    """A two-word brand with a category still resolves the way it always did."""
    terms, source = await gw._resolve_brand_anchor_terms("knight unicorn blush")
    assert terms == ["knight", "unicorn"]
    assert source == "category_residual"


@pytest.mark.asyncio
async def test_a_heuristic_guess_is_refused(monkeypatch: pytest.MonkeyPatch):
    """Only 'catalog'/'static' count. 'heuristic' is a guess about an unknown
    string and would re-admit the false positives the residual rule excluded."""

    async def _noop_load() -> None:
        return None

    monkeypatch.setattr(agent_api, "_ensure_brand_dictionary_loaded", _noop_load)
    monkeypatch.setattr(
        agent_api,
        "_detect_brand_query",
        lambda q: {"brand_like": True, "brand_terms": ["brightening"], "mode": "heuristic"},
    )
    terms, source = await gw._resolve_brand_anchor_terms("brightening serum")
    assert terms == []
    assert source is None


@pytest.mark.asyncio
async def test_a_cold_or_broken_dictionary_keeps_todays_behaviour(monkeypatch: pytest.MonkeyPatch):
    """Fail-safe: detection must never break recall."""

    async def _boom() -> None:
        raise RuntimeError("dictionary unavailable")

    monkeypatch.setattr(agent_api, "_ensure_brand_dictionary_loaded", _boom)
    terms, source = await gw._resolve_brand_anchor_terms("show me Murad products")
    assert terms == []
    assert source is None


def test_the_pivot_handler_consumes_the_resolver() -> None:
    """The delivery line.

    Every assertion above exercises the helper directly, so reverting the call
    site in `_handle_find_products_multi_via_pivot` to the bare
    `_category_brand_anchor_terms(query)` leaves them all green while single-word
    brands go unanchored again in production — a mutant that survived until this
    was added. `_handle_find_products_multi_via_pivot` needs a live catalog to
    invoke, so the call site is pinned by source.
    """
    source = Path(gw.__file__).read_text(encoding="utf-8")
    assert (
        "brand_anchor_terms, brand_anchor_source = await _resolve_brand_anchor_terms(query)"
        in source
    ), "the pivot handler must resolve anchors through _resolve_brand_anchor_terms"
    # And the resolver must remain the ONLY way that lane obtains anchor terms.
    assert (
        "brand_anchor_terms = _category_brand_anchor_terms(query)" not in source
    ), "a direct call re-introduces the single-word-brand blind spot"


# ---------------------------------------------------------------------------------------------
# The guards below each exist because a review REPRODUCED the failure. They run the REAL
# `_detect_brand_query` with the real flag, because every test above monkeypatches it — and with
# the flag unset the headline fix is a no-op while the static path still fires, which no
# monkeypatched test could ever have caught.


@pytest.fixture
def real_detector(monkeypatch: pytest.MonkeyPatch):
    """Real detector, flag ON, with a seeded catalog dictionary."""
    monkeypatch.setenv("GATEWAY_DYNAMIC_BRAND_DETECT", "1")
    monkeypatch.setattr(
        agent_api,
        "_DYNAMIC_BRAND_SET",
        frozenset({"murad", "cerave", "the ordinary", "essence", "sigma beauty"}),
    )
    monkeypatch.setattr(agent_api, "_DYNAMIC_BRAND_LOADED_AT", float("inf"))


@pytest.mark.asyncio
async def test_the_real_detector_anchors_a_real_brand(real_detector):
    """The integration, not a stub: mode really is 'catalog' for a catalogued brand."""
    assert agent_api._detect_brand_query("murad cleanser").get("mode") == "catalog"
    terms, source = await gw._resolve_brand_anchor_terms("murad cleanser")
    assert (terms, source) == (["murad"], "catalog")


@pytest.mark.asyncio
async def test_the_static_path_can_never_anchor(real_detector):
    """`static` is NOT gated by the flag and matches by COMPACT SUBSTRING over the whole
    query, so it fired on strings that merely contain a brand. Reproduced with the flag OFF:
    "four sigmatic mushroom coffee" resolved ['sigma','beauty'] and collapsed a ten-product
    page to Sigma Beauty — dropping the Four Sigmatic row the user asked for."""
    for query in ("four sigmatic mushroom coffee", "sigma brushes"):
        assert agent_api._detect_brand_query(query).get("mode") == "static"  # still detected...
        assert await gw._resolve_brand_anchor_terms(query) == ([], None)     # ...never anchors


@pytest.mark.asyncio
async def test_two_brands_refuse_rather_than_picking_one(real_detector):
    """"tom ford vs jo malone" kept the alphabetically-first brand and answered a comparison
    with a Jo Malone-only page. A tie is not ours to break."""
    assert await gw._resolve_brand_anchor_terms("tom ford vs jo malone") == ([], None)
    # NOTE: what actually refuses this today is the mode guard — the two-brand list came from
    # `static`. The `len(spans) != 1` check in the resolver is defensive: `_detect_brand_query`'s
    # catalog branch returns exactly one span by construction, so no test can kill that line, and
    # a surviving mutant there is correct rather than missing coverage.


@pytest.mark.asyncio
async def test_degenerate_short_tokens_never_anchor(monkeypatch: pytest.MonkeyPatch, real_detector):
    """`_normalize_brand_query_text` deletes apostrophes/accents into spaces rather than folding
    them, so "L'Oreal" becomes the span "l or al" -> ['l','or','al'], which matched "Floral
    Street" while reporting a brand hit."""
    monkeypatch.setattr(agent_api, "_DYNAMIC_BRAND_SET", frozenset({"l or al"}))
    # The ACCENTED spelling is the one that degenerates: "L'Oreal serum" normalizes to
    # "l oreal serum" and never matches the span, so an unaccented test would pass for the
    # wrong reason — it did, and this mutant survived until the accent was put back.
    assert agent_api._normalize_brand_query_text("L'Or\u00e9al serum") == "l or al serum"
    assert agent_api._detect_brand_query("L'Or\u00e9al serum").get("mode") == "catalog"
    assert await gw._resolve_brand_anchor_terms("L'Or\u00e9al serum") == ([], None)


@pytest.mark.asyncio
async def test_a_span_that_is_itself_a_category_never_anchors(real_detector):
    """`essence` is both a real drugstore brand and a product type. Anchoring "essence toner"
    on the brand narrows a page of K-beauty essences to one label. category_path_prefix_for_query
    resolves 'essence' to a category and 'murad' to None, so it decides — not a stopword list."""
    assert "essence" in agent_api._DYNAMIC_BRAND_SET
    assert await gw._resolve_brand_anchor_terms("essence toner") == ([], None)
    # ...while a real brand with a category term still anchors.
    assert await gw._resolve_brand_anchor_terms("murad cleanser") == (["murad"], "catalog")


# The gateway post-filter is the THIRD path that decides the page, alongside the recall admit
# branch and the +180 score. A review measured all three and only one was word-delimited: a raw
# substring test here KEPT the rows the SQL refuses — `lush` kept "Blush Cosmetics" and "Plush
# Beauty", `sigma` kept "Four Sigmatic" — each reported as brand_anchor_matched: true.


@pytest.mark.parametrize(
    "brand,term,kept",
    [
        ("LUSH", "lush", True),
        ("Blush Cosmetics", "lush", False),
        ("Plush Beauty", "lush", False),
        ("Four Sigmatic", "sigma", False),
        ("Sigma Beauty", "sigma", True),
        ("La Roche-Posay", "roche", True),   # separators fold, so a punctuated brand still matches
        ("Kiehl's Since 1851", "kiehl", True),
        ("Murad, Inc.", "murad", True),
    ],
)
def test_the_post_filter_matches_on_WORD_boundaries(brand, term, kept):
    """Mirrors `_brand_identity_expr` in the recall SQL. The two must agree or the page is decided
    by whichever ran last."""
    assert gw._product_matches_brand_anchor({"brand": brand}, [term]) is kept
