"""Per-SKU strategic brief assembly and grounding validation.

The LLM is only allowed to frame deterministic audit facts. This module builds
the facts, sends the exact brief prompt when enabled/keyed, and rejects any
brief that names entities or lanes outside the evidence block.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlparse

from config.settings import settings
from services.llm_synthesis import (
    LLMSynthesisError,
    configured_key_for_provider,
    default_model_for_provider,
    normalize_provider,
    synthesize,
)

_STRATEGIC_BRIEF_SYSTEM_PROMPT = """You are a senior D2C brand & growth strategist — the merchant's marketing director — writing the
next-steps section of an AI-shopping-visibility audit. You make sharp, decisive calls a smart founder
would act on. You are NOT a checklist generator.

ABSOLUTE GROUNDING RULES (this is a trust product — violating these is worse than being vague):
- Use ONLY facts present in the EVIDENCE block. Every brand, website/source, attribute, certification,
  format, audience, and lane you mention MUST appear in EVIDENCE verbatim.
- NEVER invent competitors, sources, statistics, review counts, prices, certifications, or claims.
- If EVIDENCE doesn't support a point, don't make it. Distinguish "AI's answers show…" (fact) from
  "this suggests…" (your inference) explicitly.
- No internal jargon, no scores, no taxonomy terms (no "/100", "ownership state", "source route",
  "opportunity score"). Plain language a busy merchant reads in 60 seconds.
- Reference ONLY channels, communities, retailers, publishers, blogs, and brands that appear in EVIDENCE. Do
  NOT name specific influencers, publications, blogs, subreddits, hashtags, or platforms that are not in
  EVIDENCE (e.g. do not invent a subreddit name or an influencer handle). If EVIDENCE says a forum/community
  controls a lane, refer to it generically (e.g. "the relevant communities").
- Describe position qualitatively. NEVER output a number, score, rating, review count, price, percentage, or
  "/100" — none appear in your EVIDENCE.
- When you mention a search lane/query, use the EVIDENCE wording for it.

CLAIM DISCIPLINE (do not let confident prose outrun the EVIDENCE — this is a trust product):
- COMPETITORS: EVIDENCE names which competitors win, but NOT their product attributes
  (grounding_notes.competitor_attributes = "not_assessed"). NEVER state as fact that a competitor lacks,
  is missing, or does not have a feature. You MAY note a likely positioning gap as YOUR INFERENCE, marked as
  such: "incumbents are generally positioned as broad <category>; a dedicated <your differentiator> looks like
  an opening — worth confirming." Your differentiation is YOUR attributes; the WEDGE is real, the competitor
  comparison is an inference to verify.
- CHANNELS: recommend a specific marketplace, retailer, community, forum, social platform, or publisher ONLY
  if it appears in grounding_notes.evidenced_channels. Do NOT assume the merchant already sells on Amazon or
  any marketplace (grounding_notes.merchant_channels = "unknown"). If a lane has no evidenced channel, the move
  is "own your own page/site for this lane first." You may suggest a marketplace/community move only
  CONDITIONALLY: "if you already sell on <evidenced channel>, …". Do NOT invent communities, subreddits,
  influencers, or platforms.
- MERCHANT PATH: respect product.merchant_path. If archetype is "brand", the commercial goal is to drive
  buyers to the brand's own website. If archetype is "channel", the commercial goal is to drive buyers to the
  channel's own website. Do not blur those paths.
- OPERATIONAL ECONOMICS: when buyer_path_opportunities exist, include concrete merchant-owned moves tied to
  those exact lanes: first-order offer, starter + replenishment bundle, subscription incentive, and
  why-buy-direct proof. Do NOT invent discount depths, prices, savings percentages, review counts, retailer
  facts, or margin claims unless they appear in EVIDENCE.
