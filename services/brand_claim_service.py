"""P1 — brand claim WRITE PATH (the one missing primitive).

classify_offer_type (services/offer_classification.py) only returns
'brand_direct' for an internal merchant whose
catalog_merchants.metadata_json.brand_relationship == 'brand_direct' — a
VERIFIED value it "never assumes." No service ever wrote it. This module is
that writer: a brand claims its merchant/domain, verifies ownership (DNS TXT
first), and on success we set brand_relationship='brand_direct'.

Storefront-agnostic: claiming is domain/registry-based, and catalog_merchants is
the storefront-optional registry — so a store-less brand (e.g. Anua/Anuko) can
enter the index as a brand-attested canonical product.

Pure helpers (brand_direct_metadata / dns_txt_proves_claim / make_challenge_token)
are unit-tested with no I/O; the DB-bound functions are best-effort.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from db import brand_claims as bc
from db.database import database

logger = logging.getLogger(__name__)

BRAND_DIRECT = "brand_direct"
_TXT_PREFIX = "pivota-verify="


# ---------------------------------------------------------------------------
# Pure helpers (no I/O — unit-tested)
# ---------------------------------------------------------------------------
def brand_direct_metadata(existing: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge brand_relationship='brand_direct' into a metadata_json dict without
    clobbering other keys. This value is the ONLY thing classify_offer_type
    trusts to surface a brand-direct offer."""
    md = dict(existing or {})
    md["brand_relationship"] = BRAND_DIRECT
    return md


def make_challenge_token() -> str:
    """A DNS/email verification challenge: 'pivota-verify=<random>'."""
    return _TXT_PREFIX + secrets.token_urlsafe(24)


def make_email_code() -> str:
    """A short, human-enterable email verification code (6 digits). Sent to a
    brand-domain alias; entering it proves control of that mailbox."""
    return f"{secrets.randbelow(10 ** 6):06d}"


def email_target_valid(email: Optional[str], brand_domain: Optional[str]) -> bool:
    """The verification email MUST be a well-formed address AT the brand_domain —
    receiving a code there proves control of a brand-domain mailbox (the email
    analogue of DNS-TXT control). Case-insensitive EXACT domain match (no
    subdomains — avoids attacker-controlled `x.brand_domain` look-alikes)."""
    e = (email or "").strip().lower()
    d = (brand_domain or "").strip().lower()
    if not e or not d or e.count("@") != 1:
        return False
    local, _, host = e.partition("@")
    return bool(local) and host == d


def brand_claim_email_enabled() -> bool:
    """Flag: accept the email claim-verification method. Default OFF — ships dark,
    flipped per-env once the email infra is confirmed for this surface."""
    import os

    return os.getenv("ENABLE_BRAND_CLAIM_EMAIL_VERIFY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


async def send_brand_claim_email(
    to_email: str, code: str, brand_domain: Optional[str]
) -> bool:
    """Best-effort: email the verification code to a brand-domain alias. Never
    raises — a send failure just leaves the pending claim (the brand re-starts).
    Runs the sync SES sender off the event loop."""
    import asyncio
    import os

    from utils.email_sender import send_email

    subject = "Verify your Pivota brand claim"
    text = (
        f"Your Pivota brand verification code for {brand_domain or 'your brand'} "
        f"is: {code}\n\nEnter it in Pivota to verify your brand. If you didn't "
        "request this, you can ignore this email."
    )
    try:
        result = await asyncio.to_thread(
            send_email,
            to_email=to_email,
            subject=subject,
            text_body=text,
            from_email=(os.getenv("FROM_EMAIL") or "noreply@pivota.ai").strip(),
            tags={"type": "brand_claim_verify"},
        )
        return bool(getattr(result, "ok", False))
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "send_brand_claim_email failed for %s: %s", to_email, str(exc)[:200]
        )
        return False


def dns_txt_proves_claim(expected_token: str, txt_records: Iterable[str]) -> bool:
    """True iff the expected 'pivota-verify=<token>' value appears verbatim among
    the domain's TXT records. Exact-match — a partial/prefix match doesn't prove
    control of the record's value."""
    if not expected_token:
        return False
    return any((r or "").strip() == expected_token for r in (txt_records or []))


# ---------------------------------------------------------------------------
# Brand-identity binding (B1) + hostname validation (B4)
# DNS control proves "I control this domain" — NOT "I am this brand". We only
# flip brand_direct when the verified domain is also one Pivota already
# associates with the merchant (its catalog source/canonical hosts or onboarding
# store_url). Otherwise the claim is recorded but left for review.
# ---------------------------------------------------------------------------
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$"
)


def normalize_host(value: Optional[str]) -> str:
    """Lowercase registrable host from a domain or URL: strip scheme, userinfo,
    port, path, and a leading 'www.'."""
    if not value or not isinstance(value, str):
        return ""
    v = value.strip().lower()
    v = urlparse(v).netloc if "://" in v else v.split("/")[0]
    v = v.split("@")[-1].split(":")[0]
    return v[4:] if v.startswith("www.") else v


