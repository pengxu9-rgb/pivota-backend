"""Reviewed seed batch: add reviewed variants to EXISTING active US seeds, from a private, pinned batch manifest.

This is the reviewed one-seed apply/rollback core (2026-11-18), with its per-seed constants moved into manifest
entries, so later batches need a data review only. The code never names a real seed, seller or product: every
identifier comes from the mounted manifest. See scripts/ops/reviewed_seed_batch.py for the job entry.

One write shape (v1): an existing active US seed, attached to a serving canonical product of its own observed
seller, gains N reviewed variants (native id, GTIN-14, SKU, shade) at the listing's one USD price. Each entry is
ONE SERIALIZABLE transaction: lock -> snapshot -> verify the plan's preimage digests -> collision check -> seed
variant write -> SKU/offer projection for exactly those variants -> same-PDP refresh -> exact readback.

Batch manifest (schema_version 1, at most MAX_ENTRIES entries):
  {"schema_version": 1, "batch_id": "<slug>", "market": "US", "currency": "USD", "entries": [{
     "seed_id": "eps_<24 hex>", "product_key": "prod::<seller>::external_seed::<ext id>", "seller_ref": "merch_obs_<16 hex>",
     "signature": "sig_<32 hex>", "group_id": "pg_<32 hex>", "content_key": "ck_<32 hex>",
     "listing": {"url": "https://<store>/products/<handle>", "native_product_id": <int>, "price_minor": <int>,
                 "brand": "<brand>", "title": "<title>"},
     "existing": [["<native id>", "<GTIN-14>", "<shade>", "<SKU>"], ...],
     "additions": [["<native id>", "<GTIN-14>", "<shade>", "<SKU>"], ...]}]}
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re

SCHEMA_VERSION = 1
MAX_ENTRIES = 5
MAX_ADDITIONS = 40
MANIFEST_LIMIT = 262144
PLAN_MAX_AGE = timedelta(minutes=60)
OPERATION = "reviewed_seed_batch_variant_expansion"
PROPOSER = "reviewed_seed_batch"

_SEED = re.compile(r"eps_[0-9a-f]{24}\Z")
_SELLER = re.compile(r"merch_obs_[0-9a-f]{16}\Z")
_SIGNATURE = re.compile(r"sig_[0-9a-f]{32}\Z")
_GROUP = re.compile(r"pg_[0-9a-f]{32}\Z")
_CONTENT = re.compile(r"ck_[0-9a-f]{32}\Z")
_EXTERNAL = re.compile(r"ext_[0-9a-f]{24}\Z")
_BATCH = re.compile(r"[a-z0-9][a-z0-9-]{2,62}\Z")
_NATIVE = re.compile(r"[1-9][0-9]{0,23}\Z")
_SKU = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")


class Refused(RuntimeError):
    """A guard refused. Before a transaction opens, nothing was read for writing; inside one, it rolls back."""


def require(test, message):
    if not test:
        raise Refused(message)


def compact(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return sha256(compact(value).encode()).hexdigest()


def body(value):
    return json.loads(value) if isinstance(value, str) else value


def _unique_object(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Refused("duplicate manifest key")
    return dict(pairs)


@dataclass(frozen=True)
class Entry:
    seed_id: str
    product_key: str
    seller_ref: str
    signature: str
    group_id: str
    content_key: str
    url: str
    native_product_id: int
    price_minor: int
    brand: str
    title: str
    existing: tuple[tuple[str, str, str, str], ...]
    additions: tuple[tuple[str, str, str, str], ...]

    @property
    def external_product_id(self) -> str:
        return self.product_key.rsplit("::", 1)[1]

    def landing(self, native: str) -> str:
        return f"{self.url}?variant={native}"


def _variants(raw, what, *, limit):
    from services.catalog_identity import validated_source_gtin

    require(isinstance(raw, list) and 0 < len(raw) <= limit, f"entry {what} must list 1..{limit} variants")
    checked = []
    for row in raw:
        require(isinstance(row, list) and len(row) == 4 and all(isinstance(item, str) for item in row),
                f"entry {what} variant must be [native id, GTIN-14, shade, SKU]")
        native, gtin, shade, sku = row
        require(_NATIVE.fullmatch(native) and len(gtin) == 14 and gtin[0] == "0" and validated_source_gtin(gtin) == gtin
                and 0 < len(shade.strip()) <= 80 and shade == shade.strip() and _SKU.fullmatch(sku),
                f"entry {what} variant has an invalid native id, GTIN-14, shade or SKU")
        checked.append((native, gtin, shade, sku))
    return tuple(checked)


def _entry(raw) -> Entry:
    fields = {"seed_id", "product_key", "seller_ref", "signature", "group_id", "content_key", "listing", "existing",
              "additions"}
    require(isinstance(raw, dict) and raw.keys() == fields, "entry has missing or extra fields")
    listing = raw["listing"]
    require(isinstance(listing, dict) and listing.keys() == {"url", "native_product_id", "price_minor", "brand", "title"},
            "entry listing has missing or extra fields")
    for key, pattern in (("seed_id", _SEED), ("seller_ref", _SELLER), ("signature", _SIGNATURE), ("group_id", _GROUP),
                         ("content_key", _CONTENT)):
        require(isinstance(raw[key], str) and pattern.fullmatch(raw[key]), f"entry {key} is invalid")
    parts = raw["product_key"].split("::") if isinstance(raw["product_key"], str) else []
    require(len(parts) == 4 and parts[0] == "prod" and parts[1] == raw["seller_ref"] and parts[2] == "external_seed"
            and _EXTERNAL.fullmatch(parts[3]), "entry product_key is not this seller's external seed product")
    # The serving loader's own rule, so a plan never passes a url web would refuse to mount.
    from services.external_seed_review_pilot import is_product_page_url

    require(is_product_page_url(listing["url"]), "entry listing url is not an https product page")
    require(type(listing["native_product_id"]) is int and 0 < listing["native_product_id"] < 10 ** 20,
            "entry listing native_product_id is invalid")
    require(type(listing["price_minor"]) is int and 0 < listing["price_minor"] < 10 ** 7, "entry listing price is invalid")
    for key in ("brand", "title"):
        require(isinstance(listing[key], str) and 0 < len(listing[key].strip()) <= 120, f"entry listing {key} is invalid")
    existing = _variants(raw["existing"], "existing", limit=MAX_ADDITIONS)
    additions = _variants(raw["additions"], "additions", limit=MAX_ADDITIONS)
    every = existing + additions
    for index, what in ((0, "native id"), (1, "GTIN"), (3, "SKU")):
        require(len({row[index] for row in every}) == len(every), f"entry repeats a {what}")
    return Entry(raw["seed_id"], raw["product_key"], raw["seller_ref"], raw["signature"], raw["group_id"],
                 raw["content_key"], listing["url"], listing["native_product_id"], listing["price_minor"],
                 listing["brand"], listing["title"], existing, additions)


def parse_manifest(data: bytes) -> tuple[str, list[Entry]]:
    """(batch_id, entries) from the manifest bytes, or Refused naming the first problem."""
    require(isinstance(data, bytes) and 0 < len(data) <= MANIFEST_LIMIT, "batch manifest is empty or too large")
    try:
        raw = json.loads(data, object_pairs_hook=_unique_object)
    except ValueError as error:
        raise Refused(f"batch manifest is not JSON: {error}") from None
    require(isinstance(raw, dict) and raw.keys() == {"schema_version", "batch_id", "market", "currency", "entries"},
            "batch manifest must be {schema_version, batch_id, market, currency, entries}")
    require(type(raw["schema_version"]) is int and raw["schema_version"] == SCHEMA_VERSION, "unsupported schema_version")
    require(isinstance(raw["batch_id"], str) and _BATCH.fullmatch(raw["batch_id"]), "batch_id is invalid")
    require((raw["market"], raw["currency"]) == ("US", "USD"), "v1 batches are US/USD only")
    require(isinstance(raw["entries"], list) and 0 < len(raw["entries"]) <= MAX_ENTRIES,
            f"a batch lists 1..{MAX_ENTRIES} entries")
    entries = [_entry(row) for row in raw["entries"]]
    for key in ("seed_id", "product_key", "group_id", "content_key"):
        require(len({getattr(e, key) for e in entries}) == len(entries), f"two entries share a {key}")
    natives = [row[0] for e in entries for row in e.existing + e.additions]
    gtins = [row[1] for e in entries for row in e.existing + e.additions]
    require(len(set(natives)) == len(natives) and len(set(gtins)) == len(gtins), "two entries share a variant or GTIN")
    return raw["batch_id"], entries


def serving_listing(entry: Entry):
    """The serving release's listing for this seed: the public LISTINGS entry, else the private named-referral one.
    Its url, seller, native product id and existing variants must be this entry's, for public and private alike."""
    from services.catalog_identity import validated_source_gtin
    from services.external_seed_review_pilot import LISTINGS, NAMED_REFERRAL_LISTINGS

    listing = LISTINGS.get(entry.seed_id) or NAMED_REFERRAL_LISTINGS.get(entry.seed_id)
    require(listing is not None, "the serving release has no reviewed listing for an entry's seed")
    require((listing.url, listing.seller_ref, listing.native_product_id) ==
            (entry.url, entry.seller_ref, entry.native_product_id), "entry listing differs from the serving listing")
    require({(n, validated_source_gtin(g), sku) for n, g, _, sku in listing.variants} ==
            {(n, g, sku) for n, g, _, sku in entry.existing}, "entry's existing variants differ from the serving listing")
    require(not listing.price_minor or listing.price_minor == entry.price_minor,
            "entry price differs from the serving listing's reviewed price")
    return listing


