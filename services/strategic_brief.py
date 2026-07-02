"""Per-SKU strategic brief assembly and grounding validation.

The LLM is only allowed to frame deterministic audit facts. This module builds
the facts, sends the exact brief prompt when enabled/keyed, and rejects any
brief that names entities or lanes outside the evidence block.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import urlparse

from config.settings import settings
from services.buyer_path_stable_controllers import (
    stable_buyer_path_controllers_for_row,
)
from services.buyer_path_controller_quality import (
    controller_profile as build_controller_profile,
    aggregate_controller_profile,
    is_canonical_source_vacuum,
)
from services.llm_synthesis import (
    LLMSynthesisError,
    LLMSynthesisHTTPError,
    MissingLLMKeyError,
    configured_key_for_provider,
    default_model_for_provider,
    normalize_provider,
    synthesize,
)
from services.sku_lane_priority import (
    build_lane_product_evidence,
    build_sideways_wedge,
    has_lane_demand,
    is_third_party_controlled_lane,
    prioritize_lanes,
)

logger = logging.getLogger(__name__)

# Reliability knobs for the LLM ("mainline") brief. The deterministic brief is a
# strictly worse merchant experience, so we work hard to land a grounded LLM
# brief before falling back: retry transient provider failures with backoff, and
# retry grounding rejections with a targeted repair hint so the model self-corrects.
_BRIEF_CONTENT_ATTEMPTS = 3          # regenerations for bad-shape / grounding-failed output
_BRIEF_TRANSPORT_RETRIES = 3         # extra retries for a single transient provider blip
_BRIEF_RETRY_BASE_DELAY_S = 0.5      # exponential backoff base for transient retries
_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

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
- COMPETITORS: EVIDENCE names which competitors win. If grounding_notes.competitor_attributes is
  "not_assessed", EVIDENCE does NOT support competitor product attributes. If it is assessed, you may use ONLY
  attributes_present as competitor PRESENCE facts for the named competitor. NEVER state as fact that a
  competitor lacks, is missing, does not have, is the only one without, or fails to offer a feature. Do not turn
  merchant attributes into competitor deficiencies. You MAY note a likely positioning gap as YOUR INFERENCE,
  marked as such: "incumbents are generally positioned as broad <category>; a dedicated <your differentiator>
  looks like an opening — worth confirming." Your differentiation is YOUR attributes; competitor contrast is
  presence-only when assessed, otherwise an inference to verify.
- CHANNELS: recommend a specific marketplace, retailer, community, forum, social platform, or publisher ONLY
  if it appears in grounding_notes.evidenced_channels. Do NOT assume the merchant already sells on Amazon or
  any marketplace (grounding_notes.merchant_channels = "unknown"). If a lane has no evidenced channel, the move
  is "own your own page/site for this lane first." You may suggest a marketplace/community move only
  CONDITIONALLY: "if you already sell on <evidenced channel>, …". Do NOT invent communities, subreddits,
  influencers, or platforms.
- MERCHANT PATH: respect product.merchant_path. If archetype is "brand", the commercial goal is to drive
  buyers to the brand's own website. If archetype is "channel", the commercial goal is to drive buyers to the
  channel's own website. Do not blur those paths.
- OPERATIONAL ECONOMICS: when buyer_path_opportunities exist, match the prescription to the controller
  strategy. For canonical_source_vacuum or source_authority_gap, write a mechanism-level authority playbook
  in this order: (1) make the merchant page more retrievable by targeting the exact evidenced lane, (2) make it
  more extractable with product/offer/review/FAQ schema, (3) state the lane's evidenced attributes in plain page
  text, (4) build verified reviews/proof to close the authority gap, (5) work the cited source by controller
  type: forum/community = participate in or seed accurate product info in the evidenced discussion; publisher/
  listicle = pitch the evidenced publisher with exact SKU facts and proof; retailer/marketplace = claim or fix
  the evidenced listing, (6) keep SKU facts consistent across the merchant page and cited sources, (7) re-audit
  the same lane and verify materiality before treating exposure as lost buyer traffic. Only after that, add
  first-order offer, starter + replenishment bundle, subscription incentive, and why-buy-direct proof. For
  leading retailer/marketplace competition, keep listing fixes and direct-buy mechanics as the click-winning
  play, with only a light retrieval/schema layer. Do NOT invent discount depths, prices, savings percentages,
  review counts, retailer facts, or margin claims unless they appear in EVIDENCE.
- AUTHORITY HONESTY: never promise that ChatGPT, Gemini, or any AI engine will cite, rank, or route to the
  merchant page. The work is making the page more retrievable, extractable, citable, and authoritative for the
  evidenced lane — but to the merchant SAY THIS IN PLAIN WORDS ("easy for AI to find, quote, and trust"), never
  the internal terms (see PLAIN LANGUAGE below). Keep the caveat: showing up in AI answers is citation evidence,
  not proven buyer traffic, until the lane is re-audited and verified.
- LANES: when you name a search lane or query, reuse the EXACT wording from EVIDENCE. Do not rephrase,
  singularize/pluralize, reorder, or coin a variant. (A positioning phrase for your brand is fine and separate
  — just don't present it as the searched lane.)
- SIDEWAYS DEMAND: if sideways_wedge.recommended_beachhead_lane exists, frame that lane as the first
  beachhead before broad/high-pressure prompts. Explain which broad or weak-fit lanes to not chase yet, using
  only sideways_wedge.head_prompt_pressure and sideways_wedge.do_not_chase_yet.
- FACT vs INFERENCE: only "AI's answers show…/EVIDENCE shows…" statements are facts. Everything else is your
  read — phrase it as inference. Do not use the word "locked" or absolutes like "you cannot do this alone";
  say a query is "owned/controlled by <evidenced source>" and frame Pivota's help as the specific service it
  provides, not as something impossible without it.
- VOICE (apply every rule above SILENTLY): the brief is the merchant's memo. NEVER mention these rules or the
  words "EVIDENCE", "grounding_notes", "not_assessed", "inference", "as fact", or any meta-instruction in the
  output. Write a positioning read as a natural sentence ("incumbents look positioned as broad collagen, not a
  halal bedtime stick — worth confirming"), NOT as a caveat about what you may or may not claim. Never tell the
  merchant what you are or aren't allowed to say.
- PLAIN LANGUAGE (write for a busy shop owner, not an SEO engineer): NEVER use the words "lane", "beachhead",
  "wedge", "materiality", "controller", "canonical" (as a noun), or "vacuum" in the output. Say the plain thing
  instead — a search query is the "'<query>' search" (quote the exact evidenced query); "the first beachhead
  lane" → "the first search worth winning"; "verify materiality" → "check whether it's driving real buyer
  traffic"; "who controls the lane" → "who currently owns that answer". Also NEVER write "retrievable" or
  "extractable" to the merchant — say "easy for AI to find and quote". Expand PDP on first use ("your product
  page").
- NO FORMULA (this is the line between a bespoke memo and mail-merge — the merchant reads several of these briefs
  back to back and WILL notice a repeated skeleton): NEVER use the "Stop chasing/Stop trying to win [broad]
  … Instead, own/do …" construction ANYWHERE in core_decision — not as the opener, not mid-sentence, and not as
  a variant ("Stop trying to win that broad query", "Stop chasing broad category queries", "don't fight the
  broad search — instead…"). State the positive call directly instead of framing it as stop-this-then-do-that.
  Likewise do NOT open your_angle with "Reframe from 'a X' to 'the Y'"; do NOT open substitution_play with
  "When AI … hands the buyer to …. To win those buyers back, position…"; do NOT end first_moves with a
  boilerplate "re-audit … before treating exposure as material" step. Lead every field with the ONE thing true only of THIS product — its specific angle, the exact competitor
  AI named, or the exact source that controls the answer — and build the sentence around that fact, not around a
  reusable frame. Vary sentence shape from field to field. Use each of these stock phrases AT MOST ONCE in the
  whole brief and never as filler: "citable and buyable", "starter + replenishment bundle", "why-buy-direct
  proof", "first-order offer". State the reasons to buy direct once, in plain words — never as a repeated
  four-item list.

WRITE the brief as JSON with these fields — each must be specific to THIS product and EVIDENCE:
- position: one honest sentence on where THIS product really stands, in words that fit this product's actual
  situation. Do NOT reuse a stock label like "niche challenger, strong when named, invisible in the category" —
  ground it in what the evidence shows for this SKU.
- core_decision: the ONE big strategic call for THIS product, stated plainly and decisively — the action and the
  real reason from evidence (the specific product fact, the exact competitor AI named, or the exact source that
  controls the answer that makes this the call). It may imply what to stop doing, but do NOT format it as a
  "stop X, instead do Y" template — open with the product-specific reason, not the reusable verb frame. GOOD
  (opens with the product-specific reason, no template): "Your shea-butter-and-green-tea butter is the only
  thing in this category with real reviews behind it, so put those reviews on your own page and make it the
  answer for the 'reviews hair butter treatment' search before spending anything on the crowded 'best hair mask'
  question." BAD (template opener): "Stop chasing broad category queries… Instead, own the reviews lane first."
- why_you_lose: WHY the category winners win — the fact is that the evidenced SOURCES cite/rank the named
  winners. Attribute their advantage to that SOURCE relationship (e.g. "Forbes lists them, which points to
  editorial authority the AI trusts"), phrased as YOUR inference. Do NOT state competitor product attributes,
  qualities, reviews, distribution, or authority AS FACT, and do NOT claim competitor feature gaps — you have
  only which source cited whom. Describe the merchant's own absence plainly ("your page is not cited there
  yet"), never as the merchant "lacking" or being "without" something.
- your_angle: the defensible positioning = the merchant's differentiating attributes that the named product
  actually has. Position it as a specific product rather than a generic {category} — without a fixed "reframe
  from 'a X' to 'the Y'" formula, and without saying winners lack those attributes as fact. Use exact EVIDENCE
  query wording where their differentiation IS the answer.
- traffic_strategy: a ranked list of where the missed, WINNABLE demand is + who controls each channel
  (name only sources/retailers/communities from grounding_notes.evidenced_channels) + the realistic path in.
  If no channel is evidenced for a lane, say to own your page/site first. Marketplace/community moves must be
  conditional ("if you already sell on <evidenced channel>..."). Explicitly say which big lanes to NOT chase
  yet and why.
- substitution_play: if a substitution is present, how to win those buyers back (comparison/positioning vs
  the named substitute), else null. Vary the phrasing per product — do NOT always open with "When AI … hands
  the buyer to …".
- first_moves: 3-5 concrete actions that EXECUTE the strategy above, in priority order, each tied to a
  strategic reason (not generic "add an FAQ" — "add the halal + bedtime story to your page so it is more
  citable for the lane you're claiming"). When EVIDENCE shows weak reseller/source-route exposure, at
  least one first move must name the exact lane and source-authority repair, using the mechanism order above
  and controller-type source route. When EVIDENCE shows credible retailer/marketplace competition, at least
  one first move must name the exact lane, listing fix, light retrieval/schema layer, and the operational reason
  to buy from the merchant-controlled page (offer, bundle, subscription, or why-buy-direct proof). Do NOT pad the
  list to a fixed length or close every brief with the same "re-audit … before treating exposure as material"
  step — include a re-measure step only when it is genuinely the most useful next action, phrased in this
  product's own terms.
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
_COMMUNITY_CONTROLLER_TYPES = {"community", "forum", "reddit", "social"}
_PUBLISHER_CONTROLLER_TYPES = {"editorial", "publisher", "video"}
_LISTING_CONTROLLER_TYPES = {"marketplace", "retailer"}
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
# Apostrophe inside a word (contraction/possessive: don't, publisher's, it's).
# Stripped before quoted-lane scanning so it is not mistaken for a quote delimiter.
_INTRAWORD_APOSTROPHE_RE = re.compile(r"(?<=\w)['’](?=\w)")
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
_COMPETITOR_GENERIC_TERMS = {
    "category winners",
    "competitor",
    "competitors",
    "incumbent",
    "incumbents",
    "rival",
    "rivals",
    "substitute",
    "substitutes",
    "winner",
    "winners",
}
_COMPETITOR_LACK_PATTERNS = (
    re.compile(r"\b(?:lacks?|lack of|missing)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:doesn['’]?t|does not|don['’]?t|do not)\s+"
        r"(?:offer|have|carry|include|feature)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwithout\s+(?:a|an|the)?\s*[a-z0-9][a-z0-9-]*", re.IGNORECASE),
    re.compile(r"\bno\s+(?:evident\s+)?[a-z0-9][a-z0-9-]*", re.IGNORECASE),
)
_COMPETITOR_EXCLUSIVE_RE = re.compile(
    r"\bonly\b[^.!?\n]{0,80}\b(?:has|have|offers?|with|for|featuring)\b",
    re.IGNORECASE,
)
# Market-exclusivity ("the only one/brand/seller with <attr>") is a disguised
# competitor-deficiency claim — it asserts everyone else lacks the attribute — so
# it must fail on its own, even when no competitor is named. Restricted to
# market-noun subjects so merchant-self "only your page offers X" stays allowed.
_COMPETITOR_MARKET_EXCLUSIVE_RE = re.compile(
    r"\bonly\s+(?:one|brand|brands|company|companies|seller|sellers|"
    r"player|players|option|options|maker|makers|vendor|vendors|"
    r"retailer|retailers|store|stores|name|names|product|products)\b",
    re.IGNORECASE,
)
_COMPETITOR_ATTRIBUTE_CLAIM_RE = re.compile(
    r"\b(?P<verb>known for|associated with|built around|positioned as|"
    r"positioned around|positioned for|features?|offers?|has|have|carries|"
    r"uses|leans on|centers on|centered on)\s+"
    r"(?P<attrs>[^.;:\n,]{2,120})",
    re.IGNORECASE,
)
_COMPETITOR_CLAIM_COMMON_WORDS = {
    "also",
    "answer",
    "answers",
    "authority",
    "broad",
    "category",
    "distribution",
    "general",
    "line",
    "mainstream",
    "market",
    "position",
    "positioning",
    "product",
    "products",
    "publisher",
    "retail",
    "retailer",
    "review",
    "reviews",
    "source",
    "sources",
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
    merchant_host: Optional[str] = None,
    competitor_attributes: Optional[Any] = None,
) -> Dict[str, Any]:
    del primary_gaps, scores
    opportunity_map = _as_mapping(opportunity)
    identity_map = _as_mapping(identity)
    attributes = _attribute_evidence(attribute_graph)
    title = _clean_str(sku_title) or _clean_str(identity_map.get("name")) or "this SKU"
    anchors = _as_mapping(identity_map.get("anchors"))
    brand = _clean_str(anchors.get("brand"))
    merchant_path = _merchant_path(identity=identity_map, opportunity=opportunity_map)
    product_evidence = _as_mapping(opportunity_map.get("product_evidence")) or build_lane_product_evidence(
        product={"title": title, "brand": brand, "category": anchors.get("category")},
        attribute_graph=attribute_graph,
        identity=identity_map,
        sku_title=title,
    )

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
        competitor_attributes=competitor_attributes,
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
            product_evidence=product_evidence,
            merchant_host=merchant_host,
        ),
        "sideways_wedge": _as_mapping(opportunity_map.get("sideways_wedge")) or build_sideways_wedge(
            _as_list(opportunity_map.get("per_prompt")),
            product_evidence=product_evidence,
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
    system, base_user = build_sku_brief_prompt(evidence)

    # The LLM brief is the merchant-facing "mainline"; the deterministic brief is
    # a strictly worse read, so we only fall back after genuinely exhausting the
    # LLM. Two failure modes each get their own recovery:
    #   - transient provider errors (timeout / network / 429 / 5xx): retry the
    #     same call with exponential backoff (a single blip must NOT drop the
    #     merchant to the deterministic brief);
    #   - bad-shape or grounding-rejected output: regenerate, feeding the model a
    #     targeted repair hint so it self-corrects instead of falling back.
    user = base_user
    for _attempt in range(_BRIEF_CONTENT_ATTEMPTS):
        try:
            result = await _synthesize_with_transport_retry(
                system=system,
                user=user,
                provider=selected_provider,
                model=selected_model,
            )
        except LLMSynthesisError as exc:
            logger.warning(
                "strategic brief LLM call failed (%s: %s); using deterministic fallback",
                type(exc).__name__,
                exc,
            )
            break
        brief = _parse_brief_json(result.get("text"))
        if not isinstance(brief, dict) or not _has_required_shape(brief):
            user = f"{base_user}\n\n{_SHAPE_REPAIR_HINT}"
            continue
        failures = _grounding_failures(brief, evidence)
        if not failures:
            return _with_source(brief, "llm")
        # Grounding rejected the draft — tell the model exactly what to fix so the
        # next attempt can land the mainline brief rather than falling back.
        user = f"{base_user}\n\n{_grounding_repair_hint(failures)}"
    else:
        logger.warning(
            "strategic brief exhausted %d LLM attempts (grounding/shape); "
            "using deterministic fallback",
            _BRIEF_CONTENT_ATTEMPTS,
        )
    return _with_source(_validated_deterministic_brief(evidence), "deterministic")


def _is_retryable_synthesis_error(exc: LLMSynthesisError) -> bool:
    """A transient provider failure worth retrying vs. a fatal one. Missing key,
    unsupported provider, and 4xx (except throttling) are fatal — retrying just
    wastes time and delays the deterministic fallback. Transport failures (no
    status code) and 429/5xx are transient."""
    if isinstance(exc, MissingLLMKeyError):
        return False
    if isinstance(exc, LLMSynthesisHTTPError):
        if exc.status_code is None:
            return True  # timeout / network / transport failure
        return exc.status_code in _RETRYABLE_HTTP_STATUS
    return False


async def _synthesize_with_transport_retry(
    *,
    system: str,
    user: str,
    provider: str,
    model: str,
) -> Dict[str, Any]:
    """synthesize() with exponential-backoff retry on transient provider errors.
    Raises the last error once retries are exhausted or the error is fatal."""
    delay = _BRIEF_RETRY_BASE_DELAY_S
    for transport_attempt in range(_BRIEF_TRANSPORT_RETRIES + 1):
        try:
            return await synthesize(
                system=system,
                user=user,
                provider=provider,
                model=model,
                # Headroom for the full 8-field JSON brief; 1200 truncated to
                # invalid JSON (a "bad shape" retry / avoidable fallback).
                max_tokens=1600,
            )
        except LLMSynthesisError as exc:
            if (
                _is_retryable_synthesis_error(exc)
                and transport_attempt < _BRIEF_TRANSPORT_RETRIES
            ):
                logger.info(
                    "strategic brief transient LLM error (%s), retry %d/%d after %.1fs",
                    type(exc).__name__,
                    transport_attempt + 1,
                    _BRIEF_TRANSPORT_RETRIES,
                    delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    # Unreachable: the loop either returns or re-raises on the last attempt.
    raise LLMSynthesisError("synthesis retry loop exhausted", provider=provider)


_SHAPE_REPAIR_HINT = (
    "Your previous reply was not valid JSON with every required field. Reply with "
    "ONLY a single JSON object containing exactly these keys: position, "
    "core_decision, why_you_lose, your_angle, traffic_strategy, substitution_play, "
    "first_moves, diy_vs_pivota. No prose outside the JSON."
)

# Map the internal grounding-failure codes to a plain corrective instruction so a
# retry can fix the specific violation instead of dropping to the deterministic brief.
_GROUNDING_REPAIR_RULES: Tuple[Tuple[str, str], ...] = (
    ("competitor-exclusive-claim",
     "Do not use 'only' to say one brand/product/seller has or does something. "
     "Rephrase without any exclusivity claim about the field."),
    ("competitor-lack-claim",
     "Do not use the words 'no', 'not', 'without', 'lacks', 'missing', or 'only' in "
     "ANY sentence that names a competitor. If you need to note that the merchant is "
     "absent, put it in a SEPARATE sentence that names no competitor (e.g. 'Your page "
     "is not cited there yet.'). Never say a competitor lacks or is missing anything."),
    ("unassessed-competitor-attribute",
     "Do not attribute any feature, ingredient, quality, reviews, distribution, or "
     "authority to a named competitor. Say only that the cited source lists/recommends "
     "them; frame their advantage as YOUR inference about the source, not a stated fact."),
    ("ungrounded-competitor-attribute",
     "Do not attribute any feature, ingredient, quality, reviews, distribution, or "
     "authority to a named competitor. Say only that the cited source lists/recommends "
     "them; frame their advantage as YOUR inference about the source, not a stated fact."),
    ("unknown-domain",
     "Only name websites, retailers, publishers, or communities that appear verbatim "
     "in the evidence. Remove any others."),
    ("unknown-quoted-lane",
     "Only put a search query in quotes if it appears verbatim in the evidence."),
    ("safety-sensitive",
     "Remove any unproven health, medical, or safety claim that is not in the evidence."),
    ("overwide-controller-list",
     "Name at most three sources in a single sentence."),
    ("forbidden",
     "Remove all numbers, prices, percentages, scores, '/100', and internal jargon "
     "(retrievable, extractable, lane, materiality, controller, canonical, vacuum)."),
)


def _grounding_repair_hint(failures: List[str]) -> str:
    """Build a targeted rewrite instruction from the grounding-failure codes."""
    instructions: List[str] = []
    seen: Set[str] = set()
    for failure in failures:
        code = str(failure).split(":", 1)[0]
        for prefix, instruction in _GROUNDING_REPAIR_RULES:
            if code.startswith(prefix) and instruction not in seen:
                seen.add(instruction)
                instructions.append(instruction)
                break
    if not instructions:
        instructions.append(
            "Only mention brands, products, sources, and search queries that appear "
            "verbatim in the evidence."
        )
    bullet_list = "\n".join(f"- {line}" for line in instructions)
    return (
        "Your previous draft broke these grounding rules. Rewrite the full JSON "
        "brief, fixing every one, and change nothing else:\n" + bullet_list
    )


def _with_source(
    brief: Optional[Dict[str, Any]], source: str
) -> Optional[Dict[str, Any]]:
    """Tag a brief with the path that produced it ("llm" vs "deterministic") so
    the fallback rate is measurable from stored audits. Applied AFTER grounding
    validation so the marker never affects the grounding check. None passes
    through unchanged (both paths failed → no brief)."""
    if isinstance(brief, dict):
        brief["brief_source"] = source
    return brief


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
        source_roles = _stable_source_role_chips(row)
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
    product_evidence: Mapping[str, Any],
    merchant_host: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows: List[Mapping[str, Any]] = []
    for row in _as_list(opportunity.get("per_prompt")):
        if not isinstance(row, Mapping):
            continue
        query = _clean_str(row.get("query"))
        if not query:
            continue
        if not is_third_party_controlled_lane(row):
            continue
        if not has_lane_demand(row):
            continue
        rows.append(row)
    prioritized = prioritize_lanes(
        rows,
        product_evidence=product_evidence,
    )
    opportunities = [_buyer_path_opportunity(row, merchant_path) for row in prioritized[:5]]
    if opportunities:
        _apply_lead_aggregate_profile(
            opportunities,
            prioritized,
            merchant_path=merchant_path,
            merchant_host=merchant_host,
        )
    return opportunities


def _apply_lead_aggregate_profile(
    opportunities: List[Dict[str, Any]],
    prioritized: List[Mapping[str, Any]],
    *,
    merchant_path: Mapping[str, Any],
    merchant_host: Optional[str] = None,
) -> None:
    """Stabilize the lead opportunity's controller archetype by aggregating
    controller evidence across all qualifying lanes (same dominance rule the
    next-best-action layer uses), so the brief framing does not flip with one
    noisy hero prompt's cited sources."""

    groups = [
        chips
        for row in prioritized
        if isinstance(row, Mapping)
        for chips in (stable_buyer_path_controllers_for_row(row),)
        if chips
    ]
    if not groups:
        return
    lead_profile = aggregate_controller_profile(
        groups,
        exclude_hosts=[merchant_host] if merchant_host else None,
    )
    if not _as_list(lead_profile.get("classified_controllers")):
        return
    lead = opportunities[0]
    lead_row = prioritized[0] if prioritized else {}
    lead["controller_profile"] = lead_profile
    lead["controller_strategy"] = lead_profile.get("strategy")
    lead["controller_strategy_label"] = lead_profile.get("label")
    lead["exposure_confidence"] = lead_profile.get("exposure_confidence")
    lead["exposure_read"] = lead_profile.get("exposure_read")
    named = [host for host in _as_list(lead_profile.get("controllers")) if host]
    if named:
        existing = {
            _clean_str(_as_mapping(ctrl).get("host")): _as_mapping(ctrl)
            for ctrl in _as_list(lead.get("controlled_by"))
        }
        role_by_host = _aggregate_role_by_host(lead_profile)
        lead["controlled_by"] = [
            dict(
                existing.get(host)
                or {"host": host, "role": role_by_host.get(host, "third-party")}
            )
            for host in named[:3]
        ]
    lead["recommended_moves"] = _buyer_path_moves(
        merchant_path,
        lead_profile,
        row=lead_row,
        controllers=_as_list(lead.get("controlled_by")),
    )


