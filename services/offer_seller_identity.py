"""Fix Plan C — offer seller-identity derivation (backend mirror of
PIVOTA-Agent/src/services/offerSellerIdentity.js). Single deterministic rule so
the sync write path (Node) and the backend write paths (dual-write, crawl
onboard) agree on WHO IS SELLING an offer.

`catalog_offers.offer_type` must reflect the actual seller (brand direct vs a
third-party retailer), NOT the ingest lane. Precedence:

  0. offer domain is a KNOWN retailer host          -> retailer      (preempts, even if
                                                        identity data wrongly names it official)
  1. offer domain matches the product official domain -> brand_direct (first-party)
  2. normalized brand token is inside the offer domain -> brand_direct (first-party)
  3. no positive evidence                              -> None ("unknown"); DO NOT guess

Pure functions (no I/O). Retailer list is env-extendable via KNOWN_RETAILER_DOMAINS.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Optional
from urllib.parse import urlparse

OFFER_TYPE_BRAND_DIRECT = "brand_direct"
OFFER_TYPE_RETAILER = "retailer"

# Kept in sync with the JS module + mcp-server DEFAULT_RESELLER_HOSTS.
DEFAULT_KNOWN_RETAILER_DOMAINS = (
    "ulta.com", "target.com", "bestbuy.com",
    "amazon.com", "amazon.co.uk", "amazon.co.jp", "amazon.de", "amazon.ca", "amzn.to", "amzn.com",
    "sephora.com", "walmart.com",
    "oliveyoung.com", "global.oliveyoung.com", "oliveyoung.co.kr",
    "nordstrom.com", "macys.com", "dermstore.com", "lookfantastic.com",
    "cultbeauty.com", "cultbeauty.co.uk", "yesstyle.com", "stylevana.com",
    "iherb.com", "ebay.com", "kohls.com", "jcpenney.com", "beautylish.com",
    "revolve.com", "asos.com", "boots.com", "feelunique.com", "skinstore.com",
    # Dept-store / marketplace beauty retailers (added Fix Plan C read-review):
    "selfridges.com", "harrods.com", "spacenk.com",
    "coupang.com", "gmarket.co.kr",
)

_MULTI_LEVEL_TLDS = frozenset({
    "co.uk", "com.au", "co.jp", "co.kr", "co.za", "com.br", "co.nz", "co.in",
    "com.tr", "com.mx", "co.il", "com.sg", "com.hk", "com.tw", "co.th", "com.my",
})


def known_retailer_domains(extra_csv: Optional[str] = None) -> tuple:
    csv = extra_csv if extra_csv is not None else os.getenv("KNOWN_RETAILER_DOMAINS", "")
    extra = tuple(
        h.strip().lower().removeprefix("www.")
        for h in str(csv or "").split(",")
        if h.strip()
    )
    return DEFAULT_KNOWN_RETAILER_DOMAINS + extra


def normalize_host(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    host = str(value).strip().lower()
    if not host:
        return None
    if "://" in host:
        try:
            host = urlparse(host).hostname or ""
        except ValueError:
            return None
    else:
        host = re.split(r"[/?#:]", host, maxsplit=1)[0]
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def host_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        parsed = urlparse(str(url).strip())
        if parsed.hostname:
            return normalize_host(parsed.hostname)
    except ValueError:
        pass
    return normalize_host(url)


def registrable_base(host: Optional[str]) -> Optional[str]:
    h = normalize_host(host)
    if not h:
        return None
    parts = [p for p in h.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts) or None
    last_two = ".".join(parts[-2:])
    if last_two in _MULTI_LEVEL_TLDS:
        return ".".join(parts[-3:])
    return last_two


def domain_name_label(host: Optional[str]) -> Optional[str]:
    base = registrable_base(host)
    if not base:
        return None
    return base.split(".")[0] or None


def domains_match(a: Optional[str], b: Optional[str]) -> bool:
    x = normalize_host(a)
    y = normalize_host(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if x.endswith("." + y) or y.endswith("." + x):
        return True
    bx = registrable_base(x)
    by = registrable_base(y)
    return bool(bx) and bx == by


def normalize_brand(brand: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", str(brand or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def is_known_retailer(host: Optional[str], extra_csv: Optional[str] = None) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    return any(h == base or h.endswith("." + base) for base in known_retailer_domains(extra_csv))


def brand_owns_domain(brand: Optional[str], host: Optional[str]) -> bool:
    """True only when the domain's SLD *is* the brand (collapsed), i.e. exact
    label equality — NOT mere substring containment.

    Unanchored substring matching produced retailer->brand_direct false positives:
    'elf' is inside 'selfridges', 'mac' inside 'pharmacy', and the reverse rule
    made brand 'beautyofjoseon' match the label 'beauty' (beauty.com). Every
    genuine D2C case the suite pins is exact (cosrx.com, tomfordbeauty.com,
    wholesale.publicgoods.com -> 'publicgoods'), so exact equality keeps all real
    matches while erring toward 'unknown' (never a false brand_direct) for affixed
    hosts like shopcosrx.com — the honest, safe direction."""
    b = normalize_brand(brand)
    label = domain_name_label(host)
    if not b or not label:
        return False
    label_norm = re.sub(r"[^a-z0-9]+", "", label)
    if not label_norm:
        return False
    return b == label_norm


def derive_offer_seller_identity(
    *,
    domain: Optional[str] = None,
    canonical_url: Optional[str] = None,
    official_domain: Optional[str] = None,
    brand: Optional[str] = None,
    retailer_csv: Optional[str] = None,
) -> dict:
    """Return {offer_type, is_first_party, rule, evidence_domain}. offer_type is
    None ("unknown") when there is no positive seller evidence."""
    evidence = normalize_host(domain) or host_from_url(canonical_url)
    official = normalize_host(official_domain)

    if not evidence:
        return {"offer_type": None, "is_first_party": False, "rule": "unknown_no_domain", "evidence_domain": None}
    # 0. Known retailer host preempts every brand signal (guards contaminated
    #    identity data where official_domain was wrongly set to a retailer host).
    if is_known_retailer(evidence, retailer_csv):
        return {"offer_type": OFFER_TYPE_RETAILER, "is_first_party": False,
                "rule": "known_retailer_domain", "evidence_domain": evidence}
    # 1. official/brand domain match
    if official and domains_match(evidence, official):
        return {"offer_type": OFFER_TYPE_BRAND_DIRECT, "is_first_party": True,
                "rule": "official_domain_match", "evidence_domain": evidence}
    # 2. brand token contained in the offer domain
    if brand and brand_owns_domain(brand, evidence):
        return {"offer_type": OFFER_TYPE_BRAND_DIRECT, "is_first_party": True,
                "rule": "brand_in_domain", "evidence_domain": evidence}
    # 3. unknown — do not guess
    return {"offer_type": None, "is_first_party": False, "rule": "unknown_no_evidence", "evidence_domain": evidence}
