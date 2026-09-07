"""No NEW writer may mint a variant id out of the product's identity.

Five sites already do it, and between them they account for 11,811 + 1,473 + 166 prod rows whose
`catalog_skus.source_variant_id` the gateway's `isRestatedProductId` guard refuses to spend against
(measured 2026-09-07). Each of the five had a defensible local reason and no way to see the other
four; the cost only shows up at a checkout, in another repo. This ratchet is where they become
visible to each other.

It pins the METHOD, not a count: the sweep looks for a variant-id sink being assigned from an
expression that carries a product identifier, in any of the forms our writers actually use, and
every known site is registered with the REASON it is allowed rather than by filename — so moving or
renaming a file cannot launder it, and a genuinely new fabrication has to be argued for here.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("services", "scripts", "routes", "jobs")

#: The left-hand sides that end up in catalog_skus.source_variant_id / a seed variant's id.
_SINKS = ("variant_id", "source_variant_id", "shopify_variant_id")

#: Tokens that name the PRODUCT, not a variant of it. An id built out of any of these
#: restates identity the product already had.
_PRODUCT_TOKENS = (
    "product_key", "product_id", "external_product_id", "epid",
    "handle", "source_product_id", "canonical_product_name",
)

#: Sites that write a product-derived id into catalog_skus.source_variant_id — the column the
#: checkout path reads as merchant identity. These are the ones that cost money, and the set is
#: pinned exactly (see test_no_new_writer_puts_a_derived_id_into_catalog_skus): a new one must
#: not be waved through by filing it under a display reason.
MONEY_VISIBLE = {
    (
        "A STORAGE TOKEN to satisfy idx_catalog_skus_source_identity. Every agent SKU shares "
        "merchant_id/platform='external_seed', so source_variant_id must vary per PDP or the "
        "unique index rejects the row. It names the product, not a variant of it, and the "
        "gateway's isRestatedProductId guard is right to refuse it — 11,811 prod rows."
    ): {
        ("services/catalog_enrichment_agent/ingestion.py", "source_variant_id"),
        ("scripts/mirror_external_seeds_to_catalog_products.py", "source_variant_id"),
    },
    (
        "MERCHANT SYNC fallback: `variant.variant_id or variant.id or product_key`, commented "
        "'no variant id: collide only within this product'. This is the first-party Shopify "
        "path, so even a connected merchant's product lands unbuyable identity when the "
        "platform payload carries no variant id. Fixing it needs the merchant lane's own "
        "backfill and is not in scope here."
    ): {("services/catalog_sync_service.py", "source_variant_id")},
    (
        "A repair script deriving a deterministic id from (product_key, canonical_url) so its "
        "rows are idempotent across re-runs. Deterministic is not the same as merchant-issued: "
        "these rows are repairable images, not purchasable identity."
    ): {("scripts/source_pdp_offer_image_repair.py", "source_variant_id")},
}

#: Sites that build a product-derived id for DISPLAY, dedup or attribution and never write
#: catalog_skus. Tolerated, because a serving response still has to key its variants somehow.
DISPLAY_ONLY = {
    (
        "Mints a synthetic default variant so the readiness gate does not score the seed "
        "`zero_variants` and drop it from recall (live incident 2026-07-11: 2,151 seeds "
        "invisible to find_products_multi). Stamped variant_id_provenance=product_derived "
        "and purchasable=False so no consumer has to sniff the string."
    ): {
        ("scripts/onboard_external_brand_from_crawl.py", "variant_id"),
        ("services/catalog_enrichment_agent/ingestion.py", "variant_id"),
    },
    (
        "Serving lanes need a stable per-variant key to render and de-duplicate a response "
        "when the seed's variant carries no id of its own. The value reaches a JSON payload, "
        "never catalog_skus; a buyer cannot transact against it."
    ): {
        ("services/beauty_external_ranking.py", "variant_id"),
        ("routes/agent_api.py", "variant_id"),
        ("routes/agent_sdk_fixed.py", "variant_id"),
        ("routes/agent_v2.py", "variant_id"),
        ("routes/agent_shop_gateway.py", "variant_id"),
        ("routes/employee_products.py", "variant_id"),
    },
    (
        "curated_brand_feed.py falls back to handle:index when a feed entry carries no id of "
        "its own; the value is display/dedup plumbing for the fold lane."
    ): {("services/curated_brand_feed.py", "variant_id")},
    (
        "Attribution fills a missing variant_id from product_id so an event still joins to "
        "something. It records what happened; it never proposes what to buy."
    ): {("services/commerce_attribution_service.py", "variant_id")},
}

ALLOWED = {**MONEY_VISIBLE, **DISPLAY_ONLY}
ALLOWED_PAIRS = {pair for pairs in ALLOWED.values() for pair in pairs}
MONEY_VISIBLE_PAIRS = {pair for pairs in MONEY_VISIBLE.values() for pair in pairs}


def _carries_product_token(node: ast.AST) -> bool:
    """True when the assigned expression mentions something that names the PRODUCT.

    Walks the expression rather than regexing the line, so an f-string, a concatenation,
    a .format() call and a bare `x = product_key` are all caught by the same rule — the
    trap of a ratchet that matches one syntactic form and permits the others.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in _PRODUCT_TOKENS:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in _PRODUCT_TOKENS:
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if sub.value in _PRODUCT_TOKENS:
                return True
    return False


