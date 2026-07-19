"""SKU attribute graphing and sidewalk prompt generation.

The free SKU wedge has a tight prompt budget, so sidewalk prompts must come
from product-substantiated attributes instead of loose keyword expansion. This
module keeps the data shapes small and explicit so later scoring/rendering can
reuse the same attribute_basis and evidence without re-parsing product copy.
"""

from __future__ import annotations

from html import unescape
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from services.promo_terms import is_promo_term


ATTRIBUTE_CLASSES: Tuple[str, ...] = (
    "category",
    "format",
    "ingredient",
    "certification_constraint",
    "audience",
    "use_case",
    "geography",
    "offer_variant",
    "proof",
    "exclusion",
    # Scenario/occasion demand (2026-07-03 plan P1): the buy trigger — travel,
    # flight, beach, heat styling, wedding... Populated from direct page
    # mentions (lexicon below) AND inferred from evidence edges (e.g. stick
    # format -> travel), with inferred provenance marked in evidence.
    "scenario",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_CONTEXT_SPLIT_RE = re.compile(r"[,.;:!?()\[\]\n\r]+")
# Non-Latin scripts we preserve through tokenization. The original tokenizer and
# _search_text stripped every non-ASCII character, so an all-Korean storefront
# (e.g. anukoofficial.com) tokenized to nothing -> zero attributes, no category,
# and a thin/generic strategic brief. Korean is space-separated, so its runs line
# up with lexicon phrases directly; Japanese/Chinese runs still need word
# segmentation before they can match (a Kana/Kanji run tokenizes as one blob),
# tracked separately alongside the LLM extractor fallback for the long tail.
# Ranges: Hangul syllables, Hiragana+Katakana, CJK Unified Ideographs.
_CJK_RANGES = "가-힣぀-ヿ一-鿿"
_TOKEN_RE = re.compile(rf"[a-z0-9]+|[{_CJK_RANGES}]+")

_CATEGORY_TERMS: Tuple[Tuple[str, str], ...] = (
    ("collagen", "collagen"),
    ("sunscreen", "sunscreen"),
    ("sun screen", "sunscreen"),
    ("deodorant", "deodorant"),
    ("multivitamin", "multivitamin"),
    ("multi vitamin", "multivitamin"),
    ("serum", "serum"),
    ("hair vitamins", "hair vitamin"),
    ("hair vitamin", "hair vitamin"),
    ("balm", "balm"),
    ("supplement", "supplement"),
    # K-beauty skincare/haircare categories. The lexicon above was built for a
    # supplement/sunscreen/deodorant pilot and had no haircare or core-skincare
    # categories, so an ANUKO shampoo resolved no category (hence no sidewalk
    # queries) even from an English title.
    ("shampoo", "shampoo"),
    ("conditioner", "conditioner"),
    ("toner", "toner"),
    ("essence", "essence"),
    ("ampoule", "ampoule"),
    ("cleanser", "cleanser"),
    # i18n (ko): Korean words/transliterations mapped to the canonical English
    # attribute so downstream query/brief generation is unchanged.
    ("샴푸", "shampoo"),
    ("컨디셔너", "conditioner"),
    ("세럼", "serum"),
    ("선크림", "sunscreen"),
    ("토너", "toner"),
    ("에센스", "essence"),
    ("앰플", "ampoule"),
    ("클렌저", "cleanser"),
    ("콜라겐", "collagen"),
    # Haircare category pack (2026-07-03): ANUKO/DamDam runs showed hair-oil /
    # hair-mask / moisturizer SKUs resolving no category from real titles.
    ("hair oil", "hair oil"),
    ("hair mask", "hair mask"),
    ("hair treatment", "hair treatment"),
    ("hair butter", "hair butter"),
    ("hair pack", "hair mask"),
    ("moisturizer", "moisturizer"),
    ("moisturiser", "moisturizer"),
    ("water cream", "moisturizer"),
    ("헤어 오일", "hair oil"),
    ("헤어오일", "hair oil"),
    ("헤어 마스크", "hair mask"),
    ("헤어팩", "hair mask"),
    ("트리트먼트", "hair treatment"),
    ("헤어버터", "hair butter"),
    ("헤어 버터", "hair butter"),
    ("수분크림", "moisturizer"),
    ("수분 크림", "moisturizer"),
    ("모이스처라이저", "moisturizer"),
)
_FORMAT_TERMS: Tuple[Tuple[str, str], ...] = (
    ("refill pod", "refill pod"),
    ("refill pods", "refill pod"),
    ("stick", "stick"),
    ("sticks", "stick"),
    ("gummy", "gummy"),
    ("gummies", "gummy"),
    ("balm", "balm"),
    ("powder", "powder"),
    ("jelly", "jelly"),
    ("capsule", "capsule"),
    ("capsules", "capsule"),
    ("tablet", "tablet"),
    ("tablets", "tablet"),
    ("serum", "serum"),
)

# A product's FORM is a structural fact that lives in its title / product type /
# variant, not in ingredient prose. Restricting format extraction to these
# authoritative sources stops e.g. "low molecular collagen powder" in the body
# copy making a tablet SKU read as a "powder", which then yields
# "<attr> collagen powder" lanes the tablet can't substantiate.
_FORM_AUTHORITATIVE_SOURCES = {"title", "product_type", "variant", "tag"}
# When form signals conflict (merchants tag a tablet SKU with a loose "powder"
# category tag while the title says "84 tablets"), the most specific source
# wins. The title/variant name the actual SKU; tags are loose categorization.
_FORM_SOURCE_PRIORITY = ("title", "variant", "product_type", "tag")
_INGREDIENT_TERMS: Tuple[Tuple[str, str], ...] = (
    ("fish collagen", "fish collagen"),
    ("marine collagen", "marine collagen"),
    ("low molecular collagen", "low molecular collagen"),
    ("vitamin c", "vitamin c"),
    ("omega-3 dha", "omega-3 dha"),
    ("omega 3 dha", "omega-3 dha"),
    ("omega-3", "omega-3"),
    ("omega 3", "omega-3"),
    ("dha", "dha"),
    ("vitamin d3", "vitamin d3"),
    ("vitamin d", "vitamin d"),
    ("vitamin b12", "vitamin b12"),
    ("b12", "vitamin b12"),
    ("methylated folate", "methylated folate"),
    ("folate", "folate"),
    ("iron", "iron"),
    ("glycine", "glycine"),
    ("zinc oxide", "zinc oxide"),
    ("titanium dioxide", "titanium dioxide"),
    ("niacinamide", "niacinamide"),
    ("hyaluronic acid", "hyaluronic acid"),
    # i18n (ko): map to English actives already present in this lexicon.
    ("나이아신아마이드", "niacinamide"),
    ("히알루론산", "hyaluronic acid"),
    ("비타민 c", "vitamin c"),
    ("비타민c", "vitamin c"),
    # Haircare/K-beauty actives pack (2026-07-03): argan/shea/yuja were the
    # literal hero ingredients of real audited SKUs and had no lexicon entry.
    ("argan oil", "argan oil"),
    ("shea butter", "shea butter"),
    ("yuzu seed oil", "yuzu seed oil"),
    ("yuja seed oil", "yuzu seed oil"),
    ("green tea", "green tea"),
    ("camellia oil", "camellia oil"),
    ("tea tree", "tea tree"),
    ("keratin", "keratin"),
    ("ceramide", "ceramide"),
    ("biotin", "biotin"),
    ("centella", "centella"),
    ("cica", "centella"),
    ("ginkgo", "ginkgo"),
    ("peptide", "peptide"),
    ("panthenol", "panthenol"),
    ("아르간 오일", "argan oil"),
    ("아르간오일", "argan oil"),
    ("시어버터", "shea butter"),
    ("시어 버터", "shea butter"),
    ("유자씨 오일", "yuzu seed oil"),
    ("유자씨오일", "yuzu seed oil"),
    ("녹차", "green tea"),
    ("그린티", "green tea"),
    ("동백 오일", "camellia oil"),
    ("동백오일", "camellia oil"),
    ("티트리", "tea tree"),
    ("케라틴", "keratin"),
    ("세라마이드", "ceramide"),
    ("비오틴", "biotin"),
    ("병풀", "centella"),
    ("시카", "centella"),
    ("펩타이드", "peptide"),
    ("판테놀", "panthenol"),
)
_CERTIFICATION_TERMS: Tuple[Tuple[str, str], ...] = (
    ("halal certified", "halal"),
    ("halal-certified", "halal"),
    ("halal", "halal"),
    ("reef safe", "reef-safe"),
    ("reef-safe", "reef-safe"),
    ("vegan", "vegan"),
    ("fragrance free", "fragrance-free"),
    ("fragrance-free", "fragrance-free"),
    ("baking soda free", "baking-soda-free"),
    ("baking-soda-free", "baking-soda-free"),
    ("cruelty free", "cruelty-free"),
    ("cruelty-free", "cruelty-free"),
    ("mineral", "mineral"),
    # i18n (ko): positive claim terms only. 무향 ("scent-free") already encodes
    # the negated concept, so mapping it directly sidesteps the English-only
    # negation guardrails (a bare Korean active with Korean negation grammar is
    # out of scope for this head-term pass — see the LLM fallback follow-up).
    ("비건", "vegan"),
    ("무향", "fragrance-free"),
    ("크루얼티프리", "cruelty-free"),
)
_AUDIENCE_TERMS: Tuple[Tuple[str, str], ...] = (
    ("kids", "kids"),
    ("children", "kids"),
    ("child", "kids"),
    ("baby", "kids"),
    ("postpartum", "postpartum"),
    ("men", "men"),
    ("women", "women"),
    ("woman", "women"),
    ("prenatal", "prenatal"),
    ("sensitive skin", "sensitive skin"),
    ("eczema prone", "sensitive skin"),
    ("eczema-prone", "sensitive skin"),
    ("pregnancy", "pregnancy"),
    ("pregnant", "pregnancy"),
)
_USE_CASE_TERMS: Tuple[Tuple[str, str], ...] = (
    ("before bed", "before bed"),
    ("bedtime", "before bed"),
    ("bed time", "before bed"),
    ("good night", "before bed"),
    ("travel", "travel"),
    ("on the go", "travel"),
    ("on-the-go", "travel"),
    ("portable", "travel"),
    ("gym", "gym"),
    ("summer camp", "summer"),
    ("summer", "summer"),
    ("sensitive skin", "sensitive skin"),
)
_GEOGRAPHY_TERMS: Tuple[Tuple[str, str], ...] = (
    ("k beauty", "k-beauty"),
    ("k-beauty", "k-beauty"),
    ("korean", "korean"),
    ("singapore", "singapore"),
    ("uae", "uae"),
    ("united states", "us"),
    (" usa ", "us"),
)
_EXCLUSION_TERMS: Tuple[Tuple[str, str], ...] = (
    ("no water needed", "no water"),
    ("no water", "no water"),
    ("without water", "no water"),
    ("water free", "no water"),
    ("water-free", "no water"),
    ("no melatonin", "no melatonin"),
    ("melatonin free", "no melatonin"),
    ("melatonin-free", "no melatonin"),
    ("no baking soda", "no baking soda"),
    ("baking soda free", "no baking soda"),
    ("baking-soda-free", "no baking soda"),
    ("sugar free", "sugar-free"),
    ("sugar-free", "sugar-free"),
    ("no artificial dye", "no artificial dye"),
    ("no white cast", "no white cast"),
)
_PROOF_TERMS: Tuple[Tuple[str, str], ...] = (
    ("dermatologist tested", "dermatologist-tested"),
    ("dermatologist-tested", "dermatologist-tested"),
    ("clinically tested", "clinically tested"),
    ("traceable", "traceable"),
    ("traceability", "traceable"),
    ("usp verified", "usp verified"),
    ("verified", "verified"),
    ("award", "award"),
    ("reviews", "reviews"),
    ("low molecular", "low molecular"),
    ("1000 da", "1000 da"),
    ("1,000 da", "1000 da"),
)

# ---------------------------------------------------------------------------
# Scenario/occasion demand layer (2026-07-03 plan P1). Scenarios are the buy
# TRIGGER (travel, flight, beach, heat styling, a wedding), distinct from
# use_case routine terms. Two population paths:
#   1. direct page mention via _SCENARIO_TERMS (EN + KR aliases);
#   2. inferred via _SCENARIO_EVIDENCE_EDGES from already-evidenced attributes
#      (stick format -> travel; reef-safe -> swim/beach), marked "inferred" in
#      evidence so rendering can never present it as a page claim.
# use_case values {travel, gym, summer} pre-date this class and stay where they
# are; the prompt generator unions them into the scenario pool (no dupes).
# Safety: no medical/pregnancy scenarios — those remain audience-gated.
_SCENARIO_TERMS: Tuple[Tuple[str, str], ...] = (
    ("heat styling", "heat-styling"),
    ("heat protection", "heat-styling"),
    ("heat protectant", "heat-styling"),
    ("blow dry", "heat-styling"),
    ("blow-dry", "heat-styling"),
    ("flat iron", "heat-styling"),
    ("고데기", "heat-styling"),
    ("열기구", "heat-styling"),
    ("드라이기", "heat-styling"),
    ("열 보호", "heat-styling"),
    ("열보호", "heat-styling"),
    ("flight", "flight"),
    ("in-flight", "flight"),
    ("carry on", "flight"),
    ("carry-on", "flight"),
    ("기내", "flight"),
    ("jet lag", "jet-lag"),
    ("jetlag", "jet-lag"),
    ("시차", "jet-lag"),
    ("beach", "beach"),
    ("해변", "beach"),
    ("바닷가", "beach"),
    ("swim", "swim"),
    ("swimming", "swim"),
    ("수영", "swim"),
    ("물놀이", "swim"),
    ("hiking", "hiking"),
    ("trekking", "hiking"),
    ("등산", "hiking"),
    ("wedding", "wedding"),
    ("bridal", "wedding"),
    ("웨딩", "wedding"),
    ("결혼식", "wedding"),
    ("party", "party"),
    ("night out", "party"),
    ("파티", "party"),
    ("office", "office"),
    ("사무실", "office"),
    ("humidity", "humidity"),
    ("humid", "humidity"),
    ("습기", "humidity"),
    ("습한", "humidity"),
    ("winter", "winter"),
    ("겨울", "winter"),
    ("overnight", "overnight"),
    ("오버나이트", "overnight"),
    ("여행", "travel"),
    ("여행용", "travel"),
    ("휴대용", "travel"),
)

# use_case values that are really scenarios; the prompt generator unions them
# into the scenario pool so legacy extraction keeps working unchanged.
_USE_CASE_SCENARIO_SLUGS = frozenset({"travel", "gym", "summer"})

# Shopper-natural phrase per scenario slug, for "best {category} for {phrase}"
# shapes. Keep phrases plain and provable — never a claim.
_SCENARIO_PHRASES: Dict[str, str] = {
    "heat-styling": "heat styling",
    "flight": "a long flight",
    "jet-lag": "jet lag",
    "beach": "the beach",
    "swim": "swimming",
    "hiking": "hiking",
    "wedding": "a wedding",
    "party": "a night out",
    "office": "the office",
    "humidity": "humid weather",
    "winter": "winter dryness",
    "overnight": "overnight use",
    "travel": "travel",
    "gym": "the gym",
    "summer": "summer",
}

# Scenarios where "what {category} to pack for {phrase}" reads naturally.
_PACKABLE_SCENARIOS = frozenset({"travel", "flight", "beach", "gym", "hiking"})

# Inferred scenario edges: (attribute_class, attribute_value) -> scenario slugs.
# Only substantiation-safe inferences — a physical property implying a context
# of use, never an efficacy claim.
_SCENARIO_EVIDENCE_EDGES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("format", "stick", ("travel",)),
    ("format", "refill pod", ("travel",)),
    ("format", "gummy", ("travel",)),
    ("exclusion", "no water", ("travel",)),
    ("exclusion", "no melatonin", ("jet-lag",)),
    ("certification_constraint", "reef-safe", ("swim", "beach")),
    ("category", "sunscreen", ("beach", "swim")),
)


