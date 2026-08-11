"""The backfill SCRIPT's drive loop must not strip the overlays it rewrites.

WHY THIS FILE EXISTS. `tests/test_backfill_agent_pdp_view.py` is named after the
script but imports `services.agent_pdp_view_assembler` and exercises the pure
assembly helpers. The script's own fetch->assemble->upsert loop had no test at
all, and that is where the defect lived:

    row = assemble_row(content_key=..., products=..., skus=..., offers=...,
                       external_seed=..., refresh_source=...)

`evidence`, `enrichment` and `seller_trust_by_id` were left at their None
defaults, and UPSERT_SQL assigns EVERY column from EXCLUDED on conflict. So
`--apply` did not merely fail to propagate enrichment — it NULLed
`bullet_points`, `usage_scenarios`, `evidence_profile`, `required_disclaimers`
and the enrichment-derived `description` on every row it touched that had them.
The stranded-enrichment backfill (workstream 1) points this script at the
enriched cohort, which is exactly the set that would have been wiped.

WHAT IS ASSERTED, and why it is not a textual pin: the test drives `_drive()`
with a product that HAS an enrichment overlay and asserts the row handed to the
UPSERT still carries it. Any future edit that rebuilds a row without the
overlays — a reintroduced local `assemble_row` call, a new "fast path", a
refactor that drops a kwarg — fails here regardless of how it is spelled.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_agent_pdp_view as script  # noqa: E402
from services import agent_pdp_view_assembler as apv  # noqa: E402

_CONTENT_KEY = "ck-enriched-1"

_PRODUCT = {
    "product_key": "pk-1",
    "merchant_id": "m1",
    "platform": "shopify",
    "source_product_id": "sp-1",
    "title": "Glow Serum",
    "description": "The raw storefront description, which the overlay replaces.",
    "brand": "AuraGlow",
}

# Shape mirrors db/product_enrichment.py's overlay columns.
_ENRICHMENT = {
    "merchant_id": "m1",
    "platform": "shopify",
    "platform_product_id": "sp-1",
    "geo_code": "default",
    "description_markdown": "Curated Pivota copy for the agent PDP surface.",
    "bullet_points": ["Niacinamide 5%", "Fragrance free"],
    "usage_scenarios": ["Morning routine, before SPF"],
    "updated_by_employee_id": "emp-1",
}


class _RecordingDB:
    """Captures upserts; `is_connected` keeps _drive from opening a real pool."""

    is_connected = True

    def __init__(self, content_keys: List[str]) -> None:
        self._content_keys = content_keys
        self.executes: List[Dict[str, Any]] = []

    async def fetch_all(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, str]]:
        return [{"content_key": ck} for ck in self._content_keys]

    async def execute(self, sql: str, params: Dict[str, Any]) -> None:
        self.executes.append({"sql": sql, "params": params})


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> _RecordingDB:
    """One enriched content_key, all source reads stubbed, overlay present."""
    db = _RecordingDB([_CONTENT_KEY])

    async def fake_products(content_key: str, *, db: Any = None):
        return [dict(_PRODUCT)]

    async def fake_empty_list(product_keys, *, db: Any = None):
        return []

    async def fake_seed(product_keys, *, db: Any = None):
        return None

    async def fake_evidence(product_keys, *, db: Any = None):
        # Shape mirrors fetch_evidence_for_keys' return: assemble_row reads
        # `.get("evidence_profile")` / `.get("required_disclaimers")` off it.
        return {
            "evidence_profile": {"substantiated_claims": [{"claim": "fragrance free"}]},
            "required_disclaimers": None,
        }

    async def fake_enrichment(products):
        return dict(_ENRICHMENT)

    monkeypatch.setattr(apv, "fetch_products_for_key", fake_products)
    monkeypatch.setattr(apv, "fetch_skus_for_keys", fake_empty_list)
    monkeypatch.setattr(apv, "fetch_offers_for_keys", fake_empty_list)
    monkeypatch.setattr(apv, "fetch_external_seed_for_keys", fake_seed)
    monkeypatch.setattr(apv, "fetch_evidence_for_keys", fake_evidence)
    monkeypatch.setattr(apv, "_fetch_enrichment_for_canonical", fake_enrichment)
    monkeypatch.setattr(apv, "database", db)
    monkeypatch.setattr(script, "database", db)
    return db


def _args(**overrides: Any) -> argparse.Namespace:
    base = {"apply": False, "scope": "enriched", "limit": 0, "offset": 0}
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_apply_upserts_a_row_that_still_carries_the_enrichment(wired) -> None:
    """The regression. An --apply pass over an enriched content_key must write
    the overlay, not blank it."""
    report = await script._drive(_args(apply=True))

    assert report["outcome_counts"]["rows_upserted"] == 1
    assert len(wired.executes) == 1
    params = wired.executes[0]["params"]

    # The three overlay families the old loop silently dropped.
    assert params["bullet_points"], "bullet_points were dropped by the backfill"
    assert params["usage_scenarios"], "usage_scenarios were dropped by the backfill"
    assert params["evidence_profile"], "evidence_profile was dropped by the backfill"
    # description_markdown must win over the raw storefront description.
    assert params["description"] == _ENRICHMENT["description_markdown"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_reports_the_overlays(wired) -> None:
    report = await script._drive(_args(apply=False))

    assert wired.executes == []
    counts = report["outcome_counts"]
    assert counts["rows_assembled"] == 1
    assert counts["rows_upserted"] == 0
    assert counts["rows_skipped_no_op_in_dry_run"] == 1
    # Dry-run has to SHOW the propagation, or an operator cannot tell whether
    # --apply is worth running.
    assert counts["with_bullet_points"] == 1
    assert counts["with_usage_scenarios"] == 1
    assert counts["with_evidence_profile"] == 1


@pytest.mark.asyncio
async def test_enriched_scope_is_tagged_distinctly_in_the_audit_trail(wired) -> None:
    report = await script._drive(_args(apply=True))
    assert report["refresh_source"] == script.ENRICHED_REFRESH_SOURCE
    assert wired.executes[0]["params"]["refresh_source"] == script.ENRICHED_REFRESH_SOURCE


@pytest.mark.asyncio
async def test_all_scope_keeps_the_original_refresh_source(wired) -> None:
    report = await script._drive(_args(apply=True, scope="all"))
    assert report["refresh_source"] == apv.BACKFILL_REFRESH_SOURCE


@pytest.mark.asyncio
async def test_a_content_key_with_no_catalog_rows_is_skipped_not_written(
    monkeypatch: pytest.MonkeyPatch, wired
) -> None:
    async def no_products(content_key: str, *, db: Any = None):
        return []

    monkeypatch.setattr(apv, "fetch_products_for_key", no_products)
    report = await script._drive(_args(apply=True))

    assert wired.executes == []
    assert report["outcome_counts"]["rows_skipped_nothing_to_build"] == 1
    assert report["outcome_counts"]["rows_upserted"] == 0


def test_enriched_scope_joins_the_identity_triple_on_source_product_id() -> None:
    """product_enrichment spells the third identity column `platform_product_id`;
    catalog_products spells it `source_product_id`. Joining on the wrong one is
    what killed the on-write publish bridge — the cohort query must not repeat
    it, and a zero-row join here would look exactly like "nothing to backfill"."""
    sql = " ".join(script._ENRICHED_KEYS_SQL.split())
    assert "cp.source_product_id = pe.platform_product_id" in sql
    assert "cp.platform_product_id" not in sql
