"""Connection layer — the founder's 2026-07-27 taxonomy, as one named authority.

**The policy.** "All of our crawled merchants and products should be considered as
real merchants. We just have different layers of connections: 1) crawled, and
transactable. 2) product synced, and transactable. 3) product synced, and PSP
integrated, and transactable."

    layer 1  crawled                        transactable
    layer 2  product synced                 transactable
    layer 3  product synced + PSP integrated transactable

All three are transactable. **The layer describes HOW a transaction executes, not
WHETHER one can.** This supersedes the "the ACP feed is empty by design until a
real merchant connects" position, which defined *real merchant* as *connected via
product sync*. See ``docs/adr/ADR-018-connection-layer-and-priced-serving-lane.md``.

WHY THIS FILE EXISTS AT ALL, given the layer is derivable in one CASE: because it
was derivable in one CASE *in three different places with three different answers*
is how ``psp_connected`` became load-bearing for "may this merchant be served."
One module, one Python function, one SQL twin, in the same file so they cannot
drift silently.

────────────────────────────────────────────────────────────────────────────────
THREE THINGS THE MODEL DOES NOT ABSORB — read before consuming this module.

**(F1) Layers 2 and 3 have zero real population.** Measured in prod 2026-07-27:
the entire ``catalog_track='internal_merchant'`` track is 5 test rigs plus one
brand-authored row that was never synced; every ``merchant_stores`` row with
``status='active'`` is a rig or empty; all 3 ``psp_connected`` merchants are rigs
or deleted. **100% of the real serving catalog is layer 1.** So today this module
returns a constant on real data. That is fine — it fixes the contract before there
is a second value — but do not read a layer as informative yet.

**(F2) The layer number does NOT predict execution quality.** The layers are a
*supply* axis; execution is a separate, orthogonal axis:

  - layer 1 executes via the attributed ``/r`` redirect, and for allowlisted
    brands upgrades at click time to a warm handoff (a pre-built cart on the
    brand's own checkout) — ``services/outbound_warm_handoff.py``;
  - layer 2 (synced, no PSP) executes via… the same attributed redirect. It is a
    data-freshness tier, not a distinct execution path in today's code;
  - layer 3 would execute via the Pivota-orchestrated ACP checkout, which is
    **dark** (``ACP_REST_ENABLED`` unset). It is presently the *least*
    transactable of the three through Pivota.

And warm-handoff eligibility keys on **brand domain**, not on layer — so a
crawled layer-1 COSRX row out-executes a hypothetical non-allowlisted layer-2
row. That is why ``resolve_execution_paths`` is a SEPARATE function taking
separate inputs, and why callers must advertise BOTH: collapsing them into one
"tier" would let a redirect-only item read as one-click, which is exactly the
execution-layer fallback the standing rule forbids.

**(F3) "Merchant" is two entities.** ``merchant_onboarding`` holds parties who
signed up. Crawled sellers are not in it — layer-1 rows carry the synthetic
bucket id ``'external_seed'`` or an ADR-009 observed seller ``merch_obs_*``. So a
per-merchant layer join through ``merchant_onboarding`` misses every crawled
seller and yields NULL, which ``COALESCE(psp_connected, false)`` reads as "not
PSP" — accidentally right, for the wrong reason. **Absence from
``merchant_onboarding`` is layer 1 BY CONSTRUCTION here, never an unknown to be
defaulted.** That is the ``merchant_known`` parameter.
────────────────────────────────────────────────────────────────────────────────

This module is a LABEL, never a GATE. "May this merchant be served at all" is a
different question with a different owner (``AGENT_CHECKOUT_CAPABILITY_GATE`` /
the gateway's ``transactionCapableMerchantWhere``). Fusing a gate and a label is
how ``psp_connected = may be served`` happened. Do not add a gate here.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# ---- the taxonomy -----------------------------------------------------------

LAYER_CRAWLED = 1
LAYER_SYNCED = 2
LAYER_SYNCED_PSP = 3

#: Stable slugs for outward emission. The integer is the founder's numbering; the
#: slug is what a protocol surface should carry (a bare integer on a public feed
#: invites consumers to do arithmetic on it — see F2, there is no ordering to
#: exploit).
CONNECTION_LAYER_SLUGS: Dict[int, str] = {
    LAYER_CRAWLED: "crawled",
    LAYER_SYNCED: "product_synced",
    LAYER_SYNCED_PSP: "product_synced_psp",
}

#: ``catalog_products.catalog_track`` values, which already encode layer 1 vs 2.
TRACK_EXTERNAL_REFERRAL = "external_referral"
TRACK_INTERNAL_MERCHANT = "internal_merchant"

#: ``merchant_stores.status`` values that count as a LIVE store connection.
#: Byte-identical to the repo's canonical predicate in
#: ``services/merchant_store_service`` (``status IN ('active','connected')``) —
#: a narrower set here would make the SQL twin disagree with every other read
#: path about what "connected" means.
LIVE_STORE_STATUSES: tuple = ("active", "connected")

# ---- the orthogonal axis: execution paths -----------------------------------

#: Signed ``/r`` attribution link to the merchant's own destination. The floor —
#: always available when a destination is derivable, never a dead end.
EXECUTION_ATTRIBUTED_REDIRECT = "attributed_redirect"
#: The ``/r`` click is upgraded to a PRE-BUILT cart on the brand's own checkout.
#: Cart-build only; never payment, never completion.
EXECUTION_WARM_HANDOFF = "warm_handoff"
#: The merchant's own native/platform checkout completes the purchase.
EXECUTION_DELEGATED_CHECKOUT = "delegated_checkout"
#: Pivota-orchestrated ACP checkout against the merchant's PSP. Pivota
#: orchestrates and NEVER holds funds (ADR-016).
EXECUTION_PIVOTA_PSP_CHECKOUT = "pivota_psp_checkout"

#: Ordered best-first. ``resolve_execution_paths`` returns a subset in this order,
#: so ``paths[0]`` is the best path actually available.
EXECUTION_PATH_PREFERENCE: tuple = (
    EXECUTION_PIVOTA_PSP_CHECKOUT,
    EXECUTION_WARM_HANDOFF,
    EXECUTION_DELEGATED_CHECKOUT,
    EXECUTION_ATTRIBUTED_REDIRECT,
)

_ENV_TRUE = {"1", "true", "yes", "on"}


def connection_layer_field_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Default-OFF gate on OUTWARD emission of the layer fields.

    The classifier is always importable and always callable — only publishing the
    result on a protocol surface is flagged, because that surface is public and
    externally ingested. Flag off ⇒ byte-identical payloads to today.
    """
    source = env if env is not None else os.environ
    return str(source.get("CONNECTION_LAYER_FIELD_ENABLED", "")).strip().lower() in _ENV_TRUE


