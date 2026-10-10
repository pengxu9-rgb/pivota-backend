"""ADR-011 intake identity contract — resolve-or-attach before every mint.

Every intake door that writes catalog_products MUST pass this one shared
primitive before inserting a row (R1). It composes EXISTING machinery — no new
matching invention:

  - services.catalog_identity.make_content_key / normalize_gtin (the minter),
  - the audit ER gate's exact matchers (services.pdp_matcher.deterministic:
    canonical_url_match / source_product_id_match — the same Tier-0 matchers
    services.audit_index_intake routes through route_audit_match),
  - the deposit gate (services.catalog_identity.resolve_deposit_content_key,
    annotating how strongly the resolved key is grounded),
  - the ADR-008 / P1.4 brand-fragmentation guard
    (services.audit_index_intake.apply_intake_brand_fragmentation_guard).

IDENTITY MODEL (SPU — Amazon ASIN / Dewu SPU; founder direction 2026-07-09):
GTIN is a MATCH ATTRIBUTE, never key-material. There is ONE canonical family
identity per product — `content_key = make_content_key(brand, title)`, GTIN-less
— and the barcode lives beside it in `catalog_products.gtin`. Many products have
no standard GTIN, so a GTIN can never be *required* to have an identity; and a
product seen with-then-without a barcode must converge on ONE identity, not
fragment. The primitive therefore:

  - mints/keys purely on brand+title (the always-available signal),
  - uses GTIN as the STRONGEST Tier-0 matcher (GS1 GTIN = same physical product
    across merchants), attaching to whatever identity already carries it,
  - keeps the two-grain split (ADR-010, R7): content_key is the FAMILY grain;
    the GTIN attribute is the variant/buy-box discriminator downstream. Two
    genuinely-different products that collide on the deliberately non-unique
    brand+title key are FLAGged (never silently forked — the deterministic hash
    can't fork anyway) and told apart downstream by their distinct gtin
    attribute + the deposit gate.

Tier-0 EXACT matches only (ADR-010's auto tier): GTIN attribute, canonical_url,
source_product_id. Fuzzy/attribute matching stays propose-only elsewhere.

ATTACH semantics (ADR-011, review-verified): a catalog_products row IS still
inserted by the door — it reuses the resolved content_key / product_group_id
instead of minting fresh ones. This is NOT P1.3's seed-detach ATTACH (nothing
here touches external_product_seeds.attached_product_key) and there is no
serving change of any kind.

R3 disagreement semantics (FLAG, never a silent second identity):
  - same GTIN, different brand+title family → GTIN is authoritative: ATTACH to
    the GTIN's identity, FLAG the title/brand drift for review;
  - same brand+title, different GTIN → two distinct products colliding on the
    family key → FLAG for disambiguation (the row still lands under the shared
    family key, discriminated by its own gtin attribute downstream);
  - a barcode one SELLER puts on two different products (F2, 2026-10-10: OPI
    shades sharing one barcode at universalnailsupplies) is not a GTIN at all →
    FLAG, never attached on (barcode_reuse_reason).

The GTIN tier reads the product gtin AND variant barcodes on both sides
(catalog_skus.barcode) since F2; see the block comment at gtin_spellings.

Rollout: per-door enable flags, default OFF (mirror-then-sync enable order).
Fail-open: any internal error degrades to MINT (today's behavior) — the
primitive must never block intake on its own account.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# --- Actions --------------------------------------------------------------------

ACTION_ATTACH = "ATTACH"  # reuse the resolved content_key/pg for the new listing
ACTION_MINT = "MINT"      # no exact match — mint fresh, exactly as today
ACTION_FLAG = "FLAG"      # proceed, but a conflict was enqueued for review
ACTION_SKIP = "SKIP"      # do not insert (observed-data doors only)


# --- Doors + per-door rollout flags (default OFF) --------------------------------

DOOR_CATALOG_SYNC = "catalog_sync"
DOOR_EXTERNAL_SEED_MIRROR = "external_seed_mirror"
DOOR_BRAND_AUTHORED = "brand_authored"
DOOR_CATALOG_ENRICHMENT = "catalog_enrichment"
DOOR_URL_AUDIT = "url_audit_intake"

_DOOR_FLAG_ENV = {
    DOOR_CATALOG_SYNC: "ENABLE_INTAKE_IDENTITY_SYNC",
    DOOR_EXTERNAL_SEED_MIRROR: "ENABLE_INTAKE_IDENTITY_MIRROR",
    DOOR_BRAND_AUTHORED: "ENABLE_INTAKE_IDENTITY_BRAND_AUTHORED",
    DOOR_CATALOG_ENRICHMENT: "ENABLE_INTAKE_IDENTITY_ENRICHMENT",
    DOOR_URL_AUDIT: "ENABLE_INTAKE_IDENTITY_AUDIT",
}

# ADR-008 door semantics: first-party doors (connected sync, the merchant's own
# manual authoring) are NEVER blocked — a brand conflict FLAGs and the record
# proceeds (reconcile-at-connect). Observed-data doors (mirror, retailer crawl,
# audit) SKIP the orphan mint; review is enqueued either way.
_DOOR_BLOCKS_ON_BRAND_CONFLICT = {
    DOOR_CATALOG_SYNC: False,
    DOOR_BRAND_AUTHORED: False,
    DOOR_EXTERNAL_SEED_MIRROR: True,
    DOOR_CATALOG_ENRICHMENT: True,
    DOOR_URL_AUDIT: True,
}


def intake_identity_enabled(door: str) -> bool:
    """Per-door rollout flag for the ADR-011 primitive. Default OFF for every
    door; enable mirror-then-sync (the ADR's staged order)."""
    env = _DOOR_FLAG_ENV.get(door)
    if not env:
        return False
    return os.getenv(env, "").strip().lower() in {"1", "true", "yes", "on"}


def canonical_gtin(value: Optional[str]) -> Optional[str]:
    """GS1-canonical GTIN-14 for storage + matching, or None.

    The rule is catalog_identity.validated_source_gtin's (GTIN-8/12/13/14, check
    digit, never all-zero) — not a second copy of it. normalize_gtin alone
    zero-pads, so a feed barcode of "0" became "00000000000000": a GS1-shaped
    key that the GLOBAL Tier-0 lookup (_rows_by_gtin) ATTACHES on, joining
    unrelated products across merchants."""
    from services.catalog_identity import validated_source_gtin

    return validated_source_gtin(value)


# --- Variant barcodes (F2, 2026-10-10) ---------------------------------------------
#
# Tier-0a read only catalog_products.gtin, and the crawl lane sets that column only for a product
# with exactly ONE barcoded variant (curated_brand_feed: a family's barcodes are per shade/size, so
# promoting one would mislabel the family). Every multi-variant row therefore had gtin NULL and its
# barcodes sat in catalog_skus.barcode, which no matcher read. Census 2026-10-10
# (reports/coverage_census_2026_10_10/matching_diagnosis.md): 88 minority-seller keys across the 46
# "matching gap" brands share a variant barcode with another seller's key -- beautyencounter's
# Shalimar (10 variants, gtin NULL) vs perfumania's (product gtin 03346470113541), and ~64 KISS-family
# distributor keys. Tier-0a now matches the listing's product gtin AND its variants' barcodes against
# existing rows' product gtin AND their SKU barcodes.
#
# The same census found the hazard this widens: universalnailsupplies reuses ONE barcode
# (00619828139641) on 4 different OPI shades, and Tier-0a merged them into one key (ck_772bcc30...),
# raising gtin_match_brand_title_drift and attaching anyway. A barcode is identity evidence only
# while it names one product at every seller that carries it -- see barcode_reuse_reason.

VARIANT_BARCODE_MATCH_ENV = "INTAKE_IDENTITY_VARIANT_BARCODE_MATCH"

#: A listing with more distinct barcodes than this is matched on the first ones only (product gtin
#: first, then variants in feed order): the lookup binds 4 spellings per barcode.
MAX_INCOMING_BARCODES = 64

#: Rows the barcode lookup may return. A result this size may be cut short, and a cut-short
#: result can hide the very same-seller row that shows a barcode is reused, so the widened match
#: is not used at all then (evidence says so) -- product-gtin matching runs as before.
BARCODE_LOOKUP_LIMIT = 200

_BANNED_BUCKET_MERCHANT_ID = "external_seed"  # ADR-009's legacy mirror bucket: many sellers, one id
_UNTITLED_VARIANTS = {"", "default title", "default"}


def variant_barcode_match_enabled() -> bool:
    """Rollout flag for the widened (SKU-barcode) lookup. Default OFF: every intake door is enabled
    in prod, and until migration 260's index exists (numbered migrations do not self-apply in prod)
    the SKU arm is a sequential scan of catalog_skus on EVERY barcoded intake call, on the 2-vCPU
    primary. Order: merge -> apply 260 (CONCURRENTLY) and confirm the index is valid -> set this
    to 1 on the drain, then web/worker. The reuse guard stays on either way -- it is a fix to the
    product-gtin path too, not part of the widening."""
    return os.getenv(VARIANT_BARCODE_MATCH_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def gtin_spellings(gtin14: str) -> List[str]:
    """Every digit string a writer may have STORED for one GTIN-14.

    catalog_skus.barcode holds the source's barcode as given (`str(v["barcode"]).strip()`, e.g.
    "8809530070499" for 08809530070499): GS1 right-aligns GTIN-8/12/13 inside the 14-digit form, so
    the shorter spellings are exactly the 14-digit one with leading zeros dropped. Matching on this
    set keeps the lookup a plain btree IN-list instead of a per-row normalization in SQL. A barcode
    stored with spaces or hyphens inside it is not found (none measured; validated_source_gtin would
    accept it, the IN-list does not)."""
    out = [gtin14]
    for length in (13, 12, 8):
        pad = 14 - length
        if gtin14[:pad] == "0" * pad:
            out.append(gtin14[pad:])
    return out


def _variant_title(title: Any, sku_key: Any = None) -> str:
    """A variant's normalized title, or '' when it names nothing: the crawl lane's synthetic
    `::canonical` SKU repeats the product barcode under the PRODUCT title next to the native
    variant's "Default Title", so neither may count as a second variant title."""
    if str(sku_key or "").endswith("::canonical"):
        return ""
    from services.catalog_identity import normalize_title

    norm = normalize_title(title if isinstance(title, str) else None)
    return "" if norm in _UNTITLED_VARIANTS else norm


def incoming_barcodes(
    gtin14: Optional[str], variants: Optional[Sequence[Mapping[str, Any]]]
) -> Tuple[List[str], Dict[str, List[str]]]:
    """The listing's canonical barcodes (product gtin first, then variants in order, deduped,
    capped) and the ones it reuses itself: a barcode on 2+ of its own variants with different
    titles names no single product (the incoming side of the OPI shade reuse)."""
    ordered: List[str] = [gtin14] if gtin14 else []
    titles: Dict[str, set] = {}
    for variant in variants or ():
        if not isinstance(variant, Mapping):
            continue
        raw = variant.get("barcode")
        code = canonical_gtin(str(raw).strip() if raw is not None else None)
        if not code:
            continue
        if code not in ordered:
            ordered.append(code)
        vt = _variant_title(variant.get("title"), variant.get("sku_key"))
        if vt:
            titles.setdefault(code, set()).add(vt)
    reused = {code: sorted(t) for code, t in titles.items() if len(t) > 1}
    return ordered[:MAX_INCOMING_BARCODES], reused


def seller_key(row: Mapping[str, Any]) -> str:
    """Who sells a row, for the reuse guard and the backfill's "different seller" test.

    merchant_id IS the seller (ADR-009 D2: one observed id per retailer domain, or per brand+domain
    for a brand store), except the banned legacy `external_seed` bucket, which still holds rows of
    many sellers under one id: there the domain decides, and a row with no domain is its own seller
    (never assume two such rows share a store)."""
    merchant = str(row.get("merchant_id") or "")
    if merchant and merchant != _BANNED_BUCKET_MERCHANT_ID:
        return merchant
    domain = str(row.get("source_domain") or "").strip().lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return f"{merchant}|{domain}" if domain else f"{merchant}|pk:{row.get('product_key')}"


def barcode_reuse_reason(
    matches: Sequence[Mapping[str, Any]],
    *,
    incoming: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Why one barcode is NOT identity evidence, or None when it is.

    `matches` are the existing rows carrying the barcode (as product gtin and/or SKU barcode; a row
    may appear once per way it carries it). `incoming` ({product_key, merchant_id, source_domain,
    title}) is the listing being resolved; its own stored row (same product_key: a re-crawl) is not
    evidence against itself.

    A GS1 barcode names one physical product. Within ONE seller it must therefore sit on one
    product: one title, one family key. Seeing it on two differently-titled products at one store
    (universalnailsupplies' 4 OPI shades on 00619828139641), or split across two family keys there,
    or on two differently-titled variants of one product, means the store reuses it -- and then it
    cannot say which of them another seller's row is. The same title test includes the incoming
    listing when its own store already holds a row with the barcode: that is the drift branch's
    same-merchant shade conflict, which used to attach and merge the shades.

    ACROSS sellers different titles are expected ("Shalimar Perfume" vs "Shalimar by Guerlain for
    Women") and are not a reason: that is today's attach + gtin_match_brand_title_drift FLAG."""
    from services.catalog_identity import normalize_title

    own_pk = str((incoming or {}).get("product_key") or "") or None
    titles: Dict[str, set] = {}
    families: Dict[str, set] = {}
    variant_titles: Dict[str, set] = {}
    for m in matches:
        pk = str(m.get("product_key") or "")
        if own_pk and pk == own_pk:
            continue
        seller = seller_key(m)
        title = normalize_title(m.get("title"))
        if title:
            titles.setdefault(seller, set()).add(title)
        if m.get("content_key"):
            families.setdefault(seller, set()).add(m["content_key"])
        if m.get("match_source") == "sku_barcode":
            vt = _variant_title(m.get("sku_title"), m.get("sku_key"))
            if vt:
                variant_titles.setdefault(pk, set()).add(vt)
    if incoming:
        seller = seller_key(incoming)
        title = normalize_title(incoming.get("title"))
        if title and seller in titles:
            titles[seller].add(title)
    if any(len(t) > 1 for t in titles.values()):
        return "titles_differ_within_seller"
    if any(len(f) > 1 for f in families.values()):
        return "families_differ_within_seller"
    if any(len(v) > 1 for v in variant_titles.values()):
        return "variant_titles_differ_within_product"
    return None


# --- DB lookups (each one small, exact, and monkeypatch-friendly) ----------------

_ROW_COLUMNS = (
    "product_key, merchant_id, platform, source_product_id, canonical_url, "
    "title, brand, content_key, gtin, pivota_signature_id, pivota_canonical_url"
)


async def _rows_by_gtin(
    gtin14: str, prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing listings carrying this GTIN attribute (the authoritative Tier-0
    matcher). GLOBAL scope — a GS1 GTIN identifies one physical product across
    every merchant. Same-merchant rows first, then oldest (the original
    identity), suppressed rows excluded."""
    if not gtin14:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE gtin = :gtin
          AND suppression_reason IS NULL
        ORDER BY (merchant_id = :merchant_id) DESC, created_at ASC
        LIMIT 5
        """,
        {"gtin": gtin14, "merchant_id": prefer_merchant_id or ""},
    )
    return [dict(row) for row in rows or []]


def barcode_lookup_sql(n_spellings: int) -> str:
    """The widened Tier-0a lookup over `n_spellings` bound spellings (:b0..), one row per way a live
    product carries one of them: `product_gtin` (catalog_products.gtin) or `sku_barcode` (a live
    catalog_skus.barcode). Both arms are IN-lists on a partial btree -- idx_catalog_products_gtin
    and idx_catalog_skus_barcode (migration 260) -- joined to the product by its primary key.
    Without 260's index the SKU arm is a sequential scan of catalog_skus per call; correct, and
    bounded by that table's size."""
    if n_spellings < 1:
        raise ValueError("barcode_lookup_sql needs at least one spelling")
    ph = ", ".join(f":b{i}" for i in range(n_spellings))
    return f"""
        SELECT cp.product_key, cp.merchant_id, cp.platform, cp.source_product_id,
               cp.canonical_url, cp.title, cp.brand, cp.content_key, cp.gtin,
               cp.pivota_signature_id, cp.pivota_canonical_url, cp.source_domain,
               cp.created_at,
               hit.matched_barcode, hit.match_source, hit.sku_key, hit.sku_title
        FROM (
            SELECT product_key, gtin AS matched_barcode, 'product_gtin' AS match_source,
                   CAST(NULL AS TEXT) AS sku_key, CAST(NULL AS TEXT) AS sku_title
            FROM catalog_products
            WHERE gtin IN ({ph})
            UNION ALL
            SELECT product_key, barcode, 'sku_barcode', sku_key, title
            FROM catalog_skus
            WHERE barcode IN ({ph})
              AND suppression_reason IS NULL
        ) hit
        JOIN catalog_products cp ON cp.product_key = hit.product_key
        WHERE cp.suppression_reason IS NULL
        ORDER BY (cp.merchant_id = :merchant_id) DESC, cp.created_at ASC,
                 cp.product_key ASC, hit.match_source ASC
        LIMIT {BARCODE_LOOKUP_LIMIT}
        """


async def _rows_by_barcodes(
    barcodes: Sequence[str], prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing live listings carrying any of these canonical GTIN-14s, as product gtin OR as a
    live SKU's barcode, each tagged with `matched_gtin` (canonical) and `match_source`. GLOBAL
    scope, same order as _rows_by_gtin (same merchant first, then oldest)."""
    spellings: List[str] = []
    for code in barcodes:
        for s in gtin_spellings(code):
            if s not in spellings:
                spellings.append(s)
    if not spellings:
        return []
    from db.database import database

    params: Dict[str, Any] = {f"b{i}": s for i, s in enumerate(spellings)}
    params["merchant_id"] = prefer_merchant_id or ""
    rows = await database.fetch_all(barcode_lookup_sql(len(spellings)), params)
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        d["matched_gtin"] = canonical_gtin(str(d.get("matched_barcode") or "").strip() or None)
        if d["matched_gtin"]:
            out.append(d)
    return out


async def _open_identity_review_exists(product_key: Optional[str], matcher: str) -> bool:
    """An identity review task for this listing and matcher is still open. The barcode FLAGs fire
    on every crawl of a listing whose store reuses a barcode, and pdp_review_tasks has no dedupe of
    its own (842 open identity tasks on 2026-10-10, none drained); one open task per listing and
    matcher is the useful amount. Unsure -> False (enqueue: a duplicate beats a lost flag)."""
    if not product_key:
        return False
    try:
        from db.database import database

        row = await database.fetch_one(
            """
            SELECT 1 AS one FROM pdp_review_tasks
            WHERE pdp_id = :pdp_id
              AND module_key = 'identity'
              AND status = 'needs_review'
              AND checklist->>'matcher' = :matcher
            LIMIT 1
            """,
            {"pdp_id": str(product_key)[:96], "matcher": matcher},
        )
        return row is not None
    except Exception as exc:  # noqa: BLE001 — dedupe is best-effort
        logger.warning("intake_identity review dedupe lookup failed: %s", str(exc)[:200])
        return False


async def _rows_by_content_key(
    content_key: str, prefer_merchant_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Existing listings on this brand+title FAMILY key. Same-merchant rows
    first, then oldest, suppressed rows excluded."""
    if not content_key:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE content_key = :content_key
          AND suppression_reason IS NULL
        ORDER BY (merchant_id = :merchant_id) DESC, created_at ASC
        LIMIT 5
        """,
        {"content_key": content_key, "merchant_id": prefer_merchant_id or ""},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_canonical_url(url_path_fragment: str) -> List[Dict[str, Any]]:
    """LIKE prefilter on the URL path (catches scheme/www drift); the pure
    matcher does the exact normalized-equality check. Unlike the pdp_matcher
    runner's candidate fetch, intake attaches against ANY live listing — no
    pdp_scope restriction (most catalog rows are merchant_owned/unverified)."""
    if not url_path_fragment:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE canonical_url IS NOT NULL
          AND LOWER(canonical_url) LIKE :url_like
          AND suppression_reason IS NULL
        LIMIT 25
        """,
        {"url_like": f"%{url_path_fragment.lower()}%"},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_source_id(
    source_product_id: str, merchant_id: str
) -> List[Dict[str, Any]]:
    """source_product_id is only identity evidence WITHIN a merchant (two
    Shopify stores can both have product id 12345) — same-merchant scope."""
    if not source_product_id or not merchant_id:
        return []
    from db.database import database

    rows = await database.fetch_all(
        f"""
        SELECT {_ROW_COLUMNS}
        FROM catalog_products
        WHERE source_product_id = :source_product_id
          AND merchant_id = :merchant_id
          AND suppression_reason IS NULL
        LIMIT 25
        """,
        {"source_product_id": source_product_id, "merchant_id": merchant_id},
    )
    return [dict(row) for row in rows or []]


async def _existing_pg_for_listing(row: Dict[str, Any]) -> Optional[str]:
    """The product_group the matched listing already belongs to (curated or
    singleton). Falls back to the deterministic singleton pg for the resolved
    content_key when no membership row exists yet."""
    from db.database import database

    member = await database.fetch_one(
        """
        SELECT product_group_id FROM product_group_members
        WHERE merchant_id = :merchant_id
          AND platform = :platform
          AND platform_product_id = :platform_product_id
        """,
        {
            "merchant_id": str(row.get("merchant_id") or ""),
            "platform": str(row.get("platform") or ""),
            "platform_product_id": str(row.get("source_product_id") or ""),
        },
    )
    return member["product_group_id"] if member else None


async def _write_provenance(provenance: Dict[str, Any]) -> None:
    """R1's provenance requirement: every outcome writes {door, action, matcher,
    evidence} (feeds ADR-010 D-2 + gold-label capture). Best-effort — a
    provenance failure must never block intake (the structured log line below
    is the always-on fallback)."""
    try:
        from db.database import database

        await database.execute(
            """
            INSERT INTO intake_identity_events
              (door, action, matcher, merchant_id, product_key, content_key,
               product_group_id, evidence)
            VALUES
              (:door, :action, :matcher, :merchant_id, :product_key, :content_key,
               :product_group_id, CAST(:evidence AS jsonb))
            """,
            {
                "door": provenance.get("door"),
                "action": provenance.get("action"),
                "matcher": provenance.get("matcher"),
                "merchant_id": provenance.get("merchant_id"),
                "product_key": provenance.get("product_key"),
                "content_key": provenance.get("content_key"),
                "product_group_id": provenance.get("product_group_id"),
                "evidence": json.dumps(
                    provenance.get("evidence") or {}, ensure_ascii=False, default=str
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort
        logger.warning("intake_identity provenance write failed: %s", str(exc)[:200])


# --- Result assembly --------------------------------------------------------------


def _singleton_pg(content_key: Optional[str]) -> Optional[str]:
    if not content_key:
        return None
    from services.product_group_autogrouper import make_singleton_product_group_id

    return make_singleton_product_group_id(content_key)


def _deposit_basis(brand, title, gtin, content_key) -> str:
    """Deposit-gate annotation (catalog_identity.resolve_deposit_content_key):
    how strongly the resolved key is grounded (GTIN-backed identities are
    well-grounded even though our KEY no longer folds the GTIN in). Evidence
    only — deposits themselves stay gated at their own call sites."""
    from services.catalog_identity import resolve_deposit_content_key

    return resolve_deposit_content_key(
        brand=brand, title=title, gtin=gtin, existing_content_key=content_key
    ).basis


async def _finish(
    *,
    action: str,
    content_key: Optional[str],
    product_group_id: Optional[str],
    matcher: Optional[str],
    door: str,
    merchant_ctx: Dict[str, Any],
    detail: Dict[str, Any],
    gtin: Optional[str],
    attach: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    provenance = {
        "door": door,
        "action": action,
        "matcher": matcher,
        "merchant_id": merchant_ctx.get("merchant_id"),
        "product_key": merchant_ctx.get("product_key"),
        "content_key": content_key,
        "product_group_id": product_group_id,
        "evidence": detail,
    }
    logger.info(
        "intake_identity.resolve door=%s action=%s matcher=%s merchant=%s ck=%s pg=%s",
        door, action, matcher, merchant_ctx.get("merchant_id"),
        content_key, product_group_id,
    )
    await _write_provenance(provenance)
    return {
        "content_key": content_key,
        "product_group_id": product_group_id,
        "action": action,
        "gtin": gtin,
        "evidence": provenance,
        "attach": attach,
    }


def _attach_info(row: Dict[str, Any], merchant_id: Optional[str]) -> Dict[str, Any]:
    """What the door needs to reuse the matched listing's identity. R4 /
    ADR-010 D-6: on a SAME-merchant attach, the audit door must resolve to the
    listing's existing source_product_id/sig instead of minting a URL-fresh
    one — `same_merchant` is that signal."""
    return {
        "product_key": row.get("product_key"),
        "merchant_id": row.get("merchant_id"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        "pivota_signature_id": row.get("pivota_signature_id"),
        "pivota_canonical_url": row.get("pivota_canonical_url"),
        "same_merchant": bool(
            merchant_id and str(row.get("merchant_id") or "") == str(merchant_id)
        ),
    }


async def _attach_pg(row: Dict[str, Any], content_key: Optional[str], *, strict: bool = False) -> Optional[str]:
    try:
        existing = await _existing_pg_for_listing(row)
    except Exception as exc:  # noqa: BLE001 — pg lookup is best-effort
        if strict:
            raise
        logger.warning("intake_identity pg lookup failed: %s", str(exc)[:200])
        existing = None
    return existing or _singleton_pg(content_key)


async def _flag_review(
    door: str,
    merchant_ctx: Dict[str, Any],
    content_key: Optional[str],
    matcher: str,
    detail: Dict[str, Any],
) -> None:
    """FLAG outcomes ride the SAME review rail as the ER gate / P1.4 guard
    (pdp_review_tasks, module 'identity') so reconciliation sees one queue."""
    from services.audit_index_intake import enqueue_audit_identity_review

    await enqueue_audit_identity_review(
        {
            "product_key": merchant_ctx.get("product_key"),
            "content_key": content_key,
        },
        {
            "product_key": detail.get("conflict_product_key"),
            "matcher": matcher,
            "confidence": None,
            "evidence": {**detail, "door": door},
        },
    )


async def _flag_review_once(
    door: str,
    merchant_ctx: Dict[str, Any],
    content_key: Optional[str],
    matcher: str,
    detail: Dict[str, Any],
) -> bool:
    """_flag_review unless this listing already has an open task for `matcher`. True if enqueued."""
    if await _open_identity_review_exists(merchant_ctx.get("product_key"), matcher):
        return False
    await _flag_review(door, merchant_ctx, content_key, matcher, detail)
    return True


def _sorted_candidates(rows: Sequence[Dict[str, Any]], merchant_id: Optional[str]) -> List[Dict[str, Any]]:
    """One entry per product (first occurrence wins), in _rows_by_gtin's order: the caller's merchant
    first, then oldest. Rows of several barcodes are merged, so the order is re-imposed here."""
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        pk = str(r.get("product_key") or "")
        if pk and pk not in seen:
            seen[pk] = r
    return sorted(
        seen.values(),
        key=lambda r: (
            str(r.get("merchant_id") or "") != str(merchant_id or ""),
            r.get("created_at") is None,
            str(r.get("created_at") or ""),
            str(r.get("product_key") or ""),
        ),
    )


# --- The primitive ---------------------------------------------------------------


_RETAILER_LISTING_KEY_PREFIX = "ext:retailer:"


def _is_retailer_listing_key(product_key: Optional[str]) -> bool:
    return str(product_key or "").startswith(_RETAILER_LISTING_KEY_PREFIX)


def _is_retailer_listing_ctx(door: str, ctx: Dict[str, Any]) -> bool:
    """Tier-0e runs only for a retailer-lane listing at the crawl door."""
    return door == DOOR_CATALOG_ENRICHMENT and _is_retailer_listing_key(ctx.get("product_key"))


async def _listing_has_membership(ctx: Dict[str, Any], source_product_id: Optional[str]) -> bool:
    """True when this listing already belongs to a product group -- a re-crawl, or any doubt."""
    if not source_product_id:
        return True
    try:
        return bool(await _existing_pg_for_listing({
            "merchant_id": ctx.get("merchant_id"), "platform": ctx.get("platform"),
            "source_product_id": source_product_id,
        }))
    except Exception:  # noqa: BLE001 -- unsure means existing: never risk a group move
        return True


def _brand_stripped_content_key(brand: Optional[str], title: Optional[str]) -> Optional[str]:
    """The family key of `title` with a LEADING brand name removed, or None when the title does not
    start with the brand (normalized as content_key normalizes titles, so "[MISSHA] X", "Missha X" and
    "MISSHA X" all strip), or when nothing would be left."""
    from services.catalog_identity import make_content_key, normalize_title

    norm_title = normalize_title(title)
    norm_brand = normalize_title(brand)
    if not norm_title or not norm_brand:
        return None
    for prefix in dict.fromkeys((norm_brand, norm_brand.replace(" ", ""), norm_brand.replace("-", " "))):
        if prefix and norm_title.startswith(prefix + " "):
            rest = norm_title[len(prefix) + 1:].strip()
            return make_content_key(brand, rest) if rest else None
    return None


async def resolve_or_attach_content_identity(
    brand: Optional[str],
    title: Optional[str],
    gtin: Optional[str] = None,
    canonical_url: Optional[str] = None,
    source_product_id: Optional[str] = None,
    door: str = "",
    merchant_ctx: Optional[Dict[str, Any]] = None,
    variants: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Resolve-or-attach the content identity for one incoming catalog row.

    `variants` ([{barcode, title}], optional): the listing's variants as its door
    observed them. Their barcodes join the product gtin in Tier-0a; their titles
    tell a reused barcode from a per-variant one.

    Returns {content_key, product_group_id, action, gtin, evidence, attach}:
      - ATTACH: an existing identity matched Tier-0 exactly (GTIN attribute or a
        variant barcode, content_key, canonical_url, or same-merchant
        source_product_id) — the
        door inserts its row REUSING content_key/product_group_id (and,
        same-merchant at the audit door, the matched listing's
        source_product_id/sig — R4).
      - MINT:   no exact match — content_key is the GTIN-less family key
        make_content_key(brand, title) and product_group_id its deterministic
        singleton pg. `gtin` (canonicalized) is returned for the door to persist
        as the match attribute.
      - FLAG:   a conflict (GTIN/brand-title disagreement, or a
        brand-fragmentation conflict at a first-party door) was enqueued for
        identity review; the door PROCEEDS with the returned identity — flagged,
        not silent.
      - SKIP:   observed-data doors only — a brand-fragmentation conflict; the
        door must not insert (review enqueued).

    Fail-open by construction: any internal error returns MINT with today's
    brand+title identity. Never raises.
    """
    ctx = dict(merchant_ctx or {})
    # Only the selected primary enrichment writer opts into this contract.
    # A successful lookup returning no membership still permits a singleton.
    strict_pg = door == DOOR_CATALOG_ENRICHMENT and ctx.get("strict_group_resolution") is True
    merchant_id = str(ctx.get("merchant_id") or "") or None

    from services.catalog_identity import make_content_key

    gtin14 = canonical_gtin(gtin)
    content_key = make_content_key(brand, title)  # single canonical FAMILY key
    # What Tier-0a decided about barcodes when it did NOT attach on them (an untrusted
    # barcode, an ambiguous family, a re-crawl left to the backfill). Rides every later
    # outcome's evidence, and turns a final MINT into a FLAG when a review was raised.
    barcode_note: Dict[str, Any] = {}

    def _base_detail() -> Dict[str, Any]:
        detail = {
            "brand": brand,
            "title": title,
            "gtin": gtin14,
            "canonical_url": canonical_url,
            "source_product_id": source_product_id,
            "content_key": content_key,
        }
        if barcode_note:
            detail["barcode_match"] = dict(barcode_note)
        return detail

    try:
        barcodes, self_reused = incoming_barcodes(gtin14, variants)
        legacy_rows: List[Dict[str, Any]] = []
        widened: List[Dict[str, Any]] = []
        untrusted: Dict[str, str] = {}
        own_pk = str(ctx.get("product_key") or "") or None
        if barcodes:
            legacy_rows = await _rows_by_gtin(gtin14, merchant_id) if gtin14 else []
            if variant_barcode_match_enabled():
                try:
                    widened = await _rows_by_barcodes(barcodes, merchant_id)
                except Exception as exc:  # noqa: BLE001 — the widening must never cost today's match
                    logger.warning("intake_identity barcode lookup failed: %s", str(exc)[:200])
                    barcode_note["widened_lookup"] = "error"
            if len(widened) >= BARCODE_LOOKUP_LIMIT:
                barcode_note["widened_lookup"] = "truncated"
            # Every existing row per barcode: the product-gtin rows (as today) plus the widened ones.
            matches: Dict[str, List[Dict[str, Any]]] = {}
            for r in legacy_rows:
                matches.setdefault(gtin14, []).append({**r, "match_source": "product_gtin"})
            widened = [r for r in widened if r.get("matched_gtin")]
            for r in widened:
                matches.setdefault(r["matched_gtin"], []).append(r)
            incoming_listing = {
                "product_key": ctx.get("product_key"), "merchant_id": merchant_id,
                "source_domain": ctx.get("source_domain"), "title": title,
            }
            for code in barcodes:
                if code in self_reused:
                    untrusted[code] = "variant_titles_differ_within_listing"
                    continue
                reason = barcode_reuse_reason(matches.get(code) or [], incoming=incoming_listing)
                if reason:
                    untrusted[code] = reason
            # FLAG only a reused barcode that matched SOMEONE else's row: that is a match this tier
            # declined. A listing that reuses a barcode nobody else carries changed no decision.
            refused = {
                code: why for code, why in untrusted.items()
                if any(str(m.get("product_key") or "") != (own_pk or "") for m in matches.get(code) or [])
            }
            if untrusted:
                barcode_note["untrusted_barcodes"] = dict(sorted(untrusted.items()))
            if refused:
                barcode_note["flag_matcher"] = "gtin_reused_within_seller"
                detail = {
                    **_base_detail(),
                    "reason": "gtin_reused_within_seller",
                    "refused_barcodes": dict(sorted(refused.items())),
                    "conflict_product_key": next(
                        (m.get("product_key") for code in refused for m in matches.get(code) or []
                         if str(m.get("product_key") or "") != (own_pk or "")), None),
                }
                await _flag_review_once(door, ctx, content_key, "gtin_reused_within_seller", detail)
            if gtin14 in untrusted:
                legacy_rows = []

        # -- Tier-0a: GTIN attribute (authoritative, cross-merchant). A GS1 GTIN
        # identifies one physical product regardless of who sells it or how the
        # title is phrased, so it OUTRANKS brand+title. A barcode the reuse guard
        # refused above is not a GTIN here (legacy_rows emptied): the OPI shades
        # at universalnailsupplies no longer merge, and the next tiers decide.
        if gtin14:
            rows = legacy_rows
            if rows:
                row = rows[0]
                matched_ck = row.get("content_key") or content_key
                distinct_cks = {r.get("content_key") for r in rows if r.get("content_key")}
                drift = bool(content_key and matched_ck != content_key)
                if drift or len(distinct_cks) > 1:
                    # Same GTIN under a different brand+title family (or split
                    # across families). GTIN is authoritative → ATTACH to it,
                    # but FLAG the metadata drift. We never fork a new identity,
                    # so this is non-silent-attach, not a second identity.
                    detail = {
                        **_base_detail(),
                        "reason": "gtin_match_brand_title_drift",
                        "conflict_product_key": row.get("product_key"),
                        "matched_content_key": matched_ck,
                        "incoming_content_key": content_key,
                        "distinct_content_keys": sorted(c for c in distinct_cks if c),
                        "deposit_basis": _deposit_basis(brand, title, gtin, matched_ck),
                    }
                    await _flag_review(
                        door, ctx, matched_ck, "gtin_match_brand_title_drift", detail
                    )
                    return await _finish(
                        action=ACTION_FLAG, content_key=matched_ck,
                        product_group_id=await _attach_pg(row, matched_ck, strict=strict_pg),
                        matcher="gtin_match_brand_title_drift", door=door,
                        merchant_ctx=ctx, detail=detail, gtin=gtin14,
                        attach=_attach_info(row, merchant_id),
                    )
                return await _finish(
                    action=ACTION_ATTACH, content_key=matched_ck,
                    product_group_id=await _attach_pg(row, matched_ck, strict=strict_pg),
                    matcher="gtin_match", door=door, merchant_ctx=ctx,
                    detail={
                        **_base_detail(),
                        "matched_product_key": row.get("product_key"),
                        "deposit_basis": _deposit_basis(brand, title, gtin, matched_ck),
                    },
                    gtin=gtin14, attach=_attach_info(row, merchant_id),
                )

        # -- Tier-0a, widened (F2, 2026-10-10): the same GS1 evidence, read where the crawl lane
        # actually keeps it -- the listing's variant barcodes and existing rows' SKU barcodes (see
        # the block comment at gtin_spellings). Narrower than the product-gtin match above on
        # purpose, because it is new and runs at every enabled door:
        #   - every barcode the reuse guard refused is ignored;
        #   - it attaches only when the trusted barcodes resolve to exactly ONE family; two
        #     families (seller B lists per shade what seller A lists as one family, or two
        #     sellers disagree) is FLAGged, not guessed;
        #   - only a NEW listing: one already stored (its own row is among the hits) or already
        #     in a product group is left where it is -- moving existing listings is
        #     attach_membership's job (scripts/propose_variant_barcode_links.py), as for Tier-0e,
        #     and a crawl that resolved a grouped listing elsewhere would have its group refused
        #     and fail the whole store job (_ensure_primary_retailer_group).
        candidates: List[Dict[str, Any]] = []
        if widened and barcode_note.get("widened_lookup") != "truncated":
            candidates = _sorted_candidates(
                [r for r in widened if r.get("matched_gtin") not in untrusted], merchant_id
            )
        if candidates:
            families = sorted({r["content_key"] for r in candidates if r.get("content_key")})
            matched_codes = sorted({r["matched_gtin"] for r in candidates})
            existing = any(str(r.get("product_key") or "") == (own_pk or "") for r in candidates) or (
                await _listing_has_membership(ctx, source_product_id)
            )
            if existing:
                barcode_note["variant_barcode_match"] = "existing_listing_left_to_backfill"
            elif len(families) > 1:
                barcode_note["variant_barcode_match"] = "ambiguous_family"
                barcode_note.setdefault("flag_matcher", "variant_barcode_ambiguous_family")
                detail = {
                    **_base_detail(),
                    "reason": "variant_barcode_ambiguous_family",
                    "matched_barcodes": matched_codes,
                    "candidate_content_keys": families[:10],
                    "conflict_product_key": candidates[0].get("product_key"),
                }
                await _flag_review_once(door, ctx, content_key, "variant_barcode_ambiguous_family", detail)
            elif families:
                matched_ck = families[0]
                row = next(r for r in candidates if r.get("content_key") == matched_ck)
                detail = {
                    **_base_detail(),
                    "matched_product_key": row.get("product_key"),
                    "matched_barcodes": matched_codes,
                    "match_source": row.get("match_source"),
                    "deposit_basis": _deposit_basis(brand, title, gtin, matched_ck),
                }
                matcher = "variant_barcode_match"
                action = ACTION_ATTACH
                if content_key and matched_ck != content_key:
                    # As the product-gtin drift branch: the barcode is authoritative across
                    # sellers, the wording difference is surfaced for review.
                    matcher, action = "variant_barcode_match_brand_title_drift", ACTION_FLAG
                    detail.update({
                        "reason": matcher,
                        "conflict_product_key": row.get("product_key"),
                        "matched_content_key": matched_ck,
                        "incoming_content_key": content_key,
                    })
                    await _flag_review_once(door, ctx, matched_ck, matcher, detail)
                return await _finish(
                    action=action, content_key=matched_ck,
                    product_group_id=await _attach_pg(row, matched_ck, strict=strict_pg),
                    matcher=matcher, door=door, merchant_ctx=ctx, detail=detail,
                    gtin=gtin14, attach=_attach_info(row, merchant_id),
                )

        if not content_key:
            # No brand+title identity and no GTIN attach — honest absence (the
            # door keeps today's content_key-NULL behavior; pg stays NULL). We
            # never mint an identity from a GTIN alone.
            return await _finish(
                action=ACTION_MINT, content_key=None, product_group_id=None,
                matcher=None, door=door, merchant_ctx=ctx, gtin=gtin14,
                detail={**_base_detail(), "reason": "no_identity_inputs"},
            )

        # -- Tier-0b: content_key (brand+title FAMILY key).
        rows = await _rows_by_content_key(content_key, merchant_id)
        if rows:
            known_gtins = {r.get("gtin") for r in rows if r.get("gtin")}
            if gtin14 and known_gtins and gtin14 not in known_gtins:
                # Same brand+title, but a DIFFERENT GTIN already lives on this
                # family key → two distinct products colliding on the
                # deliberately non-unique family key (ADR-010 two-grain:
                # family=content_key, variant=gtin). The deterministic hash
                # can't fork, so the row still lands under this family key,
                # discriminated by its own gtin attribute + the deposit gate
                # downstream. FLAG for disambiguation — never silent.
                detail = {
                    **_base_detail(),
                    "reason": "brand_title_collision_distinct_gtin",
                    "conflict_product_key": rows[0].get("product_key"),
                    "existing_gtins": sorted(g for g in known_gtins if g),
                }
                await _flag_review(
                    door, ctx, content_key, "brand_title_collision", detail
                )
                return await _finish(
                    action=ACTION_FLAG, content_key=content_key,
                    product_group_id=_singleton_pg(content_key),
                    matcher="brand_title_collision", door=door, merchant_ctx=ctx,
                    detail=detail, gtin=gtin14,
                )
            row = rows[0]
            ck = row.get("content_key") or content_key
            return await _finish(
                action=ACTION_ATTACH, content_key=ck,
                product_group_id=await _attach_pg(row, ck, strict=strict_pg),
                matcher="content_key", door=door, merchant_ctx=ctx,
                detail={
                    **_base_detail(),
                    "matched_product_key": row.get("product_key"),
                    "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                },
                gtin=gtin14, attach=_attach_info(row, merchant_id),
            )

        # -- Tier-0c: canonical_url exact (the ER gate's matcher, unchanged:
        # unique normalized-equality hit or nothing).
        if canonical_url:
            from urllib.parse import urlparse

            from services.pdp_matcher.deterministic import (
                canonical_url_match,
                normalize_canonical_url,
            )

            normalized = normalize_canonical_url(canonical_url)
            path_only = (urlparse(normalized).path or "") if normalized else ""
            candidates = (
                await _candidates_by_canonical_url(path_only) if path_only else []
            )
            match = (
                canonical_url_match(
                    seed={"canonical_url": canonical_url}, candidates=candidates
                )
                if candidates
                else None
            )
            if match:
                row = next(
                    c for c in candidates
                    if c.get("product_key") == match.get("product_key")
                )
                if row.get("content_key"):
                    ck = row["content_key"]
                    return await _finish(
                        action=ACTION_ATTACH, content_key=ck,
                        product_group_id=await _attach_pg(row, ck, strict=strict_pg),
                        matcher="canonical_url_match", door=door,
                        merchant_ctx=ctx,
                        detail={
                            **_base_detail(),
                            "matched_product_key": row.get("product_key"),
                            "matcher_evidence": match.get("evidence"),
                            "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                        },
                        gtin=gtin14, attach=_attach_info(row, merchant_id),
                    )

        # -- Tier-0d: source_product_id exact, SAME-merchant scope only.
        if source_product_id and merchant_id:
            from services.pdp_matcher.deterministic import source_product_id_match

            candidates = await _candidates_by_source_id(
                str(source_product_id), merchant_id
            )
            match = (
                source_product_id_match(
                    seed={"external_product_id": source_product_id},
                    candidates=candidates,
                )
                if candidates
                else None
            )
            if match:
                row = next(
                    c for c in candidates
                    if c.get("product_key") == match.get("product_key")
                )
                if row.get("content_key"):
                    ck = row["content_key"]
                    return await _finish(
                        action=ACTION_ATTACH, content_key=ck,
                        product_group_id=await _attach_pg(row, ck, strict=strict_pg),
                        matcher="source_product_id_match", door=door,
                        merchant_ctx=ctx,
                        detail={
                            **_base_detail(),
                            "matched_product_key": row.get("product_key"),
                            "matcher_evidence": match.get("evidence"),
                            "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                        },
                        gtin=gtin14, attach=_attach_info(row, merchant_id),
                    )

        # -- Tier-0e: a retailer LISTING whose title only prefixes the brand's name
        # ("Missha Artemisia Calming Ampoule" at a retailer, "Artemisia Calming
        # Ampoule" on misshaus.com). The brand prefix never changes which product
        # this is, so the brand-stripped title's family key is tried -- and
        # attached ONLY when that family already holds a non-retailer row (the
        # brand's own product), so a listing joins the brand's product as another
        # seller instead of minting a second product. Sizes are NOT stripped
        # (normalize_title: sizes are identity). Measured 2026-09-26: 51 live
        # retailer listings are the brand store's product under this rule.
        #
        # LAST among the exact tiers, and only for a listing with NO group membership yet (review of
        # #2386): an existing listing must keep matching its own row through canonical_url /
        # source_product_id even when its title or brand spelling drifted, and its membership is never
        # overwritten (_ensure_primary_retailer_group refuses a different group, which fails the whole
        # store job). Moving existing listings onto the brand's product is the identity engine's
        # attach_membership, not a crawl. _rows_by_content_key returns 5 rows, the caller's merchant first,
        # then oldest: crawl-lane brand rows and listings share one synthetic merchant, so the brand row --
        # older than every listing attached to it -- stays in view; a family with 5+ older same-merchant
        # rows could hide it, and then this tier simply does not attach (today's behaviour).
        stripped = _brand_stripped_content_key(brand, title) if _is_retailer_listing_ctx(door, ctx) else None
        if stripped and await _listing_has_membership(ctx, source_product_id):
            stripped = None  # the membership lookup runs only for a title that strips at all
        if stripped:
            family = await _rows_by_content_key(stripped, merchant_id)
            known_gtins = {r.get("gtin") for r in family if r.get("gtin")}
            brand_row = next((r for r in family if not _is_retailer_listing_key(r.get("product_key"))), None)
            if brand_row is not None and not (gtin14 and known_gtins and gtin14 not in known_gtins):
                ck = brand_row.get("content_key") or stripped
                return await _finish(
                    action=ACTION_ATTACH, content_key=ck,
                    product_group_id=await _attach_pg(brand_row, ck, strict=strict_pg),
                    matcher="brand_prefix_title", door=door, merchant_ctx=ctx,
                    detail={
                        **_base_detail(),
                        "matched_product_key": brand_row.get("product_key"),
                        "stripped_content_key": stripped,
                        "deposit_basis": _deposit_basis(brand, title, gtin, ck),
                    },
                    gtin=gtin14, attach=_attach_info(brand_row, merchant_id),
                )

        # -- No exact match → ADR-008 / P1.4 brand-fragmentation guard, now
        # uniform across ALL five doors (extends the guard to doors 3/4).
        # merchant_ctx["brand_guard_memo"] (a set) keeps door 1's once-per-
        # brand-per-run economy.
        guard_action = "proceed"
        guard_detail: Dict[str, Any] = {}
        memo = ctx.get("brand_guard_memo")
        brand_key = str(brand or "").strip().lower()
        memo_hit = isinstance(memo, set) and brand_key and brand_key in memo
        if merchant_id and brand_key and not memo_hit:
            if isinstance(memo, set):
                memo.add(brand_key)
            from services.audit_index_intake import (
                apply_intake_brand_fragmentation_guard,
            )

            guard = await apply_intake_brand_fragmentation_guard(
                merchant_id,
                {
                    "product_key": ctx.get("product_key"),
                    "brand": brand,
                    "source_domain": ctx.get("source_domain"),
                    "canonical_url": canonical_url,
                    "content_key": content_key,
                },
                door=door,
                block_on_conflict=_DOOR_BLOCKS_ON_BRAND_CONFLICT.get(door, True),
            )
            guard_action = guard.get("action") or "proceed"
            guard_detail = {
                "conflict_product_key": guard.get("conflict_product_key"),
                "conflict_merchant_id": guard.get("conflict_merchant_id"),
            }

        if guard_action == "skip":
            return await _finish(
                action=ACTION_SKIP, content_key=content_key,
                product_group_id=None, matcher="brand_host_fragmentation",
                door=door, merchant_ctx=ctx, gtin=gtin14,
                detail={**_base_detail(), "reason": "brand_fragmentation", **guard_detail},
            )
        if guard_action == "flag":
            return await _finish(
                action=ACTION_FLAG, content_key=content_key,
                product_group_id=_singleton_pg(content_key),
                matcher="brand_host_fragmentation", door=door, merchant_ctx=ctx,
                gtin=gtin14,
                detail={**_base_detail(), "reason": "brand_fragmentation", **guard_detail},
            )

        if barcode_note.get("flag_matcher"):
            # Tier-0a declined a barcode match and raised a review (reuse or ambiguity): the row
            # mints its own family as MINT would, but the outcome says FLAG, not a clean MINT.
            return await _finish(
                action=ACTION_FLAG, content_key=content_key,
                product_group_id=_singleton_pg(content_key),
                matcher=barcode_note["flag_matcher"], door=door, merchant_ctx=ctx,
                gtin=gtin14,
                detail={
                    **_base_detail(),
                    "reason": barcode_note["flag_matcher"],
                    "deposit_basis": _deposit_basis(brand, title, gtin, content_key),
                },
            )

        return await _finish(
            action=ACTION_MINT, content_key=content_key,
            product_group_id=_singleton_pg(content_key), matcher=None,
            door=door, merchant_ctx=ctx, gtin=gtin14,
            detail={
                **_base_detail(),
                "deposit_basis": _deposit_basis(brand, title, gtin, content_key),
            },
        )
    except Exception as exc:  # noqa: BLE001 — fail-open: never block intake
        logger.warning(
            "resolve_or_attach_content_identity failed door=%s merchant=%s: %s",
            door, merchant_id, str(exc)[:300],
        )
        return {
            "content_key": content_key,
            "product_group_id": None,
            "action": ACTION_MINT,
            "gtin": gtin14,
            "evidence": {
                "door": door,
                "action": ACTION_MINT,
                "matcher": None,
                "merchant_id": merchant_id,
                "evidence": {"reason": "error", "error": str(exc)[:300]},
            },
            "attach": None,
        }
