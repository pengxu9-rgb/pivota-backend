"""Trust provenance vocabulary for the canonical commerce ledger.

Every row in ``commerce_interaction_events`` carries four columns stamped by
the ingress that authenticated the caller: ``write_path`` (the concrete
ingress), ``authority`` (whose statement of fact the row is), the
``agent_identity_confidence`` that ingress may assert, and ``synthetic``.
None of them is read from an event body. This module is the single table
that pairs a write path with its authority and permitted confidence; it has
no service imports so both the batch ingest and the direct ledger writers can
depend on it without a cycle.
"""

from __future__ import annotations

from typing import Dict, Literal

# The concrete ingress that authenticated a write. Every production caller
# names its own path as a literal; the ledger stores it verbatim so a reader
# can tell a PSP fact from a merchant-asserted one without parsing the
# caller-supplied `source` string.
WritePath = Literal[
    # Batch ingress (routes/merchant_events.py and the native adapters)
    "universal_web_collector",
    "shopify_web_pixel",
    "merchant_hmac_batch",
    "shopify_webhook",
    "cafe24_webhook",
    "cafe24_reconciliation",
    "woocommerce_webhook",
    "bigcommerce_webhook",
    "wix_webhook",
    "shopline_webhook",
    "shoplazza_webhook",
    # Squarespace is observed through TWO ingresses that report the same
    # platform facts: the signed webhook (OAuth-connected sites only) and the
    # Orders-API reconciliation sweep (every site, and the ONLY path for
    # API-key sites). Both carry the same `platform` authority; the pair is
    # what services.commerce_interaction_service.recorded_refund_amount_cents
    # reads across so a cumulative refund total is never counted twice.
    "squarespace_webhook",
    "squarespace_reconciliation",
    # Webflow is observed through the same two ingresses: a webhook receiver
    # (authenticated by a per-store URL secret, and additionally by Webflow's
    # signature when the deployment runs an OAuth app) and an Orders-list
    # reconciliation sweep. Both carry authority `platform`. They must: Webflow
    # webhooks are best-effort, so the sweep is the recovery path for every
    # store and the ONLY path for one whose webhooks are not provisioned yet,
    # and filing its orders below an identical provisioned store's would make a
    # merchant's standing depend on whether an ensure had been run.
    "webflow_webhook",
    "webflow_reconciliation",
    "sfcc_cartridge",
    # PrestaShop has no outbound webhooks; the signed sender is the module
    # Pivota ships (integrations/prestashop-module/).
    "prestashop_module",
    "adobe_io_events",
    "stripe_webhook",
    # First-party writers that call record_commerce_event directly
    "agent_commerce_api",
    "surface_click_attribution",
    "commerce_attribution_edge",
    "surface_listing_registry",
]

# Whose statement of fact an event is. Derived from the write path on the
# server; the event body cannot raise it. `observational` rows may describe a
# funnel but never money movement; `psp` is the settlement authority and wins
# a refund de-duplication against `platform` and `merchant` reports; `pivota`
# is a first-party server fact about Pivota's own checkout, attribution, or
# listing pipeline.
LedgerAuthority = Literal["observational", "merchant", "platform", "psp", "pivota"]

LEDGER_AUTHORITY_BY_WRITE_PATH: Dict[str, str] = {
    "universal_web_collector": "observational",
    "shopify_web_pixel": "observational",
    "merchant_hmac_batch": "merchant",
    "shopify_webhook": "platform",
    "cafe24_webhook": "platform",
    "cafe24_reconciliation": "platform",
    "woocommerce_webhook": "platform",
    "bigcommerce_webhook": "platform",
    "wix_webhook": "platform",
    "shopline_webhook": "platform",
    "shoplazza_webhook": "platform",
    "squarespace_webhook": "platform",
    "squarespace_reconciliation": "platform",
    "webflow_webhook": "platform",
    "webflow_reconciliation": "platform",
    "sfcc_cartridge": "platform",
    "prestashop_module": "platform",
    "adobe_io_events": "platform",
    "stripe_webhook": "psp",
    "agent_commerce_api": "pivota",
    "surface_click_attribution": "pivota",
    "commerce_attribution_edge": "pivota",
    "surface_listing_registry": "pivota",
}