def _truthy(value: Any) -> bool:
    """Strict truth. ``None`` (unknown) is NEVER yes — mirrors the identity check
    in ``get_platform_settlement_rails``, where an unverified fact must not light
    a rail."""
    return value is True


def classify_connection_layer(
    *,
    catalog_track: Optional[str] = None,
    merchant_known: bool = False,
    has_active_store: bool = False,
    psp_connected: Optional[bool] = None,
    has_native_payments: Optional[bool] = None,
) -> int:
    """Return the connection layer (1/2/3) for one catalog row / merchant.

    :param catalog_track: ``catalog_products.catalog_track``. ``external_referral``
        is layer 1 outright — a crawled row is layer 1 even if its observed seller
        happens to also have a connected store, because THAT ROW did not come
        from the sync (ADR-001: Pivota owns the canonical record, merchants supply
        offers; the row's provenance is the row's, not the merchant's).
    :param merchant_known: is there a ``merchant_onboarding`` row at all. False ⇒
        layer 1 by construction (F3), never an unknown to be defaulted.
    :param has_active_store: a ``merchant_stores`` row whose status is live —
        ``active`` OR ``connected``, matching the repo's canonical predicate in
        ``services/merchant_store_service`` (``status IN ('active','connected')``).
        Sync without a live store connection is not layer 2 — the sync is stale by
        definition once the store is disconnected. **Callers must check the
        status, not merely that a store dict exists**: ``get_primary_store``'s
        legacy-MCP leg synthesises a store row with ``status='disconnected'`` and
        returns it, so ``bool(store)`` is true for a merchant with no live
        connection at all.
    :param psp_connected: ``merchant_onboarding.psp_connected``. ``None`` = never
        went through the flow, which is not yes.
    :param has_native_payments: the ``pcs_merchant_capabilities.has_shopify_payments``
        fact. ``None`` = never verified, which is not yes.

    Layer 3 admits EITHER a Pivota-side PSP connection OR a verified native
    payments fact, because both mean "a real settlement rail exists on this
    merchant" — which is what the founder's "PSP integrated" denotes. The two
    differ in WHO orchestrates, and that difference lands in
    ``resolve_execution_paths``, not here.
    """
    track = str(catalog_track or "").strip().lower()

    if track == TRACK_EXTERNAL_REFERRAL:
        return LAYER_CRAWLED
    if not merchant_known:
        return LAYER_CRAWLED
    if track != TRACK_INTERNAL_MERCHANT:
        # An unrecognised or ABSENT track is not evidence of a sync — note the
        # unconditional comparison: an earlier cut guarded this with `if track
        # and ...`, which let a NULL/empty track fall through to layer 2/3 while
        # the SQL twin (`lower(COALESCE(...)) <> 'internal_merchant'`) correctly
        # returned 1. That divergence is exactly what the paired tests exist to
        # catch, and it was caught here. Fail to the honest floor.
        return LAYER_CRAWLED
    if not has_active_store:
        return LAYER_CRAWLED
    if _truthy(psp_connected) or _truthy(has_native_payments):
        return LAYER_SYNCED_PSP
    return LAYER_SYNCED