def pilot_listing(entry: Entry, *, variants):
    """The storefront-evidence reader's listing: the SERVING listing (so every pin it carries, such as the shop
    identity, shipping source, seller alias and corroboration, still applies), with this entry's additions."""
    from dataclasses import replace

    return replace(serving_listing(entry), variants=tuple(variants), price_minor=entry.price_minor)


def seed_variant_rows(entry: Entry) -> list[dict]:
    """The seed `variants` items appended for the additions (same shape the reviewed one-seed apply wrote)."""
    price = entry.price_minor / 100
    return [{"variant_id": native, "title": shade, "sku": sku, "barcode": gtin[-13:], "option_name": "SHADE",
             "option_value": shade, "url": entry.landing(native), "price_amount": price, "price_currency": "USD",
             "availability": "in_stock", "in_stock": True}
            for native, gtin, shade, sku in entry.additions]


def named_referral_manifest(entries: list[Entry], public_listings) -> dict:
    """The serving manifest (schema v2) that pins exactly these additions; one source of truth with the batch."""
    listings = {}
    for entry in entries:
        item = {"additions": {native: [gtin, sku] for native, gtin, _, sku in entry.additions}}
        if entry.seed_id not in public_listings:
            item["listing"] = {"url": entry.url, "seller_ref": entry.seller_ref,
                               "native_product_id": entry.native_product_id,
                               "variants": [list(row) for row in entry.existing]}
        listings[entry.seed_id] = item
    return {"schema_version": 2, "listings": listings}