def is_valid_public_hostname(value: Optional[str]) -> bool:
    """B4: a well-formed public hostname (labels + a TLD). Rejects internal
    names, IPs, and malformed input before we ever resolve TXT on it."""
    host = normalize_host(value)
    return bool(host) and len(host) <= 253 and bool(_HOSTNAME_RE.match(host))


# Public / shared-platform suffixes under which two different registrations are
# NOT the same organization. A host that IS one of these — or that has too few
# labels to be a registrable domain (a bare TLD like "com") — must never anchor
# a "same registrable org" suffix match: otherwise a junk/short host in the
# merchant's OWN catalog (source_domain/canonical_url) or onboarding
# (store_url/website) — e.g. a bare "myshopify.com" or "com" — would widen the
# bind to every unrelated org that merely shares that suffix.
#
# The repo has no Public Suffix List dependency (cf. canonical_source_discovery's
# curated _TLD_LABELS/_GENERIC_DOMAINS), so this is a conservative, additive
# subset of the PSL, not an exhaustive one. The has-a-dot rule in
# _is_registrable_base is the always-on structural backstop for bare TLDs; this
# set extends it to the multi-label suffixes that rule can't catch on its own.
_PUBLIC_SUFFIXES = frozenset({
    # shared storefront platforms: one tenant (shop.myshopify.com) is a
    # different org from another tenant of the same platform. Kept in step with
    # the platform tokens canonical_source_discovery._GENERIC_DOMAINS already
    # treats as never-a-brand.
    "myshopify.com", "shopify.com", "wixsite.com", "bigcartel.com",
    "squarespace.com",
    # common multi-label registry suffixes (a registrable domain sits one label
    # deeper than these). Covers Pivota's active markets (KR/JP/UK/AU/...).
    "co.uk", "org.uk", "me.uk", "ac.uk", "gov.uk",
    "co.kr", "or.kr", "ne.kr", "go.kr",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.au", "net.au", "org.au",
    "com.br", "com.cn", "com.hk", "com.mx", "com.my", "com.ph",
    "com.sg", "com.tr", "com.tw",
    "co.id", "co.il", "co.in", "co.nz", "co.th", "co.za",
})


def _is_registrable_base(host: str) -> bool:
    """True iff `host` is specific enough to anchor a 'same registrable org'
    suffix match: it has at least two labels AND is not itself a public/shared
    suffix (a bare TLD like 'com', a registry suffix like 'co.uk', or a
    storefront platform like 'myshopify.com'). Suffix-matching against a public
    suffix would bind unrelated orgs that merely share it, so we refuse to."""
    if not host or "." not in host:
        return False  # single label / bare TLD — never a registrable domain
    return host not in _PUBLIC_SUFFIXES


def host_matches_known(domain: Optional[str], known_hosts: Iterable[str]) -> bool:
    """Pure: does the claimed domain match a known merchant host? Exact match, or
    same registrable org (sub.brand.com <-> brand.com) — but a subdomain match is
    honored ONLY when the host acting as the registrable base is a real
    registrable domain, never a public/platform suffix. That guard keeps a
    junk/short host in the merchant's own catalog/onboarding data (e.g. a bare
    'myshopify.com' or 'com') from widening the bind to unrelated orgs."""
    d = normalize_host(domain)
    if not d:
        return False
    for kh in known_hosts:
        k = normalize_host(kh)
        if not k:
            continue
        if d == k:
            return True  # exact host — the strongest, unconditional bind
        # d is a subdomain of k  -> k is the registrable base
        if d.endswith("." + k) and _is_registrable_base(k):
            return True
        # k is a subdomain of d  -> d is the registrable base
        if k.endswith("." + d) and _is_registrable_base(d):
            return True
    return False


async def _inferred_merchant_hosts(merchant_id: str, *, strict: bool = False) -> set:
    """The INFERRED tier, unchanged: hosts Pivota derives from what it already
    holds — onboarding store_url/website + catalog product source/canonical
    hosts. Does NOT include pivota_canonical_url (that's Pivota's host, not the
    brand's).

    Kept as a separate function because it is now one of two tiers, and because
    the liveness sweep needs it to seed rows it can check
    (services/official_domain_liveness.seed_inferred_domains). It is NOT
    weakened and NOT removed: merchants who have never asserted a domain still
    get exactly the set they got before, minus anything measured dead.
    """
    hosts: set = set()
    if not merchant_id:
        return hosts
    try:
        from db.merchant_onboarding import get_merchant_onboarding

        ob = await get_merchant_onboarding(merchant_id) or {}
        for key in ("store_url", "website"):
            h = normalize_host(ob.get(key))
            if h:
                hosts.add(h)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        logger.warning("_inferred_merchant_hosts: onboarding load failed: %s", str(exc)[:200])
    try:
        rows = await database.fetch_all(
            """
            SELECT DISTINCT source_domain, canonical_url
              FROM catalog_products
             WHERE merchant_id = :merchant_id
             LIMIT 500
            """,
            {"merchant_id": merchant_id},
        )
        for r in rows or []:
            for key in ("source_domain", "canonical_url"):
                h = normalize_host(r[key])
                if h:
                    hosts.add(h)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise
        logger.warning("_inferred_merchant_hosts: catalog load failed: %s", str(exc)[:200])
    return hosts


