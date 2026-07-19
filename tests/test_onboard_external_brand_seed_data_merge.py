"""Regression for #1516: the onboard seed UPSERT must MERGE seed_data on
re-run, not REPLACE it.

Incident: `_upsert_seed`'s ON CONFLICT clause rebuilt seed_data from the cohort
JSON and stored `seed_data=EXCLUDED.seed_data`. A serve-mode re-run therefore
wiped enrichment keys written to seed_data AFTER onboarding (curated
`electronics_meta` was erased from 3 HoverAir rows). The catalog_products
product_payload copy only survived because that mirror INSERT is ON CONFLICT DO
NOTHING.

Fix: the conflict update is now a per-top-level-key jsonb merge
    seed_data = COALESCE(external_product_seeds.seed_data, '{}') || EXCLUDED.seed_data
so stored-only enrichment keys survive while cohort-carried keys win.

There is no Postgres in CI (the suite runs on SQLite, which has no jsonb `||`),
so we assert two things:
  1. structural — the emitted SQL uses the merge form, never the bare replace;
  2. behavioural — a faithful Python model of Postgres `||` (a shallow, per-top-
     level-key dict merge, EXCLUDED-wins) preserves enrichment AND refreshes
     cohort keys. If someone reverts to `seed_data=EXCLUDED.seed_data`, (1) fails.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))

    async def fetch_one(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return None


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(onboard, "database", db)
    return db


def _seed_upsert_sql(db):
    for sql, _params in db.calls:
        if "INSERT INTO external_product_seeds" in sql:
            return sql
    raise AssertionError("no external_product_seeds upsert was emitted")


async def test_seed_upsert_merges_seed_data_not_replaces(fake_db):
    """The conflict update must merge (`|| EXCLUDED.seed_data` over a
    COALESCE'd stored value), never bare-replace with EXCLUDED.seed_data."""
    await onboard._upsert_seed(
        {"external_product_id": "hoverair_x1", "title": "HOVERAir X1", "price_amount": 349.0},
        seller_ref="merch_obs_deadbeefdeadbeef",
        seed_kind="self",
    )
    sql = _seed_upsert_sql(fake_db)
    normalized = " ".join(sql.split())

    # The merge form is present...
    assert "EXCLUDED.seed_data" in normalized
    assert "|| EXCLUDED.seed_data" in normalized
    assert "COALESCE(external_product_seeds.seed_data" in normalized

    # ...and the bare-replace form is gone. Strip SQL line comments first so a
    # documentation mention of the old form can't mask a real regression.
    code_only = "\n".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )
    code_only = " ".join(code_only.split())
    assert not re.search(r"seed_data\s*=\s*EXCLUDED\.seed_data(?!\s*[|])", code_only), (
        "conflict update bare-replaces seed_data; enrichment keys will be wiped on re-run"
    )


def _pg_jsonb_concat(stored: dict, excluded: dict) -> dict:
    """Faithful model of Postgres `stored || excluded` for jsonb objects:
    a shallow union where EXCLUDED wins per top-level key."""
    merged = dict(stored)
    merged.update(excluded)
    return merged


def test_merge_semantics_preserve_enrichment_and_refresh_cohort_keys():
    """Behavioural: onboard writes a snapshot; enrichment adds electronics_meta
    later; a re-run refreshes snapshot but must keep electronics_meta."""
    # 1st onboard writes the cohort-derived seed_data.
    after_onboard = _pg_jsonb_concat({}, {"snapshot": {"title": "HOVERAir X1"}})

    # Curated enrichment adds a top-level key the cohort never carries.
    after_enrichment = dict(after_onboard)
    after_enrichment["electronics_meta"] = {"battery_wh": 8.2, "weight_g": 125}

    # 2nd onboard (serve-mode re-run) with a refreshed cohort snapshot.
    excluded = {"snapshot": {"title": "HOVERAir X1 (2026)"}}
    after_rerun = _pg_jsonb_concat(after_enrichment, excluded)

    # Enrichment survives...
    assert after_rerun["electronics_meta"] == {"battery_wh": 8.2, "weight_g": 125}
    # ...and the cohort-carried key is refreshed (EXCLUDED wins per key).
    assert after_rerun["snapshot"]["title"] == "HOVERAir X1 (2026)"


async def test_captured_bind_payload_is_valid_json(fake_db):
    """Sanity: the `data` bind param is still JSON-serialised cohort seed_data
    (the value that becomes EXCLUDED.seed_data)."""
    await onboard._upsert_seed(
        {"external_product_id": "x1", "title": "T", "price_amount": 1.0},
        seller_ref=None,
        seed_kind=None,
    )
    _sql, params = next(
        (s, p) for s, p in fake_db.calls if "INSERT INTO external_product_seeds" in s
    )
    parsed = json.loads(params["data"])
    assert "snapshot" in parsed