# ---------------------------------------------------------------- reads shared by plan and apply

PARENT_SQL = """
    SELECT p.pivota_signature_id,p.content_key,g.product_group_id,m.status,i.serving_eligible,p.sync_status,p.platform
    FROM catalog_products p
    JOIN product_group_members g ON g.merchant_id=p.merchant_id AND g.platform=p.platform
       AND g.platform_product_id=p.source_product_id AND g.is_primary
    JOIN index_pipeline_state i ON i.content_key=p.content_key
    JOIN agent_pdp_view a ON a.content_key=p.content_key
    JOIN catalog_merchants m ON m.merchant_id=p.merchant_id
    WHERE p.product_key=:pk
"""

COLLISION_SQL = """
    SELECT EXISTS(
      SELECT 1 FROM catalog_skus s
      WHERE s.sku_key=ANY(:keys) OR (s.product_key=:pk AND s.source_variant_id=ANY(:native))
      UNION ALL
      SELECT 1 FROM catalog_skus s
      JOIN catalog_products p USING (product_key)
      JOIN index_pipeline_state i ON i.content_key=p.content_key AND i.serving_eligible
      WHERE s.barcode=ANY(:gtins) AND s.product_key<>:pk AND s.suppressed_at IS NULL
        AND s.suppression_reason IS NULL AND p.suppressed_at IS NULL AND p.suppression_reason IS NULL
    )
"""


