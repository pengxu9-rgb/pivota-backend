"""P4.3 — derive canonical evidence + findings from the legacy
brand_report shape.

Strategy: don't touch the giant agent_center_bd_report_service.py.
Instead, the audit_run_worker's verifying stage hands the completed
brand_report to this builder, which extracts evidence_items +
readiness_findings rows.

This is a shadow-write — same data, derived view persisted in
canonical form. Phase 6 will retire the legacy JSONB once consumers
migrate to the canonical tables; until then the canonical tables
are a derived index over the source of truth.

Why shadow-write (not dual-write inside build_structured_report):
  - agent_center_bd_report_service.py is 3000+ lines and touched
    by many overlapping PRs (PR-7a/b/c/d/e + PR-8a-d, etc.)
  - Adding writes inline increases merge-conflict surface and
    couples canonical-table writes to every report-shape change
  - A separate builder lets P4 evolve the canonical schema without
    re-opening the giant report builder

What this module extracts (best-effort — missing fields silently
skip):
  - grounding_chunk evidence: per_product[*].evidence_quotes
  - competitor_mention evidence: cross_product_competitors
  - url_match evidence: per_product[*].raw.merchant_store_attribution
  - merchant_visible_via_retailers_only finding: verdict label +
    attribution score mismatch
  - category_visibility_low finding: avg_category_visibility < 40
  - integration_state_incomplete finding: merchant_view tracking
    block signals
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Mapping, Optional

from observability.citation_deposit_metrics import record_deposit_dropped

logger = logging.getLogger(__name__)


def _count_depositable_observations(sku: Dict[str, Any]) -> int:
    """How many citation rows this SKU would have emitted — i.e. how many are
    dropped when its identity is unresolved. Mirrors the emit filter below
    (a row needs both a query and a provider)."""
    n = 0
    for host in (sku.get("authority_hosts") or []):
        if not isinstance(host, dict):
            continue
        for obs in (host.get("query_observations") or []):
            if isinstance(obs, dict) and obs.get("query") and obs.get("provider"):
                n += 1
    return n


# Confidence values per evidence/finding type. Pinned constants make
# it easy to audit confidence calibration in one place rather than
# spreading magic numbers through the extractor.
CONFIDENCE_EVIDENCE_HIGH = 90
CONFIDENCE_EVIDENCE_MEDIUM = 70
CONFIDENCE_EVIDENCE_LOW = 50

CONFIDENCE_FINDING_HIGH = 85
CONFIDENCE_FINDING_MEDIUM = 65


# Allowed phase values (mirrors db.audit_evidence.PHASE_*). Unknown
# values default to None — the action still inserts, just without
# a phase classification.
_VALID_PHASES = frozenset({
    "week_1_to_4", "week_4_to_12", "week_12_to_24",
})


# Lever taxonomy. Mirrors db.audit_evidence.LEVER_* but kept as a
# string set here so the extractor doesn't have to import the
# accessor module just to validate.
_VALID_LEVERS = frozenset({
    "pivota_integration", "content_creation", "pdp_optimization",
    "sitemap_hygiene", "kol_outreach", "editorial_outreach",
})


# =====================================================================
# Evidence extraction
# =====================================================================


def extract_evidence_items(
    brand_report: Dict[str, Any],
    content_key_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return a list of evidence-item dicts ready for
    insert_evidence_item. Each dict has:
      {evidence_type, payload, product_key?, confidence?}

    Order matters less than coverage — the writer iterates and
    inserts; sort order within evidence_items table is by
    created_at (set at insert time).
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    # Per-product evidence_quotes (PR-7e) → grounding_chunk evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        quotes = product.get("evidence_quotes") or []
        for q in quotes:
            if not isinstance(q, dict):
                continue
            excerpt = (q.get("excerpt_text") or "").strip()
            host = (q.get("source_host") or "").strip()
            if not excerpt:
                continue
            out.append({
                "evidence_type": "grounding_chunk",
                "payload": {
                    "host": host,
                    "source_title": q.get("source_title"),
                    "excerpt_text": excerpt[:1000],
                    "query": q.get("query"),
                    "attribution_path": q.get("attribution_path"),
                },
                "product_key": product_key,
                "confidence": (
                    CONFIDENCE_EVIDENCE_HIGH if host
                    else CONFIDENCE_EVIDENCE_MEDIUM
                ),
            })

    # Cross-product competitor mentions → competitor_mention evidence.
    # Q-P1-3: rollup entries now carry a `confidence` tier
    # (verified_competitor / grounded_competitor / possible_peer_host).
    # Map those to evidence confidence so possible_peer_host doesn't
    # drive HIGH-confidence "X is a competitor" calls in downstream
    # report prose. Back-compat: entries without a confidence field
    # fall through to MEDIUM (the prior behavior).
    competitors = brand_report.get("cross_product_competitors") or []
    _competitor_confidence_map = {
        "verified_competitor": CONFIDENCE_EVIDENCE_HIGH,
        "grounded_competitor": CONFIDENCE_EVIDENCE_MEDIUM,
        "possible_peer_host": CONFIDENCE_EVIDENCE_LOW,
    }
    for c in competitors:
        if not isinstance(c, dict):
            continue
        host = (c.get("host") or "").strip()
        if not host:
            continue
        rollup_confidence = c.get("confidence")
        out.append({
            "evidence_type": "competitor_mention",
            "payload": {
                "host": host,
                "times_cited": c.get("times_cited"),
                "source": c.get("source"),
                "rollup_confidence": rollup_confidence,
                "buyer_intent_cited": c.get("buyer_intent_cited"),
                "category_cited": c.get("category_cited"),
            },
            "product_key": None,  # brand-level
            "confidence": _competitor_confidence_map.get(
                rollup_confidence, CONFIDENCE_EVIDENCE_MEDIUM,
            ),
        })

    # Per-product merchant URL matches → url_match evidence.
    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)
        raw = product.get("raw") or {}
        attribution = raw.get("merchant_store_attribution") or {}
        # Each run that hit a URL match becomes a row.
        for run in (attribution.get("raw_runs") or []):
            if not isinstance(run, dict):
                continue
            url_match = run.get("url_match")
            if not isinstance(url_match, dict):
                continue
            if not url_match.get("matched"):
                continue
            out.append({
                "evidence_type": "url_match",
                "payload": {
                    "matched_url": url_match.get("matched_url"),
                    "matched_in": url_match.get("matched_in"),
                    "query": run.get("query"),
                },
                "product_key": product_key,
                "confidence": CONFIDENCE_EVIDENCE_HIGH,
            })

    # P0.2: stamp the canonical entity key on every evidence dict that maps to
    # a depositable (resolved) content_key. Section-agnostic final pass so new
    # evidence sections inherit it for free. Unresolved / unmapped product_keys
    # leave content_key unset — no regression, and no cross-contamination on
    # the deliberately non-unique content_key (migration 083).
    if content_key_map:
        for ev in out:
            pk = ev.get("product_key")
            resolved = content_key_map.get(pk) if pk else None
            if resolved is not None and getattr(resolved, "is_depositable", False):
                ev["content_key"] = resolved.content_key

    return out


def _clean_destination_rank(value: Any) -> Optional[int]:
    """B3: the stored position must be a non-negative int or absent. A report
    written before B3 carries no rank at all, and NULL is the honest answer for
    it — coercing a missing position to 0 would say "the answer's first
    citation", which is a claim the old payload never made.

    STRICTLY `int`, not "anything int() accepts". The only producer is
    build_authority_map's `enumerate`, so being strict drops nothing real —
    whereas a permissive coercion would turn 2.9 into position 2 and a stray
    `True` into position 1 (bool is an int subclass, hence the explicit check
    first), inventing an ordering no answer ever had."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def extract_citation_observations(
    brand_report: Dict[str, Any],
    content_key_map: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """P0.2 — flatten the authority map into per-(content_key, host, query,
    provider) citation observations. This is the cross-channel matrix that
    build_authority_map otherwise drops into report_jsonb.

    Gating: with a content_key_map, only DEPOSITABLE products emit, using the
    RESOLVED content_key + basis (so unresolved/non-unique keys never
    accrete). Without a map (pure tests), the sku-entry's own content_key is
    used with basis 'unknown'. Rows missing a query or provider are skipped
    (both are NOT NULL in citation_observations).

    B3: each observation also carries `destination_rank` (the host's zero-based
    position in that response's citation list) and `is_primary_destination`
    (services/primary_destination.py picked it as the one place the answer sent
    the buyer). AT MOST ONE row per response may claim primary, and that is
    ENFORCED HERE rather than assumed: build_authority_map guarantees it by
    construction, but this function also runs over authority maps loaded from
    stored report_jsonb written by other builds, and a second primary would make
    every "AI sends buyers to X" count double-book. A duplicate claim is demoted
    to False and logged.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out
    authority = brand_report.get("authority_map")
    if not isinstance(authority, dict):
        return out

    # Response identity within one audit run: (content_key, provider, query) —
    # the citation_observations key minus audit_run_id (constant per call) and
    # cited_host (the thing being ranked).
    primary_claimed: set = set()
    demoted_primaries = 0

    dropped_skus = 0
    dropped_observations = 0
    for sku in (authority.get("skus") or []):
        if not isinstance(sku, dict):
            continue
        product_key = sku.get("product_key")
        content_key = sku.get("content_key")
        basis = "unknown"
        if content_key_map is not None:
            resolved = content_key_map.get(product_key) if product_key else None
            if resolved is None or not getattr(resolved, "is_depositable", False):
                # Silent-fragmentation signal (ADR-008 #2): an unresolved /
                # missing identity drops this SKU's citations from deposit. Count
                # it so the drop is observable instead of invisible.
                n = _count_depositable_observations(sku)
                record_deposit_dropped(
                    basis=getattr(resolved, "basis", None) or "missing",
                    observations=n,
                )
                dropped_skus += 1
                dropped_observations += n
                continue
            content_key = resolved.content_key
            basis = resolved.basis
        if not content_key:
            continue

        for host in (sku.get("authority_hosts") or []):
            if not isinstance(host, dict):
                continue
            evidence_urls = host.get("evidence_urls") or []
            evidence_url = evidence_urls[0] if evidence_urls else None
            for obs in (host.get("query_observations") or []):
                if not isinstance(obs, dict):
                    continue
                q = obs.get("query")
                provider = obs.get("provider")
                if not q or not provider:
                    continue
                is_primary = bool(obs.get("is_primary_destination"))
                if is_primary:
                    response_key = (content_key, provider, q)
                    if response_key in primary_claimed:
                        # Second claim on the same response — demote, don't
                        # persist. Two primaries would mean one answer sent the
                        # buyer to two places, which the signal cannot express.
                        is_primary = False
                        demoted_primaries += 1
                    else:
                        primary_claimed.add(response_key)
                out.append({
                    "content_key": content_key,
                    "product_key": product_key,
                    "content_key_basis": basis,
                    "provider": provider,
                    "query": q,
                    "query_class": obs.get("query_class"),
                    "axis": obs.get("axis"),
                    "cited_host": host.get("host"),
                    "host_type": host.get("host_type"),
                    "citation_role": host.get("citation_role"),
                    "first_party": host.get("first_party"),
                    "is_competitor": host.get("is_competitor"),
                    "evidence_url": evidence_url,
                    "destination_rank": _clean_destination_rank(
                        obs.get("destination_rank")
                    ),
                    "is_primary_destination": is_primary,
                })
    if demoted_primaries:
        logger.warning(
            "citation_deposit.duplicate_primary_destination demoted=%d — an "
            "authority map claimed more than one primary destination for the "
            "same (content_key, provider, query); only the first was kept",
            demoted_primaries,
        )
    if dropped_skus:
        logger.info(
            "citation_deposit.dropped skus=%d observations=%d "
            "(unresolved identity -> citations not deposited; brand-fragmentation signal)",
            dropped_skus, dropped_observations,
        )
    return out


# =====================================================================
# Finding extraction
# =====================================================================


# Bands the rollup uses. `blocked` and `partial` are the report's own words for
# "an agent cannot resolve this" and "it resolves sometimes" — they are findings
# by the report's own reckoning, so a projection that showed nothing for them
# would be contradicting the same run's headline.
_ROLLUP_FINDING_BANDS = {"blocked": "high", "partial": "medium"}

# What each dimension means to the merchant, in the report's own language, so a
# finding does not invent a claim the rollup did not make.
_ROLLUP_DIMENSION_FINDING = {
    "identity": "product_identity_unresolvable",
    "citation": "category_citation_weak",
    "routability": "pivota_serving_not_ready",
    "content_richness": "content_too_thin_to_cite",
}

# Dimensions that measure PIVOTA'S OWN readiness, not the brand's AI
# visibility. Every one of routability's buckets reads our data and only our
# data — serving_eligibility (30) is our index pipeline state,
# offer_orderability (25) and price_currency_confidence (15) read our
# `catalog_offers` rows, and the last 10 read our merchant/verification record.
# Not one of them reads a model's answer.
#
# The writer already knows this and says so: _INTERNAL_STATE_GAPS (#1504) — a
# product deliberately held out of serving scores 0 here, and the gap was
# annotated and STOPPED FROM HEADLINING precisely so it could not be read as
# "brand isn't AI-visible". Emitting it to the merchant as high-severity
# "destination unroutable, AI has no buyable offer to send anyone to" undid
# that demotion in a new place, and filed it under GET CITED, where it reads as
# a citation failure. On the live Anuko run this dimension banded `blocked`
# with median 6 — the merchant would have read our own un-ingested catalog as
# their AI-visibility failure, and had nothing they could do about it.
#
# It is still measured, still carried in the headline distribution, and still
# visible to internal_ops. It is just not a defect the merchant is told to fix.
_PIVOTA_INTERNAL_DIMENSIONS = frozenset({"routability"})


# The headline finding's type. `low` severity on purpose: the projection's
# stage lists take critical/high/medium only, so this row carries the
# distribution to the headline WITHOUT appearing to the merchant as a problem
# of its own. It is a measurement, not a defect.
FINDING_HEADLINE_DISTRIBUTION = "brand_headline_distribution"


# Emitted when the rollup is readable but scored nothing. Distinct from
# report_shape_unreadable ("I could not read this") — this one means "I read it
# and it measured nothing". Both are meta-findings about the RUN, not claims
# about the merchant; see _META_FINDING_TYPES in audit_projection_builder.
FINDING_NO_DIMENSION_SCORED = "no_dimension_was_scored"


def _rollup_bands_seen(rollup: Dict[str, Any]) -> List[str]:
    dims = rollup.get("dimensions")
    if not isinstance(dims, dict):
        return []
    return sorted({
        str(d.get("band") or "missing")
        for d in dims.values() if isinstance(d, dict)
    })


def _rollup_scored_any_dimension(rollup: Dict[str, Any]) -> bool:
    """True when at least one dimension carries a band the writer scored."""
    dims = rollup.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        return False
    for dim in dims.values():
        if not isinstance(dim, dict):
            continue
        band = str(dim.get("band") or "").strip().lower()
        if band and band not in ("unscored", "unknown"):
            return True
    return False


def _prompt_split_from_rollup(rollup: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The branded-vs-unbranded citation split, with n, or None.

    The §6 definition of done asks the merchant surface for this split. It is
    NOT a new measurement: `prompt_mix.branded_axes` already says which intent
    axes are branded, and `citation_by_intent` already reports cited/total per
    axis. This classifies the second by the first and sums. Nothing is
    estimated, and an axis the report did not classify is named in
    `unclassified_axes` rather than being quietly counted as unbranded.

    Returns None when either input is missing — an absent split must read as
    absent, never as parity.
    """
    mix = rollup.get("prompt_mix")
    by_intent = rollup.get("citation_by_intent")
    if not isinstance(mix, dict) or not isinstance(by_intent, dict):
        return None
    branded_axes = mix.get("branded_axes")
    if not isinstance(branded_axes, list) or not branded_axes:
        return None
    branded_set = {str(a) for a in branded_axes}

    buckets = {
        "branded": {"cited": 0, "answered": 0, "axes": []},
        "unbranded": {"cited": 0, "answered": 0, "axes": []},
    }
    unclassified: List[str] = []
    for axis, stats in by_intent.items():
        if not isinstance(stats, dict):
            unclassified.append(str(axis))
            continue
        cited = stats.get("cited")
        total = stats.get("total")
        if not isinstance(cited, int) or not isinstance(total, int):
            unclassified.append(str(axis))
            continue
        side = "branded" if str(axis) in branded_set else "unbranded"
        buckets[side]["cited"] += cited
        buckets[side]["answered"] += total
        buckets[side]["axes"].append(str(axis))

    # An axis the mix calls branded but the report never scored would silently
    # shrink the branded denominator. Say it instead.
    axes_without_data = sorted(branded_set - set(buckets["branded"]["axes"]))

    for side in buckets.values():
        side["axes"] = sorted(side["axes"])
        answered = side["answered"]
        # No answered prompts means no rate. 0/0 is not 0%.
        side["rate"] = (
            round(side["cited"] / answered, 3) if answered else None
        )

    if not buckets["branded"]["axes"] and not buckets["unbranded"]["axes"]:
        return None

    return {
        "basis": (
            "citation_by_intent axes classified by prompt_mix.branded_axes; "
            "cited and answered are prompt counts, not SKU counts"
        ),
        "branded": buckets["branded"],
        "unbranded": buckets["unbranded"],
        "unclassified_axes": sorted(unclassified),
        "branded_axes_without_citation_data": axes_without_data,
        "branded_prompt_count": mix.get("branded"),
        "unbranded_prompt_count": mix.get("unbranded"),
        # Rule: scores are not comparable across a prompt_mix_version change.
        # Whoever renders or diffs this must be able to see which version it is.
        "prompt_mix_version": rollup.get("prompt_mix_version"),
    }


