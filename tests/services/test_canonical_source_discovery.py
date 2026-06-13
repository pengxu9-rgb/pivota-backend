from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services import canonical_source_discovery as disc
from services.canonical_inci_resolver import ResolvedInci


# --- pure helpers -------------------------------------------------------------

def test_registrable_name():
    assert disc.registrable_name("https://www.theordinary.com/en-us/x.html") == "theordinary"
    assert disc.registrable_name("https://brand.co.uk/p") == "brand"
    assert disc.registrable_name("https://anuko.myshopify.com/products/x") == "anuko"
    assert disc.registrable_name(None) == ""


def test_is_brand_official_url():
    assert disc.is_brand_official_url("The Ordinary", "https://theordinary.com/p") is True
    assert disc.is_brand_official_url("Anuko", "https://anuko.myshopify.com/products/x") is True
    assert disc.is_brand_official_url("Winona", "https://www.sephora.com/p/winona") is False
    assert disc.is_brand_official_url("BB", "https://bb.com") is False  # too-short brand token
    assert disc.is_brand_official_url("The Ordinary", "https://amazon.com/dp/x") is False  # generic host


def test_discover_sources_orders_by_authority():
    cands = disc.discover_sources(
        brand="The Ordinary",
        canonical_url="https://theordinary.com/p",
        barcode="769915190328",
    )
    assert [c.source for c in cands] == ["brand_official", "inci_database"]
    assert cands[0].method == "url" and cands[1].method == "openbeautyfacts"


def test_discover_sources_reseller_url_ranks_last():
    cands = disc.discover_sources(
        brand="Winona", canonical_url="https://www.sephora.com/p/winona", barcode="123456789012"
    )
    assert [c.source for c in cands] == ["inci_database", "reseller_listing"]


def test_discover_sources_empty_when_no_inputs():
    assert disc.discover_sources(brand="X", canonical_url=None, barcode=None) == []


# --- orchestrator -------------------------------------------------------------

class FakeDB:
    def __init__(self, row):
        self._row = row

    async def fetch_one(self, query, params=None):
        return dict(self._row) if self._row else None


def _patch(monkeypatch, *, url_inci=None, obf_inci=None, ingest=None):
    async def _urls(urls, *, fetch):
        return ResolvedInci(url_inci, urls[0]) if url_inci else None

    async def _obf(barcode, *, fetch):
        return ResolvedInci(obf_inci, f"obf:{barcode}") if obf_inci else None

    calls = {}

    async def _ingest(pk, raw_inci, source, *, db=None, dry_run=False):
        calls.update(pk=pk, raw_inci=raw_inci, source=source)
        return {"product_key": pk, "status": "ok", "source": source}

    monkeypatch.setattr(disc, "resolve_inci_from_urls", _urls)
    monkeypatch.setattr(disc, "resolve_inci_from_openbeautyfacts", _obf)
    monkeypatch.setattr(disc, "ingest_canonical_inci", ingest or _ingest)
    return calls


def test_source_canonical_inci_uses_brand_official_first(monkeypatch):
    calls = _patch(monkeypatch, url_inci="Aqua, Niacinamide, Glycerin", obf_inci="should not reach")
    db = FakeDB({"brand": "The Ordinary", "canonical_url": "https://theordinary.com/p", "barcode": "769915190328"})
    res = asyncio.run(disc.source_canonical_inci("pk1", db=db, fetch=None))

    assert res["status"] == "ok"
    assert res["sourced_from"]["source"] == "brand_official"
    assert calls["source"] == "brand_official"  # brand-official won over OBF


def test_source_falls_through_to_obf_when_url_fails(monkeypatch):
    calls = _patch(monkeypatch, url_inci=None, obf_inci="Aqua, Niacinamide, Panthenol")
    db = FakeDB({"brand": "The Ordinary", "canonical_url": "https://theordinary.com/p", "barcode": "769915190328"})
    res = asyncio.run(disc.source_canonical_inci("pk1", db=db, fetch=None))
    assert res["sourced_from"]["method"] == "openbeautyfacts"
    assert calls["source"] == "inci_database"


def test_source_no_candidates_and_not_found(monkeypatch):
    _patch(monkeypatch)
    db = FakeDB({"brand": "X", "canonical_url": None, "barcode": None})
    assert asyncio.run(disc.source_canonical_inci("pk1", db=db))["status"] == "no_candidates"
    assert asyncio.run(disc.source_canonical_inci("ghost", db=FakeDB(None)))["status"] == "not_found"


def test_source_no_inci_when_nothing_resolves(monkeypatch):
    _patch(monkeypatch, url_inci=None, obf_inci=None)
    db = FakeDB({"brand": "The Ordinary", "canonical_url": "https://theordinary.com/p", "barcode": "111111111111"})
    res = asyncio.run(disc.source_canonical_inci("pk1", db=db, fetch=None))
    assert res["status"] == "no_inci_sourced"
    assert res["candidates_tried"] == ["brand_official", "inci_database"]
