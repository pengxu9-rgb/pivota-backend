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
from services.text_normalization import sanitize_display_name
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
from services.vertical_profiles import (
    BEAUTY_PROFILE,
    BriefRules,
    resolve_profile_for_vertical,
    resolve_vertical,
)

logger = logging.getLogger(__name__)

# Reliability knobs for the LLM brief. W4: there is NO deterministic fallback —
# a brief that can't ground returns None (honest failure + refund), never a
# fabricated template — so we retry transient provider failures with backoff to
# make honest-failure rare (a single blip must not lose the merchant a real brief).
# The content-attempt count / max tokens live in _STRATEGIC_BRIEF_MAX_ATTEMPTS /
# _STRATEGIC_BRIEF_MAX_TOKENS below.
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
  influencers, or platforms. Name AT MOST THREE source sites/domains in any single field — if more control a
  lane, name the two or three biggest and say "and others"; never list four or more.
- NUMBERS: never write a percentage, price, "Nx" multiplier, or a review/rating/follower count — not even one
  quoted in EVIDENCE. Describe magnitude in words ("most of its formula", "a large review base"), never digits
  with %, $, x, or counts.
- INGREDIENTS: name ingredients in plain consumer terms ("green tea", "shea butter", "argan oil"). NEVER write a
  scientific / latin / INCI ingredient name (e.g. "Camellia Sinensis Leaf Water") or any multi-word proper-noun
  ingredient — the validator treats it as an unverified entity.
- CLAIMS: never use clinical or medical-efficacy language — no "clinical", "clinically", "proven", "treats",
  "cures", "repairs" as a proven outcome. Describe a benefit as positioning ("positioned around bond repair"),
  not as a proven result.
- SOURCES PER FIELD: name AT MOST THREE source sites in any single sentence/field; only sites in
  grounding_notes.evidenced_channels. If more matter, name the top two or three and stop.
- REALISTIC OUTREACH: do NOT tell the merchant to "pitch" a major mainstream publisher (Vogue, Forbes,
  Marie Claire, Allure, Cosmopolitan, Good Housekeeping, etc.) — they rarely cover an emerging or medium-tail
  brand on a cold pitch, so that is not a real action. For winning a publisher-controlled lane, prescribe the
  REACHABLE path first: earn authentic reviews + get listed/reviewed on the review aggregators AI already
  cites, build genuine Reddit / niche-community presence, and collect first-party reviews — editorial pickup
  follows those signals. Only suggest contacting a publisher when it is a niche/community site in EVIDENCE.
- QUOTES: put a phrase in quotes ONLY when it is an EXACT search lane from EVIDENCE. NEVER quote your own
  product's features, ingredients, or angle ("bond technology that repairs disulfide bonds") as if it were a
  searched lane — write those as plain prose.
- COMPETITOR CONTRAST (reinforce): refer to a rival only as "recommended/ranked by <evidenced source>". NEVER
  ascribe a specific product feature, ingredient, or capability to a named competitor, and NEVER say a
  competitor lacks, misses, is the only one without, or fails to offer anything.
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
- position: one honest sentence on where THIS product really stands, grounded in what the evidence shows for
  this SKU. Do NOT reuse a stock label like "niche challenger, strong when named, invisible in the category".
- core_decision: the ONE big strategic call for THIS product, stated plainly and decisively — the action and the
  real reason from evidence (the specific product fact, the exact competitor AI named, or the exact source that
  controls the answer that makes this the call). It may imply what to stop doing, but do NOT format it as a
  "stop X, instead do Y" template — open with the product-specific reason, not the reusable verb frame. GOOD
  (opens with the product-specific reason, no template): "Your shea-butter-and-green-tea butter is the only
  thing in this category with real reviews behind it, so put those reviews on your own page and make it the
  answer for the 'reviews hair butter treatment' search before spending anything on the crowded 'best hair mask'
  question." BAD (template opener): "Stop chasing broad category queries… Instead, own the reviews lane first."
- why_you_lose: WHY the category winners win. READ category_answers (the AI's VERBATIM answers on the category
  lanes) and synthesize the specific winning PRODUCTS named (from recommends) and the SOURCES that rank them
  (from cited_sources). The FACT is that those evidenced sources cite/rank the winners — attribute their
  advantage to that SOURCE relationship ("Forbes lists them, which points to editorial authority the AI trusts"),
  phrased as YOUR inference. Only NAME products from recommends and sources from cited_sources/grounding_notes
  (never invent others). Do NOT state competitor product attributes, qualities, reviews, distribution, or
  authority AS FACT, and do NOT claim competitor feature gaps. Describe the merchant's own absence plainly
  ("your page is not cited there yet"), never as the merchant "lacking" or being "without" something.
- your_angle: the defensible positioning = the merchant's differentiating attributes that the named product
  actually has. CRITICAL: if the winning ANGLE from category_answers is one the merchant's OWN attributes already
  match (e.g. the product already claims "bond repair"), say so plainly — the wedge is "you have the winning
  claim but aren't in the sources that rank it." Position it as a specific product rather than a generic
  {category} — without a fixed "reframe from 'a X' to 'the Y'" formula, and without saying winners lack those
  attributes as fact. Use exact EVIDENCE query wording where their differentiation IS the answer.
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

