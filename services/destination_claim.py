"""P0 item 8 (§14) — what the ANSWER SAYS a brand's official store is.

THE GAP THIS FILLS. Everything else in the destination lane reads grounding-chunk
METADATA: which hosts a provider cited, in what order, how often. The single
highest-cost error an engine makes is not in that metadata at all — it is in the
prose:

    "The official website for Judydoll is judydoll.shop"          (Gemini)
    "Joocyee's official website is joocyeebeauty.com"             (ChatGPT)
    "Joocyee's official website for US shoppers is joocyee.co"    (Gemini)

All three on Tier-A "official website" queries. Two of those hosts have no DNS
record at all — the engine invented them. A merchant whose buyers are told their
official store is a domain they do not own is losing the purchase at the last
step, and no host-frequency metric can see it, because the claim is a
RELATIONSHIP asserted in text, not a citation.

WHY IT IS DELIBERATELY CONSERVATIVE. A false positive here tells a merchant that
AI is misdirecting their buyers when it is not — a scary, expensive, wrong claim,
and exactly the kind of overclaim the rest of this workstream exists to remove.
So the extractor requires an explicit relationship word ("official website",
"official site", "official store") bound to the brand, and returns the sentence
it matched so a human can check it. Anything it cannot parse it declines to
report; there is no "probably a claim" tier.

It also reports claims that point at a VERIFIED domain. That is not noise: the
absence of a claim and a correct claim are different facts, and only one of them
is reassuring.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from services.brand_claim_service import normalize_host

# The relationship words that make a sentence a CLAIM rather than a mention.
# "available at", "sold on", "you can buy it at" are deliberately NOT here: they
# describe a retailer, which is normal and correct, and treating them as an
# official-store claim would fire on almost every commerce answer.
_RELATIONSHIP = r"(?:official\s+(?:web\s?site|site|store|shop|online\s+store|retailer))"

# A host, as it appears in prose: at least two labels and a TLD, optionally
# wrapped in a URL. Trailing sentence punctuation is stripped by the caller.
_HOST = r"(?:https?://)?(?:www\.)?([a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"

_PATTERNS = (
    # "The official website for Judydoll is judydoll.shop"
    re.compile(rf"\b{_RELATIONSHIP}\s+(?:for|of)\s+(?P<brand>[^,.;:]{{1,60}}?)\s+is\s+{_HOST}", re.I),
    # "Joocyee's official website is joocyeebeauty.com"
    # "Joocyee's official website for US shoppers is joocyee.co"
    re.compile(rf"(?P<brand>[^,.;:]{{1,60}}?)(?:'s|’s)\s+{_RELATIONSHIP}(?:\s+for\s+[^,.;:]{{1,40}}?)?\s+is\s+{_HOST}", re.I),
    # "Judydoll's products are sold on their official website, judydoll.shop"
    re.compile(rf"\b{_RELATIONSHIP}\s*[,:]\s*{_HOST}", re.I),
)

CLAIM_OFFICIAL_STORE = "official_store"


def _sentences(text: str) -> List[str]:
    """Split on sentence enders, keeping it dumb on purpose.

    A claim is scoped to ONE sentence: "Their official site is example.com. They
    also sell on retailer.com" must not attribute retailer.com to the claim. A
    smarter splitter would buy nothing here and would make the excerpt returned
    to the merchant harder to check against the raw answer.
    """
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def extract_destination_claims(
    text: Optional[str],
    *,
    verified_official_hosts: Optional[Iterable[str]] = None,
    brand: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Destination-relationship claims asserted in one answer's prose.

    Returns one dict per claim:
        claim_kind          always CLAIM_OFFICIAL_STORE today
        claimed_host        normalized host the answer called official
        matches_verified    True / False / None  (None = we have no verified
                            set, so we CANNOT say whether it is right)
        brand_mentioned     the brand string the sentence bound the claim to
        excerpt             the sentence, so a human can check the machine

    `matches_verified=None` is load-bearing. With no verified official-domain
    set we know a claim was made and nothing about whether it is true, and
    reporting that as False would manufacture the alarming reading out of
    missing configuration. Item 5 supplies the set.
    """
    verified = {
        normalize_host(h) for h in (verified_official_hosts or []) if h
    }
    verified.discard("")

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for sentence in _sentences(text or ""):
        for pattern in _PATTERNS:
            for m in pattern.finditer(sentence):
                host = normalize_host((m.group(m.lastindex) or "").rstrip(".,;:!?"))
                if not host or "." not in host:
                    continue
                claimed_brand = ""
                try:
                    claimed_brand = (m.groupdict().get("brand") or "").strip()
                except Exception:  # noqa: BLE001 - group may not exist on pattern 3
                    claimed_brand = ""
                # A brand was supplied and the sentence names a DIFFERENT one:
                # the answer is talking about somebody else's official store,
                # which is not this merchant's evidence.
                if brand and claimed_brand:
                    if brand.strip().lower() not in claimed_brand.lower() \
                            and claimed_brand.lower() not in brand.strip().lower():
                        continue
                if host in seen:
                    continue
                seen.add(host)
                out.append({
                    "claim_kind": CLAIM_OFFICIAL_STORE,
                    "claimed_host": host,
                    "matches_verified": (host in verified) if verified else None,
                    "brand_mentioned": claimed_brand or None,
                    "excerpt": sentence[:300],
                })
                break
    return out


def claims_pointing_away(claims: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The subset that is merchant evidence: an answer named an official store
    that is NOT one of the merchant's verified domains.

    `matches_verified is None` is excluded on purpose — unknown is not wrong,
    and this list is what a merchant-facing finding is built from.
    """
    return [c for c in claims if c.get("matches_verified") is False]
