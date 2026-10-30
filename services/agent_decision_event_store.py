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
_OFFER_ID_MAX = 255  # catalog_offer_id VARCHAR(255) on every ledger table (migration 140)
# agent_decision_funnel_links' foreign keys that a miss nulls rather than losing the link (all ON
# DELETE SET NULL in migration 140). Their parents are written outside this queue, so a miss means
# the value never joins (a product_key-as-content_key, a deleted offer or edge).
# exposure_id and checkout_decision_id are SET NULL too but are not in here: their rows go through
# this same async queue, possibly on another instance, so a miss can mean "not written yet". Nulling
# would be permanent (the re-send hits ON CONFLICT DO NOTHING; the backfill skips linked events), so
# that link is dropped and reported as before, and a re-send or the backfill writes it in full.
# decision_id (RESTRICT) and funnel_event_id (NOT NULL) are what the link is about.
_LINK_OPTIONAL_REFS = ("content_key", "catalog_offer_id", "commerce_attribution_edge_id")
_LINK_TABLE = "agent_decision_funnel_links"

try:
    from asyncpg.exceptions import ForeignKeyViolationError as _FK_VIOLATION
except ImportError:  # pragma: no cover - asyncpg is a hard dependency in every deployed image
    class _FK_VIOLATION(Exception):  # type: ignore[no-redef]
        pass


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
    off the search request, and databases 0.7 would otherwise share that request's Connection."""
    from services.scheduler_job_runner import spawn_isolated

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
            found = await spawn_isolated(database.fetch_all(
                "SELECT product_key, content_key FROM catalog_products WHERE product_key = ANY(:keys)",
                {"keys": product_keys},
            ), name="decision_ledger_content_keys")
            by_product = {r["product_key"]: r["content_key"] for r in found if is_content_key(r["content_key"])}
            for row in cards:
                if not is_content_key(row.get("content_key")):
                    key = next((by_product[k] for k in _row_product_keys(row) if k in by_product), None)
                    if key:
                        row["content_key"] = key
        if offer_ids:
            found = await spawn_isolated(database.fetch_all(
                "SELECT offer_id FROM catalog_offers WHERE offer_id = ANY(:ids)", {"ids": offer_ids}
            ), name="decision_ledger_offer_ids")
            known = {r["offer_id"] for r in found}
            for row in cards:
                offer_id = _row_offer_id(row)
                if offer_id and not offer_id.startswith("of:") and offer_id not in known:
                    row["catalog_offer_id"] = None
                    row["offer_id"] = None
    except Exception as exc:  # noqa: BLE001
        _report(exc, op="resolve_card_identities")