# --- Vertical-aware brief rules (Phase 1b) -----------------------------------
# The prompt above is the INCUMBENT (beauty) prompt; all of it is vertical-neutral
# EXCEPT two spans — the INGREDIENTS+CLAIMS block and the mainstream-publisher
# list a merchant shouldn't cold-pitch. We DERIVE a template from the incumbent by
# replacing exactly those two single-occurrence spans with sentinels (no
# retyping), so beauty renders byte-identically and only the two slots swap per
# vertical. See services.vertical_profiles.BriefRules.
_BRIEF_CLAIM_SENTINEL = "CATEGORY_CLAIM_RULES"
_BRIEF_PUBLISHER_SENTINEL = "COLD_PITCH_PUBLISHERS"


def _derive_brief_template(incumbent: str) -> Tuple[str, BriefRules]:
    claim = incumbent[incumbent.index("- INGREDIENTS:"): incumbent.index("\n- SOURCES PER FIELD:")]
    p1 = incumbent.index("major mainstream publisher (") + len("major mainstream publisher (")
    publishers = incumbent[p1: incumbent.index(")", p1)]
    template = incumbent.replace(claim, _BRIEF_CLAIM_SENTINEL).replace(
        "(" + publishers + ")", "(" + _BRIEF_PUBLISHER_SENTINEL + ")"
    )
    return template, BriefRules(claim_rules=claim, cold_pitch_publishers=publishers)


_SYSTEM_PROMPT_TEMPLATE, _BEAUTY_BRIEF_RULES = _derive_brief_template(_STRATEGIC_BRIEF_SYSTEM_PROMPT)


def _render_system_prompt(profile: Any) -> str:
    """The brief system prompt for a vertical. A profile with no ``brief_rules``
    (beauty / anything not affirmatively switched) returns the incumbent prompt
    VERBATIM — byte-identical. Electronics swaps only the two category slots."""
    rules = getattr(profile, "brief_rules", None)
    if rules is None:
        return _STRATEGIC_BRIEF_SYSTEM_PROMPT
    return _SYSTEM_PROMPT_TEMPLATE.replace(
        _BRIEF_CLAIM_SENTINEL, rules.claim_rules
    ).replace(_BRIEF_PUBLISHER_SENTINEL, rules.cold_pitch_publishers)


def _brief_profile_for_evidence(evidence: Mapping[str, Any]) -> Any:
    """Choose the brief profile. An affirmatively-electronics or -beauty vertical
    applies its runtime sub-split from the evidence text (electronics -> drone vs
    audio; beauty -> topical vs hair-styling device); fashion / other / unknown
    keep the incumbent (beauty) prompt.

    Beauty stays on the incumbent INCI/claims prompt UNLESS the SKU is
    affirmatively a DEVICE — a device has no INCI, so it must get its device
    class's spec/claim rules (hair-styling / skincare-energy / hair-removal /
    generic), never the ingredient guard. The topical/device split
    (``_beauty_device_class``) is conservative: any topical FORM noun keeps the SKU
    on the incumbent prompt, so an ambiguous beauty SKU never loses the guard."""
    vertical = str((evidence or {}).get("vertical") or "").strip().lower()
    if vertical in ("electronics", "beauty"):
        ev = evidence if isinstance(evidence, Mapping) else None
        return resolve_profile_for_vertical(
            vertical, ev, title=(evidence or {}).get("title")
        )
    return BEAUTY_PROFILE


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

# Free-text EVIDENCE (mined AI answers, competitor "known for") can quote stats
# like "90% of ingredients" or "$24" that the brief is forbidden to repeat
# (forbidden:% / $ patterns above). Neutralise those stats to words BEFORE the
# evidence reaches the prompt so a faithful draft doesn't trip the validator —
# the qualitative signal is what the brief needs, not the exact number.
_NUMERIC_CLAIM_SUBS: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"\b\d+(?:\.\d+)?\s?%"), "a large share"),
    (re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?"), "a set price"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*x\b", re.IGNORECASE), "several-fold"),
    (re.compile(
        r"\b\d[\d,]*\+?\s*(reviews?|ratings?|stars?|customers?|users?|sales?|followers?|subscribers?)\b",
        re.IGNORECASE,
    ), r"many \1"),
    (re.compile(r"/100"), ""),
)


