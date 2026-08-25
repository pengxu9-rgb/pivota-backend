"""A refresh must be able to SEE a dead link — and a 404 must never look like freshness.

The chain this pins, end to end (docs/external-seed-dead-pdp-link-audit.md §4.2):

    _fetch_html                 raise_for_status() -> a bare httpx.HTTPStatusError
    resolve_external_offer      except Exception: return the CACHED snapshot
    _refresh_external_seed_by_id  writes that snapshot, sets updated_at = NOW()
    stale_snapshot              reads updated_at -> the row is now FRESH

So fetching a 404 made a dead seed look *newer*. Three changes break it, and each has a test
here: the typed `ExternalOfferUnavailable`, the `raise_on_unavailable` opt-in that stops the
cached-snapshot fallback from hiding it, and a refresh that records the observation instead of
returning an anonymous `degraded`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from services import external_seed_destination_liveness as liveness
from services.external_offers_service import ExternalOfferUnavailable


def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_dest_1",
        "external_product_id": "ext_dest_1",
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
    }
    row.update(overrides)
    return row


def _snapshot(**overrides: Any) -> SimpleNamespace:
    base = {
        "canonical_url": "https://brand.com/products/toner",
        "domain": "brand.com",
        "title": "Toner",
        "image_url": "https://cdn.example.com/img.jpg",
        "price_amount": 28.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "fetched_at": None,
        "evidence": {},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Recorder:
    """Captures what the refresh asked the liveness module to record."""

    def __init__(self) -> None:
        self.observations: List[liveness.DestinationObservation] = []
        self.retired: List[str] = []
        self.streak = 0

    async def record(self, seed_id, observation, *, now=None):
        self.observations.append(observation)
        if observation.confirmed_dead:
            self.streak += 1
        elif observation.reached_origin:
            self.streak = 0
        return {
            "seed_id": seed_id,
            "verdict": observation.verdict,
            "failure_streak": self.streak,
            "retire": liveness.should_retire(observation.verdict, self.streak),
        }

    async def retire(self, seed_id, observation, *, now=None):
        self.retired.append(seed_id)
        return {"seed_id": seed_id, "retired": True}


def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stored_row: Dict[str, Any],
    *,
    resolve,
    recorder: Optional[_Recorder] = None,
) -> Dict[str, Any]:
    import routes.employee_products as mod

    recorder = recorder or _Recorder()

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == stored_row["id"]:
            return stored_row
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        stored_row.update(values)

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(
        mod.destination_liveness, "record_destination_observation", recorder.record
    )
    monkeypatch.setattr(
        mod.destination_liveness, "retire_seed_for_dead_destination", recorder.retire
    )

    result = asyncio.run(mod._refresh_external_seed_by_id(stored_row["id"]))
    result["_recorder"] = recorder
    return result


# --------------------------------------------------------------------- the dead-link path

def test_a_404_is_recorded_as_a_dead_destination_not_as_an_anonymous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(**kwargs):
        assert kwargs.get("raise_on_unavailable") is True, (
            "without this the cached-snapshot fallback swallows the 404 and the refresh "
            "reports success on a dead link"
        )
        raise ExternalOfferUnavailable(
            status_code=404, url="https://brand.com/products/toner"
        )

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)

    assert result["status"] == "degraded"
    assert "http 404" in result["error"]
    recorder = result["_recorder"]
    assert [o.verdict for o in recorder.observations] == [liveness.VERDICT_DEAD_404]
    assert recorder.retired == [], "one observation is never enough to retire"


def test_a_404_does_not_touch_the_seed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ORIGINAL DEFECT. The refresh used to write the cached snapshot and bump
    `updated_at`, so probing a dead URL made the row look freshly verified."""
    row = _seed_row()
    before = dict(row)

    async def resolve(**kwargs):
        raise ExternalOfferUnavailable(status_code=404, url=row["destination_url"])

    _run_refresh(monkeypatch, row, resolve=resolve)

    for column in ("canonical_url", "price_amount", "availability", "seed_data"):
        assert row[column] == before[column], f"{column} must not move on a dead destination"


def test_the_refresh_records_a_dead_destination_but_never_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retirement belongs to the sweep, which has stage-1 corroboration. This path does not.

    A refresh sees ONE url and ONE status code. It has not read the brand catalogue, so it
    cannot tell a deleted product from a WAF that answers 404 to an unfamiliar client — and
    repetition does not help, because a WAF policy outlasts a dead product. This used to call
    `retire_seed_for_dead_destination` directly, which also meant it was the one retirement
    path with NO feature flag in front of it: `EXTERNAL_SEED_DESTINATION_SWEEP_RETIRE` gates
    the sweep only, and `jobs/external_referral_refresh` batch-calls this function.
    """
    recorder = _Recorder()
    recorder.streak = 1  # yesterday's sweep already saw it dead

    async def resolve(**kwargs):
        raise ExternalOfferUnavailable(status_code=410, url="https://brand.com/products/toner")

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve, recorder=recorder)

    assert [o.verdict for o in recorder.observations] == [liveness.VERDICT_DEAD_404]
    assert recorder.retired == [], "the refresh may observe; it may not withdraw"
    assert "retired" not in result["destination_refresh"]


def test_the_refresh_marks_its_observations_uncorroborated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is what `record_destination_observation` keys the streak on.

    Asserting "did not retire" alone would still pass if the route regained the retire call
    while the recorder happened to report a streak below the threshold.
    """

    async def resolve(**kwargs):
        raise ExternalOfferUnavailable(status_code=404, url="https://brand.com/products/toner")

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    observations = result["_recorder"].observations

    assert [o.corroborated for o in observations] == [False]


