"""The baseline report's own guards, which had no tests at all.

Its SQL is planned by the driven prepare gate, but two behaviours carry the
weight and neither was constrained: the duplicate-label guard (labels are dict
keys, so a collision silently DROPS a row from the report) and the zero-join
WARNING, which is the whole reason the file exists — a mis-spelled identity join
returns zero rows and reads exactly like "there is nothing to backfill".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.report_enrichment_propagation_baseline as report  # noqa: E402


def test_every_source_count_label_is_unique() -> None:
    """Labels are dict keys. product_enrichment and agent_pdp_view both have a
    `with bullet_points` count, and when they shared a label the second silently
    overwrote the first — the report printed 6 rows where it should print 8, with
    no error anywhere."""
    labels = [label.strip() for label, _sql in report.SOURCE_COUNTS]
    duplicates = sorted({x for x in labels if labels.count(x) > 1})
    assert not duplicates, f"duplicate SOURCE_COUNTS labels drop rows silently: {duplicates}"


def test_every_source_count_names_the_table_it_counts() -> None:
    """The collision above was possible because labels were bare field names.
    Qualifying them is what prevents a recurrence, so pin it."""
    for label, _sql in report.SOURCE_COUNTS:
        assert ":" in label, f"{label!r} does not say which table it counts"


@pytest.mark.asyncio
async def test_a_zero_join_is_reported_as_a_broken_join_not_an_empty_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing warning. Source rows exist, none of them join — that is
    the signature of the identity-spelling bug that killed the publish bridge,
    and it must not be read as 'nothing to backfill'."""
    class _FakeDB:
        is_connected = True

        async def fetch_val(self, sql, params=None):
            return 7

        async def fetch_one(self, sql, params=None):
            if "rows_that_join" in sql:
                return {"enrichment_rows": 7, "rows_that_join": 0}
            return {}

        async def fetch_all(self, sql, params=None):
            return []

    monkeypatch.setattr(report, "database", _FakeDB())
    out = await report.collect()

    assert "WARNING" in out, "a zero join was reported as an empty backfill"
    assert "identity join is wrong" in out["WARNING"]
    # It must also reach the human-readable output, not just the JSON — render()
    # marks it with `!!` rather than repeating the key name.
    rendered = report.render(out)
    assert "identity join is wrong" in rendered, "the warning never reaches the operator"
    assert "!!" in rendered


@pytest.mark.asyncio
async def test_no_warning_when_the_join_lands(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side: a genuinely empty corpus must not cry wolf."""
    class _FakeDB:
        is_connected = True

        async def fetch_val(self, sql, params=None):
            return 7

        async def fetch_one(self, sql, params=None):
            if "rows_that_join" in sql:
                return {"enrichment_rows": 7, "rows_that_join": 7}
            return {}

        async def fetch_all(self, sql, params=None):
            return []

    monkeypatch.setattr(report, "database", _FakeDB())
    out = await report.collect()
    assert "WARNING" not in out
