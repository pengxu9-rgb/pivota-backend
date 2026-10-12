"""The market registry: one checked-in source for every market fact (ADR-026 decision 2).

`config/market_registry.json` holds one entry per ISO-3166-1 alpha-2 market: its pricing currency, its
rollout state (off / acquisition / shadow / served), whether it is a measured pricing region, whether the
retailer_ingest lane may ingest it and the shopify_markets capture may target it, its locales and how its
prices are displayed. This module loads and validates that file at import and derives every list from it.

P1 STEP 1 IS PARITY, NOT A SWITCH. Nothing reads these accessors yet. tests/test_market_registry.py
asserts that each one equals the constant it will replace -- region_pricing.REGION_PRICING_CURRENCY and
ACQUISITION_MARKETS, retailer_ingest.pipeline.INGEST_MARKETS, shopify_markets.CAPTURE_MARKETS, the Reap
lane's _MARKET_CURRENCY, the gateway's buyerRegion map -- so the later switch of each constant to its
accessor is a no-op by construction, and until then the registry cannot drift from them.

WHAT IS DELIBERATELY NOT HERE:
  * The SERVED regions in effect at runtime ARE `served_markets()` since 2026-10-12
    (index_pipeline_state_service.serving_pricing_regions reads it; PIVOTA_SERVING_PRICING_REGIONS may only
    narrow it, never widen it).
  * No FX, no conversion, no inference of a market from a currency (ADR-024).
  * No fallback: an unknown market raises (`market()`), exactly as region_pricing does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "market_registry.json"

STATUSES = ("off", "acquisition", "shadow", "served")
PRICE_DISPLAYS = ("tax_inclusive", "tax_exclusive")
_MARKET_CODE = re.compile(r"^[A-Z]{2}$")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_FIELDS = {"currency": str, "status": str, "pricing_region": bool, "ingest": bool, "capture": bool,
           "locales": list, "price_display": str}


def _validate(doc: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The registry's own rules. Raises ValueError naming the first entry that breaks one."""
    if not isinstance(doc, Mapping) or doc.get("version") != 1 or not isinstance(doc.get("markets"), Mapping):
        raise ValueError("market registry: expected {'version': 1, 'markets': {...}}")
    markets: Dict[str, Dict[str, Any]] = {}
    for code, entry in doc["markets"].items():
        if not _MARKET_CODE.match(str(code)):
            raise ValueError(f"market registry: {code!r} is not an ISO-3166-1 alpha-2 code")
        if not isinstance(entry, Mapping) or set(entry) != set(_FIELDS):
            raise ValueError(f"market registry: {code} must have exactly the fields {sorted(_FIELDS)}")
        for field, want in _FIELDS.items():
            if not isinstance(entry[field], want) or (want is int and isinstance(entry[field], bool)):
                raise ValueError(f"market registry: {code}.{field} must be {want.__name__}")
        if not _CURRENCY_CODE.match(entry["currency"]):
            raise ValueError(f"market registry: {code}.currency {entry['currency']!r} is not ISO-4217")
        if entry["status"] not in STATUSES:
            raise ValueError(f"market registry: {code}.status must be one of {STATUSES}")
        if entry["price_display"] not in PRICE_DISPLAYS:
            raise ValueError(f"market registry: {code}.price_display must be one of {PRICE_DISPLAYS}")
        if not entry["locales"] or not all(isinstance(loc, str) and _LOCALE.match(loc) for loc in entry["locales"]):
            raise ValueError(f"market registry: {code}.locales must be non-empty ll-CC tags")
        # A market that is acquired, shadowed or served must be ingestible and a measured pricing region;
        # only an `off` market may be neither. A capture target must be ingestible too.
        if entry["status"] != "off" and not (entry["ingest"] and entry["pricing_region"]):
            raise ValueError(f"market registry: {code} is {entry['status']} but not ingest + pricing_region")
        if entry["capture"] and not entry["ingest"]:
            raise ValueError(f"market registry: {code} is a capture target but not ingestible")
        markets[str(code)] = dict(entry)
    if not any(e["status"] == "served" for e in markets.values()):
        raise ValueError("market registry: at least one market must be served")
    return markets


def load(path: Path = REGISTRY_PATH) -> Dict[str, Dict[str, Any]]:
    return _validate(json.loads(Path(path).read_text(encoding="utf-8")))


MARKETS: Dict[str, Dict[str, Any]] = load()


def normalize_market(code: Any) -> str:
    return str(code or "").strip().upper()


def market(code: Any) -> Dict[str, Any]:
    """The entry for `code`. Raises ValueError for any market the registry does not list: no fallback."""
    normalized = normalize_market(code)
    try:
        return dict(MARKETS[normalized])
    except KeyError:
        raise ValueError(
            f"unknown market {normalized or code!r}; known: {', '.join(sorted(MARKETS))}") from None


def market_or_none(code: Any) -> Optional[Dict[str, Any]]:
    entry = MARKETS.get(normalize_market(code))
    return dict(entry) if entry else None


def currency_for(code: Any) -> str:
    return market(code)["currency"]


def _codes(predicate) -> Tuple[str, ...]:
    return tuple(code for code, entry in MARKETS.items() if predicate(entry))


def markets_with_status(*statuses: str) -> Tuple[str, ...]:
    unknown = set(statuses) - set(STATUSES)
    if unknown:
        raise ValueError(f"unknown status {sorted(unknown)}")
    return _codes(lambda e: e["status"] in statuses)


def served_markets() -> Tuple[str, ...]:
    return markets_with_status("served")


def acquisition_markets() -> Tuple[str, ...]:
    return markets_with_status("acquisition")


def ingest_markets() -> Tuple[str, ...]:
    return _codes(lambda e: e["ingest"])


def capture_markets() -> Tuple[str, ...]:
    return _codes(lambda e: e["capture"])


def pricing_region_currency() -> Dict[str, str]:
    """Market -> currency for the measured pricing regions (region_pricing.REGION_PRICING_CURRENCY's shape)."""
    return {code: entry["currency"] for code, entry in MARKETS.items() if entry["pricing_region"]}


def market_currency() -> Dict[str, str]:
    """Market -> currency for every listed market, pricing region or not."""
    return {code: entry["currency"] for code, entry in MARKETS.items()}


def gateway_projection() -> Dict[str, Any]:
    """The JSON the gateway reads (PIVOTA-Agent), generated by scripts/generate_market_registry_json.py.
    Sorted keys and a stable shape, so CI can compare the gateway's copy byte for byte."""
    return {
        "version": 1,
        "generated_from": "pivota-backend config/market_registry.json",
        "markets": {code: MARKETS[code] for code in sorted(MARKETS)},
    }


def gateway_projection_json() -> str:
    return json.dumps(gateway_projection(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


__all__: List[str] = [
    "REGISTRY_PATH", "STATUSES", "PRICE_DISPLAYS", "MARKETS", "load", "normalize_market", "market",
    "market_or_none", "currency_for", "markets_with_status", "served_markets", "acquisition_markets",
    "ingest_markets", "capture_markets", "pricing_region_currency", "market_currency",
    "gateway_projection", "gateway_projection_json",
]
