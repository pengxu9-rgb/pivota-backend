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

from services.brand_claim_service import is_valid_public_hostname, normalize_host

# The relationship words that make a sentence a CLAIM rather than a mention.
#
# "official retailer" is DELIBERATELY ABSENT, and was present in the first cut,
# contradicting the paragraph above. "ANUKO's official retailer is
# oliveyoung.com" is a correct, desirable and very common sentence in this
# vertical; reporting it as "AI named a store you do not own" is precisely the
# false alarm this module is built to avoid.
_RELATIONSHIP = r"(?:official\s+(?:web\s?site|site|store|shop|online\s+store))"

# A host as it appears in prose. Validated against is_valid_public_hostname
# afterwards — this pattern alone matches "5.0", which was reported to a
# merchant as their official store.
_HOST = (
    r"(?P<host>(?:https?://)?(?:www\.)?"
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+)"
)

# The brand, bound TIGHTLY to the relationship phrase: at most three words
# immediately adjacent to it.
#
# The first cut used `[^,.;:]{1,60}?`, which swallowed the whole clause — so
# "Judydoll is sold at Sephora and Sephora's official website is sephora.com"
# captured "Judydoll is sold at Sephora and Sephora", matched the merchant by
# SUBSTRING, and told them AI had named Sephora as their official store.
_BRAND = r"(?P<brand>(?:[A-Za-z0-9&'’\-]+(?:\s+|$)){1,3}?)"

# The possessive form needs a brand class WITHOUT the apostrophe: otherwise the
# brand group swallows the "'s" that the pattern then expects to match, and
# "Joocyee's official website is joocyeebeauty.com" — a real engine claim, and
# one of this module's three motivating examples — silently stops matching.
_BRAND_POSS = r"(?P<brand>(?:[A-Za-z0-9&\-]+\s+){0,2}[A-Za-z0-9&\-]+)"

# Hedges and negations. A sentence that questions or denies the relationship is
# not an assertion of it, and reporting "It is unclear whether X is official" as
# a claim overstates what the engine said.
_HEDGE = re.compile(
    r"\b(?:unclear|not\s+clear|no\s+evidence|cannot\s+confirm|can'?t\s+confirm|"
    r"unverified|allegedly|claims?\s+to\s+be|purports?|may\s+be|might\s+be|"
    r"appears\s+to\s+be|seems\s+to\s+be|possibly|reportedly)\b",
    re.I,
)

_PATTERNS = (
    # "The official website for Judydoll is judydoll.shop"
    re.compile(rf"\b{_RELATIONSHIP}\s+(?:for|of)\s+{_BRAND}is\s+{_HOST}", re.I),
    # "Joocyee's official website is joocyeebeauty.com"
    # "Joocyee's official website for US shoppers is joocyee.co"
    re.compile(
        rf"{_BRAND_POSS}(?:'s|’s)\s+{_RELATIONSHIP}"
        rf"(?:\s+for\s+[^,.;:]{{1,40}}?)?\s+is\s+{_HOST}",
        re.I,
    ),
    # "…their official website, judydoll.shop"  /  "Official site: judydoll.shop"
    #
    # This pattern has NO brand group, so the brand guard cannot apply to it —
    # in the first cut that made it attribute ANY "official website, <host>" to
    # whatever merchant the report belonged to, including a competitor's. It is
    # kept because the shape is real, but it now yields claims marked
    # `brand_bound=False`, and the finding layer requires a bound claim.
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
    brand_norm = _norm(brand) if brand else ""

    out: List[Dict[str, Any]] = []
    seen: set = set()
    for sentence in _sentences(text or ""):
        hedged = bool(_HEDGE.search(sentence))
        for pattern in _PATTERNS:
            for m in pattern.finditer(sentence):
                host = normalize_host((m.group("host") or "").rstrip(".,;:!?"))
                # A dotted token is not a hostname. Without this the extractor
                # reported "5.0" (from "…is 5.0 out of 5 stars") to a merchant
                # as their official store.
                if not host or not is_valid_public_hostname(host):
                    continue
                groups = m.groupdict()
                claimed_brand = (groups.get("brand") or "").strip()
                # Pattern 3 carries no brand group, so nothing binds its claim
                # to this merchant. Recorded, but never merchant evidence.
                brand_bound = "brand" in groups and bool(claimed_brand)
                if brand_norm and brand_bound:
                    # TOKEN equality, not substring. `brand in claimed_brand`
                    # is a "appears anywhere in the clause" test: it bound
                    # "Anua" inside "Manual" and matched a competitor named
                    # three words earlier.
                    if not _tokens_match(brand_norm, _norm(claimed_brand)):
                        continue
                if host in seen:
                    continue
                seen.add(host)
                out.append({
                    "claim_kind": CLAIM_OFFICIAL_STORE,
                    "claimed_host": host,
                    "matches_verified": (
                        _is_own_host(host, verified) if verified else None
                    ),
                    "brand_mentioned": claimed_brand or None,
                    "brand_bound": brand_bound,
                    "hedged": hedged,
                    "excerpt": sentence[:300],
                })
                break
    return out


def _is_own_host(host: str, own: Iterable[str]) -> bool:
    """Exact host or a subdomain of one.

    Subdomain-aware because `us.brand.com` IS the merchant and reporting it as
    a foreign store would be a false alarm on the merchant's own regional site.
    Resemblance is deliberately NOT accepted: `brand.shop` is a claim to check,
    not a match — see _known_official_hosts in audit_evidence_builder for why
    that distinction is the whole feature.
    """
    h = (host or "").strip().lower().lstrip(".")
    for o in own:
        o = (o or "").strip().lower().lstrip(".")
        if o and (h == o or h.endswith("." + o)):
            return True
    return False


def _norm(value: str) -> str:
    """Casefold and strip punctuation that differs between a brand record and
    an engine's prose (possessives, accents are left alone deliberately —
    see the note in _tokens_match)."""
    return re.sub(r"[^\w\s]", " ", str(value or "")).casefold().strip()


def _tokens_match(brand: str, claimed: str) -> bool:
    """True when the claimed brand IS the brand, on token boundaries.

    Accepts a claimed string that ends with the brand ("the retailer Judydoll")
    or equals it, and rejects one that merely contains it as a substring of a
    different word. Multi-word brands are matched as a contiguous token run.
    """
    b = brand.split()
    c = claimed.split()
    if not b or not c:
        return False
    n = len(b)
    return any(c[i:i + n] == b for i in range(len(c) - n + 1))


def claims_pointing_away(claims: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The subset that is merchant evidence.

    Four conditions, each removing a way the first cut produced a false alarm:

      matches_verified is False  — not None. Unknown is not wrong; with no
                                   host set we know a claim was made and
                                   nothing about whether it is right.
      brand_bound                — the sentence tied the claim to THIS brand.
                                   Pattern 3 has no brand group, so its claims
                                   are observations, never accusations.
      not hedged                 — "it is unclear whether X is official" is a
                                   question, not an assertion.
    """
    return [
        c for c in claims
        if c.get("matches_verified") is False
        and c.get("brand_bound")
        and not c.get("hedged")
    ]
