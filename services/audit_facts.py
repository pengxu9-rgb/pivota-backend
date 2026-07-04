"""W1 RunFacts — the single audit fact layer (phase 1: compute + stamp + parity).

"Was the merchant cited?" had ~13 independent implementations across the
codebase, each with a different definition over three different denominators
(architecture review 2026-07-03 §1.1). This module is the one place those
facts are computed, as three explicit tiers (decision sheet
`~/dev/reports/w1_runfacts_decision_sheet_2026-07-04.md`):

  T1 ``own_url_cited``    — the merchant's own domain resolved as a cited
                            grounding-source host. "AI cited YOUR page."
  T2 ``endorsed``         — an independent (editorial/community/creator,
                            non-listing) host cites AND names the merchant.
                            "AI recommends you on someone else's authority."
  T3 ``brand_mentioned``  — a cited source's title/label names the brand (or
                            carries its domain). "AI knows you exist" —
                            distribution, not endorsement.

Phase-1 contract (this file changes NO displayed number):
  * ``compute_run_facts`` is called once at report assembly and the result is
    stamped on the payload as ``run_facts``.
  * Every legacy implementation keeps computing its own version; call sites
    compare against RunFacts via :func:`parity_check`, which logs
    ``RUNFACTS_PARITY_DRIFT`` (log-only) on mismatch.
  * The cutover (legacy sites rewired to RunFacts, then deleted) is phase 2,
    gated on founder sign-off + a parity window — see
    :data:`LEGACY_CITEDNESS_SITES`.

This module must NOT import from ``services.agent_center_bd_report_service``
(the god module). The functions below that used to live there
(``normalize_host``, ``_identify_run_sources``, ``_source_matches_merchant``,
``_own_url_cited_runs``, the query-class vocabulary) were MOVED here — the god
module re-imports them so every existing import path keeps working.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from services.brand_alias import derive_brand_aliases, text_mentions_brand
from services.cited_host_classifier import (
    classify_host,
    is_endorsement_role,
    is_findability_role,
    merchant_relative_role,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Host + source resolution (moved verbatim from the god module)
# ---------------------------------------------------------------------------


def normalize_host(url: str) -> Optional[str]:
    """Strip www, lowercase. Returns None for unparseable URLs."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


# Vertex AI grounding wraps every cited URL in a redirector — the URI we
# get back is `vertexaisearch.cloud.google.com/grounding-api-redirect/...`
# which hides the actual destination domain. The structured chunk's
# `title` field contains the human-readable source name ("Sephora",
# "Olive Young Global", "Beauty of Joseon Official Store") — much more
# useful for BD competitor analysis than the redirector hostname.
_VERTEX_REDIRECTOR_HOSTS = {
    "vertexaisearch.cloud.google.com",
    "vertex-ai-search.cloud.google.com",
}


# A bare-domain string ("oliveyoung.com", "the-independent.com") rather than
# a human-readable source title ("Olive Young Global"): has a dot, no
# whitespace, plausible DNS labels. Used to decide whether a grounding-chunk
# `title` can be taken as the real host verbatim.
_HOSTLIKE_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def _looks_like_host(value: Optional[str]) -> bool:
    host = normalize_host(value or "")
    return bool(host) and " " not in host and bool(_HOSTLIKE_RE.match(host))