def _headline_finding_from_rollup(
    rollup: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Every dimension the rollup banded, plus the split, as ONE finding.

    Deliberately carries dimensions the merchant PASSED as well as the ones
    that blocked. _findings_from_brand_rollup emits a finding only for
    blocked/partial, so a headline built from those alone would show the bad
    dimensions with no denominator — the reader could not tell four-of-four
    from four-of-nine. `dimensions_considered` is that denominator.
    """
    dims = rollup.get("dimensions")
    if not isinstance(dims, dict):
        return None
    rows: List[Dict[str, Any]] = []
    for key, dim in sorted(dims.items(), key=lambda kv: str(kv[0])):
        if not isinstance(dim, dict):
            continue
        rows.append({
            "dimension": str(key),
            "label": dim.get("dimension_label") or str(key),
            "band": dim.get("band"),
            # The raw enum above is internal vocabulary; anything rendering
            # this row to a merchant must use band_label. Both travel so the
            # renderer never has to re-derive one from the other.
            "band_label": dim.get("band_label"),
            "measures": (
                "pivota_readiness"
                if str(key) in _PIVOTA_INTERNAL_DIMENSIONS
                else "brand_ai_visibility"
            ),
            "median": dim.get("median"),
            "p25": dim.get("p25"),
            "p75": dim.get("p75"),
            # n travels WITH the number. A band over 3 SKUs is not a band over
            # 300 and the reader is entitled to know which one this is.
            "n": dim.get("total_count"),
            "above_count": dim.get("above_count"),
        })
    split = _prompt_split_from_rollup(rollup)
    if not rows and split is None:
        return None
    return {
        "finding_type": FINDING_HEADLINE_DISTRIBUTION,
        # "low" so the projection's stage lists (critical/high/medium) skip it.
        "severity": "low",
        "payload": {
            "dimensions": rows,
            "dimensions_considered": len(rows),
            "prompt_split": split,
            "skus_audited": rollup.get("skus_audited"),
            "verdict_label": rollup.get("brand_verdict_label"),
        },
        "confidence": CONFIDENCE_FINDING_HIGH,
        "short_summary": (
            f"Per-dimension distribution over {len(rows)} dimension(s)"
            + ("" if split else "; no branded/unbranded split available")
        ),
    }


def _findings_from_brand_rollup(
    brand_report: Dict[str, Any], rollup: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Findings from the shape production writes.

    Deliberately conservative: it reports ONLY what the rollup already asserts —
    a dimension's own band and median — and derives nothing. Mapping these onto
    the legacy avg_visibility/avg_attribution numbers would be inventing a
    comparison the report never made, which is how the /100-score-rendered-as-a-
    percentage defect got in next door.
    """
    out: List[Dict[str, Any]] = []
    dims = rollup.get("dimensions")
    if not isinstance(dims, dict):
        return out
    for key, dim in dims.items():
        if not isinstance(dim, dict):
            continue
        band = str(dim.get("band") or "").lower()
        severity = _ROLLUP_FINDING_BANDS.get(band)
        if not severity:
            continue
        if str(key) in _PIVOTA_INTERNAL_DIMENSIONS:
            # `low` keeps it out of the projection's merchant stage lists
            # (critical/high/medium) without discarding the measurement.
            severity = "low"
        label = dim.get("dimension_label") or key
        meaning = dim.get("meaning") or ""
        # The band enum is INTERNAL vocabulary the writer forbids reaching a
        # merchant as a raw token; `band_label` ("Needs work" / "Not yet
        # visible") is the merchant-safe rendering it publishes for exactly
        # this. "Identity: blocked" was shipping the raw enum into
        # findings_summary[].summary on the merchant projection.
        band_text = dim.get("band_label") or band or "unscored"
        out.append({
            "finding_type": _ROLLUP_DIMENSION_FINDING.get(
                str(key), f"dimension_{key}_below_band",
            ),
            "severity": severity,
            "payload": {
                "dimension": key,
                "band": dim.get("band"),
                "median": dim.get("median"),
                "p25": dim.get("p25"),
                "p75": dim.get("p75"),
                # n, because a band from 3 SKUs is not a band from 300 and the
                # reader is entitled to know which one this is.
                "above_count": dim.get("above_count"),
                "total_count": dim.get("total_count"),
                "verdict_label": brand_report.get("brand_verdict_label"),
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                # For an internal-state dimension the report's own `meaning`
                # ("AI has no buyable offer to route a shopper to") describes
                # the merchant's AI visibility, which is not what the buckets
                # measured. It does not reach the merchant at `low` severity,
                # but it does reach internal_ops and the findings table, and
                # the same sentence misleads whoever reads it there.
                f"{label}: {band_text} — Pivota has not finished ingesting "
                f"and serving this store's offers. This measures our own "
                f"readiness, not the brand's AI visibility."
                if str(key) in _PIVOTA_INTERNAL_DIMENSIONS
                else f"{label}: {band_text}"
                + (f" — {meaning}" if meaning else "")
            ),
        })
    return out


FINDING_UNVERIFIED_OFFICIAL_STORE = "ai_named_an_unverified_official_store"


def _known_official_hosts(brand_report: Dict[str, Any]) -> List[str]:
    """The merchant's own hosts, as this REPORT can establish them.

    Deliberately NOT called "verified": the verified set is item 5 and lives in
    `merchant_official_domains`, which a pure function cannot read. This is the
    report's own `merchant_domain` plus the hosts its classifier already marked
    first-party. The basis is named in every finding built from it so nobody
    downstream mistakes an inference for a verification.
    """
    hosts: List[str] = []
    md = brand_report.get("merchant_domain")
    if isinstance(md, str) and md.strip():
        hosts.append(md.strip())
    authority = brand_report.get("authority_map")
    if isinstance(authority, dict):
        for sku in (authority.get("skus") or []):
            if not isinstance(sku, dict):
                continue
            for host in (sku.get("authority_hosts") or []):
                if isinstance(host, dict) and host.get("first_party") \
                        and isinstance(host.get("host"), str):
                    hosts.append(host["host"])
    return sorted(set(hosts))


def _findings_from_destination_claims(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """P0 item 8 (§14) — "AI told your buyers your official store is <host>".

    The highest-cost error an engine makes, and the one no host-frequency metric
    can see: it is a RELATIONSHIP asserted in the answer's prose, not a citation.
    Measured at 3.1% of brand-intent responses, on the single query where being
    wrong costs the most.

    Only claims pointing AWAY from the merchant's own hosts become findings. A
    claim naming the right domain is correct behaviour, and an unknown verdict
    (no host set available) is not evidence of anything — `claims_pointing_away`
    drops both.
    """
    from services.destination_claim import (
        claims_pointing_away, extract_destination_claims,
    )

    authority = brand_report.get("authority_map")
    if not isinstance(authority, dict):
        return []
    official = _known_official_hosts(brand_report)
    if not official:
        # An EARLY-OUT, not the safety net — and the comment used to claim
        # otherwise. Correctness here comes from `matches_verified`, which is
        # None (not False) when no host set was supplied, and
        # `claims_pointing_away` drops anything that is not explicitly False.
        # A mutant deleting this line changes no output, which is how the
        # over-claim was found. It stays because parsing every excerpt to
        # produce nothing is wasted work, and it goes on saying only that.
        return []

    brand = None
    facts = (brand_report.get("brand_rollup") or {}).get("run_facts")
    if isinstance(facts, dict):
        brand = (facts.get("identity") or {}).get("brand")

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for sku in (authority.get("skus") or []):
        if not isinstance(sku, dict):
            continue
        for host in (sku.get("authority_hosts") or []):
            if not isinstance(host, dict):
                continue
            claims = extract_destination_claims(
                host.get("evidence_excerpt"),
                verified_official_hosts=official,
                brand=brand,
            )
            for claim in claims_pointing_away(claims):
                key = claim["claimed_host"]
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "finding_type": FINDING_UNVERIFIED_OFFICIAL_STORE,
                    "severity": "high",
                    "payload": {
                        "claimed_host": key,
                        "excerpt": claim["excerpt"],
                        "cited_on_host": host.get("host"),
                        "brand_mentioned": claim.get("brand_mentioned"),
                        # Name the basis. These are the report's own
                        # merchant_domain + first-party hosts, NOT item 5's
                        # verified set, and a reader must be able to tell.
                        "compared_against": official,
                        "comparison_basis": "report_merchant_domain_and_first_party",
                    },
                    "confidence": CONFIDENCE_FINDING_HIGH,
                    "short_summary": (
                        f"An AI answer told shoppers your official store is "
                        f"{key}, which is not one of your known domains."
                    ),
                })
    return out


