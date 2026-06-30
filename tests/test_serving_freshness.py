from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.serving_freshness import PRICE_FRESHNESS_TTL, serving_freshness  # noqa: E402


def test_fresh_row_within_ttl_is_not_stale() -> None:
    now = datetime(2026, 6, 30, 12, 0, 0)
    observed = now - timedelta(minutes=30)  # inside the 1h window
    block = serving_freshness(observed, now=now)

    assert block["is_stale"] is False
    assert block["observed_at"] == observed.isoformat()
    assert block["fresh_until"] == (observed + PRICE_FRESHNESS_TTL).isoformat()
    assert block["ttl_seconds"] == 3600


def test_old_row_past_ttl_is_stale() -> None:
    now = datetime(2026, 6, 30, 12, 0, 0)
    observed = now - timedelta(hours=2)  # outside the 1h window
    block = serving_freshness(observed, now=now)

    assert block["is_stale"] is True
    assert block["fresh_until"] == (observed + PRICE_FRESHNESS_TTL).isoformat()


def test_exactly_at_ttl_boundary_is_not_stale() -> None:
    now = datetime(2026, 6, 30, 12, 0, 0)
    observed = now - PRICE_FRESHNESS_TTL  # fresh_until == now
    block = serving_freshness(observed, now=now)

    # is_stale is now > fresh_until -- the boundary instant still counts fresh.
    assert block["is_stale"] is False


def test_missing_refreshed_at_is_stale() -> None:
    block = serving_freshness(None)

    assert block["is_stale"] is True
    assert block["observed_at"] is None
    assert block["fresh_until"] is None
    assert block["ttl_seconds"] == 3600


def test_aware_datetime_is_normalized_to_utc() -> None:
    now = datetime(2026, 6, 30, 12, 0, 0)
    # 11:30 UTC == 06:30 US/Eastern; supply it aware, expect naive-UTC handling.
    observed_aware = datetime(2026, 6, 30, 11, 30, 0, tzinfo=timezone.utc)
    block = serving_freshness(observed_aware, now=now)

    assert block["is_stale"] is False
    assert block["observed_at"] == "2026-06-30T11:30:00"


def test_iso_string_refreshed_at_is_parsed() -> None:
    now = datetime(2026, 6, 30, 12, 0, 0)
    block = serving_freshness("2026-06-30T11:45:00Z", now=now)

    assert block["is_stale"] is False
    assert block["observed_at"] == "2026-06-30T11:45:00"


def test_unparseable_refreshed_at_is_stale() -> None:
    block = serving_freshness("not-a-date", now=datetime(2026, 6, 30, 12, 0, 0))

    assert block["is_stale"] is True
    assert block["observed_at"] is None