def _neutralize_numeric_claims(text: Any) -> str:
    s = str(text or "")
    for pattern, repl in _NUMERIC_CLAIM_SUBS:
        s = pattern.sub(repl, s)
    return s


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
# Standards/spec references the brief cites when recommending structured data.
# These are not competitors or buyer-path sources, so they must not trip the
# unknown-domain guard (e.g. "add Product schema per schema.org").
_TECHNICAL_ALLOWED_DOMAINS = {"schema.org", "json-ld.org", "ogp.me"}
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
# Generic merchandising + AEO/structured-data vocabulary the brief legitimately
# RECOMMENDS the merchant create (a Starter Kit, a Subscribe & Save offer, an
# About Us page, Organization/Product/Review/FAQ structured data). These are not
# fabricated brands or competitors, so they must not trip the unknown-entity
# guard. Single tokens — multiword constructs ("Starter Kit", "About Us") pass
# because every token is here. Competitor/stat fabrication is still caught by the
# separate competitor + forbidden-pattern checks.
_GENERIC_COMMERCE_ENTITIES = frozenset({
    "about", "us",
    "schema", "markup", "jsonld", "metadata", "structured", "data",
    "breadcrumb", "breadcrumbs", "breadcrumblist",
    "organization", "product", "offer", "offers", "review", "reviews",
    "rating", "ratings", "aggregate", "aggregaterating",
    "subscribe", "subscription", "save", "saver", "autoship", "replenish",
    "replenishment",
    "starter", "kit", "kits", "refill", "refills", "pack", "packs",
    "bundle", "bundles", "set", "sets",
    "homepage", "landing", "hero", "listing", "listings", "collection",
    "guarantee", "guarantees", "warranty", "samples", "sample", "loyalty",
    "rewards", "returns", "return", "shipping", "stock", "availability",
    "testimonial", "testimonials", "quiz", "guide", "guides", "tutorial",
})
# Plain prose nouns/adverbs the LLM routinely Capitalizes (mid-sentence or after
# a colon/bullet) — "Legitimacy", "After", "Trust signals". They are common
# English, never invented proper nouns, so they must not trip the unknown-entity
# guard. (Distinctive product-attribute tokens still surface separately.)
_COMMON_PROSE_NOUNS = frozenset({
    "after", "before", "during",
    "legitimacy", "authenticity", "trust", "trustworthiness", "credibility",
    "purchase", "purchases", "checkout", "secure", "welcome", "satisfaction",
    "signal", "signals", "concern", "concerns", "result", "results", "proof",
    "awareness", "consideration", "conversion", "retention", "discovery",
    "visibility", "exposure", "intent", "traffic", "story", "badge", "badges",
    "customer", "customers", "shopper", "shoppers", "buyer", "buyers", "social",
})
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
    # Reputation/standing descriptors. Calling the cited source "credible" or
    # "established" is the brief's grounded inference about WHY it out-cites the
    # merchant (it IS the cited/ranking source) — not a fabricated product
    # feature. Product-feature tokens in the same claim still fail.
    "credible",
    "dominant",
    "established",
    "known",
    "large",
    "leading",
    "legitimate",
    "major",
    "popular",
    "prominent",
    "recognized",
    "reliable",
    "reputable",
    "respected",
    "strong",
    "trusted",
    "well",
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
    "treatment",
    "clinical",
    "clinically",
    "fda",
}
# Word-families where one grounded member licenses the rest (same cosmetic claim,
# not a new medical one). Used only to extend the OWN-evidence safety allow-list.
_SAFETY_TERM_FAMILIES = (
    frozenset({"treat", "treats", "treatment", "treatments"}),
)
# Max LLM drafts per SKU before honest failure (return None). The grounding
# validator is strict, so a clean draft is found reliably only with several tries;
# a passing draft short-circuits, so the common case stays cheap.
_STRATEGIC_BRIEF_MAX_ATTEMPTS = 6
# Raised from 1200: the brief evidence now carries mined category answers +
# competitor "known for" depth, so 1200-token drafts were truncating
# (finish_reason=length, shape_ok=False) on ~half the attempts → honest failure.
# 2000 gives the full brief room to land.
_STRATEGIC_BRIEF_MAX_TOKENS = 2000


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
    # Sanitize the display name before it renders into brief copy (dirty
    # identity strings like "...30 sticks's page" must not surface verbatim).
    title = (
        sanitize_display_name(sku_title)
        or sanitize_display_name(identity_map.get("name"))
        or "this SKU"
    )
    anchors = _as_mapping(identity_map.get("anchors"))
    brand = _clean_str(anchors.get("brand"))
    # Resolve the SKU vertical ONCE for the brief (Principle 1). Signal = category
    # + title + the attribute values (ingredients / specs) the brief reasons over.
    # See _brief_profile_for_evidence for how the vertical changes the prompt.
    _brief_signal = {
        "product_type": _clean_str(anchors.get("category")),
        "category": _clean_str(anchors.get("category")),
    }
    _brief_signal_title = " ".join(
        [title, brand, *[item for values in attributes.values() for item in values]]
    )
    _brief_vertical = resolve_vertical(_brief_signal, title=_brief_signal_title)
    # Apply the runtime sub-split (electronics audio/drone, beauty topical/device)
    # so a device brief reads the device profile's health_sensitive flag, not the
    # topical/audio default.
    _brief_profile = resolve_profile_for_vertical(
        _brief_vertical, _brief_signal, title=_brief_signal_title
    )
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
        # Resolved SKU vertical (Principle 1) — read by build_sku_brief_prompt to
        # pick the category rules block, and by the notes.health_sensitive gate.
        "vertical": _brief_vertical,
        "product": {
            "title": title,
            "brand": brand or None,
            "merchant_path": merchant_path,
            "attributes": attributes,
            # The merchant's own canonical host. A useful brief MUST be able to
            # name it ("make anukoofficial.com the buyable canonical page"); add
            # it so grounding allows it instead of rejecting it as an unknown
            # domain (which used to force honest failure — the brief couldn't even
            # name the merchant's own buyable page).
            "merchant_host": _normalize_host(merchant_host) or None,
        },
        "position": _position_from_ladder(opportunity_map),
        "category_battle": category_battle,
        # The verbatim AI answers on the category lanes — winning products,
        # sources, and the category angle the brief should mine (see
        # _category_answers + the why_you_lose / your_angle prompt fields).
        "category_answers": _category_answers(category_rows),
        # The merchant's OWN product facts — licensed so the brief can name its
        # own ingredients/angle without tripping unknown-entity/quoted-lane.
        "own_product_facts": _own_product_facts(product_evidence),
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
            # Metadata flag (one call site). The profile can hard-set it — electronics
            # is health_sensitive=False (a "battery"/"waterproof" token must not flag
            # earphones health-sensitive); a profile leaving it None (beauty / other)
            # falls back to the token detector, byte-identical to before.
            "health_sensitive": (
                _brief_profile.health_sensitive
                if _brief_profile.health_sensitive is not None
                else _health_sensitive(title=title, brand=brand, attributes=attributes)
            ),
        },
    }