def _grounding_source_host(source: Dict[str, Any]) -> Optional[str]:
    """Resolve the *real* publisher host for one grounding source dict.

    Gemini grounding wraps every cited URL in a Vertex redirector
    (`vertexaisearch.cloud.google.com/grounding-api-redirect/<token>`) that
    hides the destination domain; the structured chunk's `title` carries the
    real source — almost always the bare domain ("oliveyoung.com", "ebay.com"),
    occasionally a display name ("Olive Young Global"). Resolution order:

      1. Real (non-redirector) URI host — use it directly (covers the case
         where probe-time resolution already followed the 302, and any
         non-Gemini provider that cites a plain URL).
      2. Redirector URI -> derive the host from `title`:
         a. title shaped like a domain -> that domain.
         b. title is a display name -> BD cited-host registry alias lookup -> host.
      3. Unresolvable -> None, so the citation is dropped from the host rollup
         rather than mis-attributed to the opaque redirector domain.

    This is the per-source analogue of `_identify_run_sources` (the
    competitor-extraction path's redirector fix). Without it,
    `authority_map.hosts` collapses every Gemini citation onto
    `vertexaisearch.cloud.google.com` (the v3 per-SKU regression: the real
    hosts live in each chunk's `title`, not the redirector URI host).
    """
    if not isinstance(source, dict):
        return None
    uri_host = normalize_host(source.get("uri") or "")
    if uri_host and uri_host not in _VERTEX_REDIRECTOR_HOSTS:
        return uri_host
    title = (source.get("title") or "").strip()
    if not title:
        return None
    if _looks_like_host(title):
        return normalize_host(title)
    # Display-name title — try the BD cited-host registry alias index, which
    # maps "Sephora"/"Olive Young Global" to a canonical host. Restrict this to
    # short, name-like titles: a real source name is a few words ("Beauty of
    # Joseon Official Store"), whereas a long page headline could match a
    # registry alias as a coincidental substring and fabricate a cited host
    # (e.g. "...best collagen for your target audience" -> target.com). The
    # no-fabrication guardrail makes dropping such a citation the safe default.
    if len(title.split()) <= 6:
        resolved = classify_host(title).get("host")
        if resolved and _looks_like_host(resolved):
            return resolved
    return None


