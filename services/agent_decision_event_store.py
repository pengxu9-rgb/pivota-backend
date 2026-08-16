"""Best-effort writer for the decision-layer event store.

This is the only write interface for decision attribution events. The month-3
Redis-backed queue migration should keep these public functions stable and swap
only the in-memory enqueue/flush internals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Iterable, Optional, Tuple

from db._jsonb_safe import _json_safe
from db.database import database
from services.protocols import DEFAULT_PROTOCOL, validate_protocol

logger = logging.getLogger(__name__)

_QUEUE_MAX = 1000
_QUEUE: Optional[asyncio.Queue[Tuple[str, Dict[str, Any]]]] = None
_WORKER: Optional[asyncio.Task[None]] = None
_QUEUE_LOOP: Optional[asyncio.AbstractEventLoop] = None
_MANY = {"candidates", "exposures"}
_SPEC = {
    "decision": ("agent_decision_events", ("decision_id", "merchant_id", "brand_id", "surface", "channel", "agent_context", "protocol", "attribution_model", "attribution_window_seconds"), "decision_id", {"agent_context"}),
    "candidates": ("agent_decision_candidates", ("candidate_id", "decision_id", "content_key", "catalog_offer_id", "rank_position", "eligibility_flags"), "candidate_id", {"eligibility_flags"}),
    "exposures": ("agent_exposure_events", ("exposure_id", "decision_id", "content_key", "catalog_offer_id", "exposure_position", "slot"), "exposure_id", set()),
    "checkout": ("checkout_decisions", ("checkout_decision_id", "decision_id", "order_id", "merchant_id", "content_key", "catalog_offer_id", "purchase_route", "quote_context", "psp_context", "protocol"), "checkout_decision_id", {"quote_context", "psp_context"}),
    "funnel_link": ("agent_decision_funnel_links", ("link_id", "decision_id", "funnel_event_id", "exposure_id", "checkout_decision_id", "content_key", "catalog_offer_id", "commerce_attribution_edge_id", "merchant_id", "protocol", "attribution_model", "attribution_window_seconds"), "funnel_event_id", set()),
}
_SQL: Dict[str, str] = {}


def _json_param(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(_json_safe(value), separators=(",", ":"), default=str)


def _sql(op: str) -> str:
    if op not in _SQL:
        table, cols, conflict, json_cols = _SPEC[op]
        values = [f"CAST(:{col} AS jsonb)" if col in json_cols else f":{col}" for col in cols]
        _SQL[op] = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(values)}) ON CONFLICT ({conflict}) DO NOTHING"
    return _SQL[op]


def _report(exc: Exception, *, op: str, payload: Optional[Dict[str, Any]] = None) -> None:
    try:
        from config.sentry_config import capture_exception

        capture_exception(exc, {
            "component": "agent_decision_event_store",
            "operation": op,
            "decision_id": (payload or {}).get("decision_id"),
            "merchant_id": (payload or {}).get("merchant_id"),
        })
    except Exception:  # noqa: BLE001
        logger.debug("decision event store reporting failed", exc_info=True)


def _ensure_worker() -> None:
    global _QUEUE, _WORKER, _QUEUE_LOOP
    loop = asyncio.get_running_loop()
    if _QUEUE is None or _QUEUE_LOOP is not loop:
        _QUEUE = asyncio.Queue(maxsize=_QUEUE_MAX)
        _QUEUE_LOOP = loop
        _WORKER = None
    if _WORKER is None or _WORKER.done():
        # Fresh context => own `databases` Connection (issue #1754): this
        # singleton is spawned from whatever request/job first enqueues and
        # must not inherit — and then keep alive — that context's Connection.
        from services.scheduler_job_runner import spawn_isolated
        _WORKER = spawn_isolated(_flush_loop(_QUEUE), name="agent_decision_event_flush")


async def _enqueue(op: str, payload: Dict[str, Any]) -> None:
    _ensure_worker()
    assert _QUEUE is not None
    try:
        _QUEUE.put_nowait((op, payload))
    except asyncio.QueueFull as exc:
        _report(exc, op=f"{op}.queue_full", payload=payload)


async def record_decision_event(
    *, decision_id: Optional[str] = None, merchant_id: Optional[str] = None,
    brand_id: Optional[str] = None, surface: Optional[str] = None,
    channel: Optional[str] = None, agent_context: Optional[Dict[str, Any]] = None,
    protocol: str = DEFAULT_PROTOCOL,
    attribution_model: str = "last_agent_decision_v1", attribution_window_seconds: int = 2592000,
) -> str:
    decision_id = decision_id or str(uuid.uuid4())
    protocol = validate_protocol(protocol)
    await _enqueue("decision", {
        "decision_id": decision_id, "merchant_id": merchant_id, "brand_id": brand_id,
        "surface": surface, "channel": channel, "agent_context": _json_param(agent_context),
        "protocol": protocol, "attribution_model": attribution_model,
        "attribution_window_seconds": attribution_window_seconds,
    })
    return decision_id


async def record_decision_candidates(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = [{
        "candidate_id": str(row.get("candidate_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": row.get("content_key"), "catalog_offer_id": row.get("catalog_offer_id") or row.get("offer_id"),
        "rank_position": row.get("rank_position", row.get("position", idx)),
        "eligibility_flags": _json_param(row.get("eligibility_flags") or row.get("flags")),
    } for idx, row in enumerate(rows) if isinstance(row, dict)]
    if values:
        await _enqueue("candidates", {"decision_id": decision_id, "rows": values})


async def record_exposure_events(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = [{
        "exposure_id": str(row.get("exposure_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": row.get("content_key"), "catalog_offer_id": row.get("catalog_offer_id") or row.get("offer_id"),
        "exposure_position": row.get("exposure_position", row.get("position", idx)), "slot": row.get("slot"),
    } for idx, row in enumerate(rows) if isinstance(row, dict)]
    if values:
        await _enqueue("exposures", {"decision_id": decision_id, "rows": values})


async def record_checkout_decision(
    *, checkout_decision_id: Optional[str] = None, decision_id: Optional[str] = None,
    order_id: Optional[str] = None, merchant_id: Optional[str] = None,
    content_key: Optional[str] = None, catalog_offer_id: Optional[str] = None,
    purchase_route: Optional[str] = None, quote_context: Optional[Dict[str, Any]] = None,
    psp_context: Optional[Dict[str, Any]] = None,
    protocol: str = DEFAULT_PROTOCOL,
) -> str:
    checkout_decision_id = checkout_decision_id or str(uuid.uuid4())
    protocol = validate_protocol(protocol)
    await _enqueue("checkout", {
        "checkout_decision_id": checkout_decision_id, "decision_id": decision_id,
        "order_id": order_id, "merchant_id": merchant_id, "content_key": content_key,
        "catalog_offer_id": catalog_offer_id, "purchase_route": purchase_route,
        "quote_context": _json_param(quote_context), "psp_context": _json_param(psp_context),
        "protocol": protocol,
    })
    return checkout_decision_id


async def record_funnel_link(
    *, link_id: Optional[str] = None, decision_id: Optional[str] = None,
    funnel_event_id: Optional[str] = None, exposure_id: Optional[str] = None,
    checkout_decision_id: Optional[str] = None, content_key: Optional[str] = None,
    catalog_offer_id: Optional[str] = None, commerce_attribution_edge_id: Optional[str] = None,
    merchant_id: Optional[str] = None, attribution_model: str = "last_agent_decision_v1",
    attribution_window_seconds: int = 2592000, protocol: str = DEFAULT_PROTOCOL,
) -> str:
    if not funnel_event_id:
        return ""
    link_id = link_id or str(uuid.uuid4())
    protocol = validate_protocol(protocol)
    await _enqueue("funnel_link", {
        "link_id": link_id, "decision_id": decision_id, "funnel_event_id": funnel_event_id,
        "exposure_id": exposure_id, "checkout_decision_id": checkout_decision_id,
        "content_key": content_key, "catalog_offer_id": catalog_offer_id,
        "commerce_attribution_edge_id": commerce_attribution_edge_id, "merchant_id": merchant_id,
        "protocol": protocol, "attribution_model": attribution_model,
        "attribution_window_seconds": attribution_window_seconds,
    })
    return link_id


def _decision_layer_node(metadata: Any) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    node = metadata.get("decision_layer")
    return node if isinstance(node, dict) else {}


def _first_nonempty(*values: Any) -> Optional[str]:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def extract_order_decision_linkage(metadata: Any) -> Dict[str, Optional[str]]:
    """Pull decision-layer linkage out of an order's stored metadata.

    Reads the `decision_layer` block that ``agent_api`` persists onto the order
    at creation time (``decision_layer.{decision_id, checkout_decision_id}``),
    with conservative fallbacks. Pure + side-effect-free so it is safe to call
    from the money-path webhook. Returns null fields when absent — the caller
    decides whether enough linkage exists to record a funnel link.

    The order row's `metadata` JSONB may arrive as a dict OR as a raw JSON
    string depending on the DB driver's JSONB codec, so a string is parsed here
    (the same hazard the order-creation path guards against). A value that is
    neither a dict nor parseable degrades to empty linkage, never raises.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:  # noqa: BLE001
            metadata = {}
    node = _decision_layer_node(metadata)
    meta = metadata if isinstance(metadata, dict) else {}
    return {
        "decision_id": _first_nonempty(
            node.get("decision_id"), node.get("agent_decision_id"), meta.get("decision_id")
        ),
        "checkout_decision_id": _first_nonempty(
            node.get("checkout_decision_id"), meta.get("checkout_decision_id")
        ),
        "content_key": _first_nonempty(node.get("content_key"), meta.get("content_key")),
        "catalog_offer_id": _first_nonempty(
            node.get("catalog_offer_id"), meta.get("catalog_offer_id"), meta.get("offer_id")
        ),
        "protocol": _first_nonempty(node.get("protocol"), meta.get("protocol")),
    }


async def _flush_loop(queue: asyncio.Queue[Tuple[str, Dict[str, Any]]]) -> None:
    while True:
        op, payload = await queue.get()
        try:
            if op in _MANY:
                await database.execute_many(_sql(op), payload["rows"])
            else:
                await database.execute(_sql(op), payload)
        except Exception as exc:  # noqa: BLE001
            _report(exc, op=op, payload=payload)
        finally:
            queue.task_done()