async def lock_scope(db, entry: Entry, *, lock: bool) -> dict:
    from services.catalog_variant_offer_projection import MIRROR, PRODUCT_SQL, SEED_SQL, seed_scope

    suffix = " FOR UPDATE" if lock else ""
    product = await db.fetch_one(PRODUCT_SQL + suffix, {"pk": entry.product_key, "src": MIRROR})
    require(product is not None and (product["merchant_id"], product["source_ref"]) == (entry.seller_ref, entry.seed_id),
            "canonical product is absent or changed")
    seed = await db.fetch_one(SEED_SQL + suffix, {"pk": entry.product_key, "seed_id": entry.seed_id})
    scope = seed_scope(dict(product), dict(seed)) if seed is not None else None
    require(scope and scope.get("seed_market") == "US", "active US seed is absent or changed")
    parent = await db.fetch_one(PARENT_SQL, {"pk": entry.product_key})
    require(parent and tuple(parent[key] for key in (
        "pivota_signature_id", "content_key", "product_group_id", "status", "serving_eligible", "sync_status",
    )) == (entry.signature, entry.content_key, entry.group_id, "observed", True, "live"),
        "canonical product, merchant or view drifted")
    return {"product": {**dict(product), **scope, "platform": parent["platform"], "source_system": MIRROR},
            "seed": dict(seed)}


async def snapshot(db, entry: Entry, *, lock: bool) -> dict:
    suffix = " FOR UPDATE" if lock else ""
    seed = body(await db.fetch_val("SELECT seed_data FROM external_product_seeds WHERE id=:seed", {"seed": entry.seed_id}))
    skus = [body(row["data"]) for row in await db.fetch_all(
        "SELECT to_jsonb(s)::text AS data FROM catalog_skus s WHERE s.product_key=:pk ORDER BY s.sku_key" + suffix,
        {"pk": entry.product_key})]
    offers = [body(row["data"]) for row in await db.fetch_all(
        "SELECT to_jsonb(o)::text AS data FROM catalog_offers o WHERE o.product_key=:pk ORDER BY o.offer_id" + suffix,
        {"pk": entry.product_key})]
    return {"seed": seed, "skus": skus, "offers": offers}


def original_view(state: dict, entry: Entry) -> dict:
    old = {row[0] for row in entry.existing}
    skus = [sku for sku in state["skus"] if sku["source_variant_id"] in old
            and sku["suppressed_at"] is None and sku["suppression_reason"] is None]
    return {"seed_variant_array": state["seed"]["variants"], "original_skus": skus,
            "existing_product_offers": state["offers"]}


def rollback_digests(state: dict, entry: Entry) -> dict:
    view = original_view(state, entry)
    return {"preimage_sha256": digest(view), "original_seed_variant_array": view["seed_variant_array"],
            "original_sku_rows_sha256": digest(view["original_skus"]),
            "existing_offer_rows_sha256": digest(view["existing_product_offers"])}