# A write path and the confidence it may assert are one contract, fixed here
# so a future route cannot pair a browser token with a platform-grade claim.
#
# `verified` is issued by exactly one path. routes/agent_commerce.py runs
# behind get_agent_context, and every branch of that dependency authenticates
# the agent's OWN Pivota credential before yielding context.agent_id: an
# `ak_…` API key looked up in the agents table, a Pivota-signed checkout token
# whose agent_id is then looked up, or an internal trusted key; the test-key
# shortcut fails closed on every deployed host. That is what "the ingress
# authenticated that agent itself" means in commerce_interaction_service.
# It is NOT a statement about the agent vendor's identity beyond the
# credential Pivota issued to it.
#
# The click, attribution-edge, and listing writers carry whatever agent id
# their attribution context happened to hold; nothing at the writer
# authenticates it, so they may only assert `unknown`.
_ALLOWED_CONFIDENCE_BY_WRITE_PATH: Dict[str, frozenset[str]] = {
    "universal_web_collector": frozenset({"browser_observed"}),
    "shopify_web_pixel": frozenset({"browser_observed"}),
    "merchant_hmac_batch": frozenset({"merchant_asserted"}),
    "shopify_webhook": frozenset({"platform_asserted"}),
    "cafe24_webhook": frozenset({"platform_asserted"}),
    "cafe24_reconciliation": frozenset({"platform_asserted"}),
    "woocommerce_webhook": frozenset({"platform_asserted"}),
    "bigcommerce_webhook": frozenset({"platform_asserted"}),
    "wix_webhook": frozenset({"platform_asserted"}),
    "shopline_webhook": frozenset({"platform_asserted"}),
    "shoplazza_webhook": frozenset({"platform_asserted"}),
    "squarespace_webhook": frozenset({"platform_asserted"}),
    "squarespace_reconciliation": frozenset({"platform_asserted"}),
    "webflow_webhook": frozenset({"platform_asserted"}),
    "webflow_reconciliation": frozenset({"platform_asserted"}),
    "sfcc_cartridge": frozenset({"platform_asserted"}),
    "prestashop_module": frozenset({"platform_asserted"}),
    "adobe_io_events": frozenset({"platform_asserted"}),
    "stripe_webhook": frozenset({"platform_asserted"}),
    "agent_commerce_api": frozenset({"verified"}),
    "surface_click_attribution": frozenset({"unknown"}),
    "commerce_attribution_edge": frozenset({"unknown"}),
    "surface_listing_registry": frozenset({"unknown"}),
}

# The surface ops probes have always written. Rows carrying it are synthetic
# even when the batch did not say so, which keeps the pre-flag canary honest.
OPS_CANARY_SURFACE = "ops_canary"


def resolve_ledger_authority(write_path: str, agent_identity_confidence: str) -> str:
    """Return the authority a write path carries, refusing a mismatched claim."""
    try:
        authority = LEDGER_AUTHORITY_BY_WRITE_PATH[write_path]
    except KeyError as exc:
        raise ValueError(f"unknown ledger write_path: {write_path}") from exc
    if agent_identity_confidence not in _ALLOWED_CONFIDENCE_BY_WRITE_PATH[write_path]:
        raise ValueError(
            f"write_path {write_path} cannot assert "
            f"agent_identity_confidence={agent_identity_confidence}"
        )
    return authority


def ledger_provenance(write_path: str, agent_identity_confidence: str) -> Dict[str, str]:
    """The three stamp kwargs for a direct ``record_commerce_event`` call.

    Callers splat this so the pairing is checked at the writer and the
    authority is never typed by hand::

        await record_commerce_event(
            event_type=..., metadata=..., source=...,
            **ledger_provenance("agent_commerce_api", "verified"),
        )
    """
    return {
        "write_path": write_path,
        "authority": resolve_ledger_authority(write_path, agent_identity_confidence),
        "agent_identity_confidence": agent_identity_confidence,
    }
