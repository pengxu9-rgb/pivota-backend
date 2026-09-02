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


# A bare-domain string ("oliveyoung.com", "independent.co.uk") rather than
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


def own_url_cited_runs_any(
    raw_runs: Sequence[Mapping[str, Any]],
    hosts: Sequence[Optional[str]],
) -> Optional[int]:
    """Multi-host T1: runs where ANY of the merchant's own hosts is the resolved
    host of a cited grounding source. This is the unified own-domain resolver the
    phase-2 cutover points `first_party_rate` at (a merchant legitimately owns
    several hosts: store domain, Pivota canonical, second storefronts) — in
    phase 1 it only feeds parity measurement. Returns None when no host
    normalizes (mirror of `_own_url_cited_runs`'s no-false-zero contract)."""
    own = {h for h in (normalize_host(x) for x in hosts or [] if x) if h}
    if not own:
        return None
    count = 0
    for run in raw_runs or []:
        if not isinstance(run, Mapping):
            continue
        for src in _identify_run_sources(dict(run)) or []:
            host = normalize_host((src.get("host") or "").strip())
            if host and host in own:
                count += 1
                break
    return count


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
# `_build_per_sku_base_query_specs`). The coarse branded-vs-category split must
# describe the partition the PROBE ACTUALLY RAN UNDER: the fanout picks its scan
# mode with `_scan_mode_for_query_spec`, which asks
# `intent_axis_for(query, axis) in BRANDED_INTENTS`. So the report's split is
# derived from the same predicate, by construction — see the intent taxonomy
# below. The old implementation was a one-value whitelist (only the literal
# "category" was non-branded), which filed every unbranded discovery shape the
# generator emits (sidewalk / attribute / head / problem / dupe) — and every
# missing axis — as branded, inflating "found when shoppers name you" with
# probes that ran under organic category visibility.
QUERY_CLASS_BRANDED = "branded_navigational"
QUERY_CLASS_CATEGORY = "category_discovery"

# The axis value stamped when a record carries no axis of its own. It must NOT
# be a real branded axis: an unknown axis is classified as discovery so a
# degraded/unstamped record can never inflate a brand's own numbers.
AXIS_UNCLASSIFIED = "unclassified"

# Fine intent-axis taxonomy (Step 2) — a per-query INTENT classification layered
# on top of the coarse `axis`, so the report shows citation performance by the
# WAY shoppers ask (head term vs problem/need vs constraint vs trust vs
# navigational), not just branded-vs-category. Classified from (query, axis) at
# report-build time — additive, no probe-pipeline change. MOVED here from the
# god module so the coarse class and the scan-mode partition cannot drift.
INTENT_AXES = ("category_head", "problem_jtbd", "constraint", "trust", "navigational", "custom")

# Non-branded DISCOVERY intents — where a brand wins NEW demand inside frontier
# models ("best hair oil", "best hair oil for damaged hair"). vs BRANDED intents
# (by name / "is X legit") which are low-value: a shopper who types a product
# name already found it elsewhere, so being cited there isn't the prize.
DISCOVERY_INTENTS = frozenset({"category_head", "problem_jtbd", "constraint"})
BRANDED_INTENTS = frozenset({"navigational", "trust"})

