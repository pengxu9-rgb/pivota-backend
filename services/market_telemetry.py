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

Fields, all additive, on one ``multi.invoke.market`` log event per request:
  market_requested   the value the caller sent, verbatim, capped; else None
  market_source      where it was named -- explicit_search | explicit_payload |
                     explicit_payload_metadata | explicit_metadata | explicit_locale | defaulted
  market_resolved    what the pivot lane resolved, or None when that lane did not run
  market_bound       the markets seed recall was called with, in call order ("*" = no partition);
                     None when no seed recall ran
  lane               the response's query_source
  served_via         fresh | dedup_cache | dedup_inflight | pending
  upstream_fallback_attempted   True when THIS request relied on the upstream fallback. In prod
                     that URL points back at this same service, so a request that falls back is
                     answered by a second request -- which is why the next field exists.
  upstream_fallback_hop         0 for a request from a caller; >0 for the request the fallback
                     itself made. Filter on hop == 0 to count each caller request once; without it
                     a fallen-back request is recorded twice.
  served_currencies / served_currency_mismatch   as on the Node door

The main route is the thing being measured. A healthy request is ``upstream_fallback_hop == 0``
and ``upstream_fallback_attempted is False``: answered by this door's own lanes.
"""

from __future__ import annotations

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
    return {
        **describe_requested(raw_payload, envelope_metadata),
        "market_resolved": observed.get("market_resolved"),
        "market_bound": list(bound) if isinstance(bound, list) else None,
        "lane": metadata.get("query_source"),
        "upstream_fallback_attempted": bool(metadata.get("upstream_fallback_attempted")),
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
    "OBSERVATION_KEY",
    "build_record",
    "describe_requested",
    "observe_bound",
    "observe_resolved",
    "served_via",
    "strip_for_forwarding",
    "summarise_served_products",
)