def verify_original(state: dict, entry: Entry, rollback: dict) -> None:
    view = original_view(state, entry)
    require(len(view["original_skus"]) == len(entry.existing) and rollback_digests(state, entry) == rollback,
            "production preimage differs from the planned baseline")


async def check_collisions(db, entry: Entry, planned: dict) -> None:
    gtins = sorted({gtin for _, gtin, _, _ in entry.additions} | {gtin[-13:] for _, gtin, _, _ in entry.additions})
    require(not await db.fetch_val(COLLISION_SQL, {
        "keys": sorted(row["sku_key"] for row in planned.values()), "pk": entry.product_key,
        "native": sorted(planned), "gtins": gtins,
    }), "new native SKU or serving GTIN collision")


# ---------------------------------------------------------------- plan (read only)

async def plan_entry(db, entry: Entry) -> dict:
    """Read-only. Caller holds a REPEATABLE READ READ ONLY transaction on the pinned production primary."""
    from services.catalog_identity import validated_source_gtin
    from services.catalog_offer_writer_guard import validate_catalog_offer_rows
    from services.catalog_variant_offer_projection import EXISTING_SQL, MIRROR, SKUS_SQL, TEMPLATES_SQL, plan_offers
    from services.catalog_variant_promoter import build_variant_row

    scope = await lock_scope(db, entry, lock=False)
    product, seed = scope["product"], scope["seed"]
    before = body(seed["seed_data"])["variants"]
    old = {native: (gtin, sku) for native, gtin, _, sku in entry.existing}
    require(len(before) == len(old) and
            {str(r["variant_id"]): (validated_source_gtin(r["barcode"]), r["sku"]) for r in before} == old,
            "the seed's current variants differ from the manifest's existing variants")
    baseline = [dict(row) for row in await db.fetch_all(SKUS_SQL, {"pk": entry.product_key, "merchant": entry.seller_ref})]
    require(len(baseline) == len(old) and {r["source_variant_id"] for r in baseline} == old.keys(),
            "production has extra or missing native SKU rows")
    additions = seed_variant_rows(entry)
    planned_skus = [asdict(build_variant_row(variant=row, primary=product)) for row in additions]
    planned_keys = {row["source_variant_id"]: row["sku_key"] for row in planned_skus}
    templates = [dict(row) for row in await db.fetch_all(TEMPLATES_SQL, {
        "pk": entry.product_key, "canonical": entry.product_key + "::canonical", "merchant": entry.seller_ref,
        "mirror_offer_id": "offer:external_seed:" + sha256(entry.product_key.encode()).hexdigest()[:32],
        "mirror_source": MIRROR,
    })]
    existing = [dict(row) for row in await db.fetch_all(EXISTING_SQL, {"pk": entry.product_key})]
    baseline_keys = {r["sku_key"] for r in baseline}
    protected = [r for r in existing if r["sku_key"] in baseline_keys and r["merchant_id"] == entry.seller_ref
                 and (r["market"], r["currency"]) == ("US", "USD") and r["suppressed_at"] is None
                 and r["suppression_reason"] is None]
    require(len(protected) == len(old) and {r["sku_key"] for r in protected} == baseline_keys,
            "production does not have one live US/USD offer per existing variant")
    proposed, skips = plan_offers(product, before + additions, baseline + planned_skus, templates, existing)
    accepted, rejects, _ = validate_catalog_offer_rows(proposed, existing_sku_keys=set(planned_keys.values()))
    require(not rejects and len(accepted) == len(entry.additions)
            and {r["sku_key"] for r in accepted} == set(planned_keys.values())
            and all((r["market"], r["currency"], r["product_key"], r["merchant_id"], r["list_price"], r["availability"]) ==
                    ("US", "USD", entry.product_key, entry.seller_ref, entry.price_minor / 100, "in_stock")
                    for r in accepted), "offer projection differs from exactly the reviewed US additions")
    require(not await db.fetch_val(COLLISION_SQL, {
        "keys": sorted(planned_keys.values()), "pk": entry.product_key, "native": sorted(planned_keys),
        "gtins": sorted({g for _, g, _, _ in entry.additions} | {g[-13:] for _, g, _, _ in entry.additions}),
    }), "new native SKU or serving GTIN collides with an existing catalog row")
    state = await snapshot(db, entry, lock=False)
    by_key = {row["sku_key"]: row for row in accepted}
    rows = [{"variant": native, "sku": sku, "gtin14": gtin, "sku_key": planned_keys[native],
             "offer_id": by_key[planned_keys[native]]["offer_id"], "price_minor": entry.price_minor,
             "stored_offer_url": body(by_key[planned_keys[native]]["offer_payload"])["destination_url"]}
            for native, gtin, _, sku in entry.additions]
    return {"seed_id": entry.seed_id, "existing_variants": len(old), "added_variants": len(entry.additions),
            "planned_variants": len(old) + len(entry.additions), "skips": skips, "rows": rows,
            "rollback": rollback_digests(state, entry)}