def _aggregate_role_by_host(profile: Mapping[str, Any]) -> Dict[str, str]:
    role_by_host: Dict[str, str] = {}
    for row in _as_list(profile.get("classified_controllers")):
        if not isinstance(row, Mapping):
            continue
        host = _clean_str(row.get("host"))
        role = _clean_str(row.get("input_role")) or _clean_str(row.get("type"))
        if host and role:
            role_by_host[host] = role
    for host in _as_list(profile.get("source_authority_controllers")):
        if host:
            role_by_host.setdefault(host, "publisher")
    for host in _as_list(profile.get("known_retail_controllers")):
        if host:
            role_by_host[host] = "retailer"
    for host in _as_list(profile.get("leading_controllers")):
        if host:
            role_by_host[host] = "retailer"
    return role_by_host


def _lane_priority_fields(row: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in (
        "lane_priority_score",
        "merchant_fit_score",
        "conversion_fit_score",
        "merchant_fit_reasons",
        "fit_penalties",
        "selection_reason",
    ):
        if key in row:
            out[key] = row.get(key)
    return out


def _buyer_path_opportunity(
    row: Mapping[str, Any],
    merchant_path: Mapping[str, Any],
) -> Dict[str, Any]:
    controllers = _buyer_path_controllers(row)
    profile = build_controller_profile(controllers)
    route = _clean_str(row.get("source_route")).lower()
    ownership = _clean_str(row.get("ownership_state")).lower()
    route_label = route or ownership.replace("-owned", "") or "third-party"
    return {
        "query": _clean_str(row.get("query")),
        "exposure": ownership or None,
        "route": route_label,
        "controlled_by": controllers,
        "controller_strategy": profile.get("strategy"),
        "controller_strategy_label": profile.get("label"),
        "controller_profile": profile,
        "exposure_confidence": profile.get("exposure_confidence"),
        "exposure_read": profile.get("exposure_read"),
        "destination": _clean_str(merchant_path.get("destination")),
        "merchant_archetype": _clean_str(merchant_path.get("archetype")),
        "recommended_moves": _buyer_path_moves(
            merchant_path,
            profile,
            row=row,
            controllers=controllers,
        ),
        **_lane_priority_fields(row),
    }


def _buyer_path_controllers(row: Mapping[str, Any]) -> List[Dict[str, str]]:
    return _unique_host_roles(stable_buyer_path_controllers_for_row(row))[:3]


def _buyer_path_moves(
    merchant_path: Mapping[str, Any],
    controller_profile: Optional[Mapping[str, Any]] = None,
    *,
    row: Optional[Mapping[str, Any]] = None,
    controllers: Optional[List[Mapping[str, Any]]] = None,
) -> List[str]:
    page = _clean_str(merchant_path.get("page_label")) or "merchant-controlled page"
    profile = _as_mapping(controller_profile)
    lane = _clean_str(_as_mapping(row).get("query")) or "this lane"
    controller_phrase = _controller_phrase(list(controllers or []))
    attributes = _buyer_path_attribute_phrase(_as_mapping(row))
    if is_canonical_source_vacuum(profile) or _clean_str(profile.get("strategy")) == "source_authority_gap":
        return [
            (
                f"Build {page} to rank for the exact lane {lane}: use the lane wording "
                "in title, H1, metadata, and body copy so it is more retrievable and citable."
            ),
            (
                f"Add product/offer/review/FAQ schema on {page} for {lane}, covering "
                "price, availability, reviews, and decision questions."
            ),
            (
                f"State {attributes} in plain page text for {lane}, not only in images, "
                "PDFs, or variant labels."
            ),
            (
                f"Build verified review and proof signals on {page} so it is more "
                f"authoritative for {lane}."
            ),
            _sentence(
                _controller_source_route_action(profile, controller_phrase, lane, page)
            ),
            (
                f"Keep SKU name, {attributes}, images, availability, and canonical URL "
                f"consistent across {page} and {controller_phrase}."
            ),
            (
                f"Re-audit {lane} after the changes; verify whether exposure becomes "
                "more citable through the merchant path before treating it as material buyer traffic."
            ),
            (
                "After the page is more retrievable, extractable, and authoritative, add "
                "first-order offer, starter + replenishment bundle, subscription incentive, "
                "and why-buy-direct proof."
            ),
        ]
    return [
        f"Make {page} the more citable + buyable canonical page for this lane.",
        "Add a first-order offer without inventing a discount depth.",
        "Add a starter + replenishment bundle.",
        "Add a subscription incentive where the product supports replenishment.",
        "Add why-buy-direct proof: guarantee, samples, loyalty, returns, stock, and fresh facts.",
    ]


def _buyer_path_attribute_phrase(row: Mapping[str, Any]) -> str:
    values = (
        _as_str_list(row.get("attribute_basis"))
        or _as_str_list(row.get("why_fit"))
        or _as_str_list(row.get("evidence"))
    )
    return _phrase_join(values[:5], "the evidenced lane attributes")


def _controller_types(profile: Mapping[str, Any]) -> List[str]:
    out: List[str] = []
    for row in _as_list(profile.get("classified_controllers")):
        if not isinstance(row, Mapping):
            continue
        for key in ("input_role", "type"):
            value = _clean_str(row.get(key)).lower()
            if value:
                out.append(value)
    return out


def _controller_source_route_move_type(profile: Mapping[str, Any]) -> str:
    types = set(_controller_types(profile))
    if types & _COMMUNITY_CONTROLLER_TYPES:
        return "community_source_participation"
    if types & _PUBLISHER_CONTROLLER_TYPES:
        return "publisher_source_pitch"
    if types & _LISTING_CONTROLLER_TYPES:
        return "retailer_listing_accuracy"
    return "evidenced_source_update"


def _split_controllers_by_move_type(profile: Mapping[str, Any]):
    forum: List[str] = []
    publisher: List[str] = []
    other: List[str] = []
    seen: set = set()
    for row in _as_list(profile.get("classified_controllers")):
        if not isinstance(row, Mapping):
            continue
        host = _clean_str(row.get("host"))
        if not host or host in seen:
            continue
        seen.add(host)
        role = (_clean_str(row.get("input_role")) or _clean_str(row.get("type"))).lower()
        if role in _COMMUNITY_CONTROLLER_TYPES:
            forum.append(host)
        elif role in _PUBLISHER_CONTROLLER_TYPES:
            publisher.append(host)
        else:
            other.append(host)
    return forum, publisher, other


def _controller_source_route_action(
    profile: Mapping[str, Any],
    controller_phrase: str,
    lane: str,
    page: str,
) -> str:
    move_type = _controller_source_route_move_type(profile)
    if move_type == "community_source_participation":
        forum, publisher, other = _split_controllers_by_move_type(profile)
        if publisher or other:
            # Mixed controller sets: address each by its own play instead of
            # calling non-community sources part of "the discussion".
            clauses: List[str] = []
            if forum:
                clauses.append(
                    f"participate in or seed accurate product info in the "
                    f"{_phrase_join(forum, controller_phrase)} discussion"
                )
            if publisher:
                clauses.append(
                    f"pitch {_phrase_join(publisher, controller_phrase)} with exact SKU "
                    "facts, proof assets, images, and availability"
                )
            if other:
                clauses.append(
                    f"work the evidenced source trail around "
                    f"{_phrase_join(other, controller_phrase)}"
                )
            return (
                f"{_phrase_join(clauses, '')} for {lane}, using only facts already "
                f"published on {page}"
            )
        return (
            f"participate in or seed accurate product info in the {controller_phrase} "
            f"discussion for {lane}, using only facts already published on {page}"
        )
    if move_type == "publisher_source_pitch":
        return (
            f"pitch {controller_phrase} for {lane} with exact SKU facts, proof "
            f"assets, images, availability, and the canonical page at {page}"
        )
    if move_type == "retailer_listing_accuracy":
        return (
            f"claim or fix the {controller_phrase} listing for {lane}: title, "
            "images, variant, stock, authorization, and SKU facts"
        )
    return (
        f"work the evidenced source trail around {controller_phrase} for {lane} "
        f"with the same facts published on {page}"
    )


def _sentence(text: str) -> str:
    cleaned = _clean_str(text)
    if not cleaned:
        return ""
    return cleaned[:1].upper() + cleaned[1:].rstrip(".") + "."


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
        controlled_by = _unique_host_roles(_stable_source_role_chips(row))
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
    competitor_attributes: Optional[Any] = None,
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
        "competitor_attributes": _competitor_attributes_note(competitor_attributes),
        "merchant_channels": "unknown",
        "evidenced_channels": _unique_host_roles(evidenced_channels),
    }


