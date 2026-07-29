"""/__catalog_health must say WHY rows are blocked, not only how many.

The endpoint reported `quality_blocked` as one number, and the only way to learn
why those rows were blocked was a psql session against the Railway proxy — which
is how the 2026-07-28 serving-coverage audit spent its first hour (the answer
turned out to be `low_quality: 92%`, which reframed the entire plan). The
histogram makes that question self-serve.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class _FakeDb:
    """Dispatches on SQL text — the same shape test_catalog_invariant_checks uses."""

    def __init__(self):
        self.queries = []

    async def fetch_all(self, query, *args, **kwargs):
        text = str(query)
        self.queries.append(text)
        if "GROUP BY 1, 2" in text:  # the cohort funnel
            return [
                {"source_system": "external_seed", "cohort": "public", "cnt": 10},
                {"source_system": "external_seed", "cohort": "quality_blocked", "cnt": 5},
            ]
        if "blocker_code" in text:  # the histogram
            return [
                {"blocker": "low_quality", "n": 3},
                {"blocker": "no_price", "n": 2},
            ]
        raise AssertionError(f"unexpected query: {text[:120]}")


@pytest.mark.asyncio
async def test_blocker_histogram_is_in_the_payload(monkeypatch):
    import routes.__catalog_health as mod

    fake = _FakeDb()
    import db.database as dbmod

    monkeypatch.setattr(dbmod, "database", fake)

    async def _stub_drift(_db):
        return {"total": 0, "cached": False}

    monkeypatch.setattr(mod, "_cached_agent_pdp_view_drift", _stub_drift)
    monkeypatch.setattr(mod, "_cached_pdp_will_render_drift", _stub_drift)

    body = await mod.catalog_health()
    assert body.get("error") is None, body
    assert body["blocker_histogram"] == {"low_quality": 3, "no_price": 2}
    # The cohort funnel is untouched by the addition.
    assert body["cohorts"]["public"] == 10


def test_histogram_excludes_serving_eligible_keys():
    """A histogram that includes eligible keys would be dominated by
    blocker='none' and read as a backlog that does not exist. Pinned at the SQL
    because the FakeDb above cannot execute a WHERE clause."""
    import inspect

    import routes.__catalog_health as mod

    src = inspect.getsource(mod.catalog_health)
    assert "serving_eligible IS NOT TRUE" in src, (
        "the histogram must count only BLOCKED content_keys — eligible keys all "
        "carry blocker_code='none' and would swamp the signal"
    )
