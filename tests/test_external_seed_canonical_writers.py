"""Seed creation, CSV import and preview store a canonical only when it IS the destination.

`canonical_url` is the served URL (`destination_of` reads it first) while the click is minted
from `destination_url`. A stored canonical that is not the same destination makes the seed
serve one page and send buyers to another, and locks it out of every refresh (which fetches
`destination_url`). Creation made it worse: it keyed its "existing seed" lookup and its
supersede on the page's canonical, and a fentybeauty shade page declares a SIBLING shade
canonical -- so creating the `...-470` seed found the `...-340` seed, rewrote its destination
and disabled the rest. `_canonical_for_destination` is the one rule all three now apply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

DEST = "https://fentybeauty.com/products/pro-filtr-soft-matte-longwear-foundation-470"
SIBLING = "https://fentybeauty.com/products/pro-filtr-soft-matte-longwear-foundation-340"
WWW = DEST.replace("://", "://www.")


def _snapshot(canonical: Optional[str], domain: str = "fentybeauty.com") -> SimpleNamespace:
    return SimpleNamespace(
        canonical_url=canonical,
        domain=domain,
        title="Pro Filt'r Foundation 470",
        image_url=None,
        price_amount=40.0,
        price_currency="USD",
        availability="in_stock",
        evidence={},
    )


@pytest.mark.parametrize(
    "candidates,expected",
    [
        ((SIBLING,), DEST),
        ((WWW,), WWW),
        ((None, WWW), WWW),
        ((SIBLING, WWW), WWW),
        ((DEST + "?variant=2",), DEST),
        ((None,), DEST),
        ((), DEST),
    ],
)
def test_canonical_for_destination_takes_only_the_destination(candidates, expected):
    from routes.employee_products import _canonical_for_destination

    assert _canonical_for_destination(DEST, *candidates) == expected


# ----------------------------------------------------------------- preview


def test_preview_shows_the_destination_not_a_sibling_canonical(monkeypatch):
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(SIBLING)))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    out = asyncio.run(
        ep.preview_external_seed(ep.PreviewExternalSeedRequest(destination_url=DEST, market="US"), current_user={})
    )["preview"]

    assert out["canonical_url"] == DEST
    assert out["page_canonical_url"] == SIBLING, "the page's claim is still shown"
    assert out["external_product_id"] == ep._stable_external_product_id(DEST), "not the sibling's id"


def test_preview_keeps_a_www_canonical(monkeypatch):
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(WWW, "www.fentybeauty.com")))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    out = asyncio.run(
        ep.preview_external_seed(ep.PreviewExternalSeedRequest(destination_url=DEST, market="US"), current_user={})
    )["preview"]
    assert (out["canonical_url"], out["domain"]) == (WWW, "www.fentybeauty.com")


# ----------------------------------------------------------------- create


def _run_create(monkeypatch, *, page_canonical: Optional[str], existing: Optional[Dict[str, Any]] = None):
    import routes.employee_products as ep

    lookups: List[Dict[str, Any]] = []
    writes: List[Dict[str, Any]] = []
    supersedes: List[Dict[str, Any]] = []

    async def fetch_one(query, values=None):
        lookups.append({"sql": query, **(values or {})})
        return existing

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    async def execute(query, values=None):
        supersedes.append({"sql": query, **(values or {})})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(page_canonical)))
    monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    out = asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    return out, lookups, writes, supersedes


def test_creating_a_seed_never_matches_or_stores_a_sibling_canonical(monkeypatch):
    import routes.employee_products as ep

    out, lookups, writes, _ = _run_create(monkeypatch, page_canonical=SIBLING)

    (lookup,) = lookups
    assert SIBLING not in (lookup["match_url"], lookup["dest"]), "the sibling's seed is never looked up"
    assert "canonical_url IN (:match_url, :dest)" in lookup["sql"]
    (insert,) = writes
    assert insert["sql"].strip().upper().startswith("INSERT")
    assert insert["canonical_url"] == DEST
    assert insert["external_product_id"] == ep._stable_external_product_id(DEST), "not the sibling's id"
    assert out["seed"]["canonical_url"] == DEST
    seed_data = insert["seed_data"] if isinstance(insert["seed_data"], dict) else __import__("json").loads(insert["seed_data"])
    assert seed_data["snapshot"]["canonical_url"] == SIBLING, "the page's claim is still recorded"


def test_updating_an_existing_seed_drops_a_canonical_that_is_not_its_destination(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": SIBLING,  # written earlier by the old refresh
        "domain": "fentybeauty.com",
        "seed_data": {"title": "Pro Filt'r Foundation 470"},
    }
    _, _, writes, supersedes = _run_create(monkeypatch, page_canonical=SIBLING, existing=existing)

    (update,) = writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert (update["destination_url"], update["canonical_url"]) == (DEST, DEST)
    (supersede,) = supersedes
    assert SIBLING not in (supersede["match_url"], supersede["dest"]), "never disables the sibling's seeds"
    assert "destination_url IN (:match_url, :dest)" in supersede["sql"]


def test_updating_an_existing_seed_keeps_its_own_canonical_when_that_is_the_destination(monkeypatch):
    """No canonical on the page this time; the row's own `www` canonical is still valid."""
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": WWW,
        "domain": "www.fentybeauty.com",
        "seed_data": {},
    }
    _, _, writes, _ = _run_create(monkeypatch, page_canonical=None, existing=existing)
    assert writes[0]["canonical_url"] == WWW


def test_creating_a_seed_keeps_a_www_canonical(monkeypatch):
    _, lookups, writes, _ = _run_create(monkeypatch, page_canonical=WWW)
    assert writes[0]["canonical_url"] == WWW
    assert (lookups[0]["match_url"], lookups[0]["dest"]) == (WWW, DEST)


# ----------------------------------------------------------------- CSV import (row lane)


def _run_csv(monkeypatch, csv_text: str, existing: Optional[Dict[str, Any]] = None):
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def fetch_one(query, values=None):
        return existing

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    result = asyncio.run(
        ep._import_external_seeds_csv_text(
            text=csv_text, current_user={"employee_id": "emp_test"}, market="US", tool="*", mode="upsert"
        )
    )
    return result, writes


def test_an_imported_canonical_that_is_not_the_destination_is_replaced_and_reported(monkeypatch):
    result, writes = _run_csv(monkeypatch, f"destination_url,canonical_url,title\n{DEST},{SIBLING},Foundation 470\n")

    assert result.errors == []
    (write,) = writes
    assert write["canonical_url"] == DEST
    assert len(result.warnings) == 1 and SIBLING in result.warnings[0]


def test_an_imported_canonical_that_is_the_destination_is_kept(monkeypatch):
    result, writes = _run_csv(monkeypatch, f"destination_url,canonical_url,title\n{DEST},{WWW},Foundation 470\n")

    assert writes[0]["canonical_url"] == WWW
    assert result.warnings == []


def test_an_import_update_drops_the_rows_old_canonical_when_it_is_not_the_destination(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": SIBLING,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    _, writes = _run_csv(monkeypatch, f"destination_url,title\n{DEST},Foundation 470\n", existing=existing)

    (update,) = writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert (update["destination_url"], update["canonical_url"]) == (DEST, DEST)
