"""Capabilities for external systems that participate in Commerce Index.

Product data and payment data have deliberately different contracts.  A PSP may
be a valid checkout and webhook integration while providing no merchant-authorized
catalogue feed at all.  Keeping that distinction here prevents a "successful"
empty catalogue sync from masking an unsupported provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CommerceSourceCapabilities:
    catalog_pull: bool = False
    catalog_events: bool = False
    reviews_pull: bool = False
    live_quote: bool = False
    checkout: bool = False
    payment_webhooks: bool = False


@dataclass(frozen=True)
class CommerceSourceDefinition:
    provider: str
    source_kind: str
    capabilities: CommerceSourceCapabilities
    integration_layer: str = "catalog"
    catalog_sync_guidance: Optional[str] = None


_CATALOG_CAPABILITIES = CommerceSourceCapabilities(
    catalog_pull=True,
    catalog_events=True,
    live_quote=True,
    checkout=True,
)

_PAYMENT_ONLY_GUIDANCE = (
    "{provider} is configured as a payment-orchestration source, not a product "
    "catalogue source. Connect a merchant-authorized storefront, PIM/ERP/POS, "
    "or contracted catalogue feed before running Commerce Index product sync."
)

_SOURCES: Dict[str, CommerceSourceDefinition] = {
    # Universal public-web evidence source. It is intentionally neither a
    # storefront adapter nor a payment capability: it may cover any public
    # domain, but can only emit review-required evidence.
    "public_web": CommerceSourceDefinition(
        "public_web", "public_crawl", CommerceSourceCapabilities(), "evidence",
        "Public web is evidence-only: it cannot publish stock, price, checkout, or payment authority.",
    ),
    "shopify": CommerceSourceDefinition("shopify", "storefront", _CATALOG_CAPABILITIES),
    "wix": CommerceSourceDefinition("wix", "storefront", _CATALOG_CAPABILITIES),
    "woocommerce": CommerceSourceDefinition("woocommerce", "storefront", _CATALOG_CAPABILITIES),
    "bigcommerce": CommerceSourceDefinition("bigcommerce", "storefront", _CATALOG_CAPABILITIES),
    "cafe24": CommerceSourceDefinition("cafe24", "storefront", _CATALOG_CAPABILITIES),
    # Phase 1 is an authenticated native catalog pull. Commerce telemetry is
    # already coverable by the universal collectors, but no Magento-native
    # catalog-event, quote, or checkout connector is claimed yet.
    "magento": CommerceSourceDefinition(
        "magento",
        "storefront",
        CommerceSourceCapabilities(catalog_pull=True),
    ),
    # SCAPI Shopper Search/Products is a native, site-scoped sellable catalog
    # source. Checkout remains unclaimed. Commerce lifecycle telemetry is a
    # separate signed cartridge/outbox contract; this registry has no commerce-
    # telemetry flag, and catalog_events intentionally remains false.
    "salesforce_commerce_cloud": CommerceSourceDefinition(
        "salesforce_commerce_cloud",
        "storefront",
        CommerceSourceCapabilities(catalog_pull=True),
    ),
    "shopline": CommerceSourceDefinition(
        "shopline", "storefront", CommerceSourceCapabilities(catalog_pull=True)
    ),
    "shoplazza": CommerceSourceDefinition(
        "shoplazza", "storefront", CommerceSourceCapabilities(catalog_pull=True)
    ),
    # Squarespace ships COMMERCE TELEMETRY only: a signed webhook receiver and
    # an Orders-API reconciliation sweep that feed the canonical ledger. No
    # catalogue capability is claimed — nothing in this repo reads Squarespace's
    # Products API, so `catalog_pull` here would make an empty product sync
    # report success. Telemetry is not a flag this registry models (the SFCC
    # cartridge is the same shape), so every capability stays false and the
    # guidance says why.
    "squarespace": CommerceSourceDefinition(
        "squarespace",
        "storefront",
        CommerceSourceCapabilities(),
        "catalog",
        "Squarespace is connected for commerce telemetry only (order, refund, "
        "and cancellation events). No Squarespace catalogue adapter exists yet; "
        "connect a merchant-authorized catalogue feed before running Commerce "
        "Index product sync.",
    ),
    # Webflow ships COMMERCE TELEMETRY only: a webhook receiver and an
    # Orders-list reconciliation sweep that feed the canonical ledger. No
    # catalogue capability is claimed — nothing in this repo reads Webflow's CMS
    # or Products API, so `catalog_pull` here would make an empty product sync
    # report success instead of an honest blocker. Telemetry is not a flag this
    # registry models (Squarespace and the SFCC cartridge are the same shape),
    # so every capability stays false and the guidance says why.
    "webflow": CommerceSourceDefinition(
        "webflow",
        "storefront",
        CommerceSourceCapabilities(),
        "catalog",
        "Webflow is connected for commerce telemetry only (order, payment, and "
        "refund events). No Webflow catalogue adapter exists yet; connect a "
        "merchant-authorized catalogue feed before running Commerce Index "
        "product sync.",
    ),
    # Square is retained because the existing sync endpoint accepts its credentials;
    # its fetch adapter can be enabled independently of this policy contract.
    "square": CommerceSourceDefinition("square", "storefront", _CATALOG_CAPABILITIES),
    "stripe": CommerceSourceDefinition(
        "stripe",
        "payment_orchestration",
        CommerceSourceCapabilities(payment_webhooks=True),
        "payment",
        _PAYMENT_ONLY_GUIDANCE.format(provider="Stripe"),
    ),
    "adyen": CommerceSourceDefinition(
        "adyen",
        "payment_orchestration",
        CommerceSourceCapabilities(payment_webhooks=True),
        "payment",
        _PAYMENT_ONLY_GUIDANCE.format(provider="Adyen"),
    ),
    # Antom catalogue and UCP payment are deliberately modelled as independent
    # integration identities.  The catalogue source is reserved for the
    # merchant-authorized product/offer feed; it is not enabled by connecting a
    # UCP payment account and will gain its own feed adapter and schedule.
    "antom_catalog": CommerceSourceDefinition(
        "antom_catalog",
        "catalog_feed",
        CommerceSourceCapabilities(),
        "catalog",
        "Antom Catalog is a separate merchant-authorized feed integration. "
        "Its contracted feed schema and credentials must be connected before "
        "Commerce Index can ingest products, offers, stock, images, or reviews.",
    ),
    "antom_ucp": CommerceSourceDefinition(
        "antom_ucp",
        "payment_orchestration",
        CommerceSourceCapabilities(payment_webhooks=True),
        "payment",
        _PAYMENT_ONLY_GUIDANCE.format(provider="Antom"),
    ),
}


def normalize_commerce_provider(provider: Optional[str]) -> str:
    normalized = str(provider or "").strip().lower().replace("-", "_")
    # Preserve backwards compatibility for existing PSP configuration rows:
    # unqualified "antom" means the payment/UCP connector, never a catalogue.
    return "antom_ucp" if normalized == "antom" else normalized


def get_commerce_source(provider: Optional[str]) -> Optional[CommerceSourceDefinition]:
    return _SOURCES.get(normalize_commerce_provider(provider))


def catalog_sync_blocker(provider: Optional[str]) -> Optional[str]:
    """Return an operator-safe explanation when a provider cannot supply products."""
    normalized = normalize_commerce_provider(provider)
    definition = get_commerce_source(normalized)
    if definition is None:
        return f"{normalized or 'Unknown provider'} is not a supported Commerce Index catalogue source."
    if definition.capabilities.catalog_pull:
        return None
    return definition.catalog_sync_guidance or (
        f"{definition.provider.title()} does not expose a merchant-authorized catalogue feed."
    )