def _identify_run_sources(run: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return a list of `{key, label, host}` source identifiers for one run.

    Reads the new `grounding_sources` field (list of `{uri, title}`)
    when present (PIVOTA-Agent #1302+), falls back to the legacy
    `grounding_chunks` (URI strings only) for older payloads.

    `key` is what we use for de-dup + merchant matching.
    `label` is what we show in the competitor table — title preferred,
    URI host as fallback when title is missing.
    """
    sources_raw = run.get("grounding_sources")
    out: List[Dict[str, str]] = []
    seen_keys = set()
    if isinstance(sources_raw, list) and sources_raw:
        for s in sources_raw:
            if not isinstance(s, dict):
                continue
            uri = s.get("uri") or ""
            title = (s.get("title") or "").strip()
            host = normalize_host(uri) or ""
            # The resolved publisher DOMAIN for this source (redirector ->
            # title/registry; real URI -> uri host). This is the canonical
            # "what site" key for the cited-host rollup; `label` stays the
            # human title for display + merchant-brand matching.
            resolved_host = _grounding_source_host(s) or ""
            # Prefer title for the label/key when the URI is a redirector
            # (which it almost always is with Vertex AI grounding).
            if host in _VERTEX_REDIRECTOR_HOSTS:
                if not title:
                    continue  # nothing meaningful to surface
                label = title
                key = title.lower()
                # Redirector titles ARE site names — resolve to the domain when
                # we can; keep the display name as a last resort so an
                # unregistered Gemini citation still surfaces.
                rollup_host = resolved_host or title
            else:
                # Real (non-redirected) host — use the host for key and
                # title for label when we have it.
                label = title or host
                key = host or title.lower()
                # NEVER roll up by the title here: a real-URI source's title is
                # the page headline (OpenAI web_search returns "The 15 Best Hair
                # Butters … | Marie Claire"), which would leak into
                # top_cited_hosts as a fake host. Use the resolved domain only.
                rollup_host = resolved_host or host
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({"key": key, "label": label, "host": rollup_host})
        return out
    # Legacy fallback: only URI strings available.
    chunks = run.get("grounding_chunks") or []
    for url in chunks:
        host = normalize_host(url) if isinstance(url, str) else None
        if not host or host in _VERTEX_REDIRECTOR_HOSTS:
            continue
        if host in seen_keys:
            continue
        seen_keys.add(host)
        out.append({"key": host, "label": host, "host": host})
    return out


# ---------------------------------------------------------------------------
# Merchant matching (moved verbatim from the god module)
# ---------------------------------------------------------------------------


def _clean_identity_tuple(values: Optional[Tuple[str, ...]]) -> Tuple[str, ...]:
    out: List[str] = []
    seen = set()
    for value in values or ():
        cleaned = re.sub(r"\s+", " ", str(value or "").strip())
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return tuple(out)


def _source_matches_merchant(
    source: Dict[str, str],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> bool:
    """A grounding source counts as merchant-attribution when:
      - host matches the verified merchant host (rare with redirectors), OR
      - title contains the merchant host (e.g. "beautyofjoseon.com" in
        "Beauty of Joseon Official Store" — only true for some titles), OR
      - title contains the merchant brand name.
    """
    label_lower = source.get("label", "").lower()
    if merchant_host and merchant_host in label_lower:
        return True
    if merchant_brand:
        brand_lower = merchant_brand.strip().lower()
        if brand_lower and brand_lower in label_lower:
            return True
        # Phase B: alias-aware match — the merchant is recorded as
        # "BB Lab Global" but the cited source title says "BB Lab". Only
        # ADDS matches over the literal compare above (never removes one).
        if text_mentions_brand(
            label_lower,
            derive_brand_aliases(
                merchant_brand,
                merchant_host,
                _clean_identity_tuple(merchant_vendors),
            ),
        ):
            return True
    return False


def _own_url_cited_runs(
    raw_runs: List[Dict[str, Any]],
    *,
    merchant_host: Optional[str],
) -> Optional[int]:
    """Strict own-URL citation count: runs where the merchant's OWN domain is the
    resolved host of a cited grounding source.

    Distinct from `merchant_cited_runs` (which also counts brand-name-in-title
    mentions and third-party listings) — this is the honest "AI cited YOUR page"
    signal, mirroring the channels builder's `own_site_cited` logic. Returns None
    when there's no merchant host to match against (so callers can fall back to
    the softer signal rather than assert a false zero)."""
    own = normalize_host(merchant_host) if merchant_host else None
    if not own:
        return None
    count = 0
    for run in raw_runs or []:
        for src in _identify_run_sources(run) or []:
            host = normalize_host((src.get("host") or "").strip())
            if host and host == own:
                count += 1
                break
    return count


# ---------------------------------------------------------------------------
# Query-class vocabulary (moved verbatim from the god module)
# ---------------------------------------------------------------------------

# Fix 2 — query-class tagging. Every probe query carries an `axis` (set in
# `_build_per_sku_base_query_specs`). Only the `category` axis is a non-branded
# discovery query ("best <product_type> ...", "top <product_type> for <topic>");
# every other axis (intent / price / review / comparison / brand / identity /
# content / custom) names the SKU or brand, i.e. branded/navigational. The two
# classes must be reported separately: being found on a branded query ("where to
# buy <my product>") proves nothing about category discovery.
QUERY_CLASS_BRANDED = "branded_navigational"
QUERY_CLASS_CATEGORY = "category_discovery"
_CATEGORY_DISCOVERY_AXES = frozenset({"category"})


def query_class_for_axis(axis: Optional[str]) -> str:
    a = str(axis or "").strip().lower()
    return QUERY_CLASS_CATEGORY if a in _CATEGORY_DISCOVERY_AXES else QUERY_CLASS_BRANDED


def run_query_class(run: Dict[str, Any]) -> str:
    meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
    return query_class_for_axis(meta.get("axis"))


# ---------------------------------------------------------------------------
# The fact layer
# ---------------------------------------------------------------------------

# Bump when the stamped dict shape changes, so payload consumers (W7
# invariants, the UI, ops queries over stored reports) can branch explicitly.
RUN_FACTS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AuditIdentity:
    """The merchant identity the facts were computed against, recorded on the
    payload so a stamped number can never be re-litigated against a different
    identity than the one that produced it."""

    host: Optional[str] = None
    brand: Optional[str] = None
    vendors: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"host": self.host, "brand": self.brand, "vendors": list(self.vendors)}


@dataclass(frozen=True)
class SourceFact:
    """One cited grounding source, classified once.

    ``own`` is the strict T1 signal (resolved host == merchant's own domain,
    exact match — same rule as the legacy ``_own_url_cited_runs`` /
    ``own_site_cited``). ``names_merchant`` is the T3 name-gate (label carries
    the domain or brand — same rule as ``_source_matches_merchant``). The two
    are computed independently on purpose: T1 ⊆ T3 is NOT mechanically
    guaranteed by the legacy matchers, and phase 1 mirrors them exactly.
    """

    host: Optional[str]
    label: str
    own: bool
    endorsing: bool
    listing: bool
    names_merchant: bool


@dataclass(frozen=True)
class PromptFacts:
    """Facts for one prompt group (all runs of one query, across providers)."""

    query: str
    cls: str  # QUERY_CLASS_BRANDED | QUERY_CLASS_CATEGORY
    provider_verdicts: Dict[str, Dict[str, bool]]
    own_url_cited: bool
    brand_mentioned: bool
    endorsed_by: Tuple[str, ...]
    winner_brands: Tuple[str, ...]  # phase 2 (winner extraction) — empty in phase 1
    cited_hosts: Tuple[str, ...]
    run_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "cls": self.cls,
            "provider_verdicts": self.provider_verdicts,
            "own_url_cited": self.own_url_cited,
            "brand_mentioned": self.brand_mentioned,
            "endorsed_by": list(self.endorsed_by),
            "winner_brands": list(self.winner_brands),
            "cited_hosts": list(self.cited_hosts),
            "run_count": self.run_count,
        }


@dataclass(frozen=True)
class RunFacts:
    """The compute-once fact table for one set of probe runs.

    Denominators are explicit: ``*_runs`` counters are over RUNS (provider ×
    query executions), ``prompt_count_by_class`` is over PROMPT GROUPS, and
    ``provider_coverage`` states how many runs each provider contributed —
    the three bases the legacy implementations silently mixed.
    """

    prompts: Tuple[PromptFacts, ...]
    own_url_cited_runs: Optional[int]  # None = no own host to match (mirrors legacy)
    brand_mentioned_runs: int
    endorsement_hosts: Tuple[str, ...]
    prompt_count_by_class: Dict[str, int]
    provider_coverage: Dict[str, int]
    identity: AuditIdentity
    run_count: int
    runs_with_citations: int

    def to_dict(self) -> Dict[str, Any]:
        """Compact JSON-safe dict, stamped on the report payload."""
        return {
            "schema_version": RUN_FACTS_SCHEMA_VERSION,
            "prompts": [p.to_dict() for p in self.prompts],
            "prompt_count": len(self.prompts),
            "own_url_cited_runs": self.own_url_cited_runs,
            "brand_mentioned_runs": self.brand_mentioned_runs,
            "endorsement_hosts": list(self.endorsement_hosts),
            "prompt_count_by_class": dict(self.prompt_count_by_class),
            "provider_coverage": dict(self.provider_coverage),
            "identity": self.identity.to_dict(),
            "run_count": self.run_count,
            "runs_with_citations": self.runs_with_citations,
        }


def _source_fact(
    src: Mapping[str, str],
    *,
    own_host: Optional[str],
    merchant_brand: Optional[str],
    merchant_vendors: Optional[Tuple[str, ...]],
) -> SourceFact:
    host = normalize_host((src.get("host") or "").strip())
    own = bool(own_host and host and host == own_host)
    role = merchant_relative_role(
        classify_host(host).get("type") if host else None,
        first_party=own,
    )
    return SourceFact(
        host=host,
        label=src.get("label") or "",
        own=own,
        endorsing=is_endorsement_role(role),
        listing=bool(not own and is_findability_role(role)),
        names_merchant=_source_matches_merchant(
            dict(src),
            merchant_host=own_host,
            merchant_brand=merchant_brand,
            merchant_vendors=merchant_vendors,
        ),
    )


def compute_run_facts(
    raw_runs: Sequence[Mapping[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str] = None,
    merchant_vendors: Optional[Tuple[str, ...]] = None,
) -> RunFacts:
    """Walk every run's grounding sources ONCE and derive all three fact tiers.

    Runs are grouped into prompt groups by their ``query`` (case-insensitive);
    runs without a query share the ``""`` group (they still count in run-level
    rollups). Within-run source dedup follows ``_identify_run_sources``.

    Parity anchors (what each rollup must equal during the phase-1 window):
      * ``own_url_cited_runs``   == legacy ``_own_url_cited_runs``          (T1)
      * ``brand_mentioned_runs`` == ``extract_cited_hosts``'s
        ``merchant_cited_runs``                                             (T3)
      * ``runs_with_citations``  == ``extract_cited_hosts``'s
        ``runs_with_any_citation``
    """
    own_host = normalize_host(merchant_host) if merchant_host else None
    vendors = _clean_identity_tuple(merchant_vendors)
    brand = (merchant_brand or "").strip() or None

    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    own_url_cited_runs = 0
    brand_mentioned_runs = 0
    runs_with_citations = 0
    run_count = 0
    provider_coverage: Counter = Counter()

    for run in raw_runs or []:
        if not isinstance(run, Mapping):
            continue
        run_count += 1
        provider = (
            str(run.get("_provider") or run.get("provider") or "").strip().lower()
            or "unknown"
        )
        provider_coverage[provider] += 1

        sources = _identify_run_sources(dict(run))
        facts = [
            _source_fact(
                src,
                own_host=own_host,
                merchant_brand=brand,
                merchant_vendors=vendors,
            )
            for src in sources
        ]
        if facts:
            runs_with_citations += 1
        run_own = bool(own_host) and any(f.own for f in facts)
        run_mentioned = any(f.names_merchant for f in facts)
        run_endorsed_by = tuple(
            sorted({f.host for f in facts if f.host and f.endorsing and f.names_merchant and not f.own})
        )
        if run_own:
            own_url_cited_runs += 1
        if run_mentioned:
            brand_mentioned_runs += 1

        query = str(run.get("query") or "").strip()
        key = query.lower()
        group = groups.get(key)
        if group is None:
            group = {
                "query": query,
                "cls": run_query_class(dict(run)),
                "providers": {},
                "own": False,
                "mentioned": False,
                "endorsed_by": set(),
                "cited_hosts": set(),
                "run_count": 0,
            }
            groups[key] = group
            order.append(key)
        group["run_count"] += 1
        group["own"] = group["own"] or run_own
        group["mentioned"] = group["mentioned"] or run_mentioned
        group["endorsed_by"].update(run_endorsed_by)
        group["cited_hosts"].update(f.host for f in facts if f.host)
        pv = group["providers"].setdefault(
            provider, {"own_url_cited": False, "brand_mentioned": False}
        )
        pv["own_url_cited"] = pv["own_url_cited"] or run_own
        pv["brand_mentioned"] = pv["brand_mentioned"] or run_mentioned

    prompts = tuple(
        PromptFacts(
            query=groups[key]["query"],
            cls=groups[key]["cls"],
            provider_verdicts=groups[key]["providers"],
            own_url_cited=groups[key]["own"],
            brand_mentioned=groups[key]["mentioned"],
            endorsed_by=tuple(sorted(groups[key]["endorsed_by"])),
            winner_brands=(),
            cited_hosts=tuple(sorted(groups[key]["cited_hosts"])),
            run_count=groups[key]["run_count"],
        )
        for key in order
    )
    prompt_count_by_class: Counter = Counter(p.cls for p in prompts)
    endorsement_hosts = tuple(
        sorted({h for p in prompts for h in p.endorsed_by})
    )
    return RunFacts(
        prompts=prompts,
        own_url_cited_runs=own_url_cited_runs if own_host else None,
        brand_mentioned_runs=brand_mentioned_runs,
        endorsement_hosts=endorsement_hosts,
        prompt_count_by_class=dict(prompt_count_by_class),
        provider_coverage=dict(provider_coverage),
        identity=AuditIdentity(host=own_host, brand=brand, vendors=vendors),
        run_count=run_count,
        runs_with_citations=runs_with_citations,
    )


def aggregate_run_facts(
    facts_dicts: Sequence[Optional[Mapping[str, Any]]],
    *,
    identity: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Fold per-product/per-SKU stamped ``run_facts`` dicts into one
    brand-level rollup — the SAME basis for every product (kills the
    51-vs-28 counter class at cutover; in phase 1 it's the stamped
    reconciliation target for W7's ``_inv_counters_match_run_facts``).

    Prompt-level detail is not carried up (it lives on each per-product
    stamp); the rollup keeps the counters + coverage only.
    """
    valid = [f for f in facts_dicts or [] if isinstance(f, Mapping)]
    own_values = [
        f.get("own_url_cited_runs")
        for f in valid
        if isinstance(f.get("own_url_cited_runs"), int)
    ]
    prompt_count_by_class: Counter = Counter()
    provider_coverage: Counter = Counter()
    endorsement_hosts: set = set()
    for f in valid:
        for cls, n in (f.get("prompt_count_by_class") or {}).items():
            prompt_count_by_class[str(cls)] += int(n or 0)
        for provider, n in (f.get("provider_coverage") or {}).items():
            provider_coverage[str(provider)] += int(n or 0)
        endorsement_hosts.update(
            h for h in (f.get("endorsement_hosts") or []) if isinstance(h, str)
        )
    return {
        "schema_version": RUN_FACTS_SCHEMA_VERSION,
        "aggregated_from": len(valid),
        "own_url_cited_runs": sum(own_values) if own_values else None,
        "brand_mentioned_runs": sum(int(f.get("brand_mentioned_runs") or 0) for f in valid),
        "endorsement_hosts": sorted(endorsement_hosts),
        "prompt_count": sum(int(f.get("prompt_count") or 0) for f in valid),
        "prompt_count_by_class": dict(prompt_count_by_class),
        "provider_coverage": dict(provider_coverage),
        "identity": dict(identity) if identity else None,
        "run_count": sum(int(f.get("run_count") or 0) for f in valid),
        "runs_with_citations": sum(int(f.get("runs_with_citations") or 0) for f in valid),
    }


# ---------------------------------------------------------------------------
# Phase-1 parity: the 12 legacy citedness sites + drift logging
# ---------------------------------------------------------------------------

# The full inventory from the 2026-07-03 architecture review §1.1 (site 0,
# PIVOTA-Agent's `url_match.in_grounding`, lives in the other repo). Each site
# is deleted in phase 2 only after its parity window is green; flip
# `instrumented` to True when its RUNFACTS_PARITY_DRIFT comparison lands.
# Deliberately env-less and constant: this list IS the cutover checklist.
LEGACY_CITEDNESS_SITES: Tuple[Dict[str, Any], ...] = (
    {"id": 1, "site": "bd_report._source_matches_merchant", "tier": "T3-matcher",
     "definition": "host OR brand-in-title OR alias", "instrumented": True},
    {"id": 2, "site": "bd_report.extract_cited_hosts.merchant_cited_runs", "tier": "T3",
     "definition": ">=1 source matches merchant", "instrumented": True},
    {"id": 3, "site": "bd_report._own_url_cited_runs", "tier": "T1",
     "definition": "own domain == resolved source host", "instrumented": True},
    {"id": 4, "site": "bd_report.score_category_visibility", "tier": "mixed",
     "definition": "url_match OR title OR excerpt-triple", "instrumented": False},
    {"id": 5, "site": "bd_report.build_channel_appearance.own_site_cited", "tier": "T1",
     "definition": "own domain in per-prompt cited hosts", "instrumented": False},
    {"id": 6, "site": "bd_report._citation_by_intent", "tier": "T3",
     "definition": "per-prompt merchant_cited_runs > 0", "instrumented": False},
    {"id": 7, "site": "bd_report.compute_citation_score.first_party_rate", "tier": "T1-variant",
     "definition": "merchant PDP cited (url_match)", "instrumented": False},
    {"id": 8, "site": "bd_report._citation_signals (endorsement/findability)", "tier": "T2",
     "definition": "name-gated citation_role", "instrumented": False},
    {"id": 9, "site": "sku_opportunity._score_prompt_group", "tier": "T3-soft",
     "definition": "free-text mention in answer body", "instrumented": False},
    {"id": 10, "site": "citation_operator_service", "tier": "T3-soft",
     "definition": "own thresholds + substring match", "instrumented": False},
    {"id": 11, "site": "task_queue_service proof-of-done", "tier": "T2-ish",
     "definition": "endorsement-host-now-cites", "instrumented": False},
    {"id": 12, "site": "co_occurrence_finder._brand_in_text", "tier": "T3-soft",
     "definition": "manually-synced copy of site 4's matcher", "instrumented": False},
)


def parity_check(
    site: str,
    legacy_value: Any,
    facts_value: Any,
    *,
    context: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Phase-1 parity probe: compare a legacy implementation's number against
    the RunFacts value and WARN (log-only, never raises, never changes the
    returned number) on drift. Returns True when the values agree.

    Grep target: ``RUNFACTS_PARITY_DRIFT``. A week of drift-free logs on a
    site is the evidence that lets phase 2 rewire it to RunFacts and delete
    the legacy path.
    """
    try:
        equal = legacy_value == facts_value
        if not equal:
            logger.warning(
                "RUNFACTS_PARITY_DRIFT site=%s legacy=%r run_facts=%r context=%s",
                site,
                legacy_value,
                facts_value,
                dict(context) if context else {},
            )
        return equal
    except Exception:  # noqa: BLE001 — parity logging must never break a report
        logger.warning("RUNFACTS_PARITY_CHECK_FAILED site=%s", site, exc_info=True)
        return False
