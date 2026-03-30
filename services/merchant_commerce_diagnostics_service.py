from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.commerce_interactions import commerce_interaction_events, commerce_interactions
from db.database import database
from db.surface_listing_registry import surface_listing_errors, surface_listing_states


def _normalize_text(value: Any) -> str:
    return str(value or "").strip()


async def build_merchant_commerce_funnel_issues(
    *,
    merchant_id: str,
    surface: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    listing_query = select(surface_listing_states).where(surface_listing_states.c.merchant_id == merchant_id)
    listing_error_query = select(surface_listing_errors).where(surface_listing_errors.c.merchant_id == merchant_id)
    click_query = select(surface_click_events).where(surface_click_events.c.merchant_id == merchant_id)
    edge_query = select(commerce_attribution_edges).where(commerce_attribution_edges.c.merchant_id == merchant_id)
    interaction_query = select(commerce_interactions).where(commerce_interactions.c.merchant_id == merchant_id)
    event_query = select(commerce_interaction_events).where(commerce_interaction_events.c.merchant_id == merchant_id)

    if surface:
        listing_query = listing_query.where(surface_listing_states.c.surface == surface)
        listing_error_query = listing_error_query.where(surface_listing_errors.c.surface == surface)
        click_query = click_query.where(surface_click_events.c.surface == surface)
        edge_query = edge_query.where(commerce_attribution_edges.c.surface == surface)
        interaction_query = interaction_query.where(commerce_interactions.c.surface == surface)
        event_query = event_query.where(commerce_interaction_events.c.surface == surface)

    listing_rows = [dict(row) for row in await database.fetch_all(listing_query)]
    listing_errors = [dict(row) for row in await database.fetch_all(listing_error_query)]
    click_rows = [dict(row) for row in await database.fetch_all(click_query)]
    edge_rows = [dict(row) for row in await database.fetch_all(edge_query)]
    interactions = [dict(row) for row in await database.fetch_all(interaction_query)]
    event_rows = [dict(row) for row in await database.fetch_all(event_query)]

    interaction_by_id = {str(row.get("interaction_id")): row for row in interactions if row.get("interaction_id")}
    click_by_id = {str(row.get("click_id")): row for row in click_rows if row.get("click_id")}

    buckets: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "code": None,
            "count": 0,
            "severity": "warning",
            "message": None,
            "sample_interaction_ids": [],
            "samples": [],
        }
    )

    def add_issue(code: str, *, message: str, severity: str = "warning", interaction_id: Optional[str] = None, sample: Optional[Dict[str, Any]] = None) -> None:
        bucket = buckets[code]
        bucket["code"] = code
        bucket["severity"] = severity
        bucket["message"] = message
        bucket["count"] += 1
        if interaction_id and interaction_id not in bucket["sample_interaction_ids"] and len(bucket["sample_interaction_ids"]) < 5:
            bucket["sample_interaction_ids"].append(interaction_id)
        if sample and len(bucket["samples"]) < 5:
            bucket["samples"].append(sample)

    for row in listing_rows:
        status = _normalize_text(row.get("status")).lower()
        if status in {"blocked", "error"}:
            add_issue(
                "LISTING_ERROR",
                message="Listing registry still contains blocked or error rows.",
                severity="warning",
                interaction_id=_normalize_text(row.get("interaction_id")) or None,
                sample={
                    "listing_id": row.get("listing_id"),
                    "surface": row.get("surface"),
                    "status": row.get("status"),
                    "canonical_variant_id": row.get("canonical_variant_id"),
                },
            )

    for row in listing_errors:
        add_issue(
            "LISTING_ERROR",
            message="Listing registry recorded publish or validation errors.",
            severity="warning",
            sample={
                "listing_id": row.get("listing_id"),
                "error_code": row.get("error_code"),
                "error_message": row.get("error_message"),
            },
        )

    for row in click_rows:
        if not _normalize_text(row.get("canonical_variant_id")):
            add_issue(
                "MISSING_INFO",
                message="Click rows are missing canonical variant ids.",
                severity="warning",
                interaction_id=_normalize_text(row.get("interaction_id")) or None,
                sample={"click_id": row.get("click_id"), "surface": row.get("surface")},
            )

    for row in edge_rows:
        click_id = _normalize_text(row.get("click_id"))
        interaction_id = _normalize_text(row.get("interaction_id")) or None
        if not click_id:
            add_issue(
                "UNATTRIBUTED_ORDER",
                message="Orders reached attribution edges without a click_id.",
                severity="critical",
                interaction_id=interaction_id,
                sample={"order_id": row.get("order_id"), "surface": row.get("surface")},
            )
            continue
        click_row = click_by_id.get(click_id)
        if not click_row:
            add_issue(
                "TRACE_BROKEN",
                message="Attribution edges reference clicks that are missing from click tracking.",
                severity="critical",
                interaction_id=interaction_id,
                sample={"order_id": row.get("order_id"), "click_id": click_id},
            )
            continue
        if _normalize_text(click_row.get("canonical_variant_id")) and _normalize_text(row.get("canonical_variant_id")) and _normalize_text(click_row.get("canonical_variant_id")) != _normalize_text(row.get("canonical_variant_id")):
            add_issue(
                "VARIANT_MISMATCH",
                message="Order attribution variant does not match the clicked variant.",
                severity="warning",
                interaction_id=interaction_id or _normalize_text(click_row.get("interaction_id")) or None,
                sample={
                    "order_id": row.get("order_id"),
                    "click_id": click_id,
                    "clicked_variant_id": click_row.get("canonical_variant_id"),
                    "ordered_variant_id": row.get("canonical_variant_id"),
                },
            )

    diagnostic_event_map = {
        "prompt.triggered": "PROMPT_TRIGGERED",
        "recall.miss": "RECALL_MISS",
        "surface.click": "CLICK_ATTRIBUTED",
    }
    for row in event_rows:
        event_type = _normalize_text(row.get("event_type")).lower()
        code = diagnostic_event_map.get(event_type)
        if not code:
            continue
        add_issue(
            code,
            message=f"Observed {event_type} signals in the canonical event ledger.",
            severity="info",
            interaction_id=_normalize_text(row.get("interaction_id")) or None,
            sample={"event_id": row.get("event_id"), "event_type": row.get("event_type")},
        )

    issues = sorted(
        buckets.values(),
        key=lambda item: (
            {"critical": 0, "warning": 1, "info": 2}.get(str(item.get("severity") or "warning"), 9),
            -int(item.get("count") or 0),
            str(item.get("code") or ""),
        ),
    )
    return {
        "merchant_id": merchant_id,
        "surface": surface,
        "issues": issues[: max(1, limit)],
        "summary": {
            "interaction_count": len(interaction_by_id),
            "listing_rows_total": len(listing_rows),
            "click_rows_total": len(click_rows),
            "edge_rows_total": len(edge_rows),
        },
    }