def plan_document(*, batch_id, manifest_sha256, release_sha, execution, finished_at, entries) -> dict:
    return {"scope": OPERATION, "mode": "plan", "batch_id": batch_id, "manifest_sha256": manifest_sha256,
            "release_sha": release_sha, "execution": execution, "finished_at": finished_at,
            "production_writes": 0, "entries": entries}


def check_plan(plan: dict, *, manifest_sha256: str, release_sha: str, entries: list[Entry], now: datetime) -> dict:
    """The reviewed plan this apply may use: same manifest, same release, every entry, and fresh."""
    require(plan.get("scope") == OPERATION and plan.get("mode") == "plan" and plan.get("production_writes") == 0,
            "not a reviewed seed batch plan")
    require(plan.get("manifest_sha256") == manifest_sha256, "plan was made from a different batch manifest")
    require(plan.get("release_sha") == release_sha, "plan was made on a different release")
    finished = datetime.fromisoformat(plan["finished_at"])
    require(finished.tzinfo is not None and timedelta(0) <= now - finished <= PLAN_MAX_AGE,
            "plan is older than the freshness limit; re-plan")
    by_seed = {row["seed_id"]: row for row in plan.get("entries", [])}
    require(list(by_seed) == [e.seed_id for e in entries], "plan entries differ from the batch entries")
    for entry in entries:
        row = by_seed[entry.seed_id]
        require([r["variant"] for r in row["rows"]] == [a[0] for a in entry.additions]
                and [(r["gtin14"], r["sku"]) for r in row["rows"]] == [(a[1], a[3]) for a in entry.additions]
                and (row["existing_variants"], row["added_variants"]) == (len(entry.existing), len(entry.additions)),
                "plan rows differ from the batch additions")
    return by_seed


# ---------------------------------------------------------------- storefront re-read (apply only)

async def confirm_current_storefront(entry: Entry) -> str:
    from services.catalog_identity import validated_source_gtin
    from services.external_seed_review_pilot import capture_shopify_evidence

    listing = pilot_listing(entry, variants=[(n, g[-13:], shade, sku) for n, g, shade, sku in entry.additions])
    result = await capture_shopify_evidence(listing)
    sources = result.get("sources", {})
    require(set(sources) == {f"shopify_us:{native}" for native, *_ in entry.additions},
            f"current seller-native evidence lacks the reviewed rows ({result.get('reason', 'mismatch')})")
    for native, gtin, _, sku in entry.additions:
        source = sources[f"shopify_us:{native}"]
        require((source["country"], source["currency"], source["price_minor"], source["in_stock"], source["merchant_sku"],
                 validated_source_gtin(source["native_barcode"]), source["offer_url"]) ==
                ("US", "USD", entry.price_minor, True, sku, gtin, entry.landing(native)),
                "current seller-native evidence differs from the reviewed row")
    return result["captured_at"]


# ---------------------------------------------------------------- apply / rollback (one entry, one transaction)