def extract_findings(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of finding dicts ready for insert_finding.
    Each dict has:
      {finding_type, payload, severity, product_key?,
       confidence?, short_summary?}
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        # A MISSING report is not an all-clear either. Returning [] put it on
        # exactly the same footing as "read the report, found nothing", which
        # is the ambiguity this function was silently trading on.
        return [{
            "finding_type": "report_shape_unreadable",
            "severity": "high",
            "payload": {"received_type": type(brand_report).__name__},
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                "No brand report was available to extract findings from. "
                "This is NOT an all-clear."
            ),
        }]

    # WHICH REPORT SHAPE IS THIS?
    #
    # Everything below reads the LEGACY shape: `aggregate` + `per_product`. The
    # per-SKU report that production actually writes has neither — its top level
    # is `brand_rollup` / `brand_verdict_label`, with the per-product detail in
    # `per_sku_reports` — which IS on the brand report
    # (agent_center_bd_report_service.py writes it there), not only on the
    # response as an earlier revision of this comment claimed. That matters:
    # the per-SKU breakdowns are readable from here, which is where a finding
    # that needs bucket-level detail would get it. So on every real run this
    # function fell
    # through every branch and returned [], and
    # build_revenue_recovery_projection turned that empty list into
    # `NO_FINDINGS` — "we checked and found nothing" — for audits that had found
    # blocked dimensions. Verified against a live run on 2026-09-05:
    # get_selected and get_cited both rendered NO_FINDINGS while the same run
    # scored the brand 1.6/10 with identity and routability BLOCKED.
    #
    # The empty list was never the bug on its own; the AMBIGUITY was. "[] because
    # nothing is wrong" and "[] because I could not read this" are different
    # facts and only one of them is an all-clear. `unreadable_shape` is appended
    # as a finding so the caller cannot mistake the second for the first, and so
    # the projection has a row to cite instead of inventing a pass.
    _legacy = ("aggregate" in brand_report) or ("per_product" in brand_report)
    _rollup = brand_report.get("brand_rollup")
    _modern = isinstance(_rollup, dict) and bool(_rollup)
    if not _legacy and not _modern:
        out.append({
            "finding_type": "report_shape_unreadable",
            "severity": "high",
            "payload": {"top_level_keys": sorted(brand_report.keys())[:20]},
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                "The findings extractor did not recognise this report's shape, "
                "so no finding could be derived from it. This is NOT an "
                "all-clear."
            ),
        })
        return out

    if _modern:
        out.extend(_findings_from_brand_rollup(brand_report, _rollup))
        _headline = _headline_finding_from_rollup(_rollup)
        if _headline is not None:
            out.append(_headline)
        # A rollup whose dimensions all came back `unscored` — or that carries
        # no `dimensions` at all — passes the _modern shape check, produces no
        # band finding, and used to leave `out` empty for the rollup lane. The
        # projection renders empty as NO_FINDINGS: "we checked and found
        # nothing". It is the SAME false all-clear this function was rewritten
        # to close, one level in. `_dimension_band(None)` returns "unscored",
        # which happens when every SKU hit missing_inputs — nothing was
        # measured, so nothing may be passed.
        if not _rollup_scored_any_dimension(_rollup):
            out.append({
                "finding_type": FINDING_NO_DIMENSION_SCORED,
                "severity": "high",
                "payload": {
                    "bands_seen": _rollup_bands_seen(_rollup),
                    "skus_audited": _rollup.get("skus_audited"),
                },
                "confidence": CONFIDENCE_FINDING_HIGH,
                "short_summary": (
                    "This audit scored none of the four dimensions, so nothing "
                    "was established about this store. This is NOT an "
                    "all-clear."
                ),
            })

    # §14 reads authority_map, which BOTH report shapes carry, so this sits
    # outside the modern/legacy split rather than inside either branch.
    out.extend(_findings_from_destination_claims(brand_report))

    aggregate = brand_report.get("aggregate") or {}
    avg_vis = aggregate.get("avg_visibility")
    avg_attr = aggregate.get("avg_attribution")
    avg_cat = aggregate.get("avg_category_visibility")
    verdict_label = aggregate.get("brand_verdict_label") or ""

    # Paradox finding — "visible via retailers" with weak attribution.
    # The hand-written Grüns report led with this; PR-8a executive
    # summary builder consumes this finding to fire the paradox
    # narrative template.
    visible_via_retailers = (
        "VISIBLE VIA RETAILERS" in str(verdict_label).upper()
    )
    weak_attribution = (
        isinstance(avg_attr, (int, float)) and avg_attr < 30
    )
    if visible_via_retailers and weak_attribution:
        out.append({
            "finding_type": "merchant_visible_via_retailers_only",
            "severity": "high",
            "payload": {
                "verdict_label": verdict_label,
                "avg_visibility": avg_vis,
                "avg_attribution": avg_attr,
                "avg_category_visibility": avg_cat,
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Brand surfaces via editorial / retailer mentions "
                f"(visibility={avg_vis}) but first-party attribution "
                f"is weak ({avg_attr})."
            ),
        })

    # Low category visibility finding.
    if isinstance(avg_cat, (int, float)) and avg_cat < 40:
        out.append({
            "finding_type": "category_visibility_low",
            "severity": "medium" if avg_cat >= 20 else "high",
            "payload": {
                "avg_category_visibility": avg_cat,
                "products_with_category_data": aggregate.get(
                    "products_succeeded",
                ),
            },
            "confidence": CONFIDENCE_FINDING_HIGH,
            "short_summary": (
                f"Category-open queries surface the brand only "
                f"{int(avg_cat)}% of the time."
            ),
        })

    # First-party PDP indexing gap — Pivota canonical URLs in use.
    # When the audit used the Pivota canonical PDP for any product,
    # that signals the merchant's own URL was unavailable / not
    # indexable. Phase 5 verifier `pdp_in_sitemap` will produce
    # paired verification evidence.
    audited_via_pivota = brand_report.get(
        "audited_via_pivota_canonical",
    ) or []
    # NOTE: that field on the brand_report itself isn't populated
    # today (it's on the audit response, not the report). Check
    # per-product url_source instead.
    pivota_used_count = 0
    for p in (brand_report.get("per_product") or []):
        if not isinstance(p, dict):
            continue
        mv = p.get("merchant_view") or {}
        headline = mv.get("headline") or {}
        if headline.get("audited_via_pivota_canonical"):
            pivota_used_count += 1
    if pivota_used_count > 0:
        out.append({
            "finding_type": "first_party_pdp_indexing_gap",
            "severity": "medium",
            "payload": {
                "products_audited_via_pivota_canonical": (
                    pivota_used_count
                ),
                "total_products": aggregate.get("products_succeeded"),
            },
            "confidence": CONFIDENCE_FINDING_MEDIUM,
            "short_summary": (
                f"{pivota_used_count} of "
                f"{aggregate.get('products_succeeded') or '?'} "
                f"products audited against Pivota canonical PDP "
                f"(merchant's own URL not available / not indexable)."
            ),
        })

    # Integration state incomplete — check the first product's
    # tracking block. Integration state is merchant-level so all
    # per_product reports carry the same value.
    per_product = brand_report.get("per_product") or []
    if per_product and isinstance(per_product[0], dict):
        mv = per_product[0].get("merchant_view") or {}
        tracking = mv.get("tracking") or {}
        integration = tracking.get("integration_state") or {}
        if isinstance(integration, dict):
            phase_0_complete = integration.get("phase_0_complete")
            if phase_0_complete is False:
                out.append({
                    "finding_type": "integration_state_incomplete",
                    "severity": "critical",
                    "payload": dict(integration),
                    "confidence": CONFIDENCE_FINDING_HIGH,
                    "short_summary": (
                        "Pivota integration incomplete — auditing "
                        "results reflect partial pipeline. Complete "
                        "onboarding to unlock the full action loop."
                    ),
                })

    return out


