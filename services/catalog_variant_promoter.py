"""Promote embedded variant data into catalog_skus rows.

Stage 2b-ii of the PDP architecture roadmap (plans/rosy-mixing-bengio.md).
Solves a class of bug surfaced by Stage 2b-i's prod telemetry:

  - Tom Ford's "Architecture Radiance Foundation" had 43 catalog_products
    rows clustered under one product_group. Each row carries a
    seed_data.snapshot.variants array with the SAME 40 shade variants.
    Pre-2b-ii: agent UI saw 43 catalog_skus rows (the synthetic
    `<product_key>::canonical` SKU per catalog_products row), all
    titled "Architecture Radiance Foundation" — no shade selector
    surfaced. The 40 shades existed in jsonb but never reached the
    SKU layer.

Stage 2b-ii fix: for the PRIMARY catalog_products row of each multi-
member product_group, read the embedded variants array, filter out
Shopify "Default Title" single-variant placeholders, and upsert one
catalog_skus row per real variant. After this:

  - Tom Ford product_group has 40 catalog_skus rows representing the
    40 shades (sku_key = `<primary_product_key>::v::<variant_id>`)
  - Agent UI (and Stage 3's agent_pdp_view) can render shade swatches
    under one canonical PDP — same UX as Sephora / Ulta / Tom Ford's
    own site

What this service does NOT do:
  - Touch catalog_products. Identity stays where it is.
  - Touch seed_data / product_payload. Variants are already there —
    we just project them to a separate table.
  - Apply to non-primary group members. The primary's variants are
    representative of the whole group (verified empirically: the 43
    Tom Ford rows have IDENTICAL variants arrays). Re-running the
    extractor on every member would just produce duplicate upserts;
    the unique index on (merchant_id, platform, product_key,
    source_variant_id) would dedup but it's wasted work.
  - Touch Shopify Default-Title placeholders. MOYU 26-Foundation-Brush
    case: each row has one variant with title='Default Title' / options
    'Default Title'. Already correctly modeled by Stage 2b-i as
    catalog-level entries. No SKU promotion needed.

Variant data shape (real-world examples):
  Path B (external_seed mirror): seed_data.snapshot.variants is an
  array of dicts with shape:
    {
      "sku": "TCT117",
      "price": "95.00",
      "title": "8.5N Vellum / 30.0 ml",
      "options": [
        {"name": "Shade", "value": "8.5N Vellum", "axis_kind": "shade"},
        {"name": "Size", "value": "30.0 ml", "axis_kind": "size"}
      ],
      "variant_id": "53059916267733",
      "image_url": "...",
      "barcode": "...",
      "currency": "USD"
    }

  Path A (Shopify sync): catalog_products.product_payload['variants']
  has the StandardProduct shape:
    {
      "id": "53012671693097",
      "variant_id": "53012671693097",
      "sku": "...",
      "title": "Default Title",
      "options": {"Title": "Default Title"},
      "price": "...",
      ...
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from db.database import database
from services.variant_identity import variant_id_provenance

logger = logging.getLogger(__name__)


# A variant whose title is "Default Title" or whose options carry only
# "Default Title" / "Default" values is a Shopify placeholder for a
# single-variant product. Don't promote — agent UI shouldn't render a
# shade selector with one swatch labeled "Default Title".
_SHOPIFY_DEFAULT_TITLES = ("default title", "default", "")


@dataclass
class VariantRow:
    """One variant ready to upsert into catalog_skus."""

    sku_key: str
    product_key: str
    merchant_id: str
    platform: str
    source_product_id: str
    source_variant_id: str
    sku: Optional[str]
    barcode: Optional[str]
    title: str
    currency: Optional[str]
    image_url: Optional[str]
    visible_option_labels: List[str]
    visible_attributes: Dict[str, str]
    sku_payload: Dict[str, Any]


@dataclass
class GroupOutcome:
    product_group_id: str
    primary_product_key: str
    variants_found: int
    #: Rows written on a lane where the write can actually PROMOTE — the money
    #: lane. See `variants_tier_held` for what this deliberately excludes.
    variants_promoted: int
    #: Rows written whose `readiness_tier` did NOT move and could not have.
    #:
    #: On the redirect lane `promoted_readiness_tier` offers 'referral_only', the
    #: FLOOR of `index_graduation_ladder.OBSERVED_READINESS_LADDER`, so
    #: `UPSERT_SKU_SQL`'s upward-only CASE is a no-op by construction: an existing
    #: row keeps whatever tier it had, and an INSERT mints the floor. Counting
    #: those in `variants_promoted` is the same lie the INSERT-only
    #: `readiness_tier` told before this PR — "promoted" has to mean the tier moved
    #: or the lane could move it. The content re-projection still lands (title, sku,
    #: options, payload merge); this counter says so without claiming a promotion.
    variants_tier_held: int = 0
    skipped_reason: Optional[str] = None
    sample_variant_titles: List[str] = field(default_factory=list)
    #: Rows refused by the OTHER unique constraint — same sku_key, different
    #: identity tuple. Counted rather than fatal; see the upsert loop.
    #:
    #: NOT A HEALTHY ZERO, AND NOT A SELF-CLEARING NUMBER. This promoter has no
    #: adoption or heal arbiter of the kind `apply._adopt_existing_sku_identities`
    #: gives the ingest lane. A variant whose IDENTITY is free but whose derived
    #: `sku_key` is already held under a different tuple is refused here on this run
    #: and on every run after it, for ever, because nothing in this file ever
    #: reconciles the planned key with the row that holds it. Read this counter as
    #: "variants this lane can never write", not as "variants that failed once".
    skus_identity_conflict: int = 0
    #: Variants dropped because an EARLIER variant in the same group already bound
    #: to this identity — two merchant ids sharing a 128-char prefix. One stored row
    #: either way; this says how many shades that cost.
    skus_deduped_same_identity: int = 0
    #: Rows refused for any OTHER reason (22001 on an over-long id, 23502, a bad
    #: payload). Counted rather than fatal too: a re-raise here aborted every group
    #: still queued behind this one.
    skus_write_failed: int = 0


@dataclass
class PromoterReport:
    groups_considered: int = 0
    groups_promoted: int = 0
    groups_skipped_no_real_variants: int = 0
    groups_skipped_no_primary: int = 0
    skus_upserted_total: int = 0
    #: See `GroupOutcome.variants_tier_held`. Rows written on a lane whose offered
    #: tier is the ladder floor, so nothing was promoted. NOT included in
    #: `skus_upserted_total`: read the two together to get rows written.
    skus_tier_held_total: int = 0
    #: See `GroupOutcome.skus_identity_conflict` — these are permanent refusals, not
    #: retriable ones.
    skus_identity_conflict_total: int = 0
    skus_write_failed_total: int = 0
    skus_deduped_same_identity_total: int = 0
    per_group: List[GroupOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Variant array extraction
# ---------------------------------------------------------------------------


def _coerce_jsonb(value: Any) -> Any:
    """asyncpg returns JSONB as dict OR JSON string depending on codec.
    Same defensive coercion the writer service uses."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _extract_variants_from_seed(seed_data: Any) -> List[Dict[str, Any]]:
    """Path B / external_seed: variants live at seed_data.snapshot.variants.
    Returns [] when missing/empty/not a list."""
    sd = _coerce_jsonb(seed_data)
    if not isinstance(sd, dict):
        return []
    snapshot = sd.get("snapshot")
    if not isinstance(snapshot, dict):
        return []
    variants = snapshot.get("variants")
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, dict)]