def _apply_scenario_edges(
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    """Append evidence-edge-inferred scenarios (provenance marked 'inferred').
    Direct page mentions win: an already-present slug is never overwritten."""
    present = set(classes.get("scenario") or [])
    for source_class, value, slugs in _SCENARIO_EVIDENCE_EDGES:
        if value not in (classes.get(source_class) or []):
            continue
        for slug in slugs:
            if slug in present:
                continue
            present.add(slug)
            classes.setdefault("scenario", []).append(slug)
            evidence.setdefault(
                slug, f"inferred from {source_class}: {value}",
            )


_MEDICAL_BLOCKLIST: Tuple[str, ...] = (
    "sleep aid",
    "helps you sleep",
    "help you sleep",
    "sleep support",
    "cure",
    "treat",
    "heals",
    "healing",
    "medical",
    "eczema",
    "therapy",
    "doctor recommended",
    "pregnancy safe",
    "safe for kids",
)
_GENERIC_CATEGORIES = {"", "product", "products", "item", "items"}

# Tablet/capsule fillers, flow agents and colorants. These are never a
# selling-point lane in ANY vertical, so a SKU is never pitched on them even if
# they appear in an ingredient list or tag.
_EXCIPIENT_BLOCKLIST: Tuple[str, ...] = (
    "magnesium stearate",
    "silicon dioxide",
    "silicon dioxide (anti caking)",
    "microcrystalline cellulose",
    "calcium phosphate",
    "dicalcium phosphate",
    "croscarmellose",
    "croscarmellose sodium",
    "hydroxypropyl methylcellulose",
    "hypromellose",
    "carnauba wax",
    "maltodextrin",
)
# Mineral UV filters: a genuine selling point ONLY for mineral sunscreen, but a
# coating colorant/excipient in an ingestible (e.g. titanium dioxide in a
# collagen tablet). Kept as a lane attribute only in a mineral-sunscreen context;
# stripped otherwise so "titanium dioxide collagen powder" can never headline.
_MINERAL_ACTIVES = {"titanium dioxide", "zinc oxide"}
_INGESTIBLE_CATEGORIES = {
    "collagen", "supplement", "multivitamin", "vitamin", "hair vitamin", "gummy",
}
_INGESTIBLE_FORMATS = {"tablet", "capsule", "powder", "gummy", "jelly", "stick"}


def _is_mineral_sunscreen_context(classes: Mapping[str, List[str]]) -> bool:
    """True only when the product reads as a mineral sunscreen / topical, where
    titanium dioxide and zinc oxide are real actives rather than excipients."""
    cats = {_clean_attr(c) for c in (classes.get("category") or [])}
    certs = {_clean_attr(c) for c in (classes.get("certification_constraint") or [])}
    proof = {_clean_attr(c) for c in (classes.get("proof") or [])}
    if "sunscreen" in cats:
        return True
    if "mineral" in certs or "mineral" in proof:
        return True
    # An ingestible category/format with no sunscreen signal is never mineral.
    return False
_NEGATION_TOKENS = {
    "not",
    "no",
    "without",
    "lack",
    "lacks",
    "lacking",
    "isnt",
    "doesnt",
    "dont",
    "cant",
    "cannot",
}
_NEGATION_AWARE_CLASSES = {
    "certification_constraint",
    "exclusion",
    "ingredient",
    "proof",
    "audience",
}
_HIGH_CARE_AUDIENCES = {"pregnancy", "kids"}
_HIGH_CARE_POSITIVE_SOURCES = {"product_type", "tag", "title", "variant"}
_DIRECT_TAG_STOPWORDS = {
    "best seller",
    "bestseller",
    "new",
    "sale",
    "subscription",
    "subscribe",
    "trial",
}
_DIRECT_TAG_NOISE = {
    "berry",
    "blueberry",
    "cherry",
    "flavor",
    "flavoured",
    "flavored",
    "garden",
    "glow",
    "grape",
    "lemon",
    "mango",
    "orange",
    "shine",
    "strawberry",
    "vanilla",
}
_CATEGORY_MARKETING_NOISE = {
    "glow",
    "grape",
    "jelly",
    "orange",
    "shine",
}
_DIRECT_INGREDIENT_HINT_RE = re.compile(
    r"\b(?:omega|dha|epa|vitamin|b12|d3|folate|iron|magnesium|boron|calcium)\b",
    re.IGNORECASE,
)


def _clean_attr(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unescape(_HTML_TAG_RE.sub(" ", text))
    return _SPACE_RE.sub(" ", text).strip(" \t\r\n,.;:/")


def _search_text(value: Any) -> str:
    text = _clean_attr(value)
    text = text.replace("-", " ")
    text = re.sub(rf"[^a-z0-9${_CJK_RANGES}]+", " ", text)
    return f" {_SPACE_RE.sub(' ', text).strip()} "


def _has_phrase(text: str, phrase: str) -> bool:
    needle = _search_text(phrase).strip()
    return bool(needle) and f" {needle} " in _search_text(text)


def _context_tokens(value: Any) -> List[str]:
    text = str(value or "").lower()
    text = text.replace("’", "'").replace("`", "'")
    text = re.sub(r"\b(isn|doesn|don|aren|wasn|weren|can)'?t\b", r"\1t", text)
    text = text.replace("-", " ")
    return _TOKEN_RE.findall(text)


def _phrase_spans(tokens: List[str], phrase: str) -> List[Tuple[int, int]]:
    needle = _context_tokens(phrase)
    if not tokens or not needle or len(needle) > len(tokens):
        return []
    width = len(needle)
    return [
        (idx, idx + width)
        for idx in range(0, len(tokens) - width + 1)
        if tokens[idx:idx + width] == needle
    ]


def _exclusion_phrase_is_own_negation(phrase_tokens: List[str]) -> bool:
    if not phrase_tokens:
        return False
    if phrase_tokens[0] in {"no", "without"}:
        return True
    return phrase_tokens[-1:] == ["free"] or phrase_tokens[-2:] == ["free", "of"]


def _match_is_negated(
    *,
    tokens: List[str],
    start: int,
    end: int,
    class_name: str,
    phrase: str,
) -> bool:
    if class_name not in _NEGATION_AWARE_CLASSES:
        return False
    lookback = tokens[max(0, start - 5):start]
    own_exclusion_negation = (
        class_name == "exclusion"
        and _exclusion_phrase_is_own_negation(_context_tokens(phrase))
        and not lookback
    )
    if not own_exclusion_negation and any(token in _NEGATION_TOKENS for token in lookback):
        return True
    if len(lookback) >= 2 and lookback[-2:] == ["free", "of"]:
        return True
    if len(lookback) >= 3 and lookback[-3:] == ["doesnt", "contain"]:
        return True
    if class_name == "ingredient":
        lookahead = tokens[end:min(len(tokens), end + 2)]
        if lookahead[:1] == ["free"]:
            return True
    if class_name == "certification_constraint":
        lookahead = tokens[end:min(len(tokens), end + 4)]
        if "not" in lookahead and any(token.startswith("certif") for token in lookahead):
            return True
        if "isnt" in lookahead and any(token.startswith("certif") for token in lookahead):
            return True
    return False


def _mineral_context_allows(segment: str, tokens: List[str], start: int, end: int) -> bool:
    phrase_window = tokens[max(0, start - 3):min(len(tokens), end + 4)]
    window = " ".join(phrase_window)
    segment_text = " ".join(tokens)
    if "mineral oil" in window or "mineral oil" in segment_text:
        return False
    allowed_contexts = (
        "mineral sunscreen",
        "mineral sun screen",
        "mineral spf",
        "zinc oxide",
        "titanium dioxide",
    )
    normalized_segment = _search_text(segment)
    return any(context in normalized_segment for context in allowed_contexts)


def _high_care_positive_context(
    *,
    tokens: List[str],
    start: int,
    end: int,
    attr: str,
    source: str,
) -> bool:
    if attr not in _HIGH_CARE_AUDIENCES:
        return True
    if source in _HIGH_CARE_POSITIVE_SOURCES:
        return True
    before = tokens[max(0, start - 4):start]
    after = tokens[end:min(len(tokens), end + 4)]
    if "for" in before[-3:]:
        return True
    if any(token in before for token in {"made", "designed", "formulated", "intended", "suitable"}):
        return True
    if attr == "pregnancy" and any(token in after for token in {"support", "routine", "use"}):
        return True
    if attr == "kids" and any(token in after for token in {"sunscreen", "stick", "balm", "deodorant"}):
        return True
    return False


def _has_allowed_phrase_match(
    *,
    text: str,
    source: str,
    class_name: str,
    phrase: str,
    attr: str,
) -> bool:
    for segment in _CONTEXT_SPLIT_RE.split(str(text or "")):
        tokens = _context_tokens(segment)
        for start, end in _phrase_spans(tokens, phrase):
            if _match_is_negated(
                tokens=tokens,
                start=start,
                end=end,
                class_name=class_name,
                phrase=phrase,
            ):
                continue
            if class_name == "certification_constraint" and attr == "mineral":
                if not _mineral_context_allows(segment, tokens, start, end):
                    continue
            if class_name == "audience" and attr in _HIGH_CARE_AUDIENCES:
                if not _high_care_positive_context(
                    tokens=tokens,
                    start=start,
                    end=end,
                    attr=attr,
                    source=source,
                ):
                    continue
            return True
    return False


def _dedupe(items: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        cleaned = _clean_attr(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _tags_from_raw(raw: Any) -> List[str]:
    if isinstance(raw, str):
        return _dedupe(raw.split(","))
    if isinstance(raw, list):
        return _dedupe(str(item) for item in raw)
    return []


def _string_values(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, list):
        out: List[str] = []
        for item in value:
            out.extend(_string_values(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_string_values(item))
        return out
    return []


def _iter_product_sources(product: Mapping[str, Any]) -> Iterable[Tuple[str, str]]:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}

    for value in (
        product.get("product_type"),
        product.get("category"),
        attrs.get("product_type"),
        attrs.get("category"),
    ):
        if value:
            yield "product_type", str(value)

    for tag in _tags_from_raw(attrs.get("tags") or product.get("tags")):
        yield "tag", tag

    for value in (product.get("title"), product.get("raw_title")):
        if value:
            yield "title", str(value)

    for value in (
        attrs.get("description"),
        attrs.get("body_html"),
        product.get("description"),
    ):
        if value:
            yield "body", str(value)

    for collection_key in ("variants", "options"):
        raw_collection = attrs.get(collection_key)
        if isinstance(raw_collection, list):
            for item in raw_collection:
                joined = " ".join(_string_values(item))
                if joined.strip():
                    yield "variant", joined

    # Tier-1 retailer-evidence recycling (services/retailer_evidence.py): prior
    # runs' third-party retailer/publisher excerpts, stashed on the product by
    # the probe fan-out. Body-class on purpose — NOT an authoritative source, so
    # it can surface ingredients/use-cases/certifications the thin own-page
    # never states, but can never flip authoritative-only classes like format.
    retailer_excerpts = product.get("_retailer_excerpts")
    if isinstance(retailer_excerpts, list):
        for excerpt in retailer_excerpts:
            text = str(excerpt or "").strip()
            if text:
                yield "retailer_excerpt", text


def _add_attr(
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
    class_name: str,
    attr: str,
    source: str,
) -> None:
    cleaned = _clean_attr(attr)
    if not cleaned:
        return
    # A promotional/marketing term ("skincare discount", "free shipping") is not a
    # product attribute — it leaks in via raw merchant tags/copy and, once it lands
    # in a class like use_case, surfaces verbatim in the merchant-facing brief
    # (flagged live on the DAMDAM report). Drop it at the single attribute
    # chokepoint so it can never become an attribute in any class. Shares the stop
    # list with query generation via `services.promo_terms`.
    if is_promo_term(cleaned):
        return
    bucket = classes.setdefault(class_name, [])
    if cleaned not in bucket:
        bucket.append(cleaned)
    evidence.setdefault(cleaned, source)


def _add_lexicon_matches(
    *,
    text: str,
    source: str,
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
    class_name: str,
    lexicon: Iterable[Tuple[str, str]],
) -> None:
    for phrase, attr in lexicon:
        if _has_allowed_phrase_match(
            text=text,
            source=source,
            class_name=class_name,
            phrase=phrase,
            attr=attr,
        ):
            _add_attr(classes, evidence, class_name, attr, source)


def _safe_direct_attr(value: Any) -> str:
    cleaned = _clean_attr(value)
    if (
        not cleaned
        or len(cleaned) < 3
        or cleaned in _DIRECT_TAG_STOPWORDS
        or cleaned in _GENERIC_CATEGORIES
    ):
        return ""
    return cleaned


def _direct_tag_class(term: str) -> Optional[str]:
    cleaned = _safe_direct_attr(term)
    if not cleaned:
        return None
    if cleaned in _DIRECT_TAG_NOISE:
        return None
    for _phrase, attr in _CATEGORY_TERMS:
        if cleaned == attr or _has_phrase(cleaned, attr):
            return "category"
    for _phrase, attr in _FORMAT_TERMS:
        if cleaned == attr or _has_phrase(cleaned, attr):
            return "format"
    searchable = _search_text(cleaned)
    if (
        re.search(r"\b(?:no|without)\b", searchable)
        or cleaned.endswith("-free")
        or cleaned.endswith(" free")
        or " free " in searchable
    ):
        return "exclusion"
    if any(word in searchable for word in (" vegan ", " halal ", " cruelty free ", " reef safe ")):
        return "certification_constraint"
    if any(word in searchable for word in (" women ", " woman ", " men ", " prenatal ", " postpartum ", " kids ")):
        return "audience"
    if _DIRECT_INGREDIENT_HINT_RE.search(cleaned):
        return "ingredient"
    if any(word in searchable for word in (" traceable ", " verified ", " clinically tested ", " award ")):
        return "proof"
    if cleaned.endswith("vitamin") or cleaned.endswith("vitamins") or "multivitamin" in searchable:
        return "category"
    # Scenario tags ("heat protection", "여행용") classify as scenario, not the
    # multi-word use_case default — keeps them out of problem-framed templates
    # ("what helps with heat protection") and in the scenario shapes instead.
    for _phrase, attr in _SCENARIO_TERMS:
        if cleaned == attr or _has_phrase(cleaned, _phrase):
            return "scenario"
    if len(cleaned.split()) == 1:
        return None
    return "use_case"


def is_scenario_slug(value: str) -> bool:
    """True for canonical scenario slugs (incl. legacy use_case scenarios) —
    consumers use this to keep scenario terms out of problem-framed templates."""
    return str(value or "").strip() in _SCENARIO_PHRASES


def _canonical_scenario(term: str) -> Optional[str]:
    """Map a scenario alias ("heat protection", "여행용") to its canonical slug;
    None when the term isn't in the scenario lexicon."""
    cleaned = str(term or "").strip()
    if not cleaned:
        return None
    if cleaned in _SCENARIO_PHRASES:
        return cleaned
    for phrase, slug in _SCENARIO_TERMS:
        if cleaned == phrase or cleaned == slug or _has_phrase(cleaned, phrase):
            return slug
    return None


def _direct_category_allowed(value: str) -> bool:
    cleaned = _safe_direct_attr(value)
    if not cleaned:
        return False
    searchable = _search_text(cleaned)
    tokens = set(_context_tokens(cleaned))
    if tokens & _CATEGORY_MARKETING_NOISE:
        return False
    if " supplement " in searchable or " vitamin " in searchable or " multivitamin " in searchable:
        return True
    return any(
        cleaned == attr or _has_phrase(cleaned, attr)
        for _phrase, attr in _CATEGORY_TERMS
    )


def _add_direct_merchant_attrs(
    product: Mapping[str, Any],
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}

    for source, value in (
        ("product_type", product.get("product_type")),
        ("product_type", product.get("category")),
        ("product_type", attrs.get("product_type")),
        ("product_type", attrs.get("category")),
    ):
        direct = _safe_direct_attr(value)
        if (
            direct
            and direct not in {"beauty", "wellness", "supplements"}
            and _direct_category_allowed(direct)
        ):
            _add_attr(classes, evidence, "category", direct, source)

    for tag in _tags_from_raw(attrs.get("tags") or product.get("tags")):
        cleaned = _safe_direct_attr(tag)
        class_name = _direct_tag_class(cleaned)
        if not class_name:
            continue
        if class_name == "scenario":
            # Canonicalize alias -> slug ("heat protection"/"여행용" ->
            # heat-styling/travel) so a direct tag never mints a duplicate
            # raw-alias lane next to the lexicon's canonical one.
            cleaned = _canonical_scenario(cleaned) or ""
            if not cleaned:
                continue
        _add_attr(classes, evidence, class_name, cleaned, "tag")


def _add_offer_and_proof_attrs(
    product: Mapping[str, Any],
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    attrs = product.get("attributes_raw")
    attrs = attrs if isinstance(attrs, dict) else {}

    rating = attrs.get("aggregateRating")
    if isinstance(rating, dict) and (rating.get("ratingValue") or rating.get("reviewCount")):
        _add_attr(classes, evidence, "proof", "reviews", "structured_data")

    offers = attrs.get("offers")
    offers_list = offers if isinstance(offers, list) else [offers]
    for offer in offers_list:
        if not isinstance(offer, dict):
            continue
        availability = _clean_attr(offer.get("availability"))
        if "in stock" in availability or "instock" in availability:
            _add_attr(classes, evidence, "offer_variant", "in stock", "structured_data")
        price = _clean_attr(offer.get("price"))
        if price:
            _add_attr(classes, evidence, "offer_variant", f"${price}", "structured_data")


def _add_quantity_attrs(
    sources: Iterable[Tuple[str, str]],
    classes: Dict[str, List[str]],
    evidence: Dict[str, str],
) -> None:
    quantity_re = re.compile(
        r"\b\d+(?:\.\d+)?\s*(?:sticks?|packs?|packages?|ct|count|ml|mg|g|oz)\b",
        re.IGNORECASE,
    )
    for source, text in sources:
        for match in quantity_re.findall(text or ""):
            _add_attr(classes, evidence, "offer_variant", match, source)


def _resolve_authoritative_form(product: Mapping[str, Any], fallback: List[str]) -> List[str]:
    """Pick the SKU's real form from the most specific authoritative source that
    names one (title > variant > product_type > tag), so a loose 'powder' tag on
    an '84 tablets' SKU does not survive alongside 'tablet'."""
    by_source: Dict[str, List[str]] = {}
    for source, text in _iter_product_sources(product):
        if source not in _FORM_AUTHORITATIVE_SOURCES:
            continue
        normalized = _search_text(text)
        for phrase, attr in _FORMAT_TERMS:
            if _has_phrase(normalized, phrase):
                by_source.setdefault(source, []).append(attr)
    for source in _FORM_SOURCE_PRIORITY:
        if by_source.get(source):
            return _dedupe(by_source[source])
    return fallback


def build_sku_attribute_graph(product: dict) -> dict:
    """Normalize substantiated product facts into sidewalk-ready classes.

    Evidence is intentionally attached to the attribute, not the class, because
    prompt generation and later rendering both need to prove each lane came
    from product data rather than inferred category lore.
    """
    safe_product: Mapping[str, Any] = product if isinstance(product, dict) else {}
    classes: Dict[str, List[str]] = {name: [] for name in ATTRIBUTE_CLASSES}
    evidence: Dict[str, str] = {}
    sources = list(_iter_product_sources(safe_product))

    for source, text in sources:
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="category",
            lexicon=_CATEGORY_TERMS,
        )
        if source in _FORM_AUTHORITATIVE_SOURCES:
            _add_lexicon_matches(
                text=text,
                source=source,
                classes=classes,
                evidence=evidence,
                class_name="format",
                lexicon=_FORMAT_TERMS,
            )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="ingredient",
            lexicon=_INGREDIENT_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="certification_constraint",
            lexicon=_CERTIFICATION_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="audience",
            lexicon=_AUDIENCE_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="use_case",
            lexicon=_USE_CASE_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="geography",
            lexicon=_GEOGRAPHY_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="exclusion",
            lexicon=_EXCLUSION_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="proof",
            lexicon=_PROOF_TERMS,
        )
        _add_lexicon_matches(
            text=text,
            source=source,
            classes=classes,
            evidence=evidence,
            class_name="scenario",
            lexicon=_SCENARIO_TERMS,
        )

    _add_direct_merchant_attrs(safe_product, classes, evidence)
    _add_offer_and_proof_attrs(safe_product, classes, evidence)
    _add_quantity_attrs(sources, classes, evidence)

    if "stick" in classes["format"] and "no water" in classes["exclusion"]:
        _add_attr(
            classes,
            evidence,
            "use_case",
            "travel",
            evidence.get("no water") or evidence.get("stick") or "body",
        )

    # Resolve conflicting form signals to the most specific authoritative source.
    classes["format"] = _resolve_authoritative_form(safe_product, classes.get("format", []))

    # Scenario inference AFTER format resolution so a demoted loose-tag format
    # can't mint travel lanes the SKU's real form doesn't substantiate.
    _apply_scenario_edges(classes, evidence)

    # Mineral UV filters are excipients/colorants in an ingestible — never pitch
    # a collagen tablet on "titanium dioxide". Keep them only for sunscreen.
    if not _is_mineral_sunscreen_context(classes):
        classes["ingredient"] = [
            attr for attr in classes.get("ingredient", [])
            if _clean_attr(attr) not in _MINERAL_ACTIVES
        ]

    for class_name in ATTRIBUTE_CLASSES:
        classes[class_name] = _dedupe(classes.get(class_name, []))

    return {"classes": classes, "evidence": evidence}


# Tail-noun fallback stoplist (#1503): product_type tails that are container /
# department words, not buyer-facing categories — "Beauty" must not become
# "best beauty". Supplement stays excluded for the same reason as above.
_TAIL_NOUN_STOPLIST = frozenset({
    "beauty", "care", "misc", "general", "default", "other", "goods",
    "merchandise", "accessories", "supplement", "supplements",
    # Packaging/collection words: "Gift Bundle", "Value Pack", "Essentials"
    # describe how a product is sold, not what it is.
    "bundle", "bundles", "pack", "packs", "collection", "collections",
    "essentials", "set", "sets", "kit", "kits", "box", "boxes",
    "assortment", "variety", "value",
})


def _first_buyer_category(classes: Mapping[str, List[str]], product_type: str) -> Optional[str]:
    for category in classes.get("category") or []:
        cleaned = _clean_attr(category)
        if cleaned not in _GENERIC_CATEGORIES and cleaned != "supplement":
            return cleaned
    product_type_text = _clean_attr(product_type)
    for _phrase, category in _CATEGORY_TERMS:
        if (
            _has_phrase(product_type_text, category)
            and category not in _GENERIC_CATEGORIES
            and category != "supplement"
        ):
            return category
    # #1503 vertical-agnostic fallback: the lexicon above is beauty-fitted, so a
    # non-beauty SKU ("Camera Drone", "Bone Conduction Headphones") resolved no
    # category and the whole generator returned [] for every non-beauty vertical.
    # Use the cleaned product_type's tail noun ("drone", "headphones") — the
    # buyer-facing head of a "<modifiers> <noun>" product type — guarded by a
    # container-word stoplist so "Beauty"/"Supplements" never become categories.
    tail_tokens = re.findall(r"[a-z0-9]+", product_type_text)
    if tail_tokens:
        tail = tail_tokens[-1]
        if (
            len(tail) >= 4
            and not tail.isdigit()
            and tail not in _GENERIC_CATEGORIES
            and tail not in _TAIL_NOUN_STOPLIST
        ):
            return tail
    return None


def _plural_format(value: str) -> str:
    value = _clean_attr(value)
    if value == "stick":
        return "sticks"
    if value == "gummy":
        return "gummies"
    if value == "capsule":
        return "capsules"
    if value == "tablet":
        return "tablets"
    if value == "refill pod":
        return "refill pods"
    return value


def _geo_for_query(value: str) -> str:
    cleaned = _clean_attr(value)
    if cleaned == "k-beauty":
        return "korean"
    return cleaned


def _clean_query(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" \t\r\n,.;:/")
    return value.lower()


def _query_has_category(query: str, category: str) -> bool:
    return bool(category) and _has_phrase(query, category)


def _violates_guardrail(query: str, basis: Iterable[str] | None = None) -> bool:
    q = _clean_query(query)
    if "sleep" in q:
        return True
    for blocked in _MEDICAL_BLOCKLIST:
        if blocked in q:
            return True
    # Tablet/capsule fillers and colorants are never a lane, regardless of how
    # they entered the basis (ingredient list, tag, direct attr).
    cleaned_basis_for_excipient = {_clean_attr(b) for b in (basis or [])}
    for excipient in _EXCIPIENT_BLOCKLIST:
        if excipient in q or excipient in cleaned_basis_for_excipient:
            return True
    if "safe" in q and "reef-safe" not in q:
        return True
    cleaned_basis = set(_dedupe(basis or []))
    if re.search(r"\bfor\s+(?:pregnancy|pregnant)\b", q) and "pregnancy" not in cleaned_basis:
        return True
    if re.search(r"\bfor\s+(?:kids|children|child|baby)\b", q) and "kids" not in cleaned_basis:
        return True
    return False


def _basis_evidence(graph: Mapping[str, Any], basis: Iterable[str]) -> List[str]:
    evidence = graph.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    out: List[str] = []
    seen = set()
    for attr in basis:
        key = str(attr)
        # "scenario:<slug>" basis markers (P0 coverage signal) resolve to the
        # slug's evidence — a page mention or the "inferred from ..." note.
        if key.startswith("scenario:"):
            key = key.split(":", 1)[1]
        source = str(evidence.get(key) or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        out.append(source)
    return out


def _candidate(
    graph: Mapping[str, Any],
    query: str,
    basis: Iterable[str],
    weight: float,
) -> Optional[Dict[str, Any]]:
    cleaned_basis = _dedupe(basis)
    if not cleaned_basis:
        return None
    cleaned_query = _clean_query(query)
    if not cleaned_query or _violates_guardrail(cleaned_query, cleaned_basis):
        return None
    return {
        "query": cleaned_query,
        "axis": "sidewalk",
        "attribute_basis": cleaned_basis,
        "evidence": _basis_evidence(graph, cleaned_basis),
        "intent_weight": float(weight),
    }


def _benefit_for_category(category: str) -> Optional[str]:
    if category == "collagen":
        return "skin"
    if category == "sunscreen":
        return "summer"
    if category == "deodorant":
        return "gym"
    return None


def _destutter_query(value: str) -> str:
    words = _clean_query(value).split()
    if not words:
        return ""
    changed = True
    while changed:
        changed = False
        out: List[str] = []
        idx = 0
        while idx < len(words):
            if idx + 1 < len(words) and words[idx] == words[idx + 1]:
                out.append(words[idx])
                idx += 2
                changed = True
                continue
            if idx + 3 < len(words) and words[idx:idx + 2] == words[idx + 2:idx + 4]:
                out.extend(words[idx:idx + 2])
                idx += 4
                changed = True
                continue
            out.append(words[idx])
            idx += 1
        words = out
    return " ".join(words)


def generate_sidewalk_query_specs(
    graph: dict,
    *,
    title: str,
    product_type: str,
    n: int,
    sku_ctx: dict | None = None,
) -> list[dict]:
    """Create deterministic sidewalk prompts from evidenced attributes only."""
    del title, sku_ctx  # Reserved for later rendering/scoring without API churn.
    target = max(0, int(n or 0))
    if target <= 0:
        return []

    safe_graph: Mapping[str, Any] = graph if isinstance(graph, dict) else {}
    raw_classes = safe_graph.get("classes")
    classes: Mapping[str, List[str]] = raw_classes if isinstance(raw_classes, dict) else {}
    category = _first_buyer_category(classes, product_type or "")
    if not category:
        return []

    formats = [
        item
        for item in (classes.get("format") or [])
        if _clean_attr(item) != category
    ]
    constraints = list(classes.get("certification_constraint") or [])
    audiences = list(classes.get("audience") or [])
    use_cases = [item for item in (classes.get("use_case") or []) if item != "sleep"]
    geographies = list(classes.get("geography") or [])
    ingredients = list(classes.get("ingredient") or [])
    exclusions = list(classes.get("exclusion") or [])
    proofs = list(classes.get("proof") or [])

    candidates: List[Dict[str, Any]] = []

    # Category HEAD noun ("headphones" of "bone conduction headphones") — used to
    # reject NON-ADJACENT category repeats that _destutter can't see: when a
    # use_case/format slug already carries the product-type noun (audio's
    # product-type leaks into the use_case class), a shape like
    # "{format} {category} {format2} {use_case}" yields
    # "bluetooth 5.3 bone conduction headphones open-ear structure bone conduction
    # headphones". Adjacent-stutter collapse misses it; the token count catches it.
    _cat_head = (category.split()[-1] if category.split() else category).lower()

    def add(query: str, basis: Iterable[str], weight: float) -> None:
        query = _destutter_query(query)
        if not _query_has_category(query, category):
            return
        if _cat_head and len(_cat_head) >= 3:
            toks = re.findall(r"[a-z0-9]+", query.lower())
            if toks.count(_cat_head) > 1:
                # The category noun appears twice — a slot re-introduced it. Drop
                # rather than probe an awkward "…headphones … headphones" query;
                # the generator produces plenty of clean variants to fill from.
                return
        item = _candidate(safe_graph, query, basis, weight)
        if item:
            candidates.append(item)

    primary_format = formats[0] if formats else ""
    plural_format = _plural_format(primary_format) if primary_format else ""
    primary_constraint = constraints[0] if constraints else ""

    for constraint in constraints[:3]:
        add(f"{constraint} {category}", [constraint, category], 0.9)
        if plural_format:
            add(
                f"{constraint} {category} {plural_format}",
                [constraint, category, primary_format],
                0.95,
            )
            for use_case in use_cases[:2]:
                if use_case == "travel":
                    continue
                add(
                    f"{constraint} {category} {plural_format} {use_case}",
                    [constraint, category, primary_format, use_case],
                    0.98,
                )
        else:
            add(f"{constraint} {category}", [constraint, category], 0.86)

    for geography in geographies[:2]:
        geo = _geo_for_query(geography)
        if primary_constraint and plural_format:
            add(
                f"{primary_constraint} {geo} {category} {plural_format}",
                [primary_constraint, geography, category, primary_format],
                0.94,
            )
        elif plural_format:
            add(
                f"{geo} {category} {plural_format}",
                [geography, category, primary_format],
                0.87,
            )

    for exclusion in exclusions[:3]:
        add(f"{exclusion} {category}", [exclusion, category], 0.9)
        if primary_format:
            lane_use_cases = use_cases[:2]
            if exclusion == "no water" and "travel" in use_cases:
                lane_use_cases = ["travel"]
            for use_case in lane_use_cases:
                add(
                    f"{category} {primary_format} {exclusion} {use_case}",
                    [category, primary_format, exclusion, use_case],
                    0.97,
                )
        for audience in audiences[:2]:
            add(
                f"{exclusion} {category} for {audience}",
                [exclusion, category, audience],
                0.89,
            )
        if plural_format:
            add(
                f"{exclusion} {category} {plural_format}",
                [exclusion, category, primary_format],
                0.84,
            )

    for audience in audiences[:3]:
        for use_case in use_cases[:3]:
            if audience == use_case:
                # "sensitive skin" is both an audience and a use_case; pairing it
                # with itself stutters ("...for sensitive skin sensitive skin").
                continue
            add(
                f"{category} for {audience} {use_case}",
                [category, audience, use_case],
                0.9,
            )

    for use_case in use_cases[:3]:
        if plural_format:
            add(
                f"{use_case} {category} {plural_format}",
                [use_case, category, primary_format],
                0.85,
            )

    # Scenario/occasion shapes (plan P2): the buy-trigger demand. Pool = the
    # scenario class (direct + inferred) plus legacy use_case scenarios. The
    # "scenario:<slug>" basis marker is the P0 coverage signal downstream.
    scenario_pool: List[str] = list(classes.get("scenario") or [])
    for legacy in use_cases:
        if legacy in _USE_CASE_SCENARIO_SLUGS and legacy not in scenario_pool:
            scenario_pool.append(legacy)
    for slug in scenario_pool[:4]:
        phrase = _SCENARIO_PHRASES.get(slug) or slug.replace("-", " ")
        marker = f"scenario:{slug}"
        add(f"best {category} for {phrase}", [marker, category], 0.9)
        add(f"{category} for {phrase}", [marker, category], 0.86)
        if slug in _PACKABLE_SCENARIOS:
            add(
                f"what {category} to pack for {phrase}",
                [marker, category],
                0.88,
            )

    benefit = _benefit_for_category(category)
    for ingredient in ingredients[:4]:
        add(f"{ingredient} {category}", [ingredient, category], 0.88)
        if plural_format:
            category_text = "" if category in ingredient else f" {category}"
            add(
                f"{ingredient}{category_text} {plural_format}",
                [ingredient, category, primary_format],
                0.91,
            )
        if benefit:
            query = (
                f"{ingredient} {benefit}"
                if category in ingredient
                else f"{ingredient} {category} {benefit}"
            )
            add(query, [ingredient, category], 0.83)

    for proof in proofs[:3]:
        add(f"{proof} {category}", [proof, category], 0.82)

    # #1503 chooser fallback: every shape above stacks the category with a
    # SECONDARY evidenced attribute, so a category-only graph (thin beauty SKU,
    # any non-beauty vertical whose attributes the beauty lexicons can't see)
    # produced ZERO candidates and suggested_prompts stayed empty. When the
    # attribute shapes yielded nothing, emit a small deterministic chooser set —
    # generic buyer-decision queries any category supports. Low intent_weight
    # ranks them below every attribute-stacked shape; the probed-set subtraction
    # upstream still removes any of these the audit already ran. No behavior
    # change for SKUs with a single attribute-stacked candidate or more.
    if not candidates:
        for query, weight in (
            (f"best {category}", 0.40),
            (f"how to choose a {category}", 0.38),
            (f"{category} buying guide", 0.36),
            (f"best {category} for beginners", 0.34),
        ):
            add(query, [category], weight)

    deduped: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        query = item["query"]
        existing = deduped.get(query)
        if not existing or item["intent_weight"] > existing["intent_weight"]:
            deduped[query] = item

    ordered = sorted(
        deduped.values(),
        key=lambda item: (
            -float(item.get("intent_weight") or 0),
            -len(item.get("attribute_basis") or []),
            item.get("query") or "",
        ),
    )
    return ordered[:target]
