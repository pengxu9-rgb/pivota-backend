"""Registry-growth loop: propose `data/cited_host_registry.json` entries for the
cited hosts our audits keep hitting that `classify_host` still can't classify.

WHY THIS SCRIPT EXISTS
----------------------
Every merchant audit records the hosts Gemini/DeepSeek grounded on
(`merchant_audit_runs.report_jsonb.authority_map.hosts[]`). `classify_host`
(services/cited_host_classifier) turns a host into
type/subtype/tier/categories — but only for hosts a human has curated into
`data/cited_host_registry.json`. Everything else comes back
`type="unclassified"`, and unclassified is not a neutral state:

  - the action ladder can't rank it, so it renders as a bare hostname;
  - `merchant_narrative_builder` skips `type=brand` hosts from outreach
    ("never pitch a rival's site"), so an UNREGISTERED competitor storefront
    gets pitched to the merchant as a place to get cited. That is exactly the
    kerastase-usa.com leak (run 83e8fcb4-5cd8-45a1-9067-f46b86a56336).

Measured in prod 2026-07-14: of the top 120 cited hosts by cross-merchant
recurrence, 88 are unclassified — including sokoglam.com, dermstore.com,
jolse.com, kroger.com, jcpenney.com and rival storefronts (cosrx.com,
mojawa.com). This script turns that long tail into a ranked, reviewable queue.

SOURCE OF TRUTH: why report_jsonb and not citation_observations
---------------------------------------------------------------
`citation_observations` (migration 157, services/host_recurrence.py) is the
*intended* demand signal, but it only persists for DEPOSITABLE products —
URL-wedge audits never land there, so in prod it holds just 695 rows across 3
merchants. `merchant_audit_runs.report_jsonb` has 174 succeeded runs across 11
merchants. We read the reports. (Making the wedge deposit observations is a
separate deposit-leg design decision — deliberately NOT done here.)

WHAT IT DOES
------------
1. Reads every `merchant_audit_runs` row with status='succeeded' (the status
   vocab is 'succeeded' — never 'completed') and a non-null report_jsonb, or an
   offline dump via --input-json.
2. Extracts `authority_map.hosts[]`, falling back to the per-SKU authority
   hosts (`authority_map.skus[].authority_hosts[]` /
   `per_sku_reports[].authority_hosts[]`) when the top-level map is absent.
3. Aggregates per host: distinct merchants, total prompts_cited_count, citation
   roles, first_party / is_competitor flags, evidence URLs. Drops first_party
   rows (the merchant's own domains are not registry material).
4. Keeps only hosts where `classify_host` returns type='unclassified'.
5. Ranks by (distinct_merchants desc, citations desc) and writes
   `reports/registry_proposals_<date>.json` with a PROPOSED entry per host.

Competitor storefronts (the kerastase-usa.com class) get an explicit
`type=brand` proposal: a host whose registrable label matches a
`competitors_named` entry from the SAME runs, compared with diacritics folded
and separators removed ("Kérastase" -> "kerastase" matches "kerastase-usa.com").
Registering rivals as `type=brand` is what makes the narrative builder's
never-pitch-a-rival skip work systematically instead of case by case.

HUMAN REVIEW IS MANDATORY
-------------------------
The script NEVER writes `data/cited_host_registry.json`. Every entry carries
`proposed_by` (heuristic rule id or llm:<provider>) and `confidence`; entries
the proposer can't type get `type: null` and must be filled in by a human.
Approved entries land via a PR that edits the registry by hand.

Where the proposer is known to be weak — check these hardest:
  - A MULTI-BRAND RETAILER the engines named as a "competitor" and that only one
    merchant's audit cited (sokoglam.com, jolse.com) is proposed `brand`. Typing
    a retailer `brand` deletes a real channel from the merchant's playbook, so
    flip it to `retailer` on review. (Retailers cited by 2+ merchants are caught
    — they come through as `competitor_name_match_ambiguous` with type=null —
    and ones on the curated non-brand list are caught outright.)
  - Every `type: null` entry is the proposer saying "I don't know", not "this is
    unimportant". cosrx.com sits there with 85 citations.
The loop self-corrects: once a host is in the registry, `classify_host` types it
and the next sweep stops proposing it. A shrinking queue is the loop working.

OPERATOR CADENCE
----------------
Run after each audit batch, or weekly if audits trickle in:

  cd /Users/pengchydan/dev/pivota-backend
  railway run -- .venv/bin/python scripts/sweep_unclassified_cited_hosts.py \
      --database-url "$DATABASE_PUBLIC_URL?sslmode=require" \
      --min-merchants 2

  Connection notes (verified 2026-07-14 — this is the part that wastes an hour
  if you get it wrong):
    * Read over the PUBLIC proxy DSN, with `?sslmode=require`. asyncpg parses the
      DSN with libpq semantics, so `require` encrypts the connection without
      demanding a verifiable CA chain — which the proxy's self-signed chain
      cannot provide.
    * The script connects with asyncpg directly rather than through
      `db.database`. That pool turns `sslmode=require` into FULL certificate
      verification, which the proxy fails — and it fails by hanging on pool
      creation rather than raising, so it looks like a slow query for ten
      minutes before timing out.
    * Never "fix" a certificate error by turning TLS off or disabling
      verification. If the DSN above stops working, get the CA — don't downgrade.
    * Always .venv/bin/python (3.14, repo deps).

Then: review reports/registry_proposals_<date>.json, keep what's right, fix the
types the proposer got wrong, and open a PR editing data/cited_host_registry.json.
Re-running the sweep after the PR merges should show those hosts gone from the
queue — that is the loop closing.

Offline / no-DB usage — iterate on the heuristics without re-reading prod. Dump
OUTSIDE the repo: the reports are large (61 MB for 174 runs) and hold prod data.

  railway run -- .venv/bin/python scripts/sweep_unclassified_cited_hosts.py \
      --database-url "$DATABASE_PUBLIC_URL?sslmode=require" \
      --dump-runs /tmp/audit_reports_dump.json              # one DB read
  .venv/bin/python scripts/sweep_unclassified_cited_hosts.py \
      --input-json /tmp/audit_reports_dump.json             # re-run offline

Optional LLM proposer (heuristics are the default; the LLM only fills the
type/subtype/categories guess, it never bypasses review):

  ... scripts/sweep_unclassified_cited_hosts.py --llm deepseek
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.brand_alias import (  # noqa: E402
    _registrable_name_from_host as registrable_label,
)
from services.cited_host_classifier import (  # noqa: E402
    classify_host,
    is_profile_retailer_name,
)
from services.competitor_brand_filter import (  # noqa: E402
    is_ingredient_or_category_type,
)
from services.competitor_recurrence import _NON_BRAND  # noqa: E402

logger = logging.getLogger(__name__)

REGISTRY_PATH = "data/cited_host_registry.json"

# Competitor-name matching needs a longer floor than a generic alias match:
# a 3-char label ("cos") would prefix-match half the beauty long tail.
_MIN_COMPETITOR_ALIAS_LEN = 5

# Not publishers — the Vertex grounding redirector and friends. They are an
# artifact of how citations reach us, not a host anyone could ever pitch, get
# listed on, or compete with, so they don't belong in the registry queue at all.
# (vertexaisearch alone carries 2,644 citations in prod — it would otherwise sit
# at the top of every sweep forever.)
_INFRASTRUCTURE_HOSTS = frozenset({
    "vertexaisearch.cloud.google.com",
    "googleusercontent.com",
    "webcache.googleusercontent.com",
    "translate.google.com",
})

# The engines' `competitors_named` list is LLM-extracted and conflates rival
# BRANDS with the retailers, marketplaces and payment/delivery platforms that
# happen to appear in the same answer ("Kroger", "Dermstore", "Klarna",
# "DoorDash"). Registering those as type=brand would be actively harmful — brand
# is the type the narrative builder SKIPS as an outreach target, so a retailer
# typed as a brand silently deletes a real channel from the merchant's playbook.
# Reuse the two guards the report layer already curates for this exact conflation.
_NON_BRAND_FOLDED = frozenset(
    "".join(ch for ch in name if ch.isalnum()) for name in _NON_BRAND
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def fold(value: Optional[str]) -> str:
    """Diacritics-folded, de-spaced, alphanumeric-only form of a brand-ish name
    or host label. 'Kérastase®' -> 'kerastase'; 'Soko Glam' -> 'sokoglam'.

    NFKD-decompose FIRST, then drop combining marks, THEN strip non-alnum — the
    order matters. services.brand_alias._normalize collapses non-ASCII straight
    to a space ('kérastase' -> 'k rastase'), which is precisely why the
    kerastase-usa.com storefront never matched the 'Kérastase' competitor name
    the engines returned.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text.lower() if ch.isalnum())