def _report(
    exc: Exception, *, op: str, payload: Optional[Dict[str, Any]] = None,
    rows: Optional[int] = None, failed_rows: Optional[int] = None,
) -> None:
    # sentry_sdk is not installed in prod, so capture_exception only logs at ERROR; this line is
    # the countable record (ids and counts only, never row values).
    logger.warning(json.dumps({
        "event": "agent_decision_event_store.write_failed", "op": op,
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


async def record_decision_candidates(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = [{
        "candidate_id": str(row.get("candidate_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": _row_content_key(row), "catalog_offer_id": _row_offer_id(row),
        "rank_position": row.get("rank_position", row.get("position", idx)),
        "eligibility_flags": _json_param(row.get("eligibility_flags") or row.get("flags")),
    } for idx, row in enumerate(rows) if isinstance(row, dict)]
    if values:
        await _enqueue("candidates", {"decision_id": decision_id, "rows": values})


async def record_exposure_events(decision_id: str, rows: Iterable[Dict[str, Any]]) -> None:
    values = [{
        "exposure_id": str(row.get("exposure_id") or uuid.uuid4()), "decision_id": decision_id,
        "content_key": _row_content_key(row), "catalog_offer_id": _row_offer_id(row),
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
    # Same column guards as the slate rows: these come from order metadata, and both columns are
    # foreign keys here, so a prefixed offer id or a product_key-as-content_key failed the whole
    # link. A value that passes and still joins nothing is nulled at write time (_write_funnel_link).
    refs = {"content_key": content_key, "catalog_offer_id": catalog_offer_id}
    row_content_key, row_offer_id = _row_content_key(refs), _row_offer_id(refs)
    if content_key and row_content_key is None:
        _report_enqueue_null("content_key", content_key, funnel_event_id=funnel_event_id, decision_id=decision_id)
    offer_text = str(catalog_offer_id or "").strip()
    if offer_text and row_offer_id is None:
        _report_enqueue_null("catalog_offer_id", offer_text, funnel_event_id=funnel_event_id, decision_id=decision_id)
    if row_offer_id and row_offer_id.startswith("of:"):
        # of:internal_checkout:... never joins catalog_offers (resolve_card_identities skips them for
        # the same reason); nulled here so the expected case costs no failed INSERT and no warning.
        logger.debug("funnel link: synthetic offer id nulled before the insert")
        row_offer_id = None
    await _enqueue("funnel_link", {
        "link_id": link_id, "decision_id": decision_id, "funnel_event_id": funnel_event_id,
        "exposure_id": exposure_id, "checkout_decision_id": checkout_decision_id,
        "content_key": row_content_key, "catalog_offer_id": row_offer_id,
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


async def _write_many(op: str, payload: Dict[str, Any]) -> None:
    rows = payload["rows"]
    try:
        await database.execute_many(_sql(op), rows)
        return
    except Exception:  # noqa: BLE001
        pass
    # execute_many runs one INSERT per row with no transaction, so the rows before the failing one
    # are already in. Replay row by row (ON CONFLICT DO NOTHING keeps that idempotent) so one bad
    # card loses only itself, not the whole slate.
    # NEVER call this inside a transaction: the first failing INSERT aborts it, every replayed row
    # then fails, and the rollback discards the rows that did land (0 rows on real PG). The flush
    # worker runs in a fresh context (spawn_isolated in _ensure_worker), so it holds none.
    failed = 0
    last_exc: Optional[Exception] = None
    for row in rows:
        try:
            await database.execute(_sql(op), row)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            last_exc = exc
    if last_exc is not None:
        _report(last_exc, op=op, payload=payload, rows=len(rows), failed_rows=failed)


def _report_enqueue_null(column: str, value: Any, **ids: Any) -> None:
    """A writer passed a value the column cannot hold (a product_key as the content_key, an offer id
    over 255 chars): the countable record, with its length and first characters, never the value."""
    text = str(value)
    logger.warning(json.dumps({
        "event": "agent_decision_event_store.funnel_link_refs_nulled", "op": "funnel_link",
        "stage": "enqueue", "column": column, "length": len(text), "prefix": text[:3], **ids,
    }, default=str))


def _fk_column(exc: Exception) -> Optional[str]:
    """The funnel-link column a ForeignKeyViolationError names, from its structured fields (the
    message text depends on lc_messages): the default constraint name '<table>_<col>_fkey', on this
    table only, so an FK error raised elsewhere (a trigger) never nulls a link column."""
    if str(getattr(exc, "table_name", "") or "") != _LINK_TABLE:
        return None
    constraint = str(getattr(exc, "constraint_name", "") or "")
    return next((col for col in _SPEC["funnel_link"][1] if constraint == f"{_LINK_TABLE}_{col}_fkey"), None)


async def _write_funnel_link(payload: Dict[str, Any]) -> None:
    """Insert one funnel link; on a foreign-key miss in a reference that never joins later (see
    _LINK_OPTIONAL_REFS), null that column and insert again, so a stale or synthetic id loses only
    itself, not the order -> decision edge.

    Each retry nulls a different column, so this ends within len(_LINK_OPTIONAL_REFS) + 1 tries. A
    miss on any other column, and any other error, propagates to the flush loop, which reports it.
    A link that arrives naming neither a decision nor a checkout decision is written as given
    (decision_id is nullable; the row still carries the funnel event and the attribution edge)."""
    row = dict(payload)
    nulled: List[str] = []
    inserted: Any = None
    while True:
        try:
            if not nulled:
                await database.execute(_sql("funnel_link"), row)
                break
            # RETURNING tells an insert from an ON CONFLICT no-op (another writer linked this
            # funnel event meanwhile), so the record below counts only rows that landed nulled.
            inserted = await database.fetch_val(_sql("funnel_link") + " RETURNING link_id", row)
            break
        except _FK_VIOLATION as exc:
            column = _fk_column(exc)
            if column not in _LINK_OPTIONAL_REFS or row.get(column) is None:
                raise
            row[column] = None
            nulled.append(column)
    if nulled and inserted is not None:
        # the countable record (ids and column names only, never values)
        logger.warning(json.dumps({
            "event": "agent_decision_event_store.funnel_link_refs_nulled", "op": "funnel_link",
            "stage": "write", "link_id": inserted, "decision_id": row.get("decision_id"),
            "funnel_event_id": row.get("funnel_event_id"), "columns": nulled,
        }, default=str))


async def _flush_loop(queue: asyncio.Queue[Tuple[str, Dict[str, Any]]]) -> None:
    while True:
        op, payload = await queue.get()
        try:
            if op in _MANY:
                await _write_many(op, payload)
            elif op == "funnel_link":
                await _write_funnel_link(payload)
            else:
                await database.execute(_sql(op), payload)
        except Exception as exc:  # noqa: BLE001
            _report(exc, op=op, payload=payload)
        finally:
            queue.task_done()
