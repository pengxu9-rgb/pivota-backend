"""A successful refresh must clear the blocker that names it as the fix.

`stale_snapshot` (services/external_referral_readiness) is a SERVING BLOCKER, marked
`auto_fixable: True`, whose `recommended_action` is literally "Refresh the seed snapshot".
It reads `snapshot.extracted_at` through `get_content_extracted_at`, which deliberately has
no `updated_at` fallback -- that fallback is what made the older helper unusable as a gate.

Until this change `_refresh_external_seed_by_id` wrote only `snapshot.refreshed_at`. The two
names differ, nothing joined them, and so the gate recommended an action that could not
clear it: a seed could be refreshed successfully every night and stay blocked forever, while
reporting `auto_fixable: True`.

The fix has to be conservative in ONE specific direction. Writing `extracted_at` when we did
not actually re-read the price would present a stale number as freshly verified -- the exact
failure this audit exists to prevent, now with a cleared blocker in front of it. So the write
is gated on reaching the served origin AND on the stored price having come off the page.
Being too strict merely leaves a row blocked; being too lax quotes a wrong price.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

from services.external_offers_service import ExternalOfferUnavailable

DEST = "https://brand.com/products/toner"


def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_extract_1",
        "external_product_id": "ext_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": DEST,
        "canonical_url": DEST,
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
        "last_crawl_attempt_at": None,
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": DEST,
        "domain": "brand.com",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "fetched_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Recorder:
    def __init__(self) -> None:
        self.observations: List[Any] = []

    async def record(self, seed_id, observation, *, now=None):
        self.observations.append(observation)
        return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

    async def retire(self, seed_id, observation, *, now=None):
        return {"seed_id": seed_id, "retired": True}


def _run(monkeypatch, row, *, resolve) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    import routes.employee_products as mod

    written: List[Dict[str, Any]] = []

    async def fake_fetch_one(_q, values=None):
        return row if values and values.get("id") == row["id"] else None

    async def fake_exec(query: str, values):
        written.append(dict(values or {}))
        row.update(values)

    rec = _Recorder()
    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_exec)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", rec.record)
    monkeypatch.setattr(mod.destination_liveness, "retire_seed_for_dead_destination", rec.retire)
    result = asyncio.run(mod._refresh_external_seed_by_id(row["id"]))
    return result, written


def _extracted_at(written: List[Dict[str, Any]]) -> Optional[str]:
    """The `extracted_at` this refresh actually persisted, if any."""
    import json

    for values in written:
        payload = values.get("seed_data")
        if payload is None:
            continue
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        stamp = (payload or {}).get("snapshot", {}).get("extracted_at")
        if stamp:
            return str(stamp)
    return None


def _reaching(**snap):
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = DEST
        return _snapshot(**snap)
    return resolve


# ----------------------------------------------------------------- it clears the blocker

def test_a_real_extraction_clears_stale_snapshot_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the change, asserted through the GATE rather than the column.

    Asserting only that a key was written would pass for a value the gate cannot parse or
    still considers stale -- the two halves are in different modules and joined by nothing
    but this timestamp.
    """
    from services import external_referral_readiness as readiness
    from services.external_seed_audit import get_content_extracted_at

    row = _seed_row()
    row["seed_data"]["snapshot"]["extracted_at"] = _iso(days_ago=400)
    _, written = _run(monkeypatch, row, resolve=_reaching(price_amount=31.0))

    stamp = _extracted_at(written)
    assert stamp, "a successful refresh must record a content extraction"

    parsed = readiness._parse_timestamp(stamp)
    assert parsed is not None, f"the gate must be able to parse what we wrote: {stamp!r}"
    cutoff = datetime.now(timezone.utc) - timedelta(days=readiness.EXTERNAL_REFERRAL_STALE_DAYS)
    assert parsed > cutoff, "the freshly written stamp must read as fresh to the gate"

    assert get_content_extracted_at({}, {"extracted_at": stamp}) == stamp


def _iso(*, days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


@pytest.mark.parametrize(
    "snap,label",
    [
        ({"price_amount": 31.0}, "applied"),
        ({"price_amount": 28.0}, "unchanged"),
    ],
)
def test_a_price_we_actually_stored_counts_as_an_extraction(
    monkeypatch: pytest.MonkeyPatch, snap: Dict[str, Any], label: str
) -> None:
    """`unchanged` counts: we read the page and it still says what we store."""
    _, written = _run(monkeypatch, _seed_row(), resolve=_reaching(**snap))
    assert _extracted_at(written), f"{label} re-read the stored price"


def test_a_first_fill_counts_as_an_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _seed_row(price_amount=None, price_currency=None)
    _, written = _run(monkeypatch, row, resolve=_reaching(price_amount=28.0))
    assert _extracted_at(written)


# ------------------------------------------------------- and refuses to clear it otherwise

def test_a_cached_snapshot_fallback_never_clears_the_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_external_offer` swallows timeouts/TLS/robots and returns the CACHED row, so
    this success path runs having contacted nobody. Clearing a serving blocker there would
    present an untouched price as freshly verified."""
    async def resolve(*, observed=None, **kwargs):
        return _snapshot(price_amount=31.0)  # `observed` never populated

    result, written = _run(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] == "success"
    assert _extracted_at(written) is None


def test_a_read_of_a_url_we_do_not_serve_never_clears_the_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _seed_row(canonical_url="https://brand.com/products/toner-v2")
    _, written = _run(monkeypatch, row, resolve=_reaching(price_amount=31.0))
    assert _extracted_at(written) is None


@pytest.mark.parametrize(
    "snap,label",
    [
        ({"price_amount": None}, "unavailable -- no reading at all"),
        ({"price_amount": 0.0}, "skipped_non_positive -- a broken-offer shape"),
        ({"price_currency": None}, "skipped_incomplete_pair -- amount without a currency"),
        ({"price_currency": "KRW"}, "skipped_currency_mismatch -- needs a human"),
    ],
)
def test_a_price_we_refused_to_store_is_not_an_extraction(
    monkeypatch: pytest.MonkeyPatch, snap: Dict[str, Any], label: str
) -> None:
    """Every one of these keeps the PREVIOUS amount, so the number we quote was not re-read.

    Calling the row freshly extracted would vouch for a price this very refresh declined to
    trust -- and would clear a serving blocker to do it.
    """
    _, written = _run(monkeypatch, _seed_row(), resolve=_reaching(**snap))
    assert _extracted_at(written) is None, label


def test_a_dead_link_never_clears_the_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*, observed=None, **kwargs):
        raise ExternalOfferUnavailable(status_code=404, url=DEST)

    result, written = _run(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] != "success"
    assert _extracted_at(written) is None