# =====================================================================
# Action extraction
# =====================================================================


def extract_actions(
    brand_report: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return a list of action-item dicts ready for insert_action.
    Each dict has the PR-8b shape:
      {lever, title, body, severity, owner, kpi_to_track,
       expected_outcome, phase, product_key?}

    Sources in the brand_report (priority order):
      - per_product[*].merchant_view.actions — the AUTHORITATIVE
        unified merged list (action_items + playbook_actions +
        integration_action; built by build_structured_report and
        enriched by _enrich_action_items_v2)
      - per_product[*].action_items (legacy PR-6 / P1.1 flat list —
        kept for back-compat with older fixtures / replayed audits)
      - per_product[*].action_ladder (legacy PR-8b enriched bucket)
      - per_product[*].implementation_roadmap (PR-8c phase
        containers; nested activities ARE actions)

    De-dup: if the same (lever, title) appears for multiple products
    (e.g. brand-level "complete Pivota integration"), keep only the
    first. Brand-level actions are written with product_key=None.

    Pre-fix history: extract_actions previously ONLY looked at
    per_product[*].action_items. After build_structured_report's
    merchant_view refactor, actions moved to merchant_view.actions
    and the per_product.action_items field became stale. Combined
    with _normalize_action's strict `lever`-required rule (and
    _generate_action_items not setting lever today), 100% of
    actions got dropped — Gate 5 of the deploy validation pipeline
    observed action_plan_items=0 across multiple production audits.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(brand_report, dict):
        return out

    seen: set = set()  # (lever, title) tuples we've already emitted

    def _emit(action: Any, product_key: Optional[str]) -> None:
        normalized = _normalize_action(action, product_key)
        if normalized is None:
            return
        dedupe_key = (normalized["lever"], normalized["title"])
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        out.append(normalized)

    for product in (brand_report.get("per_product") or []):
        if not isinstance(product, dict):
            continue
        product_key = _product_key_from_report(product)

        # PRIMARY source: per_product[*].merchant_view.actions
        # build_structured_report puts the unified merged list here
        # (see agent_center_bd_report_service.py:3840 — "actions":
        # merged_actions in _build_merchant_view's return value).
        merchant_view = product.get("merchant_view") or {}
        if isinstance(merchant_view, dict):
            for action in (merchant_view.get("actions") or []):
                _emit(action, product_key)

        # Legacy back-compat path 1: per_product[*].action_items.
        # Modern builders don't emit at this path but test fixtures
        # and older audits may still have it populated. Kept so the
        # extractor doesn't regress for any existing audit replay.
        for action in (product.get("action_items") or []):
            _emit(action, product_key)

        # Legacy back-compat path 2: per_product[*].action_ladder.
        # Nested under `actions` (or sometimes the top-level value
        # IS the list).
        ladder = product.get("action_ladder") or {}
        if isinstance(ladder, dict):
            ladder_actions = ladder.get("actions") or []
        elif isinstance(ladder, list):
            ladder_actions = ladder
        else:
            ladder_actions = []
        for action in ladder_actions:
            _emit(action, product_key)

        # implementation_roadmap: PR-8c phase containers. Each
        # phase has an `activities` list; each activity is an
        # action with the phase pre-set.
        roadmap = product.get("implementation_roadmap") or {}
        if isinstance(roadmap, dict):
            phases = roadmap.get("phases") or []
        else:
            phases = []
        for phase_block in phases:
            if not isinstance(phase_block, dict):
                continue
            phase_id = phase_block.get("phase_id") or phase_block.get("label")
            for activity in (phase_block.get("activities") or []):
                # activities can be plain strings or action dicts;
                # only dicts are normalizable.
                if not isinstance(activity, dict):
                    continue
                # Inject phase from the roadmap container if the
                # activity itself didn't specify one. Copy first so
                # we don't mutate the brand_report.
                activity_copy = dict(activity)
                if (
                    not activity_copy.get("phase")
                    and isinstance(phase_id, str)
                    and phase_id in _VALID_PHASES
                ):
                    activity_copy["phase"] = phase_id
                _emit(activity_copy, product_key)

    return out


