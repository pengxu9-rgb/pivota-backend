"""Market telemetry for ``find_products_multi`` on this door (``/agent/shop/v1/invoke``).

WHY THIS EXISTS. Nothing records whether a caller names a market. Measured on prod
2026-09-18, this door emits no market, locale or region field for ``find_products_multi``, and
neither does the Node gateway (PIVOTA-Agent #2239 adds it there). So three decisions in the
market-serving design have no data behind them: what a market-less request should default to,
whether the two doors agree, and how much traffic would move if the default changed.

WHAT THIS DOOR ACTUALLY DOES WITH A MARKET -- the reason every field below exists. Measured
2026-09-18, one query returned byte-identical results for ``market: SG`` and ``market: US`` on
every lane of this door:

* ``search.market`` is DROPPED by pydantic: ``MultiSearchFilters`` has no such field.
* The pivot lane (``pivot_semantic_core_multi``) RESOLVES a market from the invoke-envelope
  ``metadata.market``/``locale`` (``_pivot_market_from_payload``) -- but canonical recall takes no
  market parameter, so the resolved value reaches only a tag and a conditional external fallback.
* The legacy seed lane binds ``market=None``: no partition at all.

So on this door, what the caller NAMED, what the door RESOLVED, and what recall BOUND are three
different things. They are recorded separately so the gap between them is measurable.

RECORDED AT THE POINT OF USE, NEVER RE-DERIVED. The Node half of this work first shipped a
``market_bound`` computed from its own guess at the door's input, and review found the guess
wrong three ways (PIVOTA-Agent #2239). So here the lanes call ``observe_resolved`` /
``observe_bound`` beside the line that uses the value, with the value they use.

THE OBSERVATION TRAVELS IN THE REQUEST'S OWN METADATA DICT, NOT A CONTEXTVAR. This door's task
queue starts a waiting task from inside whichever request just finished
(``AgentTaskManager._promote_next_locked`` -> ``asyncio.create_task``), and ``create_task`` copies
the CURRENT context -- so an ambient per-request context would hand one request's observation to
another request. The metadata dict is captured by the queued closure itself, so it always belongs
to the request that owns it. Its key starts with ``_`` so ``RequestMetadata(**...)`` ignores it,
and ``_invoke_multi_upstream_fallback`` strips it before forwarding metadata upstream.

Fields, all additive, on one ``multi.invoke.market`` event per request -- a JSON line on stdout
(see ``emit``), which Cloud Run stores as a queryable ``jsonPayload``:
  market_requested   the value the caller sent, verbatim, capped; else None
  market_source      where it was named -- explicit_search | explicit_payload |
                     explicit_payload_metadata | explicit_metadata | explicit_locale | defaulted
  market_resolved    what the pivot lane resolved, or None when that lane did not run
  market_bound       the partition every seed-recall call in this request bound, in call order --
                     recorded inside fetch_external_seed_rows, so every lane is covered ("*" = no
                     partition); None when no seed recall ran
  lane               the response's query_source
  served_via         fresh | dedup_cache | dedup_inflight | pending
  upstream_fallback_applied     True when the upstream fallback SERVED this request's result
  upstream_fallback_attempted   True when the fallback was tried at all (served or failed). In prod
                     that URL points back at this same service, so a request that falls back is
                     answered by a second request -- which is why the next field exists.
  upstream_fallback_hop         0 for a request from a caller; >0 for the request the fallback
                     itself made. Filter on hop == 0 to count each caller request once; without it
                     a fallen-back request is recorded twice.
  served_currencies / served_currency_mismatch   as on the Node door

The main route is the thing being measured. A healthy request is ``upstream_fallback_hop == 0``
and ``upstream_fallback_attempted is False``: answered by this door's own lanes, never by the
fallback.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from typing import Any, Dict, Iterable, List, Optional

OBSERVATION_KEY = "_market_observation"
MAX_REQUESTED_CHARS = 16
MAX_CURRENCIES = 8
MAX_BOUND = 8
UNPARTITIONED = "*"


def _cap(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_REQUESTED_CHARS else f"{text[:MAX_REQUESTED_CHARS]}…"


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def describe_requested(raw_payload: Any, envelope_metadata: Any) -> Dict[str, Any]:
    """Where the caller named a market, read from the RAW request -- before pydantic drops
    ``search.market``. That this door ignores what it is sent is exactly what is being measured,
    so the parsed payload would be the wrong thing to read.

    Truthiness is plain Python truthiness on the raw value, in a fixed order. It reports what the
    caller SAID; it decides nothing.
    """
    payload = _as_dict(raw_payload)
    search = _as_dict(payload.get("search"))
    payload_metadata = _as_dict(payload.get("metadata"))
    envelope = _as_dict(envelope_metadata)
    for source, value in (
        ("explicit_search", search.get("market")),
        ("explicit_payload", payload.get("market")),
        ("explicit_payload_metadata", payload_metadata.get("market")),
        ("explicit_metadata", envelope.get("market")),
        ("explicit_locale", envelope.get("locale")),
    ):
        if value:
            return {"market_requested": _cap(value), "market_source": source}
    return {"market_requested": None, "market_source": "defaulted"}


def _observation(request_metadata: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(request_metadata, dict):
        return None
    store = request_metadata.get(OBSERVATION_KEY)
    return store if isinstance(store, dict) else None


def observe_resolved(request_metadata: Any, market: Any) -> None:
    """Called by the pivot lane beside its resolution, with the value it hands recall.
    Never raises: telemetry must not be able to fail the surface it measures."""
    try:
        store = _observation(request_metadata)
        if store is not None:
            store["market_resolved"] = None if market is None else str(market)
    except Exception:  # pragma: no cover - defensive
        pass


def observe_bound(request_metadata: Any, market: Any) -> None:
    """Called beside each seed-recall call, with the market that call was given. ``None`` --
    no partition -- is recorded as ``"*"`` so it is distinguishable from "no recall ran"."""
    try:
        store = _observation(request_metadata)
        if store is None:
            return
        bound = store.setdefault("market_bound", [])
        if isinstance(bound, list) and len(bound) < MAX_BOUND:
            bound.append(UNPARTITIONED if market is None else str(market))
    except Exception:  # pragma: no cover - defensive
        pass


# --- seed binds, recorded at the ONE function every seed bind goes through -----------------------
#
# Review of this PR found a bind site nobody observed: the pivot lane's own external fallback
# (services/pivot_query_service.py) binds ``market=request.market`` for most beauty pages, and the
# record said "no seed recall ran". Chasing call sites is how both reviews of this work found a
# missed one, so the bind is recorded inside ``fetch_external_seed_rows`` itself -- every seed bind
# on this door, from any lane, goes through it.
#
# That needs a per-request sink the seed function can reach without being handed the request. A
# ContextVar is right HERE even though it was wrong for the observation itself: the sink is opened
# by ``_handle_find_products_multi`` from INSIDE the request's own task and closed on its way out,
# so it never crosses the task queue (whose promotion from another request's task is what made an
# ambient context unsafe). Child tasks created inside the handler copy the context and share the
# same dict object, so their binds land in the right request too.

_SEED_BIND_SINK: ContextVar[Optional[Dict[str, Any]]] = ContextVar("market_telemetry_seed_bind_sink", default=None)


def open_seed_bind_sink(request_metadata: Any) -> Token:
    """Point seed-bind recording at THIS request's observation until ``close_seed_bind_sink``."""
    return _SEED_BIND_SINK.set(_observation(request_metadata))


