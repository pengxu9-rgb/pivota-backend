"""C3 — _winning_products_not_carried: the winning competitor products AI names
that a RESELLER does NOT carry (a stocking/sourcing signal). Matches cleaned
competitor names against the merchant's carried brand-forms; excludes carried +
ingredient/category noise; ranks by how often AI names each.
"""

from unittest.mock import AsyncMock, patch

import pytest

from services.agent_center_bd_report_service import _winning_products_not_carried

_MOD = "services.agent_center_bd_report_service"


def _win_plan(*queries):
    """queries: (query_text, [competitor_benchmark names])."""
    return {
        "available": True,
        "sku_plans": [
            {
                "losing_queries": [
                    {"query": q, "competitor_benchmark": names} for q, names in queries
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_excludes_carried_filters_noise_and_ranks():
    wp = _win_plan(
        ("best collagen", ["Vital Proteins", "Garden of Life", "Magnesium", "NUTRIONE BB Lab"]),
        ("top collagen", ["Vital Proteins", "Thorne"]),
    )
    # merchant carries NUTRIONE + Ownist
    with patch(
        f"{_MOD}._carried_brand_words",
        new=AsyncMock(return_value=frozenset({"nutrione", "ownist"})),
    ):
        out = await _winning_products_not_carried("m1", wp)
    names = [r["name"] for r in out]
    # NUTRIONE excluded (carried); Magnesium filtered (ingredient noise); rest kept
    assert "NUTRIONE BB Lab" not in names
    assert "Magnesium" not in names
    assert {"Vital Proteins", "Garden of Life", "Thorne"} <= set(names)
    # Vital Proteins named in 2 queries -> ranked first
    assert names[0] == "Vital Proteins"
    assert out[0]["times_named"] == 2
    assert "best collagen" in out[0]["example_queries"]


@pytest.mark.asyncio
async def test_empty_when_carried_brands_unknown():
    # Safety: if we can't load what the merchant carries, never emit every
    # competitor as a false "you don't carry this".
    wp = _win_plan(("best collagen", ["Vital Proteins"]))
    with patch(f"{_MOD}._carried_brand_words", new=AsyncMock(return_value=frozenset())):
        out = await _winning_products_not_carried("m1", wp)
    assert out == []


@pytest.mark.asyncio
async def test_empty_without_winplan():
    assert await _winning_products_not_carried("m1", None) == []
    assert await _winning_products_not_carried("m1", {"available": False}) == []
