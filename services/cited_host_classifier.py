"""
Phase C-4 (PR-E): cited-host classifier.

The merchant audit report lists "non-merchant hosts cited in grounded
sources" as a flat array of hostnames (`merchant_view.receipts.top_cited_hosts`).
Merchants look at "mattressclarity.com" or "thewinners.ae" and don't
know what to do with that information — is this an editorial site I
should pitch? a retailer to onboard with? a low-priority regional
host?

This module loads a BD-curated registry (`data/cited_host_registry.json`)
and annotates each cited host with:

  - `type`         : editorial | retailer | marketplace | video | brand | unclassified
  - `subtype`      : finer-grain (review_site, department_store, ...)
  - `categories`   : merchant categories where this host has notable presence
  - `coverage_note`: 1-2 sentences on what this host actually publishes
  - `outreach_hint`: 1 sentence on which lever applies
  - `applies_to_merchant_category`: True/False/None — whether the host's
                                    `categories` list includes the merchant's
                                    category (helps the action ladder
                                    deprioritize hosts irrelevant to this
                                    merchant)

The registry is the source of truth for this knowledge — engineering
reviews schema, BD owns content. Unknown hosts get a graceful
unclassified fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logger import logger

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "cited_host_registry.json"
_REGISTRY_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _load_registry() -> Dict[str, Dict[str, Any]]:
    """Lazy-load the registry on first lookup. Returns an empty dict
    on read/parse failure so audit pipelines never crash because BD
    happened to ship malformed JSON — they just degrade to all hosts
    being unclassified."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        logger.warning(
            "cited_host_registry not found at %s — all hosts will be "
            "classified as 'unclassified' until the file is added.",
            _REGISTRY_PATH,
        )
        _REGISTRY_CACHE = {}
        return _REGISTRY_CACHE
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "cited_host_registry failed to load (%s) — all hosts will "
            "be classified as 'unclassified' until the file is fixed.",
            exc,
        )
        _REGISTRY_CACHE = {}
        return _REGISTRY_CACHE

    raw_hosts = doc.get("hosts") if isinstance(doc, dict) else None
    if not isinstance(raw_hosts, dict):
        logger.warning(
            "cited_host_registry has no 'hosts' object — all hosts "
            "will be classified as 'unclassified'."
        )
        _REGISTRY_CACHE = {}
        return _REGISTRY_CACHE

    # Normalize keys to lowercase + stripped for case-insensitive lookup.
    _REGISTRY_CACHE = {
        (k or "").strip().lower(): v
        for k, v in raw_hosts.items()
        if isinstance(v, dict) and (k or "").strip()
    }
    return _REGISTRY_CACHE


def _unclassified(host: Optional[str]) -> Dict[str, Any]:
    return {
        "host": (host or "").strip().lower() or None,
        "type": "unclassified",
        "subtype": None,
        "categories": [],
        "coverage_note": None,
        "outreach_hint": None,
        "applies_to_merchant_category": None,
    }


def classify_host(
    host: Optional[str],
    merchant_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Look up classification metadata for a single cited host.

    Returns a dict with the classification fields always populated
    (`type` is at least 'unclassified'). Safe for unknown hosts.

    `merchant_category` (e.g. 'sleepwear', 'beauty', 'fashion') is
    used to set `applies_to_merchant_category`: True when the host's
    `categories` list includes the merchant's category, False when it
    doesn't, None when either side is missing. The action ladder
    (PR-G) will deprioritize hosts where this is False.
    """
    if not host:
        return _unclassified(host)

    h = host.strip().lower()
    registry = _load_registry()
    entry = registry.get(h)
    if not entry:
        return _unclassified(h)

    categories = list(entry.get("categories") or [])
    applies: Optional[bool]
    if merchant_category and categories:
        mc_lower = merchant_category.strip().lower()
        applies = any(c.strip().lower() == mc_lower for c in categories)
    else:
        applies = None

    return {
        "host": h,
        "type": entry.get("type") or "unclassified",
        "subtype": entry.get("subtype"),
        "categories": categories,
        "coverage_note": entry.get("coverage_note"),
        "outreach_hint": entry.get("outreach_hint"),
        "applies_to_merchant_category": applies,
    }


def classify_cited_hosts(
    cited_hosts: List[Dict[str, Any]],
    merchant_category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Project a list of `{host, times_cited}` entries (the engine's
    `category_retailer_hosts` shape) into the per-entry annotated
    shape consumed by `merchant_view.receipts.cited_hosts_detailed`.

    Preserves `times_cited` from upstream; everything else is added
    by `classify_host`. Order is preserved (caller passes in already-
    ranked-by-frequency)."""
    out: List[Dict[str, Any]] = []
    for h in cited_hosts or []:
        host = h.get("host") if isinstance(h, dict) else None
        if not host:
            continue
        annotated = classify_host(host, merchant_category=merchant_category)
        annotated["times_cited"] = (h or {}).get("times_cited") or 0
        out.append(annotated)
    return out


def reset_registry_cache() -> None:
    """Test hook — drop the in-memory cache so the next lookup
    re-reads from disk. Used by tests that monkeypatch `_REGISTRY_PATH`."""
    global _REGISTRY_CACHE
    _REGISTRY_CACHE = None
