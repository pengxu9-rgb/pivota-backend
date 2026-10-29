"""Best-effort writer for the decision-layer event store.

This is the only write interface for decision attribution events. The month-3
Redis-backed queue migration should keep these public functions stable and swap
only the in-memory enqueue/flush internals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from db._jsonb_safe import _json_safe
from db.database import database
from services.catalog_identity import is_content_key
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
_CATALOG_OFFER_PREFIX = "of:catalog_offer:"
_RESOLVE_CONCURRENCY = 2
_RESOLVE_SEMAPHORE: Optional[asyncio.Semaphore] = None
_RESOLVE_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None

try:  # Row-level failures: one row's value or constraint, not the database. Only these are replayed.
    from asyncpg.exceptions import DataError as _PgDataError
    from asyncpg.exceptions import IntegrityConstraintViolationError as _PgIntegrityError

    _ROW_LEVEL_ERRORS: Tuple[type, ...] = (_PgDataError, _PgIntegrityError)
except ImportError:  # pragma: no cover - asyncpg is a hard dependency in every deployed image
    _ROW_LEVEL_ERRORS = ()
_OFFER_ID_MAX = 255  # catalog_offer_id VARCHAR(255) on every ledger table (migration 140)


def _json_param(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(_json_safe(value), separators=(",", ":"), default=str)


def _sql(op: str) -> str:
    if op not in _SQL:
        table, cols, conflict, json_cols = _SPEC[op]
        values = [f"CAST(:{col} AS jsonb)" if col in json_cols else f":{col}" for col in cols]
        _SQL[op] = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(values)}) ON CONFLICT ({conflict}) DO NOTHING"
    return _SQL[op]


def _row_content_key(row: Dict[str, Any]) -> Optional[str]:
    """content_key is VARCHAR(40) holding "ck_"+32hex (migration 140, services/catalog_identity).

    Search cards usually carry only a product_key (VARCHAR(255)). Writers used to pass that through
    as the content_key, and one product_key longer than 40 chars failed the whole slate's insert:
    164 slates lost every candidate and exposure row 09-30..10-29. Anything that is not a
    content_key is NULL here; resolve_card_identities() maps product_key -> content_key first."""
    value = row.get("content_key")
    return value if is_content_key(value) else None


def _row_offer_id(row: Dict[str, Any]) -> Optional[str]:
    """The catalog_offers.offer_id when the card names one: offers.resolve prefixes catalog offers
    with "of:catalog_offer:" (routes/agent_shop_gateway.py _catalog_offer_entry), which never joins
    catalog_offers, so the prefix is stripped. Other offer ids (of:internal_checkout:...) are kept."""
    value = str(row.get("catalog_offer_id") or row.get("offer_id") or "").strip()
    if value.startswith(_CATALOG_OFFER_PREFIX):
        value = value[len(_CATALOG_OFFER_PREFIX):].strip()
    return value if value and len(value) <= _OFFER_ID_MAX else None


def _row_product_keys(row: Dict[str, Any]) -> List[str]:
    """The card's product_key, then its attached_product_key (external-seed cards), in that order."""
    keys: List[str] = []
    for name in ("product_key", "attached_product_key"):
        key = str(row.get(name) or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


async def resolve_card_identities(rows: List[Dict[str, Any]]) -> None:
    """Fill each card row's content_key from catalog_products by product_key, and drop a catalog
    offer id that is not in catalog_offers, in place. Two primary-key lookups per slate; run it in
    the background recording task, never on the response path. Fail-soft: on a DB error the rows
    keep whatever they carried, and the store's own guards still apply.

    The lookups run in a fresh context (spawn_isolated): the writers call this from a task spawned
    off the search request, and databases 0.7 would otherwise share that request's Connection.
    They are bounded (_bounded_fetch): at most two in flight per process and a short deadline, so
    under pool pressure they give up (content_key stays NULL) instead of queueing for a connection
    beside foreground requests."""
    cards = [row for row in rows if isinstance(row, dict)]
    product_keys = sorted({
        key for row in cards if not is_content_key(row.get("content_key")) for key in _row_product_keys(row)
    })
    offer_ids = sorted({
        offer_id for offer_id in (_row_offer_id(row) for row in cards)
        if offer_id and not offer_id.startswith("of:")
    })
    try:
        if product_keys:
            found = await _bounded_fetch(
                "SELECT product_key, content_key FROM catalog_products WHERE product_key = ANY(:keys)",
                {"keys": product_keys}, name="decision_ledger_content_keys",
            )
            by_product = {r["product_key"]: r["content_key"] for r in found if is_content_key(r["content_key"])}
            for row in cards:
                if not is_content_key(row.get("content_key")):
                    key = next((by_product[k] for k in _row_product_keys(row) if k in by_product), None)
                    if key:
                        row["content_key"] = key
        if offer_ids:
            found = await _bounded_fetch(
                "SELECT offer_id FROM catalog_offers WHERE offer_id = ANY(:ids)", {"ids": offer_ids},
                name="decision_ledger_offer_ids",
            )
            known = {r["offer_id"] for r in found}
            for row in cards:
                offer_id = _row_offer_id(row)
                if offer_id and not offer_id.startswith("of:") and offer_id not in known:
                    row["catalog_offer_id"] = None
                    row["offer_id"] = None
    except Exception as exc:  # noqa: BLE001 (TimeoutError included: the rows just stay unresolved)
        _report(exc, op="resolve_card_identities", event="read_failed")


def _resolve_timeout_seconds() -> float:
    try:
        return max(0.1, float(os.getenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS") or 2.0))
    except ValueError:
        return 2.0


def _resolve_semaphore() -> asyncio.Semaphore:
    global _RESOLVE_SEMAPHORE, _RESOLVE_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _RESOLVE_SEMAPHORE is None or _RESOLVE_SEMAPHORE_LOOP is not loop:
        _RESOLVE_SEMAPHORE = asyncio.Semaphore(_RESOLVE_CONCURRENCY)
        _RESOLVE_SEMAPHORE_LOOP = loop
    return _RESOLVE_SEMAPHORE


async def _bounded_fetch(query: str, values: Dict[str, Any], *, name: str) -> List[Any]:
    """database.fetch_all in a fresh context, at most _RESOLVE_CONCURRENCY at once, and within one
    deadline that covers waiting for the semaphore, the pool checkout and the query. On timeout the
    isolated task is cancelled with the wait."""
    from services.scheduler_job_runner import spawn_isolated

    async def _run() -> List[Any]:
        async with _resolve_semaphore():
            return await spawn_isolated(database.fetch_all(query, values), name=name)

    return await asyncio.wait_for(_run(), timeout=_resolve_timeout_seconds())


def _report(
    exc: Exception, *, op: str, payload: Optional[Dict[str, Any]] = None,
    rows: Optional[int] = None, failed_rows: Optional[int] = None, event: str = "write_failed",
) -> None:
    # sentry_sdk is not installed in prod, so capture_exception only logs at ERROR; this line is
    # the countable record (ids and counts only, never row values). Reads that fail (identity
    # lookups) say read_failed, so they never inflate the write-failure count.
    logger.warning(json.dumps({
        "event": f"agent_decision_event_store.{event}", "op": op,
        "decision_id": (payload or {}).get("decision_id"), "rows": rows, "failed_rows": failed_rows,
        "error": f"{type(exc).__name__}: {exc}"[:300],
    }, default=str))
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
    await _enqueue("decision", _decision_payload(
        decision_id=decision_id, merchant_id=merchant_id, brand_id=brand_id, surface=surface,
        channel=channel, agent_context=agent_context, protocol=protocol,
        attribution_model=attribution_model, attribution_window_seconds=attribution_window_seconds,
    ))
    return decision_id


def _decision_payload(
    *, decision_id: str, merchant_id: Optional[str], brand_id: Optional[str], surface: Optional[str],
    channel: Optional[str], agent_context: Optional[Dict[str, Any]], protocol: str,
    attribution_model: str, attribution_window_seconds: int,
) -> Dict[str, Any]:
    return {
        "decision_id": decision_id, "merchant_id": merchant_id, "brand_id": brand_id,
        "surface": surface, "channel": channel, "agent_context": _json_param(agent_context),
        "protocol": validate_protocol(protocol), "attribution_model": attribution_model,
        "attribution_window_seconds": attribution_window_seconds,
    }


async def claim_decision_event(
    *, decision_id: str, merchant_id: Optional[str] = None, brand_id: Optional[str] = None,
    surface: Optional[str] = None, channel: Optional[str] = None,
    agent_context: Optional[Dict[str, Any]] = None, protocol: str = DEFAULT_PROTOCOL,
    attribution_model: str = "last_agent_decision_v1", attribution_window_seconds: int = 2592000,
) -> Optional[Dict[str, Any]]:
    """Write the decision row NOW, not through the queue, for a caller that must refuse a reused
    decision_id (the gateway slate ingest). One INSERT ... ON CONFLICT DO NOTHING RETURNING, so two
    racing calls cannot both win. Returns None when this call created the row, else the existing
    row as {"surface", "agent_context"} for the caller to compare."""
    payload = _decision_payload(
        decision_id=decision_id, merchant_id=merchant_id, brand_id=brand_id, surface=surface,
        channel=channel, agent_context=agent_context, protocol=protocol,
        attribution_model=attribution_model, attribution_window_seconds=attribution_window_seconds,
    )
    created = await database.fetch_one(_sql("decision") + " RETURNING decision_id", payload)
    if created is not None:
        return None
    existing = await database.fetch_one(
        "SELECT surface, agent_context FROM agent_decision_events WHERE decision_id = :decision_id",
        {"decision_id": decision_id},
    )
    context = existing["agent_context"] if existing is not None else None
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except Exception:  # noqa: BLE001
            context = None
    return {
        "surface": existing["surface"] if existing is not None else None,
        "agent_context": context if isinstance(context, dict) else {},
    }


def _candidate_values(decision_id: str, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "candidate_id": str(row.get("candidate_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": _row_content_key(row), "catalog_offer_id": _row_offer_id(row),
        "rank_position": row.get("rank_position", row.get("position", idx)),
        "eligibility_flags": _json_param(row.get("eligibility_flags") or row.get("flags")),
    } for idx, row in enumerate(rows) if isinstance(row, dict)]


def _exposure_values(decision_id: str, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{
        "exposure_id": str(row.get("exposure_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": _row_content_key(row), "catalog_offer_id": _row_offer_id(row),
        "exposure_position": row.get("exposure_position", row.get("position", idx)), "slot": row.get("slot"),
    } for idx, row in enumerate(rows) if isinstance(row, dict)]


async def record_decision_candidates(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = _candidate_values(decision_id, rows)
    if values:
        await _enqueue("candidates", {"decision_id": decision_id, "rows": values})


async def record_exposure_events(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = _exposure_values(decision_id, rows)
    if values:
        await _enqueue("exposures", {"decision_id": decision_id, "rows": values})


async def write_slate_rows(decision_id: str, rows: List[Dict[str, Any]]) -> int:
    """Write a slate's candidate and exposure rows NOW, not through the queue, for a caller whose
    answer tells its client whether to retry (the gateway ingest: a 202 means "do not resend").

    Returns how many rows were lost to row-level errors (a bad value or constraint loses only its
    row). Anything else (pool, connection, timeout) raises, so the caller can answer 5xx and the
    client's retry, an identical replay under the same row ids, fills the slate in.
    Must not run inside a transaction (see _write_many)."""
    failed = 0
    for op, values in (("candidates", _candidate_values(decision_id, rows)),
                       ("exposures", _exposure_values(decision_id, rows))):
        if values:
            failed += await _write_many(op, {"decision_id": decision_id, "rows": values}, raise_systemic=True)
    return failed


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
    # Same column guards as the slate rows: catalog_offer_id must join what the exposures stored
    # (prefix stripped), and an over-long content_key would otherwise lose the whole checkout row.
    refs = {"content_key": content_key, "catalog_offer_id": catalog_offer_id}
    await _enqueue("checkout", {
        "checkout_decision_id": checkout_decision_id, "decision_id": decision_id,
        "order_id": order_id, "merchant_id": merchant_id, "content_key": _row_content_key(refs),
        "catalog_offer_id": _row_offer_id(refs), "purchase_route": purchase_route,
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


async def _write_many(op: str, payload: Dict[str, Any], *, raise_systemic: bool = False) -> int:
    """Insert a batch; return how many rows were lost.

    execute_many runs one INSERT per row with no transaction, so on a ROW-LEVEL error (a value or a
    constraint: asyncpg DataError / IntegrityConstraintViolationError) the rows before the failing
    one are already in, and the batch is replayed row by row (ON CONFLICT DO NOTHING keeps that
    idempotent) so one bad card loses only itself.

    Any other error (pool checkout, connection, timeout) is about the database, not a row: it is
    reported once and NOT replayed. Replaying it would repeat the same wait once per row and stall
    the single flush worker while the shared queue overflows. The worker counts the batch lost;
    write_slate_rows (raise_systemic=True) re-raises so its caller can ask the client to retry.

    NEVER call this inside a transaction: the first failing INSERT aborts it, every replayed row
    then fails, and the rollback discards the rows that did land (0 rows on real PG). The flush
    worker runs in a fresh context (spawn_isolated in _ensure_worker), so it holds none."""
    rows = payload["rows"]
    try:
        await database.execute_many(_sql(op), rows)
        return 0
    except _ROW_LEVEL_ERRORS:
        pass
    except Exception as exc:  # noqa: BLE001
        _report(exc, op=op, payload=payload, rows=len(rows), failed_rows=None)
        if raise_systemic:
            raise
        return len(rows)
    failed = 0
    last_exc: Optional[Exception] = None
    for idx, row in enumerate(rows):
        try:
            await database.execute(_sql(op), row)
        except _ROW_LEVEL_ERRORS as exc:
            failed += 1
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 - the database went away mid-replay: stop
            _report(exc, op=op, payload=payload, rows=len(rows), failed_rows=None)
            if raise_systemic:
                raise
            return failed + (len(rows) - idx)
    if last_exc is not None:
        _report(last_exc, op=op, payload=payload, rows=len(rows), failed_rows=failed)
    return failed


async def _flush_loop(queue: asyncio.Queue[Tuple[str, Dict[str, Any]]]) -> None:
    while True:
        op, payload = await queue.get()
        try:
            if op in _MANY:
                await _write_many(op, payload)
            else:
                await database.execute(_sql(op), payload)
        except Exception as exc:  # noqa: BLE001
            _report(exc, op=op, payload=payload)
        finally:
            queue.task_done()
