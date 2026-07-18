"""Bundle-vs-base price guard for the HITL `mint_and_attach` lane.

WHY. A judge AUTO verdict can certify an SK single == an official record whose
TITLE hides that it is a multi-unit bundle — the bundle marker lives only in the
URL slug / tags. Real 2026-07-18 case: Beauty of Joseon official
`dynasty-cream-100ml-duo-f`, $72, title "Dynasty Cream 3.38 fl.oz.(100ml)". After
the decimal-size key fix (#1465) that official's `retailer_match_key` now lands on
the base "Dynasty Cream" canonical, so approving the proposal would mint a
base-titled canonical from — or attach a base-priced SK single onto — a duo
bundle. Deterministic identity keeps size digits, so the two never share a
content_key; this guard covers the *attach/mint* step, where size/promo
normalization collapses them.

SIGNAL (conservative — both required). An explicit bundle marker in the
official's URL slug or title AND a bundle-multiple price gap between the official
and the cheapest certified SK item. Each signal alone false-positives on real
queue data:
  - marker-alone: anua carries a marketing "☆BUNDLE" collection TAG but is a
    genuine single-product mint (so tags are NOT read here);
  - price-alone: anua official $28.8 box vs a $3.6 single-mask SK item is an 8x
    gap from pack-size difference, not a bundle.
Requiring BOTH fired on exactly the Dynasty duo across the seven mint_and_attach
proposals in the 2026-07-18 review queue.

Pure module — no I/O, safe to import anywhere and unit-test in isolation.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional, Sequence, Tuple

# Bundle tokens as WORD / PATH segments. Deliberately EXCLUDES bare "set"
# (product-line names — "Air-Fit Set") and does NOT read tags. A multi-unit
# count ("2 pack", "duo", "twin", "4ea") is a bundle token; an in-product count
# that IS the retail unit ("30 stick", "capsule 100") is not, and is caught only
# when the price gap independently says bundle. Separator-anchored so "duo"
# matches in `-duo-` / `(duo)` but not inside a word.
_BUNDLE_TOKEN_RE = re.compile(
    r"(?:^|[\s\-_/(])"
    r"(?:duo|bundle|twin|gift[\s\-]?set|\d+\s*pack|\d+[\s\-]?pk|\d+\s*ea|\d+\s*pcs?)"
    r"(?:$|[\s\-_/).])",
    re.IGNORECASE,
)

DEFAULT_BUNDLE_PRICE_RATIO = 1.8


def _positive_prices(offers: Sequence[Mapping[str, Any]]) -> List[float]:
    out: List[float] = []
    for o in offers or []:
        try:
            p = float(o.get("price"))
        except (TypeError, ValueError):
            continue
        if p > 0:
            out.append(p)
    return out


def _item_price(item: Mapping[str, Any]) -> Optional[float]:
    """Cheapest positive price for one SK plan item: top-level 'price' first
    (what the plan carries), else record.offers[].price."""
    try:
        p = float(item.get("price"))
        if p > 0:
            return p
    except (TypeError, ValueError):
        pass
    prices = _positive_prices((item.get("record") or {}).get("offers") or [])
    return min(prices) if prices else None


def bundle_marker(official: Mapping[str, Any]) -> Optional[str]:
    """The matched bundle token from the official's title or offer URL slug, or
    None. Public so the emit lane can report WHAT marker it saw."""
    pdp = official.get("pdp") or {}
    haystacks: List[str] = [str(pdp.get("product_name") or "")]
    for o in official.get("offers") or []:
        haystacks.append(str(o.get("canonical_url") or ""))
        haystacks.append(str(o.get("destination_url") or ""))
    for h in haystacks:
        m = _BUNDLE_TOKEN_RE.search(h)
        if m:
            return m.group(0).strip(" -_/().")
    return None


def bundle_price_guard(
    official: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    ratio_threshold: float = DEFAULT_BUNDLE_PRICE_RATIO,
) -> Tuple[bool, Optional[str]]:
    """Return (is_bundle_mismatch, reason).

    Fires when the official record carries an explicit bundle marker (URL slug /
    title) AND its price is >= ratio_threshold x the cheapest certified SK item
    — a bundle certified equal to a base single. When a marker is present but no
    comparable price exists, fires conservatively (a slug-marked bundle a human
    must confirm is really a base item). Pure; no I/O."""
    marker = bundle_marker(official)
    if not marker:
        return False, None
    off_prices = _positive_prices(official.get("offers") or [])
    if not off_prices:
        return True, f"bundle marker {marker!r} in official, no official price to compare"
    item_prices = [p for p in (_item_price(i) for i in items) if p is not None]
    if not item_prices:
        return True, f"bundle marker {marker!r} in official, no SK item price to compare"
    official_price = min(off_prices)
    base_price = min(item_prices)
    ratio = official_price / base_price if base_price else None
    if ratio is not None and ratio >= ratio_threshold:
        return True, (
            f"bundle marker {marker!r}: official ${official_price:.2f} is "
            f"{ratio:.1f}x cheapest SK item ${base_price:.2f}"
        )
    return False, None
