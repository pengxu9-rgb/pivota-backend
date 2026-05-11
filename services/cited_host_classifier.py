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
    h = (host or "").strip().lower() or None
    out: Dict[str, Any] = {
        "host": h,
        "type": "unclassified",
        "subtype": None,
        "categories": [],
        "coverage_note": None,
        "outreach_hint": None,
        "applies_to_merchant_category": None,
    }
    # PR-7c: even for unclassified hosts, the enrichment fallback
    # maps may still know about them (e.g. a host in the
    # _DEFAULT_TIER_BY_HOST map but not in the registry JSON yet).
    # Renderer expects these fields to always be present (nullable).
    if h:
        out["tier"] = _default_tier_for_host(h, "unclassified")
        out["editorial_cadence"] = _default_cadence_for_host(h)
        out["ai_grounding_weight"] = _default_grounding_weight_for_host(h)
        out["expected_outreach_cycle_weeks"] = (
            _default_outreach_cycle_weeks(out["editorial_cadence"], None)
        )
    else:
        out["tier"] = None
        out["editorial_cadence"] = None
        out["ai_grounding_weight"] = None
        out["expected_outreach_cycle_weeks"] = None
    return out


# PR-7c: tier + cadence + ai_grounding_weight defaults for hosts that
# don't have explicit values in the registry JSON. Backfilled from the
# top-cited hosts across our audit history. Future PR can populate
# the registry JSON with explicit values per host (this map is the
# fallback for hosts where explicit values aren't yet set).
_DEFAULT_TIER_BY_HOST: Dict[str, int] = {
    # Tier 1 — top-tier review sites + magazines with broad reach
    "nytimes.com": 1,            # Wirecutter
    "nymag.com": 1,              # The Strategist
    "forbes.com": 1,              # Forbes Vetted
    "wsj.com": 1,
    "bloomberg.com": 1,
    "vogue.com": 1,
    "harpersbazaar.com": 1,
    "elle.com": 1,
    "cosmopolitan.com": 1,
    "allure.com": 1,
    "womenshealthmag.com": 1,
    "mensjournal.com": 1,
    "menshealth.com": 1,
    "today.com": 1,
    "people.com": 1,
    "self.com": 1,
    "shape.com": 1,
    "byrdie.com": 1,
    "refinery29.com": 1,
    "esquire.com": 1,
    "vanityfair.com": 1,
    "wired.com": 1,
    "engadget.com": 1,
    "theverge.com": 1,
    "cnet.com": 1,
    "techcrunch.com": 1,
    # Tier 2 — niche review sites with disproportionate AI-grounding
    # influence in their vertical
    "trailandkale.com": 2,
    "outsideonline.com": 2,
    "runnersworld.com": 2,
    "bicycling.com": 2,
    "businessinsider.com": 2,
    "popsci.com": 2,
    "yahoo.com": 2,
    "msn.com": 2,
    "vice.com": 2,
    "thecut.com": 2,
    "fashionista.com": 2,
    "whowhatwear.com": 2,
    "intothegloss.com": 2,
    "cupofjo.com": 2,
    "purewow.com": 2,
    "thespruce.com": 2,
    "apartmenttherapy.com": 2,
    "bonappetit.com": 2,
    "epicurious.com": 2,
    # Tier 3 — niche / personal-brand bloggers with regional or
    # category-specific influence
    "mindbodygreen.com": 3,
    "wellandgood.com": 3,
    "bridestory.com": 3,
}


_DEFAULT_CADENCE_BY_HOST: Dict[str, str] = {
    # Quarterly refresh cycle — Forbes Vetted, Wirecutter, etc.
    "forbes.com": "quarterly",
    "nytimes.com": "quarterly",        # Wirecutter
    "nymag.com": "quarterly",
    "today.com": "quarterly",
    "wired.com": "quarterly",
    "techcrunch.com": "continuous",     # rolling reviews
    # Annual / biannual roundup publishers
    "womenshealthmag.com": "annual",
    "menshealth.com": "annual",
    "self.com": "annual",
    "shape.com": "annual",
    "vogue.com": "biannual",
    "harpersbazaar.com": "biannual",
    "elle.com": "biannual",
    "cosmopolitan.com": "biannual",
    # Continuous editorial — daily-ish posts
    "businessinsider.com": "continuous",
    "yahoo.com": "continuous",
    "msn.com": "continuous",
}