async def merchant_owned_domains_detailed(
    merchant_id: str, *, strict: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """B1 — the official-domain set WITH its provenance, host -> details.

    Two tiers, and the difference between them is the whole point of B1:

      ASSERTED / VERIFIED — rows in `merchant_official_domains` the merchant
      supplied or a brand claim proved. Included unconditionally, whether or not
      inference ever found them. This is the anua.us half of the defect: anua.com
      and anua.us are byte-identical storefronts, only anua.com was ever
      inferred, and 7 citations of anua.us were scored as retailer traffic —
      reading the branded official share as 46% instead of 67%.

      INFERRED — the legacy derivation, still the fallback for every merchant
      who has asserted nothing. Unchanged, except that a host whose STORED
      liveness verdict is `dead` drops out. This is the us.judydoll.com half:
      inferred, counted official, and carrying no DNS record at all.

    Only `dead` excludes. `unverifiable` and `unchecked` stay in the set — see
    db/merchant_official_domains.is_excluded and the measurement behind it.

    A stored row whose source is `inferred` is honoured only while inference
    still produces that host: inference is the live truth for its own tier, so a
    row left behind by a catalog that has moved on must not outlive it.

    Each value carries {source, liveness_status, verification_status,
    is_primary, last_checked_at} so a caller can tell "verified live" from
    "inferred, never checked". NOT wired into agent_center yet — the report
    will want it, and `merchant_owned_domains` stays the set-shaped contract
    every existing caller uses.
    """
    from db import merchant_official_domains as mod

    detailed: Dict[str, Dict[str, Any]] = {}
    if not merchant_id:
        return detailed

    inferred = (
        await _inferred_merchant_hosts(merchant_id, strict=True) if strict
        else await _inferred_merchant_hosts(merchant_id)
    )
    # Best-effort by construction for REPORTS: on any DB error this returns [],
    # and the result degrades to exactly today's inferred set rather than to
    # nothing. A GUARD passes strict=True and gets the exception instead — an
    # empty owned set on a DB error read as "the merchant owns nothing here",
    # which let a declaration through onto a host it already had.
    stored = (
        await mod.list_official_domains(merchant_id, strict=True) if strict
        else await mod.list_official_domains(merchant_id)
    )
    by_domain = {str(r.get("domain") or ""): r for r in stored if r.get("domain")}

    def _detail(host: str, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            # True when inference independently produces this host, whatever the
            # stored row says. The binding view needs it: a stored `asserted`
            # row must not SHADOW a genuine inferred membership for the same
            # host, or a merchant who claimed a domain before declaring it is
            # locked out of ever verifying it.
            "also_inferred": host in inferred,
            "source": str((row or {}).get("source") or mod.SOURCE_INFERRED),
            "liveness_status": str(
                (row or {}).get("liveness_status") or mod.LIVENESS_UNCHECKED
            ),
            "verification_status": (row or {}).get("verification_status"),
            "is_primary": bool((row or {}).get("is_primary") or False),
            "last_checked_at": (row or {}).get("last_checked_at"),
        }

    for host, row in by_domain.items():
        if str(row.get("source") or "") not in mod.OFFICIAL_SOURCES:
            continue
        if mod.is_excluded(row.get("liveness_status")):
            continue
        detailed[host] = _detail(host, row)

    for host in inferred:
        if host in detailed:
            continue
        row = by_domain.get(host)
        if row is not None and mod.is_excluded(row.get("liveness_status")):
            continue
        detailed[host] = _detail(host, row)

    return detailed


async def merchant_owned_domains(merchant_id: str, *, strict: bool = False) -> set:
    """The official-domain set as a plain set of hosts — the shape every caller
    already depends on (notably `build_authority_map(merchant_extra_hosts=...)`
    in services/agent_center_bd_report_service.py, which decides `first_party`
    on every cited host). See `merchant_owned_domains_detailed` for what changed
    behind it: the set is now asserted/verified plus inferred, minus anything
    measured DEAD. `strict=True` raises on a DB error instead of returning a
    smaller set; see merchant_owned_domains_detailed."""
    if strict:
        return set(await merchant_owned_domains_detailed(merchant_id, strict=True))
    return set(await merchant_owned_domains_detailed(merchant_id))


async def record_official_domain(
    merchant_id: str,
    domain: Optional[str],
    *,
    source: str = "verified",
) -> bool:
    """Backfill hook: a claim just PROVED control of `domain`, so promote it to
    the official set.

    `source='verified'` means control proven AND bound to this merchant's brand
    identity. `source='asserted'` means control proven, binding not established
    — still far stronger evidence than inference, which counts with no proof at
    all, but it does not grant brand_direct.

    Best-effort, like every other write in this file — a claim must never fail
    because the official-domain table was unavailable. The liveness sweep is the
    eventual-consistency net, and inference still covers the domain in the
    meantime if it was derivable at all.

    Liveness is left `unchecked` on a FRESH row (a claim proves control, not
    that the storefront answers HTTP); on an existing row the sweep's recorded
    verdict is preserved rather than blanked rather than assumed live: we just
    proved DNS TXT control or mailbox control, neither of which is evidence that
    the storefront answers HTTP.
    """
    from db import merchant_official_domains as mod

    host = normalize_host(domain)
    if not merchant_id or not host:
        return False
    try:
        return await mod.upsert_official_domain(
            merchant_id=merchant_id,
            domain=host,
            source=source,
            verification_status=mod.VERIFICATION_VERIFIED,
        )
    except Exception as exc:  # noqa: BLE001 — never break claim verification
        logger.warning(
            "record_official_domain failed for %s/%s: %s",
            merchant_id, host, str(exc)[:200],
        )
        return False


DECLARE_OK = "declared"
DECLARE_INVALID_HOST = "invalid_hostname"
DECLARE_TAKEN = "claimed_by_another_merchant"
DECLARE_ALREADY_PROVEN = "already_proven"
DECLARE_NOT_REGISTRABLE = "not_a_registrable_domain"
DECLARE_TOO_MANY = "too_many_declarations"
DECLARE_ALREADY_KNOWN = "already_in_your_official_set"
DECLARE_WRITE_FAILED = "write_failed"
# The owned-set read failed, so the question "does the merchant already have
# this host" could not be answered. Refused — NOT "taken", which would tell the
# merchant a rival owns their domain, and NOT granted, which is the fail-open
# downgrade this status exists to prevent. The route maps it to 503.
DECLARE_UNAVAILABLE = "official_set_unavailable"

# A merchant with more than this many unproven declarations is not filling in a
# second storefront, and each row is a free write that later readers must skip.
_MAX_DECLARED_PER_MERCHANT = 20


async def declare_official_domain(
    merchant_id: str, domain: Optional[str],
) -> Dict[str, Any]:
    """P0 item 5 — a merchant states an additional official domain.

    WHY A THIRD SOURCE. `verified` and `asserted` both mean CONTROL WAS PROVEN
    (they differ on whether the domain is also bound to the brand identity). A
    self-declaration has proven nothing, so it is written as `declared`, which
    is deliberately NOT in OFFICIAL_SOURCES: it is stored so the portal can
    offer to verify it and so a claim can be started against it, and it does not
    widen the set that decides `first_party`. A merchant who declared a retailer
    would otherwise reclassify that retailer's citations as their own.

    WHY THIS MATTERS. Measured in production: 1 of 42 merchants has any official
    domain row, and 16 of 17 audited merchants fall back entirely to inference —
    the condition the evidence base measured as a 13-point error on Anua's
    headline, because inference knew `anua.com` and not `anua.us`.

    Refuses a domain another merchant has already PROVEN. Declaration is cheap
    and unproven, so without that guard it would be a way to attach a rival's
    verified storefront to your own audit. It does NOT refuse a domain another
    merchant merely declared — two unproven claims on one host is a conflict for
    verification to settle, not for whoever typed first to win.

    Returns {status, domain, ...}; never raises for ordinary refusals.
    """
    from db import merchant_official_domains as mod

    host = normalize_host(domain)
    if not merchant_id or not is_valid_public_hostname(host):
        return {"status": DECLARE_INVALID_HOST, "domain": host or None}
    # A public suffix or shared platform host is not a domain anyone owns.
    # `myshopify.com` is not a merchant's storefront — one tenant of it is — and
    # this module already keeps the list for exactly this class of widening.
    if not _is_registrable_base(host):
        return {"status": DECLARE_NOT_REGISTRABLE, "domain": host}

    # STRICT: the resolver's default swallows a DB error into None, and `None`
    # reads as "nobody owns it" below -- a grant. The except was unreachable
    # through the real function and only its monkeypatched test ever hit it.
    owner = None
    try:
        owner = await mod.resolve_verified_merchant_for_domain(host, strict=True)
    except Exception:  # noqa: BLE001 — a lookup failure must not grant the write
        logger.warning("declare_official_domain owner lookup failed for %s",
                       host, exc_info=True)
        return {"status": DECLARE_UNAVAILABLE, "domain": host}
    if owner and str(owner) != str(merchant_id):
        return {"status": DECLARE_TAKEN, "domain": host}
    # `resolve_verified_merchant_for_domain` only finds `verified` owners, but
    # `asserted` ALSO means control was proven — it just is not bound to the
    # brand. Both are proof, so both must block someone else's unproven
    # declaration; checking only `verified` left a gap this guard's own
    # description did not admit to.
    try:
        proven_elsewhere = await mod.domain_is_proven_by_other_merchant(
            host, merchant_id,
        )
    except Exception:  # noqa: BLE001 — fails CLOSED, like the lookup above
        logger.warning("declare_official_domain proof lookup failed for %s",
                       host, exc_info=True)
        # UNAVAILABLE, not TAKEN: "we could not check" told as "a rival owns
        # your domain" is a 409 that lies about the world, on our outage.
        return {"status": DECLARE_UNAVAILABLE, "domain": host}
    if proven_elsewhere:
        return {"status": DECLARE_TAKEN, "domain": host}

    # Already proven for THIS merchant: declaring adds nothing and must not
    # downgrade a verified row to an unproven one.
    # STRICT, and refused on failure. The default `list_official_domains`
    # swallows its own errors and returns [], so `existing = {}` here meant the
    # ALREADY_PROVEN check and the cap were both skipped on a DB blip, and the
    # write below then flipped a VERIFIED row to declared/pending. The two
    # ownership lookups above deliberately fail closed; this one did not.
    try:
        existing = {
            str(r.get("domain") or ""): r
            for r in (await mod.list_official_domains(merchant_id, strict=True) or [])
        }
    except Exception:  # noqa: BLE001 — fails CLOSED
        logger.warning("declare_official_domain stored-set load failed for %s",
                       host, exc_info=True)
        return {"status": DECLARE_UNAVAILABLE, "domain": host}
    row = existing.get(host)
    if row and str(row.get("source") or "") in mod.OFFICIAL_SOURCES:
        return {"status": DECLARE_ALREADY_PROVEN, "domain": host,
                "source": row.get("source")}

    # ALREADY IN THE OFFICIAL SET — including by INFERENCE — so there is
    # nothing to declare, and declaring would actively damage it.
    #
    # This is the invariant made true by construction rather than patched at
    # each consumer. The set a run USES is
    #     (stored rows whose source is in OFFICIAL_SOURCES) UNION (inferred)
    # which no filter on the `source` column alone can express. Two earlier
    # fixes tried and both were wrong in the same way: the upsert does
    # `source = excluded.source`, so declaring an INFERRED host flipped its row
    # to `declared`, and then
    #   - the liveness sweep's `source <> 'declared'` skipped it forever, while
    #     the inferred branch kept counting it official — a host that can never
    #     be measured dead, which is the us.judydoll.com overstatement this
    #     table exists to remove; and
    #   - the audit basis stopped recording a host the run demonstrably used.
    #
    # A declaration can therefore only ever create a host that is NEW AT
    # DECLARE TIME. That is the whole guarantee this guard gives -- not
    # "disjoint by construction", which three review rounds asserted and the
    # fourth falsified: declare anua.us before the catalog carries it, then
    # ingest, and inference produces a host whose row says `declared`. The
    # other ordering is healed elsewhere: the liveness seeder promotes such a
    # row to `inferred` (services/official_domain_liveness.seed_inferred_domains)
    # and the audit basis records a declared host that inference also produces
    # (services/audit_evidence_builder.record_audit_basis). Both read the USED
    # set, not the source column alone.
    #
    # The CAP is checked before this load on purpose: the owned-set read is a
    # 500-row catalog scan plus an onboarding read on an unrate-limited route,
    # and a merchant already at the cap should not get it for free.
    declared_count = sum(
        1 for r in existing.values()
        if str(r.get("source") or "") == mod.SOURCE_DECLARED
    )
    if declared_count >= _MAX_DECLARED_PER_MERCHANT:
        return {"status": DECLARE_TOO_MANY, "domain": host,
                "declared_count": declared_count}

    try:
        already = await merchant_owned_domains(str(merchant_id), strict=True)
    except Exception:  # noqa: BLE001 — fails CLOSED, and says so
        logger.warning("declare_official_domain owned-set load failed for %s",
                       host, exc_info=True)
        return {"status": DECLARE_UNAVAILABLE, "domain": host}
    if host in already:
        return {"status": DECLARE_ALREADY_KNOWN, "domain": host}

    # INSERT-ONLY, never the upsert. The upsert's `source = excluded.source`
    # is the lever behind every downgrade this function has had to guard
    # against; `insert_declared_domain` cannot overwrite a row of any other
    # source, so a guard that raced or failed still cannot damage the set. It
    # writes PENDING, not VERIFIED: stamping an unproven row verified would
    # make it indistinguishable from a proven one in every later read.
    try:
        landed = await mod.insert_declared_domain(merchant_id=merchant_id, domain=host)
    except Exception:  # noqa: BLE001
        logger.warning("declare_official_domain write failed for %s/%s",
                       merchant_id, host, exc_info=True)
        return {"status": DECLARE_WRITE_FAILED, "domain": host}
    if landed is None:
        # A FAILED WRITE IS NOT A BAD HOSTNAME. Returning INVALID_HOST here is
        # how the missing migration presented as "domain must be a valid public
        # hostname": the feature could not store anything and told the merchant
        # their own valid domain was the problem.
        return {"status": DECLARE_WRITE_FAILED, "domain": host}
    if landed != mod.SOURCE_DECLARED:
        # Lost a race with a claim or a sweep that wrote the row first. Nothing
        # was overwritten — that is the point of the INSERT-ONLY statement.
        return {"status": DECLARE_ALREADY_PROVEN, "domain": host, "source": landed}

    return {
        "status": DECLARE_OK,
        "domain": host,
        "source": mod.SOURCE_DECLARED,
        "counts_toward_official_set": False,
        "next_step": (
            "Start a brand claim for this domain and publish the DNS TXT "
            "record to prove control; it is not counted until then."
        ),
    }


async def record_verified_official_domain(
    merchant_id: str, domain: Optional[str]
) -> bool:
    """Brand-bound verification: control proven AND the domain belongs to this
    merchant's known identity. Thin wrapper kept for the existing call sites."""
    from db import merchant_official_domains as mod

    return await record_official_domain(
        merchant_id, domain, source=mod.SOURCE_VERIFIED
    )


async def merchant_bound_domains(merchant_id: str) -> set:
    """The BINDING view: hosts that establish this merchant's brand identity.

    Deliberately NOT the same set as `merchant_owned_domains`. That one answers
    "is this cited host a destination the merchant owns?" and counts `asserted`
    rows — control proven, binding not established. This one answers "is this
    domain evidence of who the merchant IS?", which `asserted` cannot be without
    circularity: verify_brand_claim WRITES an asserted row on the unbound branch,
    so if the binding check read it back, a second identical /claim/verify call
    would find the row its own first call had just written and grant brand_direct
    — turning "needs review" into a one-call delay instead of a gate.

    A host that is asserted AND independently inferred is admitted: inference is
    evidence the merchant supplied elsewhere, and shadowing it would lock out a
    merchant who happened to claim the domain before declaring it.

    Two limits worth knowing, both PRE-EXISTING and neither closed here:
      * The exclusion is HOST-EXACT, while host_matches_known is suffix-aware.
        An excluded `x.example` is still reachable through an inferred relative
        like `us.x.example`.
      * The inferred tier is merchant-SELF-DECLARED — PUT /merchant/profile
        writes `website` with no proof — so this gate ultimately reduces to
        "prove DNS control of a domain you also typed into your profile".
        Hardening that is a separate change to the profile write path.
    """
    from db import merchant_official_domains as mod

    detailed = await merchant_owned_domains_detailed(merchant_id)
    return {
        host
        for host, detail in detailed.items()
        if str(detail.get("source") or "") != mod.SOURCE_ASSERTED
        or bool(detail.get("also_inferred"))
    }


async def merchant_owns_domain(merchant_id: str, domain: str) -> bool:
    """True iff `domain` is bound to the merchant's known brand identity.

    Reads the BINDING view, which excludes `asserted` — see
    merchant_bound_domains for why reading the reporting set here is circular."""
    return host_matches_known(domain, await merchant_bound_domains(merchant_id))


# ---------------------------------------------------------------------------
# THE missing primitive: write the verified brand relationship
# ---------------------------------------------------------------------------
async def set_merchant_brand_direct(merchant_id: str) -> bool:
    """Set metadata_json.brand_relationship='brand_direct' on catalog_merchants
    via an ATOMIC server-side JSONB merge (|| concat) — no read-modify-write, so
    a concurrent writer to metadata_json can't be clobbered (B2). Best-effort.

    CAST(:patch AS JSONB), NOT :patch::jsonb — the `::` cast-after-param form
    breaks SQLAlchemy text() binding (guarded by the repo's meta-invariant test).
    """
    if not merchant_id:
        return False
    patch = json.dumps(brand_direct_metadata(None))  # {"brand_relationship": "brand_direct"}
    try:
        await database.execute(
            """
            UPDATE catalog_merchants
               SET metadata_json = COALESCE(metadata_json, CAST('{}' AS JSONB))
                                   || CAST(:patch AS JSONB)
             WHERE merchant_id = :merchant_id
            """,
            {"patch": patch, "merchant_id": merchant_id},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "set_merchant_brand_direct failed for %s: %s", merchant_id, str(exc)[:200]
        )
        return False


# ---------------------------------------------------------------------------
# Claim lifecycle
# ---------------------------------------------------------------------------
async def start_brand_claim(
    *,
    merchant_id: str,
    brand_domain: Optional[str] = None,
    method: str = "dns",
    content_key: Optional[str] = None,
    user_id: Optional[str] = None,
    verification_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Begin a claim: issue a challenge + record a pending claim row. B3: reuse an
    existing pending claim for this (merchant, domain) rather than spamming a new
    row (and colliding with the partial-unique index).

    For the email method the challenge is a short code emailed to a brand-domain
    alias — it is NEVER echoed in the response (only the recipient mailbox sees
    it); DNS still returns its TXT token, which the brand must publish."""
    if brand_domain:
        existing = await bc.get_pending_brand_claim(merchant_id, brand_domain)
        if existing and existing.get("challenge_token"):
            ex_method = existing.get("claim_method") or method
            existing_token = existing["challenge_token"]
            email_sent = None
            if ex_method == "email" and verification_email:
                # re-send the SAME code to the (re-supplied) alias on reuse.
                email_sent = await send_brand_claim_email(
                    verification_email, existing_token, brand_domain
                )
            return _claim_response(
                claim_id=existing["claim_id"],
                method=ex_method,
                token=existing_token,
                brand_domain=brand_domain,
                verification_email=verification_email,
                email_sent=email_sent,
                reused=True,
            )

    token = make_email_code() if method == "email" else make_challenge_token()
    claim_id = await bc.insert_brand_claim(
        merchant_id=merchant_id,
        claim_method=method,
        brand_domain=brand_domain,
        content_key=content_key,
        challenge_token=token,
        created_by_user_id=user_id,
    )
    email_sent = None
    if method == "email" and verification_email:
        email_sent = await send_brand_claim_email(verification_email, token, brand_domain)
    return _claim_response(
        claim_id=claim_id,
        method=method,
        token=token,
        brand_domain=brand_domain,
        verification_email=verification_email,
        email_sent=email_sent,
        reused=False,
    )


def _claim_response(
    *,
    claim_id: Any,
    method: str,
    token: str,
    brand_domain: Optional[str],
    verification_email: Optional[str],
    email_sent: Optional[bool],
    reused: bool,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "claim_id": claim_id,
        "method": method,
        # SECURITY: never echo the emailed code — only the mailbox holder may see
        # it. DNS returns its TXT token (the brand must publish it to prove control).
        "challenge_token": None if method == "email" else token,
        "instructions": _claim_instructions(
            method, brand_domain, token, verification_email
        ),
        "reused": reused,
    }
    if method == "email":
        out["email_sent"] = email_sent
    return out


def _claim_instructions(
    method: str,
    brand_domain: Optional[str],
    token: str,
    verification_email: Optional[str] = None,
) -> str:
    if method == "dns":
        return f"Add a DNS TXT record to {brand_domain or 'your brand domain'} with value: {token}"
    if method == "email":
        target = verification_email or f"an email at {brand_domain or 'your brand domain'}"
        return (
            f"We emailed a 6-digit verification code to {target}. "
            "Enter it to verify your brand."
        )
    if method == "manual":
        return (
            "Contact Pivota support with evidence of brand ownership (e.g. brand "
            "registry, trademark, or authorized-representative proof). Our team "
            "will review and verify your claim."
        )
    return f"Verification token: {token}"


async def approve_manual_claim(
    claim_id: str, *, approved_by: str, evidence_ref: Optional[str] = None
) -> Dict[str, Any]:
    """Support-assisted verification: a Pivota EMPLOYEE, having reviewed a brand's
    ownership evidence offline, approves a method='manual' claim. The human review
    IS the verification — so this does NOT run the automated DNS/email proof or the
    B1 auto-binding; it directly grants brand_direct + promotes the brand's SKUs.
    Employee-gated at the route (approved_by is recorded in the proof). Only manual
    claims; idempotent; best-effort writes."""
    if not approved_by:
        return {"status": "forbidden"}
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}
    if claim.get("claim_method") != "manual":
        # dns / email self-verify; only the manual method is employee-approvable.
        return {"status": "not_manual", "method": claim.get("claim_method")}

    ok = await set_merchant_brand_direct(claim["merchant_id"])
    proof = f"manual:{approved_by}"
    if evidence_ref:
        proof = f"{proof}:{evidence_ref}"
    await bc.mark_claim_verified(claim_id, proof_ref=proof)
    # B1: a verified claim's domain joins the official set as source='verified'.
    await record_verified_official_domain(
        claim["merchant_id"], claim.get("brand_domain")
    )
    # Same lifecycle promotion as DNS/email: unclaimed -> claimed. Best-effort.
    from services.claim_state import promote_merchant_skus_to_claimed

    await promote_merchant_skus_to_claimed(claim["merchant_id"])
    # ADR-008 SLICE 2 — verify-to-serve: graduate this brand's brand-authored
    # products to the offer-free citation floor. Flag-gated + best-effort.
    await _graduate_storeless_brand_catalog(claim["merchant_id"])
    return {"status": "verified", "brand_direct_set": ok, "approved_by": approved_by}


async def _graduate_storeless_brand_catalog(merchant_id: str) -> None:
    """ADR-008 SLICE 2 — verify-to-serve. After a brand claim verifies, graduate
    the merchant's brand-authored products to the OFFER-FREE citation floor
    (index_eligible, NEUTRAL=merchant_owned, isolated, no offer minted).

    Flag-gated by ENABLE_STORELESS_BRAND_CATALOG: with the flag OFF this is a
    no-op, so verify behaves exactly as today. Best-effort — it must NEVER break
    claim verification, so any failure is swallowed (the nightly index-health job
    is the eventual-consistency safety net)."""
    if not merchant_id:
        return
    try:
        from readiness.flags import storeless_brand_catalog_enabled

        if not storeless_brand_catalog_enabled():
            return
        from services.brand_verified_graduation import (
            graduate_brand_authored_products,
        )

        await graduate_brand_authored_products(merchant_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break verify
        logger.warning(
            "verify-to-serve graduation failed for %s: %s",
            merchant_id, str(exc)[:200],
        )


async def verify_brand_claim(
    claim_id: str,
    *,
    submitted_code: Optional[str] = None,
    txt_resolver: Optional[Callable[[str], List[str]]] = None,
    owned_domain_check: Optional[Callable[[str, str], Any]] = None,
) -> Dict[str, Any]:
    """Verify a pending claim. DNS resolves the brand_domain's TXT records and
    checks the challenge token; EMAIL matches `submitted_code` against the code we
    sent to a brand-domain alias. Either proves DOMAIN CONTROL — but brand_direct
    means "verified as the brand," so we only flip the merchant when the verified
    domain is also BOUND to the merchant's known brand identity (B1). A
    domain-verified but unbound claim is NOT auto-granted — it returns
    'domain_verified_unbound' for review. Returns {status, brand_direct_set}."""
    claim = await bc.get_brand_claim(claim_id)
    if not claim:
        return {"status": "not_found"}
    if claim.get("verification_status") == bc.STATUS_VERIFIED:
        return {"status": "verified", "brand_direct_set": True}

    method = claim.get("claim_method")
    token = claim.get("challenge_token") or ""
    domain = claim.get("brand_domain")

    if method == "dns":
        resolver = txt_resolver or _default_txt_resolver
        try:
            records = resolver(domain) if domain else []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "verify_brand_claim: TXT resolve failed for %s: %s",
                domain, str(exc)[:200],
            )
            records = []
        if not dns_txt_proves_claim(token, records):
            return {"status": "pending", "brand_direct_set": False, "reason": "txt_not_found"}
        proof_ref = f"dns:{domain}"
    elif method == "email":
        submitted = (submitted_code or "").strip()
        # Constant-time compare; a missing/blank stored token can never match.
        if not token or not submitted or not secrets.compare_digest(submitted, token):
            return {"status": "pending", "brand_direct_set": False, "reason": "code_mismatch"}
        proof_ref = f"email:{domain}"
    else:
        # amazon / shopify / manual: not handled here (manual = employee approval).
        return {"status": "unsupported_method", "method": method}

    # Domain control proven. B1: brand_direct requires brand-identity binding —
    # the verified domain must be one Pivota already associates with this merchant.
    check = owned_domain_check or merchant_owns_domain
    try:
        bound = bool(await check(claim["merchant_id"], domain)) if domain else False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "verify_brand_claim: ownership check failed for %s: %s", domain, str(exc)[:200]
        )
        bound = False
    if not bound:
        # Domain CONTROL is proven (TXT token / emailed code matched); only
        # brand-identity BINDING is missing. That is exactly source='asserted':
        # the domain joins the merchant's official set — it is strictly stronger
        # evidence than the inference that already counts unconditionally — but
        # brand_direct stays closed pending review. Without this the proof was
        # discarded, which is why a second storefront on a different registrable
        # domain (anua.us alongside anua.com; measured 2026-09-01) could never
        # be recorded and read as third-party in the audit.
        from db import merchant_official_domains as mod

        await record_official_domain(
            claim["merchant_id"], domain, source=mod.SOURCE_ASSERTED
        )
        return {
            "status": "domain_verified_unbound",
            "brand_direct_set": False,
            "reason": "brand_domain is not associated with this merchant; needs review before brand_direct",
        }

    ok = await set_merchant_brand_direct(claim["merchant_id"])
    await bc.mark_claim_verified(claim_id, proof_ref=proof_ref)
    # B1: the domain we just proved control of joins the official set as
    # source='verified' — it is no longer only as good as what inference found.
    await record_verified_official_domain(claim["merchant_id"], domain)
    # P1: promote the verified brand's audit-seeded SKUs unclaimed -> claimed
    # (the lifecycle backbone for the syndicate-after-claim gate). Best-effort.
    from services.claim_state import promote_merchant_skus_to_claimed

    await promote_merchant_skus_to_claimed(claim["merchant_id"])
    # ADR-008 SLICE 2 — verify-to-serve: graduate this brand's brand-authored
    # products to the offer-free citation floor (index_eligible, NEUTRAL, no
    # offer). Flag-gated (ENABLE_STORELESS_BRAND_CATALOG) + best-effort.
    await _graduate_storeless_brand_catalog(claim["merchant_id"])
    return {"status": "verified", "brand_direct_set": ok}


def _default_txt_resolver(domain: str) -> List[str]:
    """Best-effort TXT lookup via dnspython if installed; [] otherwise (callers
    inject a resolver in tests, and the route can require dnspython in prod)."""
    try:
        import dns.resolver  # type: ignore

        out: List[str] = []
        for rec in dns.resolver.resolve(domain, "TXT"):
            strings = getattr(rec, "strings", None)
            if strings:
                out.append(b"".join(strings).decode("utf-8", "ignore"))
            else:
                out.append(str(rec).strip('"'))
        return out
    except Exception:  # noqa: BLE001 — missing lib / NXDOMAIN / timeout
        return []
