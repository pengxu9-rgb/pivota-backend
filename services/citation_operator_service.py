"""
Provider-aware AI-citation operator.

Turns the diagnostic audit into an operator loop:

  1. SCAN     — run grounded probes across providers (chatgpt + gemini today;
                perplexity/claude later) for a merchant's category queries,
                classify every cited source into a CHANNEL, and record the
                sources the merchant is ABSENT from as `citation_targets`.
  2. ACT      — the merchant takes a move to close a target (post a reddit
                reply, publish an owned FAQ, etc). We record it as a
                `citation_action` and, once posted, start a revisit clock.
  3. RE-CHECK — on a cadence tuned to ChatGPT/Gemini re-index latency, re-probe
                and record whether the assistant now cites the merchant
                (`citation_action_outcomes`). This is the tracking spine:
                "merchant did X -> revisit later -> show whether AI now cites them."

Why provider-aware? Spike (2026-06-09) proved which sources an LLM cites is almost
entirely a function of WHICH assistant the buyer uses:
  - Gemini  -> retailers (Olive Young), beauty editorial, YouTube. ~0 Reddit.
  - ChatGPT -> Reddit (dominant) + clinical/scientific + review sites.
So the operator playbook routes by (provider, channel).

The live probe call is isolated in `_probe()` so the pure classification + gap
logic is unit-testable without hitting any LLM.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from db.database import database
from services import agent_center_llm_client
from services import credit_consumption_service as ccs
from services.agent_center_bd_report_service import extract_cited_hosts, normalize_host
from services.cited_host_classifier import classify_host

logger = logging.getLogger(__name__)

# Providers covering the majority of consumer AI users. Perplexity/Claude later
# (the probe path already takes a provider arg, so adding them is config-only).
DEFAULT_PROVIDERS = ("gemini", "chatgpt")

CITATION_SCAN_MODE = "category_visibility_test"

# Channels (citation source types) we route operator work by.
CHANNEL_COMMUNITY = "community"
CHANNEL_EDITORIAL = "editorial"
CHANNEL_RETAILER = "retailer"
CHANNEL_SCIENTIFIC = "scientific"
CHANNEL_REVIEW = "review"
CHANNEL_VIDEO = "video"
CHANNEL_OTHER = "other"

# Revisit cadence (days AFTER the merchant posts) at which we re-probe to see if
# the assistant now cites the change. LANE-AWARE — re-index latency differs by
# (provider, channel), per the 2026-06-09 latency research:
#   - Reddit/community on ChatGPT: OpenAI's real-time Reddit Data API deal makes
#     fresh threads citable in days, not weeks -> tight loop, definitive by ~Day 14.
#   - Gemini (any channel): grounds via Google Search = crawl + index + RE-RANK
#     -> 2-6 weeks; nothing useful before ~Day 14.
#   - Default (ChatGPT/Bing lane, owned content, esp. with IndexNow): ~Day 7 onward.
# Give-up = no cadence window remaining (community ~day 14; other lanes ~day 60).
# Instrument real publish->citation telemetry (citation_action_outcomes) and
# recalibrate these priors from first-party data.
# Community/ChatGPT (Reddit real-time API deal): definitive by ~day 14 -> 3 tight checks.
_CADENCE_COMMUNITY_CHATGPT = (3, 7, 14)
# Gemini/owned (Google crawl + RE-RANK): checkpoints out to ~day 60 before giving up.
# Past the last window, non-citation is a relevance/quality problem, not latency
# (~half of AI citations go to <13-week-old content). Give-up = no window remaining.
_CADENCE_GEMINI = (14, 28, 45, 60)
_CADENCE_DEFAULT = (7, 21, 42, 60)


def recheck_cadence_days(provider: Optional[str], channel: Optional[str]) -> tuple:
    """Day-offsets (from posting) for the revisit loop, by lane."""
    p = (provider or "").strip().lower()
    c = (channel or "").strip().lower()
    if c == CHANNEL_COMMUNITY and p == "chatgpt":
        return _CADENCE_COMMUNITY_CHATGPT
    if p == "gemini":
        return _CADENCE_GEMINI
    return _CADENCE_DEFAULT

# Channel keyword sets. Checked BEFORE the curated registry because the registry
# misclassifies some community hosts (reddit.com is registry type 'video'):
# community/scientific/review/video intent must win first.
_COMMUNITY_KEYS = ("reddit", "quora", "stackexchange", "stack exchange", "forum", "/r/")
_SCIENTIFIC_KEYS = (
    "pubmed", "ncbi.nlm.nih.gov", "pmc.", "sciencedirect", "mdpi.com", "nih.gov",
    "frontiersin", "sagepub", "arxiv", "cochrane", "medscape", "journals.",
    ".edu", "kci.go.kr", "cureus", "researchgate",
)
_REVIEW_KEYS = ("webmd", "healthline", "examine.com", "trustpilot", "verywell")
_VIDEO_KEYS = ("youtube", "youtu.be", "tiktok", "vimeo")
_EDITORIAL_KEYS = (
    "vogue", "elle", "allure", "byrdie", "whowhatwear", "cosmopolitan", "glamour",
    "harpersbazaar", "nymag", "thecut", "buzzfeed", "popsugar", "refinery29",
    "instyle", "marieclaire", "goodhousekeeping", "forbes", "thegoodtrade",
    "wirecutter", "mindbodygreen", "prevention", "womenshealthmag",
)
# Includes space-separated variants because Gemini grounding returns human titles
# ("Olive Young Global") not hosts, while ChatGPT returns hosts ("oliveyoung.com").
_RETAILER_KEYS = (
    "oliveyoung", "olive young", "sephora", "ulta", "amazon", "walmart", "target.com",
    "yesstyle", "stylevana", "nykaa", "iherb", "stylekorean", "style korean", "revolve",
    "nordstrom", "macys", "etsy", "ebay", "noticemestore", "kbeautystore", "aubeautybazaar",
)

_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)", re.I)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure logic (unit-testable, no IO)
# ---------------------------------------------------------------------------

def classify_channel(host_or_label: Optional[str]) -> str:
    """Map a cited source (host or grounding label) to an operator channel."""
    if not host_or_label:
        return CHANNEL_OTHER
    s = str(host_or_label).strip().lower()
    if s.startswith("r/"):  # bare subreddit label, e.g. "r/Biohackers"
        return CHANNEL_COMMUNITY
    for keys, channel in (
        (_COMMUNITY_KEYS, CHANNEL_COMMUNITY),
        (_SCIENTIFIC_KEYS, CHANNEL_SCIENTIFIC),
        (_VIDEO_KEYS, CHANNEL_VIDEO),
        (_REVIEW_KEYS, CHANNEL_REVIEW),
        (_EDITORIAL_KEYS, CHANNEL_EDITORIAL),
        (_RETAILER_KEYS, CHANNEL_RETAILER),
    ):
        if any(k in s for k in keys):
            return channel
    # Fall back to the curated registry's host type.
    host = normalize_host(s) or s
    info = classify_host(host) or {}
    t = (info.get("type") or "").lower()
    if t in ("community", "forum"):
        return CHANNEL_COMMUNITY
    if t in ("editorial", "publisher"):
        return CHANNEL_EDITORIAL
    if t in ("retailer", "marketplace"):
        return CHANNEL_RETAILER
    if t in ("video", "social"):
        return CHANNEL_VIDEO
    return CHANNEL_OTHER


def _looks_like_host(value: Optional[str]) -> bool:
    """True for host-shaped labels (reddit.com, pubmed.ncbi.nlm.nih.gov) as ChatGPT
    returns, False for human titles ("Olive Young Global", "r/Biohackers") as Gemini
    returns. We must NOT normalize_host() a title — it fabricates a host with spaces."""
    s = (value or "").strip().lower()
    return bool(s) and " " not in s and "." in s and "/" not in s


def _target_key(merchant_id: str, provider: str, identity: str,
                sku_key: Optional[str], question_text: Optional[str]) -> str:
    raw = "|".join([
        merchant_id or "", provider or "", (identity or "").lower(),
        sku_key or "", (question_text or "")[:200],
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def community_threads(raw_runs: Sequence[Mapping[str, Any]]) -> Dict[str, Optional[str]]:
    """Distinct reddit thread URLs -> subreddit (or None) across grounding sources."""
    threads: Dict[str, Optional[str]] = {}
    for run in raw_runs or []:
        for s in (run.get("grounding_sources") or []):
            uri = (s.get("uri") or "") if isinstance(s, Mapping) else ""
            if "reddit.com" in uri.lower():
                m = _SUBREDDIT_RE.search(uri)
                threads.setdefault(uri, m.group(1) if m else None)
        for uri in (run.get("grounding_chunks") or []):
            if isinstance(uri, str) and "reddit.com" in uri.lower():
                m = _SUBREDDIT_RE.search(uri)
                threads.setdefault(uri, m.group(1) if m else None)
    return threads


def build_targets(
    *,
    provider: str,
    competitors: Mapping[str, int],
    raw_runs: Sequence[Mapping[str, Any]],
    merchant_id: str,
    sku_key: Optional[str] = None,
    question_text: Optional[str] = None,
    merchant_brand: Optional[str] = None,
    merchant_host: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Turn the cited-but-merchant-absent sources into citation_target rows.

    `competitors` is the {label: run_count} Counter from extract_cited_hosts
    (already excludes sources that matched the merchant), so every entry is a
    gap. Community targets get enriched with the actual reddit threads/subreddits.
    """
    threads = community_threads(raw_runs)
    targets: List[Dict[str, Any]] = []
    for label, cited_in_runs in competitors.items():
        label_s = str(label)
        channel = classify_channel(label_s)
        # ChatGPT labels ARE hosts; Gemini labels are human titles. Only derive a
        # host from the former — never fabricate one from a title (BUG: spaces).
        source_host = normalize_host(label_s) if _looks_like_host(label_s) else None
        identity = source_host or label_s.strip().lower()
        subreddit: Optional[str] = None
        engagement: Optional[Dict[str, Any]] = None
        # Enrich ONLY genuine Reddit targets with thread/subreddit detail — otherwise
        # a Quora/forum target gets stamped with unrelated subreddits.
        if channel == CHANNEL_COMMUNITY and "reddit" in label_s.lower() and threads:
            subs = sorted({s for s in threads.values() if s})
            subreddit = subs[0] if subs else None
            engagement = {"threads": list(threads.keys())[:25], "subreddits": subs}
        targets.append({
            "target_id": f"ctgt_{uuid.uuid4().hex[:16]}",
            "merchant_id": merchant_id,
            "sku_key": sku_key,
            "provider": provider,
            "channel": channel,
            "source_host": source_host,
            "source_label": label_s,
            "subreddit": subreddit,
            "question_text": question_text,
            "merchant_brand": merchant_brand,
            "merchant_host": merchant_host,
            "cited_in_runs": int(cited_in_runs),
            "engagement_jsonb": engagement,
            "target_key": _target_key(merchant_id, provider, identity, sku_key, question_text),
        })
    return targets