# Fallback lever assigned when an action source produces an item
# without one. The lever column is a categorization tag; missing
# levers should NOT cause the whole action to be dropped (which is
# what the prior strict-required rule did, costing us every
# severity+title-only action that _generate_action_items emits).
_DEFAULT_LEVER = "general_recommendation"


def _derive_lever_from_title(title: str) -> str:
    """Best-effort lever inference from action title keywords. Used
    as a fallback when an action dict has no explicit lever field
    (typical for _generate_action_items output, which is
    severity+title+body+evidence shaped without a categorization
    tag). Keeps action_plan_items.lever meaningful for grouping
    without forcing every caller to remember to set it."""
    if not title:
        return _DEFAULT_LEVER
    title_lower = title.lower()
    # Most specific signals first.
    if (
        "search console" in title_lower
        or "indexing" in title_lower
        or "index your" in title_lower  # matches "Index your canonical PDPs"
        or "url inspection" in title_lower
        or "sitemap" in title_lower
        or "canonical pdp" in title_lower
    ):
        return "indexing_acceleration"
    if (
        "editorial" in title_lower
        or "publisher" in title_lower
        or "outreach" in title_lower
        or "pitch" in title_lower
    ):
        return "editorial_outreach"
    if (
        "content" in title_lower
        or "brief" in title_lower
        or "article" in title_lower
    ):
        return "content_publishing"
    if (
        "competitor" in title_lower
        or "cohort" in title_lower
        or "competitive" in title_lower
    ):
        return "competitive_response"
    if (
        "integration" in title_lower
        or "onboard" in title_lower
        or "pivota" in title_lower
    ):
        return "pivota_integration"
    return _DEFAULT_LEVER