def verify_expansion(before: dict, after: dict, entry: Entry, planned: dict) -> None:
    from services.catalog_identity import validated_source_gtin
    from services.catalog_variant_offer_projection import SOURCE

    added = seed_variant_rows(entry)
    skus = {row["sku_key"]: row for row in after["skus"]}
    offers = {row["offer_id"]: row for row in after["offers"]}
    new_skus = {row["sku_key"] for row in planned.values()}
    new_offers = {row["offer_id"] for row in planned.values()}
    require(after["seed"] == {**before["seed"], "variants": before["seed"]["variants"] + added}
            and [r for r in after["skus"] if r["sku_key"] not in new_skus] == before["skus"]
            and [r for r in after["offers"] if r["offer_id"] not in new_offers] == before["offers"]
            and new_skus.isdisjoint(r["sku_key"] for r in before["skus"])
            and new_offers.isdisjoint(r["offer_id"] for r in before["offers"])
            and new_skus <= skus.keys() and new_offers <= offers.keys(), "projection was not exactly additive")
    for native, expected in planned.items():
        sku, offer = skus[expected["sku_key"]], offers[expected["offer_id"]]
        require((sku["source_variant_id"], sku["sku"], validated_source_gtin(sku["barcode"]), sku["merchant_id"],
                 sku["product_key"], sku["suppressed_at"], sku["suppression_reason"]) ==
                (native, expected["sku"], expected["gtin14"], entry.seller_ref, entry.product_key, None, None)
                and (offer["sku_key"], offer["merchant_id"], offer["product_key"], offer["market"], offer["currency"],
                     float(offer["list_price"]), offer["availability"], offer["suppressed_at"], offer["suppression_reason"],
                     offer["source_system"], body(offer["offer_payload"])["destination_url"]) ==
                (expected["sku_key"], entry.seller_ref, entry.product_key, "US", "USD", entry.price_minor / 100,
                 "in_stock", None, None, SOURCE, expected["stored_offer_url"]),
                "projected row differs from the reviewed variant or offer")


async def write_variant_array(entry: Entry, seed: dict, array: list, mode: str, go_reference: str) -> int:
    from services.seed_data_writer import upsert_seed_data

    result = await upsert_seed_data(
        seed_id=entry.seed_id, external_product_id=entry.external_product_id, proposed_seed_data={"variants": array},
        proposer=PROPOSER, source=OPERATION,
        notes=compact({"operation": mode, "seed_id": entry.seed_id, "go_reference": go_reference}),
        project_after_merge=False,
    )
    require(result.status == "merged" and result.proposal_id is not None and seed["variants"] != array,
            "canonical variant writer did not apply the requested change")
    return result.proposal_id


async def remove_expansion(db, entry: Entry, planned: dict) -> None:
    from services.catalog_variant_offer_projection import SOURCE

    keys = sorted(row["sku_key"] for row in planned.values())
    offers = sorted(row["offer_id"] for row in planned.values())
    # Row locks are held by snapshot(). Refuse if decision history or another offer now references the rows.
    require(not await db.fetch_val("""
        SELECT EXISTS(SELECT 1 FROM agent_decision_funnel_links WHERE catalog_offer_id=ANY(:offers))
            OR EXISTS(SELECT 1 FROM catalog_offers WHERE sku_key=ANY(:keys) AND NOT offer_id=ANY(:offers))
    """, {"keys": keys, "offers": offers}), "expanded rows acquired new references since the apply")
    removed_offers = await db.fetch_all("""
        DELETE FROM catalog_offers WHERE offer_id=ANY(:offers) AND sku_key=ANY(:keys)
          AND product_key=:pk AND merchant_id=:seller AND market='US' AND currency='USD' AND source_system=:source
        RETURNING offer_id
    """, {"pk": entry.product_key, "seller": entry.seller_ref, "source": SOURCE, "offers": offers, "keys": keys})
    removed_skus = await db.fetch_all("""
        DELETE FROM catalog_skus WHERE sku_key=ANY(:keys) AND source_variant_id=ANY(:native)
          AND product_key=:pk AND merchant_id=:seller AND platform='external_seed'
        RETURNING sku_key
    """, {"pk": entry.product_key, "seller": entry.seller_ref, "keys": keys, "native": sorted(planned)})
    require(sorted(r["offer_id"] for r in removed_offers) == offers and sorted(r["sku_key"] for r in removed_skus) == keys,
            "rollback affected a different SKU or offer set")


