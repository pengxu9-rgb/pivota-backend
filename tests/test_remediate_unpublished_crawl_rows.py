"""Takedown of already-ingested ad-campaign landing pages (PIVOTA-Agent#1926).

The point of this runner is that the PREVIOUS suppression sweep did not take
anything down: it wrote suppression_reason without suppressed_at, and the
serving gate reads only the latter. So the tests that matter are the ones
pinning which columns get written, and that the residual (content_keys a real
product keeps alive) is reported rather than glossed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402
import scripts.remediate_unpublished_crawl_rows as remediate  # noqa: E402
from services.shopify_publication_signal import UNKNOWN, UNPUBLISHED  # noqa: E402


class _FakeDB:
    """Records every statement so the tests can assert on the actual SQL."""

    def __init__(self, fetch_rows=None):
        self.executed = []
        self._fetch_rows = fetch_rows or []

    async def execute(self, query, values=None):
        self.executed.append((" ".join(str(query).split()), values or {}))

    async def fetch_all(self, query, values=None):
        return self._fetch_rows

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def statements_touching(self, fragment):
        return [(q, v) for q, v in self.executed if fragment in q]


def _row(seed_id, url, content_key="ck_1", reason=None):
    return {
        "seed_id": seed_id, "destination_url": url, "content_key": content_key,
        "product_key": f"prod::{seed_id}", "seed_status": "active",
        "suppression_reason": reason, "suppressed_at": None,
    }


@pytest.mark.asyncio
async def test_withdraw_sets_suppressed_at_not_just_a_reason(monkeypatch):
    """The whole defect in one assertion. suppression_reason alone leaves the
    PDP serving — step5 proved that in prod on this exact cohort."""
    db = _FakeDB()
    monkeypatch.setattr(remediate, "database", db)
    monkeypatch.setattr(remediate, "recompute_serving_eligibility",
                        lambda ck, reason=None: _async(False))

    await remediate._withdraw([_row("s1", "https://biodance.com/products/0627_cm_a")])

    assert db.statements_touching("suppressed_at=NOW()"), "must stamp the column serving reads"
    assert db.statements_touching("status='inactive'")
    assert db.statements_touching("suppression_reason=:reason")


@pytest.mark.asyncio
async def test_withdraw_preserves_an_existing_suppression_reason(monkeypatch):
    """step5's provenance is worth keeping; only fill a NULL reason."""
    db = _FakeDB()
    monkeypatch.setattr(remediate, "database", db)
    monkeypatch.setattr(remediate, "recompute_serving_eligibility",
                        lambda ck, reason=None: _async(False))

    await remediate._withdraw([_row("s1", "https://x.com/products/a", reason="step5_campaign_clone_dup")])

    reason_writes = db.statements_touching("suppression_reason=:reason")
    assert all("suppression_reason IS NULL" in q for q, _ in reason_writes)


@pytest.mark.asyncio
async def test_every_write_is_idempotency_guarded(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(remediate, "database", db)
    monkeypatch.setattr(remediate, "recompute_serving_eligibility",
                        lambda ck, reason=None: _async(False))

    await remediate._withdraw([_row("s1", "https://x.com/products/a")])

    for q, _ in db.executed:
        assert " IS NULL" in q or "status='active'" in q, f"unguarded write: {q}"


@pytest.mark.asyncio
async def test_recompute_is_called_once_per_content_key(monkeypatch):
    seen = []
    db = _FakeDB()
    monkeypatch.setattr(remediate, "database", db)

    def _rec(content_key, reason=None):
        seen.append(content_key)
        return _async(False)

    monkeypatch.setattr(remediate, "recompute_serving_eligibility", _rec)
    await remediate._withdraw([
        _row("s1", "https://x.com/products/a", content_key="ck_a"),
        _row("s2", "https://x.com/products/b", content_key="ck_a"),  # same key
        _row("s3", "https://x.com/products/c", content_key="ck_b"),
    ])
    assert sorted(seen) == ["ck_a", "ck_b"]


@pytest.mark.asyncio
async def test_residual_is_surfaced_when_a_real_product_shares_the_key(monkeypatch):
    """A content_key shared with a real product KEEPS serving — correctly, the
    real product must not go dark. The runner must report that rather than
    reporting a clean sweep."""
    db = _FakeDB()
    monkeypatch.setattr(remediate, "database", db)
    monkeypatch.setattr(remediate, "recompute_serving_eligibility",
                        lambda ck, reason=None: _async(ck == "ck_shared"))

    states = await remediate._withdraw([
        _row("s1", "https://x.com/products/a", content_key="ck_shared"),
        _row("s2", "https://x.com/products/b", content_key="ck_alone"),
    ])
    assert states == {"ck_shared": True, "ck_alone": False}


@pytest.mark.asyncio
async def test_only_unpublished_rows_are_actionable(monkeypatch):
    """PUBLISHED and UNKNOWN must never be touched — the runner changes live
    serving state, so fail-open matters more here than at ingestion."""
    verdicts = {
        "https://x.com/products/real": "published",
        "https://x.com/products/0627_cm_ad": UNPUBLISHED,
        "https://nosite.com/products/x": UNKNOWN,
    }

    class _Oracle:
        async def classify(self, item):
            return verdicts[item["destination_url"]]

    monkeypatch.setattr(remediate, "PublicationOracle", lambda: _Oracle())
    rows = [_row(f"s{i}", u) for i, u in enumerate(verdicts)]
    actionable, counts = await remediate._adjudicate(rows)

    assert [r["destination_url"] for r in actionable] == ["https://x.com/products/0627_cm_ad"]
    assert counts[UNKNOWN] == 1


def test_host_filter_is_www_insensitive():
    assert remediate._host_of("https://www.biodance.com/products/a") == "biodance.com"
    assert remediate._host_of("https://biodance.com/products/a") == "biodance.com"
    assert remediate._host_of(None) == ""


def test_candidates_query_does_not_exclude_already_reasoned_rows():
    """The step5-suppressed rows are exactly the ones whose takedown never
    landed. Filtering them out would skip 213 of biodance's 250."""
    assert "suppressed_at IS NULL" in remediate.CANDIDATES_SQL
    assert "suppression_reason IS NULL" not in remediate.CANDIDATES_SQL


# --- the ingestion gate now withdraws too -----------------------------------


@pytest.mark.asyncio
async def test_gate_suppression_withdraws_only_for_the_unpublished_ground():
    """Ad landing pages opt into suppressed_at; the dup path deliberately does
    not (separate blast radius, separate precedent)."""
    db = _FakeDB()
    original = onboard.database
    onboard.database = db
    try:
        await onboard._suppress_dropped_listings(
            [{"external_product_id": "ad_1"}], onboard.UNPUBLISHED_SUPPRESSION_REASON, withdraw=True
        )
        assert db.statements_touching("suppressed_at=NOW()")

        db.executed.clear()
        await onboard._suppress_dropped_listings([{"external_product_id": "dup_1"}])
        assert not db.statements_touching("suppressed_at=NOW()")
    finally:
        onboard.database = original


async def _async(value):
    return value