_DEFAULT_GROUNDING_WEIGHT_BY_HOST: Dict[str, str] = {
    # High = consistently appears in Gemini grounding for category
    # queries across multiple verticals
    "forbes.com": "high",
    "nytimes.com": "high",
    "nymag.com": "high",
    "wired.com": "high",
    "theverge.com": "high",
    "wirecutter.com": "high",
    # Medium = appears in vertical-specific grounding
    "trailandkale.com": "medium",
    "outsideonline.com": "medium",
    "womenshealthmag.com": "medium",
    "menshealth.com": "medium",
    "today.com": "medium",
    "byrdie.com": "medium",
    "allure.com": "medium",
    # Low = rare appearance; defensive default for unknown hosts
}


def _default_tier_for_host(host: str, host_type: str) -> Optional[int]:
    """Tier from explicit registry entry, then from default map,
    then heuristic from host_type. Returns None when no signal."""
    explicit = _DEFAULT_TIER_BY_HOST.get(host)
    if explicit:
        return explicit
    # Heuristic from type: editorial review_site → tier 2 default;
    # retailer/marketplace → no tier (tier concept is editorial-only)
    if host_type == "editorial":
        return 3  # Conservative default for unknown editorial sources
    return None


def _default_cadence_for_host(host: str) -> Optional[str]:
    return _DEFAULT_CADENCE_BY_HOST.get(host)


def _default_grounding_weight_for_host(host: str) -> Optional[str]:
    return _DEFAULT_GROUNDING_WEIGHT_BY_HOST.get(host)


def _default_outreach_cycle_weeks(
    cadence: Optional[str], host_type: Optional[str],
) -> Optional[List[int]]:
    """Default outreach cycle range based on cadence + host type.
    Returns [low, high] weeks the merchant should expect from pitch
    to inclusion-in-grounded-LLM-answers."""
    if cadence == "continuous":
        return [2, 4]      # rolling editorial
    if cadence == "quarterly":
        return [4, 8]
    if cadence == "biannual":
        return [8, 16]
    if cadence == "annual":
        return [12, 24]
    if host_type == "retailer":
        return [12, 26]    # wholesale onboarding cycles
    return None


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

    host_type = entry.get("type") or "unclassified"
    out: Dict[str, Any] = {
        "host": h,
        "type": host_type,
        "subtype": entry.get("subtype"),
        "categories": categories,
        "coverage_note": entry.get("coverage_note"),
        "outreach_hint": entry.get("outreach_hint"),
        "applies_to_merchant_category": applies,
    }
    # Phase A: pass-through pitch_recipient (when present) so the
    # playbook engine can build pitch_draft mailto: links. Without
    # this, classify_host strips the field and pitch_draft is always
    # None — Phase A unit tests worked around this with a test helper
    # that manually re-attached pitch_recipient, hiding the production
    # bug end-to-end.
    pitch_recipient = entry.get("pitch_recipient")
    if pitch_recipient is not None:
        out["pitch_recipient"] = pitch_recipient

    # PR-7c: tier + cadence + grounding weight + outreach cycle.
    # Prefer explicit values from the registry JSON; fall back to
    # default maps for known top-cited hosts; fall back to None for
    # unknowns. Renderer can show "Tier 1 publisher · quarterly
    # refresh · high AI grounding influence" when populated.
    out["tier"] = entry.get("tier") or _default_tier_for_host(h, host_type)
    out["editorial_cadence"] = (
        entry.get("editorial_cadence")
        or _default_cadence_for_host(h)
    )
    out["ai_grounding_weight"] = (
        entry.get("ai_grounding_weight")
        or _default_grounding_weight_for_host(h)
    )
    out["expected_outreach_cycle_weeks"] = (
        entry.get("expected_outreach_cycle_weeks")
        or _default_outreach_cycle_weeks(out["editorial_cadence"], host_type)
    )
    return out


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