async def run_entry(db, entry: Entry, plan_row: dict, mode: str, *, go_reference: str, observed_at: str | None,
                    receipt: dict | None, server_pins) -> dict:
    """ONE entry in ONE SERIALIZABLE transaction. Returns the entry receipt (apply) or rollback record."""
    from services.catalog_variant_offer_projection import projection_enabled
    from services.catalog_variant_promoter import promote_variants_for_group
    from services.external_seed_review_pilot import EVIDENCE_MAX_AGE
    from services.seed_data_writer import refresh_agent_pdp_view_for_seed

    planned = {row["variant"]: row for row in plan_row["rows"]}
    async with db.transaction():
        await db.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        await db.execute("SET LOCAL statement_timeout='15s'")
        await db.execute("SET LOCAL lock_timeout='2s'")
        await server_pins(db)
        await lock_scope(db, entry, lock=True)
        before = await snapshot(db, entry, lock=True)
        if mode == "apply":
            age = datetime.now(timezone.utc) - datetime.fromisoformat(observed_at)
            require(timedelta(0) <= age <= EVIDENCE_MAX_AGE and projection_enabled(),
                    "fresh store evidence expired or variant offer projection is disabled")
            verify_original(before, entry, plan_row["rollback"])
            await check_collisions(db, entry, planned)
            target = before["seed"]["variants"] + seed_variant_rows(entry)
        else:
            require(receipt is not None and digest(before) == receipt["postimage_sha256"],
                    "applied scope drifted since the apply; use the operator fallback")
            target = plan_row["rollback"]["original_seed_variant_array"]
        proposal_id = await write_variant_array(entry, before["seed"], target, mode, go_reference)
        if mode == "apply":
            result = await promote_variants_for_group(group_id=entry.group_id, apply=True, variant_ids=frozenset(planned))
            require((result.primary_product_key, result.variants_found, result.variant_offers_created,
                     result.variant_offer_projection_failed) ==
                    (entry.product_key, len(planned), len(planned), False)
                    and not (result.skus_identity_conflict or result.skus_write_failed or result.skus_deduped_same_identity),
                    "SKU/offer projector differed from the reviewed additions")
        else:
            await remove_expansion(db, entry, planned)
        require(await refresh_agent_pdp_view_for_seed(seed_id=entry.seed_id, proposal_id=proposal_id,
                                                      refresh_source=OPERATION) == "refreshed",
                "same canonical PDP did not refresh")
        await lock_scope(db, entry, lock=True)
        after = await snapshot(db, entry, lock=True)
        if mode == "apply":
            verify_expansion(before, after, entry, planned)
        else:
            verify_original(after, entry, plan_row["rollback"])
            require(digest(after) == receipt["preimage_sha256"], "rollback left other scope changes")
    return {"seed_id": entry.seed_id, "mode": mode, "preimage_sha256": digest(before), "postimage_sha256": digest(after),
            "proposal_id": proposal_id, "storefront_observed_at": observed_at, "variants": len(target),
            "sku_and_offer_count": len(planned)}


__all__ = [
    "Entry", "MAX_ENTRIES", "OPERATION", "PLAN_MAX_AGE", "Refused", "check_plan", "compact", "confirm_current_storefront",
    "digest", "named_referral_manifest", "parse_manifest", "pilot_listing", "plan_document", "plan_entry",
    "rollback_digests", "run_entry", "seed_variant_rows", "verify_expansion", "verify_original",
]
