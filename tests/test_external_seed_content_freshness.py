"""The refresh queue must order on CRAWL recency, not WRITE recency.

Migration 200 already made this argument for the destination URL, in its own header:
`updated_at` "measured 'when did we last write this row', never 'when did we last see
this URL'". The identical defect governed the CONTENT refresh queue until migration 202 --
`get_external_referral_refresh_candidate_seed_ids` ordered by `updated_at`, which is bumped
by `external_seed_servability` on attach, by `identity_resolution` on a status flip, by
`pdp_governance_service`, and by any operator PATCH. None of those go near the origin.

The sharpest case, and the one this file exists to pin: the selector's primary query is
`WHERE attached_product_key IS NOT NULL ORDER BY ...`, and ATTACHING a product key is
itself an `updated_at` bump. A seed becoming servable -- the moment its price starts being
quoted to a buyer -- was sent to the BACK of the queue that keeps its price honest.

Two halves, both pinned here:
  * the WRITER stamps `last_crawled_at` only when a fetch actually reached the origin;
  * the READER orders on it, keeping `updated_at` strictly as a tiebreak.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from services.external_offers_service import ExternalOfferUnavailable


# --------------------------------------------------------------------------- fixtures

def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_fresh_1",
        "external_product_id": "ext_fresh_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://brand.com/products/toner",
        "canonical_url": "https://brand.com/products/toner",
        "domain": "brand.com",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"title": "Toner", "snapshot": {}},
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_failure_streak": 0,
        "last_crawled_at": None,
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": "https://brand.com/products/toner",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "domain": "brand.com",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Recorder:
    def __init__(self) -> None:
        self.observations: List[Any] = []
        self.retired: List[str] = []

    async def record(self, seed_id, observation, *, now=None):
        self.observations.append(observation)
        return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

    async def retire(self, seed_id, observation, *, now=None):
        self.retired.append(seed_id)
        return {"seed_id": seed_id, "retired": True}


def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stored_row: Dict[str, Any],
    *,
    resolve,
) -> Tuple[Dict[str, Any], List[str]]:
    """Returns (result, executed_update_queries).

    The stamp is SQL TEXT (`last_crawled_at = NOW()`), not a bound value, so the harness
    has to capture the query itself -- asserting on `values` would silently pass.
    """
    import routes.employee_products as mod

    queries: List[str] = []

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(query: str, values):
        queries.append(query)
        stored_row.update(values)

    recorder = _Recorder()
    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", recorder.record)
    monkeypatch.setattr(mod.destination_liveness, "retire_seed_for_dead_destination", recorder.retire)

    result = asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"]))
    return result, queries


def _stamped(queries: List[str]) -> bool:
    return any("last_crawled_at" in q for q in queries)


# ------------------------------------------------------------------ the writer's half

def test_a_successful_refresh_stamps_last_crawled_at(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot()

    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] == "success"
    assert _stamped(queries), "a fetch that reached the origin must record that we looked"


def test_the_stamp_rides_the_same_statement_as_the_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freshness and the value it vouches for must not be separable.

    A second statement could fail on its own and leave a row claiming to have been crawled
    at a moment when its price was never written -- or the reverse.
    """
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot(price_amount=31.0)

    _, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    stamping = [q for q in queries if "last_crawled_at" in q]
    assert len(stamping) == 1
    assert "price_amount" in stamping[0]


def test_a_404_never_stamps_last_crawled_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead link must not buy freshness -- the exact trap raise_on_unavailable closed."""
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 404
            observed["final_url"] = "https://brand.com/products/toner"
        raise ExternalOfferUnavailable("gone", status_code=404)

    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] != "success"
    assert not _stamped(queries)


def test_an_unverifiable_origin_never_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 / bot-challenge is 'cannot verify', which must never read as 'verified'."""
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 429
            observed["final_url"] = "https://brand.com/products/toner"
        raise ExternalOfferUnavailable("throttled", status_code=429)

    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] != "success"
    assert not _stamped(queries)


def test_a_transport_failure_never_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*, observed=None, **kwargs):
        raise RuntimeError("connection reset")

    result, queries = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] != "success"
    assert not _stamped(queries)


# ------------------------------------------------------------------ the reader's half

def _selector_queries(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    import services.external_referral_readiness as module

    seen: List[str] = []

    async def fake_fetch_all(query, values=None):
        seen.append(str(query))
        if "merchant_stores" in str(query):
            return [{"domain": "brand.com"}]
        if "attached_product_key IS NOT NULL" in str(query):
            return [{"id": "eps_attached_1"}]
        return [{"id": "eps_unattached_1", "domain": "brand.com"}]

    monkeypatch.setattr(module.database, "fetch_all", fake_fetch_all)
    asyncio.run(module.get_external_referral_refresh_candidate_seed_ids(limit=10))
    return [q for q in seen if "external_product_seeds" in q]


def _order_by_keys(query: str) -> List[str]:
    match = re.search(r"ORDER BY(.+?)(?:LIMIT|$)", query, re.S)
    assert match, f"no ORDER BY in: {query}"
    return [
        re.split(r"\s+", part.strip())[0]
        for part in match.group(1).split(",")
        if part.strip()
    ]


def test_every_seed_query_leads_its_order_by_with_crawl_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BOTH branches, not just the one a happy-path test happens to reach.

    The attached and unattached queries are separate SQL strings; fixing one and leaving
    the other is the shape of bug this asserts against.
    """
    queries = _selector_queries(monkeypatch)
    assert len(queries) == 2, "expected the attached and unattached seed queries"

    for query in queries:
        keys = _order_by_keys(query)
        assert keys[0] == "last_crawled_at", (
            f"refresh queue must lead on crawl recency, got {keys!r}"
        )


def test_updated_at_survives_only_beneath_the_real_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeping `updated_at` is deliberate -- but only as a tiebreak.

    Today every row's `last_crawled_at` is NULL, so the NULL cohort needs a stable
    secondary order. What it must never do again is DECIDE the order.
    """
    for query in _selector_queries(monkeypatch):
        keys = _order_by_keys(query)
        assert "updated_at" in keys, "the tiebreak was dropped, not demoted"
        assert keys.index("last_crawled_at") < keys.index("updated_at")


def test_the_attached_query_is_the_one_that_was_starved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the specific pathology: attaching bumps `updated_at`, and the attached query
    is the one selecting on `attached_product_key`. That combination is why servable rows
    -- the only ones whose price a buyer can actually see -- sank in the queue."""
    queries = _selector_queries(monkeypatch)
    attached = [q for q in queries if "attached_product_key IS NOT NULL" in q]
    assert len(attached) == 1
    assert _order_by_keys(attached[0])[0] == "last_crawled_at"