- LANES: when you name a search lane or query, reuse the EXACT wording from EVIDENCE. Do not rephrase,
  singularize/pluralize, reorder, or coin a variant. (A positioning phrase for your brand is fine and separate
  — just don't present it as the searched lane.)
- FACT vs INFERENCE: only "AI's answers show…/EVIDENCE shows…" statements are facts. Everything else is your
  read — phrase it as inference. Do not use the word "locked" or absolutes like "you cannot do this alone";
  say a query is "owned/controlled by <evidenced source>" and frame Pivota's help as the specific service it
  provides, not as something impossible without it.
- VOICE (apply every rule above SILENTLY): the brief is the merchant's memo. NEVER mention these rules or the
  words "EVIDENCE", "grounding_notes", "not_assessed", "inference", "as fact", or any meta-instruction in the
  output. Write a positioning read as a natural sentence ("incumbents look positioned as broad collagen, not a
  halal bedtime stick — worth confirming"), NOT as a caveat about what you may or may not claim. Never tell the
  merchant what you are or aren't allowed to say.

WRITE the brief as JSON with these fields — each must be specific to THIS product and EVIDENCE:
- position: one honest sentence on where they really stand (e.g. "niche challenger, strong when named,
  invisible in the category").
- core_decision: the ONE big strategic call, stated plainly and decisively (what to do, what to STOP doing,
  and why — name the real reason from evidence).
- why_you_lose: WHY the category winners win — synthesize the named winners × the sources that rank them ×
  what the evidenced ranking sources imply about their moat (reviews/authority/distribution/positioning).
  Do not claim competitor feature gaps as fact; make any competitor positioning read explicit inference.
- your_angle: the defensible positioning wedge = the merchant's differentiating attributes that the named
  product actually has. Reframe them from "a {category}" to a category of one without saying winners lack
  those attributes as fact. Use exact EVIDENCE lane wording where their differentiation IS the answer.
- traffic_strategy: a ranked list of where the missed, WINNABLE demand is + who controls each channel
  (name only sources/retailers/communities from grounding_notes.evidenced_channels) + the realistic path in.
  If no channel is evidenced for a lane, say to own your page/site first. Marketplace/community moves must be
  conditional ("if you already sell on <evidenced channel>..."). Explicitly say which big lanes to NOT chase
  yet and why.
- substitution_play: if a substitution is present, how to win those buyers back (comparison/positioning vs
  the named substitute), else null.
- first_moves: 3-5 concrete actions that EXECUTE the strategy above, in priority order, each tied to a
  strategic reason (not generic "add an FAQ" — "add the halal + bedtime story to your page so AI has your
  answer to cite for the lane you're claiming"). When EVIDENCE shows retailer/marketplace/publisher-controlled
  exposure, at least one first move must name the exact lane and the operational reason to buy from the
  merchant-controlled page (offer, bundle, subscription, or why-buy-direct proof).
- diy_vs_pivota: {self_serve:[2-3 merchant-owned moves], pivota:"one honest line on what only Pivota does
  — cited+buyable canonical page, serving, monitoring"}.   # the 70/30, honest, no cold-audit hard-sell"""

_ATTRIBUTE_FIELD_MAP = {
    "category": ("category",),
    "format": ("format",),
    "ingredient": ("ingredient",),
    "certification": ("certification", "certification_constraint"),
    "audience": ("audience",),
    "use_case": ("use_case",),
    "geography": ("geography",),
    "proof": ("proof",),
    "exclusion": ("exclusion",),
}
_LOST_CATEGORY_OWNERSHIP = {
    "competitor-owned",
    "publisher-owned",
    "retailer-owned",
    "marketplace-owned",
    "forum-owned",
}
_NO_CONTROL_ROUTES = {"", "none", "unclassified", "fragmented", "open-lane"}
_REQUIRED_BRIEF_KEYS = {
    "position",
    "core_decision",
    "why_you_lose",
    "your_angle",
    "traffic_strategy",
    "substitution_play",
    "first_moves",
    "diy_vs_pivota",
}
_FORBIDDEN_PATTERNS = (
    re.compile(r"/100", re.IGNORECASE),
    re.compile(r"\$\s?\d", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s?%", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*x\b", re.IGNORECASE),
    re.compile(
        r"\b\d[\d,]*\+?\s*(?:reviews?|ratings?|stars?|customers?|users?|sales?|followers?|subscribers?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsource route\b", re.IGNORECASE),
    re.compile(r"\bopportunity score\b", re.IGNORECASE),
    re.compile(r"\bcanonical enriched\b", re.IGNORECASE),
    re.compile(r"\bagent-resolvable\b", re.IGNORECASE),
    re.compile(r"\bownership state\b", re.IGNORECASE),
    re.compile(r"\bcontent_richness\b", re.IGNORECASE),
    re.compile(r"\bschema-friendly\b", re.IGNORECASE),
    re.compile(r"\bgrounded agent\b", re.IGNORECASE),
    re.compile(r"\bscores?\b", re.IGNORECASE),
)
_DOMAIN_RE = re.compile(
    r"(?<!@)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b",
    re.IGNORECASE,
)
_QUOTE_RE = re.compile(r"[\"'`“”‘’]([^\"'`“”‘’\n]{4,160})[\"'`“”‘’]")
_CAP_WORD = r"(?:[A-Z][A-Za-z0-9&'’-]*|[A-Z]{2,}|[A-Za-z]+[A-Z][A-Za-z0-9]*)"
_PROPER_SEQUENCE_RE = re.compile(
    rf"\b{_CAP_WORD}(?:[ \t]+(?:of|the|for))*[ \t]+{_CAP_WORD}"
    rf"(?:[ \t]+(?:of|the|for|and|&|{_CAP_WORD}))*\b"
)
_QUOTE_BOUNDARY_RE = re.compile(r"[\"'`“”‘’]")
_SINGLE_ENTITY_RE = re.compile(
    r"\b(?:[A-Z]{2,}|[A-Z][a-z][A-Za-z0-9'’-]{2,}|[A-Za-z]+[A-Z][A-Za-z0-9]*)\b"
)
_ENTITY_STOPWORDS = {
    "A",
    "AI",
    "An",
    "And",
    "Answer",
    "Answers",
    "As",
    "Because",
    "Before",
    "Big",
    "Build",
    "Buyers",
    "Category",
    "Channel",
    "Create",
    "Decision",
    "Demand",
    "Do",
    "Don",
    "Earn",
    "Evidence",
    "FAQ",
    "FAQs",
    "First",
    "Fix",
    "For",
    "If",
    "Instead",
    "IS",
    "JSON",
    "Keep",
    "Lane",
    "Lanes",
    "Matters",
    "Make",
    "Merchant",
    "Muslim",
    "NOT",
    "No",
    "Own",
    "PDP",
    "Pivota",
    "Publish",
    "Seed",
    "Shoppers",
    "SKU",
    "Start",
    "Stop",
    "Strategy",
    "That",
    "The",
    "Their",
    "Then",
    "These",
    "This",
    "Those",
    "Track",
    "Traffic",
    "UGC",
    "Use",
    "What",
    "When",
    "Where",
    "Why",
    "Win",
    "Write",
    "WRITE",
    "You",
    "Your",
}
_ENTITY_STOPWORD_NORMALIZED = frozenset(
    stopword.lower() for stopword in _ENTITY_STOPWORDS
)
_INTERNAL_ALLOWED_ENTITIES = {
    "ai",
    "anthropic",
    "bing",
    "chatgpt",
    "claude",
    "copilot",
    "d2c",
    "deepseek",
    "dtc",
    "diy",
    "faq",
    "faqs",
    "gemini",
    "google",
    "json",
    "openai",
    "pdp",
    "perplexity",
    "pivota",
    "sku",
    "ugc",
}
_AI_ENGINE_ENTITIES = {
    "anthropic",
    "bing",
    "chatgpt",
    "claude",
    "copilot",
    "deepseek",
    "gemini",
    "google",
    "openai",
    "perplexity",
}
_MULTIPART_TLDS = {"co.uk", "com.au", "co.jp", "co.kr", "com.br"}
_SENTENCE_BOUNDARY_CHARS = {".", "!", "?", ":", ";", "\n", "•", "–", "—", "-"}
_SENTENCE_PREFIX_STRIP_CHARS = " \t\r\f\v\"'`“”‘’()[]{}<>"
_CONNECTOR_WORDS = {"of", "the", "for", "and", "&"}
_SHOPPING_WORDS = {
    "alternative",
    "alternatives",
    "best",
    "buy",
    "compare",
    "comparison",
    "dupe",
    "dupes",
    "review",
    "reviews",
    "shop",
    "top",
    "vs",
    "where",
}
_COMMON_WORDS = frozenset({
    "answer",
    "answers",
    "best",
    "better",
    "blog",
    "buy",
    "category",
    "certified",
    "claim",
    "claims",
    "comparison",
    "content",
    "cta",
    "ctr",
    "demand",
    "designed",
    "description",
    "difference",
    "diy",
    "faq",
    "faqs",
    "guide",
    "h1",
    "h2",
    "headline",
    "how",
    "keyword",
    "keywords",
    "lane",
    "lanes",
    "link",
    "listing",
    "matter",
    "matters",
    "meta",
    "more",
    "new",
    "only",
    "page",
    "pages",
    "phrase",
    "post",
    "product",
    "queries",
    "query",
    "repair",
    "roi",
    "routine",
    "search",
    "seo",
    "site",
    "sku",
    "story",
    "terms",
    "title",
    "tip",
    "tips",
    "traffic",
    "url",
    "what",
    "when",
    "where",
    "why",
    "win",
    "wins",
    "work",
    "works",
})
# Common English adjective/noun/verb STEMS that show up in coined titles and
# marketing prose. With _stem_is_common (suffix-stripping) these also cover
# inflections (smarter→smart, routines→routine, winning→win), so the entity
# scan never mistakes ordinary headline words for invented proper nouns. Keep
# DISTINCTIVE handle/brand words (e.g. "girl", "boss", "mama") OUT so coined
# names like "Halal Girl Boss" still fail.
_COMMON_STEMS = frozenset({
    # adjectives
    "smart", "simple", "fast", "slow", "strong", "weak", "clean", "safe",
    "fresh", "long", "short", "light", "heavy", "easy", "hard", "big", "small",
    "quick", "good", "bad", "great", "high", "low", "real", "true", "clear",
    "bold", "sharp", "soft", "warm", "cool", "rich", "pure", "natural",
    "organic", "healthy", "gentle", "daily", "nightly", "modern", "classic",
    "premium", "affordable", "effective", "powerful", "unique", "special",
    "perfect", "ideal", "essential", "key", "main", "core", "broad", "narrow",
    "deep", "wide", "full", "open", "smarter", "better",
    # nouns
    "choice", "way", "story", "moment", "secret", "formula", "advantage",
    "edge", "guide", "method", "system", "step", "move", "play", "reason",
    "truth", "point", "idea", "plan", "goal", "focus", "value", "trust",
    "proof", "result", "growth", "market", "brand", "niche", "angle", "option",
    "gap", "lever", "factor", "driver", "signal", "insight", "takeaway",
    "summary", "recommendation", "action", "tactic", "strategy", "approach",
    "audience", "segment", "customer", "shopper", "buyer", "consumer",
    "supply", "source", "channel", "platform", "community", "forum", "ranking",
    "visibility", "presence", "authority", "distribution", "copy", "article",
    "name", "label", "tag", "moment", "ritual", "glow", "skin", "beauty",
    "wellness", "supplement", "stick", "powder", "capsule", "serum", "cream",
    "routine", "night", "day", "morning", "bedtime", "story",
    # verbs / actions
    "own", "build", "create", "publish", "optimize", "capture", "target",
    "reach", "position", "reframe", "differentiate", "beat", "compete", "rank",
    "cite", "serve", "monitor", "seed", "pitch", "earn", "frame", "write",
    "add", "update", "fix", "stop", "start", "chase", "avoid", "prioritize",
    "double", "lean", "drive", "grow", "convert", "highlight", "emphasize",
    "include", "ensure", "share", "answer", "compare", "differentiate",
    # common marketing nouns/adjectives that show up in coined headlines
    "weapon", "bright", "proven", "prove", "roadmap", "playbook", "powerhouse",
    "blueprint", "hack", "game", "changer", "boost", "leader", "expert",
    "trend", "recipe", "checklist", "framework", "template", "toolkit", "hook",
    "winner", "advantage", "edge", "momentum", "leverage", "shortcut", "wedge",
})
_STEM_SUFFIXES = ("iest", "ier", "est", "ing", "er", "ed", "es", "ly", "s")
_QUOTE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "cannot",
    "cant",
    "do",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "its",
    "no",
    "not",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "when",
    "with",
    "without",
    "you",
    "your",
}
_COMMON_PROSE_WORDS = {
    "answer",
    "answers",
    "category",
    "certified",
    "claim",
    "claims",
    "designed",
    "lane",
    "lanes",
    "only",
    "page",
    "pages",
    "query",
    "queries",
    "repair",
    "site",
    "terms",
}
_ALLCAPS_FUNCTION_WORDS = _QUOTE_STOPWORDS | {
    "chase",
    "keep",
    "make",
    "must",
    "own",
    "stop",
    "use",
}
_SAFETY_SENSITIVE_TERMS = {
    "kids",
    "kid",
    "children",
    "child",
    "infant",
    "infants",
    "toddler",
    "toddlers",
    "baby",
    "babies",
    "pregnant",
    "pregnancy",
    "prenatal",
    "nursing",
    "breastfeeding",
    "diabetic",
    "diabetics",
    "diabetes",
    "hypertension",
    "medication",
    "medications",
    "disease",
    "cancer",
    "cure",
    "cures",
    "treat",
    "treats",
    "treatment",
    "clinical",
    "clinically",
    "fda",
}


def assemble_sku_brief_evidence(
    *,
    opportunity: Mapping[str, Any],
    attribute_graph: Mapping[str, Any],
    primary_gaps: Optional[List[Mapping[str, Any]]] = None,
    scores: Optional[Mapping[str, Any]] = None,
    identity: Optional[Mapping[str, Any]] = None,
    sku_title: Optional[str] = None,
) -> Dict[str, Any]:
    del primary_gaps, scores
    opportunity_map = _as_mapping(opportunity)
    identity_map = _as_mapping(identity)
    attributes = _attribute_evidence(attribute_graph)
    title = _clean_str(sku_title) or _clean_str(identity_map.get("name")) or "this SKU"
    anchors = _as_mapping(identity_map.get("anchors"))
    brand = _clean_str(anchors.get("brand"))
    merchant_path = _merchant_path(identity=identity_map, opportunity=opportunity_map)

    category_rows = _category_battle_rows(opportunity_map)
    category_battle = _category_battle(category_rows)
    top_open_lanes = _top_open_lane_rows(opportunity_map)
    channel_map = _channel_map(
        top_open_lanes=top_open_lanes,
        per_prompt=_as_list(opportunity_map.get("per_prompt")),
    )
    grounding_notes = _grounding_notes(
        category_battle=category_battle,
        channel_map=channel_map,
        opportunity=opportunity_map,
    )

    return {
        "product": {
            "title": title,
            "brand": brand or None,
            "merchant_path": merchant_path,
            "attributes": attributes,
        },
        "position": _position_from_ladder(opportunity_map),
        "category_battle": category_battle,
        "substitution": _substitution_evidence(opportunity_map),
        "open_lanes": [_open_lane_evidence(lane) for lane in top_open_lanes],
        "channel_map": channel_map,
        "buyer_path_opportunities": _buyer_path_opportunities(
            opportunity=opportunity_map,
            merchant_path=merchant_path,
        ),
        "grounding_notes": grounding_notes,
        "demand_state": opportunity_map.get("demand_state_summary"),
        "notes": {
            "merchant_can_act_in_30d": True,
            "health_sensitive": _health_sensitive(
                title=title,
                brand=brand,
                attributes=attributes,
            ),
        },
    }


def build_sku_brief_prompt(evidence: Mapping[str, Any]) -> Tuple[str, str]:
    user = "EVIDENCE:\n" + json.dumps(
        evidence,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return _STRATEGIC_BRIEF_SYSTEM_PROMPT, user


async def generate_sku_strategic_brief(
    evidence: Mapping[str, Any],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not getattr(settings, "strategic_brief_enabled", False):
        return None
    try:
        selected_provider = normalize_provider(
            provider or settings.strategic_brief_provider
        )
    except LLMSynthesisError:
        return None
    if not configured_key_for_provider(selected_provider):
        return None
    selected_model = (
        str(model or settings.strategic_brief_model or "").strip()
        or default_model_for_provider(selected_provider)
    )
    system, user = build_sku_brief_prompt(evidence)

    for _attempt in range(3):
        try:
            result = await synthesize(
                system=system,
                user=user,
                provider=selected_provider,
                model=selected_model,
                max_tokens=1200,
            )
        except LLMSynthesisError:
            return _validated_deterministic_brief(evidence)
        brief = _parse_brief_json(result.get("text"))
        if not isinstance(brief, dict) or not _has_required_shape(brief):
            continue
        if validate_grounding(brief, evidence):
            return brief
    return _validated_deterministic_brief(evidence)


def validate_grounding(brief: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    return not _grounding_failures(brief, evidence)


def _attribute_evidence(attribute_graph: Mapping[str, Any]) -> Dict[str, List[str]]:
    graph = _as_mapping(attribute_graph)
    classes = _as_mapping(graph.get("classes"))
    out: Dict[str, List[str]] = {}
    for output_name, source_names in _ATTRIBUTE_FIELD_MAP.items():
        values: List[str] = []
        for source_name in source_names:
            values.extend(_as_str_list(classes.get(source_name)))
        out[output_name] = _unique(values)
    return out


def _position_from_ladder(opportunity: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    ladder = _as_mapping(opportunity.get("intent_ladder"))
    return {
        "strong_when_named": _position_band_from_layer(
            ladder.get("branded_transactional")
        ),
        "weak_in_category": _position_band_from_layer(ladder.get("head_category")),
        "branded_consideration": _position_band_from_layer(
            ladder.get("branded_consideration")
        ),
    }


def _category_battle_rows(opportunity: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    for row in _as_list(opportunity.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        query = _clean_str(row.get("query"))
        if not query:
            continue
        query_class = _clean_str(row.get("query_class")).lower()
        axis = _clean_str(row.get("axis")).lower()
        if query_class not in {"head", "category"} and axis != "category":
            continue
        ownership = _clean_str(row.get("ownership_state")).lower()
        provider_verdicts = _as_mapping(row.get("provider_verdicts"))
        lost_by_verdict = any(
            str(verdict).strip().lower() == "loss"
            for verdict in provider_verdicts.values()
        )
        if (
            ownership in _LOST_CATEGORY_OWNERSHIP
            or lost_by_verdict
            or _as_str_list(row.get("competitors"))
        ):
            rows.append(row)
    rows.sort(
        key=lambda row: (
            -float(row.get("demand_signal") or 0),
            -float(row.get("opportunity_score") or 0),
            _clean_str(row.get("query")).lower(),
        )
    )
    return rows


def _category_battle(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    prompts: List[str] = []
    winners: List[str] = []
    ranked_by: List[Dict[str, str]] = []
    prompt_details: List[Dict[str, Any]] = []
    for row in rows:
        query = _clean_str(row.get("query"))
        if query:
            prompts.append(query)
        row_competitors = _as_str_list(row.get("competitors"))
        winners.extend(row_competitors)
        source_roles = _source_role_chips(row)
        ranked_by.extend(source_roles)
        prompt_details.append({
            "query": query,
            "ownership": _clean_str(row.get("ownership_state")) or None,
            "competitors": _unique(row_competitors),
            "source_roles": source_roles,
        })
    return {
        "prompts": _unique(prompts),
        "winners": _unique(winners),
        "ranked_by": _unique_host_roles(ranked_by),
        "prompt_details": prompt_details,
    }


def _substitution_evidence(opportunity: Mapping[str, Any]) -> Dict[str, Any]:
    substitution = _as_mapping(opportunity.get("substitution_alert"))
    handed_to = (
        _clean_str(substitution.get("handed_to"))
        or _clean_str(substitution.get("substituted_by"))
    )
    prompt = (
        _clean_str(substitution.get("on_prompt"))
        or _clean_str(substitution.get("prompt"))
    )
    present = bool(substitution.get("present")) and bool(handed_to or prompt)
    return {
        "present": present,
        "on_prompt": prompt or None,
        "handed_to": handed_to or None,
        "engines": _as_str_list(substitution.get("engines")),
    }


def _top_open_lane_rows(opportunity: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    lanes = [
        lane for lane in _as_list(opportunity.get("top_open_lanes"))
        if isinstance(lane, Mapping) and _clean_str(lane.get("query"))
    ]
    return lanes[:3]


def _open_lane_evidence(lane: Mapping[str, Any]) -> Dict[str, Any]:
    source_route = _clean_str(lane.get("source_route")).lower()
    current = _clean_str(lane.get("current_ownership")).lower()
    who_controls = current or source_route
    if who_controls in _NO_CONTROL_ROUTES or source_route in _NO_CONTROL_ROUTES:
        who_controls = "none/fragmented"
    channel_role = "open" if who_controls == "none/fragmented" else who_controls
    return {
        "query": _clean_str(lane.get("query")),
        "why_fit": _as_str_list(lane.get("why_fit")),
        "who_controls": who_controls,
        "channel_role": channel_role,
    }


def _buyer_path_opportunities(
    *,
    opportunity: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    for row in _as_list(opportunity.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        query = _clean_str(row.get("query"))
        if not query:
            continue
        ownership = _clean_str(row.get("ownership_state")).lower()
        route = _clean_str(row.get("source_route")).lower()
        if ownership not in _LOST_CATEGORY_OWNERSHIP and route not in {
            "retailer",
            "marketplace",
            "publisher",
            "forum",
        }:
            continue
        if float(row.get("demand_signal") or 0) <= 0 and float(row.get("opportunity_score") or 0) <= 0:
            continue
        if not _buyer_path_controllers(row):
            continue
        rows.append(row)
    rows.sort(
        key=lambda row: (
            -float(row.get("opportunity_score") or 0),
            -float(row.get("demand_signal") or 0),
            _clean_str(row.get("query")).lower(),
        )
    )
    return [
        _buyer_path_opportunity(row, merchant_path)
        for row in rows[:5]
    ]


def _buyer_path_opportunity(
    row: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> Dict[str, Any]:
    controllers = _buyer_path_controllers(row)
    route = _clean_str(row.get("source_route")).lower()
    ownership = _clean_str(row.get("ownership_state")).lower()
    route_label = route or ownership.replace("-owned", "") or "third-party"
    return {
        "query": _clean_str(row.get("query")),
        "exposure": ownership or None,
        "route": route_label,
        "controlled_by": controllers,
        "destination": _clean_str(merchant_path.get("destination")),
        "merchant_archetype": _clean_str(merchant_path.get("archetype")),
        "recommended_moves": _buyer_path_moves(merchant_path),
    }


def _buyer_path_controllers(row: Mapping[str, Any]) -> List[Dict[str, str]]:
    source_summary = _as_mapping(row.get("source_summary"))
    summarized: List[Dict[str, str]] = []
    route = _clean_str(row.get("source_route")) or "unclassified"
    for source in _as_list(source_summary.get("top_cited_hosts")):
        if not isinstance(source, Mapping):
            continue
        host = _normalize_host(source.get("host"))
        if host:
            summarized.append({"host": host, "role": route})
    if summarized:
        return _unique_host_roles(summarized)[:3]
    return _unique_host_roles(_source_role_chips(row))[:3]


def _buyer_path_moves(merchant_path: Mapping[str, Any]) -> List[str]:
    page = _clean_str(merchant_path.get("page_label")) or "merchant-controlled page"
    return [
        f"Make {page} the canonical cited page for this lane.",
        "Add a first-order offer without inventing a discount depth.",
        "Add a starter + replenishment bundle.",
        "Add a subscription incentive where the product supports replenishment.",
        "Add why-buy-direct proof: guarantee, samples, loyalty, returns, stock, and fresh facts.",
    ]


def _channel_map(
    *,
    top_open_lanes: List[Mapping[str, Any]],
    per_prompt: List[Any],
) -> List[Dict[str, Any]]:
    rows_by_query = {
        _norm_phrase(row.get("query")): row
        for row in per_prompt
        if isinstance(row, Mapping) and _clean_str(row.get("query"))
    }
    out: List[Dict[str, Any]] = []
    for lane in top_open_lanes:
        query = _clean_str(lane.get("query"))
        row = rows_by_query.get(_norm_phrase(query)) or {}
        controlled_by = _unique_host_roles(_source_role_chips(row))
        source_route = _clean_str(row.get("source_route") or lane.get("source_route")).lower()
        role = (
            "open"
            if source_route in _NO_CONTROL_ROUTES or not controlled_by
            else source_route
        )
        out.append({
            "lane": query,
            "query": query,
            "controlled_by": controlled_by,
            "role": role,
        })
    return out


def _grounding_notes(
    *,
    category_battle: Mapping[str, Any],
    channel_map: List[Mapping[str, Any]],
    opportunity: Mapping[str, Any],
) -> Dict[str, Any]:
    evidenced_channels: List[Mapping[str, Any]] = []
    for ranked in _as_list(_as_mapping(category_battle).get("ranked_by")):
        if isinstance(ranked, Mapping):
            evidenced_channels.append(ranked)
    for lane in channel_map:
        if not isinstance(lane, Mapping):
            continue
        for controller in _as_list(lane.get("controlled_by")):
            if isinstance(controller, Mapping):
                evidenced_channels.append(controller)
    evidenced_channels.extend(_substitution_source_roles(opportunity))
    return {
        "competitor_attributes": "not_assessed",
        "merchant_channels": "unknown",
        "evidenced_channels": _unique_host_roles(evidenced_channels),
    }


def _merchant_path(
    *,
    identity: Mapping[str, Any],
    opportunity: Mapping[str, Any],
) -> Dict[str, str]:
    archetype = _merchant_archetype(identity=identity, opportunity=opportunity)
    if archetype == "channel":
        return {
            "archetype": "channel",
            "destination": "the channel's own website",
            "page_label": "the channel PDP or category page",
            "goal": "drive buyers to the channel's website",
        }
    return {
        "archetype": "brand",
        "destination": "the brand's own website",
        "page_label": "the official brand PDP",
        "goal": "drive buyers to the brand's own website",
    }


def _merchant_archetype(
    *,
    identity: Mapping[str, Any],
    opportunity: Mapping[str, Any],
) -> str:
    for payload in (identity, opportunity, _as_mapping(identity.get("anchors"))):
        raw = _first_present(
            payload,
            (
                "merchant_archetype",
                "merchant_type",
                "business_model",
                "commerce_role",
                "seller_type",
                "retailer_type",
            ),
        )
        normalized = _normalize_merchant_archetype(raw)
        if normalized:
            return normalized
    return "brand"


def _normalize_merchant_archetype(value: Any) -> Optional[str]:
    text = _clean_str(value).lower()
    if not text:
        return None
    if any(token in text for token in ("channel", "retailer", "marketplace", "multi-brand", "multibrand")):
        return "channel"
    if any(token in text for token in ("brand", "dtc", "d2c", "manufacturer", "maker")):
        return "brand"
    return None


def _first_present(payload: Mapping[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and _clean_str(value):
            return value
    return None


def _substitution_source_roles(opportunity: Mapping[str, Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    alert = _as_mapping(opportunity.get("substitution_alert"))
    rows.extend(_source_role_chips(alert))
    alert_prompt = _norm_phrase(alert.get("prompt") or alert.get("on_prompt"))
    for row in _as_list(opportunity.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        substitution = _as_mapping(row.get("substitution"))
        row_prompt = _norm_phrase(row.get("query"))
        if not substitution.get("present") and (
            not alert_prompt or row_prompt != alert_prompt
        ):
            continue
        rows.extend(_source_role_chips(row))
        rows.extend(_source_role_chips(substitution))
    return _unique_host_roles(rows)


def _source_role_chips(row: Mapping[str, Any]) -> List[Dict[str, str]]:
    chips: List[Dict[str, str]] = []
    for source in _as_list(row.get("source_roles")):
        if not isinstance(source, Mapping):
            continue
        host = _normalize_host(source.get("host"))
        if not host:
            continue
        role = _clean_str(source.get("role")) or "unclassified"
        chips.append({"host": host, "role": role})
    if chips:
        return chips

    source_summary = _as_mapping(row.get("source_summary"))
    for source in _as_list(source_summary.get("top_cited_hosts")):
        if not isinstance(source, Mapping):
            continue
        host = _normalize_host(source.get("host"))
        if host:
            chips.append({"host": host, "role": "unclassified"})
    return chips


def _unique_host_roles(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for row in rows:
        host = _normalize_host(row.get("host"))
        role = _clean_str(row.get("role")) or "unclassified"
        if not host:
            continue
        key = (host.lower(), role.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "role": role})
    return out


def _health_sensitive(
    *,
    title: str,
    brand: str,
    attributes: Mapping[str, List[str]],
) -> bool:
    text = " ".join(
        [
            title,
            brand,
            *[
                item
                for values in attributes.values()
                for item in values
            ],
        ]
    ).lower()
    return any(
        token in text
        for token in (
            "collagen",
            "deodorant",
            "fish",
            "glycine",
            "health",
            "probiotic",
            "skin",
            "supplement",
            "vitamin",
            "wellness",
        )
    )


def _parse_brief_json(raw_text: Any) -> Optional[Dict[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            parsed = json.loads(fence.group(1))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _has_required_shape(brief: Mapping[str, Any]) -> bool:
    if not _REQUIRED_BRIEF_KEYS.issubset(brief.keys()):
        return False
    if not isinstance(brief.get("traffic_strategy"), list):
        return False
    if not isinstance(brief.get("first_moves"), list):
        return False
    if not (3 <= len(brief.get("first_moves") or []) <= 5):
        return False
    diy = brief.get("diy_vs_pivota")
    if not isinstance(diy, Mapping) or not isinstance(diy.get("self_serve"), list):
        return False
    if not isinstance(diy.get("pivota"), str):
        return False
    return True


def _validated_deterministic_brief(evidence: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    brief = _deterministic_brief(evidence)
    if brief and validate_grounding(brief, evidence):
        return brief
    return None


def _deterministic_brief(evidence: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    product = _as_mapping(evidence.get("product"))
    title = _clean_str(product.get("title")) or "this SKU"
    merchant_path = _as_mapping(product.get("merchant_path"))
    page_label = _clean_str(merchant_path.get("page_label")) or "the merchant-controlled page"
    destination = _clean_str(merchant_path.get("destination")) or "the merchant-controlled website"
    opportunities = [
        item for item in _as_list(evidence.get("buyer_path_opportunities"))
        if isinstance(item, Mapping) and _clean_str(item.get("query"))
    ]
    if not opportunities:
        return None
    lead = opportunities[0]
    query = _clean_str(lead.get("query"))
    controllers = _unique_host_roles(
        controller
        for opportunity in opportunities[:3]
        for controller in _as_list(opportunity.get("controlled_by"))
        if isinstance(controller, Mapping)
    )[:3]
    controller_phrase = _controller_phrase(controllers)
    attributes = _as_mapping(product.get("attributes"))
    angle_terms = _brief_angle_terms(attributes)

    first_moves = [
        (
            f"Make {page_label} the canonical cited page for {query}, then add "
            "a first-order offer so buyers have a reason to choose the merchant path."
        ),
        (
            f"Add a starter + replenishment bundle on {page_label} for {query}, "
            "so the page has a concrete value reason beyond third-party exposure."
        ),
        (
            "Add subscription incentive and why-buy-direct proof: guarantee, samples, "
            "loyalty, returns, stock, and fresh facts."
        ),
    ]
    if controller_phrase != "the cited sources":
        first_moves.append(
            f"Update the source trail around {controller_phrase} only after {page_label} "
            "has the direct buying reason."
        )

    return {
        "position": (
            f"{title} has real demand in AI answers, but the buying path is controlled "
            f"by {controller_phrase}, not {destination}."
        ),
        "core_decision": (
            f"Make {page_label} the better place to buy for {query}; stop treating "
            "third-party exposure as a win until buyers have a reason to choose the "
            "merchant-controlled page."
        ),
        "why_you_lose": (
            f"AI's answers show {controller_phrase} shaping {query}. That suggests "
            "source and distribution authority are capturing the buyer path before "
            f"{destination} does."
        ),
        "your_angle": (
            f"Use the evidenced product angle — {angle_terms} — as the reason the "
            "merchant-controlled page deserves to be cited and bought from."
        ),
        "traffic_strategy": [
            {
                "where": _clean_str(item.get("query")),
                "who_controls": _controller_phrase(
                    _unique_host_roles(
                        controller
                        for controller in _as_list(item.get("controlled_by"))
                        if isinstance(controller, Mapping)
                    )
                ),
                "how": (
                    f"Make {page_label} the canonical page first, then add the "
                    "offer, bundle, subscription, and why-buy-direct proof."
                ),
            }
            for item in opportunities[:3]
        ],
        "substitution_play": _deterministic_substitution_play(evidence, page_label),
        "first_moves": first_moves[:5],
        "diy_vs_pivota": {
            "self_serve": [
                "Add the offer, bundle, subscription, and why-buy-direct proof.",
                "Keep price, stock, returns, reviews, and product facts fresh.",
            ],
            "pivota": (
                "Pivota makes the page cited and buyable, then monitors whether "
                "these same lanes move toward the merchant-controlled path."
            ),
        },
    }


def _controller_phrase(controllers: List[Mapping[str, Any]]) -> str:
    hosts = [
        _normalize_host(controller.get("host"))
        for controller in controllers
        if isinstance(controller, Mapping)
    ]
    hosts = _unique(host for host in hosts if host)
    if not hosts:
        return "the cited sources"
    if len(hosts) == 1:
        return hosts[0]
    return ", ".join(hosts[:-1]) + f", and {hosts[-1]}"


def _brief_angle_terms(attributes: Mapping[str, Any]) -> str:
    terms: List[str] = []
    for key in ("certification", "ingredient", "format", "use_case", "category"):
        terms.extend(_as_str_list(attributes.get(key)))
    return _phrase_join(_unique(terms[:5]), "the evidenced product attributes")


def _phrase_join(values: List[str], fallback: str) -> str:
    cleaned = [value for value in values if _clean_str(value)]
    if not cleaned:
        return fallback
    if len(cleaned) == 1:
        return cleaned[0]
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _deterministic_substitution_play(
    evidence: Mapping[str, Any],
    page_label: str,
) -> Optional[str]:
    substitution = _as_mapping(evidence.get("substitution"))
    if not substitution.get("present"):
        return None
    prompt = _clean_str(substitution.get("on_prompt"))
    handed_to = _clean_str(substitution.get("handed_to"))
    if not prompt and not handed_to:
        return None
    if prompt and handed_to:
        return (
            f"Answer {prompt} on {page_label} with a factual comparison against "
            f"{handed_to}, then connect the comparison to the direct buying reason."
        )
    if prompt:
        return (
            f"Answer {prompt} on {page_label}, then connect the comparison to the "
            "direct buying reason."
        )
    return (
        f"Publish a factual comparison against {handed_to} on {page_label}, then "
        "connect it to the direct buying reason."
    )


def _grounding_failures(
    brief: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> List[str]:
    text = "\n".join(_iter_leaf_text(brief))
    failures = [
        f"forbidden:{pattern.pattern}"
        for pattern in _FORBIDDEN_PATTERNS
        if pattern.search(text)
    ]
    allowed = _allowed_grounding(evidence)

    for term in sorted(_SAFETY_SENSITIVE_TERMS):
        if term not in allowed["safety_words"] and re.search(
            rf"\b{re.escape(term)}\b",
            text,
            re.IGNORECASE,
        ):
            failures.append(f"safety-sensitive:{term}")

    for leaf in _iter_leaf_text(brief):
        leaf_domains = _unique(
            _normalize_host(domain)
            for domain in _DOMAIN_RE.findall(leaf)
            if _normalize_host(domain) in allowed["domains"]
        )
        if len(leaf_domains) > 3:
            failures.append("overwide-controller-list")

    for domain in _DOMAIN_RE.findall(text):
        normalized = _normalize_host(domain)
        if normalized and normalized not in allowed["domains"]:
            failures.append(f"unknown-domain:{domain}")

    for quote in _QUOTE_RE.findall(text):
        phrase = _norm_phrase(quote)
        if not phrase or len(phrase.split()) < 2:
            continue
        if not _is_checkable_lane_quote(quote, phrase, allowed):
            continue
        ungrounded = _first_ungrounded_lane_token(phrase, allowed)
        if ungrounded:
            failures.append(f"unknown-quoted-lane:{quote}:{ungrounded}")

    for entity, sentence_initial in _extract_named_entities(text):
        if _entity_allowed(entity, allowed):
            continue
        if sentence_initial and _is_multiword_entity(entity):
            first_unallowed = _sentence_initial_unallowed_token(entity, allowed)
            if first_unallowed:
                failures.append(f"unknown-entity:{first_unallowed}")
            continue
        failures.append(f"unknown-entity:{entity}")
    return failures


def _allowed_grounding(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    allowed_terms: Set[str] = set(_INTERNAL_ALLOWED_ENTITIES)
    allowed_domains: Set[str] = set()
    allowed_phrases: Set[str] = set()
    attribute_words: Set[str] = set()
    safety_words: Set[str] = set()

    def add_term(value: Any) -> None:
        text = _clean_str(value)
        if not text:
            return
        allowed_terms.add(_norm_entity(text))
        allowed_phrases.add(_norm_phrase(text))

    def add_attribute_word(word: str) -> None:
        normalized = word.lower()
        if not normalized:
            return
        attribute_words.add(normalized)
        safety_words.add(normalized)
        if normalized == "stick":
            attribute_words.add("sticks")
        elif normalized.endswith("y"):
            attribute_words.add(f"{normalized[:-1]}ies")
        elif not normalized.endswith("s"):
            attribute_words.add(f"{normalized}s")

    def add_domain(value: Any) -> None:
        host = _normalize_host(value)
        if host:
            allowed_domains.add(host)
            add_term(host)
            add_term(_registrable_label(host))

    product = _as_mapping(evidence.get("product"))
    add_term(product.get("title"))
    add_term(product.get("brand"))
    merchant_path = _as_mapping(product.get("merchant_path"))
    for value in merchant_path.values():
        add_term(value)
    attributes = _as_mapping(product.get("attributes"))
    for field, values in attributes.items():
        for attr in _as_str_list(values):
            add_term(attr)
            for word in re.findall(r"[a-z0-9]+", attr.lower()):
                add_attribute_word(word)
            attr_words = set(re.findall(r"[a-z0-9]+", attr.lower()))
            if {"before", "bed"}.issubset(attr_words):
                attribute_words.add("bedtime")
                if field in {"audience", "use_case"}:
                    safety_words.add("bedtime")

    category_battle = _as_mapping(evidence.get("category_battle"))
    for prompt in _as_str_list(category_battle.get("prompts")):
        add_term(prompt)
    for winner in _as_str_list(category_battle.get("winners")):
        add_term(winner)
    for ranked in _as_list(category_battle.get("ranked_by")):
        if isinstance(ranked, Mapping):
            add_domain(ranked.get("host"))

    substitution = _as_mapping(evidence.get("substitution"))
    add_term(substitution.get("handed_to"))
    add_term(substitution.get("on_prompt"))

    for lane in _as_list(evidence.get("open_lanes")):
        if not isinstance(lane, Mapping):
            continue
        add_term(lane.get("query"))
        for why in _as_str_list(lane.get("why_fit")):
            add_term(why)
            for word in re.findall(r"[a-z0-9]+", why.lower()):
                add_attribute_word(word)
            why_words = set(re.findall(r"[a-z0-9]+", why.lower()))
            if {"before", "bed"}.issubset(why_words):
                attribute_words.add("bedtime")

    for lane in _as_list(evidence.get("channel_map")):
        if not isinstance(lane, Mapping):
            continue
        add_term(lane.get("lane"))
        add_term(lane.get("query"))
        for controller in _as_list(lane.get("controlled_by")):
            if isinstance(controller, Mapping):
                add_domain(controller.get("host"))

    for opportunity in _as_list(evidence.get("buyer_path_opportunities")):
        if not isinstance(opportunity, Mapping):
            continue
        add_term(opportunity.get("query"))
        add_term(opportunity.get("exposure"))
        add_term(opportunity.get("route"))
        add_term(opportunity.get("destination"))
        add_term(opportunity.get("merchant_archetype"))
        for move in _as_str_list(opportunity.get("recommended_moves")):
            add_term(move)
            for word in re.findall(r"[a-z0-9]+", move.lower()):
                add_attribute_word(word)
        for controller in _as_list(opportunity.get("controlled_by")):
            if isinstance(controller, Mapping):
                add_domain(controller.get("host"))

    grounded_words = set(attribute_words)
    for value in allowed_terms | allowed_phrases:
        grounded_words.update(re.findall(r"[a-z0-9]+", value))
    grounded_words.update(_SHOPPING_WORDS)
    grounded_words.update(_COMMON_WORDS)
    grounded_words.update(_AI_ENGINE_ENTITIES)

    return {
        "terms": allowed_terms,
        "domains": allowed_domains,
        "phrases": {phrase for phrase in allowed_phrases if phrase},
        "attribute_words": attribute_words,
        "grounded_words": grounded_words,
        "safety_words": safety_words,
    }


def _extract_named_entities(text: str) -> List[Tuple[str, bool]]:
    entities: List[Tuple[str, bool]] = []
    seen_spans: List[Tuple[int, int]] = []
    for segment_start, segment in _proper_sequence_segments(text):
        for match in _PROPER_SEQUENCE_RE.finditer(segment):
            sequence_start = segment_start + match.start()
            sequence_end = segment_start + match.end()
            sequence = _clean_entity(match.group(0))
            if sequence:
                sequence_sentence_initial = _is_sentence_initial(text, sequence_start)
                for chunk_index, chunk in enumerate(re.split(r"\s+(?:and|&)\s+", sequence)):
                    entity = _clean_entity(chunk)
                    if entity and not _entity_is_stopword(entity):
                        entities.append(
                            (entity, sequence_sentence_initial and chunk_index == 0)
                        )
                seen_spans.append((sequence_start, sequence_end))

    for match in _SINGLE_ENTITY_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in seen_spans):
            continue
        sequence = _clean_entity(match.group(0))
        entity = sequence
        if entity and not _entity_is_stopword(entity):
            sentence_initial = _is_sentence_initial(text, match.start())
            if sentence_initial and not _is_brand_shaped(entity):
                continue
            entities.append((entity, sentence_initial))
    out: List[Tuple[str, bool]] = []
    seen: Set[Tuple[str, bool]] = set()
    for entity, sentence_initial in entities:
        key = (_norm_entity(entity), sentence_initial)
        if key in seen:
            continue
        seen.add(key)
        out.append((entity, sentence_initial))
    return out


def _proper_sequence_segments(text: str) -> Iterable[Tuple[int, str]]:
    start = 0
    for match in _QUOTE_BOUNDARY_RE.finditer(text):
        if match.start() > start:
            yield start, text[start:match.start()]
        start = match.end()
    if start < len(text):
        yield start, text[start:]


def _sentence_initial_unallowed_token(
    entity: str,
    allowed: Mapping[str, Any],
) -> Optional[str]:
    tokens = [
        _clean_entity(token)
        for token in re.split(r"\s+", entity)
        if _clean_entity(token)
    ]
    if (
        tokens
        and not _is_brand_shaped(tokens[0])
        and not _entity_allowed(tokens[0], allowed)
    ):
        tokens = tokens[1:]
    while tokens and _is_ignorable_entity_token(tokens[0]):
        tokens = tokens[1:]
    while tokens and _is_ignorable_entity_token(tokens[-1]):
        tokens = tokens[:-1]
    for idx, token in enumerate(tokens):
        if _is_ignorable_entity_token(token):
            continue
        if not _entity_allowed(token, allowed):
            if idx > 0 and any(
                _entity_allowed(previous, allowed)
                for previous in tokens[:idx]
                if not _is_ignorable_entity_token(previous)
            ):
                return " ".join(tokens)
            return token
    return None


def _is_multiword_entity(entity: str) -> bool:
    return len([token for token in re.split(r"\s+", entity.strip()) if token]) > 1


def _is_sentence_initial(text: str, idx: int) -> bool:
    prefix = text[:idx].rstrip(_SENTENCE_PREFIX_STRIP_CHARS)
    return not prefix or prefix[-1] in _SENTENCE_BOUNDARY_CHARS


def _is_brand_shaped(token: str) -> bool:
    return bool(
        re.search(r"[a-z][A-Z]", token)
        or re.search(r"(?:[A-Za-z]\d|\d[A-Za-z])", token)
    )


def _entity_allowed(entity: str, allowed: Mapping[str, Any]) -> bool:
    normalized = _norm_entity(entity)
    if not normalized:
        return True
    if _is_common_entity_word(normalized):
        return True
    if normalized in allowed["terms"]:
        return True
    domain = _normalize_host(entity)
    if domain and domain in allowed["domains"]:
        return True
    if normalized in _INTERNAL_ALLOWED_ENTITIES:
        return True
    for term in allowed["terms"]:
        if len(term.split()) < 2:
            continue
        if _phrase_contains(term, normalized) or _phrase_contains(normalized, term):
            return True
    words = re.findall(r"[a-z0-9]+", normalized)
    if words and all(word in allowed["attribute_words"] for word in words):
        return True
    if words and all(_entity_word_grounded_or_common(word, allowed) for word in words):
        return True
    return False


def _phrase_allowed(phrase: str, allowed_phrases: Set[str]) -> bool:
    if phrase in allowed_phrases:
        return True
    return any(
        _phrase_contains(allowed, phrase) or _phrase_contains(phrase, allowed)
        for allowed in allowed_phrases
        if len(allowed.split()) >= 2
    )


def _is_checkable_lane_quote(
    quote: str,
    phrase: str,
    allowed: Mapping[str, Any],
) -> bool:
    if quote.strip().endswith("?"):
        return False
    if _is_title_case_quote(quote):
        return False
    significant = _significant_quote_tokens(phrase)
    if len(significant) > 7:
        return False
    return _looks_like_lane_quote(phrase, allowed)


def _first_ungrounded_lane_token(
    phrase: str,
    allowed: Mapping[str, Any],
) -> Optional[str]:
    grounded_words = allowed["grounded_words"]
    for token in _significant_quote_tokens(phrase):
        if not _word_grounded(token, grounded_words):
            return token
    return None


def _looks_like_lane_quote(phrase: str, allowed: Mapping[str, Any]) -> bool:
    if phrase in allowed["phrases"]:
        return True
    words = set(re.findall(r"[a-z0-9]+", phrase))
    ordered_words = re.findall(r"[a-z0-9]+", phrase)
    if ordered_words and ordered_words[0] in {"a", "an", "the"} and not (
        words & _SHOPPING_WORDS
    ):
        return False
    if words & _SHOPPING_WORDS:
        return True
    if words & _COMMON_PROSE_WORDS:
        return False
    attribute_overlap = words & allowed["attribute_words"]
    return len(attribute_overlap) >= 2 or (len(words) >= 3 and bool(attribute_overlap))


def _significant_quote_tokens(phrase: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", phrase.lower())
        if token not in _QUOTE_STOPWORDS
        and token not in _CONNECTOR_WORDS
        and token not in _COMMON_PROSE_WORDS
        and token not in _COMMON_WORDS
    ]


def _is_title_case_quote(quote: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z0-9&'’-]*", quote)
    if len(words) < 2:
        return False
    capitalized = [
        word
        for word in words
        if word[:1].isupper() or (len(word) > 1 and word.isupper())
    ]
    return len(capitalized) / len(words) > 0.6


def _word_grounded(token: str, grounded_words: Set[str]) -> bool:
    if token in grounded_words:
        return True
    if token.endswith("ies") and f"{token[:-3]}y" in grounded_words:
        return True
    if token.endswith("s") and token[:-1] in grounded_words:
        return True
    return False


def _entity_word_grounded_or_common(
    token: str,
    allowed: Mapping[str, Any],
) -> bool:
    return _is_common_entity_word(token) or _word_grounded(
        token,
        allowed["grounded_words"],
    )


_COMMON_WORD_SETS = (
    _COMMON_WORDS,
    _COMMON_STEMS,
    _CONNECTOR_WORDS,
    _SHOPPING_WORDS,
    _QUOTE_STOPWORDS,
    _COMMON_PROSE_WORDS,
    _ENTITY_STOPWORD_NORMALIZED,
    _INTERNAL_ALLOWED_ENTITIES,
)


def _common_word_stems(normalized: str) -> Set[str]:
    """Candidate base forms for an inflected word (smarter→smart, routines→
    routine, winning→win, stories→story) so common-word membership covers
    inflections without listing every form."""
    cands = {normalized}
    if len(normalized) > 4 and normalized.endswith("ies"):
        cands.add(normalized[:-3] + "y")
    for suffix in _STEM_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 3:
            stem = normalized[: -len(suffix)]
            cands.add(stem)
            cands.add(stem + "e")  # simpler→simple, using→use
            if suffix in ("ier", "iest"):
                cands.add(stem + "y")  # easier→easy
            if len(stem) >= 2 and stem[-1] == stem[-2]:
                cands.add(stem[:-1])  # running→run, bigger→big
    return cands


def _is_common_entity_word(token: str) -> bool:
    normalized = _norm_entity(token)
    if any(normalized in word_set for word_set in _COMMON_WORD_SETS):
        return True
    # Inflected forms of a common stem (smarter, routines, winning) are common
    # too — never an invented proper noun.
    for candidate in _common_word_stems(normalized):
        if candidate in _COMMON_WORDS or candidate in _COMMON_STEMS:
            return True
    return False


def _is_ignorable_entity_token(token: str) -> bool:
    normalized = _norm_entity(token)
    return (
        normalized in _CONNECTOR_WORDS
        or _entity_is_stopword(token)
        or (token.isupper() and normalized in _ALLCAPS_FUNCTION_WORDS)
    )


def _phrase_contains(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack))


def _iter_leaf_text(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_leaf_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_leaf_text(child)


def _position_band_from_layer(layer: Any) -> Optional[str]:
    mapping = _as_mapping(layer)
    if not mapping:
        return None
    try:
        score = float(mapping.get("score"))
    except (TypeError, ValueError):
        return None
    if score >= 67:
        return "strong"
    if score >= 34:
        return "moderate"
    return "weak"


def _normalize_host(value: Any) -> str:
    text = _clean_str(value).lower()
    if not text:
        return ""
    if "://" in text:
        parsed = urlparse(text)
        text = parsed.hostname or text
    text = text.strip().strip("/").lower()
    text = re.sub(r"^www\.", "", text)
    return text.split("/", 1)[0]


def _registrable_label(host: Any) -> str:
    normalized = _normalize_host(host)
    if not normalized:
        return ""
    labels = [label for label in normalized.split(".") if label]
    if len(labels) < 2:
        return labels[0] if labels else ""
    suffix = ".".join(labels[-2:])
    if suffix in _MULTIPART_TLDS and len(labels) >= 3:
        return labels[-3]
    return labels[-2]


def _norm_entity(value: Any) -> str:
    return re.sub(r"\s+", " ", _clean_str(value).lower()).strip()


def _norm_phrase(value: Any) -> str:
    text = _norm_entity(value)
    text = re.sub(r"[^a-z0-9.&+ -]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_entity(value: str) -> str:
    return _clean_str(value).strip(" ,.;:()[]{}")


def _entity_is_stopword(entity: str) -> bool:
    if entity in _ENTITY_STOPWORDS:
        return True
    normalized = _norm_entity(entity)
    return (
        normalized in _ENTITY_STOPWORD_NORMALIZED
        or normalized in _INTERNAL_ALLOWED_ENTITIES
        or _is_common_entity_word(normalized)
    )


def _as_mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> List[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [
            item
            for item in (_clean_str(v) for v in value)
            if item
        ]
    text = _clean_str(value)
    return [text] if text else []


def _clean_str(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for value in values:
        text = _clean_str(value)
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out
