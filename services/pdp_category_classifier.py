"""PDP category classifier shared by:

- scripts/backfill_pdp_category_path.py (Phase 2 — populate catalog_products.category_path)
- services/pivot_query_service.py recall path (Phase 2b — bias recall toward
  category_path matches when the query is a category alias)

Patterns ported from PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
BEAUTY_CATEGORY_PATTERNS, augmented with explicit taxonomy paths.

DRY rule: the patterns live HERE. Both the backfill script and the search
path import from this module. Adding/removing a pattern updates everywhere
at once.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_SUNSCREEN_RE = re.compile(
    r"\b(sunscreen|sun\s*screen|broad\s+spectrum|spf\s*\d{2,3}\+?|pa\s*\+{2,4}|"
    r"sun\s+(?:serum|fluid|cream|gel|milk|stick)|"
    r"uv\s*(?:protection|shield|defen[cs]e|lock))\b",
    re.IGNORECASE,
)

# (category_label, taxonomy_path, regex). Order matters — more specific
# patterns appear earlier; the first match wins.
CATEGORY_PATTERNS: List[Tuple[str, str, "re.Pattern[str]"]] = [
    # ----- Electronics: camera drones (electronics_drone sub-vertical) -----
    # First so a "self-flying camera" / "camera drone" gets an electronics path
    # instead of falling through to a beauty/fashion rule or NULL. Tokens are
    # drone-specific (no bare "camera"), so no beauty SKU is miscategorized.
    ("Camera Drone", "electronics/drones/camera-drone", re.compile(
        r"\b(drones?|quadcopters?|fpv\s+drone|uav|self[-\s]?flying\s+camera|"
        r"flying\s+camera|camera\s+drone|follow[-\s]?me\s+drone)\b",
        re.IGNORECASE)),
    # ===== Beauty DEVICE family (beauty_device_* sub-profiles). =====
    # Own beauty/devices/* subtree, distinct from beauty/tools/* (makeup
    # applicators) and topical beauty/skincare|haircare. All are BEFORE the makeup
    # "Brush" / generic "Hair Care" / "Mask" patterns so a heat/energy/light
    # device is a DEVICE, not a topical mask/brush. Each has a leading negative
    # lookahead for topical FORM nouns so a formulation that merely names a tool
    # ("hair removal cream", "led-boosting serum", "flat iron spray") falls through
    # to its real topical pattern. hair-removal + skincare FIRST because they carry
    # "hair"/"mask"/"facial" that lower topical patterns would otherwise grab.
    ("Hair Removal Device", "beauty/devices/hair-removal", re.compile(
        r"^(?!.*\b(?:cream|wax|gel|foam|lotion|spray|serum|mousse|strips?)\b)"
        r".*\b(ipl(?:\s+hair\s+removal)?|laser\s+hair\s+removal|"
        r"hair\s+removal\s+(?:device|handset|system)|epilator)\b",
        re.IGNORECASE)),
    ("Skincare Device", "beauty/devices/skincare-energy", re.compile(
        r"^(?!.*\b(?:sheet\s+mask|cream|serum|ampoule|essence|toner|spray)\b)"
        r".*\b(led\s+(?:face\s+)?mask|light\s+therapy\s+(?:mask|device)|"
        r"red\s+light\s+therapy|microcurrent|nanocurrent|"
        r"radio\s?frequency\s+(?:device|wand)|high[-\s]?frequency\s+wand|"
        r"microdermabrasion|derma\s?roller|microneedl\w*|"
        r"skin\s+tightening\s+device|facial\s+toning\s+device)\b",
        re.IGNORECASE)),
    ("Facial Cleansing Device", "beauty/devices/facial-cleansing", re.compile(
        r"^(?!.*\b(?:cream|gel|foam|oil|balm|milk|powder)\b)"
        r".*\b(facial\s+cleansing\s+brush|cleansing\s+brush|"
        r"sonic\s+(?:facial\s+)?cleanser|facial\s+cleansing\s+device)\b",
        re.IGNORECASE)),
    ("Nail Device", "beauty/devices/nail", re.compile(
        r"\b(uv[-\s/]?led\s+(?:nail\s+)?lamp|nail\s+lamp|gel\s+lamp|nail\s+dryer|"
        r"electric\s+nail\s+file|nail\s+drill)\b",
        re.IGNORECASE)),
    # Hair-styling tools (VODANA cross-category, beauty x 3C). Negative lookahead
    # vetoes "flat iron spray" / "blow dry primer" / "curl styler cream"; no bare
    # "brush"/"hair"/"styler"; bare "straightener" survives only under the veto;
    # "blow dry" requires "dryer"/"brush"; "flat[\s-]+iron" so "flatiron" (place
    # names) does not match.
    ("Hair Styling Tool", "beauty/devices/hair-styling", re.compile(
        r"^(?!.*\b(?:spray|serum|balm|primer|cream|gel|paste|mist|lotion|mousse|"
        r"wax|pomade|treatment|oil|protectant|pencil|mascara|essence|ampoule)\b)"
        r".*\b(flat[\s-]+iron|hair\s+straightener|straightening\s+brush|"
        r"curling\s+(?:iron|wand|brush)|hair\s+curler|hair\s+dryer|"
        r"blow[-\s]?dryer|blow[-\s]?dry\s+brush|hot\s+air\s+brush|hot\s+brush|"
        r"air\s+styler|hair\s+styler|styling\s+(?:iron|wand)|"
        r"hair\s+styling\s+tool|straightener|"
        r"hair\s+waver|wave\s+iron|deep\s+waver|beachwaver|waver)\b",
        re.IGNORECASE)),
    ("Makeup Sponge", "beauty/tools/sponge", re.compile(
        r"\b(makeup sponge|beauty sponge|sponge\s*/\s*puff|powder puff|blender sponge)\b",
        re.IGNORECASE)),
    ("Brush Pouch", "beauty/tools/brush-accessory", re.compile(
        r"\b(brush bag|brush pouch|brush case|brush holder|brush roll)\b",
        re.IGNORECASE)),
    ("Brush", "beauty/tools/brush", re.compile(
        r"\b(brush|makeup brush|foundation brush|powder brush|blush brush|shader brush|kabuki)\b",
        re.IGNORECASE)),
    ("Shampoo", "beauty/haircare/shampoo", re.compile(
        r"\b(shampoo|dry shampoo|clarifying shampoo)\b", re.IGNORECASE)),
    ("Conditioner", "beauty/haircare/conditioner", re.compile(
        r"\b(conditioner|deep conditioner|leave-in conditioner|leave in conditioner)\b",
        re.IGNORECASE)),
    ("Hair Styling", "beauty/haircare/styling", re.compile(
        r"\b(edge control|styling gel|hair-thickening|hair thickening|"
        r"detangling spray|hair clip|hair clips|edge styling|slick[-\s]?back|"
        r"styling essentials)\b",
        re.IGNORECASE)),
    ("Hair Care", "beauty/haircare/general", re.compile(
        r"\b(hair care|hair repair|repair bundle|maintenance crew|"
        r"detangling|leave-in|leave in|hair|scalp)\b",
        re.IGNORECASE)),
    ("Sunscreen", "beauty/skincare/sun/sunscreen", _SUNSCREEN_RE),
    ("Fragrance", "beauty/fragrance/perfume", re.compile(
        r"\b(perfume|parfum|extrait|extract|eau de parfum|eau de toilette|cologne|body spray|scent)\b|"
        r"\bfragrance\b(?![-\s]?free)\b",
        re.IGNORECASE)),
    ("Cleanser", "beauty/skincare/cleanse/cleanser", re.compile(
        r"\b(cleanser|cleansing|face wash|facial wash|"
        r"cleansing milk|cleansing foam|cleansing gel|face wipes?|cleansing wipes?|wipes?|wash)\b",
        re.IGNORECASE)),
    ("Toner", "beauty/skincare/treat/toner", re.compile(
        r"\b(toner|tonic|mist|pad|skin booster)\b", re.IGNORECASE)),
    ("Mask", "beauty/skincare/treat/mask", re.compile(
        r"\b(face mask|clay mask|charcoal mask|sheet mask|gel mask|sleeping mask|"
        r"sleep mask|wash[-\s]?off mask|under eye patch|eye patch|pimple patch|"
        r"spot cover patch|spot patch|patchs|patches|lip\s?patch|mask)\b",
        re.IGNORECASE)),
    ("Exfoliant", "beauty/skincare/treat/exfoliant", re.compile(
        r"\b(exfoliant|exfoliating|exfoliation|peel|peeling|peeling gel|peel pads?|"
        r"scrub|polish)\b",
        re.IGNORECASE)),
    ("Treatment", "beauty/skincare/treat/treatment", re.compile(
        r"\b(spot[-\s]?target(?:ing|ed)?|spot[-\s]?treatment|blemish|acne|"
        r"clarifying treatment|targeting gel|treatment gel|spot stickers?|"
        r"pimple stickers?|acne stickers?|azelaic acid|vitamin c duo|"
        r"retinol youth renewal|retinol treatment)\b",
        re.IGNORECASE)),
    ("Face Oil", "beauty/skincare/moisturize/oil", re.compile(
        r"\b(face oils?|facial oils?|body oil|essential oil|oil drops?)\b",
        re.IGNORECASE)),
    ("Serum", "beauty/skincare/treat/serum", re.compile(
        r"\b(serum|essence|ampoule|concentrate)\b", re.IGNORECASE)),
    ("Tanning", "beauty/body/tanning", re.compile(
        r"\b(self[-\s]?tan|self[-\s]?tanning|sunless tan|gradual tanning|gradualglow)\b",
        re.IGNORECASE)),
    ("Primer", "beauty/makeup/face/primer", re.compile(
        r"\b(primer|pore prep|pore[-\s]?filling)\b", re.IGNORECASE)),
    ("Concealer", "beauty/makeup/face/concealer", re.compile(
        r"\b(concealer|corrector|correcting skinstick|skinstick|skin stick|"
        r"eye brightener|bright fix)\b",
        re.IGNORECASE)),
    ("Foundation", "beauty/makeup/face/foundation", re.compile(
        r"\b(foundation|skin tint|tint stick|foundation stick|cushion foundation)\b",
        re.IGNORECASE)),
    ("Powder", "beauty/makeup/face/powder", re.compile(
        r"\b(powder|setting powder|pressed powder|loose powder|"
        r"blurring powder|finishing powder)\b",
        re.IGNORECASE)),
    ("Highlighter", "beauty/makeup/face/highlighter", re.compile(
        r"\b(highlighter|illuminator|luminizer|luminiser|killawatt|diamond bomb|"
        r"glow drops)\b",
        re.IGNORECASE)),
    ("Blush", "beauty/makeup/face/blush", re.compile(
        r"\b(blush|cheeks out|cheek tint|flush)\b", re.IGNORECASE)),
    ("Bronzer", "beauty/makeup/face/bronzer", re.compile(
        r"\b(bronzer|contour)\b", re.IGNORECASE)),
    ("Eyeshadow", "beauty/makeup/eye/eyeshadow", re.compile(
        r"\b(eye\s?shadow|eyeshadow|eye color|eye colour)\b", re.IGNORECASE)),
    ("Eyeliner", "beauty/makeup/eye/eyeliner", re.compile(
        r"\b(eyeliner|eye liner|liquid liner|pencil liner|flypencil)\b",
        re.IGNORECASE)),
    ("Mascara", "beauty/makeup/eye/mascara", re.compile(
        r"\b(mascara)\b", re.IGNORECASE)),
    ("Brow Pencil", "beauty/makeup/eye/brow", re.compile(
        r"\b(brow pencil|eyebrow pencil|brow definer|brow sculptor|brow styler)\b",
        re.IGNORECASE)),
    ("Lip Balm", "beauty/makeup/lip/balm", re.compile(
        r"\b(lip balm|lip butter|lip treatment|lip care|lip serum|lipserum|"
        r"nightbalm|lip scrub|scrubstick)\b",
        re.IGNORECASE)),
    ("Lipstick", "beauty/makeup/lip/lipstick", re.compile(
        # `lip[\s-]*stick` matches "lipstick", "lip stick", "lip-stick",
        # and double-space variants. User typos like "lip stick" were
        # silently classifying as None and falling back to a generic
        # skincare term list, returning serums/cleansers for lipstick
        # queries. See lipstick-recall regression 2026-05-09.
        r"\b(lip[\s-]*stick|lip color|lip colour|liquid lip|lip luxe|lip lacquer|"
        r"lip gloss|lip oil|lip liner|lip stain|lip tint|pout lip|gloss luxe|"
        r"gloss drip|gloss bomb|gloss stick|gloss stix|lip combo|lip duo)\b",
        re.IGNORECASE)),
    ("Moisturizer", "beauty/skincare/moisturize/cream", re.compile(
        r"\b(moisturizer|moisturiser|cream|lotion|gel cream|gel-cream|"
        r"water gel|barrier cream)\b",
        re.IGNORECASE)),
    ("Body Care", "beauty/body/care", re.compile(
        r"\b(body milk|body relief|body essentials|body care|hand care|loofah)\b",
        re.IGNORECASE)),
    # ----- Phase O-5b: fashion / apparel patterns -----
    # Order: more specific (sweater, hoodie, dress) above the broad "apparel".
    # Pet apparel is included intentionally because some active fashion
    # merchants sell dog/cat clothing, and the LLM extractor's category
    # gate filters on category_path starting with 'fashion/' or 'apparel/'.
    ("Sweater", "fashion/apparel/tops/sweater", re.compile(
        r"\b(sweater|knit(?:ted)?\s+sweater|knit\s+top|cardigan|pullover|jumper)\b",
        re.IGNORECASE)),
    ("Hoodie", "fashion/apparel/tops/hoodie", re.compile(
        r"\b(hoodie|sweatshirt|zip[-\s]?up|pullover\s+hoodie)\b",
        re.IGNORECASE)),
    ("T-Shirt", "fashion/apparel/tops/tshirt", re.compile(
        r"\b(t[-\s]?shirt|tee\b|tank\s+top|long[-\s]?sleeve\s+tee|graphic\s+tee)\b",
        re.IGNORECASE)),
    ("Shirt", "fashion/apparel/tops/shirt", re.compile(
        r"\b(button[-\s]?up|button[-\s]?down|dress\s+shirt|blouse|polo\s+shirt)\b",
        re.IGNORECASE)),
    ("Dress", "fashion/apparel/dresses", re.compile(
        r"\b(dress|gown|sundress|maxi\s+dress|midi\s+dress|cocktail\s+dress)\b",
        re.IGNORECASE)),
    ("Skirt", "fashion/apparel/bottoms/skirt", re.compile(
        r"\b(skirt|mini\s+skirt|midi\s+skirt|maxi\s+skirt|pencil\s+skirt)\b",
        re.IGNORECASE)),
    ("Pants", "fashion/apparel/bottoms/pants", re.compile(
        r"\b(pants|trousers|chinos|slacks|joggers?\s+pants|joggers\b|cargo\s+pants|leggings)\b",
        re.IGNORECASE)),
    ("Jeans", "fashion/apparel/bottoms/jeans", re.compile(
        r"\b(jeans|denim|skinny\s+jeans|straight\s+leg|boot[-\s]?cut)\b",
        re.IGNORECASE)),
    ("Shorts", "fashion/apparel/bottoms/shorts", re.compile(
        r"\b(shorts|bermuda\s+shorts|denim\s+shorts|athletic\s+shorts)\b",
        re.IGNORECASE)),
    ("Coat", "fashion/apparel/outerwear/coat", re.compile(
        r"\b(coat|overcoat|trench\s+coat|peacoat|parka)\b",
        re.IGNORECASE)),
    ("Jacket", "fashion/apparel/outerwear/jacket", re.compile(
        r"\b(jacket|blazer|bomber|denim\s+jacket|windbreaker|puffer\s+jacket)\b",
        re.IGNORECASE)),
    ("Vest", "fashion/apparel/outerwear/vest", re.compile(
        r"\b(vest|gilet|puffer\s+vest|padded\s+vest|down\s+vest)\b",
        re.IGNORECASE)),
    ("Base Layer", "fashion/apparel/base-layer", re.compile(
        r"\b(base\s+layer|baselayer|thermal\s+(?:top|bottom|underwear|set))\b",
        re.IGNORECASE)),
    ("Lingerie", "fashion/apparel/intimates/lingerie", re.compile(
        r"\b(lingerie|bra\b|panty|panties|underwear|brief|boy[-\s]?short|push[-\s]?up)\b",
        re.IGNORECASE)),
    ("Swimwear", "fashion/apparel/swimwear", re.compile(
        r"\b(swimwear|swimsuit|bikini|one[-\s]?piece\s+swim|board\s+shorts|trunks)\b",
        re.IGNORECASE)),
    ("Activewear", "fashion/apparel/activewear", re.compile(
        r"\b(activewear|sportswear|yoga\s+pants|workout\s+(?:top|tee|set))\b",
        re.IGNORECASE)),
    ("Sleepwear", "fashion/apparel/sleepwear", re.compile(
        r"\b(sleepwear|pajamas|pyjamas|nightgown|robe\b|loungewear)\b",
        re.IGNORECASE)),
    ("Shoes", "fashion/shoes", re.compile(
        r"\b(shoes\b|sneakers|loafers|heels|boots|sandals|flats\b|oxfords|mules)\b",
        re.IGNORECASE)),
    ("Bag", "fashion/accessories/bag", re.compile(
        r"\b(handbag|tote\b|backpack|crossbody|clutch|satchel|messenger\s+bag)\b",
        re.IGNORECASE)),
    ("Jewelry", "fashion/accessories/jewelry", re.compile(
        r"\b(jewelry|necklace|earring|bracelet|ring\b|pendant|brooch)\b",
        re.IGNORECASE)),
    ("Hat", "fashion/accessories/hat", re.compile(
        r"\b(hat|cap\b|beanie|fedora|baseball\s+cap|bucket\s+hat)\b",
        re.IGNORECASE)),
    ("Scarf", "fashion/accessories/scarf", re.compile(
        r"\b(scarf|shawl|wrap\b|stole\b)\b",
        re.IGNORECASE)),
    # Pet apparel — narrower-but-still-clothing for products explicitly
    # framed as pet wear (PawStyle catalog). Sits AFTER human apparel
    # patterns so a "dog sweater" matches Sweater first (the path tree
    # uses fashion/apparel/* either way, so category_kind=fashion).
    ("Pet Apparel", "fashion/apparel/pet", re.compile(
        r"\b(pet\s+(?:apparel|wear|clothing|sweater|coat|jacket|outfit|overalls?|onesies?)|"
        r"dog\s+(?:sweater|jacket|coat|hoodie|outfit|overalls?|onesies?)|"
        r"cat\s+(?:sweater|outfit|onesies?)|"
        r"\d-leg\s+(?:onesies?|base\s+layer))\b",
        re.IGNORECASE)),
    # Pet accessories — non-clothing pet gear (harness/leash/collar/etc.).
    # Distinct fashion/accessories/pet path so the catalog can tell
    # "pet apparel" from "pet accessory" without one being a parent of
    # the other.
    ("Pet Accessory", "fashion/accessories/pet", re.compile(
        r"\b((?:dog|cat|pet)\s+(?:harness|leash|collar|bandana|bow\s*tie|tag|carrier)|"
        r"tactical\s+(?:dog|cat|pet)\s+harness|"
        r"retractable\s+(?:dog|cat|pet)\s+leash)\b",
        re.IGNORECASE)),
    # Generic apparel/clothing fallback — last so specific patterns win.
    ("Apparel", "fashion/apparel/general", re.compile(
        r"\b(apparel|clothing|garment|womenswear|menswear|kidswear)\b",
        re.IGNORECASE)),
    # ----- Electronics patterns -----
    # Keyword-matchable subset only. Model-number-only products (WH-1000XM5,
    # AirPods, etc.) have no keyword signal and go to the LLM backfill path.
    ("Headphones", "electronics/audio/headphones", re.compile(
        r"\b(headphones|over[-\s]?ear\s+headphones|on[-\s]?ear\s+headphones|"
        r"wireless\s+headphones|noise[-\s]?cancell?ing\s+headphones)\b",
        re.IGNORECASE)),
    ("Earbuds", "electronics/audio/earbuds", re.compile(
        r"\b(earbuds|ear\s+buds|true\s+wireless\s+earbuds|wireless\s+earbuds|"
        r"in[-\s]?ear\s+(?:headphones|earphones))\b",
        re.IGNORECASE)),
    ("E-Reader", "electronics/ereader", re.compile(
        r"\b(e[-\s]?reader|ebook\s+reader|e[-\s]?book\s+reader)\b",
        re.IGNORECASE)),
    ("Bluetooth Speaker", "electronics/audio/speaker", re.compile(
        r"\b(bluetooth\s+speaker|wireless\s+speaker|portable\s+speaker|smart\s+speaker)\b",
        re.IGNORECASE)),
    ("Gift Set", "beauty/sets/gift-set", re.compile(
        r"\b(skincare set|skin care set|gift set|holiday edition|routine|bundle|"
        r"essentials set|essentials|care set|duo|kit|collection|set)\b",
        re.IGNORECASE)),
]


def classify(text: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (category_label, category_path) on first matching pattern, else None."""
    if not text:
        return None
    for label, path, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return (label, path)
    return None