def connection_layer_slug(layer: Any) -> str:
    """Slug for outward emission; ANY unrecognised input falls to the honest floor.

    Never raises — this feeds a protocol payload, and a ``ValueError`` from a
    stray string would take down the surrounding response for a cosmetic field.
    """
    try:
        key = int(layer or 0)
    except (TypeError, ValueError):
        return CONNECTION_LAYER_SLUGS[LAYER_CRAWLED]
    return CONNECTION_LAYER_SLUGS.get(key, CONNECTION_LAYER_SLUGS[LAYER_CRAWLED])


def resolve_execution_paths(
    *,
    layer: int,
    has_destination_url: bool = False,
    warm_handoff_eligible: bool = False,
    native_checkout_available: bool = False,
    pivota_psp_checkout_open: bool = False,
) -> List[str]:
    """The paths a transaction can ACTUALLY take, best-first.

    Deliberately NOT derived from ``layer`` (F2). Every parameter is a live fact:

    :param has_destination_url: we can mint a ``/r`` link to the merchant's own
        page. Without it there is no floor and the list can be empty — an honest
        empty beats advertising a path that dead-ends.
    :param warm_handoff_eligible: the brand is on ``OUTBOUND_WARM_HANDOFF_BRANDS``
        (or in the pct rollout) AND the lane is enabled AND the destination is not
        an affiliate redirector. Caller resolves this; the allowlist is
        brand-keyed, not layer-keyed, which is the whole point of F2.
    :param native_checkout_available: the merchant's own platform checkout can
        complete the purchase (e.g. a verified Shopify Payments fact).
    :param pivota_psp_checkout_open: the ACP checkout doors are actually OPEN
        (``ACP_REST_ENABLED`` + ``ACP_SIGNING_SECRET``) AND this merchant has a
        live charge-ready PSP. **Both**: a live PSP behind dark doors is not a
        path an agent can take, and advertising it would be the exact
        "implies one-click, delivers a 404" failure the no-fallback rule forbids.

    Returns a possibly-empty list. Never raises.
    """
    available = set()
    if has_destination_url:
        available.add(EXECUTION_ATTRIBUTED_REDIRECT)
        if warm_handoff_eligible:
            available.add(EXECUTION_WARM_HANDOFF)
    if native_checkout_available:
        available.add(EXECUTION_DELEGATED_CHECKOUT)
    if pivota_psp_checkout_open:
        available.add(EXECUTION_PIVOTA_PSP_CHECKOUT)
    return [path for path in EXECUTION_PATH_PREFERENCE if path in available]


# ---- the SQL twin -----------------------------------------------------------

#: Aliases the generated SQL uses for its OWN correlated subqueries. A caller
#: alias equal to one of these would SHADOW the inner scope: the store leg would
#: become ``WHERE ms_cl.merchant_id = ms_cl.merchant_id``, decorrelating the
#: subquery into "does ANY active store exist anywhere" and silently returning a
#: wrong layer — no error, no injection, just a wrong label. Rejected by
#: ``_normalize_alias`` so the collision is impossible rather than documented.
_RESERVED_INTERNAL_ALIASES = frozenset({"mo_cl", "mo_psp", "pmc_cl", "ms_cl"})


# Allowed SQL identifier (table alias). Anything else falls back to the default so
# a caller can never inject SQL through the alias parameter. Same guard as the
# gateway's testMerchantPolicy/activeCatalogSourceSql helpers, plus the
# shadowing check above.
def _normalize_alias(alias: Any, fallback: str) -> str:
    text = str(alias or "").strip()
    ok = bool(text) and (text[0].isalpha() or text[0] == "_")
    if ok:
        ok = all(ch.isalnum() or ch == "_" for ch in text)
    if ok and text.lower() in _RESERVED_INTERNAL_ALIASES:
        ok = False
    return text if ok else fallback


