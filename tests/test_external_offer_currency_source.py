"""`resolve_external_offer` says whether the stored currency was READ or filled in.

When the page names no currency the reader stores the market's (`"JPY" if JP else "USD"`), and
nothing downstream could tell that from a reading: a Korean page priced ₩24,000 arrives as 24000
USD. The seed refresh's canonical-offer projection refuses a price whose currency was not read
(services/external_offer_dual_write, controller review of #2416), so the snapshot evidence records
`price_currency_source`: `page` or `market_default`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest


@pytest.mark.parametrize(
    "extracted_currency,market,stored,source",
    [
        ("sgd", "US", "SGD", "page"),
        (None, "US", "USD", "market_default"),
        ("", "JP", "JPY", "market_default"),
    ],
)
def test_the_snapshot_records_where_its_currency_came_from(
    monkeypatch: pytest.MonkeyPatch, extracted_currency, market, stored, source
) -> None:
    import services.external_offers_service as svc

    writes: List[Dict[str, Any]] = []

    async def fake_fetch_html(url, **kwargs):
        return "<html></html>", "text/html"

    def fake_extract(url, html):
        return {"price_amount": 24000.0, "price_currency": extracted_currency, "title": "x"}

    async def no_existing(*a, **k):
        return None

    async def capture(stmt, values=None):
        writes.append(dict(values or {}))

    monkeypatch.setattr(svc, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(svc, "_extract_from_html", fake_extract)
    monkeypatch.setattr(svc, "_get_snapshot_row", no_existing)
    monkeypatch.setattr(svc.database, "execute", capture)

    with pytest.raises(Exception):  # no row to re-read after the write; the write is what we test
        asyncio.run(svc.resolve_external_offer(
            market=market, url="https://brand.example/products/cream", force_refresh=True,
        ))
    assert writes, "the snapshot insert never ran"
    assert writes[-1]["price_currency"] == stored
    assert writes[-1]["evidence"]["price_currency_source"] == source