def resolve_path_from_row(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Try category, product_type, title in priority order. Used by the backfill."""
    for candidate in (category, product_type, title):
        hit = classify(candidate)
        if hit is not None:
            return hit
    return None


# Provenance enum values written to catalog_products.category_label_source.
# Confidence defaults are documented in
# ~/.claude/plans/let-s-build-a-full-breezy-taco.md.
CATEGORY_SOURCE_MERCHANT = "merchant_payload"
CATEGORY_SOURCE_VARIANT = "variant_aggregate"

CATEGORY_CONFIDENCE_MERCHANT = 1.0
CATEGORY_CONFIDENCE_VARIANT = 0.85


def fold_category_from_variants(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
    variants: Optional[list] = None,
) -> Optional[Tuple[Tuple[str, str], str, float]]:
    """Resolve (label, path) plus provenance from product-level fields,
    falling back to variant-level fields when product-level misses.

    Variants can be StandardProductVariant objects OR plain dicts (raw
    Shopify payload). For dicts, we look at:
      - top-level keys: category / product_type / title
      - platform_metadata: category / product_type
    For StandardProductVariant objects, we look at title and
    platform_metadata.get("category") / platform_metadata.get("product_type").

    Returns ((label, path), source, confidence) or None.
    """
    hit = resolve_path_from_row(category=category, product_type=product_type, title=title)
    if hit is not None:
        return (hit, CATEGORY_SOURCE_MERCHANT, CATEGORY_CONFIDENCE_MERCHANT)
    for variant in variants or []:
        v_category = _variant_field(variant, "category")
        v_product_type = _variant_field(variant, "product_type")
        v_title = _variant_field(variant, "title")
        v_hit = resolve_path_from_row(
            category=v_category, product_type=v_product_type, title=v_title,
        )
        if v_hit is not None:
            return (v_hit, CATEGORY_SOURCE_VARIANT, CATEGORY_CONFIDENCE_VARIANT)
    return None


async def fold_category_with_llm_fallback(
    *,
    merchant_id: Optional[str] = None,
    category: Optional[str] = None,
    product_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    variants: Optional[list] = None,
) -> Optional[Tuple[Tuple[str, str], str, float]]:
    """Same return shape as fold_category_from_variants, but when the
    regex+variant fallback returns None AND the LLM_CATEGORY_CLASSIFIER_ENABLED
    flag is on, fires services.category_classifier_llm.classify_via_llm
    as a final fallback. Source on hit: 'llm_category_v1'.

    Stays async so caller can `await`. Regex hits still complete
    synchronously (no LLM call) — the LLM only runs on the long tail.
    """
    regex_hit = fold_category_from_variants(
        category=category, product_type=product_type, title=title, variants=variants,
    )
    if regex_hit is not None:
        return regex_hit
    # Local import — keeps the heavy httpx+settings deps out of the
    # sync regex path. Anyone importing fold_category_from_variants
    # gets no LLM dependency.
    from services.category_classifier_llm import classify_via_llm, CATEGORY_SOURCE_LLM
    llm = await classify_via_llm(
        merchant_id=merchant_id, category=category,
        product_type=product_type, title=title, description=description,
    )
    if llm is None:
        return None
    label, path, confidence = llm
    return ((label, path), CATEGORY_SOURCE_LLM, confidence)


def _variant_field(variant, key: str) -> Optional[str]:
    """Read a field from a variant that might be a dict OR a pydantic model."""
    if variant is None:
        return None
    # dict path (raw Shopify payload before model parsing)
    if isinstance(variant, dict):
        direct = variant.get(key)
        if direct:
            return str(direct)
        meta = variant.get("platform_metadata") or {}
        if isinstance(meta, dict):
            nested = meta.get(key)
            if nested:
                return str(nested)
        return None
    # pydantic model path (StandardProductVariant). title is always present;
    # category/product_type live in platform_metadata when Shopify carries them.
    if key == "title":
        title = getattr(variant, "title", None)
        return str(title) if title else None
    meta = getattr(variant, "platform_metadata", None) or {}
    if isinstance(meta, dict):
        nested = meta.get(key)
        if nested:
            return str(nested)
    return None


def category_path_prefix_for_query(query: Optional[str]) -> Optional[str]:
    """Used by the recall path: when the user query matches a known category,
    return a 3-segment prefix like 'beauty/makeup/lip/' so the SQL can do
    `WHERE category_path LIKE :prefix || '%'`. Returning None means the
    query does NOT match a known category and recall should fall back to
    the existing trigram text scan.

    Example: 'lipstick' → 'beauty/makeup/lip/' (matches 'beauty/makeup/lip/lipstick'
    AND 'beauty/makeup/lip/balm' so users see both lipstick and lip balm
    rows on a generic 'lipstick' search). Adjust the slice depth if a more
    precise match is desired.
    """
    hit = classify(query)
    if hit is None:
        return None
    _, path = hit
    # Slice to category-parent level (drop the final segment).
    parts = path.rsplit("/", 1)
    if len(parts) <= 1:
        return path + "/"
    return parts[0] + "/"
