"""A root-less snapshot must not count as "already rescored".

`full_quality_eval` promotes rules_version to SOURCE_BACKED_COMPONENTS_RULES_VERSION
whenever the source-backed FLAG is on — it does not check whether the caller
actually supplied a `product_payload` / `seed_data` root. Without one, both
source-backed components score 0 by construction, so the snapshot carries a stamp
claiming an evaluation that never happened.

`_rescored_ids()` skips on that stamp, so such rows are locked out of the very
rescore that would score them properly. On 2026-08-17 that happened to 5,495 rows
and needed --force to recover.

Gating the STAMP was tried and reverted: it pushes root-less rows back to v1-lite,
which is the corpus-scale drift tests/test_quality_rules_version_selection.py
exists to prevent. The fix belongs in the resumability filter, which is what these
tests pin.

SCOPE: the filtering itself happens in SQL, so these assert the QUERY SHAPE rather
than execution against rows. The `COALESCE(..., TRUE)` direction is the load-bearing
part — flipping it to FALSE would re-open every snapshot written before the field
existed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.backfill_external_seed_quality_rescore as bf  # noqa: E402
from services.product_quality_service import (  # noqa: E402
    payload_has_source_backed_roots,
)


class _CapturingDB:
    """Records the SQL `_rescored_ids` issues and returns no rows."""

    def __init__(self) -> None:
        self.queries: List[str] = []

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None):
        self.queries.append(str(query))
        return []


@pytest.mark.asyncio
async def test_rescored_ids_excludes_snapshots_scored_without_a_source_root(monkeypatch):
    db = _CapturingDB()
    monkeypatch.setattr(bf, "database", db)

    await bf._rescored_ids()

    assert len(db.queries) == 1
    sql = " ".join(db.queries[0].split())

    # The stamp alone must no longer be sufficient to call a row done.
    assert "source_backed_fields" in sql
    assert "source_roots_present" in sql

    # Only an explicit `false` re-opens a row. Snapshots predating the field have
    # no value there, and defaulting those to FALSE would re-open the entire
    # historical corpus on every run.
    assert "COALESCE" in sql.upper()
    assert "TRUE" in sql.upper()


def test_payload_without_a_source_document_is_not_source_backed():
    # The shape `build_quality_payload` emits: flat fields, no source document.
    flat_payload = {
        "title_local": "Vitamin C Serum",
        "description_local": "A serum.",
        "price_local_value": 24.0,
        "main_image_url": "https://example.com/1.jpg",
        "brand": "Alpha",
        "global_category_id": "skincare",
    }
    assert payload_has_source_backed_roots(flat_payload) is False

    # The shape `build_servable_quality_payload` emits: a real source document,
    # which HAS been evaluated under the source-backed rules even when thin.
    assert payload_has_source_backed_roots({**flat_payload, "seed_data": {"summary": "x"}}) is True
    assert payload_has_source_backed_roots({**flat_payload, "product_payload": {"summary": "x"}}) is True

    # Guard the degenerate inputs rather than letting them throw.
    assert payload_has_source_backed_roots({}) is False
    assert payload_has_source_backed_roots(None) is False
    # An empty root is not a root: it carries no source text to score.
    assert payload_has_source_backed_roots({"seed_data": {}}) is False
