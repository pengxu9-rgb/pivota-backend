"""
Every column the referral gate reads off a seed row is loaded by every serving SELECT.

THE DEFECT. `should_block_external_referral_runtime` -> `evaluate_external_referral_seed` reads
the ROW it is handed, not the table. The two serving SELECTs (`fetch_external_seed_rows` and the
agent_api PDP-by-id loader, plus its TEXT fallback) listed their columns by hand and never added
the destination-liveness columns from migration 200. The gate therefore saw
`destination_checked_at` as absent on every served row:

  * every seed was `destination_never_verified`, so no seed's catalog facts were ever accepted
    (prod 2026-09-25: 0 accepted with the serving list, 1,892 with SELECT *);
  * `destination_verdict` / `destination_failure_streak` were absent, so `destination_dead`
    could never fire at serve time.

DERIVED, NOT COPIED. The column set is recorded from what the gate actually reads — every key it
touches on the row, across a live, a confirmed-dead, a never-verified and a stale row — and each
SELECT's list is parsed from the SQL it really sends. A new gate read that the SELECT does not
load fails here without anyone having to remember to update a list.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pytest

import services.external_referral_readiness as readiness
from services.external_seed_search import fetch_external_seed_rows


_NOW = datetime.now(timezone.utc)


def _base_row(**overrides: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "id": "seed:test:1",
        "external_product_id": "brand:1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": "https://brand.example/products/serum",
        "canonical_url": "https://brand.example/products/serum",
        "domain": "brand.example",
        "title": "Serum",
        "image_url": "https://brand.example/serum.jpg",
        "price_amount": 20.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {
            "title": "Serum",
            "snapshot": {
                "extracted_at": _NOW.isoformat(),
                "title": "Serum",
                "description": "A serum.",
                "canonical_url": "https://brand.example/products/serum",
                "variants": [{"sku": "S1", "price": "20.00", "currency": "USD", "available": True}],
            },
        },
        "status": "active",
        "notes": None,
        "created_by_employee_id": None,
        "attached_product_key": None,
        "attached_variant_id": None,
        "seller_ref": None,
        "seed_kind": None,
        "destination_checked_at": _NOW,
        "destination_http_status": 200,
        "destination_verdict": "live",
        "destination_failure_streak": 0,
        "created_at": _NOW - timedelta(days=30),
        "updated_at": _NOW,
    }
    row.update(overrides)
    return row


# One row per gate branch that reads a different set of columns. The dead row is the only one
# that reaches `destination_http_status`; the never-verified and stale rows take the other arms
# of the destination-age check; the row with no canonical_url walks the destination_url fallback.
_SCENARIOS = {
    "live": _base_row(),
    "dead": _base_row(destination_verdict="dead_404", destination_failure_streak=3, destination_http_status=404),
    "never_verified": _base_row(
        destination_checked_at=None, destination_verdict=None, destination_http_status=None
    ),
    "stale": _base_row(destination_checked_at=_NOW - timedelta(days=60)),
    "no_canonical": _base_row(canonical_url=None),
}

_ALLOWED = ["brand.example"]


class _RecordingRow(dict):
    def __init__(self, data: Dict[str, Any], seen: Set[str]):
        super().__init__(data)
        self._seen = seen

    def get(self, key: Any, default: Any = None) -> Any:
        self._seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self._seen.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        self._seen.add(key)
        return super().__contains__(key)


def _gate_reads(monkeypatch: pytest.MonkeyPatch) -> Set[str]:
    seen: Set[str] = set()
    # `_row_to_dict` is the gate's single entry point for the row: both the audit and the
    # liveness/freshness checks read the dict it returns.
    monkeypatch.setattr(readiness, "_row_to_dict", lambda row: _RecordingRow(dict(row or {}), seen))

    async def _run() -> Dict[str, Any]:
        return {
            name: await readiness.evaluate_external_referral_seed(row, matched_via="test", allowed_domains=_ALLOWED)
            for name, row in _SCENARIOS.items()
        }

    statuses = asyncio.run(_run())
    monkeypatch.undo()

    # The recorder must have seen the reads, and the scenarios must really have reached the
    # branches they exist for — otherwise an empty or partial read set passes vacuously.
    assert "seed_data" in seen and "id" in seen, sorted(seen)
    assert "destination_dead" in statuses["dead"].blocker_anomaly_types
    assert "destination_never_verified" in statuses["never_verified"].blocker_anomaly_types
    assert "destination_stale" in statuses["stale"].blocker_anomaly_types
    assert "destination_never_verified" not in statuses["live"].blocker_anomaly_types
    return {key for key in seen if isinstance(key, str)}


def _split_top_level(text: str) -> List[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _selected_columns(sql: str) -> Set[str]:
    match = re.search(r"\bSELECT\b(.*?)\bFROM\s+external_product_seeds\b", sql, re.IGNORECASE | re.DOTALL)
    assert match, sql
    columns: Set[str] = set()
    for item in _split_top_level(match.group(1)):
        item = item.strip()
        assert item != "*", "a SELECT * cannot drift; this test pins explicit column lists"
        alias = re.search(r"\bAS\s+(\w+)\s*$", item, re.IGNORECASE)
        columns.add(alias.group(1) if alias else item)
    return columns


class _CapturingDatabase:
    def __init__(self, *, fail_first_with: Optional[str] = None):
        self.sql: List[str] = []
        self._fail_first_with = fail_first_with

    def _record(self, sql: str) -> None:
        self.sql.append(str(sql))
        if self._fail_first_with and len(self.sql) == 1:
            raise RuntimeError(self._fail_first_with)

    async def fetch_all(self, sql: str, values: Any = None) -> List[Any]:
        self._record(sql)
        return []

    async def fetch_one(self, sql: str, values: Any = None) -> Any:
        self._record(sql)
        return None


def _search_lane_sql() -> str:
    db = _CapturingDatabase()
    asyncio.run(
        fetch_external_seed_rows(
            database=db, market="US", query="serum", limit=5, only_unattached=False, include_total_count=False
        )
    )
    assert len(db.sql) == 1
    return db.sql[0]


def _pdp_lane_sql(monkeypatch: pytest.MonkeyPatch, *, text_fallback: bool) -> str:
    agent_api = importlib.import_module("routes.agent_api")
    db = _CapturingDatabase(
        fail_first_with="operator does not exist: text ->> unknown (UndefinedFunction)" if text_fallback else None
    )
    monkeypatch.setattr(agent_api, "database", db)
    result = asyncio.run(agent_api._load_external_seed_product_by_product_id(req=None, product_id="brand:1"))
    assert result is None
    assert len(db.sql) == (2 if text_fallback else 1)
    return db.sql[-1]


def _lanes(monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    return {
        "fetch_external_seed_rows": _search_lane_sql(),
        "agent_api PDP-by-id": _pdp_lane_sql(monkeypatch, text_fallback=False),
        "agent_api PDP-by-id (TEXT seed_data fallback)": _pdp_lane_sql(monkeypatch, text_fallback=True),
    }


def test_every_serving_select_loads_every_column_the_gate_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = _gate_reads(monkeypatch)
    for lane, sql in _lanes(monkeypatch).items():
        missing = sorted(reads - _selected_columns(sql))
        assert not missing, f"{lane} does not SELECT columns the referral gate reads: {missing}"


def test_the_gate_answers_the_same_on_a_served_row_as_on_the_full_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # The behavioural half: project each scenario row onto exactly what each lane selects and
    # check the gate's verdict does not change. With the pre-fix list the live row came back
    # `destination_never_verified` and the dead row lost `destination_dead`.
    for lane, sql in _lanes(monkeypatch).items():
        columns = _selected_columns(sql)
        for name, full_row in _SCENARIOS.items():
            served_row = {key: value for key, value in full_row.items() if key in columns}

            async def _both() -> Any:
                return (
                    await readiness.evaluate_external_referral_seed(full_row, matched_via="test", allowed_domains=_ALLOWED),
                    await readiness.evaluate_external_referral_seed(served_row, matched_via="test", allowed_domains=_ALLOWED),
                )

            full_status, served_status = asyncio.run(_both())
            assert served_status.blocker_anomaly_types == full_status.blocker_anomaly_types, (lane, name)