def _extract_variants_from_payload(product_payload: Any) -> List[Dict[str, Any]]:
    """Path A / Shopify: variants live at product_payload.variants
    (StandardProduct serialization). Returns [] when missing."""
    pp = _coerce_jsonb(product_payload)
    if not isinstance(pp, dict):
        return []
    variants = pp.get("variants")
    if not isinstance(variants, list):
        return []
    return [v for v in variants if isinstance(v, dict)]


# ---------------------------------------------------------------------------
# Real-vs-default filter
# ---------------------------------------------------------------------------


def _variant_options_are_meaningful(options: Any) -> bool:
    """An options blob is "meaningful" if any of its values is NOT in
    {Default Title, Default, empty}.

    Path B shape: list of {name, value, axis_kind}.
    Path A shape: dict like {"Title": "Default Title"}.
    """
    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, dict):
                continue
            val = (opt.get("value") or "").strip().lower()
            if val and val not in _SHOPIFY_DEFAULT_TITLES:
                return True
        return False
    if isinstance(options, dict):
        for val in options.values():
            if isinstance(val, str) and val.strip().lower() not in _SHOPIFY_DEFAULT_TITLES:
                return True
        return False
    return False


def is_real_variant(variant: Dict[str, Any]) -> bool:
    """True iff this is a real shade/size/etc variant, not a Shopify
    Default-Title placeholder. The title alone isn't enough (some
    real variants have meaningful titles AND a default options dict);
    require AT LEAST ONE of: meaningful options, OR title that isn't
    a default placeholder."""
    if not isinstance(variant, dict):
        return False
    title = (variant.get("title") or "").strip().lower()
    title_is_meaningful = bool(title) and title not in _SHOPIFY_DEFAULT_TITLES
    options_are_meaningful = _variant_options_are_meaningful(variant.get("options"))
    return title_is_meaningful or options_are_meaningful