def _normalize_action(
    raw: Any, product_key: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Normalize a raw action dict (from various report shapes) to
    the insert_action contract. Returns None ONLY if the action
    lacks a title — every action must be human-presentable. Missing
    lever is tolerated (derived from title keywords) so the
    severity+title-only shape produced by _generate_action_items
    persists instead of being silently dropped.

    Pre-fix this required (lever AND title); the strict rule meant
    every _generate_action_items output was rejected, since that
    function emits severity+title+body+evidence shaped dicts with
    no `lever` field set. Gate 5 of deploy validation observed
    action_plan_items=0 across multiple production audits because
    of this.
    """
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    lever = (raw.get("lever") or "").strip() or _derive_lever_from_title(title)

    # Phase validation — only emit phases the canonical taxonomy
    # accepts. PR-8c emits these labels directly so this is mostly
    # a passthrough.
    phase = raw.get("phase")
    if phase and phase not in _VALID_PHASES:
        phase = None

    # owner / kpi / outcome are optional PR-8b fields; pass through
    # what's present, leave others None.
    return {
        "lever": lever,
        "title": title,
        "body": (raw.get("body") or "").strip() or None,
        "severity": raw.get("severity"),
        "owner": raw.get("owner"),
        "kpi_to_track": raw.get("kpi_to_track"),
        "expected_outcome": raw.get("expected_outcome"),
        "phase": phase,
        "product_key": product_key,
    }


# =====================================================================
# Persist — calls the P4.2 accessors
# =====================================================================


async def _resolve_content_keys(
    brand_report: Dict[str, Any],
    merchant_id: Optional[str],
) -> Dict[str, Any]:
    """Build {product_key: ResolvedDepositKey} for the audited products.

    P0.2 gate source: a content_key may receive entity-scoped deposits only
    when it is RESOLVED (GTIN-backed / high-confidence identity / reviewed).
    The canonical GTIN lives on agent_pdp_view.gtin13 — catalog_products has
    no GTIN column — so we read brand/title/content_key from catalog_products
    and the GTIN from the serving view, then let
    services.catalog_identity.resolve_deposit_content_key decide the basis.

    Conservative by design: products not yet in the serving view (no gtin13)
    resolve as 'unresolved' and simply don't stamp — surfaced via the coverage
    counts, never a cross-contamination risk on the deliberately non-unique
    content_key (migration 083).

    Best-effort: any failure returns {} so the deposit proceeds unstamped
    rather than failing the audit lifecycle.
    """
    if not merchant_id or not isinstance(brand_report, dict):
        return {}
    try:
        from db.database import database
        from db.catalog import agent_pdp_view, catalog_products
        from services.catalog_identity import resolve_deposit_content_key
    except Exception:  # noqa: BLE001
        return {}

    product_keys: List[str] = []
    seen = set()

    def _add(pk: Optional[str]) -> None:
        if pk and pk not in seen:
            seen.add(pk)
            product_keys.append(pk)

    for product in (brand_report.get("per_product") or []):
        if isinstance(product, dict):
            _add(_product_key_from_report(product))
    # P0.2 fix: per_product entries don't reliably carry a product_key (in prod
    # they never do — _product_key_from_report returns None), so resolving only
    # from per_product yields an empty map and NOTHING ever deposits. The
    # authority_map SKUs DO carry product_key — and are exactly the set
    # extract_citation_observations gates on — so resolve those too. Union keeps
    # evidence-item stamping (per_product) working while unblocking the citation
    # matrix deposit.
    authority = brand_report.get("authority_map")
    if isinstance(authority, dict):
        for sku in (authority.get("skus") or []):
            if isinstance(sku, dict):
                _add(sku.get("product_key"))
    if not product_keys:
        return {}

    try:
        rows = await database.fetch_all(
            catalog_products.select().where(
                catalog_products.c.merchant_id == merchant_id,
                catalog_products.c.product_key.in_(product_keys),
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_resolve_content_keys: catalog_products load failed: %s",
            str(exc)[:200],
        )
        return {}
    rows = rows or []

    # GTIN from the serving view, keyed by content_key (best-effort; on failure
    # we proceed with no GTIN, so products resolve 'unresolved' = safe).
    content_keys = [r["content_key"] for r in rows if r["content_key"]]
    gtin_by_ck: Dict[str, Any] = {}
    if content_keys:
        try:
            view_rows = await database.fetch_all(
                agent_pdp_view.select().where(
                    agent_pdp_view.c.content_key.in_(content_keys)
                )
            )
            for vr in view_rows or []:
                gtin_by_ck[vr["content_key"]] = vr["gtin13"]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "_resolve_content_keys: agent_pdp_view load failed: %s",
                str(exc)[:200],
            )

    # P0.2 fix #2: identity-graph confidence from catalog_row_trust (migration
    # 136). The deposit gate authorizes on gtin | identity_high_conf | reviewed,
    # but GTIN coverage is ~0% for the Shopify merchant base, so without this the
    # gate NEVER opens. The Node identity graph already resolved these listings to
    # their content_key with a stored confidence — pass it so high-confidence
    # products deposit on the identity_high_conf basis (threshold from
    # deposit_min_confidence(), default 0.85). MAX() dedupes any duplicate trust
    # rows per product_key. Best-effort: a miss leaves conf None = GTIN-only
    # behavior, no regression.
    conf_by_pk: Dict[str, Any] = {}
    try:
        trust_rows = await database.fetch_all(
            "SELECT product_key, MAX(identity_confidence) AS identity_confidence "
            "FROM catalog_row_trust "
            "WHERE product_key = ANY(:pks) AND identity_confidence IS NOT NULL "
            "GROUP BY product_key",
            {"pks": product_keys},
        )
        for tr in trust_rows or []:
            conf_by_pk[tr["product_key"]] = tr["identity_confidence"]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_resolve_content_keys: catalog_row_trust load failed: %s",
            str(exc)[:200],
        )

    out: Dict[str, Any] = {}
    for r in rows:
        ck = r["content_key"]
        raw_conf = conf_by_pk.get(r["product_key"])
        out[r["product_key"]] = resolve_deposit_content_key(
            brand=r["brand"],
            title=r["title"],
            gtin=gtin_by_ck.get(ck) if ck else None,
            existing_content_key=ck,
            identity_confidence=(
                float(raw_conf) if raw_conf is not None else None
            ),
        )
    return out


# =====================================================================
# A3 — the run-level audit basis
# =====================================================================


def _per_sku_reports(brand_report: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    reports = brand_report.get("per_sku_reports")
    if not isinstance(reports, list):
        return []
    return [r for r in reports if isinstance(r, Mapping)]


def build_providers_and_models(
    brand_report: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """A3: `{provider_id: {"model_id": str, "temperature": float|None}}`.

    Read from the report's own `provider_models` block (written by
    services/coverage_profiles.resolve_provider_models plus any per-run
    override), so the recorded model is the one the run actually used rather
    than whatever the config says at read time.

    TEMPERATURE IS RECORDED AS NULL, and that is a fact rather than a gap: the
    audit probe path pins a temperature in exactly one place
    (services/llm_providers/deepseek_probe.py, 0.2, inside the request body) and
    passes none for the other providers, which therefore run at whatever the
    provider's default is. Inventing a number here would put a value in an
    immutable record that no code ever set. Recording null means that if the
    probe path ever starts pinning temperatures, the basis changes and
    `bases_are_comparable` correctly refuses to compare across the change.
    """
    raw = brand_report.get("provider_models")
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(raw, Mapping):
        return out
    for provider, payload in raw.items():
        provider_id = str(provider or "").strip().lower()
        if not provider_id:
            continue
        if isinstance(payload, Mapping):
            model_id = str(payload.get("model") or payload.get("model_id") or "").strip()
        else:
            model_id = str(payload or "").strip()
        if not model_id:
            continue
        out[provider_id] = {"model_id": model_id, "temperature": None}
    return out


def build_tier_mix(brand_report: Mapping[str, Any]) -> Dict[str, int]:
    """A3: counts per QUERY CLASS over the questions the run actually probed.

    The vocabulary is `services.audit_facts.intent_axis_for` — the CURRENT one,
    imported rather than restated, so this can never drift into a parallel
    taxonomy. It reads the pinned `selected_specs` (W2.1: the exact
    `{query, axis}` records that were probed), which is the only record of the
    mix that survives into the report.

    Why the mix matters on top of `selected_set_id`: two runs can carry the same
    prompt-set identity and still be measured differently if the branded /
    discovery balance moved (PROMPT_BASIS_VERSION 3 rebalanced exactly that),
    and every share-style number in the report is a function of that balance.
    """
    from services.audit_facts import intent_axis_for

    counts: Counter = Counter()
    for sku_report in _per_sku_reports(brand_report):
        basis = sku_report.get("prompt_basis")
        if not isinstance(basis, Mapping):
            continue
        for spec in basis.get("selected_specs") or []:
            if not isinstance(spec, Mapping):
                continue
            query = spec.get("query")
            if not query:
                continue
            counts[intent_axis_for(query, spec.get("axis"))] += 1
    return dict(sorted(counts.items()))


def _pinned_set_ids(brand_report: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """The first non-empty `prompt_set_id` / `selected_set_id` across the run's
    per-SKU bases. Each is taken independently: a run whose SKUs predate W2.1
    has a prompt_set_id and no selected_set_id, and recording the one it has is
    strictly better than recording neither."""
    prompt_set_id: Optional[str] = None
    selected_set_id: Optional[str] = None
    for sku_report in _per_sku_reports(brand_report):
        basis = sku_report.get("prompt_basis")
        if not isinstance(basis, Mapping):
            continue
        if not prompt_set_id:
            prompt_set_id = str(basis.get("prompt_set_id") or "") or None
        if not selected_set_id:
            selected_set_id = str(basis.get("selected_set_id") or "") or None
        if prompt_set_id and selected_set_id:
            break
    return {"prompt_set_id": prompt_set_id, "selected_set_id": selected_set_id}


async def record_audit_basis(
    *,
    audit_run_id: str,
    brand_report: Mapping[str, Any],
    merchant_id: Optional[str],
    persist: bool = True,
) -> Optional[Dict[str, Any]]:
    """A3: record, immutably, what this run was measured WITH.

    With ``persist=False`` this builds and returns the basis PAYLOAD without
    writing it — what services/audit_delta needs for the CURRENT run, whose row
    does not exist yet when the delta is attached.

    Called at audit completion, from `persist_canonical_evidence` — the one
    place that already receives the assembled report, the run id and the
    merchant id together. Best-effort in the same way every other write here is:
    a failure logs and returns None, and never touches the audit lifecycle.

    A second call for the same run is a no-op that returns the stored row
    (db/audit_basis.record_basis), so a worker reclaim after a crash re-enters
    this path safely.
    """
    # audit_run_id is only needed to WRITE. The comparability path builds the
    # current run's basis before that run has an id to write under, and passes
    # "" deliberately — gating on it there made this whole feature inert.
    if not merchant_id or (persist and not audit_run_id):
        return None
    from db.audit_basis import METHODOLOGY_VERSION, record_basis
    from db.merchant_official_domains import is_excluded, list_official_domains
    from services.primary_destination import PRIMARY_DESTINATION_VERSION

    try:
        # The official-domain set AS IT STOOD AT RUN TIME. Snapshotting it is
        # the whole point: this set decides `first_party` on every cited host,
        # so a domain added between two runs moves the headline number with no
        # change in the world. `dead` rows are excluded here for the same reason
        # the report excludes them — the set recorded must be the set used.
        domains = [
            row.get("domain")
            for row in (await list_official_domains(str(merchant_id)))
            if row.get("domain") and not is_excluded(row.get("liveness_status"))
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "record_audit_basis: official-domain snapshot failed for %s: %s",
            merchant_id, str(exc)[:200],
        )
        domains = []

    set_ids = _pinned_set_ids(brand_report)
    # Market/language come from the audit's single-market default
    # (config.settings.audit_default_market_locale, e.g. "en-US"); the multi-
    # market path (services/multi_market_audit.py) is flag-off in production.
    # CURRENCY IS RECORDED AS NULL: the audit measures citations, not prices, and
    # no currency is pinned anywhere on this path. A guessed "USD" in an
    # immutable record would be a fabrication.
    market: Optional[str] = None
    language: Optional[str] = None
    try:
        from config.settings import settings

        locale = str(getattr(settings, "audit_default_market_locale", "") or "").strip()
        if "-" in locale:
            language, market = locale.split("-", 1)
        elif locale:
            language = locale
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("record_audit_basis: locale read failed: %s", str(exc)[:200])

    # Normalise EXACTLY as db.audit_basis.record_basis does before storing.
    # It writes sorted({lower(strip(d))}); this path returned the raw
    # list_official_domains order, which has no ORDER BY. Comparing the two
    # shapes made a run non-comparable with ITSELF, so every multi-domain
    # merchant would have been told their basis changed on every re-audit —
    # the same defect as the one being fixed, pointing the other way.
    domains = sorted({str(d).strip().lower() for d in domains if str(d).strip()})

    payload = {
        "providers_and_models": build_providers_and_models(brand_report),
        "prompt_set_id": set_ids["prompt_set_id"],
        "selected_set_id": set_ids["selected_set_id"],
        "tier_mix": build_tier_mix(brand_report),
        "official_domains": domains,
        "primary_destination_version": PRIMARY_DESTINATION_VERSION,
        "market": market,
        "language": language,
        "currency": None,
    }
    if not persist:
        # The comparability path wants the SHAPE, not a row: at delta-attach
        # time this run's basis has not been written yet (persist_canonical_
        # evidence runs later, from the worker), so reading it back would always
        # return None and the check would be permanently inert.
        payload["methodology_version"] = METHODOLOGY_VERSION
        return payload

    return await record_basis(
        audit_run_id=str(audit_run_id),
        merchant_id=str(merchant_id),
        **payload,
    )



async def _deposit_citation_observations(
    *,
    brand_report: Any,
    content_key_map: Optional[Dict[str, Any]],
    audit_run_id: str,
    merchant_id: Optional[str],
    summary: Dict[str, Any],
) -> None:
    """Deposit the cross-channel citation matrix, content_key-keyed.

    Extracted from persist_canonical_evidence so the hand-off into
    insert_citation_observation is reachable by a test. It was not: mutants
    forcing `destination_rank=None` or `is_primary_destination=False` at this
    boundary survived the whole suite, which meant the two columns B3 exists to
    produce could be zeroed here and ship green.

    Best-effort and idempotent; only depositable products emit (gated inside
    extract_citation_observations via content_key_map).
    """
    # Imported here, not at module scope: this module is imported during report
    # assembly and db.audit_evidence pulls in the DB layer.
    from db.audit_evidence import (
        compute_canonical_idempotency_key,
        insert_citation_observation,
    )

    summary["citation_observations_inserted"] = 0
    summary["citation_observations_skipped"] = 0
    for obs in extract_citation_observations(brand_report, content_key_map):
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="citation_observation",
            item_signature="{}|{}|{}|{}".format(
                obs.get("content_key"), obs.get("provider"),
                obs.get("query"), obs.get("cited_host"),
            ),
        )
        new_obs_id = await insert_citation_observation(
            audit_run_id=audit_run_id,
            merchant_id=merchant_id,
            content_key=obs["content_key"],
            product_key=obs.get("product_key"),
            provider=obs["provider"],
            query=obs["query"],
            axis=obs.get("axis"),
            query_class=obs.get("query_class"),
            cited_host=obs.get("cited_host"),
            host_type=obs.get("host_type"),
            citation_role=obs.get("citation_role"),
            first_party=obs.get("first_party"),
            is_competitor=obs.get("is_competitor"),
            evidence_url=obs.get("evidence_url"),
            content_key_basis=obs.get("content_key_basis") or "unknown",
            destination_rank=obs.get("destination_rank"),
            is_primary_destination=obs.get("is_primary_destination"),
            idempotency_key=idem_key,
        )
        if new_obs_id is None:
            summary["citation_observations_skipped"] += 1
        else:
            summary["citation_observations_inserted"] += 1


async def persist_canonical_evidence(
    *,
    audit_run_id: str,
    brand_report: Dict[str, Any],
    merchant_id: Optional[str] = None,
) -> Dict[str, int]:
    """Extract + persist evidence_items + readiness_findings +
    action_plan_items for one brand_report.

    P5.8.2 idempotency: each insert is keyed by a deterministic
    sha256(audit_run_id|item_type|item_signature). On worker
    crash + reclaim, re-running persist hits the partial unique
    index on (audit_run_id, idempotency_key) → ON CONFLICT skips
    instead of doubling rows. Idempotent-skips are counted in
    `*_deduped` so the summary distinguishes them from genuine
    persistence failures.

    P5.8.1 tenancy: merchant_id is plumbed through every accessor
    call so the canonical tables carry merchant scope at the
    column level.

    Best-effort: persistence failures don't fail the audit
    lifecycle. Returns a count summary for the worker's
    partial_result tracking.
    """
    from db.audit_evidence import (
        compute_canonical_idempotency_key,
        insert_action, insert_citation_observation,
        insert_evidence_item, insert_finding,
    )

    summary = {
        "evidence_items_inserted": 0,
        "evidence_items_deduped": 0,  # P5.8.2
        "evidence_items_failed": 0,
        "findings_inserted": 0,
        "findings_deduped": 0,
        "findings_failed": 0,
        "actions_inserted": 0,
        "actions_deduped": 0,
        "actions_failed": 0,
    }

    # Pre-flight: count what we'll attempt to insert. Lets us
    # distinguish "ON CONFLICT skipped a row that exists" (deduped)
    # from "INSERT raised an unrelated error" (failed). With the
    # accessor returning None for both cases, we re-query after
    # each loop to attribute the None correctly.

    # P0.2: resolve content_key per audited product (the gate source) and
    # stamp the depositable ones. Best-effort — an empty map stamps nothing.
    content_key_map = await _resolve_content_keys(brand_report, merchant_id)
    summary["content_keys_total"] = len(content_key_map)
    summary["content_keys_depositable"] = sum(
        1 for r in content_key_map.values()
        if getattr(r, "is_depositable", False)
    )

    # P0.2 W3: record the resolved canonical entities + basis on the run row.
    try:
        from db.merchant_audit_runs import record_audit_run_content_keys
        depositable_cks = sorted({
            r.content_key for r in content_key_map.values()
            if getattr(r, "is_depositable", False) and r.content_key
        })
        await record_audit_run_content_keys(
            run_id=audit_run_id,
            content_keys=depositable_cks,
            content_key_basis={pk: r.basis for pk, r in content_key_map.items()},
        )
        summary["content_keys"] = depositable_cks
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "persist_canonical_evidence: content_keys write failed for "
            "audit=%s: %s", audit_run_id, str(exc)[:200],
        )

    extracted_evidence = list(
        extract_evidence_items(brand_report, content_key_map)
    )
    for ev in extracted_evidence:
        signature = _evidence_signature(ev)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="evidence",
            item_signature=signature,
        )
        try:
            new_id = await insert_evidence_item(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                evidence_type=ev["evidence_type"],
                payload=ev["payload"],
                product_key=ev.get("product_key"),
                content_key=ev.get("content_key"),
                confidence=ev.get("confidence"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "persist_canonical_evidence: insert_evidence_item "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            # Attribute via re-lookup: if the row exists with our
            # idempotency_key it's a dedupe-skip, else a real failure.
            existed = await _evidence_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["evidence_items_deduped"] += 1
            else:
                summary["evidence_items_failed"] += 1
        else:
            summary["evidence_items_inserted"] += 1

    for finding in extract_findings(brand_report):
        signature = _finding_signature(finding)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="finding",
            item_signature=signature,
        )
        try:
            new_id = await insert_finding(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                finding_type=finding["finding_type"],
                payload=finding["payload"],
                severity=finding.get("severity"),
                product_key=finding.get("product_key"),
                confidence=finding.get("confidence"),
                short_summary=finding.get("short_summary"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persist_canonical_evidence: insert_finding "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            existed = await _finding_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["findings_deduped"] += 1
            else:
                summary["findings_failed"] += 1
        else:
            summary["findings_inserted"] += 1

    # P4.4: action_plan_items dual-write. Same best-effort pattern.
    for action in extract_actions(brand_report):
        signature = _action_signature(action)
        idem_key = compute_canonical_idempotency_key(
            audit_run_id=audit_run_id,
            item_type="action",
            item_signature=signature,
        )
        try:
            new_id = await insert_action(
                audit_run_id=audit_run_id,
                merchant_id=merchant_id,
                lever=action["lever"],
                title=action["title"],
                body=action.get("body"),
                severity=action.get("severity"),
                owner=action.get("owner"),
                kpi_to_track=action.get("kpi_to_track"),
                expected_outcome=action.get("expected_outcome"),
                phase=action.get("phase"),
                product_key=action.get("product_key"),
                idempotency_key=idem_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "persist_canonical_evidence: insert_action "
                "raised for audit=%s: %s",
                audit_run_id, str(exc)[:200],
            )
            new_id = None
        if new_id is None:
            existed = await _action_exists_by_idem(
                audit_run_id=audit_run_id, idempotency_key=idem_key,
            )
            if existed:
                summary["actions_deduped"] += 1
            else:
                summary["actions_failed"] += 1
        else:
            summary["actions_inserted"] += 1

    # P0.2: citation_observations — the cross-channel matrix, content_key-keyed.
    # Best-effort, idempotent; only depositable products emit (gated inside
    # extract_citation_observations via content_key_map).
    await _deposit_citation_observations(
        brand_report=brand_report,
        content_key_map=content_key_map,
        audit_run_id=audit_run_id,
        merchant_id=merchant_id,
        summary=summary,
    )

    # A3: record what this run was measured WITH, once and immutably. Placed
    # AFTER the citation deposit so the basis describes a run whose evidence has
    # landed; a failure here is logged inside record_audit_basis and only shows
    # up as basis_recorded=False.
    try:
        basis_row = await record_audit_basis(
            audit_run_id=audit_run_id,
            brand_report=brand_report,
            merchant_id=merchant_id,
        )
    except Exception as exc:  # noqa: BLE001 — defensive; must not fail the audit
        logger.warning(
            "persist_canonical_evidence: record_audit_basis raised for "
            "audit=%s: %s", audit_run_id, str(exc)[:200],
        )
        basis_row = None
    summary["basis_recorded"] = basis_row is not None

    return summary


# =====================================================================
# P5.8.2 — idempotency-signature + exists-by-idem helpers
# =====================================================================


def _evidence_signature(ev: Dict[str, Any]) -> str:
    """Deterministic substring distinguishing one evidence item
    from another for the same audit. Stable across worker re-runs
    because brand_report → extract_evidence_items is deterministic.

    Two evidence items collapse iff they would represent the same
    canonical truth (same type + product + host + excerpt prefix).
    """
    payload = ev.get("payload") or {}
    excerpt = (payload.get("excerpt_text") or "")[:80]
    host = payload.get("host") or ""
    matched_url = payload.get("matched_url") or ""
    return "|".join([
        str(ev.get("evidence_type") or ""),
        str(ev.get("product_key") or ""),
        host, excerpt, matched_url,
    ])


def _finding_signature(finding: Dict[str, Any]) -> str:
    """One finding per (type, product) within an audit. Re-running
    extract_findings on the same brand_report produces the same
    signature → ON CONFLICT skip."""
    return "|".join([
        str(finding.get("finding_type") or ""),
        str(finding.get("product_key") or ""),
    ])


def _action_signature(action: Dict[str, Any]) -> str:
    """One action per (lever, title, product). Title is truncated
    to 80 chars so minor prose churn from Gemini between re-runs
    doesn't produce a fresh signature."""
    return "|".join([
        str(action.get("lever") or ""),
        str(action.get("product_key") or ""),
        (action.get("title") or "")[:80],
    ])