def close_seed_bind_sink(token: Token) -> None:
    try:
        _SEED_BIND_SINK.reset(token)
    except Exception:  # pragma: no cover - defensive
        pass


def record_seed_bind(normalized_market: Any) -> None:
    """Called by ``fetch_external_seed_rows`` at the line that decides ``market = :market``, with the
    value that decision used. Empty means no partition. No-op outside a find_products_multi request."""
    try:
        store = _SEED_BIND_SINK.get()
        if store is not None:
            observe_bound({OBSERVATION_KEY: store}, normalized_market or None)
    except Exception:  # pragma: no cover - defensive
        pass


# --- emission ---------------------------------------------------------------------------------
#
# Review of this PR, confirmed on prod logs: ``logger.info(..., extra={...})`` on the gateway's
# module logger is NEVER emitted in production (the root logger sits at WARNING -- ``multi.invoke
# .slow``, logged the same way, has zero entries in 7 days), and nothing renders ``extra`` even where
# INFO is emitted (the task queue's ``agent_queue.start`` arrives as bare text, every field dropped).
# The tests passed because pytest's log capture forces INFO through.
#
# So this record has its OWN logger and stdout handler, independent of the root configuration --
# the same pattern utils/logger.py uses for the ``pivota`` logger, which is why the queue's events
# do reach prod -- and writes ONE JSON object per line. Cloud Run parses a JSON stdout line into
# ``jsonPayload`` (verified on prod: the structured request log lands that way), so every field is
# queryable.