def connection_layer_sql(
    product_alias: str = "cp",
    *,
    onboarding_alias: Optional[str] = None,
) -> str:
    """SQL twin of :func:`classify_connection_layer`, as a scalar ``int``
    expression safe to drop into a SELECT list.

    Kept in the same module as the Python twin ON PURPOSE: the two encode one
    policy, and this repo's recurring failure is a derivative drifting from its
    source. ``tests/test_connection_layer_postgres.py`` executes this expression
    inside a serving-shaped statement against real Postgres, because a green
    SQLite suite is not evidence here — twice in two days this repo shipped a
    statement Postgres refused to PREPARE while SQLite passed it.

    :param product_alias: alias holding ``catalog_track`` and ``merchant_id``.
        May not be one of ``_RESERVED_INTERNAL_ALIASES`` (it would shadow this
        expression's own subquery scopes); such a value falls back to ``cp``.
    :param onboarding_alias: alias of an already **LEFT**-joined
        ``merchant_onboarding``. **An INNER JOIN is wrong and silent**: it does
        not mislabel layer-1 rows, it ELIMINATES them — and per the ADR-018
        census that is 100% of the real serving catalog, i.e. an empty feed.
        ``tests/test_connection_layer_postgres.py`` executes that difference so
        the constraint is checkable rather than folkloric.
        When omitted the expression uses correlated ``EXISTS``/scalar subqueries
        so it drops into any query with no join surgery. F3 applies either way:
        NO row in ``merchant_onboarding`` ⇒ layer 1, and the ``NOT EXISTS`` leg
        below states that rather than leaving it to ``COALESCE``.

    Deliberately NOT parameterized: every literal here is a constant of the
    taxonomy, so there is nothing request-supplied to bind. The one caller-supplied
    value (the alias) is identifier-validated above, never interpolated raw.
    """
    p = _normalize_alias(product_alias, "cp")

    if onboarding_alias:
        mo = _normalize_alias(onboarding_alias, "mo")
        merchant_missing = f"{mo}.merchant_id IS NULL"
        psp_fact = f"COALESCE({mo}.psp_connected, false) = true"
    else:
        merchant_missing = (
            "NOT EXISTS (SELECT 1 FROM merchant_onboarding mo_cl "
            f"WHERE mo_cl.merchant_id = {p}.merchant_id)"
        )
        psp_fact = (
            "EXISTS (SELECT 1 FROM merchant_onboarding mo_psp "
            f"WHERE mo_psp.merchant_id = {p}.merchant_id "
            "AND COALESCE(mo_psp.psp_connected, false) = true)"
        )

    # `has_shopify_payments` lives on pcs_merchant_capabilities. Referencing a
    # relation that does not exist errors at PARSE time — before any runtime
    # guard could fire — so this leg is only emitted where the table is part of
    # the schema. It is, in prod and in the dialect gate's create_all.
    native_fact = (
        "EXISTS (SELECT 1 FROM pcs_merchant_capabilities pmc_cl "
        f"WHERE pmc_cl.merchant_id = {p}.merchant_id "
        "AND pmc_cl.has_shopify_payments = true)"
    )

    live_statuses = ", ".join(f"'{status}'" for status in LIVE_STORE_STATUSES)
    active_store = (
        "EXISTS (SELECT 1 FROM merchant_stores ms_cl "
        f"WHERE ms_cl.merchant_id = {p}.merchant_id "
        f"AND lower(btrim(COALESCE(ms_cl.status, ''))) IN ({live_statuses}))"
    )

    # `btrim` is not decoration: the Python twin normalises with `.strip()`, so a
    # whitespace-padded `catalog_track` (' internal_merchant ') would classify as
    # layer 2 in Python and layer 1 in SQL without it. That divergence was
    # executed against real Postgres before this line existed.
    track = f"lower(btrim(COALESCE({p}.catalog_track, '')))"

    return f"""
    (CASE
      WHEN {track} = '{TRACK_EXTERNAL_REFERRAL}' THEN {LAYER_CRAWLED}
      WHEN {merchant_missing} THEN {LAYER_CRAWLED}
      WHEN {track} <> '{TRACK_INTERNAL_MERCHANT}' THEN {LAYER_CRAWLED}
      WHEN NOT {active_store} THEN {LAYER_CRAWLED}
      WHEN {psp_fact} OR {native_fact} THEN {LAYER_SYNCED_PSP}
      ELSE {LAYER_SYNCED}
    END)
    """.strip()


__all__ = [
    "LAYER_CRAWLED",
    "LAYER_SYNCED",
    "LAYER_SYNCED_PSP",
    "CONNECTION_LAYER_SLUGS",
    "TRACK_EXTERNAL_REFERRAL",
    "TRACK_INTERNAL_MERCHANT",
    "LIVE_STORE_STATUSES",
    "EXECUTION_ATTRIBUTED_REDIRECT",
    "EXECUTION_WARM_HANDOFF",
    "EXECUTION_DELEGATED_CHECKOUT",
    "EXECUTION_PIVOTA_PSP_CHECKOUT",
    "EXECUTION_PATH_PREFERENCE",
    "connection_layer_field_enabled",
    "classify_connection_layer",
    "connection_layer_slug",
    "resolve_execution_paths",
    "connection_layer_sql",
]
