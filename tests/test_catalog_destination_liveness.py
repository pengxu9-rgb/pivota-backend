"""Catalog destination liveness: the seed sweep's rules, applied to rows with no seed.

No network and no database: the writer runs against a recording fake, the sweep against canned
storefront responses. The real SQL is exercised in tests/test_catalog_destination_liveness_postgres.py.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest

from services import catalog_destination_liveness as catalog
from services import crawl_politeness as cp
from services import external_seed_destination_liveness as liveness

NOW = datetime(2026, 10, 10, 4, 10, tzinfo=timezone.utc)
PK = "ext:retailer:ff9192d8a9e002262a4cf78796095b02"
URL = "https://openthebeauty.com/products/the-face-shop-dr-belmeur-mild-derma-body-wash"
DEAD = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, URL, corroborated=True)
REFRESH_DEAD = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, URL, corroborated=False)
LIVE = liveness.DestinationObservation(liveness.VERDICT_LIVE, None, None, "listed in products.json")
BLIND = liveness.DestinationObservation(liveness.VERDICT_UNVERIFIABLE, None, None, "catalogue bot_challenge")


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    cp.reset_for_tests()

    async def _allow(url, *, user_agent, max_wait=None):
        return None

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(cp, "before_request", _allow)
    monkeypatch.setattr(cp, "note_response", lambda *a, **k: None)
    monkeypatch.setattr(liveness.asyncio, "sleep", _no_sleep)
    yield
    cp.reset_for_tests()


class _FakeDb:
    def __init__(self, row: Optional[Dict[str, Any]]) -> None:
        self.row = row
        self.executed: List[Any] = []

    async def fetch_one(self, query, params=None):
        self.executed.append((query, params))
        return self.row

    async def execute(self, query, params=None):
        self.executed.append((query, params))

    async def fetch_all(self, query, params=None):
        self.executed.append((query, params))
        if "RETURNING" in str(query).upper():
            return [{"product_key": PK, "offer_id": "offer::1"}]
        return []


def _flat(sql) -> str:
    return " ".join(str(sql).split())


def _statements(db, needle):
    return [(sql, p) for sql, p in db.executed if needle in _flat(sql)]


def _record(monkeypatch, row, observation, *, suppress, now=NOW):
    db = _FakeDb(row)
    monkeypatch.setattr(catalog, "database", db)
    result = asyncio.run(catalog.record_catalog_observation(PK, URL, observation, now=now, suppress=suppress))
    return result, db


# ------------------------------------------------------------------------------- the writer

def test_a_first_corroborated_death_hides_the_row_under_the_lanes_pending_reason(monkeypatch):
    result, db = _record(monkeypatch, None, DEAD, suppress=True)
    assert result["failure_streak"] == 1 and result["pending_suppressed"] == 1
    upsert = _statements(db, "INSERT INTO catalog_destination_liveness")[0][1]
    assert upsert["streak"] == 1 and upsert["corroborated_dead_at"] == NOW and upsert["reached_origin"] is True
    hide = _statements(db, "UPDATE catalog_products SET suppressed_at = :stamp")
    assert hide and hide[0][1]["reason"] == "catalog_destination_dead_pending"
    assert hide[0][1]["product_keys"] == [PK]


def test_without_suppress_it_only_observes(monkeypatch):
    result, db = _record(monkeypatch, None, DEAD, suppress=False)
    assert result["failure_streak"] == 1
    assert _statements(db, "UPDATE catalog_products") == []


def test_an_uncorroborated_404_never_acts_even_on_a_streak(monkeypatch):
    row = {"destination_failure_streak": 1, "destination_corroborated_dead_at": NOW - timedelta(days=2),
           "destination_verdict": "dead_404", "destination_checked_at": NOW - timedelta(days=2)}
    result, db = _record(monkeypatch, row, REFRESH_DEAD, suppress=True)
    assert result["failure_streak"] == 1
    assert _statements(db, "UPDATE catalog_products") == []


def test_the_second_corroborated_death_a_day_later_withdraws_under_the_final_reason(monkeypatch):
    row = {"destination_failure_streak": 1, "destination_corroborated_dead_at": NOW - timedelta(hours=24),
           "destination_verdict": "dead_404", "destination_checked_at": NOW - timedelta(hours=24)}
    result, db = _record(monkeypatch, row, DEAD, suppress=True)
    assert result["failure_streak"] == 2 and result["retire"] is True and result["withdrawn"] == 1
    final = _statements(db, "suppression_reason = CAST(:final_reason AS text), updated_at")
    assert final and final[0][1]["final_reason"] == "catalog_destination_dead"
    assert final[0][1]["pending_reason"] == "catalog_destination_dead_pending"
    assert "pending_suppressed" not in result


def test_a_second_look_inside_the_gap_does_not_withdraw(monkeypatch):
    row = {"destination_failure_streak": 1, "destination_corroborated_dead_at": NOW - timedelta(hours=2),
           "destination_verdict": "dead_404", "destination_checked_at": NOW - timedelta(hours=2)}
    result, db = _record(monkeypatch, row, DEAD, suppress=True)
    assert result["failure_streak"] == 1 and result["retire"] is False
    assert _statements(db, "CAST(:final_reason AS text)") == []


@pytest.mark.parametrize("suppress", [True, False])
def test_a_live_answer_after_a_streak_lifts_the_pending_reason_armed_or_not(monkeypatch, suppress):
    row = {"destination_failure_streak": 1, "destination_corroborated_dead_at": NOW - timedelta(hours=20),
           "destination_verdict": "dead_404", "destination_checked_at": NOW - timedelta(hours=20)}
    result, db = _record(monkeypatch, row, LIVE, suppress=suppress)
    assert result["failure_streak"] == 0 and result["pending_lifted"] == 1
    lift = _statements(db, "UPDATE catalog_products SET suppressed_at = NULL")
    assert lift and lift[0][1]["reason"] == "catalog_destination_dead_pending"
    assert _statements(db, "INSERT INTO catalog_destination_liveness")[0][1]["corroborated_dead_at"] is None


def test_an_unreadable_host_stamps_only_the_attempt(monkeypatch):
    row = {"destination_failure_streak": 1, "destination_corroborated_dead_at": NOW - timedelta(hours=20),
           "destination_verdict": "dead_404", "destination_checked_at": NOW - timedelta(hours=20)}
    result, db = _record(monkeypatch, row, BLIND, suppress=True)
    params = _statements(db, "INSERT INTO catalog_destination_liveness")[0][1]
    assert params["reached_origin"] is False and params["streak"] == 1
    assert params["corroborated_dead_at"] == row["destination_corroborated_dead_at"]
    assert params["stamp"] == NOW, "the attempt clock must move, or the host heads the queue forever"
    assert _statements(db, "UPDATE catalog_products") == []
    assert result["failure_streak"] == 1


def test_the_upsert_guards_verdict_status_and_origin_clock_in_the_statement():
    sql = _flat(catalog.UPSERT_SQL)
    for column in ("destination_verdict", "destination_http_status", "destination_checked_at"):
        assert f"{column} = CASE WHEN CAST(:reached_origin AS BOOLEAN) THEN EXCLUDED.{column} ELSE l.{column} END" in sql
    assert "last_attempt_at = EXCLUDED.last_attempt_at" in sql


def test_the_queue_spells_out_exactly_the_confirmed_dead_verdicts():
    spelled = re.search(r"destination_verdict IN \(([^)]*)\)", catalog.CANDIDATES_SQL).group(1)
    assert {v.strip().strip("'") for v in spelled.split(",")} == set(liveness.CONFIRMED_DEAD_VERDICTS)


def test_the_job_applies_exactly_one_migration_file(monkeypatch):
    ran: List[str] = []

    class _Db:
        async def execute(self, sql, params=None):
            ran.append(_flat(sql))

    asyncio.run(catalog.ensure_table(db=_Db()))
    # the splitter keeps a statement's leading comments, so look for the statement itself
    assert sum("CREATE TABLE IF NOT EXISTS catalog_destination_liveness (" in s for s in ran) == 1
    assert sum("CREATE INDEX IF NOT EXISTS idx_catalog_destination_liveness_" in s for s in ran) == 2


# ------------------------------------------------------------------------------- the sweep

class _Client:
    def __init__(self, catalogue: Optional[List[str]], pdp: Dict[str, httpx.Response]) -> None:
        self.catalogue = catalogue
        self.pdp = pdp
        self.urls: List[str] = []

    async def get(self, url, headers=None):
        self.urls.append(url)
        if "/products.json" in url:
            if self.catalogue is None:
                resp = httpx.Response(429, headers={"cf-mitigated": "challenge"})
            else:
                page = 1 if "page=1" in url else 2
                resp = httpx.Response(200, json={"products": [{"handle": h} for h in (self.catalogue if page == 1 else [])]})
        else:
            resp = self.pdp[url]
        resp.request = httpx.Request("GET", url)
        return resp

    async def aclose(self):
        return None


def _sweep(monkeypatch, rows, client, **kwargs):
    recorded = []

    async def fake_candidates(limit):
        return rows

    async def fake_record(product_key, canonical_url, observation, *, now=None, suppress=False):
        recorded.append((product_key, observation, suppress))
        return {}

    monkeypatch.setattr(catalog, "get_candidates", fake_candidates)
    monkeypatch.setattr(catalog, "record_catalog_observation", fake_record)
    summary = asyncio.run(catalog.run_catalog_destination_sweep(client=client, **kwargs))
    return summary, recorded


def test_a_listed_handle_is_live_and_an_unlisted_one_is_probed_and_corroborated(monkeypatch):
    rows = [
        {"product_key": "pk_live", "canonical_url": "https://shop.example/products/kept"},
        {"product_key": "pk_gone", "canonical_url": "https://shop.example/products/gone"},
    ]
    client = _Client(["kept"], {"https://shop.example/products/gone": httpx.Response(404)})
    summary, recorded = _sweep(monkeypatch, rows, client, suppress=True)
    by_key = {pk: (obs, sup) for pk, obs, sup in recorded}
    assert by_key["pk_live"][0].verdict == liveness.VERDICT_LIVE
    assert by_key["pk_gone"][0].verdict == liveness.VERDICT_DEAD_404
    assert by_key["pk_gone"][0].corroborated is True
    assert {sup for _, sup in by_key.values()} == {True}
    assert summary["listed"] == 1 and summary["probed"] == 1 and summary["dead_links_found"] == 1
    assert "https://shop.example/products/kept" not in client.urls, "a listed handle costs no PDP request"


def test_an_unreadable_host_records_attempts_and_probes_nothing(monkeypatch):
    rows = [{"product_key": "pk_a", "canonical_url": "https://blocked.example/products/a"}]
    client = _Client(None, {})
    summary, recorded = _sweep(monkeypatch, rows, client)
    assert [(pk, obs.verdict) for pk, obs, _ in recorded] == [("pk_a", liveness.VERDICT_UNVERIFIABLE)]
    assert summary["hosts_unverifiable"] == 1 and summary["probed"] == 0
    assert all("/products/a" not in u or "products.json" in u for u in client.urls)


def test_the_sweep_observes_unless_asked_to_suppress(monkeypatch):
    rows = [{"product_key": "pk_gone", "canonical_url": "https://shop.example/products/gone"}]
    client = _Client([], {"https://shop.example/products/gone": httpx.Response(404)})
    summary, recorded = _sweep(monkeypatch, rows, client)
    assert recorded[0][2] is False and summary["suppress"] is False
