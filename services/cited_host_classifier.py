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

  - `type`         : editorial | retailer | marketplace | video | brand | cdn | unclassified
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
import re
from typing import Any, Dict, List, Optional

from utils.logger import logger

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "cited_host_registry.json"
_REGISTRY_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_REGISTRY_ALIAS_CACHE: Optional[Dict[str, str]] = None
_CDN_HOST_SUFFIXES = (
    "ctfassets.net",
    "cloudfront.net",
    "cloudinary.com",
    "imgix.net",
    "akamaihd.net",
)


def _load_registry() -> Dict[str, Dict[str, Any]]:
    """Lazy-load the registry on first lookup. Returns an empty dict
    on read/parse failure so audit pipelines never crash because BD
    happened to ship malformed JSON — they just degrade to all hosts
    being unclassified."""
    global _REGISTRY_CACHE, _REGISTRY_ALIAS_CACHE
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
    _REGISTRY_ALIAS_CACHE = None
    return _REGISTRY_CACHE


def _normalize_host_key(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.startswith(("http://", "https://")):
        raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def _normalize_alias(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _alias_index(registry: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    global _REGISTRY_ALIAS_CACHE
    if _REGISTRY_ALIAS_CACHE is not None:
        return _REGISTRY_ALIAS_CACHE
    aliases: Dict[str, str] = {}
    for host, entry in registry.items():
        for alias in entry.get("aliases") or []:
            normalized = _normalize_alias(alias)
            if len(normalized) >= 3 and normalized not in aliases:
                aliases[normalized] = host
    _REGISTRY_ALIAS_CACHE = aliases
    return _REGISTRY_ALIAS_CACHE


# Aliases that are also common English words (or word phrases) — resolve them
# ONLY on an exact match, never as a substring, so a coincidental title like
# "best collagen for your target audience" can't be mis-attributed to target.com,
# "the independent best serums" to the-independent.com, or "living between town
# and country" to townandcountrymag.com. An exact title of "Target" / "The
# Independent" / "Town and Country" still resolves via the exact-match path
# above; this guard only suppresses the looser substring match. Real citations
# titled "Amazon.com: ..." still resolve via domain-token extraction upstream,
# and the distinctive "&"/"+" magazine-title forms ("Town & Country",
# "Travel + Leisure") normalize to "town country" / "travel leisure", which
# stay substring-eligible.
_COMMON_WORD_ALIASES = {
    "target", "amazon", "the independent", "town and country",
    "travel and leisure",
}


def _resolve_alias(value: str, registry: Dict[str, Dict[str, Any]]) -> Optional[str]:
    normalized = _normalize_alias(value)
    if not normalized:
        return None
    aliases = _alias_index(registry)
    exact = aliases.get(normalized)
    if exact:
        return exact
    padded = f" {normalized} "
    matches = [
        (alias, host)
        for alias, host in aliases.items()
        if alias not in _COMMON_WORD_ALIASES and f" {alias} " in padded
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: (-len(item[0]), item[1]))
    return matches[0][1]


def _unclassified(host: Optional[str]) -> Dict[str, Any]:
    h = (host or "").strip().lower() or None
    out: Dict[str, Any] = {
        "host": h,
        "type": "unclassified",
        "confidence": "fallback",
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


def _default_known_host(host: str) -> Optional[Dict[str, Any]]:
    if not (
        host in _DEFAULT_TIER_BY_HOST
        or host in _DEFAULT_CADENCE_BY_HOST
        or host in _DEFAULT_GROUNDING_WEIGHT_BY_HOST
    ):
        return None
    out = _unclassified(host)
    out["type"] = "editorial"
    out["confidence"] = "default_map"
    out["subtype"] = "review_site"
    out["tier"] = _default_tier_for_host(host, "editorial")
    out["editorial_cadence"] = _default_cadence_for_host(host)
    out["ai_grounding_weight"] = _default_grounding_weight_for_host(host)
    out["expected_outreach_cycle_weeks"] = _default_outreach_cycle_weeks(
        out["editorial_cadence"],
        "editorial",
    )
    return out


def _matches_cdn_suffix(host: str) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _CDN_HOST_SUFFIXES)


def _cdn_fallback(host: str) -> Dict[str, Any]:
    out = _unclassified(host)
    out["type"] = "cdn"
    out["confidence"] = "heuristic"
    out["subtype"] = "asset_cdn"
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


# Phase 1c: register each VerticalProfile's authority hosts as review-site
# grounding sources, sourced from the profile registry so there's one home for
# "what an audio/electronics authority host is" (Rtings, SoundGuys, head-fi,
# What Hi-Fi...). Additive via setdefault: a host with an explicit higher weight
# (e.g. wirecutter.com = "high") is never downgraded. Beauty-safe — beauty audits
# don't cite these, so nothing changes until an electronics audit encounters one.
def _register_profile_authority_hosts() -> None:
    from services.vertical_profiles import VERTICAL_PROFILES

    for profile in VERTICAL_PROFILES.values():
        for host in getattr(profile, "authority_hosts", ()) or ():
            key = _normalize_host_key(host)
            if key:
                _DEFAULT_GROUNDING_WEIGHT_BY_HOST.setdefault(key, "medium")


_register_profile_authority_hosts()


# Phase 1c (retailer half): the profiles also know which RETAILERS matter per
# vertical (`retailer_tokens`: bestbuy, newegg, oliveyoung, ...), but until now
# that knowledge only fed the competitor-brand filter — never this classifier.
# Result: a cited bestbuy.com came back type="unclassified", tier=null, and the
# report layer could then mis-flag the merchant's own retail channel as a
# competitor storefront. Registering the tokens here gives every consumer of
# classify_host (source_roles, authority_map, playbooks) the retailer verdict.
# Tokens are brand labels, not hosts, so matching is by dot-label (bestbuy.com,
# shop.target.com, target.com.au all match "target"). Deliberately NOT gated on
# the BD registry loading — vertical knowledge is independent of that file.
_PROFILE_RETAILER_HOST_TOKENS: set = set()


def _register_profile_retailer_tokens() -> None:
    from services.vertical_profiles import VERTICAL_PROFILES

    for profile in VERTICAL_PROFILES.values():
        for token in getattr(profile, "retailer_tokens", ()) or ():
            cleaned = str(token or "").strip().lower()
            if len(cleaned) >= 4:
                _PROFILE_RETAILER_HOST_TOKENS.add(cleaned)


_register_profile_retailer_tokens()


def is_profile_retailer_name(name: str) -> bool:
    """True when a brand-ish NAME names a known vertical retailer ("Best Buy",
    "walmart"). Used by the report layer to keep engine-listed retailer names
    out of competitor alias sets — a retailer carrying the merchant's listing
    is a channel, not a competitor."""
    despaced = re.sub(r"[^a-z0-9]+", "", str(name or "").lower())
    return bool(despaced) and despaced in _PROFILE_RETAILER_HOST_TOKENS


def _profile_retailer_fallback(host: str) -> Optional[Dict[str, Any]]:
    labels = [part for part in host.split(".") if part]
    if not any(label in _PROFILE_RETAILER_HOST_TOKENS for label in labels):
        return None
    out = _unclassified(host)
    out["type"] = "retailer"
    out["confidence"] = "profile_token"
    out["subtype"] = "vertical_retailer"
    out["tier"] = None  # tier is an editorial-only concept
    out["expected_outreach_cycle_weeks"] = _default_outreach_cycle_weeks(
        out["editorial_cadence"], "retailer"
    )
    return out


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

    h = _normalize_host_key(host)
    registry = _load_registry()
    entry = registry.get(h)
    if not entry:
        alias_host = _resolve_alias(host, registry)
        if alias_host:
            h = alias_host
            entry = registry.get(h)
    if not entry:
        if _matches_cdn_suffix(h):
            return _cdn_fallback(h)
        known = _default_known_host(h) if registry else None
        if known:
            return known
        retailer = _profile_retailer_fallback(h)
        if retailer:
            return retailer
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
        "confidence": entry.get("confidence") or "registry",
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


# Recommend-vs-list axis (Fix 2). Does a citation from this host *type* mean an
# independent third party recommended the product (an editorial / review /
# creator source vouched for it on its own merits) — or only that the product is
# *listed for sale* there (a retailer / marketplace / brand storefront), which
# proves findability, not endorsement? Keeping these apart stops "your listing
# is indexed" from being reported as "AI recommends you".
_RECOMMENDS_HOST_TYPES = frozenset({"editorial", "video"})
_LISTS_HOST_TYPES = frozenset({"retailer", "marketplace", "brand"})


def recommendation_class(host_type: Optional[str]) -> str:
    """Return ``"recommends"``, ``"lists"`` or ``"unknown"`` for a cited-host
    ``type`` (as produced by :func:`classify_host`). ``recommends`` is the
    endorsement signal (editorial / review / creator); ``lists`` is the
    findability/distribution signal (retailer / marketplace / brand storefront);
    ``unknown`` covers unclassified / cdn / missing types — counted
    conservatively as *not* an endorsement."""
    t = (host_type or "").strip().lower()
    if t in _RECOMMENDS_HOST_TYPES:
        return "recommends"
    if t in _LISTS_HOST_TYPES:
        return "lists"
    return "unknown"


# Merchant-relative citation roles (Fix 2). The recommend-vs-list axis above is a
# property of the host *type* alone; a cited host's role is what that citation
# actually proves *for this merchant*. Two merchant-relative facts the report
# layer supplies refine the type into a role: whether the host is the merchant's
# own property (`first_party`) and whether the cited excerpt is centered on a
# competitor (`is_competitor`). The point of the split is to stop "your own
# listing is indexed" (findability) from ever reading as "an independent party
# recommends you" (endorsement).
ROLE_OWN_DOMAIN = "own_domain"
ROLE_MARKETPLACE_SELF_LISTING = "marketplace_self_listing"
ROLE_INDEPENDENT_RETAILER = "independent_retailer"
ROLE_EDITORIAL_REVIEW = "editorial_review"
ROLE_CREATOR = "creator"
ROLE_FORUM = "forum"
ROLE_COMPETITOR = "competitor"
ROLE_RELATIVE_UNCLASSIFIED = "unclassified"

# findability = the product is *findable* — its own site + its own product listed
# on a marketplace/retailer. endorsement = an independent third party
# *recommended* it (editorial / review / independent retailer / creator /
# community). competitor + unclassified count toward NEITHER merchant signal:
# a rival's storefront is not the merchant's distribution, and an unknown host is
# conservatively not an endorsement.
_FINDABILITY_ROLES = frozenset({ROLE_OWN_DOMAIN, ROLE_MARKETPLACE_SELF_LISTING})
_ENDORSEMENT_ROLES = frozenset(
    {ROLE_INDEPENDENT_RETAILER, ROLE_EDITORIAL_REVIEW, ROLE_CREATOR, ROLE_FORUM}
)


def merchant_relative_role(
    host_type: Optional[str],
    *,
    first_party: bool = False,
    is_competitor: bool = False,
) -> str:
    """Classify a cited host *relative to the merchant* into one of the Fix-2
    roles (``own_domain | marketplace_self_listing | independent_retailer |
    editorial_review | creator | forum | competitor | unclassified``).

    ``host_type`` is the folded authority host type the report layer already
    computes (``editorial`` / ``trade`` / ``creator`` / ``reddit`` / ``retailer``
    / ``unclassified``). ``first_party`` is True when the host is the merchant's
    own property; ``is_competitor`` is True when the citation is centered on a
    different brand's storefront.

    Conservative by design (the no-inflation guardrail): a retailer / marketplace
    host defaults to ``marketplace_self_listing`` (findability) and is never
    silently promoted to endorsement; ``independent_retailer`` is a defined role
    but only assigned when the report layer has a positive recommend-not-list
    signal (when that signal is unavailable the host stays findability rather than
    inflating endorsement). An unknown host is ``unclassified``, not endorsement.
    """
    if first_party:
        return ROLE_OWN_DOMAIN
    if is_competitor:
        return ROLE_COMPETITOR
    t = (host_type or "").strip().lower()
    if t in {"editorial", "trade"}:
        return ROLE_EDITORIAL_REVIEW
    if t in {"creator", "video"}:
        return ROLE_CREATOR
    if t in {"forum", "reddit"}:
        return ROLE_FORUM
    if t in {"retailer", "marketplace", "brand"}:
        return ROLE_MARKETPLACE_SELF_LISTING
    return ROLE_RELATIVE_UNCLASSIFIED


def role_signal(role: Optional[str]) -> str:
    """Map a merchant-relative role to its report signal: ``"findability"``,
    ``"endorsement"`` or ``"neither"`` (competitor / unclassified)."""
    r = (role or "").strip().lower()
    if r in _FINDABILITY_ROLES:
        return "findability"
    if r in _ENDORSEMENT_ROLES:
        return "endorsement"
    return "neither"


def is_findability_role(role: Optional[str]) -> bool:
    """True for roles that prove the product is findable (own site / own listing
    on a marketplace) — NOT an independent endorsement."""
    return (role or "").strip().lower() in _FINDABILITY_ROLES


def is_endorsement_role(role: Optional[str]) -> bool:
    """True for roles that are a genuine independent recommendation (editorial /
    review / independent retailer / creator / community)."""
    return (role or "").strip().lower() in _ENDORSEMENT_ROLES


def reset_registry_cache() -> None:
    """Test hook — drop the in-memory cache so the next lookup
    re-reads from disk. Used by tests that monkeypatch `_REGISTRY_PATH`."""
    global _REGISTRY_CACHE, _REGISTRY_ALIAS_CACHE
    _REGISTRY_CACHE = None
    _REGISTRY_ALIAS_CACHE = None
