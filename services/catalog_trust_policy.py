"""Layer C1 — catalog_trust_policy (Python port).

Pure function: (inputs) -> catalog_row_trust shape.

This is the Python parity port of PIVOTA-Agent/src/services/catalogTrustPolicy.js
(POLICY_VERSION ``c1.v0.5``). Producer dual-write call sites in pivota-backend
(catalog_sync_service, source_quarantine helpers) invoke ``derive_trust`` to
populate ``catalog_row_trust`` so the reader contract stays live between
periodic backfills.

Inputs are collected from the existing tables this module does NOT own:
  catalog_products, catalog_offers, index_pipeline_state,
  pdp_identity_listing, external_product_seeds, merchant_stores,
  catalog_source_quarantine, pdp_identity_override.

Output is a row matching ``db/migrations/136_catalog_row_trust.sql``.

Contract: every reader downstream depends on (serving_decision,
serving_reason_codes). The other fields are advisory and used for ranking,
debugging, and shadow-comparison.

Versioning: POLICY_VERSION must bump on any change to derivation LOGIC — the
mapping from inputs to a decision. It must NOT bump when only an INPUT the
current logic cannot reach changes.

CORRECTION 2026-07-28: this paragraph used to say "the backfill uses
POLICY_VERSION to detect stale rows". It does not, and the error misled a
reviewer into arguing a bump was mandatory. jobs/catalog_row_trust_backfill_cron
selects by ``ORDER BY crt.updated_at ASC NULLS FIRST`` with NO policy_version
predicate — it re-derives every row every pass (~14k rows against a 50k limit).
POLICY_VERSION appears only as one of five OR'd terms in the UPSERT write-guard
(catalog_row_trust_upserter), so it is a write-trigger and an observability tag,
never a row selector.

The cost of a cosmetic bump is therefore one extra UPDATE per row, once — not
re-derivation — plus, and this is the part that matters, it re-opens the
split-brain window where the two services disagree on the version until both are
deployed, and the resulting updated_at churn defeats the cron's stalest-first
ordering. The upside forgone is forensic: rows derived before and after the
change become indistinguishable in the table.

Worked example — P3, 2026-07-25. It changed how ``pdp_route_resolvable`` is
COMPUTED (services/pdp_renderability learned the minted attached_product_key
lane, flipping 2,051 rows to renderable). That input is read in exactly one
place, ``_derive_serving_decision``, behind ``_renderable_gate_enabled()``,
which is default OFF in prod — so every derived decision and every reason code
is byte-identical before and after, on all 14,104 rows. No bump. The version
bumps the day the GATE flips, not the day the input gets more accurate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

# The rig denylist. Imported rather than inlined: a copy here would be a FIFTH
# copy of the list, and a rig excluded everywhere but here is exactly the bug
# the gate closes. test_merchant_policy imports only stdlib, so no cycle.
from services.source_quarantine import bare_domain
from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

# Worked example 2 — the test-merchant gate, 2026-07-27. It adds a NEW arm to
# _derive_serving_decision (a real logic change), but the arm sits after the
# lifecycle/index gates and every rig row in catalog_row_trust is already
# 'blocked' by those (measured: 1,561/1,561). So every decision and every
# reason code is byte-identical on all ~14k rows, and the same no-bump
# reasoning as P3 applies: bumping would cost a full rewrite and re-open the
# split-brain window for a change no row can currently observe. The version
# bumps the day a rig row would actually reach 'public', not the day the
# backstop is installed.
# Worked example 3 — the per-row price gate, 2026-07-31. This one DOES bump,
# and the contrast with the two examples above is the whole point of keeping
# them. P3 and the test-merchant gate were byte-identical on every row (a
# default-OFF flag; an arm no row could reach), so a bump would have bought
# forensics at the price of a 14k-row rewrite. OFFER_PRICE_MISSING flips 4 real
# decisions from 'public' to 'blocked' — rows CAN observe it, which is exactly
# the condition the rule names. "The version bumps the day the GATE flips."
#
# 🚨 SHIP THE TWO REPOS BACK TO BACK. The bump re-opens the split-brain window
# against PIVOTA-Agent (deployed at c1.v0.5, measured on 14,124 prod trust rows
# 2026-07-31), and that twin also needs the OFFER_PRICE_MISSING mirror itself —
# both halves belong in one merge pair, exactly as P3 did for the seed-route
# predicate.
POLICY_VERSION = "c1.v0.6"

# ---- Reason codes (authoritative vocabulary) -------------------------------
#
# Public:
#   none required, but PUBLIC_PASSTHROUGH may be set for traceability.
#
# Shadow (would have served under legacy gates, but contract says caution):
#   IDENTITY_REVIEW_REQUIRED_LIVE_READ — pdp_identity_listing.identity_status=
#     'review_required'. Audit counted ~60 of these among external mirror rows.
#   IDENTITY_CONFIDENCE_NULL — IPS serving_eligible=true but no identity row
#     or identity_confidence IS NULL. Only emitted for non-first-party sources
#     (i.e., external_seed). For first-party merchants the corresponding
#     advisory is IDENTITY_NOT_APPLICABLE_FIRST_PARTY (see below).
#   IDENTITY_LIVE_READ_DISABLED — identity_status='approved' but
#     live_read_enabled=false. First-party sources are exempt.
#   FRESHNESS_UNVERIFIED — never observed a verification timestamp.
#
# Advisory (does not flip decision):
#   IDENTITY_NOT_APPLICABLE_FIRST_PARTY — c1.v0.3+. Marks rows where the
#     merchant IS the source of truth, so the identity-pipeline gates (which
#     exist to verify scraped third-party content) don't apply. Emitted for
#     ``product.merchant_id != 'external_seed'`` when identity is missing or
#     low-info.
#
# Blocked (no public surface):
#   TEST_MERCHANT_EXCLUDED           — 2026-07-27. merchant_id is a known rig
#     (services/test_merchant_policy KNOWN_TEST_MERCHANT_IDS). Evaluated after
#     the lifecycle/index gates, so it only fires on a rig that would
#     OTHERWISE have reached public/shadow — an already-blocked rig keeps its
#     real reason. Fires on zero rows today; it is the backstop for the day a
#     rig's suppression is cleared. Baked-in ids only, never the env hatch:
#     this table is shared state written by both twins.
#   SOURCE_QUARANTINED               — catalog_source_quarantine active match.
#   ROW_TOMBSTONED                   — catalog_products.suppression_reason set.
#   EXTERNAL_SEED_INACTIVE           — external_product_seeds.status != 'active'.
#   MERCHANT_STORE_INACTIVE          — merchant_stores.status != 'active'.
#   INDEX_NOT_SERVING_ELIGIBLE       — index_pipeline_state.serving_eligible=false.
#   OFFER_PRICE_MISSING              — 2026-07-31. THIS product_key carries no
#     unsuppressed catalog_offers row with a price > 0 (services/priced_offer_sql).
#     Evaluated right after the index gate, so a row already blocked upstream
#     keeps its real reason and only a row that would otherwise reach
#     public/shadow is reclassified.
#
#     WHY IT CANNOT BE READ OFF ips.serving_eligible, which is the obvious
#     objection: index_pipeline_state is keyed by CONTENT_KEY (migration 098) and
#     the upserters join it `ips.content_key = cp.content_key`, but trust is
#     keyed by PRODUCT_KEY and every product_key mints its own
#     pivota_signature_id — its own public PDP. index_pipeline_state_service's
#     `has_price` is per-row and always was correct; _select_content_key_state
#     then stores the BEST row's state for the whole content_key, so a
#     price-less row sharing a content_key with a priced sibling inherits
#     serving_eligible=true and publishes a price-less page. Measured on prod
#     2026-07-31: 4 Tom Ford fragrance PDPs, each with exactly one unsuppressed
#     offer whose list_price, merchant_effective_price and estimated_best_price
#     were ALL NULL, sitting at trust 'public' behind a priced
#     tomfordbeauty.com sibling. This gate asks the price question of the row it
#     is actually deciding.
#
#     TRI-STATE, like PDP_ROUTE_UNRESOLVABLE: only an explicit False blocks. A
#     producer that does not compute `row_has_priced_offer` is byte-identical to
#     c1.v0.5.
#
#     Blast radius measured on prod 2026-07-31, the whole reason it is NOT
#     env-gated: EXACTLY 4 rows move, public 'public'->'blocked'. 2,535 rows
#     also lack a priced offer of their own and every one of them is ALREADY
#     'blocked' by an upstream gate, so their decision and reason codes do not
#     change. Re-measure before shipping; the number is only as current as the
#     last sweep.
#
#     ⚠️ SAME TWIN-SYMMETRY RULE AS THE RENDERABLE GATE BELOW. PIVOTA-Agent
#     (src/services/catalogTrustPolicy.js + catalogRowTrustUpserter.js) writes
#     this same table and does NOT compute this input yet, so it re-derives
#     those 4 rows 'public' on its next identity event for them. This repo's 6h
#     cron re-asserts 'blocked', so steady state is correct and
#     `public_without_priced_offer` catches any regression — but the twin must
#     land the mirror (select the column, add the gate) to close the window.
#     The 4 rows carry live pdp_identity_listing rows, so the window is real.
#   PUBLISH_STATE_NOT_PUBLIC         — catalog_products.publish_state != 'public'.
#   IDENTITY_CONFLICT                — identity_status='conflict'.
#   OFFER_SUPPRESSED                 — subject_type='offer' with offer.suppression_reason set.
#   PDP_ROUTE_UNRESOLVABLE           — c1.v0.5. The gateway has no resolvable
#     content route for the row, so its public PDP never answers with a real
#     product page: it is either a hard HTTP 500 or a generic noindex shell
#     carrying no product JSON-LD (both measured; see services/pdp_renderability
#     for the truth table). GATED on CATALOG_TRUST_RENDERABLE_GATE, default OFF.
#     Blast radius, re-measured on prod after P3 (2026-07-25): turning it on now
#     demotes exactly ONE row — the url_audit audit-minted row — taking public
#     3,937 -> 3,936 and darkening 1 product. It was 1,376 rows / 1,011 dark
#     products before P3 taught the gateway the minted lane, which is why the
#     flip was a founder call then and is close to free now. Re-measure before
#     flipping; the number is only as current as the last sweep.
#
#     ⚠️ THE FLAG IS PER-SERVICE AND THE TWINS SHARE ONE DATABASE. PIVOTA-Agent
#     runs the same policy (src/services/catalogTrustPolicy.js) and writes the
#     same catalog_row_trust table, from prod RUNTIME — pdpIdentityGraph calls
#     upsertCatalogRowTrustForSourceListingRefs on every live-read promotion and
#     identity override. Set CATALOG_TRUST_RENDERABLE_GATE on BOTH Railway
#     services or NEITHER: with it on one side only, this backend blocks the
#     rows and Node re-derives them public on the next identity event, so rows
#     FLAP public<->blocked on the live serving surface. The same applies to
#     POLICY_VERSION itself — ship the two repos back to back.
#
#     The SEED-ROUTE INPUT is the other half of that symmetry and is NOT
#     env-gated: both repos compute pdp_seed_route_ok from their own copy of
#     the renderability predicate (services/pdp_renderability.py and
#     PIVOTA-Agent src/services/pdpRenderability.js). A predicate edit in one
#     repo only is the same split-brain with no flag to blame, which is why the
#     two files pin each other on a byte-identical SQL string in their test
#     suites. P3 changed that predicate in both, in one merge pair.


class _ReasonCodes:
    PUBLIC_PASSTHROUGH = "PUBLIC_PASSTHROUGH"

    IDENTITY_REVIEW_REQUIRED_LIVE_READ = "IDENTITY_REVIEW_REQUIRED_LIVE_READ"
    IDENTITY_CONFIDENCE_NULL = "IDENTITY_CONFIDENCE_NULL"
    IDENTITY_LIVE_READ_DISABLED = "IDENTITY_LIVE_READ_DISABLED"
    IDENTITY_NOT_APPLICABLE_FIRST_PARTY = "IDENTITY_NOT_APPLICABLE_FIRST_PARTY"
    FRESHNESS_UNVERIFIED = "FRESHNESS_UNVERIFIED"

    SOURCE_QUARANTINED = "SOURCE_QUARANTINED"
    ROW_TOMBSTONED = "ROW_TOMBSTONED"
    EXTERNAL_SEED_INACTIVE = "EXTERNAL_SEED_INACTIVE"
    MERCHANT_STORE_INACTIVE = "MERCHANT_STORE_INACTIVE"
    INDEX_NOT_SERVING_ELIGIBLE = "INDEX_NOT_SERVING_ELIGIBLE"
    PUBLISH_STATE_NOT_PUBLIC = "PUBLISH_STATE_NOT_PUBLIC"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    OFFER_SUPPRESSED = "OFFER_SUPPRESSED"
    PDP_ROUTE_UNRESOLVABLE = "PDP_ROUTE_UNRESOLVABLE"
    TEST_MERCHANT_EXCLUDED = "TEST_MERCHANT_EXCLUDED"
    OFFER_PRICE_MISSING = "OFFER_PRICE_MISSING"


REASON_CODES = _ReasonCodes()
REASON_CODE_VOCABULARY = frozenset(
    {
        v
        for k, v in vars(_ReasonCodes).items()
        if not k.startswith("_") and isinstance(v, str)
    }
)

VALID_SUBJECT_TYPES = frozenset({"product", "offer", "listing", "content_key"})


def _index_eligible_read_enabled() -> bool:
    """ADR-008 SLICE 1 read flag. When ON, the index-pipeline serving gate in
    the trust policy widens from serving_eligible to
    (serving_eligible OR index_eligible) — the OFFER-FREE citation floor.
    Default OFF ⇒ byte-identical to today (serving_eligible only).

    ⚠️ THIS ARM IS CURRENTLY UNREACHABLE FROM THE UPSERTERS, DELIBERATELY.
    Neither ``services/catalog_row_trust_upserter`` nor the Node twin
    (PIVOTA-Agent ``src/services/catalogRowTrustUpserter.js`` and its backfill)
    selects ``ips.index_eligible``, so the ``index_eligible is True`` test is
    never satisfied and both writers compute serving_eligible-only regardless of
    this flag. That is what keeps them AGREEING, and agreement is the point:
    they share one ``catalog_row_trust`` table and the UPSERT rewrites a row
    whenever ``serving_decision`` differs.

    Selecting the column would break that TODAY, because the flag is set
    ASYMMETRICALLY in prod: ``INDEX_ELIGIBLE_READ=1`` on this service (`web`),
    UNSET on the PIVOTA-Agent service (verified 2026-07-25). Wire the column up
    and the ~100 prod rows with ``index_eligible IS TRUE AND serving_eligible IS
    NOT TRUE`` would be derived ``public`` by this repo's 6h cron and ``blocked``
    by Node on the next identity event — flapping forever.

    Known consequence of leaving it unreachable: the citation READ path
    (``services/pivot_query_service``) DOES honor ``index_eligible``, so those
    ~100 rows are recall-eligible there while trust keeps blocking them.
    Closing that gap is a founder call, not a code default, and it is a
    THREE-part change — select the column in BOTH joins, and set
    ``INDEX_ELIGIBLE_READ`` on BOTH Railway services in the same operation.
    Do not do one part."""
    return (
        (os.getenv("INDEX_ELIGIBLE_READ") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _renderable_gate_enabled() -> bool:
    """c1.v0.5 gate: does 'public' have to imply a renderable PDP?

    Default OFF ⇒ byte-identical to c1.v0.4. Flipping it ON blocks every row
    whose ``pdp_route_resolvable`` input is explicitly ``False``.

    Why a flag and not a straight fix: when this shipped, the rows it caught
    were real, quality-eligible products — 1,375 Path-C minted canonicals whose
    seeds attach by ``attached_product_key`` so the gateway's
    ``external_product_id`` lookup never found them, plus 1 audit-minted
    ``url_audit`` row. Their PDPs hard-500ed, so serving them was a broken
    promise, but 1,011 of them had NO renderable sibling row: flipping the gate
    would have darkened the product entirely and dropped public 3,937 → 2,561
    (40% → 26% of the serving corpus). The RIGHT fix was always P3 — teach the
    gateway to resolve minted canonicals through ``attached_product_key``
    instead of hiding them — and the gate was the honest fallback if P3 slipped.

    P3 SHIPPED 2026-07-25 (PIVOTA-Agent, the LANE 1 arm of the seed LATERAL in
    ``resolveCatalogProductRefFromPivotaSignatureInner``). Re-measured after it:
    this gate would now block ONE row, the url_audit one, for public 3,937 →
    3,936. The flag stays — the lane it guards is real and a future source
    system can repopulate it — but it is no longer holding back a fix.
    """
    return (
        (os.getenv("CATALOG_TRUST_RENDERABLE_GATE") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


# Freshness thresholds (seconds). Tuned to keep external_seed which refreshes
# ~24h in the 'fresh' bucket and to mark internal merchant catalog stale after
# a week without sync.
FRESH_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
STALE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


# ---- Public API ------------------------------------------------------------


def derive_trust(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the catalog_row_trust row for a single subject.

    See the module docstring and the Node module for the full input shape.
    ``inputs`` is a mapping with the following optional keys:
      subject_type, subject_key — required
      product, offer, identity, ips, external_seed, merchant_store, override,
      active_quarantines, now

    Returns a dict matching the catalog_row_trust columns (without updated_at).
    """
    now = inputs.get("now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    subject_type = inputs.get("subject_type")
    subject_key = inputs.get("subject_key")

    if subject_type not in VALID_SUBJECT_TYPES:
        raise ValueError(f"invalid subject_type: {subject_type!r}")
    if not isinstance(subject_key, str) or not subject_key:
        raise ValueError("subject_key is required")

    product = inputs.get("product") or None
    offer = inputs.get("offer") or None
    identity = inputs.get("identity") or None
    ips = inputs.get("ips") or None
    external_seed = inputs.get("external_seed") or None
    merchant_store = inputs.get("merchant_store") or None
    override = inputs.get("override") or None
    # c1.v0.5 tri-state. True = the gateway can resolve a PDP content route;
    # False = it cannot (the PDP answers with a 500 or a bare noindex shell);
    # None/absent = the caller did not compute it. Absence NEVER blocks, so any
    # producer not yet taught to supply it keeps its c1.v0.4 output exactly.
    raw_route_resolvable = inputs.get("pdp_route_resolvable")
    pdp_route_resolvable = (
        None if raw_route_resolvable is None else bool(raw_route_resolvable)
    )
    # Same tri-state contract. True = this product_key has its own unsuppressed
    # priced offer, False = it does not, None/absent = not computed and the
    # OFFER_PRICE_MISSING gate stays silent.
    raw_row_priced = inputs.get("row_has_priced_offer")
    row_has_priced_offer = None if raw_row_priced is None else bool(raw_row_priced)
    raw_quarantines = inputs.get("active_quarantines") or []
    active_quarantines = list(raw_quarantines) if isinstance(raw_quarantines, Iterable) else []

    reasons: list[str] = []

    source_lifecycle = _derive_source_lifecycle(
        product=product,
        external_seed=external_seed,
        merchant_store=merchant_store,
        active_quarantines=active_quarantines,
        reasons=reasons,
        now=now,
    )

    identity_decision = _derive_identity(
        identity=identity,
        override=override,
        reasons=reasons,
    )

    freshness = _derive_freshness(
        product=product,
        ips=ips,
        external_seed=external_seed,
        now=now,
    )
    if freshness["state"] == "unverified":
        reasons.append(REASON_CODES.FRESHNESS_UNVERIFIED)

    serving = _derive_serving_decision(
        subject_type=subject_type,
        product=product,
        offer=offer,
        ips=ips,
        source_lifecycle=source_lifecycle,
        identity_decision=identity_decision,
        pdp_route_resolvable=pdp_route_resolvable,
        row_has_priced_offer=row_has_priced_offer,
        reasons=reasons,
    )

    return {
        "subject_type": subject_type,
        "subject_key": subject_key,
        "product_key": _get(product, "product_key"),
        # The identity listing's PK doubles as the source_listing_ref; for
        # external_seed rows the external_product_seeds.id is preferred.
        "source_listing_ref": (
            _get(identity, "source_listing_ref")
            or (
                str(_get(external_seed, "id"))
                if _get(external_seed, "id") is not None
                else None
            )
            or _get(product, "source_ref")
        ),
        "content_key": _get(product, "content_key"),
        "source_id": None,  # forward-compat — Layer A1 source registry not yet shipped
        "source_domain": (
            _get(product, "source_domain")
            or _get(external_seed, "domain")
            or _get(merchant_store, "domain")
        ),
        "source_lifecycle_state": source_lifecycle["state"],
        "source_last_checked_at": (
            _get(external_seed, "last_seen_at")
            or _get(merchant_store, "last_sync")
        ),
        "identity_status": identity_decision["status"],
        "identity_confidence": identity_decision["confidence"],
        # Phase 1: only the sellable_item_group_id is populated directly from
        # pdp_identity_listing. matched_product_key / matched_content_key
        # require a sibling-row lookup (resolved in Phase 2 dual-write); leave
        # null here.
        "matched_product_key": None,
        "matched_content_key": None,
        "matched_sellable_item_group_id": _get(identity, "sellable_item_group_id"),
        "freshness_state": freshness["state"],
        "last_verified_at": freshness["last_verified_at"],
        "verification_source": freshness["verification_source"],
        "serving_decision": serving["decision"],
        "serving_reason_codes": _dedupe(reasons),
        "manual_override_id": _get(override, "id"),
        "policy_version": POLICY_VERSION,
    }


# ---- Source lifecycle ------------------------------------------------------


def _derive_source_lifecycle(
    *,
    product: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    merchant_store: Optional[Mapping[str, Any]],
    active_quarantines: list[Any],
    reasons: list[str],
    now: datetime,
) -> dict[str, Any]:
    # Quarantine wins over everything.
    if _is_quarantined(
        product=product,
        external_seed=external_seed,
        merchant_store=merchant_store,
        active_quarantines=active_quarantines,
        now=now,
    ):
        reasons.append(REASON_CODES.SOURCE_QUARANTINED)
        return {"state": "quarantined"}

    # Tombstone (PR #666 / migration 135).
    if _get(product, "suppression_reason"):
        reasons.append(REASON_CODES.ROW_TOMBSTONED)
        return {"state": "tombstoned"}

    # External seed lifecycle.
    if external_seed is not None:
        status = str(_get(external_seed, "status") or "").lower()
        if status == "active":
            return {"state": "active"}
        if status in ("disabled", "inactive"):
            reasons.append(REASON_CODES.EXTERNAL_SEED_INACTIVE)
            return {"state": "inactive"}
        if status == "suspect":
            return {"state": "suspect"}
        return {"state": "unknown"}

    # Merchant store lifecycle.
    if merchant_store is not None:
        status = str(_get(merchant_store, "status") or "").lower()
        if status in ("active", "connected"):
            return {"state": "active"}
        # retired_test_rig is a decommissioned demo/test store: it must be treated
        # as inactive (not "unknown"), else its catalog leaks into serving.
        if status in ("inactive", "disconnected", "retired_test_rig"):
            reasons.append(REASON_CODES.MERCHANT_STORE_INACTIVE)
            return {"state": "inactive"}
        return {"state": "unknown"}

    return {"state": "unknown"}


def _is_quarantined(
    *,
    product: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    merchant_store: Optional[Mapping[str, Any]],
    active_quarantines: list[Any],
    now: datetime,
) -> bool:
    if not active_quarantines:
        return False
    now_ms = now.timestamp() * 1000.0

    # Normalised through the SAME helper the SQL anti-join and the Python
    # matcher use (lowercase + `www.` strip). A bare `.lower()` here made this
    # comparator disagree with them: a `www.mintree.us` quarantine — which
    # `create_quarantine` accepts verbatim — excluded the row from
    # `fetch_products_for_key` while THIS check returned False, so
    # `serving_decision` stayed `public` and the URL stayed in the sitemap.
    # Sitemap advertises a PDP with no view row. See #1639.
    domain = bare_domain(
        _get(product, "source_domain")
        or _get(external_seed, "domain")
        or _get(merchant_store, "domain")
        or ""
    )
    merchant_id = _get(product, "merchant_id") or _get(merchant_store, "merchant_id")
    platform = _get(product, "platform") or _get(merchant_store, "platform")
    source_system = _get(product, "source_system")
    source_ref = _get(product, "source_system_ref")

    for q in active_quarantines:
        if _get(q, "state") != "active":
            continue
        expires_at = _get(q, "expires_at")
        if expires_at is not None:
            ts = _to_datetime(expires_at)
            if ts is not None and ts.timestamp() * 1000.0 <= now_ms:
                continue

        match_type = _get(q, "match_type")
        match_value = _get(q, "match_value")
        if match_value is None:
            continue

        if match_type == "domain" and domain:
            # Both sides through the shared normaliser, not just the row.
            if bare_domain(match_value) == domain:
                return True
        elif match_type == "merchant_platform" and merchant_id and platform:
            if match_value == f"{merchant_id}:{platform}":
                return True
        elif match_type == "source_system_ref" and source_system and source_ref:
            if match_value == f"{source_system}:{source_ref}":
                return True
    return False


# ---- Identity --------------------------------------------------------------


def _derive_identity(
    *,
    identity: Optional[Mapping[str, Any]],
    override: Optional[Mapping[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    # Manual override of identity wins (rare but authoritative).
    if (
        override is not None
        and _get(override, "action_type") == "force_exact_group"
        and _get(override, "active")
    ):
        return {
            "status": "approved",
            "confidence": 1.0,
            "live_read": True,
            "review_required": False,
        }
    if (
        override is not None
        and _get(override, "action_type") == "force_review_required"
        and _get(override, "active")
    ):
        return {
            "status": "review_required",
            "confidence": _get(identity, "identity_confidence"),
            "live_read": _get(identity, "live_read_enabled") is True,
            "review_required": True,
        }

    if identity is None:
        # No identity row at all. The 504 external-mirror cases in the audit
        # largely fall here.
        return {
            "status": "unknown",
            "confidence": None,
            "live_read": None,
            "review_required": None,
        }

    status = str(_get(identity, "identity_status") or "").lower()
    live_read = _get(identity, "live_read_enabled") is True
    review_required = _get(identity, "review_required") is True
    raw_confidence = _get(identity, "identity_confidence")
    confidence = None if raw_confidence is None else _clamp(float(raw_confidence), 0.0, 1.0)

    if status == "conflict":
        reasons.append(REASON_CODES.IDENTITY_CONFLICT)
        return {
            "status": "conflict",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    if status == "approved":
        if review_required:
            # Approved but flagged for review — degrade to review_required so
            # downstream readers don't treat as fully trusted.
            return {
                "status": "review_required",
                "confidence": confidence,
                "live_read": live_read,
                "review_required": review_required,
            }
        return {
            "status": "approved",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    if status == "review_required":
        return {
            "status": "review_required",
            "confidence": confidence,
            "live_read": live_read,
            "review_required": review_required,
        }

    return {
        "status": "unknown",
        "confidence": confidence,
        "live_read": live_read,
        "review_required": review_required,
    }


# ---- Freshness -------------------------------------------------------------


def _derive_freshness(
    *,
    product: Optional[Mapping[str, Any]],
    ips: Optional[Mapping[str, Any]],
    external_seed: Optional[Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    candidates = [
        (_get(ips, "last_extracted_at"), "identity_resolver"),
        (_get(product, "last_seen_in_sync_at"), _derive_verification_source(product)),
        (_get(external_seed, "last_seen_at"), "external_seed_scrape"),
        (_get(ips, "quality_scored_at"), "index_pipeline"),
    ]

    chosen_ts: Optional[datetime] = None
    chosen_src: Optional[str] = None
    for raw_ts, src in candidates:
        if raw_ts is None:
            continue
        ts = _to_datetime(raw_ts)
        if ts is None:
            continue
        if chosen_ts is None or ts > chosen_ts:
            chosen_ts = ts
            chosen_src = src

    if chosen_ts is None:
        return {
            "state": "unverified",
            "last_verified_at": None,
            "verification_source": None,
        }

    age_seconds = (now - chosen_ts).total_seconds()
    if age_seconds <= FRESH_MAX_AGE_SECONDS:
        state = "fresh"
    elif age_seconds <= STALE_MAX_AGE_SECONDS:
        state = "stale"
    else:
        state = "expired"

    return {
        "state": state,
        "last_verified_at": chosen_ts,
        "verification_source": chosen_src,
    }


def _derive_verification_source(product: Optional[Mapping[str, Any]]) -> Optional[str]:
    if product is None:
        return None
    if _get(product, "merchant_id") == "external_seed":
        return "external_seed_scrape"
    platform = _get(product, "platform")
    if platform == "shopify":
        return "shopify_sync"
    if platform == "wix":
        return "wix_sync"
    return "merchant_sync"


# ---- Serving decision ------------------------------------------------------


def _derive_serving_decision(
    *,
    subject_type: str,
    product: Optional[Mapping[str, Any]],
    offer: Optional[Mapping[str, Any]],
    ips: Optional[Mapping[str, Any]],
    source_lifecycle: Mapping[str, Any],
    identity_decision: Mapping[str, Any],
    pdp_route_resolvable: Optional[bool] = None,
    row_has_priced_offer: Optional[bool] = None,
    reasons: list[str],
) -> dict[str, Any]:
    # Offer-specific block: suppressed offers never surface.
    if subject_type == "offer" and offer is not None and _get(offer, "suppression_reason"):
        reasons.append(REASON_CODES.OFFER_SUPPRESSED)
        return {"decision": "blocked"}

    # Hard blocks.
    lifecycle_state = source_lifecycle["state"]
    blocked = (
        lifecycle_state == "quarantined"
        or lifecycle_state == "tombstoned"
        or lifecycle_state == "inactive"
        or identity_decision["status"] == "conflict"
    )
    if blocked:
        return {"decision": "blocked"}

    # Index pipeline gate. All public readers honor this today; the contract
    # makes it explicit. sync_status='live' is the equivalent for catalog rows
    # before they reach IPS — see migration 084.
    #
    # c1.v0.4: for non-first-party (external_seed) catalog rows, a missing IPS
    # row is treated as INDEX_NOT_SERVING_ELIGIBLE. Pre-c1.v0.4 the policy let
    # ips=None pass on the assumption "no IPS opinion = no reason to block",
    # but Phase 3c parity found 80 external_seed catalog products with public
    # trust + no IPS row — i.e., shipping content the index pipeline hasn't
    # quality-gated yet. First-party rows (MOYU/GR/PawStyle/etc.) keep the
    # legacy behavior since first-party merchants are the source of truth and
    # IPS coverage there is sparse by design.
    # ADR-009 observed-seller trust tier (docs/adr009_observed_seller_trust_decision.md,
    # Option C). Classify by content SOURCE, not the legacy merchant_id='external_seed'
    # string: external seeds now mirror under per-brand observed sellers (merch_obs_…).
    #   - is_external_seed_content: legacy 'external_seed' lump OR an observed seller —
    #     both are scraped supply and must clear the index/quality gate.
    #   - is_observed_seller: the brand's own D2C crawl (merch_obs_), authoritative
    #     for its own content, so exempt from the identity-COVERAGE shadow gates
    #     (below) like a first-party merchant — but NOT from the index/quality gate.
    _merchant_id = str(_get(product, "merchant_id") or "") if product is not None else ""
    _platform = str(_get(product, "platform") or "").lower() if product is not None else ""
    is_external_seed_content = (
        _platform == "external_seed"
        or _merchant_id == "external_seed"
        or _merchant_id.startswith("merch_obs_")
    )
    is_observed_seller = _merchant_id.startswith("merch_obs_")
    if product is not None:
        # c1.v0.5 (2026-07-29): a missing IPS row fails CLOSED for EVERY lane,
        # not only external-seed content. The first-party carve-out below this
        # line used to let `ips is None` fall through to public on the theory
        # that "first-party merchants are the source of truth and IPS coverage
        # there is sparse by design". Both halves of that theory failed the
        # first time a real merchant-sync row arrived:
        #
        #   * Measured 2026-07-29 (Wix pilot merch_e68c20b0189746d0): 20 rows
        #     synced with content_key NULL — structurally incapable of ever
        #     having an IPS row — and every one went trust-public with NO
        #     quality gate, no scoring, no eligibility. Only the gateway's own
        #     fail-closed eligibility lookup kept them from serving, and
        #     `public_not_renderable` went red (20 > 0) within the hour.
        #   * "Sparse by design" described a corpus where every first-party
        #     merchant was a retired test rig whose rows were already blocked
        #     upstream. Blast radius of closing this measured on prod
        #     2026-07-29: EXACTLY those 20 pilot rows and zero others.
        #
        # An unscored row must not be public — that is the single lesson this
        # week's serving-coverage work keeps re-teaching (the 04:00 reclassify,
        # the v3 rescore lockstep, the url_audit stubs). The correct lifecycle
        # for a fresh sync is blocked -> scored -> eligible -> public, and rows
        # without a content_key stay blocked until identity is repaired.
        if ips is None:
            reasons.append(REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE)
            return {"decision": "blocked"}
        if ips is not None:
            # ADR-008 SLICE 1: the citation read surface accepts the OFFER-FREE
            # index_eligible floor when INDEX_ELIGIBLE_READ is ON. Flag OFF ⇒
            # serving_eligible-only, byte-identical to the pre-ADR-008 gate.
            ips_eligible = _get(ips, "serving_eligible") is True or (
                _index_eligible_read_enabled()
                and _get(ips, "index_eligible") is True
            )
            if not ips_eligible:
                reasons.append(REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE)
                return {"decision": "blocked"}
        sync_status = str(_get(product, "sync_status") or "").lower()
        if sync_status and sync_status != "live":
            reasons.append(REASON_CODES.PUBLISH_STATE_NOT_PUBLIC)
            return {"decision": "blocked"}

        # PER-ROW price gate. Immediately after the index gate on purpose: the
        # index gate answers for the CONTENT_KEY, this one answers for the
        # PRODUCT_KEY being decided, and a row that fails the coarse gate must
        # keep reporting INDEX_NOT_SERVING_ELIGIBLE rather than being relabelled.
        # See the OFFER_PRICE_MISSING entry in the reason-code vocabulary above
        # for the grain argument, the measured 4-row blast radius, and the
        # PIVOTA-Agent mirror this still needs.
        #
        # Tri-state: only an explicit False blocks. `is False` and not
        # `not row_has_priced_offer` — the difference IS the contract, since
        # None must fall through untouched.
        if row_has_priced_offer is False:
            reasons.append(REASON_CODES.OFFER_PRICE_MISSING)
            return {"decision": "blocked"}

    # Test/demo merchant gate. Placed here for the same reason as the
    # renderability gate below: AFTER the lifecycle/index gates, so a rig that
    # is already blocked keeps reporting its real reason (ROW_TOMBSTONED,
    # MERCHANT_STORE_INACTIVE, …) and ONLY a rig that would otherwise reach
    # public/shadow is reclassified. Measured 2026-07-27: all 1,561 rig rows in
    # catalog_row_trust are already 'blocked', so this arm fires on zero rows
    # today and every decision and reason code is byte-identical — which is
    # exactly why POLICY_VERSION does NOT bump for this change (see the
    # versioning note at the top of this file, and the P3 worked example).
    #
    # Why this gate exists at all: before it, the ONLY thing keeping a rig out
    # of 'public' here was data — suppression_reason making the lifecycle
    # 'tombstoned'. Clear the suppression and the rig derived straight through
    # to 'public', while the serving-lane filters correctly excluded it
    # everywhere else. That split-brain was the gap ADR-018's census left open.
    #
    # Reads the BAKED-IN frozenset only (KNOWN_TEST_MERCHANT_IDS), never
    # static_test_merchant_ids() with its env hatch. catalog_row_trust is shared
    # state written by BOTH this service and the agent-repo twin; a per-service
    # env var would make the two derive different decisions for the same row and
    # flap it public↔blocked — the same hazard documented for
    # CATALOG_TRUST_RENDERABLE_GATE. Code-only inputs here; the env hatch still
    # applies on the runtime serving lanes.
    if product is not None:
        _rig_id = str(_get(product, "merchant_id") or "").strip()
        if _rig_id in KNOWN_TEST_MERCHANT_IDS:
            reasons.append(REASON_CODES.TEST_MERCHANT_EXCLUDED)
            return {"decision": "blocked"}

    # c1.v0.5 renderability gate. Deliberately AFTER the lifecycle/index gates
    # so a row that is already blocked keeps reporting its real reason, and
    # only rows that would otherwise reach public/shadow are reclassified.
    # Tri-state: only an explicit False blocks — an absent input (any producer
    # that does not compute it) leaves the decision exactly as c1.v0.4.
    if _renderable_gate_enabled() and pdp_route_resolvable is False:
        reasons.append(REASON_CODES.PDP_ROUTE_UNRESOLVABLE)
        return {"decision": "blocked"}

    # Shadow conditions — would have served under legacy gates, but the
    # contract gates them out of public reads.
    #
    # c1.v0.3 + ADR-009 Option C: exempt from the identity-COVERAGE shadow gates
    # both (a) connected first-party merchants (the merchant IS the source of
    # truth — the pipeline exists to verify scraped third-party content) and
    # (b) per-brand observed sellers (merch_obs_, the brand's own D2C crawl,
    # authoritative for its own content). The legacy anonymous 'external_seed'
    # lump stays subject. review_required and IDENTITY_CONFLICT still apply to
    # everyone (explicit moderation/data-quality signals, not coverage gaps).
    #
    # BUT an observed seller is only "the brand's own crawl" when the seed is
    # seed_kind='self'. A RETAILER-sourced observed seller — a no-D2C brand crawled
    # from a marketplace (VODANA→Amazon), tagged seed_kind='cross' by
    # derive_seed_seller — is NOT authoritative for its own content and must stay
    # subject to the shadow gate (else it serves PUBLIC as brand-official). Gate on
    # the EXPLICIT 'cross' only: a missing / 'self' / legacy-NULL seed_kind stays
    # exempt, so no existing public observed-seller row is demoted.
    _seed_kind = (
        str(_get(product, "seed_kind") or "").strip().lower() if product is not None else ""
    )
    is_observed_seller_exempt = is_observed_seller and _seed_kind != "cross"
    is_identity_coverage_exempt = product is not None and (
        not is_external_seed_content or is_observed_seller_exempt
    )

    if identity_decision["status"] == "review_required":
        reasons.append(REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ)

    missing_confidence = (
        identity_decision["status"] in ("unknown", "approved")
        and identity_decision["confidence"] is None
    )
    if missing_confidence:
        if is_identity_coverage_exempt:
            reasons.append(REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY)
        else:
            reasons.append(REASON_CODES.IDENTITY_CONFIDENCE_NULL)

    if identity_decision["status"] == "approved" and identity_decision["live_read"] is False:
        if not is_identity_coverage_exempt:
            reasons.append(REASON_CODES.IDENTITY_LIVE_READ_DISABLED)

    shadow = (
        identity_decision["status"] == "review_required"
        or REASON_CODES.IDENTITY_CONFIDENCE_NULL in reasons
        or REASON_CODES.IDENTITY_LIVE_READ_DISABLED in reasons
        or (identity_decision["status"] == "unknown" and not is_identity_coverage_exempt)
    )

    if shadow:
        return {"decision": "shadow"}

    reasons.append(REASON_CODES.PUBLIC_PASSTHROUGH)
    return {"decision": "public"}


# ---- Utilities -------------------------------------------------------------


def _get(obj: Optional[Mapping[str, Any]], key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _clamp(value: float, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN check
        return None
    return min(hi, max(lo, v))


def _dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # Postgres/JS ISO-8601 with trailing Z or with offset.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class TrustOutput:
    """Typed view of a derive_trust result, optional for type-aware callers."""

    subject_type: str
    subject_key: str
    serving_decision: str
    serving_reason_codes: tuple[str, ...]
    policy_version: str

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "TrustOutput":
        return cls(
            subject_type=row["subject_type"],
            subject_key=row["subject_key"],
            serving_decision=row["serving_decision"],
            serving_reason_codes=tuple(row["serving_reason_codes"]),
            policy_version=row["policy_version"],
        )


__all__ = [
    "POLICY_VERSION",
    "REASON_CODES",
    "REASON_CODE_VOCABULARY",
    "VALID_SUBJECT_TYPES",
    "FRESH_MAX_AGE_SECONDS",
    "STALE_MAX_AGE_SECONDS",
    "derive_trust",
    "TrustOutput",
]