def _licensed_entity_manifest(evidence: Mapping[str, Any]) -> Dict[str, List[str]]:
    """W4 closed-world manifest: the proper-noun entities the brief is LICENSED
    to name, extracted from the same evidence the grounding validator checks.

    Making the allowlist explicit up front (competitors / sources / lanes /
    merchant identity) makes fabrication structurally hard — the model names
    from a set instead of inventing, and the validator becomes a backstop, not
    the primary defense. Deduped, order-preserving, capped so the prompt stays
    lean; empty lists are fine (an absent category just isn't licensed)."""

    def _uniq(values: List[str], *, cap: int) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for value in values:
            text = _clean_str(value)
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
            if len(out) >= cap:
                break
        return out

    product = _as_mapping(evidence.get("product"))
    battle = _as_mapping(evidence.get("category_battle"))
    substitution = _as_mapping(evidence.get("substitution"))
    grounding = _as_mapping(evidence.get("grounding_notes"))
    comp_attrs = _as_mapping(grounding.get("competitor_attributes"))

    competitors: List[str] = list(_as_str_list(battle.get("winners")))
    for answer in _as_list(evidence.get("category_answers")):
        if isinstance(answer, Mapping):
            competitors += _as_str_list(answer.get("recommends"))
    if substitution.get("handed_to"):
        competitors.append(_clean_str(substitution.get("handed_to")))
    if _clean_str(comp_attrs.get("status")).lower() == "assessed" and comp_attrs.get("competitor"):
        competitors.append(_clean_str(comp_attrs.get("competitor")))

    sources: List[str] = []
    for ranked in _as_list(battle.get("ranked_by")):
        if isinstance(ranked, Mapping) and ranked.get("host"):
            sources.append(_clean_str(ranked.get("host")))
    for answer in _as_list(evidence.get("category_answers")):
        if isinstance(answer, Mapping):
            sources += _as_str_list(answer.get("cited_sources"))
    for chan in _as_list(grounding.get("evidenced_channels")):
        if isinstance(chan, Mapping) and chan.get("host"):
            sources.append(_clean_str(chan.get("host")))
    for lane in _as_list(evidence.get("channel_map")):
        if isinstance(lane, Mapping):
            for controller in _as_list(lane.get("controlled_by")):
                if isinstance(controller, Mapping) and controller.get("host"):
                    sources.append(_clean_str(controller.get("host")))

    lanes: List[str] = list(_as_str_list(battle.get("prompts")))
    for lane in _as_list(evidence.get("open_lanes")):
        if isinstance(lane, Mapping) and lane.get("query"):
            lanes.append(_clean_str(lane.get("query")))

    merchant: List[str] = []
    for key in ("brand", "title", "merchant_host"):
        if product.get(key):
            merchant.append(_clean_str(product.get(key)))

    return {
        "competitors": _uniq(competitors, cap=12),
        "sources": _uniq(sources, cap=12),
        "lanes": _uniq(lanes, cap=14),
        "merchant": _uniq(merchant, cap=4),
    }


def _render_entity_manifest(manifest: Mapping[str, List[str]]) -> str:
    def _line(label: str, values: List[str]) -> str:
        return f"- {label}: " + (", ".join(values) if values else "(none in evidence)")

    return (
        "LICENSED ENTITIES — you may name ONLY the proper nouns below (they are "
        "the entities present in EVIDENCE). Naming any competitor brand, source "
        "site, or search lane NOT in these lists is a fabrication and will be "
        "rejected. Generic references ('the relevant communities', 'your own "
        "page') are always allowed.\n"
        + _line("Competitor brands you may name", manifest.get("competitors", []))
        + "\n"
        + _line("Source sites/domains you may name", manifest.get("sources", []))
        + "\n"
        + _line("Search lanes you may reference (use this wording)", manifest.get("lanes", []))
        + "\n"
        + _line("The merchant's own brand/site", manifest.get("merchant", []))
    )


