"""P1 — brand attestation: a CLAIMED brand submits its own product content.

The content lands in **product_enrichment** — the AGENT-READ overlay the
agent_pdp_view assembler merges into the served PDP (its E2 publish bridge) — so
agents actually discover the brand's copy. (It deliberately does NOT use
catalog_field_facts, which serving never reads — that would be the publish-gap
trap.) claim_state then advances claimed -> attested and the served PDP is
refreshed.

GATE: attestation is allowed only when the SKU is already CLAIMED
(may_publish_brand_attested) — brand-authored copy enters the served overlay
only after claim.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services.claim_state import (
    advance_product_to_attested,
    may_publish_brand_attested,
)

logger = logging.getLogger(__name__)

# Only the fields the agent_pdp_view assembler actually SERVES: the E2 publish
# bridge maps these onto the agent-read view (title_override -> title,
# description_markdown -> description, plus the bullet_points / usage_scenarios
# rich-content columns). The remaining enrichment fields (summary/audience/topic/
# disclaimer) are stored but NOT yet read into agent_pdp_view, so accepting them
# here would promise serving we can't deliver — added back as the view carries them.
_ATTEST_FIELD_MAP = {
    "title": "title_override",
    "description": "description_markdown",
    "bullet_points": "bullet_points",
    "usage_scenarios": "usage_scenarios",
}


def attestation_to_enrichment(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: map brand-submitted attestation fields -> product_enrichment served
    columns (only known fields; empty values dropped). Stamps provenance so the
    served overlay records that this copy is brand-attested."""
    out: Dict[str, Any] = {}
    for key, value in (fields or {}).items():
        col = _ATTEST_FIELD_MAP.get(key)
        if col and value not in (None, "", [], {}):
            out[col] = value
    if out:
        out["updated_by_employee_id"] = "brand_attestation"
    return out


async def attest_product(
    *,
    merchant_id: str,
    product_key: str,
    fields: Dict[str, Any],
    lab_evidence_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate the SKU is the caller's AND already claimed, write the attested
    content to the served overlay (product_enrichment), advance claim_state ->
    attested, and refresh the served PDP so agents see it. Returns {status, ...}.

    lab_evidence_ref is accepted + reported now; durable lab-evidence storage +
    substantiation grading (-> 'substantiated') is the next slice.
    """
    if not merchant_id or not product_key:
        return {"status": "bad_request"}

    enrichment = attestation_to_enrichment(fields)
    if not enrichment:
        return {"status": "no_attestable_fields"}

    from db.catalog import catalog_products
    from db.database import database

    row = await database.fetch_one(
        catalog_products.select().where(catalog_products.c.product_key == product_key)
    )
    if not row or row["merchant_id"] != merchant_id:
        # cross-tenant / missing -> don't leak existence
        return {"status": "not_found"}
    # GATE: brand-authored copy may only be attested once the SKU is claimed.
    if not may_publish_brand_attested(row["claim_state"]):
        return {"status": "not_claimed", "claim_state": row["claim_state"]}

    # 1. Write the SERVED overlay (agent-discoverable via the assembler E2 bridge).
    try:
        from db.product_enrichment import upsert_enrichment

        await upsert_enrichment(
            row["merchant_id"],
            row["platform"],
            row["source_product_id"],
            "default",
            enrichment,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "attest_product: enrichment upsert failed for %s: %s",
            product_key, str(exc)[:200],
        )
        return {"status": "error"}

    # 2. Advance the lifecycle + refresh the served PDP so agents see the new copy.
    await advance_product_to_attested(merchant_id=merchant_id, product_key=product_key)
    content_key = row["content_key"]
    if content_key:
        try:
            from services.agent_pdp_view_assembler import (
                refresh_agent_pdp_view_for_content_key,
            )

            await refresh_agent_pdp_view_for_content_key(
                content_key, refresh_source="brand_attestation"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "attest_product: pdp refresh failed for %s: %s",
                content_key, str(exc)[:200],
            )

    return {
        "status": "attested",
        "content_key": content_key,
        "fields_written": sorted(
            k for k in enrichment if k != "updated_by_employee_id"
        ),
        "lab_evidence_recorded": bool(lab_evidence_ref),
    }