def _competitor_attributes_note(value: Optional[Any]) -> Any:
    if value == "not_assessed" or value is None:
        return "not_assessed"
    if not isinstance(value, Mapping):
        return "not_assessed"
    if _clean_str(value.get("status")).lower() != "assessed":
        return "not_assessed"
    competitor = _clean_str(value.get("competitor"))
    attributes = _unique(_as_str_list(value.get("attributes_present")))[:8]
    if not competitor or not attributes:
        return "not_assessed"
    attr_by_norm = {_norm_phrase(attr): attr for attr in attributes}
    evidence_rows: List[Dict[str, str]] = []
    for item in _as_list(value.get("evidence")):
        if not isinstance(item, Mapping):
            continue
        attr = attr_by_norm.get(_norm_phrase(item.get("attribute")))
        provider = _clean_str(item.get("provider"))
        verbatim = _clean_str(item.get("verbatim"))
        if not attr or not provider or not verbatim:
            continue
        evidence_rows.append({
            "attribute": attr,
            "provider": provider,
            "verbatim": verbatim[:240],
        })
    if not evidence_rows:
        return "not_assessed"
    return {
        "status": "assessed",
        "competitor": competitor,
        "attributes_present": attributes,
        "evidence": evidence_rows[:8],
        "note": "Grounded presence only - not a claim the competitor lacks anything else.",
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
    rows.extend(_stable_source_role_chips(alert))
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
        rows.extend(_stable_source_role_chips(row))
        rows.extend(_stable_source_role_chips(substitution))
    return _unique_host_roles(rows)


def _stable_source_role_chips(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    stable = stable_buyer_path_controllers_for_row(row)
    if stable:
        return stable
    source_summary = _as_mapping(row.get("source_summary"))
    if "buyer_path_controllers" in source_summary:
        return []
    repeated: List[Dict[str, Any]] = []
    for source in _source_role_chips(row):
        if int(source.get("times_cited") or 0) >= 2:
            repeated.append(source)
    return repeated


def _source_role_chips(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    chips: List[Dict[str, Any]] = []
    for source in _as_list(row.get("source_roles")):
        if not isinstance(source, Mapping):
            continue
        host = _normalize_host(source.get("host"))
        if not host:
            continue
        role = _clean_str(source.get("role")) or "unclassified"
        chip: Dict[str, Any] = {"host": host, "role": role}
        if source.get("times_cited") is not None:
            chip["times_cited"] = source.get("times_cited")
        chips.append(chip)
    if chips:
        return chips

    source_summary = _as_mapping(row.get("source_summary"))
    for source in _as_list(source_summary.get("top_cited_hosts")):
        if not isinstance(source, Mapping):
            continue
        host = _normalize_host(source.get("host"))
        if host:
            chip = {"host": host, "role": "unclassified"}
            if source.get("times_cited") is not None:
                chip["times_cited"] = source.get("times_cited")
            chips.append(chip)
    return chips


def _unique_host_roles(rows: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
        item: Dict[str, Any] = {"host": host, "role": role}
        if row.get("times_cited") is not None:
            item["times_cited"] = row.get("times_cited")
        out.append(item)
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


def _deterministic_traffic_how(
    opportunity: Mapping[str, Any],
    *,
    page_label: str,
    default_how: str,
) -> str:
    profile = _as_mapping(opportunity.get("controller_profile")) or build_controller_profile(
        _as_list(opportunity.get("controlled_by"))
    )
    if is_canonical_source_vacuum(profile):
        return (
            f"Make {page_label} more retrievable and extractable for the exact lane, "
            "state the evidenced attributes in text, build reviews/proof, work the "
            "cited source by controller type, keep source facts consistent, re-audit "
            "and verify materiality, then add offer, bundle, subscription, and "
            "why-buy-direct proof last."
        )
    if _clean_str(profile.get("strategy")) == "source_authority_gap":
        return (
            f"Make {page_label} more retrievable, extractable, and authoritative for "
            "the exact lane, work the evidenced source trail by controller type, keep "
            "facts consistent, re-audit and verify materiality, then add direct-buy "
            "mechanics last."
        )
    return default_how


def _is_plausible_query(query: str) -> bool:
    """Guard against a product description (or other long/label-prefixed blob)
    leaking into a slot that expects a short search query. Real shopper queries
    are short ("reviews hair mask / deep conditioning treatment"); a raw
    description ("description a gentle scrub formulated with a natural exfoliator
    …") is not, and must never be inlined verbatim into merchant-facing prose."""
    q = _clean_str(query)
    if not q:
        return False
    if len(q) > 90 or len(q.split()) > 12:
        return False
    if q.lower().startswith(("description ", "description:")):
        return False
    return True


def _deterministic_wedge_decision(sideways_wedge: Mapping[str, Any]) -> str:
    beachhead = _as_mapping(sideways_wedge.get("recommended_beachhead_lane"))
    query = _clean_str(beachhead.get("query"))
    if not _is_plausible_query(query):
        return ""
    deferred = next(
        (
            _clean_str(item.get("query"))
            for item in _as_list(sideways_wedge.get("do_not_chase_yet"))
            if isinstance(item, Mapping) and _clean_str(item.get("query"))
        ),
        "",
    )
    if deferred:
        return (
            f"Win the '{query}' search first and hold off on '{deferred}' for now — "
            "the tighter search is more specific to this product, so your own page can "
            "more easily become the one AI cites and buyers choose."
        )
    return (
        f"Win the '{query}' search first — it is specific to this product and already "
        "shows demand that other sites are capturing."
    )


def _low_signal_brief(evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """Grounding-safe brief for SKUs with no buyer-path evidence yet.

    A not-yet-visible SKU has no probes, citations, competitors, or lanes, so the
    allowed-grounding set is sparse and any evidence-keyed brief (LLM or the lane-
    driven deterministic path) gets rejected — historically yielding None and a
    silent fall-through to the generic NBA boilerplate.

    This brief is built ONLY from product facts that are already in the allowed-
    grounding set (title, merchant page label/destination, evidenced attributes)
    plus generic, non-named guidance. It names no competitor, domain, search lane,
    statistic, or safety-sensitive claim, so it passes validate_grounding by
    construction (see _allowed_grounding: product.title and merchant_path.* are
    add_term'd, attributes feed _brief_angle_terms). The honest call for an
    invisible SKU is to make its own page exist, be specific, and become citable
    before any conversion play — never fabricated specifics.
    """
    product = _as_mapping(evidence.get("product"))
    title = _clean_str(product.get("title")) or "this SKU"
    merchant_path = _as_mapping(product.get("merchant_path"))
    page_label = _clean_str(merchant_path.get("page_label")) or "the merchant-controlled page"
    destination = _clean_str(merchant_path.get("destination")) or "the merchant-controlled website"
    attributes = _as_mapping(product.get("attributes"))
    angle_terms = _brief_angle_terms(attributes)

    first_moves = [
        (
            f"Make {page_label} the complete, specific canonical page for {title}: "
            "full description, images, specs, price, and availability in plain page text."
        ),
        (
            f"State {angle_terms} in plain text on {page_label}, and add product, offer, "
            "review, and FAQ schema so the page is more retrievable and extractable."
        ),
        (
            f"Build verified reviews and proof on {page_label} so {destination} reads as the "
            "most authoritative source for this product."
        ),
        (
            f"Re-audit {title} after the page is live and complete, and verify whether it has "
            "started to surface in AI shopping answers before treating any lane as lost."
        ),
    ]
    self_serve = [
        f"Make {page_label} complete and specific for {title} with full text, images, and schema.",
        "Keep product facts, price, stock, and availability fresh and consistent.",
        "Re-audit after the page is live to verify whether it surfaces in AI shopping answers.",
    ]
    return {
        "position": (
            f"{title} is not yet surfacing in AI shopping answers, so there is no grounded "
            "demand or competitive read to act on yet."
        ),
        "core_decision": (
            f"Make {page_label} the complete, specific, citable canonical page for {title} first; "
            "getting the product retrievable and extractable comes before any conversion play."
        ),
        "why_you_lose": (
            f"AI's answers do not yet reference {title}, which suggests {destination} is not yet a "
            "retrievable, extractable, authoritative source AI shopping assistants can cite for "
            "this product."
        ),
        "your_angle": (
            f"Lead with what makes this product specific — {angle_terms} — so that once "
            f"{page_label} is complete, it is the page AI cites and buyers choose."
        ),
        "traffic_strategy": [],
        "substitution_play": None,
        "first_moves": first_moves,
        "diy_vs_pivota": {
            "self_serve": self_serve,
            "pivota": (
                "Pivota makes the canonical page more citable, buyable, and agent-checkout ready, "
                "then monitors whether this product starts to surface in AI shopping answers."
            ),
        },
    }


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
        # No lane/buyer-path evidence (not-yet-visible SKU): fall back to the
        # grounding-safe low-signal brief instead of returning None, which used
        # to silently drop the per-SKU brief to generic NBA boilerplate.
        return _low_signal_brief(evidence)
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
    lead_profile = _as_mapping(lead.get("controller_profile"))
    if not _as_list(lead_profile.get("classified_controllers")):
        lead_profile = build_controller_profile(_as_list(lead.get("controlled_by")))
    vacuum_strategy = is_canonical_source_vacuum(lead_profile)
    source_authority_strategy = _clean_str(lead_profile.get("strategy")) == "source_authority_gap"
    sideways_wedge = _as_mapping(evidence.get("sideways_wedge"))
    wedge_decision = _deterministic_wedge_decision(sideways_wedge)
    source_action = _sentence(
        _controller_source_route_action(lead_profile, controller_phrase, query, page_label)
    )

    if vacuum_strategy:
        first_moves = [
            (
                f"Build {page_label} to rank for the exact lane {query} and add "
                "product/offer/review/FAQ schema, so it is more retrievable and "
                "extractable before any conversion play."
            ),
            (
                f"State {angle_terms} in plain page text for {query}, then build "
                "verified reviews and proof so the official page is more authoritative."
            ),
            (
                source_action
            ),
            (
                f"Keep SKU name, {angle_terms}, images, availability, and canonical URL "
                f"consistent across {page_label} and {controller_phrase}; re-audit {query} "
                "and verify materiality before treating exposure as lost buyer traffic."
            ),
            (
                "After the page is more retrievable, extractable, and authoritative, add "
                "first-order offer, starter + replenishment bundle, subscription incentive, "
                "and why-buy-direct proof."
            ),
        ]
    elif source_authority_strategy:
        first_moves = [
            (
                f"Build {page_label} to rank for the exact lane {query} and add "
                "product/offer/review/FAQ schema, so it is more retrievable and "
                "extractable before outreach."
            ),
            (
                f"State {angle_terms} in plain page text for {query}, then build "
                "verified reviews and proof so the official page is more authoritative."
            ),
            (
                source_action
            ),
            (
                f"Keep SKU name, {angle_terms}, images, availability, and canonical URL "
                f"consistent across {page_label} and {controller_phrase}; re-audit {query} "
                "and verify materiality before treating exposure as lost buyer traffic."
            ),
            (
                "After the page is more retrievable, extractable, and authoritative, add "
                "first-order offer, starter + replenishment bundle, subscription incentive, "
                "and why-buy-direct proof."
            ),
        ]
    else:
        first_moves = [
            (
                f"Make {page_label} the more citable + buyable canonical page for {query}, "
                "then add a first-order offer so buyers have a reason to choose the "
                "merchant path."
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
    if controllers:
        first_moves.append(
            f"Update the source trail around {controller_phrase} only after {page_label} "
            "has the direct buying reason."
        )

    if vacuum_strategy:
        position = (
            f"{title} has AI answer exposure for {query}, but the grounded source trail leans on "
            f"{controller_phrase}, not {destination}."
        )
        core_decision = (
            f"Make {page_label} the page AI most easily finds, quotes, and trusts for the "
            f"'{query}' search, then check whether it is driving real buyer traffic before "
            "treating a few obscure cited sites as a lost sale."
        )
        why_you_lose = (
            f"AI's answers show {controller_phrase} shaping the citation trail for {query}. "
            f"That suggests {destination} is not yet the most authoritative fact-bearing source "
            "for this lane."
        )
        traffic_how_default = (
            f"Make {page_label} more retrievable and extractable for {query}, state "
            f"{angle_terms} in text, build reviews/proof, work {controller_phrase} by "
            "controller type, keep facts consistent, re-audit and verify materiality, "
            "then add offer, bundle, subscription, and why-buy-direct proof last."
        )
        self_serve = [
            f"Build {page_label} around the exact lane, schema, and plain-text attributes.",
            f"Work {controller_phrase} by controller type and keep facts consistent.",
            "Re-audit the lane and verify materiality before adding direct-buy mechanics.",
        ]
    elif source_authority_strategy:
        position = (
            f"{title} has real demand in AI answers, but the buying path is shaped by "
            f"{controller_phrase}, not {destination}."
        )
        core_decision = (
            f"Make {page_label} the page AI most easily finds, quotes, and trusts for the "
            f"'{query}' search, then work with the sites AI already cites — keeping the "
            "same facts everywhere."
        )
        why_you_lose = (
            f"AI's answers show {controller_phrase} shaping {query}. That suggests "
            "third-party source authority is carrying the facts and trust before the "
            "official page does."
        )
        traffic_how_default = (
            f"Make {page_label} more retrievable and extractable for {query}, state "
            f"{angle_terms} in text, build reviews/proof, work {controller_phrase} by "
            "controller type, keep facts consistent, re-audit and verify materiality, "
            "then add direct-buy mechanics last."
        )
        self_serve = [
            f"Build {page_label} around the exact lane, schema, and plain-text attributes.",
            f"Work {controller_phrase} by controller type and keep facts consistent.",
            "Re-audit the lane and verify materiality before adding direct-buy mechanics.",
        ]
    else:
        position = (
            f"{title} has real demand in AI answers, but the buying path is controlled "
            f"by {controller_phrase}, not {destination}."
        )
        core_decision = (
            f"Make {page_label} the better place to buy for the '{query}' search — "
            "showing up in AI answers is not the win until buyers have a reason to "
            "choose your own site."
        )
        why_you_lose = (
            f"AI's answers show {controller_phrase} shaping {query}. That suggests "
            "source and distribution authority are capturing the buyer path before "
            f"{destination} does."
        )
        traffic_how_default = (
            f"Make {page_label} the more citable + buyable canonical page first, "
            "then add the offer, bundle, subscription, and why-buy-direct proof."
        )
        self_serve = [
            "Add the offer, bundle, subscription, and why-buy-direct proof.",
            "Keep price, stock, returns, reviews, and product facts fresh.",
        ]

    traffic_strategy = []
    defer_queries = {
        _clean_str(item.get("query")).lower(): item
        for item in _as_list(sideways_wedge.get("do_not_chase_yet"))
        if isinstance(item, Mapping) and _clean_str(item.get("query"))
    }
    for item in opportunities[:3]:
        item_query = _clean_str(item.get("query"))
        how = _deterministic_traffic_how(
            item,
            page_label=page_label,
            default_how=traffic_how_default,
        )
        deferred = defer_queries.get(item_query.lower())
        if isinstance(deferred, Mapping):
            beachhead = _as_mapping(sideways_wedge.get("recommended_beachhead_lane"))
            beachhead_query = _clean_str(beachhead.get("query"))
            if _is_plausible_query(beachhead_query):
                how = (
                    f"Do not start here yet — win the '{beachhead_query}' search first; "
                    "it is more specific to this product, so your page can more easily "
                    "become the best place AI can cite and buy from."
                )
            else:
                how = (
                    "Do not start here yet — win a more product-specific search first, "
                    "so your page can more easily become the best place AI can cite and "
                    "buy from."
                )
        traffic_strategy.append({
            "where": item_query,
            "who_controls": _controller_phrase(
                _unique_host_roles(
                    controller
                    for controller in _as_list(item.get("controlled_by"))
                    if isinstance(controller, Mapping)
                )
            ),
            "how": how,
        })

    if wedge_decision:
        core_decision = f"{wedge_decision} {core_decision}"

    return {
        "position": position,
        "core_decision": core_decision,
        "why_you_lose": why_you_lose,
        "your_angle": (
            f"Lead with what makes this product specific — {angle_terms} — so your own "
            "page is the one AI cites and buyers choose, not just another listing."
        ),
        "traffic_strategy": traffic_strategy,
        "substitution_play": _deterministic_substitution_play(evidence, page_label),
        "first_moves": first_moves[:5],
        "diy_vs_pivota": {
            "self_serve": self_serve,
            "pivota": (
                "Pivota makes the canonical page more citable, buyable, and agent-checkout "
                "ready, then monitors whether these same lanes move toward the "
                "merchant-controlled path."
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
        return "fragmented sources with no single site owning the answer"
    if len(hosts) == 1:
        return hosts[0]
    if len(hosts) == 2:
        return f"{hosts[0]} and {hosts[1]}"
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
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
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
    failures.extend(_competitor_lack_claim_failures(text, allowed))
    failures.extend(_competitor_attribute_claim_failures(text, allowed))

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

    # Neutralize intra-word apostrophes (contractions/possessives: "don't",
    # "publisher's", "it's") before scanning for single-quoted lanes. Otherwise
    # the apostrophe in a contraction pairs with a real lane's quote and
    # fabricates a bogus "unknown-quoted-lane" failure — which silently rejects
    # otherwise-grounded LLM briefs and forces the deterministic fallback. A real
    # quoted lane's delimiters have a non-word char on the outer side, so they are
    # untouched by this substitution.
    quote_scan_text = _INTRAWORD_APOSTROPHE_RE.sub("", text)
    for quote in _QUOTE_RE.findall(quote_scan_text):
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
    category_words: Set[str] = set()
    competitor_terms: Set[str] = set()
    competitor_attribute_words: Set[str] = set()
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

    def add_competitor_attribute_word(word: str) -> None:
        normalized = word.lower()
        if not normalized:
            return
        competitor_attribute_words.add(normalized)
        attribute_words.add(normalized)
        if normalized == "stick":
            competitor_attribute_words.add("sticks")
            attribute_words.add("sticks")
        elif normalized.endswith("y"):
            competitor_attribute_words.add(f"{normalized[:-1]}ies")
            attribute_words.add(f"{normalized[:-1]}ies")
        elif not normalized.endswith("s"):
            competitor_attribute_words.add(f"{normalized}s")
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
                if field == "category":
                    category_words.add(word)
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
        competitor_terms.add(_norm_phrase(winner))
    for ranked in _as_list(category_battle.get("ranked_by")):
        if isinstance(ranked, Mapping):
            add_domain(ranked.get("host"))

    substitution = _as_mapping(evidence.get("substitution"))
    add_term(substitution.get("handed_to"))
    if substitution.get("handed_to"):
        competitor_terms.add(_norm_phrase(substitution.get("handed_to")))
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

    sideways_wedge = _as_mapping(evidence.get("sideways_wedge"))
    for lane in (
        _as_mapping(sideways_wedge.get("recommended_beachhead_lane")),
        _as_mapping(sideways_wedge.get("canonical_page_play")),
    ):
        add_term(lane.get("query"))
        add_term(lane.get("lane"))
        for controller in _as_list(lane.get("controllers")):
            add_domain(controller)
    for key in ("head_prompt_pressure", "sideways_wedge_lanes", "do_not_chase_yet"):
        for lane in _as_list(sideways_wedge.get(key)):
            if not isinstance(lane, Mapping):
                continue
            add_term(lane.get("query"))
            add_term(lane.get("source_route"))
            for reason in _as_str_list(lane.get("merchant_fit_reasons")):
                add_term(reason)
                for word in re.findall(r"[a-z0-9]+", reason.lower()):
                    add_attribute_word(word)
            for controller in _as_list(lane.get("controllers")):
                add_domain(controller)

    grounding_notes = _as_mapping(evidence.get("grounding_notes"))
    competitor_attributes = _as_mapping(grounding_notes.get("competitor_attributes"))
    if _clean_str(competitor_attributes.get("status")).lower() == "assessed":
        competitor = _clean_str(competitor_attributes.get("competitor"))
        if competitor:
            add_term(competitor)
            competitor_terms.add(_norm_phrase(competitor))
        for attr in _as_str_list(competitor_attributes.get("attributes_present")):
            add_term(attr)
            for word in re.findall(r"[a-z0-9]+", attr.lower()):
                add_competitor_attribute_word(word)

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
        "category_words": category_words,
        "competitor_terms": {term for term in competitor_terms if term},
        "competitor_attribute_words": competitor_attribute_words,
        "grounded_words": grounded_words,
        "safety_words": safety_words,
    }


def _validation_sentences(text: str) -> List[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or "")
        if sentence and sentence.strip()
    ]


def _competitor_context_sentence(sentence: str, allowed: Mapping[str, Any]) -> bool:
    normalized = _norm_phrase(sentence)
    if not normalized:
        return False
    for term in _COMPETITOR_GENERIC_TERMS:
        if _phrase_contains(normalized, term):
            return True
    for term in allowed.get("competitor_terms") or set():
        if _phrase_contains(normalized, term):
            return True
    return False


def _attribute_context_sentence(sentence: str, allowed: Mapping[str, Any]) -> bool:
    words = set(re.findall(r"[a-z0-9]+", sentence.lower()))
    return bool(
        words
        & (
            set(allowed.get("attribute_words") or set())
            | set(allowed.get("competitor_attribute_words") or set())
        )
    )


def _competitor_lack_claim_failures(
    text: str,
    allowed: Mapping[str, Any],
) -> List[str]:
    failures: List[str] = []
    for sentence in _validation_sentences(text):
        competitor_context = _competitor_context_sentence(sentence, allowed)
        exclusive_context = (
            bool(_COMPETITOR_EXCLUSIVE_RE.search(sentence))
            and _attribute_context_sentence(sentence, allowed)
        )
        if not competitor_context and not exclusive_context:
            continue
        if any(pattern.search(sentence) for pattern in _COMPETITOR_LACK_PATTERNS):
            failures.append("competitor-lack-claim")
            continue
        # Market-exclusivity ("the only brand with <attr>") is a disguised
        # deficiency claim about the whole field, so it fails even with no
        # competitor named. The broader only-with form still needs competitor
        # context so merchant-self "only your page offers X" stays allowed.
        market_exclusive = (
            bool(_COMPETITOR_MARKET_EXCLUSIVE_RE.search(sentence))
            and _attribute_context_sentence(sentence, allowed)
        )
        if market_exclusive or (exclusive_context and competitor_context):
            failures.append("competitor-exclusive-claim")
    return failures


def _competitor_attribute_claim_failures(
    text: str,
    allowed: Mapping[str, Any],
) -> List[str]:
    competitor_attribute_words = set(allowed.get("competitor_attribute_words") or set())
    failures: List[str] = []
    for sentence in _validation_sentences(text):
        if not _competitor_context_sentence(sentence, allowed):
            continue
        for match in _COMPETITOR_ATTRIBUTE_CLAIM_RE.finditer(sentence):
            verb = _norm_phrase(match.group("verb"))
            phrase = _norm_phrase(match.group("attrs"))
            if not phrase:
                continue
            tokens = _competitor_claim_attribute_tokens(phrase)
            if not competitor_attribute_words:
                if _unassessed_competitor_positioning_allowed(
                    verb=verb,
                    phrase=phrase,
                    tokens=tokens,
                    allowed=allowed,
                ):
                    continue
                if tokens:
                    failures.append(f"unassessed-competitor-attribute:{tokens[0]}")
                continue
            unknown = [
                token
                for token in tokens
                if not _competitor_claim_token_allowed(
                    token,
                    competitor_attribute_words,
                )
            ]
            if unknown:
                failures.append(f"ungrounded-competitor-attribute:{unknown[0]}")
    return failures


def _unassessed_competitor_positioning_allowed(
    *,
    verb: str,
    phrase: str,
    tokens: List[str],
    allowed: Mapping[str, Any],
) -> bool:
    if not verb.startswith("positioned"):
        return False
    words = set(re.findall(r"[a-z0-9]+", phrase.lower()))
    if not (words & {"broad", "general", "mainstream"}):
        return False
    category_words = set(allowed.get("category_words") or set())
    return all(
        _word_grounded(token, category_words)
        or _competitor_claim_token_allowed(token, set())
        for token in tokens
    )


def _competitor_claim_attribute_tokens(phrase: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9]+", phrase.lower())
        if token not in _CONNECTOR_WORDS
        and token not in _QUOTE_STOPWORDS
        and token not in _COMMON_PROSE_WORDS
        and token not in _COMMON_WORDS
        and token not in _COMPETITOR_GENERIC_TERMS
        and token not in _COMPETITOR_CLAIM_COMMON_WORDS
    ]


def _competitor_claim_token_allowed(
    token: str,
    competitor_attribute_words: Set[str],
) -> bool:
    if token in _COMPETITOR_CLAIM_COMMON_WORDS or _is_common_entity_word(token):
        return True
    return _word_grounded(token, competitor_attribute_words)


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
    # Allow the brand form of an evidenced domain: "Amazon"/"Forbes"/"Sephora"
    # when amazon.com/forbes.com/sephora.com is grounded. The domain is already
    # in the evidence, so naming its brand is not an ungrounded claim — and
    # rejecting it silently fails otherwise-grounded LLM briefs.
    entity_key = normalized.replace(" ", "")
    if entity_key and any(
        str(dom).split(".", 1)[0] == entity_key for dom in allowed["domains"]
    ):
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
