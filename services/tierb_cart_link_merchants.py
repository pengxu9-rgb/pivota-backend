"""The Tier B cart-link merchant list: load it, validate it, and normalise its keys.

The list lives in `config/tierb_cart_link_merchants.json`: the 40 Shopify merchants measured on
2026-09-18 (reports/tierb_cart_permalink_2026_09_18/population.json, with `variant` renamed to
`variant_id`). It is a JSON list of

    {"domain": "judydoll.com", "market": "US", "variant_id": "50041364447509",
     "product_handle": "single-eyeshadow"}

`variant_id` and `product_handle` are optional HINTS. The preflight confirms the variant against
the live storefront and reports VARIANT_GONE / VARIANT_UNAVAILABLE rather than substituting
another. The handle lets it read ONE `/products/<handle>.js` instead of scanning the whole
catalog, which a store with more than 5,000 products never finishes (`catalog_scan_cap`); a
handle that 404s falls back to the scan. A row with neither is a merchant-level probe (the
preflight picks a representative variant). `scripts/ops/refresh_tierb_seed_hints.py` refreshes
the handles (read-only).

THE FILE IS HELD TO ITS CANONICAL FORM, NOT NORMALISED INTO IT. A row whose domain is not
already `normalize_domain(domain)` (`www.`, upper case, a scheme, a path, a port) is refused
rather than silently rewritten, because the domain is the key the eligibility table is read by:
a list that says `www.Judydoll.com` and a reader that asks for `judydoll.com` must not depend on
two normalisers agreeing. Unknown keys are refused for the same reason: a typo such as
`varient_id` would otherwise drop the hint without a word.

This list is Pivota's own. The preflight creates an abandoned checkout on every store it runs
against, so it must never run over a domain someone else supplied (see
services/shopify_cart_link_preflight.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

DEFAULT_MERCHANTS_PATH = Path(__file__).resolve().parents[1] / "config" / "tierb_cart_link_merchants.json"

_ALLOWED_KEYS = frozenset({"domain", "market", "variant_id", "product_handle"})
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_TLD = re.compile(r"[a-z]{2,63}")
_MARKET = re.compile(r"[A-Z]{2}")
_VARIANT_ID = re.compile(r"[0-9]{1,20}")
# The preflight's own shape for a handle, minus control characters and `%`: a handle is stored
# DECODED, because the preflight percent-encodes it and an encoded one would be encoded twice.
_HANDLE = re.compile(r"[^/?#%\s\x00-\x1f\x7f]{1,255}")


class MerchantListError(ValueError):
    """The merchant list is malformed. The job refuses to run on it rather than skip rows."""


@dataclass(frozen=True)
class Merchant:
    domain: str
    market: str
    variant_id: Optional[str] = None
    product_handle: Optional[str] = None


def normalize_domain(value: Any) -> str:
    """The eligibility table's key for a shop: lower case, no trailing dot, one leading `www.`
    removed. Raises ValueError for anything that is not a bare DNS host name — a scheme, a path,
    a port, userinfo, an IP literal, a single label — because a key that cannot be a store is a
    bug at the caller, and guessing at it would write a row nobody can read back."""
    if not isinstance(value, str):
        raise ValueError("domain must be a string")
    host = value.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or len(host) > 253:
        raise ValueError("domain is empty or too long")
    labels = host.split(".")
    if len(labels) < 2:
        raise ValueError(f"domain {host!r} is not a dotted host name")
    if not all(_LABEL.fullmatch(label) for label in labels) or not _TLD.fullmatch(labels[-1]):
        raise ValueError(f"domain {host!r} is not a bare host name")
    return host


def canonical_merchant_domain(value: Any) -> str:
    """The MERCHANT key the Reap rail matches under: `normalize_domain`, and a ValueError for a
    trailing dot as well.

    ONE FUNCTION, THREE READERS: `routes/agent_commerce_reap` (the purchase request),
    `jobs/merchant_purchasability_sweep` (the population) and `services/reap_agentic_purchase`
    (the attribution merchant key). It is `normalize_domain` rather than a second rule, stricter
    in one respect only. `normalize_domain` strips trailing dots because it keys rows this job
    wrote itself; a request or an operator row spelled `brand.com.` (or `brand.com..`) is not a
    name anybody meant to type, and folding it silently is exactly the guessing the docstring
    above refuses. Every stored column the rail matches is folded in SQL WITHOUT stripping dots,
    so admitting one here would name a merchant the SQL can never find.
    """
    if isinstance(value, str) and value.strip().endswith("."):
        raise ValueError("domain ends in a dot")
    return normalize_domain(value)


def normalize_market(value: Any) -> str:
    """ISO-2 shape, upper case. Raises ValueError otherwise."""
    if not isinstance(value, str) or not _MARKET.fullmatch(value.strip().upper()):
        raise ValueError(f"market {value!r} is not an ISO-2 code")
    return value.strip().upper()


def parse_merchants(raw: Any) -> List[Merchant]:
    """Validate a decoded merchant list. Refuses the WHOLE list on the first bad row: a job that
    skipped bad rows would report a smaller merchant set as a clean run."""
    if not isinstance(raw, list) or not raw:
        raise MerchantListError("the merchant list must be a non-empty JSON list")
    out: List[Merchant] = []
    seen: set[Tuple[str, str]] = set()
    for index, row in enumerate(raw):
        where = f"row {index}"
        if not isinstance(row, dict):
            raise MerchantListError(f"{where}: not an object")
        unknown = set(row) - _ALLOWED_KEYS
        if unknown:
            raise MerchantListError(f"{where}: unknown keys {sorted(unknown)}")
        domain = row.get("domain")
        try:
            canonical = normalize_domain(domain)
        except ValueError as exc:
            raise MerchantListError(f"{where}: {exc}") from None
        if domain != canonical:
            raise MerchantListError(f"{where}: domain {domain!r} is not canonical (expected {canonical!r})")
        market = row.get("market")
        if not isinstance(market, str) or not _MARKET.fullmatch(market):
            raise MerchantListError(f"{where}: market {market!r} is not an upper-case ISO-2 code")
        variant = row.get("variant_id")
        if variant is not None and (not isinstance(variant, str) or not _VARIANT_ID.fullmatch(variant)):
            raise MerchantListError(f"{where}: variant_id {variant!r} is not a numeric string")
        handle = row.get("product_handle")
        if handle is not None and (not isinstance(handle, str) or not _HANDLE.fullmatch(handle)):
            raise MerchantListError(f"{where}: product_handle {handle!r} is not a bare, decoded handle")
        if handle is not None and variant is None:
            raise MerchantListError(f"{where}: product_handle without variant_id (a handle hints a variant)")
        key = (canonical, market)
        if key in seen:
            raise MerchantListError(f"{where}: duplicate merchant {canonical} {market}")
        seen.add(key)
        out.append(Merchant(domain=canonical, market=market, variant_id=variant, product_handle=handle))
    return out


def load_merchants(path: Union[str, Path, None] = None) -> List[Merchant]:
    target = Path(path) if path is not None else DEFAULT_MERCHANTS_PATH
    with open(target, encoding="utf-8") as fh:
        try:
            raw = json.load(fh)
        except ValueError as exc:
            raise MerchantListError(f"{target}: not valid JSON ({exc})") from None
    return parse_merchants(raw)


def select_merchants(merchants: Sequence[Merchant], only: Optional[Sequence[str]]) -> List[Merchant]:
    """Restrict to the named domains (normalised). Naming a domain that is not on the list is an
    error, not an empty run: the list is the only set of stores this job may touch."""
    if not only:
        return list(merchants)
    wanted = {normalize_domain(d) for d in only}
    missing = wanted - {m.domain for m in merchants}
    if missing:
        raise MerchantListError(f"not on the merchant list: {sorted(missing)}")
    return [m for m in merchants if m.domain in wanted]
