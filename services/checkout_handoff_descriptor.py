from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


HANDOFF_KIND = "pivota_agent_checkout_handoff"
HANDOFF_LABEL = "Open buyable Pivota product page"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _clean_string(value: Any) -> Optional[str]:
    token = str(value or "").strip()
    return token or None


def _drop_empty(value: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != [] and item != {}
    }


def _http_url(value: Any) -> Optional[str]:
    url = _clean_string(value)
    if not url:
        return None
    lowered = url.lower()
    if lowered.startswith("https://") or lowered.startswith("http://"):
        return url
    return None


def _first_string(*values: Any) -> Optional[str]:
    for value in values:
        token = _clean_string(value)
        if token:
            return token
    return None


def build_checkout_handoff_descriptor(
    *,
    sku_ctx: Mapping[str, Any],
    deliverability: Mapping[str, Any],
    audit_run_id: Optional[str] = None,
    handoff_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build an advisory Agent checkout handoff descriptor for a SKU.

    The descriptor is intentionally not spend authority. It carries identity and
    policy facts from the audit so a later Agent resolver can revalidate them
    before quote/order/pay. Non-transactable SKUs return no descriptor.
    """
    if not isinstance(sku_ctx, Mapping) or not isinstance(deliverability, Mapping):
        return None
    source_status = _clean_string(deliverability.get("status")) or "not_measured"
    if source_status != "transactable":
        return None

    product = _as_mapping(sku_ctx.get("product"))
    sku = _as_mapping(sku_ctx.get("sku"))
    checkout = _as_mapping(deliverability.get("checkout"))
    offer = _as_mapping(checkout.get("offer"))

    merchant_id = _first_string(
        sku_ctx.get("merchant_id"),
        product.get("merchant_id"),
        offer.get("merchant_id"),
    )
    product_key = _first_string(
        sku_ctx.get("product_key"),
        product.get("product_key"),
        offer.get("product_key"),
    )
    sku_key = _first_string(
        sku_ctx.get("sku_key"),
        sku.get("sku_key"),
        offer.get("sku_key"),
    )
    offer_id = _clean_string(offer.get("offer_id"))
    pivota_signature_id = _first_string(
        product.get("pivota_signature_id"),
        sku_ctx.get("pivota_signature_id"),
    )
    commerce_path = _clean_string(checkout.get("commerce_path"))
    validation_authority = _clean_string(checkout.get("validation_authority"))
    execution_policy_version = _clean_string(checkout.get("execution_policy_version"))

    missing_inputs = []
    if not merchant_id:
        missing_inputs.append("merchant_id")
    if not product_key:
        missing_inputs.append("product_key")
    if not sku_key:
        missing_inputs.append("sku_key")
    if not offer_id:
        missing_inputs.append("checkout.offer.offer_id")
    if not pivota_signature_id:
        missing_inputs.append("catalog_products.pivota_signature_id")
    if checkout.get("allows_pivota_order") is not True:
        missing_inputs.append("checkout.allows_pivota_order")
    if checkout.get("allows_psp_creation") is not True:
        missing_inputs.append("checkout.allows_psp_creation")
    if commerce_path != "pivota_direct_quote_first":
        missing_inputs.append("checkout.commerce_path")

    reason_codes = []
    if missing_inputs:
        reason_codes.append("handoff_identity_or_policy_incomplete")

    descriptor_status = "eligible" if not missing_inputs else "not_linkable"
    safe_handoff_url = _http_url(handoff_url) if descriptor_status == "eligible" else None

    return _drop_empty(
        {
            "status": descriptor_status,
            "kind": HANDOFF_KIND,
            "label": HANDOFF_LABEL,
            "merchant_id": merchant_id,
            "product_key": product_key,
            "sku_key": sku_key,
            "offer_id": offer_id,
            "pivota_signature_id": pivota_signature_id,
            "commerce_path": commerce_path,
            "validation_authority": validation_authority,
            "execution_policy_version": execution_policy_version,
            "source_audit_run_id": _clean_string(audit_run_id),
            "source_deliverability_status": source_status,
            "handoff_url": safe_handoff_url,
            "missing_inputs": missing_inputs,
            "reason_codes": reason_codes,
        }
    )