def _sink_names(target: ast.AST):
    """Every variant-id sink this assignment writes: `x = `, `d["source_variant_id"] = `,
    and dict literals `{"source_variant_id": ...}` (handled by the caller)."""
    if isinstance(target, ast.Name) and target.id in _SINKS:
        yield target.id
    elif isinstance(target, ast.Attribute) and target.attr in _SINKS:
        yield target.attr
    elif isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and key.value in _SINKS:
            yield key.value


def _scan(path: pathlib.Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return
    rel = path.relative_to(REPO).as_posix()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            for t in targets:
                for name in _sink_names(t):
                    if _carries_product_token(value):
                        yield rel, name, getattr(node, "lineno", 0)
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in _SINKS:
                    if _carries_product_token(v):
                        yield rel, k.value, getattr(k, "lineno", 0)


def _findings():
    out = set()
    detail = {}
    for d in SCAN_DIRS:
        root = REPO / d
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            for rel, name, lineno in _scan(path):
                out.add((rel, name))
                detail.setdefault((rel, name), []).append(lineno)
    return out, detail


def test_no_unregistered_writer_builds_a_variant_id_from_the_product():
    found, detail = _findings()
    unregistered = found - ALLOWED_PAIRS
    assert not unregistered, (
        "A variant id is being built from the product's own identity at a site that is not "
        "registered in ALLOWED. The gateway's isRestatedProductId guard refuses ids of this "
        "shape, so rows written here can never complete a purchase.\n"
        + "\n".join(
            f"  {rel}:{sorted(detail[(rel, name)])} -> {name}"
            for rel, name in sorted(unregistered)
        )
        + "\n\nIf the value is display/storage plumbing and never reaches checkout, register it "
        "in ALLOWED with the REASON (not the filename) and stamp variant_id_provenance on the "
        "row. If it is meant to be identity, get a real id — see services/variant_identity.py "
        "and services/shopify_variant_identity.py."
    )


def test_no_new_writer_puts_a_derived_id_into_catalog_skus():
    """The subset that costs money, pinned exactly.

    A new site could otherwise be waved past the test above by filing it under one of the
    display reasons. `source_variant_id` is the column checkout reads, so any writer of it
    has to be argued for on its own terms — and this list shrinking is the goal, which is why
    a REMOVED entry fails too rather than silently passing."""
    found, detail = _findings()
    money_found = {(rel, name) for rel, name in found if name == "source_variant_id"}
    assert money_found == MONEY_VISIBLE_PAIRS, (
        "the set of writers putting a product-derived id into catalog_skus.source_variant_id "
        "changed.\n"
        f"  new:     {sorted(money_found - MONEY_VISIBLE_PAIRS)}\n"
        f"  removed: {sorted(MONEY_VISIBLE_PAIRS - money_found)}\n"
        "A new entry needs a real justification here. A removed one means the fabrication is "
        "gone — delete its MONEY_VISIBLE entry so the ratchet tightens behind you."
    )


def test_the_sweep_can_actually_see_a_fabrication():
    """The ratchet's own detection floor. A green sweep is only reassuring if it would
    have caught the thing it claims to catch — assert the analyser fires on each shape
    our five real writers use, so a refactor that silently blinds it fails here."""
    shapes = [
        'variant_id = f"{epid}-default"',
        'variant_id = f"{product_id}_{idx + 1}"',
        'variant_id = str(v.get("id") or f"{handle}:{i}")',
        'row = {"source_variant_id": product_key}',
        'row["source_variant_id"] = product_key',
        'variant_id = product_key + "-canonical"',
        'variant_id = "{}-default".format(external_product_id)',
    ]
    for src in shapes:
        tree = ast.parse(src)
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for name in _sink_names(t):
                        if _carries_product_token(node.value):
                            hits.append(name)
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value in _SINKS:
                        if _carries_product_token(v):
                            hits.append(k.value)
        assert hits, f"the sweep is blind to this fabrication shape: {src!r}"


def test_the_sweep_does_not_fire_on_a_real_id():
    """The positive counterpart — a ratchet that flagged everything would be useless."""
    for src in [
        'variant_id = str(v.get("variant_id") or "").strip()',
        'variant_id = variant["id"]',
        'row = {"source_variant_id": vid[:128]}',
    ]:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    for name in _sink_names(t):
                        assert not _carries_product_token(node.value), f"false positive: {src!r}"
            elif isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value in _SINKS:
                        assert not _carries_product_token(v), f"false positive: {src!r}"


@pytest.mark.parametrize("reason", sorted(ALLOWED))
def test_every_exemption_states_a_reason_not_a_filename(reason):
    assert len(reason) > 60, "an exemption must explain WHY, not name a file"
    assert not re.fullmatch(r"[\w/.\-]+\.py", reason.strip())
