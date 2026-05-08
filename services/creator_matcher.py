"""
Phase E scaffolding — creator marketplace matcher.

The audit's existing recommendation "Sponsor a YouTube creator" is the
canonical "正确的废话" example: directionally correct, totally
non-actionable. Phase E turns it into "we matched 5 candidate creators
in your category who recently covered competitor X, with their stats
and a pre-filled brief — pick one, hit send".

Status: SCAFFOLDING ONLY. The matcher returns ranked candidates from
`data/creator_database.json` — but that file is intentionally empty
(seed has only one obviously-fake placeholder entry, excluded from
production matches). Real BD-curated content lands as a separate
ongoing workstream. Engineering ships the matcher + action surface
so BD's content updates flow through immediately when they land.

Honesty rule: when the database is empty (or has no matches for the
merchant's category/competitors), no action is emitted. Better to
surface zero results than fabricate creator profiles.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DATABASE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "creator_database.json"
)
_DATABASE_CACHE: Optional[List[Dict[str, Any]]] = None


def reset_database_cache() -> None:
    """Test hook — drop the in-memory cache."""
    global _DATABASE_CACHE
    _DATABASE_CACHE = None


def _load_database() -> List[Dict[str, Any]]:
    """Lazy-load the creator database. Returns an empty list on any
    parse / read failure so the audit pipeline never crashes — it
    just emits no creator action."""
    global _DATABASE_CACHE
    if _DATABASE_CACHE is not None:
        return _DATABASE_CACHE
    try:
        with open(_DATABASE_PATH, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "creator_database not loaded (%s); creator-partnership "
            "actions will not be emitted until the file is fixed.",
            exc,
        )
        _DATABASE_CACHE = []
        return _DATABASE_CACHE

    creators_raw = doc.get("creators") if isinstance(doc, dict) else None
    if not isinstance(creators_raw, list):
        _DATABASE_CACHE = []
        return _DATABASE_CACHE

    # Filter out placeholder rows (creator_id starting with "example_")
    # — these are schema demos, not real candidates. Production matcher
    # only sees BD-verified entries.
    cleaned: List[Dict[str, Any]] = []
    for c in creators_raw:
        if not isinstance(c, dict):
            continue
        cid = (c.get("creator_id") or "").strip().lower()
        if not cid or cid.startswith("example_"):
            continue
        cleaned.append(c)
    _DATABASE_CACHE = cleaned
    return cleaned


def match_creators(
    *,
    merchant_category: Optional[str],
    competitor_brands: Optional[List[str]] = None,
    audience_size_preference: Optional[str] = None,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """Rank candidate creators for this merchant's audit.

    Ranking signals:
      - category_match (1.0): creator.category_tags includes merchant_category
      - competitor_overlap (0.5 per match): creator.recent_coverage intersects competitor_brands
      - audience_fit (0.3): creator.audience_size_band matches preference (default: small/medium)

    Hard rules:
      - merchant_category is required — without it we can't rank.
        Returns []
      - Creators without category_match are excluded (we only surface
        creators in the merchant's category, not cross-vertical matches).
      - Returns top_n creators sorted by score desc, with score > 0.

    Returned shape (subset of database row, with score added):
      {creator_id, display_name, platform, platform_url,
       audience_size_band, recent_coverage, contact_method,
       contact_url, sample_brief_template, score}
    """
    if not merchant_category or not str(merchant_category).strip():
        return []
    mc_lower = str(merchant_category).strip().lower()
    competitors_lower = {
        (b or "").strip().lower()
        for b in (competitor_brands or [])
        if isinstance(b, str) and b.strip()
    }
    pref = (audience_size_preference or "").strip().lower()

    creators = _load_database()
    scored: List[Dict[str, Any]] = []
    for c in creators:
        cats = [
            (t or "").strip().lower()
            for t in (c.get("category_tags") or [])
            if isinstance(t, str)
        ]
        if mc_lower not in cats:
            continue
        score = 1.0  # category match required → baseline 1.0

        coverage = [
            (b or "").strip().lower()
            for b in (c.get("recent_coverage") or [])
            if isinstance(b, str)
        ]
        overlap_count = sum(1 for b in coverage if b in competitors_lower)
        score += 0.5 * overlap_count

        band = (c.get("audience_size_band") or "").strip().lower()
        if pref and band == pref:
            score += 0.3
        elif not pref and band in ("small", "medium"):
            # Default preference: small/medium fit most D2C merchants
            # (~$0-5M revenue band)
            score += 0.3

        scored.append({
            "creator_id": c.get("creator_id"),
            "display_name": c.get("display_name"),
            "platform": c.get("platform"),
            "platform_url": c.get("platform_url"),
            "audience_size_band": band or None,
            "recent_coverage": list(c.get("recent_coverage") or []),
            "contact_method": c.get("contact_method"),
            "contact_url": c.get("contact_url"),
            "sample_brief_template": c.get("sample_brief_template"),
            "_overlap_count": overlap_count,
            "score": round(score, 2),
        })

    # Sort: score desc, then overlap_count desc as tiebreaker
    scored.sort(key=lambda x: (x["score"], x["_overlap_count"]), reverse=True)
    # Drop internal sort key before returning
    for s in scored:
        s.pop("_overlap_count", None)
    return scored[:top_n]


def build_creator_partnership_action(
    *,
    matches: List[Dict[str, Any]],
    merchant_category: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Build the playbook action surfaced when the matcher returned
    candidates. Returns None when matches is empty — better to omit
    than to emit a "no candidates yet" placeholder.
    """
    if not matches:
        return None
    cat = (merchant_category or "your category").strip() or "your category"
    n = len(matches)
    top_brand_overlap = matches[0].get("recent_coverage") or []
    overlap_phrase = ""
    if top_brand_overlap:
        names = [b for b in top_brand_overlap if isinstance(b, str)][:2]
        if names:
            overlap_phrase = (
                f"; the top match recently covered "
                f"{names[0] if len(names) == 1 else f'{names[0]} and {names[1]}'}"
            )
    body = (
        f"We matched {n} creator candidate{'s' if n != 1 else ''} in {cat} "
        f"based on category overlap and recent coverage of competitor "
        f"brands{overlap_phrase}. Each carries audience-size, recent "
        f"coverage, and a sample outreach brief — review and pick one to "
        f"start."
    )
    return {
        "severity": "medium",
        "lever": "creator_partnership",
        "title": f"Reach out to {n} matched creator{'s' if n != 1 else ''}",
        "body": body,
        "concrete_next_step": (
            f"Review the {n} matched creators below; pick one whose audience "
            f"+ recent coverage best fits your product, and send the "
            f"pre-filled outreach brief."
        ),
        "evidence": {
            "matched_creators": matches,
            "category": cat,
        },
    }