def build_sku_brief_prompt(evidence: Mapping[str, Any]) -> Tuple[str, str]:
    # W4: lead with the closed-world manifest so the model names from an
    # explicit allowlist, then the full evidence for the reasoning substrate.
    manifest = _render_entity_manifest(_licensed_entity_manifest(evidence))
    user = (
        manifest
        + "\n\nEVIDENCE:\n"
        + json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return _render_system_prompt(_brief_profile_for_evidence(evidence)), user


def _resolve_brief_provider(provider: Optional[str]) -> Optional[str]:
    """Resolve the brief generator, degrading by KEY availability. Explicit
    `provider` (if given) wins; else settings.strategic_brief_provider (default
    gemini); DeepSeek is the final fallback so a missing Gemini key never
    forces honest failure (None). Returns None only when NO candidate has
    a configured key."""
    chain = [provider, settings.strategic_brief_provider, "deepseek"]
    seen: Set[str] = set()
    for name in chain:
        if not name:
            continue
        try:
            canonical = normalize_provider(name)
        except LLMSynthesisError:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        if configured_key_for_provider(canonical):
            return canonical
    return None


async def generate_sku_strategic_brief(
    evidence: Mapping[str, Any],
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    debug: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    # `debug` (when provided) is populated in place with exactly why a brief did
    # or didn't ship, and persisted onto next_best_action.brief_debug so the
    # fallback cause is diagnosable from the stored report without worker-shell
    # access (the LLM lane must work continuously; honest failure is
    # not acceptable as a steady state).
    dbg: Dict[str, Any] = debug if debug is not None else {}
    dbg["enabled"] = bool(getattr(settings, "strategic_brief_enabled", False))
    if not dbg["enabled"]:
        dbg["outcome"] = "none_disabled"
        return None
    selected_provider = _resolve_brief_provider(provider)
    if not selected_provider:
        dbg["outcome"] = "none_no_key"
        dbg["provider"] = str(provider or settings.strategic_brief_provider or "")
        dbg["key_configured"] = False
        return None
    dbg["provider"] = selected_provider
    dbg["key_configured"] = True
    selected_model = (
        str(model or settings.strategic_brief_model or "").strip()
        or default_model_for_provider(selected_provider)
    )
    dbg["model"] = selected_model
    system, base_user = build_sku_brief_prompt(evidence)
    dbg["prompt_chars"] = len(system) + len(base_user)
    dbg["max_tokens"] = _STRATEGIC_BRIEF_MAX_TOKENS
    dbg["attempts"] = []
    user = base_user

    # The grounding validator is deliberately strict (a trust product), so a
    # single LLM draft can trip it stochastically. Retry enough that a clean
    # draft is reliably found — the LLM lane must work continuously, not fall
    # back to the honest failure (None). On shape/grounding rejection we
    # feed the model a targeted repair hint before the next attempt; transient
    # provider blips are retried with backoff inside _synthesize_with_transport_retry.
    best_grounded_brief: Optional[Dict[str, Any]] = None
    for _attempt in range(_STRATEGIC_BRIEF_MAX_ATTEMPTS):
        att: Dict[str, Any] = {}
        try:
            result = await _synthesize_with_transport_retry(
                system=system,
                user=user,
                provider=selected_provider,
                model=selected_model,
                max_tokens=_STRATEGIC_BRIEF_MAX_TOKENS,
            )
        except LLMSynthesisError as exc:
            att["error"] = f"{type(exc).__name__}: {exc}"[:300]
            dbg["attempts"].append(att)
            # W4: honest failure — no deterministic template. The brief is one
            # section of an otherwise-complete report (metered at the run level,
            # W6), so a failed brief means the section is absent, not that the
            # merchant got nothing. brief_status="unavailable" downstream; the
            # rest of the report ships. A thin template pretending to be
            # analysis is exactly what this workstream removes.
            dbg["outcome"] = "unavailable_llm_error"
            return None
        except Exception as exc:  # noqa: BLE001 - capture any synth failure for diagnosis
            att["error"] = f"unexpected {type(exc).__name__}: {exc}"[:300]
            dbg["attempts"].append(att)
            dbg["outcome"] = "unavailable_unexpected_error"
            return None
        text = result.get("text") or ""
        att["text_len"] = len(text)
        att["finish_reason"] = result.get("finish_reason")
        att["usage"] = result.get("usage")
        brief = _parse_brief_json(text)
        att["parsed"] = isinstance(brief, dict)
        shape_ok = isinstance(brief, dict) and _has_required_shape(brief)
        att["shape_ok"] = shape_ok
        if not shape_ok:
            if isinstance(brief, dict):
                att["missing_keys"] = sorted(_REQUIRED_BRIEF_KEYS - set(brief.keys()))
            dbg["attempts"].append(att)
            # Regenerate with a shape-repair hint instead of falling back.
            user = f"{base_user}\n\n{_SHAPE_REPAIR_HINT}"
            continue
        gf = _grounding_failures(brief, evidence)
        att["grounding_failures"] = gf
        if gf:
            # Grounding is a hard gate (trust) — regenerate with the exact rules.
            dbg["attempts"].append(att)
            user = f"{base_user}\n\n{_grounding_repair_hint(gf)}"
            continue
        # Grounding-clean. Prefer a draft that is ALSO free of the banned
        # "Stop trying to win …" opener / jargon the prompt can only *ask* for
        # (temperature is non-zero, so the model still slips ~occasionally). A
        # style slip is retried WITH a hint, but — unlike grounding — it must
        # NEVER cost the merchant the LLM brief: a style-imperfect LLM draft
        # (jargon then scrubbed deterministically) still beats honest failure, so
        # we keep the first clean draft as a floor.
        sf = _style_failures(brief)
        if sf:
            att["style_failures"] = sf
        dbg["attempts"].append(att)
        if not sf:
            dbg["outcome"] = "llm"
            return _scrub_merchant_jargon(brief)
        if best_grounded_brief is None:
            best_grounded_brief = brief
        user = f"{base_user}\n\n{_grounding_repair_hint(sf)}"
    if best_grounded_brief is not None:
        # Exhausted retries but we DID get a grounded LLM draft — ship it (jargon
        # scrubbed). A grounded-but-style-imperfect real brief is the main line.
        dbg["outcome"] = "llm"
        dbg["style_imperfect"] = True
        return _scrub_merchant_jargon(best_grounded_brief)
    # W4: every attempt failed grounding/shape — honest absence, no template.
    # The closed-world manifest makes this rare; when it happens the section is
    # withheld and brief_status reflects it, rather than shipping a generic
    # fabricated brief that reads as bespoke analysis it isn't.
    dbg["outcome"] = "unavailable_after_rejects"
    return None


def _is_retryable_synthesis_error(exc: LLMSynthesisError) -> bool:
    """A transient provider failure worth retrying vs. a fatal one. Missing key,
    unsupported provider, and 4xx (except throttling) are fatal — retrying just
    wastes time and delays honest failure. Transport failures (no
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
    max_tokens: int,
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
                max_tokens=max_tokens,
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

# Style violations the prompt bans but a non-zero-temperature model still slips
# into. Enforced deterministically via the same repair-retry loop as grounding.
_FORMULAIC_OPENER_RE = re.compile(r"\bstop\s+(?:chasing|trying\s+to\s+win)\b", re.IGNORECASE)
_MERCHANT_JARGON_RE = re.compile(r"\b(?:lane|lanes|beachhead|beachheads|materiality)\b", re.IGNORECASE)


def _style_failures(brief: Mapping[str, Any]) -> List[str]:
    """Flag banned merchant-facing style: the "Stop chasing/trying to win …"
    template opener, and internal jargon words that must never reach a merchant."""
    text = " ".join(_iter_leaf_text(brief))
    failures: List[str] = []
    if _FORMULAIC_OPENER_RE.search(text):
        failures.append("style-formulaic-opener")
    if _MERCHANT_JARGON_RE.search(text):
        failures.append("style-jargon")
    return failures


_JARGON_SUBSTITUTIONS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\blanes\b", re.IGNORECASE), "searches"),
    (re.compile(r"\blane\b", re.IGNORECASE), "search"),
    (re.compile(r"\bbeachheads\b", re.IGNORECASE), "first searches to win"),
    (re.compile(r"\bbeachhead\b", re.IGNORECASE), "first search to win"),
    (re.compile(r"\bmateriality\b", re.IGNORECASE), "real buyer traffic"),
)


def _scrub_merchant_jargon(brief: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Deterministically replace the internal jargon the prompt bans but the
    model occasionally still emits (lane→search, etc.), so it never reaches the
    merchant even when a retry could not coax a jargon-free draft. Structural
    only — every replacement is a plain-language synonym, no facts change."""
    if not isinstance(brief, dict):
        return brief

    def _scrub(value: Any) -> Any:
        if isinstance(value, str):
            for pattern, repl in _JARGON_SUBSTITUTIONS:
                value = pattern.sub(repl, value)
            return value
        if isinstance(value, list):
            return [_scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items()}
        return value

    return {k: _scrub(v) for k, v in brief.items()}


# Map the internal grounding-failure codes to a plain corrective instruction so a
# retry can fix the specific violation instead of dropping to honest failure.
_GROUNDING_REPAIR_RULES: Tuple[Tuple[str, str], ...] = (
    ("style-formulaic-opener",
     "Do NOT use a 'Stop chasing …' or 'Stop trying to win …' construction anywhere. "
     "Open with the ONE fact true only of this product and state the positive move "
     "directly, with no stop-this-then-do-that template."),
    ("style-jargon",
     "Do NOT use the words 'lane', 'beachhead', or 'materiality'. Say 'search' or "
     "'the \"<query>\" search' instead of 'lane', and plain phrasing everywhere else."),
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


def _category_answers(rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The AI's VERBATIM grounded answers on the category lanes — the richest,
    most under-used signal. The cited-evidence excerpt names the specific
    winning PRODUCTS, the SOURCES the engine pulled from, and (implicitly) the
    category's winning ANGLE/claim (e.g. hair oil is won on "bond repair /
    disulfide bonds"). Surfacing it lets the brief diagnose WHY winners win and
    whether the merchant's OWN attributes already match that angle, instead of
    giving generic "build a canonical PDP" advice."""
    answers: List[Dict[str, Any]] = []
    for row in rows:
        cited = _as_mapping(row.get("cited_evidence"))
        excerpt = _clean_str(cited.get("excerpt"))
        if not excerpt:
            continue
        recommends = _unique(
            _as_str_list(cited.get("competitors_named"))
            or _as_str_list(row.get("competitors"))
        )
        sources = _unique(
            host for host in (
                _normalize_host(h) for h in _as_str_list(cited.get("cited_hosts"))
            ) if host
        )
        answers.append({
            "query": _clean_str(row.get("query")),
            # Verbatim AI answer — read it for the winning angle/claim. Stats
            # neutralised so the brief doesn't echo a forbidden "%/$" figure.
            "ai_answer": _neutralize_numeric_claims(excerpt)[:400],
            # Specific products/brands the engine recommended for this lane.
            "recommends": recommends[:6],
            # The sources the engine cited (where the recommendations come from).
            "cited_sources": list(sources)[:5],
        })
    return answers[:6]


def _own_product_facts(product_evidence: Mapping[str, Any]) -> List[str]:
    """The merchant's OWN grounded product phrases (ingredients, claims, angle)
    from their PDP/content. Maximally groundable — the brief must be able to
    name the brand's own 'bond technology / disulfide bonds / shea butter +
    green tea' without the validator rejecting them as unknown entities or
    treating the brand's own angle as an unverified quoted lane. Licensing only
    the MERCHANT's own facts does NOT loosen any anti-competitor-fabrication
    guard."""
    pe = _as_mapping(product_evidence)
    facts: List[str] = []
    for key in ("explicit_text_phrases", "phrases"):
        facts.extend(_as_str_list(pe.get(key)))
    return _unique(f for f in facts if f)[:24]


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
        verbatim = _neutralize_numeric_claims(_clean_str(item.get("verbatim")))
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
    # W3: the shared tolerant parser is the single bare→fence→substring impl.
    from services.llm_io import parse_llm_object

    return parse_llm_object(raw_text, label="strategic_brief")


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


def _phrase_join(values: List[str], fallback: str) -> str:
    cleaned = [value for value in values if _clean_str(value)]
    if not cleaned:
        return fallback
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


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
        # A channel-plan sentence legitimately names the few controllers of a
        # lane; reject only a genuine laundry-list. (The prompt still steers to
        # ≤3; this tolerance stops a faithful 4–5-source plan from being killed,
        # which was the single most frequent brief rejection.)
        if len(leaf_domains) > 5:
            failures.append("overwide-controller-list")

    for domain in _DOMAIN_RE.findall(text):
        normalized = _normalize_host(domain)
        if (
            normalized
            and normalized not in allowed["domains"]
            and normalized not in _TECHNICAL_ALLOWED_DOMAINS
        ):
            failures.append(f"unknown-domain:{domain}")

    # Neutralize intra-word apostrophes (contractions/possessives: "don't",
    # "publisher's", "it's") before scanning for single-quoted lanes. Otherwise
    # the apostrophe in a contraction pairs with a real lane's quote and
    # fabricates a bogus "unknown-quoted-lane" failure — which silently rejects
    # otherwise-grounded LLM briefs and forces honest failure. A real
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
    # The merchant's own canonical host is grounded evidence — a brief naming it
    # ("make anukoofficial.com the buyable canonical page") must pass, not be
    # rejected as an unknown domain.
    add_domain(product.get("merchant_host"))
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

    # License the entities mined from the verbatim AI answers so the brief may
    # name the specific winning products + the sources that rank them.
    for answer in _as_list(evidence.get("category_answers")):
        if not isinstance(answer, Mapping):
            continue
        for recommended in _as_str_list(answer.get("recommends")):
            add_term(recommended)
            competitor_terms.add(_norm_phrase(recommended))
        for source in _as_str_list(answer.get("cited_sources")):
            add_domain(source)

    # The merchant's OWN product facts (ingredients/claims/angle) are maximally
    # groundable. License their terms + words so the brief can name the brand's
    # own "bond technology", "disulfide bonds", "shea butter + green tea"
    # without unknown-entity / unknown-quoted-lane rejections on the merchant's
    # own copy. (Own content only — no competitor licensing here.)
    for fact in _as_str_list(evidence.get("own_product_facts")):
        add_term(fact)
        for word in re.findall(r"[a-z0-9]+", fact.lower()):
            add_attribute_word(word)

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

    # A safety-sensitive term is only a fabrication risk when it isn't grounded.
    # When it appears in the merchant's OWN product title/brand/attributes or in
    # AI's actual queries/lanes, the brief is grounded in using it — e.g. a
    # product literally named "...Hair Treatment" or a lane "best treatment for
    # damaged hair". Without this, every such SKU had its whole brief rejected
    # (brief_status "unavailable" → the merchant saw nothing). We deliberately do
    # NOT license safety terms from competitor names/attributes or the substitute
    # (a competitor being "clinical" doesn't let the brief call THIS product
    # clinical), so those still fail the guard. Invented claims still fail too.
    own_safe_parts: List[str] = [
        _clean_str(product.get("title")),
        _clean_str(product.get("brand")),
    ]
    for values in attributes.values():
        own_safe_parts.extend(_as_str_list(values))
    own_safe_parts.extend(_as_str_list(category_battle.get("prompts")))
    for lane in _as_list(evidence.get("open_lanes")):
        if isinstance(lane, Mapping):
            own_safe_parts.append(_clean_str(lane.get("query")))
    for lane in _as_list(evidence.get("channel_map")):
        if isinstance(lane, Mapping):
            own_safe_parts.append(_clean_str(lane.get("lane")))
            own_safe_parts.append(_clean_str(lane.get("query")))
    for opp in _as_list(evidence.get("buyer_path_opportunities")):
        if isinstance(opp, Mapping):
            own_safe_parts.append(_clean_str(opp.get("query")))
    for key in ("recommended_beachhead_lane", "canonical_page_play"):
        own_safe_parts.append(_clean_str(_as_mapping(sideways_wedge.get(key)).get("query")))
    for key in ("head_prompt_pressure", "sideways_wedge_lanes", "do_not_chase_yet"):
        for lane in _as_list(sideways_wedge.get(key)):
            if isinstance(lane, Mapping):
                own_safe_parts.append(_clean_str(lane.get("query")))
    own_safe_words: Set[str] = set()
    for part in own_safe_parts:
        own_safe_words.update(re.findall(r"[a-z0-9]+", part.lower()))
    # A grounded member of a cosmetic word-family licenses the family: a product
    # named a "Treatment" can be described as something that "treats" — the verb
    # is the same cosmetic claim, not a new medical one.
    for family in _SAFETY_TERM_FAMILIES:
        if own_safe_words & family:
            own_safe_words |= family
    safety_words |= {term for term in _SAFETY_SENSITIVE_TERMS if term in own_safe_words}

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
    # After dropping the sentence-initial verb + ignorable edges, the REMAINING
    # phrase may itself be an allowed multi-word host/brand ("Pitch Who What
    # Wear" -> "Who What Wear" -> whowhatwear.com). Re-check the whole phrase
    # before flagging token-by-token, or "Who" gets reported as an unknown
    # entity even though the real cited source is grounded.
    if tokens and _entity_allowed(" ".join(tokens), allowed):
        return None
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
    # The cited source's human-readable name ("Olive Young") is grounded when
    # its domain (oliveyoung.com) is — collapse the entity's significant words
    # (dropping connectors/common words the extractor may have swept in, e.g. a
    # trailing "for") and match the registrable label of any allowed domain.
    significant = [
        word
        for word in re.findall(r"[a-z0-9]+", normalized)
        if word not in _CONNECTOR_WORDS and not _is_common_entity_word(word)
    ]
    collapsed = "".join(significant)
    if collapsed and any(
        collapsed == _registrable_label(d) for d in allowed["domains"]
    ):
        return True
    # The significant-words collapse above DROPS common words, so a host display
    # name whose parts are common ("Who What Wear" -> whowhatwear.com, "NBC News"
    # -> nbcnews.com) never resolves, and a possessive ("Reddit's") won't match
    # the bare label — both silently rejected valid briefs that named a REAL
    # cited source. Match the FULL de-punctuated form (possessive stripped)
    # against any allowed host label. Safe: only an already-allowed host resolves.
    _domain_labels = {_registrable_label(d) for d in allowed["domains"]}
    depossessed = re.sub(r"['’]s\b", "", normalized).strip()
    full_collapse = re.sub(r"[^a-z0-9]", "", depossessed)
    if full_collapse and full_collapse in _domain_labels:
        return True
    # Possessive of an otherwise-allowed entity ("Reddit's" -> Reddit).
    if depossessed and depossessed != normalized and depossessed in allowed["terms"]:
        return True
    # Hyphenated descriptor whose LEAD is an allowed brand/host and whose tail
    # words are common/grounded ("Sephora-controlled", "Reddit-native").
    if "-" in normalized:
        parts = [p for p in re.split(r"-+", normalized) if p]
        lead = parts[0] if parts else ""
        lead_ok = bool(lead) and (
            lead in allowed["terms"]
            or re.sub(r"[^a-z0-9]", "", lead) in _domain_labels
        )
        if (
            len(parts) >= 2
            and lead_ok
            and all(_entity_word_grounded_or_common(p, allowed) for p in parts[1:])
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
    _GENERIC_COMMERCE_ENTITIES,
    _COMMON_PROSE_NOUNS,
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
