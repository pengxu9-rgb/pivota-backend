from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from typing import Any

from db.database import database


async def evaluate_partner_targets(channel_partner_id: int) -> list[dict[str, Any]]:
    """Evaluate one partner's cohort targets and transition open rows."""

    rows = await _fetch_partner_targets(channel_partner_id)
    today = _today()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(await _evaluate_target_row(row, today=today, mutate=True))
    return results


async def evaluate_all_open_targets() -> dict[int, list[dict[str, Any]]]:
    """Run evaluate_partner_targets for every partner with an open target."""

    rows = await database.fetch_all(
        """
        SELECT DISTINCT channel_partner_id
        FROM partner_cohort_targets
        WHERE status = 'open'
        ORDER BY channel_partner_id
        """
    )
    results: dict[int, list[dict[str, Any]]] = {}
    for row in rows or []:
        partner_id = int(_row_get(row, "channel_partner_id"))
        results[partner_id] = await evaluate_partner_targets(partner_id)
    return results


async def get_partner_target_progress(
    channel_partner_id: int,
) -> list[dict[str, Any]]:
    """Read-only progress rows for the admin cohort dashboard."""

    rows = await _fetch_partner_targets(channel_partner_id)
    today = _today()
    progress: list[dict[str, Any]] = []
    for row in rows:
        window_start = _as_date(_row_get(row, "window_start_date"))
        window_end = _add_months(window_start, int(_row_get(row, "window_months")))
        current_count = await _current_count(
            channel_partner_id=channel_partner_id,
            window_start=window_start,
            window_end=window_end,
        )
        progress.append(
            {
                "id": int(_row_get(row, "id")),
                "label": _row_get(row, "label"),
                "target_brand_count": int(_row_get(row, "target_brand_count")),
                "current_count": current_count,
                "window_start_date": window_start,
                "window_end_date": window_end,
                "window_open": today <= window_end,
                "status": _row_get(row, "status"),
                "achieved_at": _row_get(row, "achieved_at"),
                "paid_at": _row_get(row, "paid_at"),
                "bonus_cents": int(_row_get(row, "bonus_cents") or 0),
                "days_remaining": max((window_end - today).days, 0),
            }
        )
    return progress


async def _fetch_partner_targets(channel_partner_id: int) -> list[Any]:
    rows = await database.fetch_all(
        """
        SELECT
          id,
          channel_partner_id,
          label,
          target_brand_count,
          window_months,
          window_start_date,
          bonus_cents,
          status,
          achieved_at,
          paid_at
        FROM partner_cohort_targets
        WHERE channel_partner_id = :channel_partner_id
        ORDER BY window_start_date, id
        """,
        {"channel_partner_id": channel_partner_id},
    )
    return list(rows or [])


async def _evaluate_target_row(
    row: Any,
    *,
    today: date,
    mutate: bool,
) -> dict[str, Any]:
    target_id = int(_row_get(row, "id"))
    channel_partner_id = int(_row_get(row, "channel_partner_id"))
    target_brand_count = int(_row_get(row, "target_brand_count"))
    window_start = _as_date(_row_get(row, "window_start_date"))
    window_end = _add_months(window_start, int(_row_get(row, "window_months")))
    current_count = await _current_count(
        channel_partner_id=channel_partner_id,
        window_start=window_start,
        window_end=window_end,
    )

    status_before = str(_row_get(row, "status"))
    status_after = status_before
    achieved_at_set = False

    if status_before == "open":
        if current_count >= target_brand_count:
            status_after = "achieved"
            achieved_at_set = True
            if mutate:
                await database.execute(
                    """
                    UPDATE partner_cohort_targets
                    SET status = 'achieved',
                        achieved_at = COALESCE(achieved_at, NOW())
                    WHERE id = :target_id
                      AND status = 'open'
                    """,
                    {"target_id": target_id},
                )
        elif today > window_end:
            status_after = "expired"
            if mutate:
                await database.execute(
                    """
                    UPDATE partner_cohort_targets
                    SET status = 'expired'
                    WHERE id = :target_id
                      AND status = 'open'
                    """,
                    {"target_id": target_id},
                )

    return {
        "target_id": target_id,
        "target_brand_count": target_brand_count,
        "current_count": current_count,
        "window_start": window_start,
        "window_end": window_end,
        "window_open": today <= window_end,
        "status_before": status_before,
        "status_after": status_after,
        "achieved_at_set": achieved_at_set,
    }


async def _current_count(
    *,
    channel_partner_id: int,
    window_start: date,
    window_end: date,
) -> int:
    row = await database.fetch_one(
        """
        SELECT COUNT(DISTINCT pa.merchant_id) AS current_count
        FROM partner_attribution pa
        WHERE pa.channel_partner_id = :partner_id
          AND pa.activated_at IS NOT NULL
          AND pa.activated_at >= :window_start
          AND pa.activated_at <= :window_end
        """,
        {
            "partner_id": channel_partner_id,
            "window_start": window_start,
            "window_end": window_end,
        },
    )
    return int(_row_get(row, "current_count") or 0)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _as_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except Exception:
        return getattr(row, key)
