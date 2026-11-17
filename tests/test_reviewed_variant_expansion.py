from unittest.mock import AsyncMock, Mock

import pytest

from services import reviewed_variant_expansion as expansion


@pytest.mark.asyncio
async def test_expanded_anchors_replay_without_an_additional_seed_write(monkeypatch):
    expected = expansion.EXPANDED_VARIANTS
    projected = [
        {
            "source_variant_id": native, "barcode": barcode, "sku": sku,
            "us_offers": [f"offer:{native}"], "sku_snapshot": "original row", "offer_snapshot": ["original offer"],
        }
        for native, (barcode, _, sku) in expected.items()
    ]
    monkeypatch.setenv(expansion.ENABLE_FLAG, "1")
    monkeypatch.setattr(expansion, "load_seed", AsyncMock(return_value={}))
    monkeypatch.setattr(expansion, "seed_variants", Mock(return_value=({}, expected)))
    monkeypatch.setattr(expansion, "canonical_rows", AsyncMock(return_value=projected))
    monkeypatch.setattr(expansion, "assert_unique_additions", AsyncMock())
    monkeypatch.setattr(expansion.database, "execute", AsyncMock())
    writer = AsyncMock()
    monkeypatch.setattr(expansion, "upsert_seed_data", writer)
    assert await expansion.prepare_review(None) is None
    result = await expansion.apply_anchors(None)
    assert result["existing"] == 6 and result["replayed"]
    assert {row["source_variant_id"] for row in result["rows"]} == expected.keys()
    assert all("sku_snapshot" not in row and "offer_snapshot" not in row for row in result["rows"])
    writer.assert_not_called()
