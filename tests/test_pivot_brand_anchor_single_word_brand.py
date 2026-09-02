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
