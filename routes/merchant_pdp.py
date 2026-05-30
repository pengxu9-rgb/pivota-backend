from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import httpx
from pydantic import BaseModel, Field


SKU_OPT_OVERLAY_V1_ENABLED = os.getenv("SKU_OPT_OVERLAY_V1", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Modules a merchant may self-approve via the LLM-reviewed auto-publish path.
# v1: 'copy' only (low-risk, machine-publishable). Widen deliberately.
MERCHANT_SELF_APPROVE_MODULES = {"copy"}
SOURCE_GROUNDING_TIMEOUT_S = 5.0

from db.database import database
from services.pdp_governance_service import (
    DEFAULT_MARKET,
    REVIEW_ACTOR_GPT55,
    create_merchant_contribution,
    ensure_pdp_governance_tables,
    get_pdp_projection,
    parse_product_key,
    review_module_version,
)
from services.pdp_copy_review import generate_copy_review_rubric
from utils.auth import get_current_user


router = APIRouter(prefix="/merchant/pdps", tags=["merchant-pdp-governance"])


class MerchantContributionRequest(BaseModel):
    module_key: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = None
    market: str = DEFAULT_MARKET


def _merchant_id(current_user: Dict[str, Any]) -> str:
    merchant_id = current_user.get("merchant_id")
    if not merchant_id:
        raise HTTPException(status_code=403, detail="MERCHANT_REQUIRED")
    return str(merchant_id)


def _map_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if message in {"PDP_NOT_FOUND", "PDP_MODULE_VERSION_NOT_FOUND"}:
        return HTTPException(status_code=404, detail=message)
    if message in {"INVALID_PRODUCT_KEY", "INVALID_PDP_MODULE", "PDP_RESOLUTION_REQUIRES_PRODUCT_KEY_OR_SEED"}:
        return HTTPException(status_code=400, detail=message)
    if message == "MERCHANT_PRODUCT_FORBIDDEN":
        return HTTPException(status_code=403, detail=message)
    return HTTPException(status_code=500, detail=message[:300])


def _source_grounding_enabled() -> bool:
    return os.getenv("OVERLAY_DEEPSEEK_GROUND_AGAINST_SOURCE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return " ".join(self._parts)


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore

        text = BeautifulSoup(html or "", "html.parser").get_text(" ")
    except Exception:
        parser = _TextExtractor()
        parser.feed(html or "")
        text = parser.text()
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_source_text(source_url: Optional[str]) -> Optional[str]:
    if not source_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=SOURCE_GROUNDING_TIMEOUT_S) as client:
            response = await client.get(source_url)
        response.raise_for_status()
        text = _html_to_text(response.text)
        return text[:2000] if text else None
    except Exception:
        return None


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _source_url_from_refs(source_refs: Any) -> Optional[str]:
    refs = source_refs if isinstance(source_refs, list) else []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        url = _first_text(
            ref.get("source_url"),
            ref.get("source_original_url"),
            ref.get("url"),
            ref.get("canonical_url"),
            ref.get("destination_url"),
        )
        if url:
            return url
    return None


def _resolve_copy_review_source_context(
    *,
    projection: Dict[str, Any],
    staged: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    pdp = projection.get("pdp") if isinstance(projection.get("pdp"), dict) else {}
    source_refs = staged.get("source_refs")
    return {
        "source_url": _first_text(
            staged.get("source_url"),
            staged.get("source_original_url"),
            payload.get("source_url"),
            payload.get("source_original_url"),
            _source_url_from_refs(source_refs),
        ),
        "catalog_brand": _first_text(
            pdp.get("brand"),
            staged.get("brand"),
            payload.get("brand"),
            payload.get("catalog_brand"),
        ),
        "catalog_title": _first_text(
            pdp.get("title"),
            staged.get("title"),
            payload.get("title"),
            payload.get("catalog_title"),
        ),
    }


@router.get("/product/{platform}/{platform_product_id}")
async def get_product_pdp_status(
    platform: str,
    platform_product_id: str,
    market: str = Query(default=DEFAULT_MARKET),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        projection = await get_pdp_projection(product_key=product_key, market=market)
        await ensure_pdp_governance_tables()
        rows = await database.fetch_all(
            """
            SELECT id, pdp_id, product_key, merchant_id, module_key, status,
                   reviewed_by_actor_type, reviewed_by_actor_id, review_decision,
                   review_notes, notes, created_at, updated_at
            FROM merchant_pdp_contributions
            WHERE merchant_id = :merchant_id
              AND product_key = :product_key
            ORDER BY created_at DESC
            LIMIT 50
            """,
            {"merchant_id": merchant_id, "product_key": product_key},
        )
        return {
            "status": "success",
            "product_key": product_key,
            "pdp": projection["pdp"],
            "modules": projection["modules"],
            "published_payload": projection["published_payload"],
            "contributions": [
                {
                    "id": row["id"],
                    "pdp_id": row["pdp_id"],
                    "product_key": row["product_key"],
                    "module_key": row["module_key"],
                    "status": row["status"],
                    "reviewed_by_actor_type": row["reviewed_by_actor_type"],
                    "reviewed_by_actor_id": row["reviewed_by_actor_id"],
                    "review_decision": row["review_decision"],
                    "review_notes": row["review_notes"],
                    "notes": row["notes"],
                    "created_at": str(row["created_at"]) if row["created_at"] else None,
                    "updated_at": str(row["updated_at"]) if row["updated_at"] else None,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        raise _map_error(exc)


@router.post("/product/{platform}/{platform_product_id}/contributions")
async def submit_product_pdp_contribution(
    platform: str,
    platform_product_id: str,
    body: MerchantContributionRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        return await create_merchant_contribution(
            product_key=product_key,
            merchant_id=merchant_id,
            module_key=body.module_key,
            payload=body.payload,
            notes=body.notes,
            market=body.market,
        )
    except Exception as exc:
        raise _map_error(exc)


class MerchantApproveRequest(BaseModel):
    module_key: str = "copy"
    market: str = DEFAULT_MARKET
    # NOTE: no caller-supplied version_id -- we always review exactly the staged
    # version the projection resolves for this merchant's product_key, so a caller
    # cannot point the approve at an arbitrary (or another merchant's) version.


@router.post("/product/{platform}/{platform_product_id}/approve")
async def approve_product_pdp_module(
    platform: str,
    platform_product_id: str,
    body: MerchantApproveRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Merchant approves a staged module; route through the LLM-reviewed GPT55
    gate. On pass for a low-risk module the gate auto-publishes, which (when
    SKU_OPT_OVERLAY_V1 is on) materializes a merchant_product_overlay row that the
    public PDP merge hook serves. Merchants are NOT direct publish authorities;
    the gate is. Failure or budget cap -> needs_human_review, nothing publishes.
    """
    if not SKU_OPT_OVERLAY_V1_ENABLED:
        raise HTTPException(status_code=404, detail="SKU_OPT_OVERLAY_V1_DISABLED")
    if body.module_key not in MERCHANT_SELF_APPROVE_MODULES:
        raise HTTPException(status_code=400, detail="MODULE_NOT_MERCHANT_APPROVABLE")

    merchant_id = _merchant_id(current_user)
    product_key = f"{merchant_id}|{platform}|{platform_product_id}"
    try:
        parse_product_key(product_key)
        projection = await get_pdp_projection(product_key=product_key, market=body.market)
        pdp_id = projection["pdp"]["pdp_id"]

        # Find the staged module the merchant is approving. get_pdp_projection
        # returns one summary per module_key with the staged version nested under
        # the "staged" key (NOT a top-level "stage" field).
        module_summary = next(
            (
                m
                for m in projection.get("modules", [])
                if m.get("module_key") == body.module_key
            ),
            None,
        )
        staged = (module_summary or {}).get("staged")
        if not staged:
            raise HTTPException(status_code=404, detail="NO_STAGED_MODULE")
        # Always review exactly this staged version (no caller-supplied id).
        version_id = staged.get("id")
        if not version_id:
            raise HTTPException(status_code=404, detail="NO_STAGED_MODULE")
        payload = staged.get("payload") if isinstance(staged.get("payload"), dict) else {}
        source_context: Dict[str, Optional[str]] = {}
        if _source_grounding_enabled():
            source_context = _resolve_copy_review_source_context(
                projection=projection,
                staged=staged,
                payload=payload,
            )
            source_context["source_text"] = await _fetch_source_text(
                source_context.get("source_url")
            )

        rubric = await generate_copy_review_rubric(
            merchant_id=merchant_id,
            payload=payload,
            source_refs=staged.get("source_refs"),
            **source_context,
        )
        if rubric is None:
            return {
                "status": "success",
                "product_key": product_key,
                "module_key": body.module_key,
                "decision": "needs_human_review",
                "published": False,
                "reason": "copy_review_unavailable",
            }

        result = await review_module_version(
            pdp_id=pdp_id,
            module_key=body.module_key,
            version_id=version_id,
            actor_type=REVIEW_ACTOR_GPT55,
            actor_id=f"merchant:{merchant_id}",
            external_rubric=rubric,
        )
        return {
            "status": "success",
            "product_key": product_key,
            "module_key": body.module_key,
            "decision": result.get("decision"),
            "published": bool(result.get("published")),
            "rubric_confidence": rubric.get("confidence"),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc)