async def _evidence_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    """Lookup helper after a None return from insert_evidence_item.
    Distinguishes "ON CONFLICT (existing row) → idempotent skip"
    from "INSERT raised an unrelated error → real failure"."""
    from db.audit_evidence import evidence_items
    from db.database import database
    try:
        row = await database.fetch_one(
            evidence_items.select()
            .where(
                evidence_items.c.audit_run_id == audit_run_id,
                evidence_items.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


async def _finding_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    from db.audit_evidence import readiness_findings
    from db.database import database
    try:
        row = await database.fetch_one(
            readiness_findings.select()
            .where(
                readiness_findings.c.audit_run_id == audit_run_id,
                readiness_findings.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


async def _action_exists_by_idem(
    *, audit_run_id: str, idempotency_key: str,
) -> bool:
    from db.audit_evidence import action_plan_items
    from db.database import database
    try:
        row = await database.fetch_one(
            action_plan_items.select()
            .where(
                action_plan_items.c.audit_run_id == audit_run_id,
                action_plan_items.c.idempotency_key == idempotency_key,
            )
            .limit(1)
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return False


# =====================================================================
# Internal helpers
# =====================================================================


def _product_key_from_report(
    product_report: Dict[str, Any],
) -> Optional[str]:
    """Extract the product_key from a per-product report.
    Different shapes exist across the various PRs that touched
    build_structured_report; check all known locations."""
    # Most reliable: explicit product_key field if it exists
    pk = product_report.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    # Fall back to (platform, source_product_id) tuple if present
    platform = product_report.get("platform")
    source_id = product_report.get("source_product_id")
    if platform and source_id:
        return f"{platform}::{source_id}"
    # Fall back to merchant_view.headline.product_key
    mv = product_report.get("merchant_view") or {}
    headline = mv.get("headline") or {}
    pk = headline.get("product_key")
    if isinstance(pk, str) and pk:
        return pk
    return None