def host_label(host: Optional[str]) -> str:
    """Registrable label of a host, diacritics-folded: 'www.kerastase-usa.com'
    -> 'kerastaseusa'."""
    return fold(registrable_label(host))


# ---------------------------------------------------------------------------
# Report extraction
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _host_rows_from_authority_map(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    hosts = _as_dict(report).get("authority_map")
    rows = _as_dict(hosts).get("hosts")
    return [r for r in rows or [] if isinstance(r, dict) and r.get("host")]


def _per_sku_host_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fallback for runs whose report has no top-level authority_map.hosts:
    fold the per-SKU authority hosts into the same row shape, summing
    prompts_cited_count and OR-ing the flags."""
    buckets: List[Dict[str, Any]] = []
    containers: List[Any] = []
    containers.extend(_as_dict(_as_dict(report).get("authority_map")).get("skus") or [])
    containers.extend(_as_dict(report).get("per_sku_reports") or [])
    for sku in containers:
        for row in _as_dict(sku).get("authority_hosts") or []:
            if isinstance(row, dict) and row.get("host"):
                buckets.append(row)

    merged: Dict[str, Dict[str, Any]] = {}
    for row in buckets:
        key = str(row["host"]).strip().lower()
        acc = merged.setdefault(key, {
            "host": key,
            "host_type": row.get("host_type"),
            "citation_role": row.get("citation_role"),
            "first_party": False,
            "is_competitor": False,
            "prompts_cited_count": 0,
            "competitors_named": [],
            "evidence_urls": [],
        })
        acc["first_party"] = bool(acc["first_party"] or row.get("first_party"))
        acc["is_competitor"] = bool(acc["is_competitor"] or row.get("is_competitor"))
        acc["prompts_cited_count"] += int(row.get("prompts_cited_count") or 0)
        for name in row.get("competitors_named") or []:
            if name and name not in acc["competitors_named"]:
                acc["competitors_named"].append(name)
        for url in row.get("evidence_urls") or []:
            if url and url not in acc["evidence_urls"]:
                acc["evidence_urls"].append(url)
    return list(merged.values())


def extract_host_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cited-host rows for one run. Prefers the top-level authority_map.hosts[];
    falls back to the per-SKU authority hosts when that map is absent."""
    rows = _host_rows_from_authority_map(report)
    return rows if rows else _per_sku_host_rows(report)


def extract_competitor_names(report: Dict[str, Any]) -> List[str]:
    """Every competitor brand name the engines listed anywhere in this run —
    pooled across the top-level host rows AND the per-SKU rows, because a
    competitor can be NAMED under one SKU while its storefront is CITED under
    another (that pooling is what build_authority_map does at report time)."""
    names: List[str] = []
    seen = set()

    def _take(rows: Iterable[Any]) -> None:
        for row in rows or []:
            for name in _as_dict(row).get("competitors_named") or []:
                if isinstance(name, str) and name.strip() and name not in seen:
                    seen.add(name)
                    names.append(name.strip())

    _take(_host_rows_from_authority_map(report))
    for sku in _as_dict(_as_dict(report).get("authority_map")).get("skus") or []:
        _take(_as_dict(sku).get("authority_hosts") or [])
    for sku in _as_dict(report).get("per_sku_reports") or []:
        _take(_as_dict(sku).get("authority_hosts") or [])
    return names


def competitor_aliases(names: Sequence[str]) -> Dict[str, str]:
    """{folded_alias: original_name} for the run's competitor names — the ones
    that are actually rival BRANDS.

    Three classes of name are dropped, because the engines' competitor list is
    LLM-extracted and includes all of them:
      - ingredient / category TYPES ("argan oil", "collagen"): not brands, and
        their folded forms would flag arganoilshop.com as a rival storefront;
      - vertical RETAILERS the profiles already know ("Best Buy", "Walmart"):
        a retailer carrying the merchant's listing is a channel, not a rival;
      - the curated non-brand stoplist the competitor-recurrence queue uses
        (marketplaces, department stores, "Shop App"...).
    A name that survives all three is proposed as a competitor storefront — and
    still goes to a human, because this filter is a floor, not a proof.
    """
    out: Dict[str, str] = {}
    for name in names or []:
        if is_ingredient_or_category_type(name):
            continue
        if is_profile_retailer_name(name):
            continue
        folded = fold(name)
        if folded in _NON_BRAND_FOLDED:
            continue
        if len(folded) >= _MIN_COMPETITOR_ALIAS_LEN and folded not in out:
            out[folded] = name
    return out


def competitor_match(host: str, aliases: Dict[str, str]) -> Optional[str]:
    """The competitor NAME a host's registrable label matches, or None.

    Exact or brand-prefix on the folded label, so 'kerastase-usa.com' matches
    'Kérastase' (label 'kerastaseusa' starts with 'kerastase') while the other
    brands named in the same run don't false-match. Prefix, not substring:
    'notkerastase.com' is not a match.
    """
    label = host_label(host)
    if len(label) < _MIN_COMPETITOR_ALIAS_LEN:
        return None
    best: Optional[Tuple[int, str]] = None
    for alias, name in aliases.items():
        if label == alias or label.startswith(alias):
            # longest alias wins ("glowrecipe" over "glow")
            if best is None or len(alias) > best[0]:
                best = (len(alias), name)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_hosts(runs: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Aggregate cited hosts across runs.

    `runs` = [{merchant_id, run_id, report}]. Returns {host: stat}, where a stat
    carries distinct merchants, total citations, roles, flags and the competitor
    names matched. FIRST-PARTY ROWS ARE DROPPED — a merchant's own domain is
    never registry material — but only the ROW is dropped, not the host: a host
    that is first-party for merchant A can still be a third-party citation for
    merchant B, and only B's rows count.
    """
    stats: Dict[str, Dict[str, Any]] = {}
    for run in runs or []:
        report = _as_dict(run.get("report"))
        merchant_id = str(run.get("merchant_id") or "") or None
        run_id = str(run.get("run_id") or "") or None
        aliases = competitor_aliases(extract_competitor_names(report))

        for row in extract_host_rows(report):
            if row.get("first_party"):
                continue
            host = str(row.get("host") or "").strip().lower()
            if not host or host in _INFRASTRUCTURE_HOSTS:
                continue
            stat = stats.setdefault(host, {
                "host": host,
                "merchants": set(),
                "run_ids": [],
                "citations": 0,
                "roles": defaultdict(int),
                "host_types": defaultdict(int),
                "is_competitor_rows": 0,
                "rows": 0,
                "competitor_names": {},
                "evidence_urls": [],
            })
            stat["rows"] += 1
            if merchant_id:
                stat["merchants"].add(merchant_id)
            if run_id and run_id not in stat["run_ids"]:
                stat["run_ids"].append(run_id)
            stat["citations"] += int(row.get("prompts_cited_count") or 0)
            role = row.get("citation_role")
            if role:
                stat["roles"][str(role)] += 1
            host_type = row.get("host_type")
            if host_type:
                stat["host_types"][str(host_type)] += 1
            if row.get("is_competitor"):
                stat["is_competitor_rows"] += 1
            matched = competitor_match(host, aliases)
            if matched:
                stat["competitor_names"].setdefault(matched, 0)
                stat["competitor_names"][matched] += 1
            for url in row.get("evidence_urls") or []:
                if url and len(stat["evidence_urls"]) < 3 and url not in stat["evidence_urls"]:
                    stat["evidence_urls"].append(url)
    return stats


def rank_hosts(stats: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """(distinct_merchants desc, citations desc, host asc) — cross-merchant
    recurrence first, because a host two merchants' audits both hit is a
    stronger registry candidate than one that racked up citations for one."""
    return sorted(
        stats.values(),
        key=lambda s: (-len(s["merchants"]), -int(s["citations"]), s["host"]),
    )


# ---------------------------------------------------------------------------
# Type proposal (heuristics — every rule is named in `proposed_by`)
# ---------------------------------------------------------------------------

_RETAILER_LABEL_TOKENS = ("shop", "store", "mart", "market", "outlet", "buy")
_EDITORIAL_LABEL_TOKENS = (
    "mag", "magazine", "review", "reviews", "blog", "news", "times", "daily",
    "journal", "digest", "guide",
)
_FORUM_LABEL_TOKENS = ("forum", "forums", "community", "board")
_KNOWN_SOCIAL_HOSTS = {
    "youtube.com": ("video", "creator_platform"),
    "youtu.be": ("video", "creator_platform"),
    "tiktok.com": ("video", "creator_platform"),
    "instagram.com": ("social", "social"),
    "facebook.com": ("social", "social"),
    "pinterest.com": ("social", "social"),
    "x.com": ("social", "social"),
    "twitter.com": ("social", "social"),
    "quora.com": ("forum", "community"),
}


def _brand_storefront_affix_residual(label: str) -> Optional[str]:
    """When a label is a generic storefront affix wrapped around a >=4-char
    residual ('shopzygo' -> 'zygo', 'trycosrx' -> 'cosrx'), return the residual.

    Mirrors the report layer's `_BRAND_STOREFRONT_PREFIXES` — the affixes brands
    bolt onto their OWN name for a DTC domain. A label of this shape is exactly
    as likely to be a rival's storefront as a retailer, so the proposer must not
    type it from the label alone.
    """
    for affix in ("shop", "try", "get", "buy", "my", "the", "go"):
        if label.startswith(affix) and len(label) - len(affix) >= 4:
            return label[len(affix):]
        if label.endswith(affix) and len(label) - len(affix) >= 4:
            return label[: -len(affix)]
    return None


def _is_known_non_brand_label(label: str) -> bool:
    """True when a host's registrable label names a retailer / marketplace /
    platform the repo already knows is NOT a rival brand."""
    return bool(label) and (
        label in _NON_BRAND_FOLDED or is_profile_retailer_name(label)
    )


def propose_type(stat: Dict[str, Any]) -> Dict[str, Any]:
    """Heuristic type/subtype/tier proposal for one unclassified host.

    Returns {type, subtype, tier, categories, proposed_by, confidence,
    rationale}. `type` is None when no rule fires — the entry still ships, and a
    human types it. Guessing is not allowed to look like knowing.
    """
    host = stat["host"]
    label = host_label(host)
    merchants = len(stat["merchants"])
    flagged_competitor = bool(stat.get("competitor_names") or stat.get("is_competitor_rows"))

    if _is_known_non_brand_label(label):
        # The label is on a list the repo already curates as NOT-a-rival-brand
        # (services.competitor_recurrence._NON_BRAND + the vertical profiles'
        # retailer tokens): kroger.com, jcpenney.com, holiholic.com, dodoskin.com.
        # These arrive flagged is_competitor by the audit — from the same LLM
        # competitor list that names retailers — so registering them as retailers
        # doesn't just classify them, it CORRECTS the mis-flag for every future
        # audit (a registry-known retailer name is dropped by
        # _run_competitor_aliases, so _flag_competitor_by_name stops flipping it).
        was_flagged = (
            " The audit had flagged it as a competitor; that flag is the bug this "
            "entry fixes." if flagged_competitor else ""
        )
        return {
            "type": "retailer",
            "subtype": "multi_brand_retailer",
            "tier": None,
            "categories": [],
            "proposed_by": "heuristic:known_non_brand_name",
            "confidence": "low",
            "rationale": (
                f"'{label}' is on the curated non-brand list "
                "(services.competitor_recurrence._NON_BRAND / the vertical profiles' "
                "retailer tokens) — a retailer carrying the merchant's listing is a "
                f"CHANNEL, not a rival.{was_flagged}"
            ),
        }

    if stat.get("competitor_names") and merchants == 1:
        names = ", ".join(sorted(stat["competitor_names"]))
        return {
            "type": "brand",
            "subtype": "competitor_storefront",
            "tier": None,
            "categories": [],
            "proposed_by": "heuristic:competitor_name_match",
            "confidence": "medium",
            "rationale": (
                f"Host label '{label}' matches competitor brand(s) the engines named "
                f"in the same runs ({names}), and it is cited for ONE merchant — the "
                "shape of a rival's own storefront inside that merchant's category. "
                "Registering a rival as type=brand is what makes "
                "merchant_narrative_builder skip it as an outreach target (never "
                "pitch a rival's site). Confirm it is a single-brand storefront "
                "before merging; if it sells many brands, register type=retailer."
            ),
        }

    if stat.get("competitor_names"):
        # Name-matched, but cited across several merchants. That recurrence is
        # evidence AGAINST "one rival's store" — multi-brand retailers are what
        # recur across merchants (dermstore.com, bluemercury.com), and so do
        # platforms an LLM happens to list as "competitors" (klarna.com,
        # doordash.com, webmd.com). Do NOT assert a type: brand is the type the
        # narrative builder SKIPS, so a wrong `brand` deletes a real channel.
        names = ", ".join(sorted(stat["competitor_names"]))
        return {
            "type": None,
            "subtype": None,
            "tier": None,
            "categories": [],
            "proposed_by": "heuristic:competitor_name_match_ambiguous",
            "confidence": "none",
            "rationale": (
                f"Host label '{label}' matches competitor name(s) the engines returned "
                f"({names}), BUT it is cited across {merchants} merchants — which is "
                "retailer/platform behaviour, not one rival's storefront. A human must "
                "pick: type=brand (rival's own store, and the narrative builder will "
                "stop pitching it) or type=retailer (a channel worth listing on). "
                "Typing it wrong in either direction is worse than leaving it "
                "unclassified, so the script refuses to guess."
            ),
        }

    if stat.get("is_competitor_rows"):
        return {
            "type": "brand",
            "subtype": "competitor_storefront",
            "tier": None,
            "categories": [],
            "proposed_by": "heuristic:report_is_competitor_flag",
            "confidence": "low",
            "rationale": (
                "The audit report flagged this host is_competitor "
                f"({stat['is_competitor_rows']} row(s)) — but only per-run, and from "
                "the same LLM competitor list that conflates rivals with retailers. "
                "Confirm it is really a rival's own storefront; register "
                "type=retailer if it sells many brands."
            ),
        }

    known = _KNOWN_SOCIAL_HOSTS.get(host)
    if known:
        return {
            "type": known[0],
            "subtype": known[1],
            "tier": None,
            "categories": [],
            "proposed_by": "heuristic:known_platform_host",
            "confidence": "high",
            "rationale": "Well-known platform host.",
        }

    if any(tok in label for tok in _FORUM_LABEL_TOKENS):
        return {
            "type": "forum", "subtype": "community", "tier": None, "categories": [],
            "proposed_by": "heuristic:forum_label_token", "confidence": "low",
            "rationale": f"Host label '{label}' contains a community/forum token.",
        }

    if any(tok in label for tok in _EDITORIAL_LABEL_TOKENS):
        return {
            "type": "editorial", "subtype": "review_site", "tier": 3, "categories": [],
            "proposed_by": "heuristic:editorial_label_token", "confidence": "low",
            "rationale": (
                f"Host label '{label}' contains an editorial token; tier 3 is the "
                "conservative default for an unknown editorial source."
            ),
        }

    if any(tok in label for tok in _RETAILER_LABEL_TOKENS):
        # ...but a retail word is ALSO how a brand names its own DTC storefront:
        # 'shopzygo' is not a shop, it is Zygo (bone-conduction swim headphones,
        # a direct rival of the catalog's audio brands) wearing the same prefix
        # the report layer knows as _BRAND_STOREFRONT_PREFIXES (tryanuko,
        # shopbblab). Typing that 'retailer' gets it exactly backwards and leaves
        # the narrative builder pitching a rival's store. The label cannot decide,
        # so the proposer doesn't.
        residual = _brand_storefront_affix_residual(label)
        if residual:
            return {
                "type": None, "subtype": None, "tier": None, "categories": [],
                "proposed_by": "heuristic:brand_storefront_affix_ambiguous",
                "confidence": "none",
                "rationale": (
                    f"Host label '{label}' is a retail word wrapped around "
                    f"'{residual}' — the shape of a BRAND's own DTC storefront "
                    "(shopzygo.com = Zygo, a rival) as much as of a shop. Typing it "
                    "'retailer' when it is a rival's store leaves the narrative "
                    "builder pitching it; typing it 'brand' when it is a real shop "
                    f"deletes a channel. Check '{residual}': one brand, or many?"
                ),
            }
        return {
            "type": "retailer", "subtype": "online_retailer", "tier": None,
            "categories": [],
            "proposed_by": "heuristic:retailer_label_token", "confidence": "low",
            "rationale": f"Host label '{label}' contains a retail token.",
        }

    return {
        "type": None,
        "subtype": None,
        "tier": None,
        "categories": [],
        "proposed_by": "heuristic:unresolved",
        "confidence": "none",
        "rationale": (
            "No rule fired — a human must type this host. Cited by "
            f"{len(stat['merchants'])} merchant(s) across "
            f"{stat['citations']} prompt citation(s)."
        ),
    }


def build_proposals(
    runs: Sequence[Dict[str, Any]],
    *,
    classify: Callable[[str], Dict[str, Any]] = classify_host,
    min_merchants: int = 1,
    min_citations: int = 1,
    limit: int = 0,
) -> Dict[str, Any]:
    """Full pure pipeline: runs -> aggregated -> unclassified-only -> ranked ->
    proposals. No DB, no filesystem — the whole thing is unit-testable."""
    stats = aggregate_hosts(runs)
    ranked = rank_hosts(stats)

    proposals: List[Dict[str, Any]] = []
    already_classified = 0
    for stat in ranked:
        if (classify(stat["host"]).get("type") or "unclassified") != "unclassified":
            already_classified += 1
            continue
        merchants = len(stat["merchants"])
        if merchants < min_merchants or stat["citations"] < min_citations:
            continue
        proposal = propose_type(stat)
        proposals.append({
            "host": stat["host"],
            "type": proposal["type"],
            "subtype": proposal["subtype"],
            "categories": proposal["categories"],
            "tier": proposal["tier"],
            "proposed_by": proposal["proposed_by"],
            "confidence": proposal["confidence"],
            "rationale": proposal["rationale"],
            "review_status": "pending_human_review",
            "evidence": {
                "merchants": merchants,
                "citations": stat["citations"],
                "sample_merchant_ids": sorted(stat["merchants"])[:5],
                "sample_run_ids": stat["run_ids"][:3],
                "citation_roles": dict(sorted(stat["roles"].items())),
                "report_host_types": dict(sorted(stat["host_types"].items())),
                "is_competitor_rows": stat["is_competitor_rows"],
                "competitor_names_matched": sorted(stat["competitor_names"]),
                "sample_evidence_urls": stat["evidence_urls"][:3],
            },
        })

    if limit and limit > 0:
        proposals = proposals[:limit]

    merchant_ids = {
        str(r.get("merchant_id")) for r in runs or [] if r.get("merchant_id")
    }
    return {
        "_meta": {
            "source": "merchant_audit_runs.report_jsonb (status='succeeded')",
            "runs_scanned": len(runs or []),
            "merchants_scanned": len(merchant_ids),
            "hosts_seen": len(stats),
            "hosts_already_classified": already_classified,
            "hosts_unclassified": len(stats) - already_classified,
            "proposals": len(proposals),
            "min_merchants": min_merchants,
            "min_citations": min_citations,
            "review_required": True,
            "registry_path": REGISTRY_PATH,
            "note": (
                "PROPOSALS ONLY. This file is not read by any runtime code and "
                f"this script never writes {REGISTRY_PATH}. Approved entries land "
                "via a PR that edits the registry by hand. Entries with "
                "type=null need a human to assign the type."
            ),
        },
        "proposals": proposals,
    }


# ---------------------------------------------------------------------------
# Optional LLM proposer (fills type/subtype/categories only; review still required)
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You classify web hosts that appear as citations in AI shopping answers. "
    "Answer with JSON only. Be conservative: when you do not recognise the host, "
    "return type 'unclassified' rather than guessing."
)

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "hosts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "type": {"type": "string"},
                    "subtype": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "coverage_note": {"type": "string"},
                },
                "required": ["host", "type"],
            },
        }
    },
    "required": ["hosts"],
}

_LLM_TYPES = {
    "editorial", "retailer", "marketplace", "video", "community", "forum",
    "social", "brand", "cdn", "unclassified",
}


async def apply_llm_proposals(
    doc: Dict[str, Any], *, provider: str, model: str = "", batch: int = 25,
) -> Dict[str, Any]:
    """Overlay LLM type/subtype/categories onto proposals the heuristics could
    not type (`heuristic:unresolved`). Competitor-storefront verdicts are NEVER
    overwritten — those come from the run's own competitors_named, which is
    stronger evidence than a model's recall. Failures are non-fatal: the
    heuristic entry stays as-is."""
    from services.llm_io import generate_structured

    targets = [
        p for p in doc["proposals"] if p["proposed_by"] == "heuristic:unresolved"
    ]
    if not targets:
        return doc

    by_host = {p["host"]: p for p in doc["proposals"]}
    for start in range(0, len(targets), batch):
        chunk = targets[start:start + batch]
        user = (
            "For each host below, return its type "
            f"({', '.join(sorted(_LLM_TYPES))}), a subtype, the product "
            "categories it covers, and a one-sentence coverage_note. "
            "'brand' means the host is a single brand's own storefront.\n\n"
            + "\n".join(f"- {p['host']}" for p in chunk)
        )
        result = await generate_structured(
            system=_LLM_SYSTEM, user=user, provider=provider, model=model,
            schema=_LLM_SCHEMA, expect="object", label="registry_host_proposal",
        )
        if result.outcome not in ("ok", "repaired") or not isinstance(result.value, dict):
            logger.warning(
                "LLM host proposal batch failed (%s) — heuristic entries kept.",
                result.outcome,
            )
            continue
        for item in result.value.get("hosts") or []:
            if not isinstance(item, dict):
                continue
            target = by_host.get(str(item.get("host") or "").strip().lower())
            if not target or target["proposed_by"] != "heuristic:unresolved":
                continue
            host_type = str(item.get("type") or "").strip().lower()
            if host_type not in _LLM_TYPES or host_type == "unclassified":
                continue
            target["type"] = host_type
            target["subtype"] = item.get("subtype") or None
            target["categories"] = [
                str(c) for c in (item.get("categories") or []) if c
            ][:6]
            target["tier"] = 3 if host_type == "editorial" else None
            target["proposed_by"] = f"llm:{provider}"
            target["confidence"] = "low"
            target["rationale"] = (
                (str(item.get("coverage_note") or "").strip()
                 or "Proposed by LLM; unverified.")
                + " (LLM proposal — verify the host before merging.)"
            )
    doc["_meta"]["llm_proposer"] = f"llm:{provider}"
    return doc


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

_FETCH_SQL = """
    SELECT merchant_id, run_id::text AS run_id, report_jsonb
    FROM merchant_audit_runs
    WHERE status = 'succeeded' AND report_jsonb IS NOT NULL
    ORDER BY requested_at DESC
"""


async def fetch_runs_from_db(
    limit: int = 0,
    *,
    database_url: Optional[str] = None,
    command_timeout: float = 180.0,
) -> List[Dict[str, Any]]:
    """Read the succeeded audit reports over ONE asyncpg connection.

    Deliberately not `db.database` (the app's `databases`/SQLAlchemy pool): that
    layer translates a DSN's `sslmode=require` into full certificate
    verification, which the Railway public proxy's self-signed chain fails — and
    it fails by HANGING on pool creation, not by raising. asyncpg parsing the DSN
    itself keeps libpq semantics (`require` = encrypt, don't verify the CA), which
    is what the proxy needs. A `command_timeout` is set explicitly for the same
    reason: without one, a stalled read over the proxy hangs forever.

    Never disable TLS to work around a certificate error — fix the DSN or run
    from an environment that trusts the CA.
    """
    import asyncpg

    from db.merchant_audit_runs import _decode_jsonb_field

    dsn = database_url or os.environ.get("DATABASE_URL") or ""
    if not dsn:
        raise SystemExit("No DATABASE_URL — pass --database-url or use --input-json.")

    sql = _FETCH_SQL + (f" LIMIT {int(limit)}" if limit and limit > 0 else "")
    conn = await asyncpg.connect(dsn, timeout=30, command_timeout=command_timeout)
    try:
        rows = await conn.fetch(sql)
    finally:
        await conn.close()

    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        report = _decode_jsonb_field(d.get("report_jsonb"))
        if report:
            out.append({
                "merchant_id": d.get("merchant_id"),
                "run_id": d.get("run_id"),
                "report": report,
            })
    return out


def load_runs_from_file(path: str) -> List[Dict[str, Any]]:
    """Accepts a bare list of runs, {"runs": [...]}, and rows that carry the
    report under either `report` or `report_jsonb` (so a raw psql/DB dump works
    without reshaping)."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = doc.get("runs") if isinstance(doc, dict) else doc
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        report = row.get("report")
        if report is None:
            report = row.get("report_jsonb")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except ValueError:
                report = None
        if isinstance(report, dict):
            out.append({
                "merchant_id": row.get("merchant_id"),
                "run_id": row.get("run_id"),
                "report": report,
            })
    return out


def _default_out_path() -> str:
    return f"reports/registry_proposals_{date.today().isoformat()}.json"


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input_json:
        runs = load_runs_from_file(args.input_json)
    else:
        runs = await fetch_runs_from_db(
            limit=args.max_runs, database_url=args.database_url,
        )

    if args.dump_runs:
        dump = Path(args.dump_runs)
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(json.dumps({"runs": runs}, default=str), encoding="utf-8")
        logger.info("dumped %d runs to %s", len(runs), dump)

    doc = build_proposals(
        runs,
        min_merchants=args.min_merchants,
        min_citations=args.min_citations,
        limit=args.limit,
    )
    if args.llm:
        doc = await apply_llm_proposals(doc, provider=args.llm, model=args.llm_model)

    out = Path(args.out or _default_out_path())
    out.parent.mkdir(parents=True, exist_ok=True)
    doc["_meta"]["output_path"] = str(out)
    out.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return doc


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Propose cited_host_registry entries for recurring unclassified hosts.",
    )
    ap.add_argument("--input-json", help="Run offline from a dumped runs file.")
    ap.add_argument("--database-url", help="DSN to read (default: $DATABASE_URL).")
    ap.add_argument("--dump-runs", help="Write the fetched runs to this path for offline re-runs.")
    ap.add_argument("--out", help=f"Output path (default: {_default_out_path()})")
    ap.add_argument("--max-runs", type=int, default=0, help="0 = all succeeded runs")
    ap.add_argument("--min-merchants", type=int, default=1,
                    help="Only propose hosts cited by at least N distinct merchants.")
    ap.add_argument("--min-citations", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="Cap the proposal list (0 = all).")
    ap.add_argument("--llm", help="Provider for the optional LLM proposer (e.g. deepseek).")
    ap.add_argument("--llm-model", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    doc = asyncio.run(_drive(args))
    meta = doc["_meta"]
    print(json.dumps(meta, indent=2, default=str))
    print(
        f"\n{meta['proposals']} proposal(s) -> {meta.get('output_path')}\n"
        f"REVIEW REQUIRED: nothing was written to {REGISTRY_PATH}. "
        "Approve entries by hand in a PR."
    )


if __name__ == "__main__":
    main()