# Axes whose query NAMES the product or brand -> navigational. `price` and
# `brand` join intent/identity here: "<brand> collagen price", "buy <brand>
# online" are branded lookups, the same set `sku_opportunity._query_class` and
# `report_summary_builder._SOV_BRANDED_AXES` already treat as branded.
_NAVIGATIONAL_AXES = frozenset({"intent", "identity", "price", "brand"})
# Values that mean "no axis was stamped" — fail UNBRANDED (discovery).
def intent_axis_for(query: Optional[str], axis: Optional[str]) -> str:
    a = str(axis or "").strip().lower()
    q = str(query or "").strip().lower()
    if a in INTENT_AXES:
        # Idempotent over its own output: some producers/stored reports stamp
        # the already-normalized fine name rather than the coarse axis (see
        # report_summary_builder._SOV_BRANDED_AXES, which carries both
        # vocabularies). Classifying "trust"/"navigational" as discovery would
        # silently demote a genuinely branded prompt.
        return a
    if a in _NAVIGATIONAL_AXES:
        return "navigational"
    if a == "review":
        return "trust"
    if a in ("attribute", "sidewalk"):
        return "constraint"
    if a == "category":
        # need/problem-framed ("best X for sleep", "what helps with X", "X for women")
        # vs a bare head term ("best X", "top X", "X reviews").
        if q.startswith("what helps") or " for " in q:
            return "problem_jtbd"
        return "category_head"
    # Fail UNBRANDED. An unstamped / unknown / AXIS_UNCLASSIFIED axis lands here:
    # a query we cannot classify must never be counted as one that named the
    # brand, which would inflate the merchant's own branded coverage.
    return "category_head"


def query_class_for_axis(axis: Optional[str], query: Optional[str] = None) -> str:
    """Coarse branded/category class for a (query, axis) spec. Branded IFF the
    fine intent axis is one the probe would have run under the BRANDED scan
    mode — same predicate as `_scan_mode_for_query_spec`."""
    return (
        QUERY_CLASS_BRANDED
        if intent_axis_for(query, axis) in BRANDED_INTENTS
        else QUERY_CLASS_CATEGORY
    )


def run_query_class(run: Dict[str, Any]) -> str:
    meta = run.get("axis_metadata") if isinstance(run.get("axis_metadata"), dict) else {}
    query = run.get("normalized_query") or run.get("query")
    return query_class_for_axis(meta.get("axis"), query)


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
    the three bases the legacy implementations silently mixed. Every base
    counts only runs that returned a real answer: upstream-errored runs
    (:func:`run_errored`) are excluded before any counting.
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


# Deep-tier comparison identity. Relocated here from services/deep_tier_prompts
# (same pattern as QUERY_CLASS_* and intent_axis_for): the fact layer must be
# able to exclude internal-comparison runs without importing the prompt
# generator, which already imports this module. deep_tier_prompts re-exports
# both names, so its existing consumers are unchanged.
DEEP_TIER_PROMPT_SOURCE = "deep_tier"
COMPARISON_AXIS = "comparison"


def run_is_internal_comparison(run: Any) -> bool:
    """True for a probe run generated by a deep-tier comparison block —
    internal-first (founder 2026-07-21): merchant-facing report surfaces must
    not read these until competitor-name quality is validated on the pilot.
    Requires the deep-tier source stamp AND the comparison axis (both ride
    axis_metadata); a bare axis="comparison" from any other path keeps its
    existing behavior."""
    meta = run.get("axis_metadata") if isinstance(run, dict) else None
    if not isinstance(meta, dict):
        return False
    return (
        str(meta.get("axis") or "").strip().lower() == COMPARISON_AXIS
        and str(meta.get("prompt_source") or "").strip() == DEEP_TIER_PROMPT_SOURCE
    )


def run_errored(run: Any) -> bool:
    """True when a probe run carried an upstream error instead of an answer —
    the gateway returns HTTP 200 with `raw` prefixed `__error__:` (e.g. a 429
    quota error) rather than raising. Such a run is not grounded evidence in
    EITHER direction, so it must never sit in a fact-layer denominator: a
    provider lane erroring wholesale otherwise dilutes every mention rate
    (2026-07-21 scale smoke, run 509cf81c — all 143 Gemini runs errored).

    The canonical predicate — audit_run_worker, the report service, and
    deep_tier_prompts all import it from here; this module is a pure leaf
    they may all import."""
    if not isinstance(run, Mapping):
        return False
    if run.get("error"):
        return True
    raw = run.get("raw")
    return isinstance(raw, str) and raw.startswith("__error__")


