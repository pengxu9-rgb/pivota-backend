"""The agent_pdp_view loader that feeds pick_canonical must enforce the
catalog_source_quarantine overlay (mig 134). It's opt-in (readers must anti-join)
and this serving loader historically did NOT, so a quarantined source (e.g. a
duplicate App Store review account sharing a real merchant's store) could win the
canonical pick and shadow the real merchant's enriched PDP. Regression guard."""
from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from services.agent_pdp_view_assembler import (
    _SOURCE_QUARANTINE_ANTI_JOIN,
    fetch_products_for_key,
)


class _FakeDB:
    def __init__(self) -> None:
        self.sql: Optional[str] = None
        self.params: Optional[Dict[str, Any]] = None

    async def fetch_all(self, sql: str, params: Dict[str, Any] = None):
        self.sql = sql
        self.params = params
        return []


@pytest.mark.asyncio
async def test_fetch_products_for_key_anti_joins_source_quarantine() -> None:
    db = _FakeDB()
    out = await fetch_products_for_key("ck_x", db=db)
    assert out == []
    # Still scoped to the content_key + bound param intact.
    assert db.params == {"ck": "ck_x"}
    # The quarantine anti-join is wired into the loader query, covering BOTH
    # pick_canonical call sites (assemble_row + the enrichment overlay fetch),
    # which both consume this single loader's rows.
    assert "catalog_source_quarantine" in db.sql
    # Shape-agnostic: the gate became three uncorrelated NOT IN arms in #1638
    # (correlated NOT EXISTS was a per-row rescan: 32.1ms -> 8.2ms measured).
    # Assert the GATE is present, not the join strategy it happens to use.
    assert "catalog_source_quarantine" in db.sql
    assert "match_type" in db.sql
    assert "match_type = 'merchant_platform'" in db.sql
    assert _SOURCE_QUARANTINE_ANTI_JOIN in db.sql


def test_anti_join_matches_on_merchant_platform_and_domain() -> None:
    # The fragment covers all three match conventions; merchant_platform is the
    # one used to quarantine the review account (<merchant_id>:<platform>).
    frag = _SOURCE_QUARANTINE_ANTI_JOIN
    assert "cp.merchant_id || ':' || cp.platform" in frag
    # The domain comparison now normalises BOTH sides through one helper
    # (lowercase, `www.` strip, empty-as-NULL) rather than a bare
    # `lower(q.match_value) = lower(cp.source_domain)`. Stripping only the row
    # side under-blocked a `www.`-prefixed match_value — which
    # `create_quarantine` accepts verbatim — so such a quarantine reported
    # success and blocked nothing.
    assert "cp.source_domain" in frag
    # Both sides normalised. Counted as ">= 2 and even" rather than "== 2":
    # the uncorrelated rewrite repeats the row-side expression (once for the
    # IS NULL guard, once in the NOT IN), so a fixed count pins the join shape
    # rather than the property. What matters is that q.match_value is stripped
    # too — the one-sided version under-blocked a `www.`-prefixed match_value.
    assert frag.count("LIKE 'www.%'") >= 2, "www strip must apply to BOTH sides"
    assert "lower(coalesce(q.match_value" in frag, "match_value side is not normalised"
    assert "nullif(" in frag, "empty-as-NULL guard missing"
    assert "q.state = 'active'" in frag
    assert "expires_at" in frag  # respects expiry