EVENT_NAME = "multi.invoke.market"
_EMIT_LOGGER_NAME = "pivota.market_telemetry"


class _StdoutJsonLineHandler(logging.Handler):
    """Writes each record's message -- already a JSON object -- as one line to the CURRENT
    ``sys.stdout`` (resolved per record, not at construction, so a swapped stream is honoured)."""

    _market_telemetry_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stdout.write(record.getMessage() + "\n")
            sys.stdout.flush()
        except Exception:  # pragma: no cover - defensive
            self.handleError(record)


def _emit_logger() -> logging.Logger:
    log = logging.getLogger(_EMIT_LOGGER_NAME)
    log.setLevel(logging.INFO)
    log.propagate = False
    if not any(getattr(h, "_market_telemetry_handler", False) for h in log.handlers):
        log.addHandler(_StdoutJsonLineHandler())
    return log


def emit(fields: Dict[str, Any]) -> None:
    """Write one ``multi.invoke.market`` event. Never raises."""
    try:
        _emit_logger().info(json.dumps({"event": EVENT_NAME, **fields}, default=str, ensure_ascii=False))
    except Exception:  # pragma: no cover - defensive
        pass


def summarise_served_products(products: Any) -> Dict[str, Any]:
    """Currencies on the served page. A row with no currency is ``unknown``, never a default;
    "mixed" means more than one KNOWN currency -- an unpriced row makes a page incomplete, not
    mixed. Same semantics as the Node door, so the two can be compared."""
    currencies = set()
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict):
            continue
        currency = str(product.get("currency") or product.get("price_currency") or "").strip().upper()
        currencies.add(currency or "unknown")
    served = sorted(currencies)[:MAX_CURRENCIES]
    known = [code for code in served if code != "unknown"]
    return {"served_currencies": served, "served_currency_mismatch": len(known) > 1}


def served_via(*, dedup_cache_hit: bool, dedup_inflight_joined: bool, result: Any) -> str:
    """How THIS request got its result. Only ``fresh`` means this request's own lanes ran --
    for the others the observation is empty, because another request did the work."""
    if dedup_cache_hit:
        return "dedup_cache"
    if dedup_inflight_joined:
        return "dedup_inflight"
    if isinstance(result, dict) and result.get("status") == "pending":
        return "pending"
    return "fresh"


def build_record(
    *,
    raw_payload: Any,
    envelope_metadata: Any,
    observation: Any,
    result: Any,
    dedup_cache_hit: bool = False,
    dedup_inflight_joined: bool = False,
) -> Dict[str, Any]:
    observed = observation if isinstance(observation, dict) else {}
    bound = observed.get("market_bound")
    response = result if isinstance(result, dict) else {}
    metadata = _as_dict(response.get("metadata"))
    try:
        hop = int(_as_dict(envelope_metadata).get("upstream_fallback_hop") or 0)
    except (TypeError, ValueError):
        hop = 0
    # Review of this PR: ``upstream_fallback_attempted`` on the served response is NOT this request's
    # flag when the fallback SUCCEEDS -- the served dict is the second request's answer, carrying ITS
    # own ``false``. The helper stamps ``upstream_fallback.applied`` on a result the fallback served,
    # so that is the truthful "served by the fallback" signal; ``attempted`` alone is set only on the
    # paths where the fallback failed and the door answered locally.
    applied = bool(_as_dict(metadata.get("upstream_fallback")).get("applied"))
    return {
        **describe_requested(raw_payload, envelope_metadata),
        "market_resolved": observed.get("market_resolved"),
        "market_bound": list(bound) if isinstance(bound, list) else None,
        "lane": metadata.get("query_source"),
        "upstream_fallback_applied": applied,
        "upstream_fallback_attempted": applied or bool(metadata.get("upstream_fallback_attempted")),
        "upstream_fallback_hop": max(0, hop),
        "served_via": served_via(
            dedup_cache_hit=dedup_cache_hit,
            dedup_inflight_joined=dedup_inflight_joined,
            result=result,
        ),
        **summarise_served_products(response.get("products")),
    }


def strip_for_forwarding(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Remove the observation before a metadata dict leaves this process."""
    metadata.pop(OBSERVATION_KEY, None)
    return metadata


__all__: Iterable[str] = (
    "EVENT_NAME",
    "OBSERVATION_KEY",
    "build_record",
    "close_seed_bind_sink",
    "emit",
    "open_seed_bind_sink",
    "record_seed_bind",
    "describe_requested",
    "observe_bound",
    "observe_resolved",
    "served_via",
    "strip_for_forwarding",
    "summarise_served_products",
)
