"""Issued vs clicked: what a click-out report may claim (FIX_PLAN O-08).

A `surface_click_events` row exists for every click id we ISSUED to an agent (`issued_at` set,
ADR-025 D1), whether or not a buyer ever followed it. Only `/r` (a followed link) moves
`click_count`, and only an impression call moves `impression_count`. Agents are also handed
direct cart/PDP URLs beside the `/r` link, and a buyer who follows one of those leaves no click
row at all. So:

  issued            click ids handed to agents
  clicked           issued ids a buyer followed through /r (click_count > 0)
  issued_not_clicked issued ids with no /r follow: unknown, NOT "ignored" — a direct-URL follow
                    looks exactly like this
  clicked_unissued  /r follows of an id with no issue record (links minted before issue
                    recording, or by a lane that does not record issue)

Test traffic stamped at write (services/test_traffic.py) is excluded and counted separately.
The process-local click-write failure counts (services.outbound_links_service) are attached, so a
reader can see whether "0 clicked" could be lost writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from db.database import database
from services.test_traffic import not_test_traffic_sql

_SUMMARY_SQL = """
SELECT
  SUM(CASE WHEN {not_test} AND issued_at IS NOT NULL THEN 1 ELSE 0 END) AS issued,
  SUM(CASE WHEN {not_test} AND issued_at IS NOT NULL AND COALESCE(click_count, 0) > 0
           THEN 1 ELSE 0 END) AS clicked,
  SUM(CASE WHEN {not_test} AND issued_at IS NOT NULL AND COALESCE(impression_count, 0) > 0
           THEN 1 ELSE 0 END) AS issued_with_impression,
  SUM(CASE WHEN {not_test} AND issued_at IS NULL AND COALESCE(click_count, 0) > 0
           THEN 1 ELSE 0 END) AS clicked_unissued,
  SUM(CASE WHEN {not_test} THEN COALESCE(click_count, 0) ELSE 0 END) AS click_events,
  SUM(CASE WHEN {not_test} THEN 0 ELSE 1 END) AS test_traffic_rows_excluded
FROM surface_click_events
WHERE COALESCE(issued_at, created_at) >= :start_at
  AND COALESCE(issued_at, created_at) < :end_at
""".format(not_test=not_test_traffic_sql("context"))


def _int(row: Any, key: str) -> int:
    try:
        value = row[key]
    except Exception:  # noqa: BLE001
        value = None
    return int(value or 0)


async def issued_vs_clicked(
    *, start_at: datetime, end_at: datetime, merchant_id: Optional[str] = None
) -> Dict[str, Any]:
    query = _SUMMARY_SQL
    params: Dict[str, Any] = {"start_at": start_at, "end_at": end_at}
    if merchant_id:
        query += "  AND merchant_id = :merchant_id\n"
        params["merchant_id"] = merchant_id
    row = await database.fetch_one(query, params)
    issued = _int(row, "issued")
    clicked = _int(row, "clicked")
    from services.outbound_links_service import click_write_failure_counts

    return {
        "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
        "merchant_id": merchant_id,
        "issued": issued,
        "clicked": clicked,
        "issued_not_clicked": issued - clicked,
        "issued_with_impression": _int(row, "issued_with_impression"),
        "clicked_unissued": _int(row, "clicked_unissued"),
        "click_events": _int(row, "click_events"),
        "follow_rate": round(clicked / issued, 4) if issued else None,
        "test_traffic_rows_excluded": _int(row, "test_traffic_rows_excluded"),
        # Since this process started; other instances keep their own (the log line and the
        # Prometheus counter are the fleet-wide view).
        "click_write_failures_this_process": click_write_failure_counts(),
        "notes": [
            "issued_not_clicked is unknown, not ignored: agents also receive direct cart/PDP "
            "URLs, and a buyer following one writes no click row.",
        ],
    }