def channel_mix(targets: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    mix: Dict[str, int] = {}
    for t in targets:
        c = t.get("channel") or CHANNEL_OTHER
        mix[c] = mix.get(c, 0) + 1
    return mix


def evaluate_outcome(merchant_cited_runs: int) -> bool:
    """Brand is considered cited when the assistant attributed it in >=1 run."""
    return int(merchant_cited_runs or 0) > 0


# ---------------------------------------------------------------------------
# Live probe (isolated for mocking)
# ---------------------------------------------------------------------------

PROBE_CACHE_TTL_HOURS = 24


def _probe_cache_key(provider: str, scan_mode: str, context: Mapping[str, Any]) -> str:
    """Stable hash over what actually determines the grounded result. Queries are
    SORTED so order doesn't fragment the cache; product/pdp scope it per merchant."""
    import json as _json
    canonical = _json.dumps({
        "provider": provider,
        "scan_mode": scan_mode,
        "queries": sorted(str(q) for q in (context.get("queries") or [])),
        "product": context.get("product") or {},
        "merchant_pdp_url": context.get("merchant_pdp_url"),
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _probe_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    """Fresh cached probe result, or None. Degrades to a miss on any DB error so
    the cache can never break a probe."""
    import json as _json
    try:
        cutoff = _utcnow() - timedelta(hours=PROBE_CACHE_TTL_HOURS)
        row = await database.fetch_one(
            "SELECT result_jsonb FROM citation_probe_cache "
            "WHERE cache_key = :k AND created_at > :cutoff",
            {"k": cache_key, "cutoff": cutoff},
        )
    except Exception as exc:
        logger.warning("probe cache read failed key=%s: %s", cache_key, exc)
        return None
    if not row:
        return None
    raw = dict(row)["result_jsonb"]
    return raw if isinstance(raw, dict) else _json.loads(raw)


async def _probe_cache_put(cache_key: str, provider: str, scan_mode: str,
                           result: Mapping[str, Any]) -> None:
    import json as _json
    try:
        await database.execute(
            """
            INSERT INTO citation_probe_cache (cache_key, provider, scan_mode, result_jsonb)
            VALUES (:k, :p, :s, CAST(:r AS JSONB))
            ON CONFLICT (cache_key) DO UPDATE SET
                result_jsonb = EXCLUDED.result_jsonb, created_at = now()
            """,
            {"k": cache_key, "p": provider, "s": scan_mode,
             "r": _json.dumps(result, default=str)},
        )
    except Exception as exc:  # cache write must never break the probe
        logger.warning("probe cache write failed key=%s: %s", cache_key, exc)


async def _probe(
    *,
    provider: str,
    queries: Sequence[str],
    merchant_id: str,
    merchant_pdp_url: Optional[str] = None,
    product: Optional[Mapping[str, Any]] = None,
    max_runs: Optional[int] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Run a grounded probe, with a 24h result cache for SCANS. Rechecks pass
    `use_cache=False` because they must measure citation change over time."""
    context: Dict[str, Any] = {"queries": list(queries)}
    if merchant_pdp_url:
        context["merchant_pdp_url"] = merchant_pdp_url
    if product:
        context["product"] = dict(product)

    cache_key = _probe_cache_key(provider, CITATION_SCAN_MODE, context)
    if use_cache:
        cached = await _probe_cache_get(cache_key)
        if cached is not None:
            cached["_from_cache"] = True  # caller meters on miss only (cache hit = no COGS)
            return cached

    result = await agent_center_llm_client.probe(
        scan_mode=CITATION_SCAN_MODE,
        scan_target_id=f"citation-scan-{uuid.uuid4().hex[:12]}",
        merchant_id=merchant_id,
        store_id=merchant_id,
        context=context,
        provider=provider,
        max_runs=int(max_runs or len(queries)),
    )
    # Cache (and flag as fresh) only real grounded responses — never a transient empty.
    if use_cache and isinstance(result, dict) and result.get("raw_runs"):
        await _probe_cache_put(cache_key, provider, CITATION_SCAN_MODE, result)
    if isinstance(result, dict):
        result["_from_cache"] = False
    return result


# ---------------------------------------------------------------------------
# Persistence + orchestration (IO)
# ---------------------------------------------------------------------------

def visibility_score(runs: int, merchant_cited_runs: int) -> int:
    """0-100: share of grounded runs that cited the merchant."""
    runs = int(runs or 0)
    return round(100 * int(merchant_cited_runs or 0) / runs) if runs else 0


async def _persist_scan_run(
    *, merchant_id: str, provider: str, sku_key: Optional[str], runs: int,
    merchant_cited_runs: int, channel_mix_map: Mapping[str, int], gap_count: int,
) -> None:
    import json as _json
    await database.execute(
        """
        INSERT INTO citation_scan_runs (
            scan_run_id, merchant_id, provider, sku_key, runs, merchant_cited_runs,
            visibility_score, channel_mix_jsonb, gap_count
        ) VALUES (
            :id, :mid, :p, :sku, :runs, :mc, :vis, CAST(:cm AS JSONB), :gap
        )
        """,
        {"id": f"csr_{uuid.uuid4().hex[:16]}", "mid": merchant_id, "p": provider,
         "sku": sku_key, "runs": int(runs), "mc": int(merchant_cited_runs),
         "vis": visibility_score(runs, merchant_cited_runs),
         "cm": _json.dumps(dict(channel_mix_map)), "gap": int(gap_count)},
    )


async def _upsert_target(t: Mapping[str, Any]) -> None:
    import json as _json
    await database.execute(
        """
        INSERT INTO citation_targets (
            target_id, merchant_id, sku_key, provider, channel, source_host,
            source_label, subreddit, question_text, merchant_brand, merchant_host,
            cited_in_runs, engagement_jsonb, status, target_key
        ) VALUES (
            :target_id, :merchant_id, :sku_key, :provider, :channel, :source_host,
            :source_label, :subreddit, :question_text, :merchant_brand, :merchant_host,
            :cited_in_runs, CAST(:engagement_jsonb AS JSONB), 'discovered', :target_key
        )
        ON CONFLICT (target_key) DO UPDATE SET
            cited_in_runs = EXCLUDED.cited_in_runs,
            source_label  = EXCLUDED.source_label,
            subreddit     = COALESCE(EXCLUDED.subreddit, citation_targets.subreddit),
            merchant_brand = COALESCE(EXCLUDED.merchant_brand, citation_targets.merchant_brand),
            merchant_host  = COALESCE(EXCLUDED.merchant_host, citation_targets.merchant_host),
            engagement_jsonb = COALESCE(EXCLUDED.engagement_jsonb, citation_targets.engagement_jsonb),
            updated_at    = now()
        """,
        {
            **{k: t.get(k) for k in (
                "target_id", "merchant_id", "sku_key", "provider", "channel",
                "source_host", "source_label", "subreddit", "question_text",
                "merchant_brand", "merchant_host", "cited_in_runs", "target_key",
            )},
            "engagement_jsonb": _json.dumps(t.get("engagement_jsonb")) if t.get("engagement_jsonb") else None,
        },
    )


SCAN_OPERATION_TYPE = "agent_citation_scan"


async def run_citation_scan(
    *,
    merchant_id: str,
    queries: Sequence[str],
    merchant_brand: Optional[str] = None,
    merchant_host: Optional[str] = None,
    sku_key: Optional[str] = None,
    product: Optional[Mapping[str, Any]] = None,
    merchant_pdp_url: Optional[str] = None,
    providers: Sequence[str] = DEFAULT_PROVIDERS,
    max_runs: Optional[int] = None,
    persist: bool = True,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a provider-aware citation scan; return {billing_mode, providers:{...}}.

    A scan fans REAL grounded probes across providers (COGS), so it is paid-tier
    gated UP FRONT — free-tier merchants spend nothing (no probes) and get a
    `not_paid_tier` envelope. Each provider's probes are metered via the canonical
    consume() path (idempotent per scan+provider). Persists discovered gaps as
    citation_targets (idempotent on target_key).
    """
    if not await ccs.merchant_is_paid_tier(merchant_id):
        return {"billing_mode": "preview_only", "reason": "not_paid_tier", "providers": {}}

    question_text = ("; ".join(queries))[:500] if queries else None
    # Deterministic default so a client RETRY of the same scan reuses the same
    # per-provider metering keys (a uuid default would defeat idempotency and
    # double-charge on every retry). The UTC-date bucket bounds the dedup window
    # to one day: same-day retries are idempotent, a fresh scan next day re-bills
    # (matching the 24h probe cache). Callers wanting a fresh charge pass their own.
    day_bucket = _utcnow().date().isoformat()
    base_key = idempotency_key or hashlib.sha256(
        "|".join([merchant_id, sku_key or "", question_text or "",
                  ",".join(sorted(providers)), day_bucket]).encode("utf-8")
    ).hexdigest()[:32]
    providers_summary: Dict[str, Any] = {}
    any_metered = False
    for provider in providers:
        try:
            result = await _probe(
                provider=provider, queries=queries, merchant_id=merchant_id,
                merchant_pdp_url=merchant_pdp_url, product=product, max_runs=max_runs,
            )
        except Exception as exc:  # one provider failing must not sink the others
            logger.warning("citation_scan probe failed provider=%s merchant=%s: %s",
                           provider, merchant_id, exc)
            providers_summary[provider] = {"error": str(exc), "targets": []}
            continue
        raw_runs = result.get("raw_runs") or []
        competitors, merch_cited, runs_with_cite = extract_cited_hosts(
            raw_runs, merchant_host=merchant_host, merchant_brand=merchant_brand,
        )
        targets = build_targets(
            provider=provider, competitors=competitors, raw_runs=raw_runs,
            merchant_id=merchant_id, sku_key=sku_key, question_text=question_text,
            merchant_brand=merchant_brand, merchant_host=merchant_host,
        )
        if persist:
            for t in targets:
                await _upsert_target(t)
        # Meter ONLY on a real probe (cache miss). A cache hit incurred no COGS, so
        # billing must not fire — this also removes the midnight-boundary double-bill
        # the date-bucket key would otherwise allow on a cached retry.
        if result.get("_from_cache"):
            meter = {"billing_mode": "cached", "credits": 0}
        else:
            try:
                meter = await ccs.meter_agent_workflow(
                    merchant_id, SCAN_OPERATION_TYPE, provider=provider,
                    units=len(queries), idempotency_key=f"citation_scan:{base_key}:{provider}",
                )
            except Exception as exc:  # billing error must not lose the scan results
                logger.warning("citation_scan metering failed provider=%s merchant=%s: %s",
                               provider, merchant_id, exc)
                meter = {"billing_mode": "preview_only", "credits": 0, "reason": "metering_failed"}
        if meter.get("billing_mode") == "metered":
            any_metered = True
        cmix = channel_mix(targets)
        # Snapshot per FRESH scan with REAL data for the dashboard/trend. Skip
        # cached scans (would duplicate trend points) AND transient-empty probes
        # (`raw_runs` falsy) — a blip must not inject a bogus 0% point that then
        # becomes the "latest" headline. Best-effort: a snapshot failure must not
        # 500 a scan the merchant was already charged for.
        if persist and raw_runs and not result.get("_from_cache"):
            try:
                await _persist_scan_run(
                    merchant_id=merchant_id, provider=provider, sku_key=sku_key,
                    runs=len(raw_runs), merchant_cited_runs=merch_cited,
                    channel_mix_map=cmix, gap_count=len(targets),
                )
            except Exception as exc:
                logger.warning("scan snapshot persist failed provider=%s merchant=%s: %s",
                               provider, merchant_id, exc)
        providers_summary[provider] = {
            "runs": len(raw_runs),
            "runs_with_any_citation": runs_with_cite,
            "merchant_cited_runs": merch_cited,
            "visibility_score": visibility_score(len(raw_runs), merch_cited),
            "channel_mix": cmix,
            "targets": targets,
            "billing": {"billing_mode": meter.get("billing_mode", "preview_only"),
                        "credits": int(meter.get("credits", 0) or 0)},
        }
    return {"billing_mode": "metered" if any_metered else "preview_only",
            "providers": providers_summary}


async def record_merchant_action(
    *,
    target_id: str,
    merchant_id: str,
    action_type: str,
    draft_content: Optional[str] = None,
    draft_model: Optional[str] = None,
    draft_credits: Optional[float] = None,
    posted_url: Optional[str] = None,
    sku_key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    """Record a merchant action against a target.

    If `posted_url` is given the action starts in `measuring` and the revisit
    clock is set; otherwise it's a `draft`. Idempotent on idempotency_key.
    """
    now = now or _utcnow()
    idempotency_key = idempotency_key or f"{target_id}:{action_type}:{posted_url or 'draft'}"

    existing = await database.fetch_one(
        "SELECT action_id FROM citation_actions WHERE idempotency_key = :k",
        {"k": idempotency_key},
    )
    if existing:
        return dict(existing)["action_id"]

    action_id = f"cact_{uuid.uuid4().hex[:16]}"
    if posted_url:
        target_row = await database.fetch_one(
            "SELECT provider, channel FROM citation_targets WHERE target_id=:tid",
            {"tid": target_id},
        )
        target = dict(target_row) if target_row else {}
        cadence = recheck_cadence_days(target.get("provider"), target.get("channel"))
        status, posted_at = "measuring", now
        next_check_at = now + timedelta(days=cadence[0])
    else:
        status, posted_at, next_check_at = "draft", None, None

    await database.execute(
        """
        INSERT INTO citation_actions (
            action_id, target_id, merchant_id, sku_key, action_type, draft_content,
            draft_model, draft_credits, status, posted_url, posted_at,
            next_check_at, check_count, idempotency_key
        ) VALUES (
            :action_id, :target_id, :merchant_id, :sku_key, :action_type, :draft_content,
            :draft_model, :draft_credits, :status, :posted_url, :posted_at,
            :next_check_at, 0, :idempotency_key
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        {
            "action_id": action_id, "target_id": target_id, "merchant_id": merchant_id,
            "sku_key": sku_key, "action_type": action_type, "draft_content": draft_content,
            "draft_model": draft_model, "draft_credits": draft_credits, "status": status,
            "posted_url": posted_url, "posted_at": posted_at, "next_check_at": next_check_at,
            "idempotency_key": idempotency_key,
        },
    )
    await database.execute(
        "UPDATE citation_targets SET status='actioned', updated_at=now() WHERE target_id=:tid",
        {"tid": target_id},
    )
    return action_id


async def set_action_draft_credits(*, action_id: str, credits: int) -> None:
    """Record the credits actually charged for a draft. Set AFTER metering, because
    the draft service persists the action BEFORE the debit — so a metering failure
    leaves the merchant with a saved, uncharged draft rather than a charge with no
    artifact (merchant-favorable failure)."""
    await database.execute(
        "UPDATE citation_actions SET draft_credits=:c, updated_at=now() WHERE action_id=:aid",
        {"c": int(credits), "aid": action_id},
    )


async def mark_action_posted(
    *, action_id: str, merchant_id: str, posted_url: str, now: Optional[datetime] = None,
) -> None:
    """Transition a draft action to `measuring` once the merchant has posted,
    starting the revisit clock. Ownership is enforced at the data layer
    (`merchant_id` scoping) so this is safe even without a route pre-check."""
    now = now or _utcnow()
    row = await database.fetch_one(
        """
        SELECT t.provider AS provider, t.channel AS channel
          FROM citation_actions a JOIN citation_targets t ON t.target_id = a.target_id
         WHERE a.action_id = :aid AND a.merchant_id = :mid
        """,
        {"aid": action_id, "mid": merchant_id},
    )
    row = dict(row) if row else {}
    cadence = recheck_cadence_days(row.get("provider"), row.get("channel"))
    await database.execute(
        """
        UPDATE citation_actions
           SET status='measuring', posted_url=:url, posted_at=:now,
               next_check_at=:next, updated_at=now()
         WHERE action_id=:aid AND merchant_id=:mid
           AND status IN ('draft','in_review','approved')
        """,
        {"url": posted_url, "now": now, "next": now + timedelta(days=cadence[0]),
         "aid": action_id, "mid": merchant_id},
    )


async def due_for_recheck(*, now: Optional[datetime] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """The tracking spine: actions whose scheduled revisit is due."""
    now = now or _utcnow()
    rows = await database.fetch_all(
        """
        SELECT * FROM citation_actions
         WHERE status='measuring' AND next_check_at IS NOT NULL AND next_check_at <= :now
         ORDER BY next_check_at ASC
         LIMIT :limit
        """,
        {"now": now, "limit": int(limit)},
    )
    return [dict(r) for r in rows]


def _next_check_at(
    *, provider: Optional[str], channel: Optional[str],
    posted_at: datetime, check_count: int,
) -> Optional[datetime]:
    cadence = recheck_cadence_days(provider, channel)
    if check_count >= len(cadence):
        return None
    return posted_at + timedelta(days=cadence[check_count])


async def recheck_action(
    *,
    action_id: str,
    merchant_id: str,
    queries: Sequence[str],
    merchant_brand: Optional[str] = None,
    merchant_host: Optional[str] = None,
    product: Optional[Mapping[str, Any]] = None,
    merchant_pdp_url: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Re-probe the target's provider and record whether the assistant now cites
    the merchant. Advances the revisit schedule (confirmed | no_effect | next window).
    Scoped to `merchant_id` so it can't re-probe/charge against another merchant."""
    now = now or _utcnow()
    action_row = await database.fetch_one(
        "SELECT * FROM citation_actions WHERE action_id=:aid AND merchant_id=:mid",
        {"aid": action_id, "mid": merchant_id})
    if not action_row:
        raise ValueError(f"unknown action_id {action_id} for merchant {merchant_id}")
    action = dict(action_row)
    target_row = await database.fetch_one(
        "SELECT * FROM citation_targets WHERE target_id=:tid", {"tid": action["target_id"]})
    target = dict(target_row) if target_row else {}
    provider = target.get("provider") or "chatgpt"

    result = await _probe(
        provider=provider, queries=queries, merchant_id=action["merchant_id"],
        merchant_pdp_url=merchant_pdp_url, product=product, max_runs=len(queries),
        use_cache=False,  # rechecks must probe FRESH to detect citation change over time
    )
    # Meter the recheck probes — the revisit loop is real COGS too, not free.
    # Keyed on (action, check_count) so each scheduled window bills exactly once
    # and an overlapping/retried cron tick on the same window can't double-charge.
    try:
        await ccs.meter_agent_workflow(
            action["merchant_id"], SCAN_OPERATION_TYPE, provider=provider,
            units=len(queries),
            idempotency_key=f"citation_recheck:{action_id}:{int(action['check_count'])}",
        )
    except Exception as exc:  # billing failure must not break the recheck
        logger.warning("recheck metering failed action=%s: %s", action_id, exc)
    raw_runs = result.get("raw_runs") or []
    _competitors, merch_cited, _runs_with_cite = extract_cited_hosts(
        raw_runs, merchant_host=merchant_host, merchant_brand=merchant_brand)
    brand_cited = evaluate_outcome(merch_cited)
    # Independent signal: is the SPECIFIC target source still cited at recheck time?
    # (Not gated on brand_cited — they answer different questions.)
    target_host = target.get("source_host")
    source_cited = bool(target_host and _source_present(raw_runs, target_host))

    import json as _json
    outcome_id = f"cout_{uuid.uuid4().hex[:16]}"
    await database.execute(
        """
        INSERT INTO citation_action_outcomes (
            outcome_id, action_id, provider, brand_cited, source_cited,
            merchant_cited_runs, raw_jsonb
        ) VALUES (
            :outcome_id, :action_id, :provider, :brand_cited, :source_cited,
            :merchant_cited_runs, CAST(:raw_jsonb AS JSONB)
        )
        """,
        {"outcome_id": outcome_id, "action_id": action_id, "provider": provider,
         "brand_cited": brand_cited, "source_cited": source_cited,
         "merchant_cited_runs": int(merch_cited or 0),
         "raw_jsonb": _json.dumps({"merchant_cited_runs": merch_cited, "runs": len(raw_runs)})},
    )

    check_count = int(action["check_count"]) + 1
    posted_at = action["posted_at"] or now
    if brand_cited:
        new_status, next_at = "confirmed", None
    else:
        next_at = _next_check_at(
            provider=provider, channel=target.get("channel"),
            posted_at=posted_at, check_count=check_count)
        new_status = "measuring" if next_at else "no_effect"
    await database.execute(
        """
        UPDATE citation_actions
           SET status=:status, check_count=:cc, next_check_at=:next, updated_at=now()
         WHERE action_id=:aid
        """,
        {"status": new_status, "cc": check_count, "next": next_at, "aid": action_id},
    )
    return {"action_id": action_id, "provider": provider, "brand_cited": brand_cited,
            "source_cited": source_cited, "status": new_status, "check_count": check_count,
            "next_check_at": next_at, "outcome_id": outcome_id}


def _source_present(raw_runs: Sequence[Mapping[str, Any]], source_host: str) -> bool:
    host = (source_host or "").lower()
    if not host:
        return False
    for run in raw_runs or []:
        for s in (run.get("grounding_sources") or []):
            if isinstance(s, Mapping) and host in (str(s.get("uri", "")) + str(s.get("title", ""))).lower():
                return True
        for uri in (run.get("grounding_chunks") or []):
            if isinstance(uri, str) and host in uri.lower():
                return True
    return False


# ---------------------------------------------------------------------------
# Merchant-scoped reads (ownership checks for the HTTP layer)
# ---------------------------------------------------------------------------

async def list_targets(
    *, merchant_id: str, provider: Optional[str] = None, channel: Optional[str] = None,
    status: Optional[str] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = ["merchant_id = :merchant_id"]
    vals: Dict[str, Any] = {"merchant_id": merchant_id, "limit": int(limit)}
    if provider:
        clauses.append("provider = :provider"); vals["provider"] = provider
    if channel:
        clauses.append("channel = :channel"); vals["channel"] = channel
    if status:
        clauses.append("status = :status"); vals["status"] = status
    rows = await database.fetch_all(
        f"SELECT * FROM citation_targets WHERE {' AND '.join(clauses)} "
        "ORDER BY cited_in_runs DESC, first_seen DESC LIMIT :limit",
        vals,
    )
    return [dict(r) for r in rows]


async def get_target(*, target_id: str, merchant_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        "SELECT * FROM citation_targets WHERE target_id = :tid AND merchant_id = :mid",
        {"tid": target_id, "mid": merchant_id},
    )
    return dict(row) if row else None


async def list_actions(
    *, merchant_id: str, status: Optional[str] = None, limit: int = 100,
) -> List[Dict[str, Any]]:
    clauses = ["merchant_id = :merchant_id"]
    vals: Dict[str, Any] = {"merchant_id": merchant_id, "limit": int(limit)}
    if status:
        clauses.append("status = :status"); vals["status"] = status
    rows = await database.fetch_all(
        f"SELECT * FROM citation_actions WHERE {' AND '.join(clauses)} "
        "ORDER BY created_at DESC LIMIT :limit",
        vals,
    )
    return [dict(r) for r in rows]


async def get_action(*, action_id: str, merchant_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        "SELECT * FROM citation_actions WHERE action_id = :aid AND merchant_id = :mid",
        {"aid": action_id, "mid": merchant_id},
    )
    return dict(row) if row else None


# Hard ceiling on total grounded probes a single cron invocation may issue, so a
# backlog can't turn into an unbounded COGS spike regardless of `limit`.
MAX_CRON_QUERY_BUDGET = 200


async def run_due_rechecks(
    *, limit: int = 50, query_budget: int = MAX_CRON_QUERY_BUDGET,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Cron entrypoint: re-probe every action whose revisit is due. For each, recover
    the original queries + merchant brand/host from its target (persisted at scan time)
    and recheck, scoped to the action's own merchant. Best-effort per action, and
    bounded by an aggregate `query_budget` so a backlog can't spike cost."""
    now = now or _utcnow()
    due = await due_for_recheck(now=now, limit=limit)
    results: List[Dict[str, Any]] = []
    spent = 0
    budget_exhausted = False
    for action in due:
        merchant_id = action["merchant_id"]
        target = await get_target(target_id=action["target_id"], merchant_id=merchant_id)
        question = (target or {}).get("question_text") or ""
        queries = [s.strip() for s in question.split(";") if s.strip()]
        if not queries:
            results.append({"action_id": action["action_id"], "skipped": "no_queries"})
            continue
        if spent + len(queries) > query_budget:  # stop before exceeding the cost ceiling
            budget_exhausted = True
            break
        spent += len(queries)
        try:
            results.append(await recheck_action(
                action_id=action["action_id"], merchant_id=merchant_id, queries=queries,
                merchant_brand=(target or {}).get("merchant_brand"),
                merchant_host=(target or {}).get("merchant_host"), now=now,
            ))
        except Exception as exc:  # one action must not sink the batch
            logger.warning("run_due_rechecks failed action=%s: %s", action["action_id"], exc)
            results.append({"action_id": action["action_id"], "error": str(exc)})
    return {"processed": len(results), "queries_spent": spent,
            "budget_exhausted": budget_exhausted, "results": results}