def compute_run_facts(
    raw_runs: Sequence[Mapping[str, Any]],
    *,
    merchant_host: Optional[str],
    merchant_brand: Optional[str] = None,
    merchant_vendors: Optional[Tuple[str, ...]] = None,
    include_internal_comparison: bool = False,
) -> RunFacts:
    """Walk every run's grounding sources ONCE and derive all three fact tiers.

    Runs are grouped into prompt groups by their ``query`` (case-insensitive);
    runs without a query share the ``""`` group (they still count in run-level
    rollups). Within-run source dedup follows ``_identify_run_sources``.

    Upstream-errored runs (:func:`run_errored`) are excluded from EVERY rollup:
    ``run_count``, ``provider_coverage``, and prompt grouping (a query whose
    runs all errored yields no PromptFacts row at all, never a false
    own_url_cited=False / brand_mentioned=False verdict). An errored run is no
    evidence in either direction, so counting it dilutes every rate consumers
    derive from these denominators.

    Parity anchors (what each rollup must equal during the phase-1 window):
      * ``own_url_cited_runs``   == legacy ``_own_url_cited_runs``          (T1)
      * ``brand_mentioned_runs`` == ``extract_cited_hosts``'s
        ``merchant_cited_runs``                                             (T3)
      * ``runs_with_citations``  == ``extract_cited_hosts``'s
        ``runs_with_any_citation``
    These compare cited-run NUMERATORS; an errored run carries no grounding
    sources, so it can never contribute to either side and the anchors hold
    with or without the exclusion.
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
        if run_errored(run):
            continue
        # Deep-tier comparison probes are internal-first: every rollup here
        # feeds merchant-facing surfaces, so they are excluded by default.
        # The deep-tier rollup itself opts back in.
        if not include_internal_comparison and run_is_internal_comparison(run):
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
# `instrumented` to True when its comparison lands. Deliberately env-less and
# constant: this list IS the cutover checklist.
#
# `mode` states what the comparison MEANS:
#   "drift"   — same definition; values must be equal. Logged as a WARNING
#               (RUNFACTS_PARITY_DRIFT) on mismatch; a drift-free week is the
#               cutover gate for the site.
#   "measure" — the legacy definition intentionally differs from the decision-
#               sheet target tier; the comparison QUANTIFIES the gap for the
#               founder sign-off. Logged at INFO (RUNFACTS_PARITY_MEASURE) on
#               every computation, agreement and disagreement alike — cutover
#               here is a product decision, not a zero-drift gate.
LEGACY_CITEDNESS_SITES: Tuple[Dict[str, Any], ...] = (
    {"id": 1, "site": "bd_report._source_matches_merchant", "tier": "T3-matcher",
     "definition": "host OR brand-in-title OR alias", "instrumented": True,
     "mode": "drift", "rewired": True,
     "notes": "ALREADY UNIFIED (confirmed 2026-07-04): a single implementation in"
              " audit_facts (moved from the god module) is the ONE merchant-source"
              " matcher — called by extract_cited_hosts (site 2), build_authority_map,"
              " AND RunFacts' _source_fact. Since site 2's T3 parity is 0-drift and"
              " both its sides call this fn, site 1 is transitively proven consistent."
              " No duplicate to collapse"},
    {"id": 2, "site": "bd_report.extract_cited_hosts.merchant_cited_runs", "tier": "T3",
     "definition": ">=1 source matches merchant", "instrumented": True,
     "mode": "drift",
     "notes": "main verdict path REWIRED to run_facts (slice 1);"
              " sku_opportunity.prompt_group.* REWIRED 2026-07-04 (Anuko 0 drift /14"
              " groups). Still on legacy + log-only instrumented pending drift:"
              " bd_report.prompt_lane.* (per-prompt lane classifier — brand-report"
              " path, NOT yet exercised by a url-audit run). extract_cited_hosts"
              " itself STAYS — it supplies the competitor rollup"},
    {"id": 3, "site": "bd_report._own_url_cited_runs", "tier": "T1",
     "definition": "own domain == resolved source host", "instrumented": True,
     "mode": "drift", "rewired": True,
     "notes": "REWIRED (verdict path reads run_facts.own_url_cited_runs since"
              " slice 1); FINALIZED 2026-07-04 — the legacy fn was removed from"
              " the god module's import surface (zero production callers"
              " remained) and now lives in audit_facts as the test-only"
              " equivalence oracle"},
    {"id": 4, "site": "bd_report.score_category_visibility", "tier": "mixed",
     "definition": "url_match OR title OR excerpt-triple", "instrumented": True,
     "mode": "measure", "decision": "keep-variant",
     "notes": "KEEP (decided 2026-07-04, like site 7): an intentional composite"
              " SUPERSET, not a duplicate. Credits a category match via"
              " url_match.in_grounding OR brand-in-title OR the excerpt-triple path"
              " (excerpt + llm_self_report + grounding source — the 'Gruns fix' for"
              " editorial citations where the brand is in the excerpt but not the"
              " grounding metadata). RunFacts T3 deliberately has no excerpt/"
              " self-report analogue, so collapsing to T3 would DROP signal and lower"
              " category scores. The parity_measure stays to quantify the title_match"
              " sub-component's matcher gap only"},
    {"id": 5, "site": "bd_report.build_channel_appearance.own_site_cited", "tier": "T1",
     "definition": "RunFacts source-walk T1 per prompt (rewired)", "instrumented": True,
     "mode": "measure", "rewired": True,
     "notes": "REWIRED 2026-07-04 (first phase-2 cutover, shipped early as a"
              " defect fix — decision sheet already said 'T1, keep'): the"
              " own-site row now reads per-prompt own_url_cited from the"
              " RunFacts source walk. The legacy top_cited_hosts scan was a"
              " COMPETITOR rollup that dropped own-domain sources whose label"
              " names the brand — measured on the 2026-07-04 DamDam run as"
              " 'Your site 0/14' displayed vs 13/14 true"},
    {"id": 6, "site": "bd_report._citation_by_intent", "tier": "T3",
     "definition": "per-prompt merchant_cited_runs > 0", "instrumented": True,
     "mode": "drift",
     "notes": "pure view over site 9's per-prompt source_summary — checked as"
              " an internal-consistency equality, must never drift"},
    {"id": 7, "site": "bd_report.compute_citation_score.first_party_rate", "tier": "T1-variant",
     "definition": "merchant PDP cited as PRIMARY grounding + not negative-verdict",
     "instrumented": True, "mode": "measure",
     "notes": "measured vs own_url_cited_runs_any over _first_party_url_candidates"
              " hosts — quantifies the primary/negative gating + multi-host gap"
              " the 'unify the two own-domain matchers' cutover must decide on"},
    {"id": 8, "site": "bd_report._citation_signals (endorsement/findability)", "tier": "T2",
     "definition": "name-gated citation_role (cites_exact_sku/near_variant)",
     "instrumented": True, "mode": "measure", "rewired": True,
     "notes": "REWIRED 2026-07-04 (founder decision: brand-named counts). The"
              " rendered endorsement set + derived flags now come from the RunFacts"
              " T2 fold (endorsement-role host that names the BRAND); the SKU-name"
              " gate produced false 'no endorsement' negatives (Anuko: legacy [] vs"
              " RunFacts ['hwahae.com'], contradicting the report's own host table)."
              " build_authority_map overlays it per-SKU + brand; legacy value stashed"
              " as endorsement_hosts_legacy for the parity tripwire. Verdict does NOT"
              " depend on endorsement (score-driven), so no verdict change"},
    {"id": 9, "site": "sku_opportunity per-prompt verdicts", "tier": "T3-soft",
     "definition": "free-text mention in answer body (merchant_mention -> win/partial)",
     "instrumented": True, "mode": "measure",
     "notes": "per-SKU rollup measured: prompts with a win/partial provider"
              " verdict vs prompts with source-walk brand_mentioned — the"
              " answer-text-vs-source-walk gap the decision sheet's T3 row"
              " resolves"},
    {"id": 10, "site": "citation_operator_service", "tier": "T3-soft",
     "definition": "own thresholds + substring match", "instrumented": False,
     "mode": "measure", "decision": "defer",
     "notes": "DEFER (2026-07-04): a SEPARATE probe-scan product (run_citation_scan"
              " runs its own probes + extract_cited_hosts over them), not the audit"
              " report path. Rewiring needs stamped run_facts on ITS scan payloads"
              " first — distinct plumbing, no drift evidence, low traffic. Not a"
              " quick collapse; left as-is"},
    {"id": 11, "site": "task_queue_service proof-of-done", "tier": "T2-ish",
     "definition": "endorsement-host-now-cites", "instrumented": True,
     "mode": "measure", "rewired": True,
     "notes": "REWIRED 2026-07-04: reverify_outreach_records now reads the ONE"
              " endorsement set (host_attribution_summary.endorsement_hosts = the"
              " RunFacts T2 fold post site-8) instead of re-deriving a divergent"
              " SKU-name gate off per-host rows. Proof-of-done now shares the same"
              " brand-grain endorsement definition as the rendered 'independently"
              " recommended' card; a competitor-only citation still can't false-flip"
              " (not in the T2 set)"},
    {"id": 12, "site": "co_occurrence_finder._brand_in_text", "tier": "T3-soft",
     "definition": "manually-synced copy of site 4's matcher", "instrumented": False,
     "mode": "drift", "decision": "keep-variant",
     "notes": "KEEP (2026-07-04): a word-boundary brand matcher with a deliberate"
              " 4-char threshold (false-positive guard on fetched ARTICLE text,"
              " mirrors site 4). The shared brand_alias.text_mentions_brand uses a"
              " looser 3-char _MIN_ALIAS_LEN, so swapping would REGRESS the guard;"
              " and it matches arbitrary claimed competitor brands (no alias sets"
              " to derive). Unify only if the shared matcher gains a configurable"
              " threshold"},
)


# In-process parity rollup. The cutover's safety gate is "review the drift
# distribution per site before flipping a Type-B (number-changing) site" — but
# the RUNFACTS_PARITY_* logs go to a logger the prod host doesn't surface, so
# that gate was unobservable. This aggregator makes it queryable via
# GET /admin/runfacts-parity: per site, how many times it ran, how often legacy
# and RunFacts DISAGREED, and the last disagreeing sample. Bounded, cheap,
# never blocks a report. Resets on process restart (a recent-traffic signal,
# not an audit ledger).
_PARITY_STATS: Dict[str, Dict[str, Any]] = {}

# W7 C3: sites that have already pushed a drift alert this process lifetime. A
# parity_check drift is a Type-A invariant violation (the numbers were supposed to
# be EQUAL) — it must page, not sit in a warning log Railway hides. But parity_check
# runs in hot per-prompt loops, so we alert ONCE per site per process (magnitude for
# every drift still accumulates in _PARITY_STATS / GET /admin/runfacts-parity).
# Resets on restart, like _PARITY_STATS.
_ALERTED_PARITY_SITES: set = set()


def _alert_parity_drift(site: str, legacy_value: Any, facts_value: Any) -> None:
    """Escalate a parity_check drift to Sentry (the reliable sink — app WARNING/ERROR
    logs are hidden on Railway). Best-effort, first-time-per-site, never raises."""
    if site in _ALERTED_PARITY_SITES:
        return
    _ALERTED_PARITY_SITES.add(site)
    try:
        import sentry_sdk

        sentry_sdk.capture_message(
            f"RUNFACTS_PARITY_DRIFT (Type-A invariant) site={site} "
            f"legacy={legacy_value!r} run_facts={facts_value!r}",
            level="error",
        )
    except Exception:  # noqa: BLE001 — alerting must never break a report
        pass


def _record_parity(site: str, legacy_value: Any, facts_value: Any, equal: bool) -> None:
    try:
        row = _PARITY_STATS.get(site)
        if row is None:
            row = {"checks": 0, "drifts": 0, "last_drift": None}
            _PARITY_STATS[site] = row
        row["checks"] += 1
        if not equal:
            row["drifts"] += 1
            # Keep only a small string sample so this never grows unbounded.
            row["last_drift"] = {
                "legacy": repr(legacy_value)[:120],
                "run_facts": repr(facts_value)[:120],
            }
    except Exception:  # noqa: BLE001 — telemetry must never break a report
        pass


def parity_stats_snapshot() -> Dict[str, Dict[str, Any]]:
    """Per-site parity rollup since process start: {site: {checks, drifts,
    drift_rate, last_drift}}. Drives GET /admin/runfacts-parity so the founder
    can SEE each site's drift distribution before signing off a Type-B cutover
    (the drift the logs emit but the host doesn't surface)."""
    out: Dict[str, Dict[str, Any]] = {}
    for site, row in _PARITY_STATS.items():
        checks = int(row.get("checks") or 0)
        drifts = int(row.get("drifts") or 0)
        out[site] = {
            "checks": checks,
            "drifts": drifts,
            "drift_rate": round(drifts / checks, 4) if checks else 0.0,
            "last_drift": row.get("last_drift"),
        }
    return out


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

    Grep target: ``RUNFACTS_PARITY_DRIFT``; queryable via parity_stats_snapshot.
    A drift-free window on a site is the evidence that lets phase 2 rewire it to
    RunFacts and delete the legacy path.
    """
    try:
        equal = legacy_value == facts_value
        _record_parity(site, legacy_value, facts_value, equal)
        if not equal:
            # ERROR, not warning: a parity_check drift is an invariant violation
            # (these were supposed to be EQUAL) — an alarm, per this function's
            # contract vs parity_measure. W7 C3 also pushes it to Sentry.
            logger.error(
                "RUNFACTS_PARITY_DRIFT site=%s legacy=%r run_facts=%r context=%s",
                site,
                legacy_value,
                facts_value,
                dict(context) if context else {},
            )
            _alert_parity_drift(site, legacy_value, facts_value)
        return equal
    except Exception:  # noqa: BLE001 — parity logging must never break a report
        logger.warning("RUNFACTS_PARITY_CHECK_FAILED site=%s", site, exc_info=True)
        return False


def parity_measure(
    site: str,
    legacy_value: Any,
    facts_value: Any,
    *,
    context: Optional[Mapping[str, Any]] = None,
) -> None:
    """Phase-1 gap measurement for sites whose legacy definition intentionally
    differs from the decision-sheet target tier (mode="measure" in
    :data:`LEGACY_CITEDNESS_SITES`). Unlike :func:`parity_check` this is NOT an
    alarm: it records every computation — agreement included — so the drift
    distribution behind each displayed-number change is visible (via
    parity_stats_snapshot / GET /admin/runfacts-parity) before the founder signs
    off the cutover.

    Grep target: ``RUNFACTS_PARITY_MEASURE``.
    """
    try:
        equal = legacy_value == facts_value
        _record_parity(site, legacy_value, facts_value, equal)
        logger.info(
            "RUNFACTS_PARITY_MEASURE site=%s legacy=%r run_facts=%r equal=%s context=%s",
            site,
            legacy_value,
            facts_value,
            equal,
            dict(context) if context else {},
        )
    except Exception:  # noqa: BLE001 — measurement must never break a report
        logger.warning("RUNFACTS_PARITY_MEASURE_FAILED site=%s", site, exc_info=True)
