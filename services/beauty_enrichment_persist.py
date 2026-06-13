"""Persist deterministic beauty enrichment into the beauty_* tables.

Wraps services.beauty_enrichment (the pure derivation) with ownership-safe,
idempotent writes + a serving-eligibility recompute, so an onboarding job (or a
backfill) can turn a shell SKU into a decision-grade record.

Write policy: FILL-ONLY-WHEN-EMPTY. Auto-enrichment never overwrites a field
that already has a value -- not merchant-authored data, not a previous
enrichment. So it is safe to run on every sync and safe to re-run (idempotent),
and it can never clobber a human's authoring. active_ingredients carry their own
provenance (source="inci"|"text"); concerns are text-derived.

Concretely, per product:
  * concerns  -> beauty_product_profiles.concerns_json, only if currently empty.
  * actives   -> beauty_sku_ingredients.active_ingredients_json, per SKU, only on
                 rows whose active list is currently empty AND that are not
                 merchant-owned (source_system merchant_payload/merchant_authored).
Then recompute_serving_eligibility(content_key, reason="beauty_enrichment").
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from db.database import database
from services.beauty_enrichment import enrich_beauty_record
from services.beauty_field_authoring import (
    SOURCE_MERCHANT_AUTHORED,
    SOURCE_MERCHANT_PAYLOAD,
)
from services.claim_safety import REVIEW_OBSERVED, SUBSTANTIATION_SUBSTANTIATED
from services.index_pipeline_state_service import recompute_serving_eligibility

# Marks an active list written by deterministic enrichment (vs merchant-owned).
SOURCE_AUTO_ENRICHMENT = "auto_enrichment_v1"

_MERCHANT_OWNED = {SOURCE_MERCHANT_PAYLOAD, SOURCE_MERCHANT_AUTHORED}


def _inci_substantiated_claims(actives: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Provenance-backed ingredient-presence claims from INCI-VERIFIED actives.

    Claim-safety (strictest): ONLY `source="inci"` actives become `substantiated`
    -- the INCI ingredient list is the authoritative source for ingredient
    *identity* (it does NOT substantiate efficacy; "contains retinol" is identity,
    "reduces wrinkles" is a drug/efficacy claim handled elsewhere). A text-derived
    active is ingredient-PRESENT, not verified, so it never earns `substantiated`
    here -- that distinction is what keeps marketing copy from masquerading as
    evidence. This is the `justify` dimension's provenance-backed-claim signal.
    """
    claims: List[Dict[str, Any]] = []
    seen: set = set()
    for active in actives or []:
        if not isinstance(active, dict) or active.get("source") != "inci":
            continue
        label = str(active.get("label") or "").strip()
        key = label.lower()
        if not label or key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "claim_text": f"Contains {label}",
                "source_ref": "INCI",
                "source_type": "inci",
                "evidence_grade": "ingredient_list",
                "substantiation_status": SUBSTANTIATION_SUBSTANTIATED,
            }
        )
    return claims


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _evidence_has_claims(value: Any) -> bool:
    """True when an evidence_profile already carries ≥1 claim (so auto-enrichment
    must not clobber it -- same fill-only-when-empty rule as concerns/actives)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return False
    if isinstance(value, dict):
        claims = value.get("claims")
        return isinstance(claims, list) and len(claims) > 0
    return False


async def enrich_and_persist_product(
    product_key: str,
    *,
    db: Any = database,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Derive + fill-empty-persist the beauty structure for one product.

    Returns a summary: what was derived and what was actually written (or would
    be, under dry_run). Never raises on a missing product -- returns
    status="not_found". Idempotent and ownership-safe.
    """
    cp = await db.fetch_one(
        """
        SELECT content_key, title, description, product_type, category_path, category_kind
        FROM catalog_products
        WHERE product_key = :pk
        LIMIT 1
        """,
        {"pk": product_key},
    )
    if cp is None:
        return {"product_key": product_key, "status": "not_found"}
    cp = dict(cp)

    sku_rows = [
        dict(r)
        for r in await db.fetch_all(
            """
            SELECT sku_key, raw_inci, active_ingredients_json, concentration_notes_json, source_system
            FROM beauty_sku_ingredients
            WHERE product_key = :pk
            """,
            {"pk": product_key},
        )
    ]
    profile = await db.fetch_one(
        "SELECT concerns_json, evidence_profile FROM beauty_product_profiles WHERE product_key = :pk LIMIT 1",
        {"pk": product_key},
    )
    profile_dict = dict(profile) if profile else {}
    stored_concerns = _as_list(profile_dict.get("concerns_json"))
    stored_evidence = profile_dict.get("evidence_profile")

    # Use a representative INCI / concentration from any SKU that carries it.
    raw_inci = next((r.get("raw_inci") for r in sku_rows if r.get("raw_inci")), None)
    concentration = next(
        (r.get("concentration_notes_json") for r in sku_rows if r.get("concentration_notes_json")),
        None,
    )

    enriched = enrich_beauty_record(
        cp.get("category_kind"),
        title=cp.get("title"),
        description=cp.get("description"),
        raw_inci=raw_inci,
        concentration_notes=_as_list(concentration) or None,
        product_type=cp.get("product_type"),
        category_path=cp.get("category_path"),
    )
    derived_actives = enriched.get("active_ingredients") or []
    derived_concerns = enriched.get("concerns") or []

    wrote_concerns = False
    actives_written_skus: List[str] = []

    # concerns: fill only when the profile carries none.
    if derived_concerns and not stored_concerns and not dry_run:
        await db.execute(
            """
            INSERT INTO beauty_product_profiles (product_key, concerns_json, updated_at)
            VALUES (:pk, CAST(:concerns AS jsonb), NOW())
            ON CONFLICT (product_key) DO UPDATE SET
              concerns_json = EXCLUDED.concerns_json,
              updated_at = NOW()
            WHERE beauty_product_profiles.concerns_json IS NULL
               OR jsonb_typeof(beauty_product_profiles.concerns_json) <> 'array'
               OR jsonb_array_length(beauty_product_profiles.concerns_json) = 0
            """,
            {"pk": product_key, "concerns": json.dumps(derived_concerns)},
        )
        wrote_concerns = True
    would_write_concerns = bool(derived_concerns and not stored_concerns)

    # actives: per SKU, fill only empty + non-merchant-owned rows.
    if derived_actives:
        for row in sku_rows:
            if _as_list(row.get("active_ingredients_json")):
                continue  # already populated -> never overwrite
            if row.get("source_system") in _MERCHANT_OWNED:
                continue  # merchant owns this row -> hands off
            if not dry_run:
                await db.execute(
                    """
                    UPDATE beauty_sku_ingredients
                    SET active_ingredients_json = CAST(:actives AS jsonb),
                        updated_at = NOW()
                    WHERE sku_key = :sk
                      AND (active_ingredients_json IS NULL
                           OR jsonb_typeof(active_ingredients_json) <> 'array'
                           OR jsonb_array_length(active_ingredients_json) = 0)
                    """,
                    {"sk": row["sku_key"], "actives": json.dumps(derived_actives)},
                )
            actives_written_skus.append(row["sku_key"])

    # evidence: substantiated ingredient-presence claims from INCI-verified
    # actives -> beauty_product_profiles.evidence_profile, fill-only-when-empty.
    # This is the `justify` dimension's provenance-backed-claim signal. Empty when
    # there is no INCI (text-derived actives never earn `substantiated`).
    inci_claims = _inci_substantiated_claims(derived_actives)
    wrote_evidence = False
    would_write_evidence = bool(inci_claims and not _evidence_has_claims(stored_evidence))
    if would_write_evidence and not dry_run:
        evidence_payload = {"claims": inci_claims, "review_state": REVIEW_OBSERVED}
        await db.execute(
            """
            INSERT INTO beauty_product_profiles (product_key, evidence_profile, updated_at)
            VALUES (:pk, CAST(:evidence AS jsonb), NOW())
            ON CONFLICT (product_key) DO UPDATE SET
              evidence_profile = EXCLUDED.evidence_profile,
              updated_at = NOW()
            WHERE beauty_product_profiles.evidence_profile IS NULL
               OR jsonb_typeof(beauty_product_profiles.evidence_profile -> 'claims') IS DISTINCT FROM 'array'
               OR jsonb_array_length(beauty_product_profiles.evidence_profile -> 'claims') = 0
            """,
            {"pk": product_key, "evidence": json.dumps(evidence_payload)},
        )
        wrote_evidence = True

    recomputed: Optional[bool] = None
    if (wrote_concerns or actives_written_skus or wrote_evidence) and not dry_run:
        recomputed = await recompute_serving_eligibility(
            cp["content_key"], reason="beauty_enrichment"
        )

    return {
        "product_key": product_key,
        "content_key": cp.get("content_key"),
        "category_kind": cp.get("category_kind"),
        "status": "ok",
        "dry_run": dry_run,
        "derived": {
            "active_ingredients": [a.get("label") for a in derived_actives],
            "active_source": enriched.get("provenance", {}).get("active_ingredients"),
            "concerns": derived_concerns,
            "substantiated_claims": [c["claim_text"] for c in inci_claims],
        },
        "written": {
            "concerns": (would_write_concerns if dry_run else wrote_concerns),
            "actives_skus": (
                [r["sku_key"] for r in sku_rows
                 if not _as_list(r.get("active_ingredients_json"))
                 and r.get("source_system") not in _MERCHANT_OWNED]
                if dry_run and derived_actives
                else actives_written_skus
            ),
            "evidence_claims": (would_write_evidence if dry_run else wrote_evidence),
        },
        "serving_eligible": recomputed,
    }