def filter_real_variants(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop Default-Title placeholders.

    This docstring used to promise a second rule — "if after filtering only one variant
    remains AND that variant's title matches the parent product title, still drop it;
    caller passes the parent title for that check" — which the body never implemented
    and the signature could not have supported (no parent title is passed). It was an
    unverified claim, and it mattered: it read as cover for exactly the case that turns
    out to need care, the lone variant whose title repeats its parent's.

    The rule is NOT reinstated, because measurement says the title is the wrong test.
    Of the 1,648 prod products in that shape on 2026-09-07, 40 carry a genuine numeric
    Shopify variant id — a real single-variant product legitimately repeats the product
    title — while the other 1,608 carry an id we minted ourselves. Title collision
    separates those two groups not at all. Provenance does, so the buyability decision
    belongs to `services.variant_identity.is_merchant_issued_variant_id` and the caller
    that spends money, not to a string comparison here.
    """
    return [v for v in variants if is_real_variant(v)]


# ---------------------------------------------------------------------------
# Build the catalog_skus row dict for upsert
# ---------------------------------------------------------------------------


#: `catalog_skus.sku_key` is varchar(255) and `catalog_skus.source_variant_id` is
#: varchar(128). A key or a variant id longer than its column does not fail one
#: row — Postgres raises 22001 (string_data_right_truncation), which is NOT a
#: unique violation, so the promoter used to re-raise it and abort the whole run
#: on the first over-long merchant variant id. `derive_product_key` alone can
#: reach 214 chars, which leaves 36 for the infix and the id.
_SKU_KEY_MAX = 255
_SOURCE_VARIANT_ID_MAX = 128
_VARIANT_INFIX = "::v::"


def _derive_sku_key(primary_product_key: str, variant_id: str) -> str:
    """Stable, debuggable: <primary_product_key>::v::<variant_id>.
    Distinct from the existing `::canonical` synthetic SKU so the two
    coexist without conflict on the catalog_skus PK.

    A key that already FITS is returned byte-for-byte unchanged — this is the
    primary key of 4,286 live rows the 2026-09-08 variant-identity backfill
    adopted and hung `catalog_offers` on (no FK to catch a rename), so the
    truncation below must be reachable ONLY by the keys that could never have
    been written in the first place. Over the limit it mirrors
    `ingestion.derive_variant_sku_key`: truncate the id, and fall back to a sha1
    digest of the FULL id when even the truncation does not fit, which keeps the
    key stable across re-runs (the whole point of deriving it from the merchant's
    own variant id)."""
    key = f"{primary_product_key}{_VARIANT_INFIX}{variant_id}"
    if len(key) <= _SKU_KEY_MAX:
        return key
    token = str(variant_id)[:60]
    budget = _SKU_KEY_MAX - len(primary_product_key) - len(_VARIANT_INFIX)
    if budget < len(token):
        digest = hashlib.sha1(str(variant_id).encode("utf-8")).hexdigest()[:16]
        token = digest if budget >= len(digest) else digest[: max(budget, 0)]
    # Last resort: a product_key that alone overruns the column. Nothing keeps a
    # variant of it distinct, but a truncated key is a row that lands and can be
    # found, where an over-long one is a 22001 that used to take the run down.
    return f"{primary_product_key}{_VARIANT_INFIX}{token}"[:_SKU_KEY_MAX]


def _visible_option_labels(options: Any) -> List[str]:
    """Flatten options to a list of display labels for the SKU's
    visible_option_labels jsonb column."""
    labels: List[str] = []
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                val = (opt.get("value") or "").strip()
                if val:
                    labels.append(val)
    elif isinstance(options, dict):
        for val in options.values():
            if isinstance(val, str) and val.strip():
                labels.append(val.strip())
    return labels


def _visible_attributes(options: Any) -> Dict[str, str]:
    """Structured options as {axis_name: value} dict. Path B's
    axis_kind is preferred over name when present (more semantic)."""
    attrs: Dict[str, str] = {}
    if isinstance(options, list):
        for opt in options:
            if isinstance(opt, dict):
                key = (opt.get("axis_kind") or opt.get("name") or "").strip().lower()
                val = (opt.get("value") or "").strip()
                if key and val:
                    attrs[key] = val
    elif isinstance(options, dict):
        for k, v in options.items():
            if isinstance(v, str) and v.strip():
                attrs[str(k).strip().lower()] = v.strip()
    return attrs


def build_variant_row(
    *, variant: Dict[str, Any], primary: Dict[str, Any]
) -> Optional[VariantRow]:
    """Assemble the catalog_skus upsert dict from one variant + the
    primary's identity. Returns None when the variant doesn't carry
    a variant_id (can't compute a stable sku_key).

    Admission is deliberately NOT gated on provenance. This promoter exists so the agent
    UI can render shade swatches (Stage 2b-ii), and a variant whose id we cannot place is
    still a real shade the buyer needs to see. What would be wrong is letting that row look
    like merchant identity downstream, so the provenance is decided here, once, and carried
    on the row so a consumer can filter on it — measured on prod 2026-09-07, 1,645 of the
    2,803 promotable products carry an id one of our own writers minted. As of that date no
    money path reads the stamp yet; the gateway's isRestatedProductId guard is what refuses
    a product-derived id at checkout."""
    raw_variant_id = str(variant.get("variant_id") or variant.get("id") or "").strip()
    if not raw_variant_id:
        return None

    # THE KEY IS DERIVED FROM THE ID WE WILL ACTUALLY STORE. `source_variant_id` is
    # varchar(128) and the upsert used to bind `variant_id[:128]` while the key came
    # off the FULL id: two ids sharing a 128-char prefix then produced ONE identity
    # tuple under TWO different sku_keys, so the second row's INSERT resolved through
    # `ON CONFLICT (merchant_id, platform, product_key, source_variant_id)` and
    # silently DO UPDATEd the first — one stored row, `promoted` counting two, and
    # the second variant's title/options overwriting the first's. Bind first, derive
    # second, and the key and the identity cannot disagree.
    variant_id = raw_variant_id[:_SOURCE_VARIANT_ID_MAX]
    sku_key = _derive_sku_key(primary["product_key"], variant_id)
    options = variant.get("options")
    payload = dict(variant)
    # Provenance is asked of the id the MERCHANT issued, not of our bound copy — a
    # truncation must not be able to turn a restated product id into a clean one.
    payload["variant_id_provenance"] = variant_id_provenance(
        raw_variant_id,
        product_id=primary.get("source_product_id"),
        product_key=primary.get("product_key"),
    )
    return VariantRow(
        sku_key=sku_key,
        product_key=primary["product_key"],
        merchant_id=primary["merchant_id"],
        platform=primary["platform"],
        source_product_id=primary["source_product_id"],
        source_variant_id=variant_id,
        sku=variant.get("sku") or None,
        barcode=variant.get("barcode") or None,
        title=str(variant.get("title") or "").strip() or "(untitled variant)",
        currency=variant.get("currency") or None,
        image_url=variant.get("image_url") or None,
        visible_option_labels=_visible_option_labels(options),
        visible_attributes=_visible_attributes(options),
        sku_payload=payload,
    )


# ---------------------------------------------------------------------------
# Group resolution + upsert
# ---------------------------------------------------------------------------


SELECT_GROUP_PRIMARY_SQL = """
    SELECT cp.product_key,
           cp.merchant_id,
           cp.platform,
           cp.source_product_id,
           cp.catalog_track,
           cp.title AS parent_title,
           cp.product_payload AS product_payload,
           eps.seed_data AS seed_data
    FROM product_group_members pgm
    JOIN catalog_products cp
      ON cp.merchant_id = pgm.merchant_id
     AND cp.platform = pgm.platform
     AND cp.source_product_id = pgm.platform_product_id
    LEFT JOIN external_product_seeds eps
      ON eps.external_product_id = cp.source_product_id
    WHERE pgm.product_group_id = :group_id
      AND pgm.is_primary = TRUE
    LIMIT 1
"""


SELECT_GROUPS_TO_PROCESS_SQL = """
    SELECT pgm.product_group_id AS group_id, count(*) AS member_count
    FROM product_group_members pgm
    JOIN catalog_products cp
      ON cp.merchant_id = pgm.merchant_id
     AND cp.platform = pgm.platform
     AND cp.source_product_id = pgm.platform_product_id
    WHERE pgm.product_group_id LIKE 'pg_%%'
"""


#: The ONE track that may claim a checkout — an ALLOWLIST of one, deliberately;
#: see `promoted_readiness_tier`. `catalog_products.catalog_track` is
#: `String(32), nullable=False, server_default="internal_merchant"` (db/catalog.py),
#: so this is both the schema default and the money lane. The redirect lane's own
#: spelling ('external_referral' — `ingestion.DEFAULT_CATALOG_TRACK`,
#: `mirror_external_seeds_to_catalog_products.CATALOG_TRACK`,
#: `index_graduation_ladder._OBSERVED_CATALOG_TRACK`) is deliberately NOT named
#: here: naming it would make this a denylist again the day a third track lands.
_INTERNAL_MERCHANT_TRACK = "internal_merchant"


def promoted_readiness_tier(catalog_track: Optional[str]) -> str:
    """The `readiness_tier` a promoted variant may claim, from its PRODUCT's track.

    THE PROMOTER RUNS ON EXTERNAL-SEED PRODUCTS. `SELECT_GROUPS_TO_PROCESS_SQL`
    filters on `product_group_id LIKE 'pg_%'` and nothing else — no track, no
    platform — and `SELECT_GROUP_PRIMARY_SQL` LEFT JOINs `external_product_seeds`
    precisely so Path B (external_seed scrape) variants can be promoted. So the
    literal `'commerce_ready'` this used to write was minted on the redirect lane
    as readily as on the money lane, and #2139 measured the result: 5,083
    `catalog_skus` rows on external-seed products holding 'commerce_ready' that
    should hold 'referral_only'. #2139 repairs that data and names this writer as
    the one that produces it. Making the tier UPWARD-ONLY without this gate would
    have turned the promoter into the writer that RE-MINTS the leak the moment
    #2139's repair landed — a `<pk>::v:` key on an external_seed product, healed to
    'referral_only', promoted straight back to 'commerce_ready' on the next run.

    `commerce_ready` means a checkout can be proved. An `external_referral` product
    is a redirect to somebody else's storefront, which is exactly what
    `ingestion.OFFER_READINESS_TIER` says by writing 'referral_only' for the same
    lane.

    AN ALLOWLIST, NOT A DENYLIST — the round-4 cut of this function was
    `if not track or track == 'external_referral': referral_only; else
    commerce_ready`, which is a denylist of ONE. It contradicted the paragraph
    below it: 'citation' and 'marketplace' are not `external_referral`, so they
    came out `commerce_ready` while the docstring claimed an unknown track gets the
    floor. `catalog_products.catalog_track` is `VARCHAR(32) NOT NULL DEFAULT
    'internal_merchant'` and today's two column writers spell it
    `internal_merchant` or `external_referral` — but the repo's `catalog_track`
    VOCABULARY is already wider than its writers (`pivot_query_service` emits
    'citation' on the serving side for an offer-free, deliberately un-buyable
    item), and the next track added is added by someone who is not reading this
    file. An allowlist makes that person's default the floor.

    So: `commerce_ready` ONLY for `internal_merchant`. Everything else — the
    redirect lane, an unknown track, a track that does not exist yet, or no column
    at all in the row handed to this function — gets the floor. The asymmetry is
    the whole reason: over-claiming a tier fabricates a purchasable SKU,
    under-claiming one only understates a row another lane can still promote.
    """
    track = str(catalog_track or "").strip().lower()
    return "commerce_ready" if track == _INTERNAL_MERCHANT_TRACK else "referral_only"


# THE 3-COLUMN INDEX THIS USED TO NAME NO LONGER EXISTS. Migration 123
# (`db/migrations/123_catalog_skus_4col_unique_index.sql`) created
# `idx_catalog_skus_source_identity_v2 (merchant_id, platform, product_key,
# source_variant_id)` and DROPPED the old `(merchant_id, platform,
# source_variant_id)`. Postgres answers an ON CONFLICT clause matching no unique
# index with SQLSTATE 42P10 at PARSE time, so this statement — and with it
# `promote_variants_all` — has been unexecutable ever since. Nothing caught it:
# the SQLite suite drives a fake DB and asserted on the string, and a string
# assertion cannot tell a live index from a plausible-looking dead one.
UPSERT_SKU_SQL = """
    INSERT INTO catalog_skus (
        sku_key, product_key, merchant_id, platform,
        source_product_id, source_variant_id,
        sku, barcode, title, currency, image_url,
        visible_option_labels, visible_attributes, sku_payload,
        readiness_tier, updated_at
    ) VALUES (
        :sku_key, :product_key, :merchant_id, :platform,
        :source_product_id, :source_variant_id,
        :sku, :barcode, :title, :currency, :image_url,
        CAST(:visible_option_labels AS jsonb),
        CAST(:visible_attributes AS jsonb),
        CAST(:sku_payload AS jsonb),
        -- WAS THE LITERAL 'commerce_ready', ON EVERY LANE. `promoted_readiness_tier`
        -- now decides it from the PRODUCT's `catalog_track`: an external_referral
        -- row is a redirect, not a checkout, so it gets 'referral_only'. See that
        -- function for the #2139 leak this closes.
        :readiness_tier, NOW()
    )
    ON CONFLICT (merchant_id, platform, product_key, source_variant_id)
    DO UPDATE SET
        -- NO `sku_key =`, NO `product_key =`, NO `source_product_id =`. Repointing
        -- the arbiter without dropping these would have been worse than the outage
        -- it fixes: on the 4,286 rows the 2026-09-08 variant-identity backfill
        -- ADOPTED, a re-promotion would rename the primary key back to this lane's
        -- `<pk>::v::<vid>` spelling under the live offers keyed on it
        -- (catalog_offers has no FK to catch that), and rewrite the product_key of
        -- a row another writer placed.
        sku = EXCLUDED.sku,
        barcode = EXCLUDED.barcode,
        title = EXCLUDED.title,
        currency = EXCLUDED.currency,
        image_url = EXCLUDED.image_url,
        visible_option_labels = EXCLUDED.visible_option_labels,
        visible_attributes = EXCLUDED.visible_attributes,
        -- MERGE, never replace: the row we land on may carry the backfill's
        -- `variant_id_provenance` / `source_system` stamps, or an ingest lane's
        -- `agent_version` / `canonical_url`. COALESCE because `NULL || jsonb` is
        -- NULL and the column is nullable.
        sku_payload = COALESCE(catalog_skus.sku_payload, CAST('{}' AS jsonb))
                      || EXCLUDED.sku_payload,
        -- `readiness_tier` WAS INSERT-ONLY, and with the identity arbiter that made
        -- the promoter's headline count a lie. An ingest-spelled `<pk>::v:<vid>` row
        -- sits at 'referral_only' (that is what `apply._SKU_UPSERT_SQL` inserts);
        -- the promoter resolves onto it through the identity index, DO UPDATEs the
        -- content, counts `variants_promoted`, and leaves the tier exactly where it
        -- was — measured `promoted=1, tier_after='referral_only'`, on 4,474 such
        -- rows. "Promoted" has to mean the tier moved.
        --
        -- UPWARD-ONLY, on `services.index_graduation_ladder.OBSERVED_READINESS_LADDER`
        -- (referral_only -> knowledge_ready -> commerce_ready). A tier is never
        -- lowered here, and a value OFF that ladder (a first-party 'vertical_ready')
        -- is left untouched — the same monotonic rule, and the same treatment of
        -- off-ladder values, that `index_graduation_ladder._tiers_below` expresses
        -- one table up. The ladder is repeated as a literal rather than interpolated
        -- because this statement has to stay a top-level constant the repo PREPARE
        -- gate can collect;
        -- `test_the_promoters_tier_move_is_the_repos_ladder_and_only_upward` drives
        -- every (stored, offered) pair through this CASE and checks it against that
        -- module's list.
        readiness_tier = CASE
            WHEN catalog_skus.readiness_tier = 'referral_only'
                 AND EXCLUDED.readiness_tier IN ('knowledge_ready', 'commerce_ready')
              THEN EXCLUDED.readiness_tier
            WHEN catalog_skus.readiness_tier = 'knowledge_ready'
                 AND EXCLUDED.readiness_tier = 'commerce_ready'
              THEN EXCLUDED.readiness_tier
            ELSE catalog_skus.readiness_tier
        END,
        updated_at = NOW()
"""


def _is_unique_violation(exc: BaseException) -> bool:
    """True for a Postgres unique violation (SQLSTATE 23505).

    Same test as `routes/billing_routes._is_unique_violation` and
    `services/catalog_enrichment_agent/apply._is_unique_violation`: the driver's
    `sqlstate`/`pgcode`, class name as fallback, NEVER the message text — a text
    match would fold every other constraint failure into this one bucket. The
    cause/context chain is walked because a wrapper can hide the driver error.
    """
    seen: set = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if code == "23505":
            return True
        if "uniqueviolation" in type(cur).__name__.lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _extract_variants_for_primary(primary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Try Path B (seed_data.snapshot.variants) first, then Path A
    (product_payload.variants). Returns whichever was non-empty.
    Path B preferred because external_seed scrapes have richer
    options metadata (axis_kind, structured shade/size labels)."""
    seed_variants = _extract_variants_from_seed(primary.get("seed_data"))
    if seed_variants:
        return seed_variants
    payload_variants = _extract_variants_from_payload(primary.get("product_payload"))
    return payload_variants


async def promote_variants_for_group(
    *, group_id: str, apply: bool = False
) -> GroupOutcome:
    """Process one product_group. Idempotent — repeat calls upsert.
    Wraps the per-group upserts in a transaction so partial failure
    doesn't leave a group with some variants but not others."""
    primary_row = await database.fetch_one(
        SELECT_GROUP_PRIMARY_SQL, {"group_id": group_id}
    )
    if primary_row is None:
        return GroupOutcome(
            product_group_id=group_id,
            primary_product_key="",
            variants_found=0,
            variants_promoted=0,
            skipped_reason="no_primary_for_group",
        )

    primary = dict(primary_row)
    raw_variants = _extract_variants_for_primary(primary)
    real_variants = filter_real_variants(raw_variants)

    if not real_variants:
        return GroupOutcome(
            product_group_id=group_id,
            primary_product_key=primary["product_key"],
            variants_found=len(raw_variants),
            variants_promoted=0,
            skipped_reason="no_real_variants",
        )

    # ONE ROW PER IDENTITY. `source_variant_id` is varchar(128), so two merchant
    # variant ids that share a 128-char prefix bind to the SAME identity tuple. They
    # are one row in `catalog_skus` whatever we do — the identity index says so — so
    # the choice is between writing the second over the first and COUNTING two
    # promotions, or dropping it and saying so. Counted, because a silent overwrite
    # is how one shade's title ends up on another shade's row.
    rows_to_upsert: List[VariantRow] = []
    identity_collisions = 0
    seen_identities: set = set()
    for v in real_variants:
        row = build_variant_row(variant=v, primary=primary)
        if row is None:
            continue
        identity = (
            row.merchant_id, row.platform, row.product_key, row.source_variant_id
        )
        if identity in seen_identities:
            identity_collisions += 1
            logger.warning(
                "variant dropped (same identity as an earlier variant in this "
                "group): sku_key=%s identity=(merchant_id=%s, platform=%s, "
                "product_key=%s, source_variant_id=%s) — two merchant variant ids "
                "bind to one source_variant_id (varchar(%d)); writing this row would "
                "DO UPDATE the first through the identity index, not add a shade",
                row.sku_key, row.merchant_id, row.platform, row.product_key,
                row.source_variant_id, _SOURCE_VARIANT_ID_MAX,
            )
            continue
        seen_identities.add(identity)
        rows_to_upsert.append(row)

    sample_titles = [r.title for r in rows_to_upsert[:5]]

    # The tier every row in this group may claim, decided ONCE from the primary
    # product's track (they are all rows of that product). See
    # `promoted_readiness_tier`: the promoter runs on external-seed products too,
    # and a redirect is not a checkout.
    readiness_tier = promoted_readiness_tier(primary.get("catalog_track"))

    # AND WHETHER A WRITE ON THIS LANE CAN PROMOTE ANYTHING. The offered tier is
    # decided once for the group, so this is too: 'referral_only' is the FLOOR of
    # `index_graduation_ladder.OBSERVED_READINESS_LADDER`, so `UPSERT_SKU_SQL`'s
    # upward-only CASE cannot move a stored tier and an INSERT mints the floor.
    # Every row written on that lane is `variants_tier_held`, never
    # `variants_promoted` — the content re-projection lands, the tier does not
    # move, and the counter says which.
    tier_can_move = readiness_tier != "referral_only"

    promoted = 0
    tier_held = 0
    identity_conflicts = 0
    write_failures = 0
    if apply and rows_to_upsert:
        async with database.transaction():
            for r in rows_to_upsert:
                params = {
                    "readiness_tier": readiness_tier,
                    "sku_key": r.sku_key,
                    "product_key": r.product_key,
                    "merchant_id": r.merchant_id,
                    "platform": r.platform,
                    "source_product_id": r.source_product_id,
                    # varchar(128). An over-long merchant variant id is a 22001,
                    # which is not a unique violation — before this bound it took
                    # the whole run down rather than one variant. The bound is
                    # applied ONCE, in `build_variant_row`, so `r.sku_key` is derived
                    # from this exact string; re-slicing here would be the return of
                    # the key-vs-identity split. Kept as a defensive no-op.
                    "source_variant_id": str(r.source_variant_id or "")[
                        :_SOURCE_VARIANT_ID_MAX
                    ],
                    "sku": r.sku,
                    "barcode": r.barcode,
                    "title": r.title,
                    "currency": r.currency,
                    "image_url": r.image_url,
                    "visible_option_labels": json.dumps(r.visible_option_labels),
                    "visible_attributes": json.dumps(r.visible_attributes),
                    "sku_payload": json.dumps(r.sku_payload, default=str),
                }
                try:
                    # SAVEPOINT per row. The identity arbiter closes the collision
                    # that matters, but catalog_skus' OTHER unique constraint is
                    # still reachable from the opposite direction -- this row's
                    # `sku_key` already held by a DIFFERENT identity tuple -- and a
                    # 23505 ABORTS the enclosing Postgres transaction. Without the
                    # savepoint one such variant takes the whole group down with it.
                    async with database.transaction():
                        await database.execute(UPSERT_SKU_SQL, params)
                    if tier_can_move:
                        promoted += 1
                    else:
                        tier_held += 1
                except Exception as exc:  # noqa: BLE001
                    if not _is_unique_violation(exc):
                        # ONE BAD VARIANT IS NOT A BAD RUN. This used to re-raise,
                        # so any non-23505 write error — a 22001 from an over-long
                        # id, a 23502, a 22P02 on a malformed payload — aborted
                        # `promote_variants_for_group` and, with it, every group
                        # still queued in `promote_variants_all`. The savepoint has
                        # already rolled this row back; count it, log it loudly, and
                        # let the remaining variants land.
                        write_failures += 1
                        logger.exception(
                            "catalog_skus upsert failed (%s) for sku_key=%s "
                            "identity=(merchant_id=%s, platform=%s, product_key=%s, "
                            "source_variant_id=%s) — variant skipped, group "
                            "continues: %s",
                            type(exc).__name__, r.sku_key, r.merchant_id, r.platform,
                            r.product_key, r.source_variant_id, str(exc)[:200],
                        )
                        continue
                    identity_conflicts += 1
                    logger.error(
                        "catalog_skus upsert refused (unique violation, SQLSTATE "
                        "23505): sku_key=%s vs identity=(merchant_id=%s, platform=%s, "
                        "product_key=%s, source_variant_id=%s) -- the key and the "
                        "identity tuple name different rows; variant skipped, group "
                        "continues: %s",
                        r.sku_key, r.merchant_id, r.platform, r.product_key,
                        r.source_variant_id, str(exc)[:200],
                    )

    return GroupOutcome(
        product_group_id=group_id,
        primary_product_key=primary["product_key"],
        variants_found=len(raw_variants),
        # The dry run predicts the SAME split the apply path reports — a preview
        # that says "2 promoted" for a lane on which nothing can be promoted is the
        # very claim this counter exists to stop making.
        variants_promoted=(
            promoted if apply else (len(rows_to_upsert) if tier_can_move else 0)
        ),
        variants_tier_held=(
            tier_held if apply else (0 if tier_can_move else len(rows_to_upsert))
        ),
        sample_variant_titles=sample_titles,
        skus_identity_conflict=identity_conflicts,
        skus_write_failed=write_failures,
        skus_deduped_same_identity=identity_collisions,
    )


async def promote_variants_all(
    *,
    apply: bool = False,
    product_group_id: Optional[str] = None,
    merchant_id: Optional[str] = None,
    limit: int = 100,
) -> PromoterReport:
    """Iterate every multi-member product_group (or scope by group_id
    / merchant_id) and promote variants from the primary.

    THIS ENTRY POINT HAS BEEN UNEXECUTABLE SINCE MIGRATION 123. `UPSERT_SKU_SQL`
    named `ON CONFLICT (merchant_id, platform, source_variant_id)`, an index that
    `db/migrations/123_catalog_skus_4col_unique_index.sql` dropped when it created
    `idx_catalog_skus_source_identity_v2`. Postgres refuses an ON CONFLICT clause
    matching no unique constraint at parse time (SQLSTATE 42P10), so every
    `apply=True` run has raised on its first variant since that migration landed;
    `apply=False` never reaches the statement, which is why the outage was quiet.

    AND THE TABLE HAS MOVED UNDER IT. The 2026-09-08 variant-identity backfill
    (`scripts/backfill_variant_identity_skus.py`) ADOPTED 4,286 rows this promoter
    had written — it conflicts on the same identity index, takes `RETURNING
    sku_key`, and hangs a live `catalog_offers` row off whichever key already held
    the identity. Those offers are keyed on THIS lane's `<pk>::v::<vid>` spelling,
    with no foreign key to protect them, so the repointed upsert deliberately
    updates no identity column: a `sku_key = EXCLUDED.sku_key` here would rename
    primary keys out from under live supply, and a `sku_payload = EXCLUDED...`
    would erase the backfill's `variant_id_provenance` / `source_system` stamps.

    THIS LANE STILL HAS NO ADOPTION/HEAL ARBITER, and this PR does not add one.
    `apply._adopt_existing_sku_identities` reconciles a planned ingest row with the
    rows already holding its key or its identity (adopt / heal / refuse); nothing
    here does. So a variant whose identity is free while its derived `sku_key` is
    held under a different tuple is refused on every run for ever, and
    `skus_identity_conflict_total` is a standing backlog, not a transient. Do not
    read it as healthy because it is stable.

    AND `variants_promoted` NO LONGER MEANS 'commerce_ready'. The tier a promoted
    row may claim comes from its PRODUCT's `catalog_track`
    (`promoted_readiness_tier`): this entry point's candidate query admits
    external-seed products, so on the redirect lane a write offers 'referral_only'.
    On the money lane the tier now MOVES — upward only — where it was INSERT-only
    before, and a promotion could leave a 'referral_only' row exactly where it
    found it while counting itself.

    SO THE COUNT IS SPLIT. `referral_only` is the FLOOR of the repo's ladder, so on
    the redirect lane the upward-only CASE cannot move any stored tier and an
    INSERT mints the floor: NOTHING on that lane is ever promoted, whatever the
    write did to the row's content. Those rows are `variants_tier_held` /
    `skus_tier_held_total`; `variants_promoted` / `skus_upserted_total` now count
    only rows on a lane whose write can actually raise the tier. Rows WRITTEN is
    the two added together — a reader watching only `skus_upserted_total` on an
    external-seed corpus will now correctly see zero promotions rather than a
    headline number that never promoted anything.
    """
    report = PromoterReport()

    # Build the group fetch SQL based on scope
    sql = SELECT_GROUPS_TO_PROCESS_SQL
    params: Dict[str, Any] = {}
    if product_group_id:
        sql += " AND pgm.product_group_id = :group_id"
        params["group_id"] = product_group_id
    elif merchant_id:
        sql += " AND pgm.merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id
    sql += " GROUP BY pgm.product_group_id HAVING count(*) >= 1 ORDER BY pgm.product_group_id"
    if limit > 0:
        sql += " LIMIT :limit"
        params["limit"] = int(limit)

    rows = await database.fetch_all(sql, params)
    group_ids = [r["group_id"] for r in rows or []]

    for gid in group_ids:
        report.groups_considered += 1
        outcome = await promote_variants_for_group(group_id=gid, apply=apply)
        report.per_group.append(outcome)
        if outcome.skipped_reason == "no_primary_for_group":
            report.groups_skipped_no_primary += 1
        elif outcome.skipped_reason == "no_real_variants":
            report.groups_skipped_no_real_variants += 1
        else:
            report.groups_promoted += 1
            report.skus_upserted_total += outcome.variants_promoted
            report.skus_tier_held_total += outcome.variants_tier_held
        report.skus_identity_conflict_total += outcome.skus_identity_conflict
        report.skus_write_failed_total += outcome.skus_write_failed
        report.skus_deduped_same_identity_total += outcome.skus_deduped_same_identity

    return report