def test_a_seed_whose_served_url_differs_from_the_fetched_one_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict must describe the link we hand out, not a legacy column.

    This function fetches `destination_url` but the gate and the sweep both resolve a seed to
    `canonical_url or destination_url` — and this same function rewrites `canonical_url` from
    the fetched page, so the two drift apart by design. Recording a 404 on a superseded
    `destination_url` would mark a seed dead whose served link is healthy.
    """
    row = _seed_row()
    row["canonical_url"] = "https://brand.com/products/toner-2024"
    row["destination_url"] = "https://brand.com/products/toner-legacy"

    async def resolve(**kwargs):
        raise ExternalOfferUnavailable(
            status_code=404, url="https://brand.com/products/toner-legacy"
        )

    result = _run_refresh(monkeypatch, row, resolve=resolve, recorder=_Recorder())

    assert result["destination_refresh"] == {
        "status": "not_observed",
        "reason": "not_the_served_url",
    }
    assert result["_recorder"].observations == []


@pytest.mark.parametrize("status_code", [403, 429, 500, 503])
def test_an_origin_that_refuses_or_fails_is_unverifiable_never_dead(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """A 403/429/5xx says something about the SERVER, not about the product."""

    async def resolve(**kwargs):
        raise ExternalOfferUnavailable(
            status_code=status_code, url="https://brand.com/products/toner"
        )

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    recorder = result["_recorder"]

    assert [o.verdict for o in recorder.observations] == [liveness.VERDICT_UNVERIFIABLE]
    assert recorder.retired == []


def test_a_transport_failure_records_nothing_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """We never reached the origin, so there is no observation to make.

    Recording `unverifiable` here would be harmless but wrong in a way that matters: it would
    overwrite a real verdict from the sweep with a row about our own network.
    """

    async def resolve(**kwargs):
        raise TimeoutError("connect timeout")

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    recorder = result["_recorder"]

    assert result["status"] == "degraded"
    assert "snapshot_failed" in result["error"]
    assert recorder.observations == []


# --------------------------------------------------------------------- the live path

def test_a_successful_fetch_records_a_live_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot()

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    recorder = result["_recorder"]

    assert result["status"] == "success"
    assert [o.verdict for o in recorder.observations] == [liveness.VERDICT_LIVE]
    assert result["destination_refresh"]["verdict"] == liveness.VERDICT_LIVE


def test_a_redirect_off_the_product_is_seen_even_though_it_answered_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 301 onto a collection page is a broken link that never raises.

    92 of the 490 delisted URLs measured were exactly this — `kylies-looks`,
    `collections/brush-sets` — so a refresh that only watched for exceptions would call every
    one of them healthy.
    """

    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/collections/brush-sets"
        return _snapshot()

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    recorder = result["_recorder"]

    assert [o.verdict for o in recorder.observations] == [
        liveness.VERDICT_REDIRECTED_OFF_PRODUCT
    ]


def test_a_resolve_that_reports_no_observation_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_external_offer` returns the CACHED snapshot when it did not fetch.

    Nothing left the process, so there is nothing to record — and stamping
    `destination_checked_at` from a cache read is precisely the lie this whole change removes.
    """

    async def resolve(*, observed=None, **kwargs):
        return _snapshot()  # never touches `observed`

    result = _run_refresh(monkeypatch, _seed_row(), resolve=resolve)
    recorder = result["_recorder"]

    assert result["status"] == "success"
    assert recorder.observations == []
    assert result["destination_refresh"] == {
        "status": "not_observed",
        "reason": "no_origin_response",
    }


def test_a_200_on_a_url_we_do_not_serve_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The served-URL guard on the SUCCESS path, not just the unavailable one.

    The two paths carry the same guard and a mutant that removes only one of them survived a
    battery that exercised only the other — the exception path. A 200 is the more dangerous
    direction anyway: it is what produces `redirected_off_product`, a confirmed-dead verdict,
    from a legacy `destination_url` that 301s to a collection while the served `canonical_url`
    is perfectly healthy.
    """
    row = _seed_row()
    row["canonical_url"] = "https://brand.com/products/toner-2024"
    row["destination_url"] = "https://brand.com/products/toner-legacy"

    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/collections/all"
        return _snapshot()

    result = _run_refresh(monkeypatch, row, resolve=resolve, recorder=_Recorder())

    assert result["status"] == "success"
    assert result["destination_refresh"] == {
        "status": "not_observed",
        "reason": "not_the_served_url",
    }
    assert result["_recorder"].observations == []


def test_a_200_on_the_url_we_DO_serve_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard must not be so tight that the ordinary case stops being observed."""
    row = _seed_row()
    row["canonical_url"] = "https://brand.com/products/toner"
    row["destination_url"] = "https://brand.com/products/toner"

    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot()

    result = _run_refresh(monkeypatch, row, resolve=resolve, recorder=_Recorder())

    assert [o.verdict for o in result["_recorder"].observations] == [liveness.VERDICT_LIVE]


def test_a_trailing_slash_is_not_a_different_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two columns are written by different paths and differ cosmetically constantly."""
    row = _seed_row()
    row["canonical_url"] = "https://brand.com/products/toner/"
    row["destination_url"] = "https://brand.com/products/toner"

    async def resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = "https://brand.com/products/toner"
        return _snapshot()

    result = _run_refresh(monkeypatch, row, resolve=resolve, recorder=_Recorder())

    assert [o.verdict for o in result["_recorder"].observations] == [liveness.VERDICT_LIVE]
