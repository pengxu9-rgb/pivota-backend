#!/usr/bin/env python3
"""A9-4 — Seller-of-record backfill tooling (ADR-009 D4, ratified decision 3).

Retires the `merchant_id='external_seed'` bucket and the pre-A9-3 NULL seller
state, via a parity window (W1 RunFacts pattern) — NEVER a big-bang. This module
is the *tooling*; actually executing it against prod is a separate, founder-
visible step. It is **DRY-RUN BY DEFAULT**; `--execute` is required for any
write, and dry-run is strictly READ-ONLY (safe to run against prod).

Required reading (cited throughout):
  - `docs/adr/ADR-009-seller-of-record-identity.md` §D4 + "Resolved decisions" 3
    + the A9-2 serving amendment under D2.
  - `docs/IDENTITY_REFERENCE.md` — Traps T1 (pipe vs prod::), T3 (tenant/seller
    conflation), T5 (sigs write-once & public), T6 (content_key cross-merchant).

Four phases (increasing risk), each independently reportable:

  1. SEEDS seller_ref backfill (additive, lowest risk). For each
     `external_product_seeds` row with `seller_ref IS NULL`, derive
     (seller_ref, seed_kind) via the SAME logic A9-3 wired for new writes
     (`services.seller_identity.derive_seed_seller` — reused, not reimplemented).
     Unresolvable rows are LEFT NULL and written to the review output — never
     guessed (founder no-fallback directive). This is the kill-path input for
     A9-3's `metadata.seller_ref_missing` metric.

  2. BARE-format attached_product_key repair. Re-derive the
     STORAGE-format `prod::` key from the seed's own fields the way the mirror
     legitimately produces it (`scripts.mirror_external_seeds_to_catalog_products`
     -> `services.external_seed_servability.backlink_seed_to_product`):
     `make_catalog_product_key('external_seed','external_seed', external_product_id)`
     — and ONLY when that key resolves to a real `catalog_products` row. Anything
     undecodable goes to the review queue; NEVER a widened matcher, NEVER a
     partial/third-format key (ADR-009 ratified decision 3; IDENTITY_REFERENCE T1).
     AND NEVER a key that already resolves — see run_barekey. The "~720 rows"
     this once claimed as its cohort were `ext:` keys that resolve perfectly
     well; rewriting them took 364 elected PDPs to HTTP 500 on 2026-08-01. They
     are now SKIPPED to review, so this phase repairs only genuinely dangling
     links, which is a much smaller set than the scan count suggests.

  3. CATALOG re-key (the risky phase). Rows in `catalog_products` with
     `merchant_id='external_seed'` are RE-SUBJECTED to their observed per-brand
     seller (`services.seller_identity.ensure_observed_seller` from the row's
     brand + domain). See "CASCADE DESIGN CHOICE" below — we re-subject the
     `merchant_id` COLUMN only and leave `product_key`/`sku_key` as historical-
     format storage tokens (ADR-009 D4.2 says "re-key `merchant_id`"). Batched
     with per-batch parity verification, ADR-008 fragmentation-guard collision
     routing, sig FROZEN, and a resumable checkpoint.

  4. EDGES check. Count `commerce_attribution_edges WHERE merchant_id='external_seed'`;
     include them in the re-key mapping if >0, else assert-and-log (ADR-009 D4
     notes converted-edge history under the bucket is ~zero).

CASCADE DESIGN CHOICE — re-subject `merchant_id`, keep `product_key` historical
================================================================================
`product_key` is `prod::{merchant}::{platform}::{pid}` (IDENTITY_REFERENCE §2), so
it EMBEDS the merchant. Re-keying `merchant_id` while leaving `product_key` leaves
them inconsistent; re-keying `product_key` cascades to `catalog_skus.sku_key`
(TWO un-parseable generations — Trap T2), `catalog_offers`, ~6 `beauty_*` tables,
`agent_pdp_view`, and every seed's `attached_product_key`.

We choose (b): **re-subject the `merchant_id` column only; treat `product_key`
(and `sku_key`) as opaque historical-format storage tokens.** Evidence:
  - ADR-009 **D4.2 literally says** "re-key `merchant_id` on crawled rows" — the
    subject is `merchant_id`, not `product_key`.
  - IDENTITY_REFERENCE §1: `product_key`/`sku_key` are "merchant-catalog plumbing
    and must not leak into agent-facing or cross-merchant semantics." The DECISION
    layer keys on `content_key`/`product_group_id` — both UNCHANGED by re-subject.
  - Traps T1/T2/T6 mandate "look up, don't parse." Re-keying `product_key` would
    require reconstructing `sku_key` across two generations you must NOT parse.
  - T5: sigs are write-once/public. Option (b)'s catalog UPDATE never references
    `pivota_signature_id`/`pivota_canonical_url` (asserted below + in tests).
  - Because `product_key`/`sku_key`/`content_key` are STABLE under (b), every
    dependent-table re-subject and the seed<->listing `attached_product_key` join
    stay valid with zero key churn.
  - The one live code path that PARSES a merchant out of a product key —
    `services.crawled_inci_ingest.merchant_id_from_product_key`, which feeds
    `beauty_sku_ingredients.merchant_id` at INCI-ingest time — already returns
    `'external_seed'` for these rows today and is not a serving/decision path;
    leaving `product_key` historical keeps it BYTE-IDENTICAL (no NEW regression).
    This is the documented historical-format residue (IDENTITY_REFERENCE §4 A9-4
    note). `routes.agent_shop_gateway._parse_catalog_product_key`'s merchant
    extraction is telemetry-only over `attached_product_key` (unchanged here).

Full product_key re-key (option a) is deliberately NOT implemented: it is strictly
higher risk (PK rewrite + un-parseable sku_key reconstruction across a dozen
tables) with zero decision-layer benefit, and it contradicts D4.2. If the founder
later wants it, it is a separate, reviewed packet.

Usage:
  # dry-run (READ-ONLY) — prints the full plan as JSON + writes a report file
  .venv/bin/python -m scripts.backfill_seller_of_record --db-url "$DATABASE_URL"

  # execute (writes) — required flag; runs the parity window
  .venv/bin/python -m scripts.backfill_seller_of_record --db-url ... --execute

  # scope to specific phases / a smaller batch during canary
  ... --phases seeds,barekey,catalog,edges --batch-size 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("a9_4_seller_backfill")

# The single named constant for the banned bucket (reuse the one seller_identity
# already owns — do not re-declare a second literal).
from services.seller_identity import BANNED_BUCKET_MERCHANT_ID  # noqa: E402
from services.seller_identity import (  # noqa: E402
    ensure_observed_seller_of_record,
    resolve_seed_seller_identity,
)
from services.catalog_sync_service import make_catalog_product_key  # noqa: E402

EXTERNAL_SEED_PLATFORM = "external_seed"  # the mirror's platform literal
SOURCE_SYSTEM_SEEDS = "a9_4_seeds_backfill"
SOURCE_SYSTEM_CATALOG = "a9_4_catalog_rekey"

# R3: the retired founder test rig's Shopify dev store. Its 50 mirror rows are
# the one cohort that must NOT be re-keyed: the rig merchant is status=inactive
# with a non-NULL last_full_sync_at, so a re-key would both drop the rows from
# serving and expose them to the stale-product sweep. They get their own
# suppression step, founder-gated — never a ride-along in the flip.
EXCLUDED_SOURCE_DOMAINS = frozenset({"jwx893-fz.myshopify.com"})


def _plan_domain_candidates(p: Dict[str, Any]) -> List[str]:
    """The row's OWN domain sources, in preference order: catalog
    source_domain -> canonical_url host -> attached seed's domain -> attached
    seed's destination host (design §3: the seed is the row's upstream source
    of truth — 250 Biodance rows carry a marketing tagline in source_domain
    and an empty canonical_url while their seeds hold the real biodance.com)."""
    from urllib.parse import urlparse

    candidates: List[str] = []
    sd = str(p.get("source_domain") or "").strip()
    if sd:
        candidates.append(sd)
    cu = str(p.get("canonical_url") or "").strip()
    if cu:
        host = urlparse(cu).hostname or ""
        if host:
            candidates.append(host)
    seed_dom = str(p.get("seed_domain") or "").strip()
    if seed_dom:
        candidates.append(seed_dom)
    seed_dest = str(p.get("seed_destination_url") or "").strip()
    if seed_dest:
        host = urlparse(seed_dest).hostname or ""
        if host:
            candidates.append(host)
    return candidates


def _plan_domain(p: Dict[str, Any]) -> Tuple[str, bool]:
    """First candidate that actually RESOLVES a seller identity, plus whether
    any did. resolved=False means the row must go to review — the caller must
    NOT fall through to a resolver that lacks the plausible-registrable guard
    (make_observed_merchant_id happily keys on a bare 'com.vn')."""
    candidates = _plan_domain_candidates(p)
    for cand in candidates:
        try:
            resolve_seed_seller_identity(brand=str(p.get("brand") or ""), domain=cand)
            return cand, True
        except ValueError:
            continue
    return (candidates[0] if candidates else ""), False


def _is_excluded_rig_row(p: Dict[str, Any]) -> bool:
    """Any of the row's domain candidates matching the rig set excludes it —
    an exact-source_domain-only match would let a www. variant or a rig row
    whose domain only appears in canonical_url ride into the re-key."""
    for cand in _plan_domain_candidates(p):
        host = cand.strip().lower().removeprefix("www.")
        if host in EXCLUDED_SOURCE_DOMAINS:
            return True
    return False

# Tables that carry a merchant_id/primary_merchant_id but are NOT product-scoped
# ownership rows we may blindly re-subject. Edges get their own phase-4 mapping;
# the others are tenant/economic subjects handled elsewhere or out of scope.
CASCADE_DENYLIST = {
    "catalog_merchants",
    "merchant_onboarding",
    "commerce_attribution_edges",
    "aggregated_outcomes",
    "seller_trust",
    "surface_click_events",
    "orders",
    "external_product_seeds",  # attached_product_key handled in phases 1-2, not merchant_id
    # brand_claims.merchant_id is the CLAIMING TENANT, not a product listing —
    # re-subjecting it would corrupt ownership. It happens to carry a content_key
    # column so reflection would otherwise catch it; the `merchant_id='external_seed'`
    # guard already makes a mutation impossible (no claim is ever under the bucket),
    # but we exclude it explicitly so the plan is honest (IDENTITY_REFERENCE T3:
    # claiming attaches, never re-keys).
    "brand_claims",
    "catalog_products_claim_state",
}

# The catalog_products re-subject is the ONE statement that touches the row owning
# a sig. It is a dedicated constant so `assert_sig_frozen_sql` can pin that it
# NEVER references the frozen columns (IDENTITY_REFERENCE T5 / ADR-009 D4.3).
CATALOG_PRODUCTS_RESUBJECT_SQL = (
    "UPDATE catalog_products SET merchant_id = :obs, updated_at = NOW() "
    "WHERE product_key = :pk AND merchant_id = :banned"
)

_FROZEN_TOKENS = ("pivota_signature", "pivota_canonical_url", "sig_")


def assert_sig_frozen_sql(sql: str) -> None:
    """Guard (ADR-009 D4.3, IDENTITY_REFERENCE T5): a re-key statement must NEVER
    reference the write-once, publicly-GSC-submitted sig / canonical columns.
    Raises loudly rather than let a re-mint slip in."""
    low = sql.lower()
    for tok in _FROZEN_TOKENS:
        if tok in low:
            raise AssertionError(
                f"SIG-FROZEN VIOLATION: re-key SQL references {tok!r}; sigs are "
                f"write-once/public and must never be re-minted by a re-key. SQL={sql!r}"
            )


# Fail-fast at import: the load-bearing constant must be clean.
assert_sig_frozen_sql(CATALOG_PRODUCTS_RESUBJECT_SQL)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a DB)
# ---------------------------------------------------------------------------

def classify_attached_key(key: Optional[str]) -> str:
    """'prod' (storage form), 'null' (NULL/empty), or 'bare' (any other legacy
    encoding). IDENTITY_REFERENCE T1: only the `prod::` double-colon form is
    storage; a pipe/bare token is not interchangeable."""
    s = str(key or "").strip()
    if not s:
        return "null"
    if s.startswith("prod::"):
        return "prod"
    return "bare"


def candidate_storage_key_from_seed(seed_row: Dict[str, Any]) -> Optional[str]:
    """Re-derive the STORAGE-format `prod::` attached key the way the mirror
    legitimately produces it: `make_catalog_product_key('external_seed',
    'external_seed', external_product_id)` (scripts.mirror_external_seeds_to_
    catalog_products; external_seed_servability.backlink_seed_to_product).

    Returns the candidate key, or None when the seed has no usable
    `external_product_id` or the resulting key would exceed the storage width
    (mirror gate: pid <= 128, product_key <= 255). The CALLER must still verify
    the key resolves to a real catalog_products row — a candidate is not a repair
    (no-fallback: never mint a key that points at nothing; ADR-009 decision 3)."""
    epid = str(seed_row.get("external_product_id") or "").strip()
    if not epid or len(epid) > 128:
        return None
    key = make_catalog_product_key(EXTERNAL_SEED_PLATFORM, EXTERNAL_SEED_PLATFORM, epid)
    if len(key) > 255:
        return None
    return key


def _seed_brand(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> str:
    """Brand identity across the shapes `external_product_seeds` actually uses.

    SNAPSHOT FIRST, and that omission is why this phase resolved NOTHING.
    Measured on prod 2026-07-31, over the 2,026 seeds with a NULL `seller_ref`:

        brand at seed_data.brand ........... 0
        brand ONLY at snapshot.brand ... 2,024
        vendor at seed_data.vendor ......... 0
        no brand anywhere .................. 2

    So the three lookups this used to do missed 2,024 of 2,026. A9-4 phase 1 ran
    to completion in `--execute` mode and wrote zero rows — `resolve_seller_readonly`
    refused every seed for an empty brand, and because a missing/unmintable
    identity is DEBUG-quiet under the no-fallback rule (never guess, never invent
    a bucket), it failed silently. The destination was never the problem:
    `_seed_destination` already falls back to `destination_url`, and etld1()
    reduces those to real registrable domains.

    Resolution order matches the repo's established convention for reading seed
    fields — `index_pipeline_state_service._resolve_seed_title`, itself matching
    `external_seed_audit.audit_external_seed_row`: snapshot -> row -> seed_data.
    Order is convention rather than behaviour here (0 rows carry a top-level
    brand today), but a sibling resolver that disagrees on precedence is exactly
    how these shapes drift apart in the first place.
    """
    snapshot = _coerce_seed_data(seed_data.get("snapshot"))
    for candidate in (
        snapshot.get("brand"),
        seed_row.get("brand"),
        seed_data.get("brand"),
        snapshot.get("vendor"),
        seed_data.get("vendor"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if candidate not in (None, ""):
            text = str(candidate).strip()
            if text:
                return text
    return ""


def _seed_destination(seed_row: Dict[str, Any], seed_data: Dict[str, Any]) -> str:
    """A destination identity to key a seller on (etld1() reduces a full URL)."""
    return str(
        seed_row.get("domain")
        or seed_data.get("domain")
        or seed_row.get("destination_url")
        or seed_data.get("destination_url")
        or seed_row.get("canonical_url")
        or seed_data.get("canonical_url")
        or ""
    ).strip()


def _coerce_seed_data(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Read-only seller resolution — a faithful, NON-WRITING shadow of
# services.seller_identity.derive_seed_seller / ensure_observed_seller. It reuses
# seller_identity's OWN pure helpers (etld1, _anchor_owns_domain,
# make_observed_merchant_id, _resolve_claimed_merchant) so the branching cannot
# drift; `tests/test_a9_4_seller_backfill.py` pins equivalence to the real
# derive_seed_seller. Dry-run uses this (no writes); --execute materializes the
# mint via the real ensure_observed_seller.
# ---------------------------------------------------------------------------

@dataclass
class SellerResolution:
    seller_ref: Optional[str]
    seed_kind: Optional[str]         # 'self' | 'cross' | None
    disposition: str                 # no_destination|self|cross_claimed|cross_mint|unresolvable_cross
    would_mint: bool = False


async def resolve_seller_readonly(
    si_mod: Any,
    *,
    anchor_merchant_id: Optional[str],
    brand: Optional[str],
    destination_domain: Optional[str],
) -> SellerResolution:
    """Read-only mirror of derive_seed_seller (order: no-destination -> self ->
    mint-attempt[claim -> observed]). Never writes."""
    registrable = si_mod.etld1(destination_domain)
    if not registrable:
        return SellerResolution(None, None, "no_destination")
    anchor = str(anchor_merchant_id or "").strip() or None
    if anchor and await si_mod._anchor_owns_domain(anchor, registrable):
        return SellerResolution(anchor, "self", "self")
    try:
        observed_id = si_mod.make_observed_merchant_id(brand or "", destination_domain or "")
    except ValueError:
        return SellerResolution(None, None, "unresolvable_cross")
    claimed = await si_mod._resolve_claimed_merchant(registrable)
    if claimed:
        return SellerResolution(claimed, "cross", "cross_claimed")
    existing = await si_mod.database.fetch_one(
        "SELECT merchant_id FROM catalog_merchants WHERE merchant_id = :mid",
        {"mid": observed_id},
    )
    return SellerResolution(observed_id, "cross", "cross_mint", would_mint=not existing)


# ---------------------------------------------------------------------------
# Report structures
# ---------------------------------------------------------------------------

@dataclass
class BackfillReport:
    mode: str
    started_at: str
    phases: List[str]
    batch_size: int
    counts: Dict[str, Any] = field(default_factory=dict)
    seeds: Dict[str, Any] = field(default_factory=dict)
    barekey: Dict[str, Any] = field(default_factory=dict)
    catalog: Dict[str, Any] = field(default_factory=dict)
    edges: Dict[str, Any] = field(default_factory=dict)
    review_queue: Dict[str, List[Any]] = field(default_factory=dict)
    cascade_tables: List[Dict[str, Any]] = field(default_factory=list)
    parity: List[Dict[str, Any]] = field(default_factory=list)
    epilogue: List[str] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "packet": "A9-4",
            "adr": "ADR-009-D4",
            "cascade_design_choice": "resubject_merchant_id_only (product_key historical) — ADR-009 D4.2",
            "mode": self.mode,
            "started_at": self.started_at,
            "phases": self.phases,
            "batch_size": self.batch_size,
            "counts": self.counts,
            "seeds": self.seeds,
            "barekey": self.barekey,
            "catalog": self.catalog,
            "edges": self.edges,
            "review_queue": self.review_queue,
            "cascade_tables": self.cascade_tables,
            "parity": self.parity,
            "epilogue": self.epilogue,
        }


# ---------------------------------------------------------------------------
# The backfiller
# ---------------------------------------------------------------------------

class SellerBackfill:
    def __init__(self, *, database: Any, si_mod: Any, execute: bool, batch_size: int,
                 max_batches: int = 0, max_products: int = 0):
        self.db = database
        self.si = si_mod
        self.max_batches = int(max_batches or 0)
        self.max_products = int(max_products or 0)
        self.execute = execute
        self.batch_size = batch_size

    # -- discovery / scan ----------------------------------------------------

    async def scan_counts(self) -> Dict[str, int]:
        async def _count(sql: str, params: Optional[Dict[str, Any]] = None) -> int:
            # `databases` rejects a bound param the SQL does not reference — only
            # pass :banned to the queries that use it.
            row = await self.db.fetch_one(sql, params or {})
            return int(dict(row)["c"]) if row else 0

        banned = {"banned": BANNED_BUCKET_MERCHANT_ID}
        return {
            "seeds_seller_ref_null": await _count(
                "SELECT count(*) AS c FROM external_product_seeds WHERE seller_ref IS NULL"
            ),
            "seeds_attached_bare": await _count(
                "SELECT count(*) AS c FROM external_product_seeds "
                "WHERE attached_product_key IS NOT NULL "
                "AND btrim(attached_product_key) <> '' "
                "AND attached_product_key NOT LIKE 'prod::%'"
            ),
            "catalog_external_seed": await _count(
                "SELECT count(*) AS c FROM catalog_products WHERE merchant_id = :banned", banned
            ),
            "edges_external_seed": await _count(
                "SELECT count(*) AS c FROM commerce_attribution_edges WHERE merchant_id = :banned",
                banned,
            ),
        }

    async def discover_cascade_tables(self) -> List[Dict[str, Any]]:
        """Reflect every base table that carries a seller column
        (merchant_id/primary_merchant_id) AND a product-scoping key
        (product_key/content_key/sku_key), minus the denylist. Auto-includes the
        beauty_* tables and any future dependent — completeness over a hardcoded
        list that could silently miss one (founder: loud over silent)."""
        rows = await self.db.fetch_all(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND column_name IN
                   ('merchant_id','primary_merchant_id','product_key','content_key','sku_key')
            """
        )
        cols: Dict[str, set] = {}
        for r in rows or []:
            d = dict(r)
            cols.setdefault(d["table_name"], set()).add(d["column_name"])
        targets: List[Dict[str, Any]] = []
        for table, cset in sorted(cols.items()):
            if table in CASCADE_DENYLIST or table == "catalog_products":
                continue
            seller_col = (
                "merchant_id" if "merchant_id" in cset
                else "primary_merchant_id" if "primary_merchant_id" in cset
                else None
            )
            if not seller_col:
                continue
            if "product_key" in cset:
                scope = "product_key"
            elif "content_key" in cset:
                scope = "content_key"
            elif "sku_key" in cset:
                scope = "sku_key"
            else:
                continue  # a bare seller column with no product scope — not a target
            targets.append({"table": table, "seller_col": seller_col, "scope_col": scope})
        return targets

    # -- phase 1: seeds seller_ref ------------------------------------------

    async def run_seeds(self, report: BackfillReport) -> None:
        # NOTE: external_product_seeds has no top-level `brand` column — brand
        # lives inside seed_data JSON (_seed_brand falls back to it). Selecting a
        # non-existent `brand` column crashed this phase against the prod schema
        # (UndefinedColumnError), which is why the seeds backfill had never run.
        rows = await self.db.fetch_all(
            "SELECT id, attached_product_key, domain, destination_url, "
            "canonical_url, seed_data FROM external_product_seeds WHERE seller_ref IS NULL"
        )
        stats = {"scanned": 0, "self": 0, "cross_existing": 0, "cross_mint": 0,
                 "unresolvable": 0, "written": 0}
        unresolvable: List[str] = []
        for row in rows or []:
            d = dict(row)
            stats["scanned"] += 1
            seed_data = _coerce_seed_data(d.get("seed_data"))
            anchor = self.si.anchor_merchant_from_product_key(d.get("attached_product_key"))
            brand = _seed_brand(d, seed_data)
            dest = _seed_destination(d, seed_data)
            res = await resolve_seller_readonly(
                self.si, anchor_merchant_id=anchor, brand=brand, destination_domain=dest
            )
            if res.seller_ref is None:
                stats["unresolvable"] += 1
                unresolvable.append(str(d.get("id")))
                continue
            if res.disposition == "self":
                stats["self"] += 1
            elif res.disposition == "cross_claimed":
                stats["cross_existing"] += 1
            else:
                stats["cross_mint"] += 1
            if self.execute:
                # Reuse the REAL A9-3 writer path for the actual mint + derivation
                # so seeds land byte-identical to how new writes would (no shadow
                # divergence): derive_seed_seller mints the observed row as needed.
                seller_ref, seed_kind = await self.si.derive_seed_seller(
                    anchor_merchant_id=anchor,
                    brand=brand,
                    destination_domain=dest,
                    source_system=SOURCE_SYSTEM_SEEDS,
                )
                if seller_ref is None:
                    # Should agree with the readonly shadow; if not, do not guess.
                    stats["unresolvable"] += 1
                    stats[res.disposition] = stats.get(res.disposition, 0) - 1
                    unresolvable.append(str(d.get("id")))
                    continue
                await self.db.execute(
                    "UPDATE external_product_seeds SET seller_ref = :sr, seed_kind = :sk, "
                    "updated_at = NOW() WHERE id = :id AND seller_ref IS NULL",
                    {"sr": seller_ref, "sk": seed_kind, "id": d.get("id")},
                )
                await self._checkpoint("seeds", str(d.get("id")), seller_ref, "done")
                stats["written"] += 1
        report.seeds = stats
        report.review_queue["seeds_unresolvable_seller_ref"] = unresolvable

    # -- phase 2: bare attached_product_key repair --------------------------

    async def run_barekey(self, report: BackfillReport) -> None:
        rows = await self.db.fetch_all(
            "SELECT id, external_product_id, attached_product_key, canonical_url, "
            "destination_url, seed_data FROM external_product_seeds "
            "WHERE attached_product_key IS NOT NULL AND btrim(attached_product_key) <> '' "
            "AND attached_product_key NOT LIKE 'prod::%'"
        )
        stats = {"scanned": 0, "repaired": 0, "undecodable": 0,
                 "already_resolving": 0}
        undecodable: List[Dict[str, Any]] = []
        already_resolving: List[Dict[str, Any]] = []
        for row in rows or []:
            d = dict(row)
            stats["scanned"] += 1

            # 🚨 NEVER DETACH A KEY THAT ALREADY RESOLVES. This phase validates
            # that the key it is about to WRITE points at a real row; until
            # 2026-08-01 nothing validated the key it was about to OVERWRITE.
            #
            # `ext:<slug>::<hash>` is not a bare/malformed key — it is a LIVE
            # product_key format on ~1,795 unsuppressed catalog_products rows,
            # every one Path-C (`catalog_enrichment_agent_v1`). The gateway
            # routes a minted PDP by matching
            # `external_product_seeds.attached_product_key = cp.product_key`
            # (services/pdp_renderability seed_route_resolves_sql, minted arm),
            # so a seed pointing at an `ext:` row is CORRECT RELATIVE TO THAT ROW.
            #
            # Conforming the seed to `prod::` while the row keeps its `ext:` key
            # breaks the join. Measured when it happened: 720 seeds moved, 364
            # ELECTED, trust-public PDPs went to HTTP 500 — confirmed live, not
            # inferred from an invariant. Restored from a reconstructed
            # content_key mapping (587 exact; 133 needed hand inspection).
            #
            # A rewrite here has TWO ends. Validating only the destination is how
            # a "repair" silently detaches a working link.
            #
            # DELIBERATELY BROADER than the minted arm: no source_system and no
            # seed-status filter. Broader means more SKIPS, which is the safe
            # direction (a skip is reviewable in the report; a detach is a 500),
            # so do not "tighten" this to match the predicate — that converts
            # review items back into outages.
            # RAW, NOT btrim'd — the join this guard protects does not trim
            # either side (services/pdp_renderability seed_route_resolves_sql:
            # `_seed_route_minted.attached_product_key = cp.product_key`), and
            # product_key is varchar, so trailing space is significant. Comparing
            # a stripped value would declare a PADDED key "already resolving",
            # skip the very row this phase exists to repair, and report it under
            # a value the column does not contain — hiding it from the human who
            # would otherwise catch it. The SELECT above already guarantees this
            # is non-null and not blank.
            current_apk = d.get("attached_product_key")
            live = await self.db.fetch_one(
                "SELECT product_key FROM catalog_products WHERE product_key = :pk",
                {"pk": current_apk},
            )
            if live:
                stats["already_resolving"] += 1
                already_resolving.append({
                    "seed_id": str(d.get("id")),
                    "attached_product_key": current_apk,
                })
                continue

            candidate = candidate_storage_key_from_seed(d)
            resolved: Optional[str] = None
            if candidate:
                # Verify the derived key resolves to a real catalog_products row —
                # never mint a key that points at nothing (ADR-009 decision 3).
                hit = await self.db.fetch_one(
                    "SELECT product_key FROM catalog_products WHERE product_key = :pk",
                    {"pk": candidate},
                )
                if hit:
                    resolved = candidate
                else:
                    # Fall back to identity LOOK-UP (not a widened matcher): a
                    # unique catalog row at the mirror's (platform, source_product_id).
                    epid = str(d.get("external_product_id") or "").strip()
                    if epid:
                        hits = await self.db.fetch_all(
                            "SELECT product_key FROM catalog_products "
                            "WHERE platform = :pf AND source_product_id = :pid",
                            {"pf": EXTERNAL_SEED_PLATFORM, "pid": epid},
                        )
                        pks = {str(dict(h)["product_key"]) for h in hits or []}
                        if len(pks) == 1:
                            resolved = next(iter(pks))
            if not resolved:
                stats["undecodable"] += 1
                undecodable.append({"seed_id": str(d.get("id")),
                                    "attached_product_key": d.get("attached_product_key"),
                                    "external_product_id": d.get("external_product_id")})
                continue
            if self.execute:
                # Snapshot the PREVIOUS value before overwriting it. Recovery
                # from the 2026-08-01 incident needed archaeology precisely
                # because the checkpoint stored only the NEW key; an UPDATE that
                # cannot be undone from its own audit trail is not resumable, it
                # is just fast.
                await self._checkpoint(
                    "barekey", str(d.get("id")), resolved, "pending",
                    previous_value=current_apk,
                )
                # COMPARE-AND-SWAP on the pre-image. The guard above is a
                # READ; without this the WHERE never mentions the column, so any
                # writer between the unbatched fetch_all and this UPDATE is
                # clobbered with no re-check — a silent detach, which is exactly
                # the failure mode this PR exists to close. Phase 1 carries the
                # same discipline (`AND seller_ref IS NULL`). A concurrent write
                # now makes this a no-op instead.
                await self.db.execute(
                    "UPDATE external_product_seeds SET attached_product_key = :pk, "
                    "updated_at = NOW() WHERE id = :id AND attached_product_key = :old",
                    {"pk": resolved, "id": d.get("id"), "old": current_apk},
                )
                await self._checkpoint("barekey", str(d.get("id")), resolved, "done")
            stats["repaired"] += 1
        report.barekey = stats
        report.review_queue["barekey_undecodable"] = undecodable
        report.review_queue["barekey_already_resolving"] = already_resolving

    # -- phase 3: catalog re-key --------------------------------------------

    async def run_catalog(self, report: BackfillReport) -> None:
        cascade = await self.discover_cascade_tables()
        report.cascade_tables = cascade
        # Assert every generated re-subject SQL is sig-frozen up front.
        for t in cascade:
            assert_sig_frozen_sql(
                f"UPDATE {t['table']} SET {t['seller_col']} = :obs "
                f"WHERE {t['scope_col']} = :scope AND {t['seller_col']} = :banned"
            )

        rows = await self.db.fetch_all(
            # Seed attach by attached_product_key + status='active' — the
            # repo's own attach relation. An external_product_id equi-join
            # fans out across markets/statuses and, for Path C rows (whose
            # source_product_id is a NAME SLUG, not an ext id), can mis-join
            # an unrelated seed into the domain candidates.
            # DISTINCT ON: two active seeds (different market/tool) can attach
            # one product; a fan-out would double-plan it. Deterministic pick:
            # the seed with a non-empty domain first, then newest.
            # DISTINCT ON picks ONE seed for the domain candidates, but a
            # product can carry SEVERAL active attached seeds and each may own
            # its own identity listing — the canary stranded 3 listings on a
            # 4-seed product when only the picked seed's listing migrated
            # (2026-08-08). seed_listing_ids aggregates ALL of them.
            "SELECT DISTINCT ON (cp.product_key) "
            "cp.product_key, cp.content_key, cp.brand, cp.source_domain, "
            "cp.canonical_url, cp.platform, cp.source_product_id, "
            "e.external_product_id AS seed_external_product_id, "
            "e.domain AS seed_domain, e.destination_url AS seed_destination_url, "
            "(SELECT array_agg(e2.external_product_id) FROM external_product_seeds e2 "
            "  WHERE e2.attached_product_key = cp.product_key AND e2.status = 'active') "
            "  AS seed_listing_ids "
            "FROM catalog_products cp "
            "LEFT JOIN external_product_seeds e "
            "  ON e.attached_product_key = cp.product_key AND e.status = 'active' "
            "WHERE cp.merchant_id = :banned "
            "ORDER BY cp.product_key, (coalesce(e.domain, '') = '') ASC, e.updated_at DESC NULLS LAST"
            + (" LIMIT :cap" if self.max_products else ""),
            {"banned": BANNED_BUCKET_MERCHANT_ID,
             **({"cap": self.max_products} if self.max_products else {})},
        )
        products = [dict(r) for r in rows or []]
        stats = {"scanned": len(products), "resubjected": 0, "review_collision": 0,
                 "review_unresolvable": 0, "review_excluded_rig": 0,
                 "batches": 0, "distinct_sellers": 0, "retailer_keyed": 0}
        collisions: List[Dict[str, Any]] = []
        unresolvable: List[Dict[str, Any]] = []
        sellers_seen: set = set()

        # Plan every product first (read-only), then execute batched.
        # _plan_one mutates nothing, so a row whose lookup dies on a proxy
        # flake retries on a fresh connection without double-counting stats.
        planned: List[Dict[str, Any]] = []
        for p in products:
            verdict, payload = await self._retry_after_reset(
                lambda p=p: self._plan_one(p), what=f"plan {p.get('product_key')}")
            if verdict in ("rig", "unresolvable"):
                key = "review_excluded_rig" if verdict == "rig" else "review_unresolvable"
                stats[key] += 1
                unresolvable.append(payload)
            elif verdict == "collision":
                stats["review_collision"] += 1
                collisions.append(payload)
            elif verdict == "planned":
                sellers_seen.add(payload["observed_id"])
                # Counted here, not at resolve time: retailer_keyed now means
                # "retailer rows actually PLANNED" — a retailer row routed to
                # collision review no longer inflates it (delta vs pre-refactor
                # reports, where the count included collided rows).
                if payload["seller_kind"] == "retailer":
                    stats["retailer_keyed"] += 1
                planned.append(payload)
            else:  # a verdict typo must never fall through to the WRITE path
                raise RuntimeError(f"unknown planning verdict {verdict!r}")

        stats["distinct_sellers"] = len(sellers_seen)
        report.review_queue["catalog_collision"] = collisions
        report.review_queue["catalog_unresolvable"] = unresolvable

        # Batched execution with per-batch parity.
        for i in range(0, len(planned), self.batch_size):
            if self.max_batches and stats["batches"] >= self.max_batches:
                stats["stopped_at_max_batches"] = self.max_batches
                break
            batch = planned[i:i + self.batch_size]
            # Each batch is one transaction: a flake mid-batch rolls back
            # cleanly, so one retry on a fresh connection re-does the whole
            # batch from a clean slate.
            parity = await self._retry_after_reset(
                lambda batch=batch: self._resubject_batch(batch, cascade),
                what=f"batch {stats['batches'] + 1}")
            if parity.get("skipped_to_review"):
                report.review_queue.setdefault(
                    "catalog_execute_skips", []).extend(parity["skipped_to_review"])
            report.parity.append(parity)
            stats["batches"] += 1
            stats["resubjected"] += parity.get("resubjected", 0)
        report.catalog = stats

    async def _plan_one(self, p: Dict[str, Any]) -> tuple:
        """Plan ONE product (read-only, mutates nothing) so a proxy flake can
        retry the row on a fresh connection. Returns (verdict, payload):
        'rig' / 'unresolvable' -> a review entry, 'collision' -> the collision
        record, 'planned' -> the planned row."""
        brand = str(p.get("brand") or "").strip()
        dest, dest_resolved = _plan_domain(p)
        if _is_excluded_rig_row(p):
            return "rig", {"product_key": p.get("product_key"), "brand": brand,
                           "domain": dest, "reason": "excluded_test_rig_dev_store"}
        if not dest_resolved:
            # No row-owned candidate names a plausible seller. Review — never
            # fall through to a resolver without the registrable guard, which
            # would mint an identity on a bare public suffix.
            return "unresolvable", {"product_key": p.get("product_key"), "brand": brand,
                                    "domain": dest, "reason": "seller_of_record_unresolved"}
        # W2 keying decides the IDENTITY SHAPE first: a retailer domain keys
        # on etld1 alone (one retailer = one merchant), everything else
        # keeps the brand-direct path (which also handles anchors/claims).
        kind = "brand_direct"
        try:
            ident = resolve_seed_seller_identity(brand=brand, domain=dest)
            kind = ident["kind"]
        except ValueError:
            ident = None
        if ident is not None and kind == "retailer":
            seller_ref, disposition = ident["merchant_id"], "retailer_domain"
        else:
            res = await resolve_seller_readonly(
                self.si, anchor_merchant_id=None, brand=brand, destination_domain=dest
            )
            seller_ref, disposition = res.seller_ref, res.disposition
        if seller_ref is None:
            return "unresolvable", {"product_key": p.get("product_key"), "brand": brand,
                                    "domain": dest, "reason": disposition}
        collision = await self._fragmentation_collision(seller_ref, brand, dest, p)
        if collision:
            return "collision", collision
        # The identity listing may live under the catalog row's
        # source_product_id (Path B: the ext id) OR the attached seed's
        # external_product_id (Path C: source_product_id is a name slug;
        # measured 2026-08-07 — 0 Path C listings under the slug, 2,444
        # under the seed id). Carry every candidate.
        listing_pids = [str(v) for v in dict.fromkeys(
            [p.get("source_product_id"), p.get("seed_external_product_id"),
             *(p.get("seed_listing_ids") or [])]) if v]
        return "planned", {**p, "observed_id": seller_ref, "disposition": disposition,
                           "seller_kind": kind, "seller_domain": dest,
                           "listing_product_ids": listing_pids}

    async def _retry_after_reset(self, thunk, *, what: str):
        """Run `thunk` once; on failure, HARD-reset the shared DB connection
        and run it exactly once more, propagating the second failure.

        Why a hard reset: a socket death in the middle of an operation leaves
        `databases`' task-local Connection permanently wedged ("Connection is
        already acquired") — every later query in the run fails until the
        Connection object is discarded. `disconnect()` drops the task's
        Connection and `connect()` rebuilds the pool, so the retry runs clean
        (verified against databases==0.7.0 core). This is a bounded retry of
        the SAME route on a fresh connection, never a degraded answer — a
        deterministic failure fails again and aborts the run loudly
        (2026-08-09: two warn-and-continue guards turned one flake into a run
        planning with its safety checks silently disarmed)."""
        try:
            return await thunk()
        except Exception as exc:  # noqa: BLE001 — one bounded retry, then raise
            logger.warning("%s failed (%s); hard-resetting the DB connection "
                           "and retrying once", what, str(exc)[:200])
            await self.db.disconnect()
            await self.db.connect()
            return await thunk()

    async def _fragmentation_collision(
        self, observed_id: str, brand: str, dest: str, product: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """ADR-008 fragmentation guard (always-on since W5 P2) as the collision
        tripwire (ADR-009 D4.3): if the same brand+host is ALREADY canonical under
        a DIFFERENT real merchant, route to review — do NOT merge identities.

        A failed lookup PROPAGATES. It must never read as "no collision": that
        answer disarms the anti-merge tripwire exactly when the connection is
        broken — and a wedged connection breaks every later lookup in the run
        (observed 2026-08-09, run 31345305445). The import is local to avoid
        a cycle, but an import FAILURE propagates too — an unimportable guard
        answering "no collision" for the whole run is the same defect."""
        from services.audit_index_intake import _existing_brand_canonical_conflict
        fields = {"brand": brand, "source_domain": dest, "canonical_url": product.get("canonical_url")}
        conflict = await _existing_brand_canonical_conflict(observed_id, fields)
        if not conflict:
            return None
        cm = str(conflict.get("merchant_id") or "")
        # A sibling still in the bucket, or the very identity we're minting, is not
        # a cross-merchant collision — same brand+host collapses to the same
        # observed seller by construction.
        if cm in (BANNED_BUCKET_MERCHANT_ID, observed_id):
            return None
        return {"product_key": product.get("product_key"), "brand": brand, "host": dest,
                "observed_id": observed_id, "conflict_merchant_id": cm,
                "conflict_product_key": conflict.get("product_key")}

    async def _resubject_batch(
        self, batch: List[Dict[str, Any]], cascade: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        pks = [str(b["product_key"]) for b in batch]
        # BEFORE: parity snapshots (row count, sig set, servable set).
        before = await self._parity_snapshot(pks)
        if not self.execute:
            # Dry-run: predict the after-state without writing. Servable/sig sets
            # are unchanged by construction (option (b) touches only merchant_id).
            # The NEW writes are rehearsed read-only so the founder-reviewed
            # report shows what the flip will actually touch (review finding 13).
            spids_all = [pid for b in batch for pid in (b.get("listing_product_ids") or [])]
            pgm_spids = [str(b.get("source_product_id") or "") for b in batch if b.get("source_product_id")]
            platforms = sorted({str(b.get("platform") or "") for b in batch if b.get("platform")})
            pgm_moves = listing_moves = listing_conflicts = 0
            if pgm_spids and platforms:
                row = await self.db.fetch_one(
                    "SELECT count(*) AS c FROM product_group_members "
                    "WHERE merchant_id = :banned AND platform = ANY(:platforms) "
                    "AND platform_product_id = ANY(:spids)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "platforms": platforms, "spids": pgm_spids})
                pgm_moves = int(dict(row)["c"]) if row else 0
            if spids_all:
                row = await self.db.fetch_one(
                    "SELECT count(*) AS c FROM pdp_identity_listing "
                    "WHERE merchant_id = :banned AND product_id = ANY(:spids)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "spids": spids_all})
                listing_moves = int(dict(row)["c"]) if row else 0
                for b in batch:
                    if await self._listing_new_ref_conflict(b, str(b["observed_id"])):
                        listing_conflicts += 1
            return {
                "ok": True, "mode": "dry_run", "product_count": len(pks),
                "resubjected": len(pks), "before_rows": before["rows"],
                "before_sigs": before["sig_count"], "before_servable": before["servable_count"],
                "distinct_sellers": len({str(b["observed_id"]) for b in batch}),
                "pgm_rows_to_move": pgm_moves, "listings_to_migrate": listing_moves,
                "listing_new_ref_conflicts": listing_conflicts,
            }
        skipped_in_batch: List[Dict[str, Any]] = []
        async with self.db.transaction():
            for b in batch:
                pk = str(b["product_key"])
                ck = b.get("content_key")
                obs = str(b["observed_id"])
                # Materialize the observed seller via the REAL minter
                # (idempotent; resolves claim -> existing -> mint) — fed the
                # PLANNED domain, never the raw columns: the plan may have
                # recovered the domain from canonical_url or the seed while
                # source_domain is garbage, and feeding ensure a different
                # input than the plan silently minted a DIFFERENT merchant
                # than the one the row was re-keyed to (review finding 1).
                ensured_id = await ensure_observed_seller_of_record(
                    kind=str(b.get("seller_kind") or "brand_direct"),
                    brand=str(b.get("brand") or ""),
                    domain=str(b.get("seller_domain") or ""),
                    source_system=SOURCE_SYSTEM_CATALOG,
                    primary_platform=b.get("platform"),
                )
                if ensured_id != obs:
                    # Claim-attach or derivation divergence: the merchant that
                    # actually exists is not the planned one. Never re-key onto
                    # an unbacked id — route the product to review, loudly.
                    skipped_in_batch.append({
                        "product_key": pk, "planned": obs, "ensured": ensured_id,
                        "reason": "ensured_seller_diverges_from_plan"})
                    continue
                # Listing new-ref conflict pre-check: if a listing ALREADY
                # exists under a new ref, merging children onto it and deleting
                # the old row would silently discard the old listing's content
                # and re-attach operator overrides to different content. Review.
                conflict_ref = await self._listing_new_ref_conflict(b, obs)
                if conflict_ref:
                    skipped_in_batch.append({
                        "product_key": pk, "planned": obs,
                        "conflicting_ref": conflict_ref,
                        "reason": "listing_new_ref_conflict"})
                    continue
                # 1) the sig-owning row (dedicated FROZEN statement).
                await self.db.execute(
                    CATALOG_PRODUCTS_RESUBJECT_SQL,
                    {"obs": obs, "pk": pk, "banned": BANNED_BUCKET_MERCHANT_ID},
                )
                # 2) dependents (stable keys — product_key/content_key/sku_key
                #    do not change under option (b)).
                sku_keys = await self._sku_keys_for_product(pk)
                for t in cascade:
                    await self._resubject_dependent(t, obs, pk, ck, sku_keys)
                # 3) the two tables reflection CANNOT see (R0 finding): group
                #    membership keys on (merchant, platform, platform_product_id)
                #    and the identity listing embeds the merchant in its PK.
                await self._resubject_group_membership(b, obs)
                await self._migrate_listing_refs(b, obs)
            # Residue audit INSIDE the txn: any enumerated table still holding a
            # banned seller for this batch's products = un-migrated cascade.
            skipped_keys = {str(x["product_key"]) for x in skipped_in_batch}
            done = [b for b in batch if str(b["product_key"]) not in skipped_keys]
            done_pks = [str(b["product_key"]) for b in done]
            residue = await self._residue_audit(done_pks, cascade)
            spids = [pid for b in done for pid in (b.get("listing_product_ids") or [])]
            # ALL candidate ids, NO platform filter — the audit must count
            # exactly what the independent verifier counts. The old shape
            # (source ids only, platform-scoped) shared the resubjection's
            # blind spot, which is how 77 seed-keyed pgm rows sailed through
            # five green parity checks (verifier catch, 2026-08-11).
            if spids:
                row = await self.db.fetch_one(
                    "SELECT count(*) AS c FROM product_group_members "
                    "WHERE merchant_id = :banned AND platform_product_id = ANY(:spids)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "spids": spids})
                c = int(dict(row)["c"]) if row else 0
                if c:
                    residue["product_group_members"] = residue.get("product_group_members", 0) + c
            if spids:
                row = await self.db.fetch_one(
                    "SELECT count(*) AS c FROM pdp_identity_listing "
                    "WHERE merchant_id = :banned AND product_id = ANY(:spids)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "spids": spids})
                c = int(dict(row)["c"]) if row else 0
                if c:
                    residue["pdp_identity_listing"] = residue.get("pdp_identity_listing", 0) + c
            # AFTER: parity assertions. On failure, raise -> rollback the batch.
            after = await self._parity_snapshot(done_pks)
            before_done = {**before,
                           "rows": len([k for k in before["sigs"] if k in set(done_pks)]),
                           "sigs": {k: v for k, v in before["sigs"].items() if k in set(done_pks)},
                           "servable": {k for k in before["servable"] if k in set(done_pks)}}
            before_done["sig_count"] = len(before_done["sigs"])
            before_done["servable_count"] = len(before_done["servable"])
            parity = self._diff_parity(before_done, after, residue)
            if skipped_in_batch:
                # Report mutation happens in the CALLER off the returned
                # parity — this method must stay retry-safe (a retried batch
                # would double-extend the review queue from here).
                parity["skipped_to_review"] = skipped_in_batch
            if not parity["ok"]:
                raise RuntimeError(
                    f"A9-4 batch parity FAILED (loud abort, no silent absorb): {parity}"
                )
            for b in done:
                # The overwritten value is the constant this phase selects on
                # (merchant_id = BANNED_BUCKET_MERCHANT_ID, pinned by the
                # UPDATE's own WHERE), so it is reconstructible — but the rule
                # in _checkpoint says any phase that OVERWRITES passes it, and a
                # rule the same file violates is not a rule.
                await self._checkpoint("catalog", str(b["product_key"]),
                                       str(b["observed_id"]), "done",
                                       previous_value=BANNED_BUCKET_MERCHANT_ID)
        return parity

    async def repair_listings(self, report: BackfillReport) -> None:
        """Migrate identity listings still under the sentinel for products the
        catalog phase already checkpointed done. Exists because a re-run cannot
        reach them (moved rows no longer match the sentinel-scoped scan): the
        canary's 4-seed product migrated only the picked seed's listing,
        stranding 3 (verifier catch, 2026-08-08). Also repairs
        product_group_members stragglers for the same products — the pgm
        resubjection had the mirror-image blind spot (source_product_id only)
        and stranded 77 seed-keyed rows (verifier catch, 2026-08-11)."""
        rows = await self.db.fetch_all(
            "SELECT ck.ref_id AS product_key, ck.observed_id, cp.source_product_id, "
            "(SELECT array_agg(e2.external_product_id) FROM external_product_seeds e2 "
            "  WHERE e2.attached_product_key = ck.ref_id AND e2.status = 'active') AS seed_listing_ids "
            "FROM a9_4_backfill_checkpoint ck "
            "JOIN catalog_products cp ON cp.product_key = ck.ref_id "
            "WHERE ck.phase = 'catalog' AND ck.status = 'done'"
        )
        stats = {"scanned": len(rows or []), "stale_found": 0, "migrated": 0, "conflicts": 0,
                 "pgm_stale_found": 0, "pgm_moved": 0, "pgm_retired": 0}
        for r in rows or []:
            d = dict(r)
            obs = str(d.get("observed_id") or "")
            b = {"source_product_id": d.get("source_product_id"),
                 "listing_product_ids": [str(v) for v in dict.fromkeys(
                     [d.get("source_product_id"), *(d.get("seed_listing_ids") or [])]) if v]}
            stale = []
            for old_ref, _new_ref in self._listing_refs_for(b, obs):
                row = await self.db.fetch_one(
                    "SELECT 1 FROM pdp_identity_listing WHERE source_listing_ref = :r",
                    {"r": old_ref})
                if row:
                    stale.append(old_ref)
            # pgm stragglers: count in BOTH modes (the dry-run must report
            # them), write only in execute.
            pgm_row = await self.db.fetch_one(
                "SELECT count(*) AS c FROM product_group_members "
                "WHERE merchant_id = :banned AND platform_product_id = ANY(:pids)",
                {"banned": BANNED_BUCKET_MERCHANT_ID, "pids": b["listing_product_ids"]})
            pgm_stale = int(dict(pgm_row)["c"]) if pgm_row else 0
            stats["pgm_stale_found"] += pgm_stale
            if pgm_stale and self.execute:
                async with self.db.transaction():
                    moved = await self._resubject_group_membership(b, obs)
                stats["pgm_moved"] += moved["moved"]
                stats["pgm_retired"] += moved["retired"]
            if not stale:
                continue
            stats["stale_found"] += len(stale)
            if not self.execute:
                continue
            conflict = await self._listing_new_ref_conflict(b, obs)
            if conflict:
                stats["conflicts"] += 1
                report.review_queue.setdefault("listing_repair_conflicts", []).append(
                    {"product_key": d.get("product_key"), "conflicting_ref": conflict})
                continue
            async with self.db.transaction():
                await self._migrate_listing_refs(b, obs)
            stats["migrated"] += len(stale)
        report.catalog["listing_repair"] = stats

    async def _resubject_group_membership(self, b: Dict[str, Any], obs: str) -> Dict[str, int]:
        """product_group_members keys on (merchant_id, platform,
        platform_product_id) and carries NONE of the reflection scope columns —
        the one ownership table `discover_cascade_tables` cannot see (R0
        finding, 2026-08-06). Self-heal semantics: if the observed-seller row
        already exists (graph/mirror wrote it), the stale banned row is
        RETIRED; otherwise the banned row moves in place. When the two rows
        disagree on product_group_id, the retirement is still correct (one row
        per merchant by PK) but the divergence is logged for review — a group
        move must never be silently indistinguishable from a duplicate."""
        # EVERY id a pgm row may key on — the same union the listing
        # migration carries. Keying on source_product_id alone stranded 77
        # seed-keyed rows across the first 1,075 moved products (verifier
        # catch, 2026-08-11): third instance of the "handle every id a child
        # row may key on" class, after Path C listings and multi-seed
        # listings. The banned rows are ENUMERATED (any platform) so the hunt
        # matches the verifier's join exactly; each is then moved or retired
        # under its own (platform, pid) so the PK stays safe.
        pids = [str(v) for v in dict.fromkeys(
            [*(b.get("listing_product_ids") or []), b.get("source_product_id")]) if v]
        if not pids:
            return {"moved": 0, "retired": 0}
        stats = {"moved": 0, "retired": 0}
        banned_rows = await self.db.fetch_all(
            "SELECT platform, platform_product_id, product_group_id "
            "FROM product_group_members "
            "WHERE merchant_id = :banned AND platform_product_id = ANY(:pids)",
            {"banned": BANNED_BUCKET_MERCHANT_ID, "pids": pids},
        )
        for row in banned_rows or []:
            d = dict(row)
            platform = str(d.get("platform") or "")
            spid = str(d.get("platform_product_id") or "")
            # `databases` binds through SQLAlchemy text(), which REJECTS a
            # params dict carrying a key the statement does not name. Each
            # query gets exactly its own binds — a shared superset dict
            # aborted the canary batch (caught 2026-08-07, before any write).
            scope = {"platform": platform, "spid": spid}
            obs_scope = {"obs": obs, **scope}
            banned_scope = {"banned": BANNED_BUCKET_MERCHANT_ID, **scope}
            existing = await self.db.fetch_one(
                "SELECT product_group_id FROM product_group_members WHERE merchant_id = :obs "
                "AND platform = :platform AND platform_product_id = :spid",
                obs_scope,
            )
            if existing:
                if str(d.get("product_group_id")) != str(dict(existing).get("product_group_id")):
                    logger.warning(
                        "A9-4 pgm group divergence for %s: banned row group %s vs observed row group %s",
                        spid, d.get("product_group_id"), dict(existing).get("product_group_id"),
                    )
                await self.db.execute(
                    "DELETE FROM product_group_members WHERE merchant_id = :banned "
                    "AND platform = :platform AND platform_product_id = :spid",
                    banned_scope,
                )
                stats["retired"] += 1
                continue
            sql = ("UPDATE product_group_members SET merchant_id = :obs "
                   "WHERE merchant_id = :banned AND platform = :platform "
                   "AND platform_product_id = :spid")
            assert_sig_frozen_sql(sql)
            await self.db.execute(sql, {"obs": obs, **banned_scope})
            stats["moved"] += 1
        return stats

    _listing_columns_cache: Optional[List[str]] = None

    async def _listing_columns(self) -> List[str]:
        if self._listing_columns_cache is None:
            rows = await self.db.fetch_all(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'pdp_identity_listing' "
                "ORDER BY ordinal_position"
            )
            self._listing_columns_cache = [str(dict(r)["column_name"]) for r in rows or []]
        return self._listing_columns_cache

    def _listing_refs_for(self, b: Dict[str, Any], obs: str) -> List[Tuple[str, str]]:
        """(old_ref, new_ref) pairs for every listing id the product may carry.
        Path B listings key on the row's source_product_id; Path C listings key
        on the ATTACHED SEED's external_product_id (the catalog row's
        source_product_id is a name slug — measured 2026-08-07: 0 Path C
        listings under the slug, 2,444 under the seed id)."""
        pids = b.get("listing_product_ids") or [
            str(v) for v in [b.get("source_product_id")] if v]
        return [(f"{BANNED_BUCKET_MERCHANT_ID}:{pid}", f"{obs}:{pid}") for pid in pids]

    async def _listing_new_ref_conflict(self, b: Dict[str, Any], obs: str) -> Optional[str]:
        """A listing already existing under a NEW ref while the OLD ref also
        exists means the migration would merge operator children onto content
        it never reviewed and delete the old listing silently — review instead."""
        for old_ref, new_ref in self._listing_refs_for(b, obs):
            old_row = await self.db.fetch_one(
                "SELECT 1 FROM pdp_identity_listing WHERE source_listing_ref = :r", {"r": old_ref})
            if not old_row:
                continue
            new_row = await self.db.fetch_one(
                "SELECT 1 FROM pdp_identity_listing WHERE source_listing_ref = :r", {"r": new_ref})
            if new_row:
                return new_ref
        return None

    async def _migrate_listing_refs(self, b: Dict[str, Any], obs: str) -> None:
        """pdp_identity_listing's PRIMARY KEY (source_listing_ref) embeds the
        merchant ('<merchant>:<product_id>'), and 7,036 operator overrides plus
        515 review-queue rows hang off the old refs (measured 2026-08-06) — so
        delete-and-let-the-graph-reheal would destroy operator work. The FK
        (review_queue -> listing) is NOT deferrable, so an in-place PK UPDATE
        cannot reorder its checks: COPY the row under the new ref, REPOINT both
        children, DELETE the old row — one product, same transaction. Columns
        are discovered from the live schema (never a hardcoded list, quoted
        against reserved words) with the two identity columns replaced; every
        generated statement passes the sig-freeze assert. Idempotent under
        resume; new-ref conflicts were routed to review BEFORE any write."""
        cols = None
        for old_ref, new_ref in self._listing_refs_for(b, obs):
            exists = await self.db.fetch_one(
                "SELECT 1 FROM pdp_identity_listing WHERE source_listing_ref = :old_ref",
                {"old_ref": old_ref},
            )
            if not exists:
                continue
            if cols is None:
                cols = await self._listing_columns()
            quoted = [f'"{c}"' for c in cols]
            select_exprs = []
            for c in cols:
                if c == "source_listing_ref":
                    select_exprs.append(":new_ref")
                elif c == "merchant_id":
                    select_exprs.append(":obs")
                else:
                    select_exprs.append(f'"{c}"')
            copy_sql = (
                f"INSERT INTO pdp_identity_listing ({', '.join(quoted)}) "
                f"SELECT {', '.join(select_exprs)} FROM pdp_identity_listing "
                f"WHERE source_listing_ref = :old_ref "
                f"ON CONFLICT (source_listing_ref) DO NOTHING"
            )
            assert_sig_frozen_sql(copy_sql)
            params = {"new_ref": new_ref, "obs": obs, "old_ref": old_ref}
            await self.db.execute(copy_sql, params)
            for child_sql in (
                "UPDATE pdp_identity_review_queue SET source_listing_ref = :new_ref "
                "WHERE source_listing_ref = :old_ref",
                "UPDATE pdp_identity_override SET source_listing_ref = :new_ref "
                "WHERE source_listing_ref = :old_ref",
            ):
                assert_sig_frozen_sql(child_sql)
                await self.db.execute(child_sql, {"new_ref": new_ref, "old_ref": old_ref})
            await self.db.execute(
                "DELETE FROM pdp_identity_listing WHERE source_listing_ref = :old_ref",
                {"old_ref": old_ref},
            )

    async def _sku_keys_for_product(self, product_key: str) -> List[str]:
        rows = await self.db.fetch_all(
            "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": product_key}
        )
        return [str(dict(r)["sku_key"]) for r in rows or []]

    async def _resubject_dependent(
        self, t: Dict[str, Any], obs: str, pk: str, ck: Optional[str], sku_keys: List[str]
    ) -> None:
        table, seller_col, scope = t["table"], t["seller_col"], t["scope_col"]
        if scope == "product_key":
            sql = (f"UPDATE {table} SET {seller_col} = :obs WHERE product_key = :scope "
                   f"AND {seller_col} = :banned")
            params = {"obs": obs, "scope": pk, "banned": BANNED_BUCKET_MERCHANT_ID}
        elif scope == "content_key":
            if not ck:
                return
            sql = (f"UPDATE {table} SET {seller_col} = :obs WHERE content_key = :scope "
                   f"AND {seller_col} = :banned")
            params = {"obs": obs, "scope": ck, "banned": BANNED_BUCKET_MERCHANT_ID}
        else:  # sku_key
            if not sku_keys:
                return
            sql = (f"UPDATE {table} SET {seller_col} = :obs WHERE sku_key = ANY(:scope) "
                   f"AND {seller_col} = :banned")
            params = {"obs": obs, "scope": sku_keys, "banned": BANNED_BUCKET_MERCHANT_ID}
        assert_sig_frozen_sql(sql)
        await self.db.execute(sql, params)

    async def _residue_audit(self, pks: List[str], cascade: List[Dict[str, Any]]) -> Dict[str, int]:
        """After re-subjecting a batch, assert no ENUMERATED dependent still holds
        the banned seller for these products. Non-zero = a cascade miss -> the
        parity diff fails loudly."""
        residue: Dict[str, int] = {}
        for t in cascade:
            table, seller_col, scope = t["table"], t["seller_col"], t["scope_col"]
            if scope == "product_key":
                row = await self.db.fetch_one(
                    f"SELECT count(*) AS c FROM {table} WHERE {seller_col} = :banned "
                    f"AND product_key = ANY(:pks)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "pks": pks},
                )
            elif scope == "content_key":
                row = await self.db.fetch_one(
                    f"SELECT count(*) AS c FROM {table} t WHERE t.{seller_col} = :banned "
                    f"AND t.content_key IN (SELECT content_key FROM catalog_products "
                    f"WHERE product_key = ANY(:pks) AND content_key IS NOT NULL)",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "pks": pks},
                )
            else:  # sku_key
                row = await self.db.fetch_one(
                    f"SELECT count(*) AS c FROM {table} t WHERE t.{seller_col} = :banned "
                    f"AND t.sku_key IN (SELECT sku_key FROM catalog_skus "
                    f"WHERE product_key = ANY(:pks))",
                    {"banned": BANNED_BUCKET_MERCHANT_ID, "pks": pks},
                )
            c = int(dict(row)["c"]) if row else 0
            if c:
                residue[table] = c
        return residue

    async def _parity_snapshot(self, pks: List[str]) -> Dict[str, Any]:
        """(i) row count, (ii) frozen sig/canonical set, (iii) servable set — the
        three per-batch parity invariants (ADR-009 D4.3)."""
        rows = await self.db.fetch_all(
            "SELECT product_key, pivota_signature_id, pivota_canonical_url, content_key "
            "FROM catalog_products WHERE product_key = ANY(:pks)",
            {"pks": pks},
        )
        sigs: Dict[str, Tuple[Any, Any]] = {}
        for r in rows or []:
            d = dict(r)
            sigs[str(d["product_key"])] = (d.get("pivota_signature_id"), d.get("pivota_canonical_url"))
        # Servable set: the D2-amended public gate (status IN active/observed,
        # indexable, content_key present) — proves the sig still resolves to
        # serving content after re-subject.
        srow = await self.db.fetch_all(
            """
            SELECT cp.product_key
              FROM catalog_products cp
              JOIN catalog_merchants cm ON cp.merchant_id = cm.merchant_id
             WHERE cp.product_key = ANY(:pks)
               AND cm.indexable IS TRUE
               AND cm.status IN ('active','observed')
               AND cp.content_key IS NOT NULL
            """,
            {"pks": pks},
        )
        servable = {str(dict(r)["product_key"]) for r in srow or []}
        return {"rows": len(sigs), "sigs": sigs, "sig_count": len(sigs),
                "servable": servable, "servable_count": len(servable)}

    def _diff_parity(self, before: Dict[str, Any], after: Dict[str, Any],
                     residue: Dict[str, int]) -> Dict[str, Any]:
        rows_ok = before["rows"] == after["rows"]
        sigs_ok = before["sigs"] == after["sigs"]  # byte-identical sig+canonical
        servable_ok = before["servable"] == after["servable"]
        residue_ok = not residue
        return {
            "ok": rows_ok and sigs_ok and servable_ok and residue_ok,
            "rows_before": before["rows"], "rows_after": after["rows"], "rows_ok": rows_ok,
            "sigs_frozen": sigs_ok,
            "servable_before": before["servable_count"],
            "servable_after": after["servable_count"], "servable_ok": servable_ok,
            "cascade_residue": residue, "residue_ok": residue_ok,
            "resubjected": after["rows"],
        }

    # -- phase 4: edges ------------------------------------------------------

    async def run_edges(self, report: BackfillReport) -> None:
        rows = await self.db.fetch_all(
            "SELECT edge_id, merchant_id, metadata FROM commerce_attribution_edges "
            "WHERE merchant_id = :banned",
            {"banned": BANNED_BUCKET_MERCHANT_ID},
        )
        edges = [dict(r) for r in rows or []]
        info: Dict[str, Any] = {"count": len(edges)}
        if not edges:
            # ADR-009 D4.4: converted-edge history under the bucket is ~zero.
            logger.info("A9-4 edges: 0 rows under %r — assert-and-log, nothing to map.",
                        BANNED_BUCKET_MERCHANT_ID)
            info["action"] = "assert_and_log_zero"
        else:
            # >0: do NOT silently re-key an economic subject we can't derive a
            # seller for. Surface every one to review with a loud warning.
            logger.warning("A9-4 edges: %d rows under %r require a seller mapping — "
                           "routed to review (no silent re-key).", len(edges),
                           BANNED_BUCKET_MERCHANT_ID)
            info["action"] = "routed_to_review"
            report.review_queue["edges_under_bucket"] = [str(e.get("edge_id")) for e in edges]
        report.edges = info

    # -- checkpoint / resume -------------------------------------------------

    async def ensure_checkpoint_table(self) -> None:
        if not self.execute:
            return
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS a9_4_backfill_checkpoint (
                phase      TEXT NOT NULL,
                ref_id     TEXT NOT NULL,
                observed_id TEXT,
                previous_value TEXT,
                status     TEXT NOT NULL DEFAULT 'done',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (phase, ref_id)
            )
            """
        )
        # Additive for tables created before the column existed.
        await self.db.execute(
            "ALTER TABLE a9_4_backfill_checkpoint "
            "ADD COLUMN IF NOT EXISTS previous_value TEXT"
        )

    async def _checkpoint(self, phase: str, ref_id: str, observed_id: Optional[str],
                          status: str, previous_value: Optional[str] = None) -> None:
        """Record what this run did to `ref_id`.

        `previous_value` is what makes the run UNDOABLE. It is optional only so
        phases that overwrite nothing (seeds fills a NULL) can omit it; any phase
        that OVERWRITES an existing value must pass it, or recovery means
        reconstructing the old state from whatever joins happen to survive.
        """
        if not self.execute:
            return
        await self.db.execute(
            """
            INSERT INTO a9_4_backfill_checkpoint
                (phase, ref_id, observed_id, previous_value, status, updated_at)
            VALUES (:phase, :ref_id, :obs, :prev, :status, NOW())
            ON CONFLICT (phase, ref_id)
            DO UPDATE SET observed_id = EXCLUDED.observed_id,
                          previous_value = COALESCE(a9_4_backfill_checkpoint.previous_value,
                                                    EXCLUDED.previous_value),
                          status = EXCLUDED.status,
                          updated_at = NOW()
            """,
            {"phase": phase, "ref_id": ref_id, "obs": observed_id,
             "prev": previous_value, "status": status},
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_backfill(*, database: Any, si_mod: Any, execute: bool, batch_size: int,
                       phases: List[str], max_batches: int = 0,
                       max_products: int = 0) -> BackfillReport:
    report = BackfillReport(
        mode="execute" if execute else "dry_run",
        started_at=datetime.now(timezone.utc).isoformat(),
        phases=phases, batch_size=batch_size,
    )
    bf = SellerBackfill(database=database, si_mod=si_mod, execute=execute,
                        batch_size=batch_size, max_batches=max_batches,
                        max_products=max_products)
    await bf.ensure_checkpoint_table()
    report.counts = await bf.scan_counts()
    if "seeds" in phases:
        await bf.run_seeds(report)
    if "barekey" in phases:
        await bf.run_barekey(report)
    if "catalog" in phases:
        await bf.run_catalog(report)
    if "repair-listings" in phases:
        await bf.repair_listings(report)
    if "edges" in phases:
        await bf.run_edges(report)
    report.epilogue = [
        "A9-4 is the BACKFILL only. It deliberately does NOT delete A9-3's "
        "`metadata.seller_ref_missing` legacy closure path, nor the legacy "
        "external_seed bucket-heal helpers.",
        "Those kills happen ONLY AFTER this backfill has RUN against prod and the "
        "W1-style parity window is green (drift=0). Sequence: run --execute -> watch "
        "seller_ref_missing -> 0 and catalog_external_seed -> 0 over the parity week "
        "-> then a separate packet removes the legacy paths.",
        "Sigs/canonical URLs were FROZEN throughout (assert_sig_frozen_sql on every "
        "re-key statement; per-batch byte-identical sig parity).",
    ]
    return report


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="A9-4 seller-of-record backfill (dry-run default).")
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""),
                    help="Postgres URL (defaults to $DATABASE_URL).")
    ap.add_argument("--execute", action="store_true",
                    help="Perform writes. WITHOUT this flag the run is READ-ONLY (dry-run).")
    ap.add_argument("--max-batches", type=int, default=0,
                    help="stop the catalog phase after N batches (canary; 0 = no limit)")
    ap.add_argument("--repair-listings", action="store_true",
                    help="migrate any listing still under the sentinel for products "
                         "ALREADY checkpointed done (e.g. multi-seed stragglers). "
                         "Honors --execute; dry-run reports what would move.")
    ap.add_argument("--max-products", type=int, default=0,
                    help="cap the catalog SCAN itself (canary; 0 = all). Planning "
                         "resolves per-row over the network — a capped canary must "
                         "not pay the full-corpus planning cost.")
    ap.add_argument("--batch-size", type=int, default=100,
                    help="Products per catalog re-key batch (default 100).")
    ap.add_argument("--phases", default="seeds,barekey,catalog,edges",
                    help="Comma-separated subset of: seeds,barekey,catalog,edges.")
    ap.add_argument("--report", default=None,
                    help="Path to write the JSON report (default: scratch/report file).")
    return ap.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    if not args.db_url:
        print("ERROR: --db-url or $DATABASE_URL is required.", file=sys.stderr)
        return 2
    # db.database binds Database(DATABASE_URL) at import (db/database.py:108); by the
    # time we get here the module tree is already imported, so main() re-execs with
    # DATABASE_URL in the environment before any import runs (see `main`). Assert the
    # binding matches so a stale sqlite binding can never silently win.
    from db.database import DATABASE_URL as _BOUND_URL, database
    from services import seller_identity as si_mod

    if args.db_url not in (_BOUND_URL, _BOUND_URL.replace("postgresql+asyncpg://", "postgresql://", 1)):
        print(
            f"ERROR: db.database is bound to {_BOUND_URL[:60]!r} but --db-url is "
            f"{args.db_url[:60]!r}. Invoke with DATABASE_URL set in the environment, "
            f"or let this script re-exec.",
            file=sys.stderr,
        )
        return 3

    phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    if getattr(args, "repair_listings", False) and "repair-listings" not in phases:
        phases.append("repair-listings")
    await database.connect()
    try:
        report = await run_backfill(
            database=database, si_mod=si_mod, execute=args.execute,
            max_batches=args.max_batches, max_products=args.max_products,
            batch_size=args.batch_size, phases=phases,
        )
    finally:
        await database.disconnect()

    payload = report.to_json()
    out = json.dumps(payload, indent=2, default=str)
    print(out)
    report_path = args.report or str(
        Path(os.getenv("SCRATCHPAD_DIR", ".")) / f"a9_4_backfill_report_{report.mode}.json"
    )
    try:
        Path(report_path).write_text(out)
        print(f"\n[report written] {report_path}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[report write failed] {exc}", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)
    # `db.database` resolves DATABASE_URL at IMPORT. If --db-url was supplied and the
    # environment does not already carry it, re-exec this process with DATABASE_URL
    # set so the module binds to the right DB (no stale sqlite binding). The re-exec
    # is skipped once the env matches (idempotent) and never happens under pytest
    # (tests import in-process with DATABASE_URL pre-set).
    if args.db_url and os.environ.get("DATABASE_URL", "") != args.db_url \
            and os.environ.get("_A9_4_REEXEC") != "1":
        env = dict(os.environ)
        env["DATABASE_URL"] = args.db_url
        env["_A9_4_REEXEC"] = "1"
        os.execve(sys.executable,
                  [sys.executable, "-m", "scripts.backfill_seller_of_record", *(argv or sys.argv[1:])],
                  env)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
